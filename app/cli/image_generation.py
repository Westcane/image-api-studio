"""纯文字生成图片：python app/cli/image_generation.py

也可直接指定：python app/cli/image_generation.py --prompt "夕阳下的锦鲤池，浮世绘风格"
依赖：python -m pip install --upgrade openai
"""

import argparse
import base64
import getpass
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


BASE_URL = "http://38.145.218.40:12001/v1"
MODEL = "「YS」gpt-image-2.6-sunburst-T"
SIZE_PRESETS = ("2048x2048", "2048x3072", "3072x2048", "1536x2304", "2304x1536",
                "3840x2160", "2160x3840", "1024x1024", "1536x1024", "1024x1536", "auto")
MODEL_OPTIONS = (
    ("「XXQ」gpt-image-2.5-sunburst-超", "1元/次"),
    ("「CS」gpt-image-2.5-sunburst", "2.5$/次"),
    ("「ZX」gpt-image-2.5-sunburst-high", "9元/次"),
    ("「YS」gpt-image-2.6-sunburst-Web", "1元/次"),
    ("「YS」gpt-image-2.6-sunburst-T", "4$/次"),
    ("「Rim」gemini-nano-banana-2.1", "3.5元/次"),
    ("「KS」gemini-nano-banana-2.1-count", "8元/次"),
)
DEFAULT_PROMPT = "A serene koi pond at sunset, ukiyo-e style."
ROOT_DIR = Path(__file__).resolve().parents[2]


def choose_model():
    default = next(i for i, (name, _) in enumerate(MODEL_OPTIONS, 1) if name == MODEL)
    choices = {str(number) for number in range(1, len(MODEL_OPTIONS) + 1)}
    print("请选择模型：")
    for number, (name, price) in enumerate(MODEL_OPTIONS, 1):
        label = f"（{price}）" if price else ""
        print(f"  {number}. {name}{label}")
    while True:
        choice = input(f"输入编号 1–{len(MODEL_OPTIONS)}（回车默认 {default}）：").strip() or str(default)
        if choice in choices:
            model = MODEL_OPTIONS[int(choice) - 1][0]
            print(f"已选择：{model}")
            return model
        print(f"请输入 1 至 {len(MODEL_OPTIONS)} 的编号。")


def supported_url(url):
    parsed = urlparse(url)
    return bool(parsed.netloc) and (
        parsed.scheme == "https"
        or (parsed.scheme == "http" and parsed.netloc == "38.145.218.40:12001")
    )


def masked_api_key(prompt):
    if os.name != "nt":
        return getpass.getpass(prompt)
    import msvcrt

    print(prompt, end="", flush=True)
    characters = []
    while True:
        character = msvcrt.getwch()
        if character in {"\r", "\n"}:
            print()
            return "".join(characters)
        if character in {"\x03", "\x1a"}:
            print()
            raise KeyboardInterrupt
        if character in {"\x00", "\xe0"}:
            msvcrt.getwch()
        elif character in {"\b", "\x7f"}:
            if characters:
                characters.pop()
                print("\b \b", end="", flush=True)
        elif character.isprintable():
            characters.append(character)
            print("*", end="", flush=True)


