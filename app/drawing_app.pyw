"""无终端启动入口；启动失败时用窗口提示。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from app.drawing_app import main
    main()
except Exception as exc:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("绘图工具启动失败", f"无法启动（{type(exc).__name__}）。\n请确认 app 程序文件夹完整，且已安装 openai 与 Pillow。")
    root.destroy()
