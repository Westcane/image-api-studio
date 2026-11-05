"""独立 EXE 入口。"""
from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if len(sys.argv) == 3 and sys.argv[1] == "--self-check":
    from app.packaging_check import run_check
    sys.exit(run_check(Path(sys.argv[2])))

try:
    from app.drawing_app import main
    main()
except Exception as exc:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("绘图小屋启动失败", f"启动失败（{type(exc).__name__}）：{exc}")
    root.destroy()
