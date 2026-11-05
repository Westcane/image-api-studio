"""打包产物的离线启动检查，不访问网络或输出密钥。"""
import json
from pathlib import Path
import sys


def run_check(report_path: Path) -> int:
    report = {}
    root = None
    try:
        import tkinter as tk
        import openai
        import httpx2
        from PIL import Image
        from app.drawing_app import DrawingApp, ROOT_DIR
        from app import gemini_backend
        from app.drawing_backend import DrawingRequest, MODEL_OPTIONS
        root = tk.Tk()
        root.withdraw()
        app = DrawingApp(root)
        app.key.set("")
        assert Path(app.output_dir.get()) == ROOT_DIR / "outputs"
        if getattr(sys, "frozen", False):
            assert ROOT_DIR == Path(sys.executable).resolve().parent
        app.family.set("gemini")
        app.change_family()
        assert len(app.available_models()) == 2
        assert app.gemini_settings.grid_info()
        request = DrawingRequest(mode="generate", model=MODEL_OPTIONS[-1][0], prompt="offline check",
                                 api_key="not-a-real-key", output_dir=ROOT_DIR / "outputs")
        assert gemini_backend.gemini_payload(request)["generationConfig"]["imageConfig"]["imageSize"] == "2K"
        assert app.prompt in app.input_menus
        report.update(ok=True, frozen=bool(getattr(sys, "frozen", False)),
                      app_directory=str(ROOT_DIR), output_directory=app.output_dir.get(),
                      model_count=len(MODEL_OPTIONS), openai_version=openai.__version__,
                      tkinter=True, pillow=True, httpx=True, gemini_native=True)
    except Exception as exc:
        report.update(ok=False, error_type=type(exc).__name__, error=str(exc))
    finally:
        if root is not None:
            root.destroy()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report.get("ok") else 1
