"""上传 Procreate 导出的草稿，通过 /images/edits 编辑并保存图片。

安装：python -m pip install --upgrade openai
运行：python app/cli/image_edit.py
或：  python app/cli/image_edit.py "草稿.png" --prompt "保留构图，完成赛璐璐上色"
"""

import argparse
import base64
import binascii
import getpass
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse
from urllib.request import urlopen


BASE_URL = "http://38.145.218.40:12001/v1"
MODEL = "「CS」gpt-image-2.5-sunburst"
SIZE_PRESETS = ("2048x2048", "2048x3072", "3072x2048", "1536x2304", "2304x1536",
                "3840x2160", "2160x3840", "1024x1024", "1536x1024", "1024x1536", "auto")
MODEL_OPTIONS = (
    ("「XXQ」gpt-image-2.5-sunburst-超", "1$/次"),
    ("「CS」gpt-image-2.5-sunburst", "2.5$/次"),
    ("「ZX」gpt-image-2.5-sunburst-high", "9$/次"),
    ("「YS」gpt-image-2.6-sunburst-Web", "1$/次"),
    ("「YS」gpt-image-2.6-sunburst-T", "4$/次"),
    ("「Rim」gemini-nano-banana-2.1", "3.5元/次"),
    ("「KS」gemini-nano-banana-2.1-count", "8元/次"),
)
DEFAULT_PROMPT = (
    "请根据这张草稿完成日式动画风格插画。"
    "保留人物原有的姿势、构图、五官、发型和服装设计。"
    "细化线条并完成赛璐璐上色。"
)
ROOT_DIR = Path(__file__).resolve().parents[2]


def is_nano_banana(model: str) -> bool:
    return "gemini-nano-banana" in model


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
    """Windows 终端逐字符读取密钥，只回显星号；支持粘贴和退格。"""
    if os.name != "nt":
        return getpass.getpass(prompt + "（当前系统使用隐藏输入）")

    import msvcrt

    print(prompt, end="", flush=True)
    characters = []
    while True:
        character = msvcrt.getwch()
        if character in {"\r", "\n"}:
            print()
            return "".join(characters)
        if character == "\x03":  # Ctrl+C
            print()
            raise KeyboardInterrupt
        if character == "\x1a":  # Ctrl+Z
            print()
            raise EOFError
        if character in {"\x00", "\xe0"}:  # 方向键等扩展按键
            msvcrt.getwch()
            continue
        if character in {"\b", "\x7f"}:
            if characters:
                characters.pop()
                print("\b \b", end="", flush=True)
        elif character.isprintable():
            characters.append(character)
            print("*", end="", flush=True)


