"""本地模拟验证，不访问中转站：python -m unittest discover -s tests -v"""

import base64
import io
import json
import sys
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from urllib.parse import unquote
from unittest.mock import patch

import httpx2
from openai import APIStatusError, OpenAI, omit
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import drawing_backend as backend
from app.drawing_app import DrawingApp
from app import gemini_backend


class DrawingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.folder = Path(self.temp.name)
        self.sketch = self.folder / "sketch.png"
        Image.new("RGB", (32, 32), "#245E49").save(self.sketch)
        self.data = self.sketch.read_bytes()
        self.item = SimpleNamespace(url=None, b64_json=base64.b64encode(self.data).decode())

    def tearDown(self):
        self.temp.cleanup()

    def request(self, *, mode: str = "generate", model: str | None = None,
                size: str = "1024x1024", image_path: Path | None = None,
                quality: str = "high") -> backend.DrawingRequest:
        return backend.DrawingRequest(
            mode=mode,
            model=model if model is not None else backend.DEFAULT_MODEL,
            prompt="test",
            api_key="fake-test-key",
            output_dir=self.folder,
            size=size,
            image_path=image_path,
            quality=quality,
        )

    def test_all_models_sizes_and_modes_are_single_requests(self):
        for mode in ("generate", "edit"):
            for model, _ in backend.MODEL_OPTIONS:
                if backend.is_nano_banana(model):
                    continue
                for size in backend.SIZE_OPTIONS.values():
                    with self.subTest(mode=mode, model=model, size=size), patch.object(backend, "OpenAI") as sdk:
                        client = sdk.return_value.__enter__.return_value
                        method = client.images.edit if mode == "edit" else client.images.generate
                        method.return_value = SimpleNamespace(data=[self.item])
                        def answer(**params):
                            if mode == "edit":
                                self.assertEqual(params["image"].read(), self.data)
                            return SimpleNamespace(data=[self.item])
                        method.side_effect = answer
                        path = backend.run_once(self.request(mode=mode, model=model, size=size,
                                                image_path=self.sketch), lambda *args: None)
                        self.assertEqual(path.read_bytes(), self.data)
                        self.assertEqual(method.call_count, 1)
                        self.assertEqual(method.call_args.kwargs["model"], model)
                        self.assertEqual(method.call_args.kwargs["size"], size)
                        self.assertEqual(method.call_args.kwargs["n"], 1)
                        self.assertEqual(method.call_args.kwargs["quality"], "high")
                        self.assertEqual(sdk.call_args.kwargs["max_retries"], 0)

    def test_503_stops_and_redacts_key(self):
        for mode in ("generate", "edit"):
            with patch.object(backend, "OpenAI") as sdk:
                client = sdk.return_value.__enter__.return_value
                method = client.images.edit if mode == "edit" else client.images.generate
                response = httpx2.Response(503, request=httpx2.Request("POST", backend.BASE_URL))
                method.side_effect = APIStatusError("busy", response=response,
                                                    body={"error": {"message": "busy fake-test-key"}})
                with self.assertRaises(backend.DrawingError) as caught:
                    backend.run_once(self.request(mode=mode, image_path=self.sketch), lambda *args: None)
                self.assertIn("503", str(caught.exception))
                self.assertNotIn("fake-test-key", str(caught.exception))
                self.assertEqual(method.call_count, 1)

    def test_quality_forwarding_and_rejection_does_not_downgrade(self):
        for mode in ("generate", "edit"):
            for quality in backend.QUALITY_OPTIONS.values():
                with patch.object(backend, "OpenAI") as sdk:
                    client = sdk.return_value.__enter__.return_value
                    method = client.images.edit if mode == "edit" else client.images.generate
                    method.return_value = SimpleNamespace(data=[self.item])
                    request = self.request(mode=mode, quality=quality, image_path=self.sketch)
                    backend.run_once(request, lambda *args: None)
                    self.assertEqual(method.call_args.kwargs["quality"], quality)
                    response = httpx2.Response(400, request=httpx2.Request("POST", backend.BASE_URL))
                    method.reset_mock()
                    method.side_effect = APIStatusError("unsupported quality", response=response,
                        body={"error": {"message": "unsupported quality", "param": "quality"}})
                    with self.assertRaises(backend.DrawingError):
                        backend.run_once(request, lambda *args: None)
                    self.assertEqual(method.call_count, 1)
                    self.assertEqual(method.call_args.kwargs["quality"], quality)

    def test_download_failure_keeps_link_without_second_api_call(self):
        with patch.object(backend, "OpenAI") as sdk, patch.object(backend, "urlopen", side_effect=TimeoutError()):
            client = sdk.return_value.__enter__.return_value
            client.images.generate.return_value = SimpleNamespace(data=[SimpleNamespace(b64_json=None,
                                                      url="https://example.com/test.png")])
            with self.assertRaises(backend.DrawingError):
                backend.run_once(self.request(), lambda *args: None)
            self.assertEqual(client.images.generate.call_count, 1)
            links = list(self.folder.glob("image_link_*.txt"))
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0].read_text(), "https://example.com/test.png")

    def test_nano_native_payload_images_and_503_are_single_requests(self):
        real_client = httpx2.Client
        for mode in ("generate", "edit"):
            for model, _ in backend.MODEL_OPTIONS:
                if not backend.is_nano_banana(model):
                    continue
                requests = []
                failed = False
                def handle(request):
                    requests.append(request)
                    self.assertEqual(unquote(request.url.path), f"/v1beta/models/{model}:generateContent")
                    payload = json.loads(request.read())
                    self.assertEqual(payload["generationConfig"]["imageConfig"], {"imageSize": "4K", "aspectRatio": "2:3"})
                    self.assertNotIn("quality", payload["generationConfig"])
                    parts = payload["contents"][0]["parts"]
                    self.assertEqual(parts[0]["text"], "test")
                    if mode == "edit":
                        self.assertEqual(base64.b64decode(parts[1]["inlineData"]["data"]), self.data)
                    else: self.assertEqual(len(parts), 1)
                    if failed:
                        return httpx2.Response(503, json={"error": {"message": "busy fake-test-key"}})
                    return httpx2.Response(200, json={"candidates": [{"content": {"parts": [
                        {"thought": True, "inlineData": {"mimeType": "image/png", "data": "not-final"}},
                        {"inlineData": {"mimeType": "image/png", "data": self.item.b64_json}}]}}]})
                def make_client(**kwargs):
                    return real_client(transport=httpx2.MockTransport(handle), **kwargs)
                native_request = backend.DrawingRequest(mode=mode, model=model, prompt="test", api_key="fake-test-key",
                    output_dir=self.folder, image_path=self.sketch, gemini_resolution="4K", gemini_aspect_ratio="2:3")
                with patch.object(gemini_backend.httpx2, "Client", side_effect=make_client), patch.object(backend, "OpenAI") as sdk:
                    path = backend.run_once(native_request, lambda *args: None)
                    self.assertEqual(path.read_bytes(), self.data)
                    sdk.assert_not_called()
                    failed = True
                    with self.assertRaises(backend.DrawingError) as caught:
                        backend.run_once(native_request, lambda *args: None)
                    self.assertIn("503", str(caught.exception))
                    self.assertNotIn("fake-test-key", str(caught.exception))
                self.assertEqual(len(requests), 2)

    def test_window_masks_key_guards_duplicate_clicks_and_previews_result(self):
        root = tk.Tk()
        root.withdraw()
        app = DrawingApp(root)
        gate = threading.Event()
        try:
            app.key.set("fake-test-key")
            app.output_dir.set(str(self.folder))
            app.prompt.insert("1.0", "test")
            self.assertEqual(backend.QUALITY_OPTIONS[app.quality.get()], "high")
            default_index = app.model_box.current()
            app.family.set("gemini")
            app.change_family()
            self.assertEqual(len(app.available_models()), 2)
            self.assertTrue(app.gemini_settings.grid_info())
            self.assertFalse(app.gpt_settings.grid_info())
            app.set_busy(True)
            app.set_busy(False)
            app.family.set("gpt")
            app.change_family()
            app.model_box.current(default_index)
            app.update_model_controls()
            self.assertTrue(app.quality_box.instate(["readonly", "!disabled"]))
            app.quality.set(next(label for label, value in backend.QUALITY_OPTIONS.items() if value == "max"))
            self.assertTrue(app.key_entry.cget("show"))
            app.show_key.set(True)
            app.toggle_key()
            self.assertEqual(app.key_entry.cget("show"), "")
            def work(request, emit):
                gate.wait(2)
                return self.sketch
            with patch("app.drawing_app.run_once", side_effect=work) as mocked:
                app.start()
                self.assertTrue(app.busy)
                self.assertTrue(app.start_button.instate(["disabled"]))
                app.start()
                gate.set()
                deadline = time.monotonic()+3
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.01)
                self.assertFalse(app.busy)
                self.assertEqual(mocked.call_count, 1)
                self.assertEqual(mocked.call_args.args[0].quality, "max")
                self.assertEqual(app.result_path, self.sketch)
                self.assertIn(app.result_canvas, app.preview_images)
                self.assertTrue(app.start_button.instate(["!disabled"]))
                app.show_error("HTTP 503\nupstream busy")
                self.assertIn("503", app.banner.cget("text"))
        finally:
            gate.set()
            root.destroy()

    def test_key_context_menu_shortcuts_and_busy_state(self):
        root = tk.Tk()
        root.withdraw()
        app = DrawingApp(root)
        try:
            app.key.set("old-key")
            app.select_key()
            with patch.object(root, "clipboard_get", return_value="  fake-pasted-key\r\n"):
                app.paste_key()
                self.assertEqual(app.key.get(), "fake-pasted-key")
                app.set_busy(True)
                self.assertTrue(app.key_entry.instate(["!disabled"]))
                app.select_key()
                app.paste_key()
                self.assertEqual(app.key.get(), "fake-pasted-key")
                app.set_busy(False)
                app.select_key()
                app.paste_key(SimpleNamespace())
                self.assertEqual(app.key.get(), "fake-pasted-key")
            self.assertTrue(app.key_entry.cget("show"))
            for sequence in ("<Control-v>", "<Control-V>", "<Shift-Insert>", "<<Paste>>"):
                self.assertTrue(app.key_entry.bind(sequence))
            with patch.object(root, "clipboard_get", side_effect=tk.TclError("no text")):
                app.paste_key()
                self.assertEqual(app.key.get(), "fake-pasted-key")
            self.assertEqual([app.key_menu.entrycget(i, "label") for i in range(4)],
                             ["粘贴", "复制", "全选", "删除"])
            app.key_entry.selection_range(0, 4)
            with patch.object(root, "clipboard_clear") as clear, patch.object(root, "clipboard_append") as append:
                app.copy_key()
                clear.assert_called_once()
                append.assert_called_once_with("fake")
            app.delete_key_selection()
            self.assertEqual(app.key.get(), "-pasted-key")
            app.select_key()
            app.delete_key_selection()
            self.assertEqual(app.key.get(), "")
        finally:
            root.destroy()

    def test_prompt_context_menu_preserves_multiline_text_and_selection(self):
        root = tk.Tk()
        root.withdraw()
        app = DrawingApp(root)
        try:
            menu = app.input_menus[app.prompt]
            self.assertEqual([menu.entrycget(i, "label") for i in range(4)],
                             ["粘贴", "复制", "全选", "删除"])
            text = "  first line\nsecond line\n"
            with patch.object(root, "clipboard_get", return_value=text):
                menu.invoke(0)
            self.assertEqual(app.prompt.get("1.0", "end-1c"), text)
            menu.invoke(2)
            with patch.object(app.prompt, "clipboard_clear"), patch.object(app.prompt, "clipboard_append") as copied:
                menu.invoke(1)
                copied.assert_called_once_with(text)
            menu.invoke(3)
            self.assertEqual(app.prompt.get("1.0", "end-1c"), "")
            app.prompt.configure(state="disabled")
            with patch.object(root, "clipboard_get", return_value="blocked"):
                menu.invoke(0)
            self.assertEqual(app.prompt.get("1.0", "end-1c"), "")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