def save_image(item, output_dir):
    if getattr(item, "b64_json", None):
        data = base64.b64decode(item.b64_json, validate=True)
    elif getattr(item, "url", None):
        # 先留存下载链接；下载失败后也能恢复，无需再次请求生成。
        output_dir.mkdir(parents=True, exist_ok=True)
        link_path = output_dir / f"generation_link_{time.time_ns()}.txt"
        with link_path.open("x", encoding="utf-8") as saved_link:
            saved_link.write(item.url)
        print(f"图片链接已备份：{link_path}", flush=True)
        print(f'仅下载：python app/cli/image_generation.py --download-only "{link_path}"', flush=True)
        if not supported_url(item.url):
            raise ValueError("图片链接必须使用 HTTPS 或指定的绘图 IP。")
        print("已收到图片链接，正在下载……", flush=True)
        # 不向图片下载地址发送 API Key。
        request = Request(item.url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
        with urlopen(request, timeout=120) as response:
            if not supported_url(response.geturl()):
                raise ValueError("图片下载重定向到了不支持的地址。")
            data = response.read(100 * 1024 * 1024 + 1)
        if len(data) > 100 * 1024 * 1024:
            raise ValueError("图片超过 100 MB，未保存。")
    else:
        raise ValueError("接口未返回图片的 b64_json 或 url 字段。")

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        extension = ".jpg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        extension = ".webp"
    else:
        raise ValueError("接口返回的内容不是 PNG、JPEG 或 WebP 图片。")
    output_dir.mkdir(parents=True, exist_ok=True)
    index = 0
    while True:
        suffix = "" if index == 0 else f"_{index}"
        path = output_dir / f"generation{suffix}{extension}"
        try:
            with path.open("xb") as output:
                output.write(data)
            return path
        except FileExistsError:
            index += 1


def waiting_notice(stop, started):
    while not stop.wait(30):
        seconds = int(time.monotonic() - started)
        print(f"仍在等待接口响应，已等待 {seconds} 秒（本地计时，并非生成进度）。", flush=True)


def report_download_error(exc):
    if isinstance(exc, HTTPError):
        detail = f"图片服务器返回 HTTP {exc.code}（例如链接失效或访问被拒绝）"
    elif isinstance(exc, URLError):
        reason = exc.reason
        detail = f"图片下载连接失败（{type(reason).__name__}）"
        if isinstance(reason, OSError) and reason.errno is not None:
            detail += f"，系统错误码 {reason.errno}"
    elif isinstance(exc, TimeoutError):
        detail = "图片下载超时"
    elif isinstance(exc, PermissionError):
        detail = "没有权限写入文件或目录"
    elif isinstance(exc, ValueError):
        detail = str(exc)
    else:
        detail = f"下载或文件读写失败（{type(exc).__name__}，错误码 {exc.errno}）"
    print(detail + "。可使用已备份的链接重试下载，或在浏览器中打开链接。", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="绘画指令；不填则启动后输入")
    parser.add_argument("--model", help="直接指定完整模型名称；不填则显示选择菜单")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--size", choices=SIZE_PRESETS, default="2048x2048",
                        help="输出尺寸，默认 2048x2048；大图与 4K 为实验选项，需线路支持")
    parser.add_argument("--quality", choices=("high", "xhigh", "max", "medium", "low", "auto"),
                        default="high", help="生成画质，默认 high；xhigh/max 需线路支持")
    parser.add_argument("--gemini-resolution", choices=("1K", "2K", "4K"), default="2K")
    parser.add_argument("--gemini-ratio", default="1:1")
    parser.add_argument("--output-dir", type=Path, default=ROOT_DIR / "outputs")
    parser.add_argument("--download-only", nargs="?", const="", metavar="LINK_FILE",
                        help="只下载，不生成、不需要密钥；可指定链接备份文件，或启动后粘贴链接")
    args = parser.parse_args()
    if args.download_only is not None:
        try:
            if args.download_only:
                url = Path(args.download_only).read_text(encoding="utf-8").strip()
            else:
                url = input("粘贴已有图片链接（不会请求重新生成）：").strip()
            if not url:
                raise ValueError("图片链接不能为空")
            path = save_image(SimpleNamespace(url=url, b64_json=None), args.output_dir.resolve())
            print(f"已保存：{path}")
            return 0
        except (OSError, ValueError) as exc:
            report_download_error(exc)
            return 1
    if args.model is None:
        args.model = choose_model()
    if not supported_url(args.base_url):
        print("API 地址必须使用 HTTPS 或指定的绘图 IP。", file=sys.stderr)
        return 1
    try:
        from openai import OpenAI, APIConnectionError, APIStatusError, APITimeoutError, omit
    except ImportError:
        print("请先安装：python -m pip install --upgrade openai", file=sys.stderr)
        return 1

    prompt = args.prompt
    if prompt is None:
        prompt = input("输入绘画 prompt（回车使用夕阳锦鲤池示例）：").strip() or DEFAULT_PROMPT
    if not prompt.strip():
        print("Prompt 不能为空。", file=sys.stderr)
        return 1
    api_key = os.environ.get("SILLYDREAM_API_KEY", "").strip()
    if not api_key:
        api_key = masked_api_key("输入 API Key（显示星号，不会保存，粘贴后按回车）：").strip()
    if not api_key:
        print("未提供 API Key。", file=sys.stderr)
        return 1

    print(f"模型：{args.model}\n尺寸：{args.size}\n正在请求生成一张图片，等待超时为 10 分钟。", flush=True)
    stop = threading.Event()
    started = time.monotonic()
    notice = threading.Thread(target=waiting_notice, args=(stop, started), daemon=True)
    try:
        if "gemini-nano-banana" in args.model:
            sys.path.insert(0, str(ROOT_DIR))
            from app.drawing_backend import DrawingRequest, run_once
            path = run_once(DrawingRequest(mode="generate", model=args.model, prompt=prompt, api_key=api_key,
                output_dir=args.output_dir, base_url=args.base_url,
                gemini_resolution=args.gemini_resolution, gemini_aspect_ratio=args.gemini_ratio),
                lambda phase, text: print(text, flush=True))
            print(f"已保存：{path}")
            return 0
        with OpenAI(base_url=args.base_url, api_key=api_key, timeout=600.0, max_retries=0) as client:
            notice.start()
            try:
                result = client.images.generate(
                    model=args.model,
                    prompt=prompt,
                    size=args.size,
                    quality=omit if "gemini-nano-banana" in args.model else args.quality,
                    n=1,
                )
            finally:
                stop.set()
                notice.join()
        print(f"接口已返回，用时 {time.monotonic() - started:.1f} 秒。", flush=True)
        if not result.data:
            raise ValueError("接口返回了空的图片列表；请查看中转站请求记录。")
        for item in result.data:
            path = save_image(item, args.output_dir.resolve())
            print(f"已保存：{path}", flush=True)
    except APITimeoutError:
        print("请求超时；服务端可能仍在生成，请先查看中转站记录再重试。", file=sys.stderr)
        return 1
    except APIConnectionError:
        print("连接失败，请检查网络及 API 地址。", file=sys.stderr)
        return 1
    except APIStatusError as exc:
        tips = {
            400: "请检查模型名称、prompt 和图片尺寸。",
            401: "请检查 API Key。",
            403: "请检查模型访问权限。",
            404: "请检查 API 地址、模型名称及是否支持 /v1/images/generations。",
            429: "请检查余额或请求频率。",
        }
        print(f"接口返回 HTTP {exc.status_code}。" + tips.get(exc.status_code, "请查看中转站请求记录。"), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        report_download_error(exc)
        return 1
    except Exception as exc:
        # 不直接打印第三方原始响应，避免输出密钥或完整 Base64 图片。
        print(f"处理失败（{type(exc).__name__}），中转站可能返回了非标准数据。", file=sys.stderr)
        return 1
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n已停止本地等待；已提交的请求请到中转站查看状态。", file=sys.stderr)
        sys.exit(1)