def image_extension(data):
    """按文件内容判断格式，避免把 JPEG 保存成 PNG。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("收到的内容不是 PNG、JPEG 或 WebP 图片，未保存。")


def image_bytes(item):
    if item.b64_json:
        try:
            return base64.b64decode(item.b64_json, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("接口返回的 Base64 图片无法解码。") from exc
    if item.url:
        if not supported_url(item.url):
            raise ValueError("图片链接必须使用 HTTPS 或指定的绘图 IP。")
        # 独立下载，不把中转站 API Key 发给图片链接所在的服务器。
        with urlopen(item.url, timeout=120) as response:
            if not supported_url(response.geturl()):
                raise ValueError("图片链接重定向到了不支持的地址。")
            data = response.read(100 * 1024 * 1024 + 1)
        if len(data) > 100 * 1024 * 1024:
            raise ValueError("返回图片超过 100 MB，未保存。")
        return data
    raise ValueError("接口未返回 b64_json 或 url；中转站可能不兼容图片编辑接口。")


def save_image(data, output_dir):
    extension = image_extension(data)
    output_dir.mkdir(parents=True, exist_ok=True)
    number = 0
    while True:
        suffix = "" if number == 0 else f"_{number}"
        target = output_dir / f"result{suffix}{extension}"
        try:
            # 使用独占创建，保留之前生成的图片。
            with target.open("xb") as output:
                output.write(data)
            return target
        except FileExistsError:
            number += 1


def diagnose_error(exc, api_key):
    """保留有限的错误字段，避免把完整请求、图片或密钥输出到日志。"""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        body = body.get("error", body)
    if not isinstance(body, dict):
        body = {"message": body if isinstance(body, str) else "服务商未提供结构化错误详情"}
    details = {}
    for field in ("code", "type", "message", "param"):
        value = body.get(field)
        if isinstance(value, (str, int)):
            value = str(value).replace(api_key, "[密钥已隐藏]") if api_key else str(value)
            value = re.sub(r"https?://\S+", "[链接已隐藏]", value)
            value = re.sub(r"[A-Za-z0-9+/=_-]{48,}", "[长数据已隐藏]", value)
            value = " ".join(value.split())[:600]
            details[field] = value
    text = " ".join(details.values()).lower()
    explicit_unsupported = (
        details.get("code", "").lower() in {"unsupported_endpoint", "image_edit_not_supported"}
        or (any(word in text for word in ("not supported", "does not support", "unsupported", "不支持"))
            and any(word in text for word in ("image edit", "images/edits", "图生图", "图片编辑")))
    )
    if explicit_unsupported:
        conclusion = "服务商明确报告当前模型或线路不支持图片编辑；请联系服务商确认可用线路。"
    elif exc.status_code in {502, 503, 504}:
        conclusion = "服务端或上游暂不可用；可能拥堵、故障或无可用渠道，不能据此判定是否支持图生图。"
    elif exc.status_code in {404, 405, 501}:
        conclusion = "可能是地址、模型或接口未开放；单凭状态码不能确定模型不支持图生图。"
    else:
        conclusion = "本次调用失败；请结合错误详情检查参数、权限和余额，尚不能验证图生图能力。"
    return {"http_status": exc.status_code, "details": details,
            "conclusion": conclusion, "explicit_unsupported": explicit_unsupported}


def request_edit(client, image_path, model, prompt, size, api_key, report_path, quality="high"):
    from openai import APIStatusError, omit

    print("提交一次图片编辑请求；失败后直接结束。", flush=True)
    try:
        with image_path.open("rb") as image:
            result = client.images.edit(model=model, image=image, prompt=prompt, size=size,
                                        quality=omit if is_nano_banana(model) else quality, n=1)
        record = {"attempt": 1, "result": "接口已响应，仍需检查返回图片"}
    except APIStatusError as exc:
        record = {"attempt": 1, **diagnose_error(exc, api_key)}
        report_path.write_text(json.dumps([record], ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"HTTP {exc.status_code}：{record['conclusion']}", file=sys.stderr)
        for key, value in record["details"].items():
            print(f"  {key}: {value}", file=sys.stderr)
        raise
    report_path.write_text(json.dumps([record], ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", help="草稿路径（PNG/JPG/WebP）")
    parser.add_argument("--prompt", help="绘画指令；不填则启动后输入")
    parser.add_argument("--model", help="直接指定完整模型名称；不填则显示选择菜单")
    parser.add_argument("--base-url", default=BASE_URL, help="中转站 API 地址")
    parser.add_argument("--output-dir", type=Path, default=ROOT_DIR / "outputs")
    parser.add_argument("--size", choices=SIZE_PRESETS, default="2048x2048",
                        help="输出尺寸，默认 2048x2048；大图与 4K 为实验选项，需线路支持")
    parser.add_argument("--quality", choices=("high", "xhigh", "max", "medium", "low", "auto"),
                        default="high", help="生成画质，默认 high；xhigh/max 需线路支持")
    parser.add_argument("--gemini-resolution", choices=("1K", "2K", "4K"), default="2K")
    parser.add_argument("--gemini-ratio", default="1:1")
    args = parser.parse_args()
    if args.model is None:
        args.model = choose_model()

    image_path = args.image
    if image_path is None:
        image_path = input("草稿路径（可拖入图片，回车使用 sketches/sketch.png）：").strip()
        image_path = image_path or str(ROOT_DIR / "sketches" / "sketch.png")
    image_path = Path(image_path.strip().strip('"').strip("'")).expanduser().resolve()
    if not image_path.is_file():
        print(f"找不到草稿：{image_path}", file=sys.stderr)
        return 1
    if image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        print("请先从 Procreate 导出 PNG、JPG 或 WebP 图片；不能直接上传 .procreate 文件。", file=sys.stderr)
        return 1
    if not 0 < image_path.stat().st_size < 50 * 1024 * 1024:
        print("草稿必须非空且小于 50 MB。", file=sys.stderr)
        return 1
    if not supported_url(args.base_url):
        print("API 地址必须使用 HTTPS 或指定的绘图 IP。", file=sys.stderr)
        return 1

    try:
        from openai import OpenAI, APIConnectionError, APIStatusError, APITimeoutError
    except ImportError:
        print("请先安装依赖：python -m pip install --upgrade openai", file=sys.stderr)
        return 1

    prompt = args.prompt
    if prompt is None:
        prompt = input("绘画指令（回车使用默认的保留构图、细化线条和赛璐璐上色）：").strip()
        prompt = prompt or DEFAULT_PROMPT
    if not prompt.strip():
        print("绘画指令不能为空。", file=sys.stderr)
        return 1
    api_key = os.environ.get("SILLYDREAM_API_KEY", "").strip()
    if not api_key:
        api_key = masked_api_key("输入中转站 API Key（显示星号，不会保存，粘贴后按回车）：").strip()
    if not api_key:
        print("未提供 API Key。", file=sys.stderr)
        return 1

    print(f"提交草稿：{image_path.name}\n模型：{args.model}\n正在编辑，请耐心等待……")
    try:
        if is_nano_banana(args.model):
            sys.path.insert(0, str(ROOT_DIR))
            from app.drawing_backend import DrawingRequest, run_once
            path = run_once(DrawingRequest(mode="edit", model=args.model, prompt=prompt, api_key=api_key,
                output_dir=args.output_dir, image_path=image_path, base_url=args.base_url,
                gemini_resolution=args.gemini_resolution, gemini_aspect_ratio=args.gemini_ratio),
                lambda phase, text: print(text, flush=True))
            print(f"已保存：{path}")
            return 0
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / f"edit_diagnostic_{time.time_ns()}.json"
        print(f"诊断报告：{report_path}", flush=True)
        # 固定单次请求，关闭 SDK 隐式重试。
        with OpenAI(base_url=args.base_url, api_key=api_key, timeout=600.0, max_retries=0) as client:
            result = request_edit(client, image_path, args.model, prompt,
                                  args.size, api_key, report_path, quality=args.quality)
        if not result.data:
            raise ValueError("接口没有返回图片数据；请检查中转站是否支持 /v1/images/edits。")
        for item in result.data:
            target = save_image(image_bytes(item), args.output_dir.resolve())
            print(f"已保存：{target}")
        print("验证结果：本次图片编辑接口成功返回图片；请查看成图是否按草稿编辑。")
    except APITimeoutError:
        print("请求超时。服务端可能仍在生成，请先查看中转站记录再决定是否重试。", file=sys.stderr)
        return 1
    except APIConnectionError:
        print("连接中转站失败，请检查网络及 API 地址。", file=sys.stderr)
        return 1
    except APIStatusError as exc:
        tips = {
            400: "请检查模型名称、图片格式，以及中转站是否接受图片编辑参数。",
            401: "请检查中转站 API Key。",
            403: "请检查账户或模型访问权限。",
            404: "请检查模型名称及 API 地址；中转站可能不支持 /v1/images/edits。",
            429: "请检查余额或请求频率。",
            503: "服务暂不可用，详细诊断见上方及报告；不能判定为不支持图生图。",
        }
        print(f"接口返回 HTTP {exc.status_code}。" + tips.get(exc.status_code, "请查看中转站的请求记录。"), file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        # 下载异常可能带签名链接，所以不直接打印第三方异常内容。
        message = str(exc) if isinstance(exc, ValueError) else "图片下载或文件读写失败，请检查网络和输出目录。"
        print(message, file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"响应处理失败（{type(exc).__name__}），中转站可能返回了非标准数据。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n已退出；如已提交请求，请到中转站查看生成记录。", file=sys.stderr)
        sys.exit(1)
