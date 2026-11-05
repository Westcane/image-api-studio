"""本地桌面绘图工具。双击根目录的启动绘图工具.vbs。"""

import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.drawing_backend import (BASE_URL, DEFAULT_MODEL, MODEL_OPTIONS, SIZE_OPTIONS, QUALITY_OPTIONS, DrawingError,
                                 GEMINI_RESOLUTIONS, GEMINI_RATIOS, DrawingRequest,
                                 is_nano_banana, run_once, safe_message, validate)

ROOT_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent)
BG = "#F4F2EC"
INK = "#272C29"
MUTED = "#6D766F"
GREEN = "#245E49"


class DrawingApp:
    def __init__(self, root):
        self.root = root
        root.title("绘图小屋 · Image Studio")
        root.geometry("1280x860")
        root.minsize(1150, 780)
        root.configure(bg=BG)
        self.events = queue.Queue()
        self.busy = False
        self.phase = "准备就绪"
        self.started = 0
        self.result_path = None
        self.sketch_path = None
        self.preview_images = {}
        self.controls = []
        self.input_menus = {}
        self.mode = tk.StringVar(value="generate")
        self.family = tk.StringVar(value="gpt")
        default_index = next(index for index, (name, _) in enumerate(self.available_models()) if name == DEFAULT_MODEL)
        self.model = tk.StringVar(value=self.model_labels()[default_index])
        self.size = tk.StringVar(value=next(iter(SIZE_OPTIONS)))
        self.quality = tk.StringVar(value=next(iter(QUALITY_OPTIONS)))
        self.gemini_resolution = tk.StringVar(value="2K")
        self.gemini_ratio = tk.StringVar(value="1:1")
        self.key = tk.StringVar(value=os.environ.get("SILLYDREAM_API_KEY", ""))
        self.show_key = tk.BooleanVar(value=False)
        self.output_dir = tk.StringVar(value=str(ROOT_DIR / "outputs"))
        self.base_url = tk.StringVar(value=BASE_URL)
        self.timeout = tk.StringVar(value="600")
        self.status = tk.StringVar(value="准备就绪")
        self.timer = tk.StringVar(value="每次点击仅提交一次")
        self.sketch_name = tk.StringVar(value="尚未选择草稿")
        self._style()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def available_models(self):
        return [(name, price) for name, price in MODEL_OPTIONS
                if is_nano_banana(name) == (self.family.get() == "gemini")]

    def model_labels(self):
        return [f"{name}  ·  {price}" for name, price in self.available_models()]

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure("TLabel", background=BG, foreground=INK, font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 23, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8))
        style.configure("Go.TButton", background=GREEN, foreground="white", padding=12,
                        font=("Microsoft YaHei UI", 12, "bold"))
        style.map("Go.TButton", background=[("disabled", "#D9DED9"), ("active", "#34785E")],
                  foreground=[("disabled", "#7D877F")])
        style.configure("TRadiobutton", background=BG, font=("Microsoft YaHei UI", 11), padding=5)
        style.configure("TCheckbutton", background=BG, font=("Microsoft YaHei UI", 9))
        style.configure("TEntry", padding=6)
        style.configure("TCombobox", padding=6, font=("Microsoft YaHei UI", 9))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9), font=("Microsoft YaHei UI", 10))
        # 覆盖 clam 主题自带的 selected padding，避免选中项反而显得更小。
        style.map("TNotebook.Tab",
                  padding=[("selected", (20, 13)), ("!selected", (18, 7))],
                  background=[("selected", "#FFFFFF"), ("!selected", "#E3E7E0")],
                  foreground=[("selected", GREEN), ("!selected", MUTED)],
                  font=[("selected", ("Microsoft YaHei UI", 11, "bold")),
                        ("!selected", ("Microsoft YaHei UI", 10))])

    def register(self, widget, state="normal"):
        self.controls.append((widget, state))
        if isinstance(widget, (tk.Text, ttk.Entry)) and not isinstance(widget, ttk.Combobox):
            self.attach_input_menu(widget)
        return widget

    def attach_input_menu(self, widget):
        menu = tk.Menu(self.root, tearoff=False)
        self.input_menus[widget] = menu
        is_text = isinstance(widget, tk.Text)

        def selection():
            try:
                return widget.index("sel.first"), widget.index("sel.last")
            except tk.TclError:
                return None

        def editable():
            return str(widget.cget("state")) not in {"disabled", "readonly"}

        def delete_selection():
            indexes = selection()
            if editable() and indexes:
                widget.delete(*indexes)

        def paste(event=None):
            if not editable():
                return "break"
            try:
                text = self.root.clipboard_get()
            except tk.TclError:
                return "break"
            delete_selection()
            widget.insert("insert", text)
            return "break"

        def copy():
            indexes = selection()
            if indexes:
                text = widget.get(*indexes) if is_text else widget.get()[indexes[0]:indexes[1]]
                widget.clipboard_clear()
                widget.clipboard_append(text)

        def select_all():
            if is_text:
                widget.tag_add("sel", "1.0", "end-1c")
                widget.mark_set("insert", "end-1c")
            else:
                widget.selection_range(0, "end")
                widget.icursor("end")

        for label, command in (("粘贴", paste), ("复制", copy), ("全选", select_all), ("删除", delete_selection)):
            menu.add_command(label=label, command=command)

        def popup(event):
            widget.focus_set()
            selected = selection() is not None
            menu.entryconfigure("粘贴", state="normal" if editable() else "disabled")
            menu.entryconfigure("复制", state="normal" if selected else "disabled")
            menu.entryconfigure("删除", state="normal" if editable() and selected else "disabled")
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return "break"

        widget.bind("<Button-3>", popup)
        for sequence in ("<Control-v>", "<Control-V>", "<Shift-Insert>", "<<Paste>>"):
            widget.bind(sequence, paste)

    def _build(self):
        shell = ttk.Frame(self.root, padding=(26, 18))
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text="绘图小屋", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="IMAGE STUDIO / api可访问 https://wish.sillydream.top/", style="Muted.TLabel").pack(side="left", padx=18, pady=(10, 0))
        family_bar = ttk.Frame(shell)
        family_bar.pack(fill="x", pady=(0, 12))
        for text, value in (("GPT Image", "gpt"), ("Nano Banana / Gemini", "gemini")):
            self.register(ttk.Radiobutton(family_bar, text=text, variable=self.family, value=value,
                                         command=self.change_family)).pack(side="left", padx=(0, 20))
        body = ttk.Frame(shell)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=2, minsize=300)
        body.columnconfigure(2, weight=3, minsize=360)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body, width=330)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        modes = ttk.Frame(left)
        modes.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        for text, value in (("文生图", "generate"), ("图生图", "edit")):
            self.register(ttk.Radiobutton(modes, text=text, variable=self.mode, value=value,
                                         command=self.change_mode)).pack(side="left", padx=(0, 18))
        ttk.Label(left, text="01  模型与连接", style="Section.TLabel").grid(row=1, column=0, sticky="w")
        self.model_box = self.register(ttk.Combobox(left, textvariable=self.model, values=self.model_labels(),
                                                   state="readonly", width=41), "readonly")
        self.model_box.grid(row=2, column=0, sticky="ew", pady=(7, 5))
        self.model_box.bind("<<ComboboxSelected>>", lambda event: self.update_model_controls())
        ttk.Label(left, text="价格按你提供的每次费用标注", style="Muted.TLabel").grid(row=3, column=0, sticky="w")
        key_header = ttk.Frame(left)
        key_header.grid(row=4, column=0, sticky="ew", pady=(10, 3))
        ttk.Label(key_header, text="API Key").pack(side="left")
        ttk.Checkbutton(key_header, text="显示", variable=self.show_key,
                        command=self.toggle_key).pack(side="right")
        # 任务提交时已复制密钥到 DrawingRequest，此处允许继续编辑供下一次使用。
        self.key_entry = ttk.Entry(left, textvariable=self.key, show="●")
        self.key_entry.grid(row=5, column=0, sticky="ew")
        for sequence in ("<Control-v>", "<Control-V>", "<Shift-Insert>", "<<Paste>>"):
            self.key_entry.bind(sequence, self.paste_key)
        self.key_menu = tk.Menu(self.root, tearoff=False)
        self.key_menu.add_command(label="粘贴", command=self.paste_key)
        self.key_menu.add_command(label="复制", command=self.copy_key)
        self.key_menu.add_command(label="全选", command=self.select_key)
        self.key_menu.add_command(label="删除", command=self.delete_key_selection)
        self.key_entry.bind("<Button-3>", self.show_key_menu)
        ttk.Label(left, text="支持粘贴 · 仅本次使用，不写入文件", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=(4, 10))
        size_row = ttk.Frame(left)
        size_row.grid(row=7, column=0, sticky="ew", pady=(0, 12))
        self.gpt_settings = size_row
        ttk.Label(size_row, text="输出尺寸").pack(anchor="w", pady=(0, 4))
        self.register(ttk.Combobox(size_row, textvariable=self.size, values=list(SIZE_OPTIONS),
                                  state="readonly", height=12), "readonly").pack(fill="x")
        ttk.Label(size_row, text="大图与 4K 属实验尺寸，需当前线路支持。",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        quality_row = ttk.Frame(size_row)
        quality_row.pack(fill="x", pady=(9, 0))
        ttk.Label(quality_row, text="GPT 画质").pack(side="left")
        self.quality_box = self.register(ttk.Combobox(quality_row, textvariable=self.quality, values=list(QUALITY_OPTIONS),
                                                     state="readonly", width=20), "readonly")
        self.quality_box.pack(side="right")
        self.gemini_settings = ttk.Frame(left)
        self.gemini_settings.grid(row=7, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(self.gemini_settings, text="Gemini 分辨率档位", style="Section.TLabel").pack(anchor="w")
        self.register(ttk.Combobox(self.gemini_settings, textvariable=self.gemini_resolution,
                                  values=GEMINI_RESOLUTIONS, state="readonly"), "readonly").pack(fill="x", pady=(6, 10))
        ttk.Label(self.gemini_settings, text="画面比例").pack(anchor="w")
        self.register(ttk.Combobox(self.gemini_settings, textvariable=self.gemini_ratio,
                                  values=GEMINI_RATIOS, state="readonly"), "readonly").pack(fill="x", pady=(6, 6))
        ttk.Label(self.gemini_settings, text="2K 默认 · 4K 高分辨率\n使用 Gemini 原生图片配置。", style="Muted.TLabel").pack(anchor="w")
        self.gemini_settings.grid_remove()
        editor = ttk.Frame(body)
        editor.grid(row=0, column=1, sticky="nsew", padx=(0, 18))
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(1, weight=1)
        ttk.Label(editor, text="02  绘画指令 / Prompt", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        prompt_box = ttk.Frame(editor)
        prompt_box.grid(row=1, column=0, sticky="nsew")
        self.prompt = self.register(tk.Text(prompt_box, height=20, width=26, wrap="word", font=("Microsoft YaHei UI", 12),
                                           bg="white", fg=INK, relief="solid", bd=1, padx=10, pady=9,
                                           undo=True, highlightthickness=0))
        self.prompt.pack(side="left", fill="both", expand=True)
        prompt_scroll = ttk.Scrollbar(prompt_box, orient="vertical", command=self.prompt.yview)
        prompt_scroll.pack(side="right", fill="y")
        self.prompt.configure(yscrollcommand=prompt_scroll.set)
        left.rowconfigure(9, weight=1)
        ttk.Label(editor, text="支持多段长指令。",
                  style="Muted.TLabel", wraplength=300).grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.sketch_frame = ttk.Frame(editor)
        self.sketch_frame.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        self.register(ttk.Button(self.sketch_frame, text="选择草稿图片…", command=self.select_sketch)).pack(side="left")
        ttk.Label(self.sketch_frame, textvariable=self.sketch_name, style="Muted.TLabel", wraplength=200).pack(side="left", padx=8)
        self.sketch_frame.grid_remove()
        out_row = ttk.Frame(left)
        out_row.grid(row=12, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(out_row, text="保存位置", style="Section.TLabel").pack(side="left")
        self.register(ttk.Button(out_row, text="选择…", command=self.select_output)).pack(side="right")
        self.register(ttk.Entry(left, textvariable=self.output_dir)).grid(row=13, column=0, sticky="ew", pady=(5, 8))
        self.register(ttk.Button(left, text="连接设置 ▾", command=self.toggle_settings)).grid(row=14, column=0, sticky="w")
        self.settings = ttk.Frame(left)
        self.settings.grid(row=15, column=0, sticky="ew", pady=5)
        self.settings.columnconfigure(1, weight=1)
        ttk.Label(self.settings, text="接口地址").grid(row=0, column=0, padx=(0, 5))
        self.register(ttk.Entry(self.settings, textvariable=self.base_url)).grid(row=0, column=1, sticky="ew")
        ttk.Label(self.settings, text="超时 / 秒").grid(row=1, column=0, padx=(0, 5), pady=5)
        self.register(ttk.Entry(self.settings, textvariable=self.timeout, width=8)).grid(row=1, column=1, sticky="w")
        self.settings.grid_remove()
        self.start_button = ttk.Button(left, text="开始生成 · 仅一次", style="Go.TButton", command=self.start)
        self.start_button.grid(row=16, column=0, sticky="ew", pady=(8, 0))

        right = ttk.Frame(body)
        right.grid(row=0, column=2, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        # 状态与错误放在预览上方，失败时无需翻找日志。
        self.banner = tk.Label(right, text="准备好后，点击左侧的开始生成。", anchor="w", justify="left",
                               bg="#E4EBE5", fg=GREEN, font=("Microsoft YaHei UI", 10), padx=13, pady=11,
                               wraplength=360)
        self.banner.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.tabs = ttk.Notebook(right)
        self.tabs.grid(row=1, column=0, sticky="nsew")
        self.result_canvas = tk.Canvas(self.tabs, bg="#FFFFFF", highlightthickness=0)
        self.sketch_canvas = tk.Canvas(self.tabs, bg="#FFFFFF", highlightthickness=0)
        self.tabs.add(self.result_canvas, text="生成结果")
        self.tabs.add(self.sketch_canvas, text="草稿预览")
        for canvas in (self.result_canvas, self.sketch_canvas):
            canvas.bind("<Configure>", lambda event, c=canvas: self.draw_preview(c))
        actions = ttk.Frame(right)
        actions.grid(row=2, column=0, sticky="ew", pady=(10, 10))
        self.open_button = ttk.Button(actions, text="打开图片", state="disabled", command=self.open_result)
        self.open_button.pack(side="left")
        self.save_button = ttk.Button(actions, text="另存为…", state="disabled", command=self.save_as)
        self.save_button.pack(side="left", padx=8)
        ttk.Button(actions, text="打开输出文件夹", command=self.open_output).pack(side="right")
        ttk.Label(right, text="任务记录", style="Section.TLabel").grid(row=3, column=0, sticky="w")
        self.log = tk.Text(right, height=5, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9),
                           bg="#EAEDE7", fg=INK, relief="flat", padx=10, pady=8)
        self.log.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        footer = ttk.Frame(shell)
        footer.pack(fill="x", pady=(13, 0))
        ttk.Label(footer, textvariable=self.status, style="Section.TLabel").pack(side="left")
        ttk.Label(footer, textvariable=self.timer, style="Muted.TLabel").pack(side="right")

    def toggle_key(self):
        self.key_entry.configure(show="" if self.show_key.get() else "●")

    def paste_key(self, event=None):
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            self.status.set("剪贴板没有可粘贴的文字，请先复制 API Key。")
            return "break"
        if not text:
            self.status.set("剪贴板文字为空，请先复制 API Key。")
            return "break"
        if self.key_entry.selection_present():
            self.key_entry.delete("sel.first", "sel.last")
        self.key_entry.insert("insert", text)
        self.status.set("密钥已粘贴，将用于下一次请求。" if self.busy else "密钥已粘贴。")
        return "break"

    def select_key(self):
        self.key_entry.selection_range(0, "end")
        self.key_entry.icursor("end")

    def copy_key(self):
        if self.key_entry.selection_present():
            first = self.key_entry.index("sel.first")
            last = self.key_entry.index("sel.last")
            self.root.clipboard_clear()
            self.root.clipboard_append(self.key.get()[first:last])

    def delete_key_selection(self):
        if self.key_entry.selection_present():
            self.key_entry.delete("sel.first", "sel.last")

    def show_key_menu(self, event):
        self.key_entry.focus_set()
        selected = self.key_entry.selection_present()
        self.key_menu.entryconfigure("复制", state="normal" if selected else "disabled")
        self.key_menu.entryconfigure("删除", state="normal" if selected else "disabled")
        self.key_menu.entryconfigure("全选", state="normal" if self.key.get() else "disabled")
        try:
            self.key_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.key_menu.grab_release()
        return "break"

    def update_model_controls(self):
        self.quality_box.configure(state="disabled" if self.busy else "readonly")

    def change_family(self):
        self.model_box.configure(values=self.model_labels())
        self.model_box.current(0)
        if self.family.get() == "gemini":
            self.gpt_settings.grid_remove()
            self.gemini_settings.grid()
        else:
            self.gemini_settings.grid_remove()
            self.gpt_settings.grid()

    def toggle_settings(self):
        if self.settings.winfo_ismapped():
            self.settings.grid_remove()
        else:
            self.settings.grid()

    def change_mode(self):
        if self.mode.get() == "edit":
            self.sketch_frame.grid()
            self.start_button.configure(text="开始生成 · 仅一次")
        else:
            self.sketch_frame.grid_remove()
            self.start_button.configure(text="开始生成 · 仅一次")

    def select_sketch(self):
        path = filedialog.askopenfilename(title="选择 Procreate 导出的草稿", initialdir=ROOT_DIR / "sketches",
                                         filetypes=[("图片", "*.png *.jpg *.jpeg *.webp")])
        if path:
            try:
                self.load_preview(self.sketch_canvas, Path(path))
            except Exception:
                messagebox.showerror("无法打开草稿", "无法读取这张图片，请使用 PNG、JPG 或 WebP。")
                return
            self.sketch_path = Path(path)
            self.sketch_name.set(Path(path).name)
            self.tabs.select(self.sketch_canvas)

    def select_output(self):
        path = filedialog.askdirectory(title="选择图片输出文件夹")
        if path:
            self.output_dir.set(path)

    def load_preview(self, canvas, path):
        with Image.open(path) as original:
            image = ImageOps.exif_transpose(original)
            dimensions = image.size
            image.thumbnail((1800, 1800))
            self.preview_images[canvas] = image.convert("RGBA")
        self.draw_preview(canvas)
        return dimensions

    def draw_preview(self, canvas):
        width, height = canvas.winfo_width(), canvas.winfo_height()
        canvas.delete("all")
        if width < 10 or height < 10:
            return
        if canvas not in self.preview_images:
            title = "画面将在这里出现" if canvas is self.result_canvas else "草稿预览"
            caption = "输入绘画指令，开始你的下一张画。" if canvas is self.result_canvas else "在图生图模式中，点击「选择草稿图片」。"
            canvas.create_rectangle(width/2-34, height/2-78, width/2+34, height/2-20, outline="#CAD5CC", width=2)
            canvas.create_text(width/2, height/2+10, text=title, fill=MUTED, font=("Microsoft YaHei UI", 17))
            canvas.create_text(width/2, height/2+45, text=caption, fill="#88938B", font=("Microsoft YaHei UI", 10))
            return
        image = self.preview_images[canvas].copy()
        image.thumbnail((max(1, width-30), max(1, height-30)), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        canvas.photo = photo
        canvas.create_image(width/2, height/2, image=photo)

    def log_message(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_busy(self, busy):
        self.busy = busy
        for widget, state in self.controls:
            widget.configure(state="disabled" if busy else state)
        self.start_button.configure(state="disabled" if busy else "normal")
        self.update_model_controls()

    def start(self):
        if self.busy:
            return
        try:
            if not self.output_dir.get().strip():
                raise DrawingError("请选择图片保存文件夹。")
            request = DrawingRequest(mode=self.mode.get(), model=self.available_models()[self.model_box.current()][0],
                                     prompt=self.prompt.get("1.0", "end").strip(), api_key=self.key.get().strip(),
                                     output_dir=Path(self.output_dir.get()).expanduser(), size=SIZE_OPTIONS[self.size.get()],
                                     image_path=self.sketch_path, base_url=self.base_url.get().strip(),
                                     timeout=float(self.timeout.get()), quality=QUALITY_OPTIONS[self.quality.get()],
                                     gemini_resolution=self.gemini_resolution.get(), gemini_aspect_ratio=self.gemini_ratio.get())
            validate(request)
        except Exception as exc:
            text = str(exc) if isinstance(exc, DrawingError) else "请检查图片、尺寸、超时及模型设置。"
            self.show_error(text)
            return
        self.started = time.monotonic()
        self.phase = "准备提交"
        self.set_busy(True)
        self.result_path = None
        self.preview_images.pop(self.result_canvas, None)
        self.draw_preview(self.result_canvas)
        self.open_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        self.tabs.select(self.result_canvas)
        self.banner.configure(text="正在处理本次请求，请稍候。", bg="#E4EBE5", fg=GREEN)
        quality_label = (f"Gemini {request.gemini_resolution} · {request.gemini_aspect_ratio}"
                         if is_nano_banana(request.model) else f"{request.size} · quality={request.quality}")
        self.log_message(f"{'图生图' if request.mode == 'edit' else '文生图'} · {request.model} · {request.size} · {quality_label} · 单次提交")
        threading.Thread(target=self.worker, args=(request,), daemon=True).start()

    def worker(self, request):
        try:
            path = run_once(request, lambda phase, text: self.events.put(("phase", (phase, text))))
            self.events.put(("done", path))
        except Exception as exc:
            if isinstance(exc, DrawingError):
                text = str(exc)
            elif isinstance(exc, PermissionError):
                text = "没有权限读写文件，请检查草稿或输出文件夹。"
            else:
                text = f"处理失败（{type(exc).__name__}）：{safe_message(exc, request.api_key)}"
            self.events.put(("error", text))

    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "phase":
                    self.phase, text = payload
                    self.banner.configure(text=text, bg="#E4EBE5", fg=GREEN)
                    self.log_message(text)
                elif kind == "done":
                    self.set_busy(False)
                    self.result_path = payload
                    self.status.set("已完成")
                    self.banner.configure(text=f"已保存：{payload}", bg="#E4EBE5", fg=GREEN)
                    self.log_message(f"图片已保存：{payload}")
                    self.open_button.configure(state="normal")
                    self.save_button.configure(state="normal")
                    try:
                        width, height = self.load_preview(self.result_canvas, payload)
                        self.banner.configure(text=f"实际尺寸：{width} × {height} 像素\n已保存：{payload}")
                    except Exception:
                        self.log_message("图片已保存，窗口预览加载失败，可点击打开图片查看。")
                elif kind == "error":
                    self.set_busy(False)
                    self.show_error(payload)
        except queue.Empty:
            pass
        if self.busy:
            elapsed = int(time.monotonic()-self.started)
            self.status.set(self.phase)
            self.timer.set(f"已等待 {elapsed//60:02d}:{elapsed%60:02d} · 本地计时")
        elif self.started:
            self.timer.set(f"本轮用时 {time.monotonic()-self.started:.1f} 秒 · 未自动重试")
            self.started = 0
        self.root.after(100, self.poll)

    def show_error(self, text):
        self.status.set("本轮未完成")
        self.banner.configure(text=text, bg="#F5E5DE", fg="#963C27")
        self.log_message(text)

    def open_result(self):
        if self.result_path:
            os.startfile(self.result_path)

    def open_output(self):
        try:
            folder = Path(self.output_dir.get()).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
        except OSError:
            messagebox.showerror("无法打开文件夹", "请检查输出路径与文件夹权限。")

    def save_as(self):
        if not self.result_path:
            return
        suffix = self.result_path.suffix
        target = filedialog.asksaveasfilename(title="另存生成图片", initialfile=self.result_path.name,
                                            defaultextension=suffix, filetypes=[("图片", "*"+suffix)])
        if target:
            try:
                if Path(target).resolve() != self.result_path.resolve():
                    shutil.copy2(self.result_path, target)
            except OSError:
                messagebox.showerror("保存失败", "请检查目标文件夹权限。")

    def close(self):
        if self.busy and not messagebox.askyesno("任务仍在进行", "关闭窗口会结束本地等待，服务器可能仍在处理或计费。\n确定关闭吗？"):
            return
        self.key.set("")
        self.root.destroy()


def main():
    root = tk.Tk()
    DrawingApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
