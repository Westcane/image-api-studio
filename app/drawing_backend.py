"""桌面绘图工具的单次请求与图片保存逻辑。"""

import base64
from dataclasses import dataclass, field
import io
from pathlib import Path
import re
import time
from typing import Callable, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image
from openai import OpenAI, APIConnectionError, APIStatusError, APITimeoutError

from app.cli.image_edit import (BASE_URL, MODEL as DEFAULT_MODEL, MODEL_OPTIONS,
                                diagnose_error, is_nano_banana, supported_url)

Quality = Literal["low", "medium", "high", "xhigh", "max", "auto"]
GEMINI_RESOLUTIONS = ("1K", "2K", "4K")
GEMINI_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")

SIZE_OPTIONS = {
    "大图正方形 · 2048 × 2048": "2048x2048",
    "大图竖幅 · 2048 × 3072": "2048x3072",
    "大图横幅 · 3072 × 2048": "3072x2048",
    "中图竖幅 · 1536 × 2304": "1536x2304",
    "中图横幅 · 2304 × 1536": "2304x1536",
    "4K 横幅 · 3840 × 2160": "3840x2160",
    "4K 竖幅 · 2160 × 3840": "2160x3840",
    "试稿正方形 · 1024 × 1024": "1024x1024",
    "试稿横幅 · 1536 × 1024": "1536x1024",
    "试稿竖幅 · 1024 × 1536": "1024x1536",
    "自动选择": "auto",
}
QUALITY_OPTIONS = {
    "高 · high": "high",
    "超高 · xhigh": "xhigh",
    "最高 · max": "max",
    "标准 · medium": "medium",
    "草稿 · low": "low",
    "自动 · auto": "auto",
}


@dataclass(frozen=True)
class DrawingRequest:
    mode: str
    model: str
    prompt: str
    api_key: str = field(repr=False)
    output_dir: Path
    size: str = "2048x2048"
    image_path: Path | None = None
    base_url: str = BASE_URL
    timeout: float = 600
    quality: str = "high"
    gemini_resolution: str = "2K"
    gemini_aspect_ratio: str = "1:1"


class DrawingError(Exception):
    pass


def validate(request: DrawingRequest) -> None:
    if request.mode not in {"generate", "edit"}:
        raise DrawingError("请选择文生图或图生图。")
    if not request.api_key.strip():
        raise DrawingError("请先粘贴 API Key。")
    if not request.prompt.strip():
        raise DrawingError("请先填写绘画指令。")
    if request.model not in {name for name, _ in MODEL_OPTIONS}:
        raise DrawingError("请选择菜单中的模型。")
    if is_nano_banana(request.model):
        if request.gemini_resolution not in GEMINI_RESOLUTIONS:
            raise DrawingError("请选择 Gemini 的 1K、2K 或 4K 分辨率档位。")
        if request.gemini_aspect_ratio not in GEMINI_RATIOS:
            raise DrawingError("请选择 Gemini 的画面比例。")
    else:
        if request.size not in SIZE_OPTIONS.values():
            raise DrawingError("请选择预设尺寸。")
        if request.quality not in QUALITY_OPTIONS.values():
            raise DrawingError("请选择画质档位。")
    if not supported_url(request.base_url):
        raise DrawingError("地址需使用 HTTPS 或指定的 HTTP 绘图 IP。")
    if not 10 <= request.timeout <= 1800:
        raise DrawingError("等待超时请设为 10 至 1800 秒。")
    if request.mode == "edit":
        path = request.image_path
        if not path or not path.is_file():
            raise DrawingError("请先选择草稿图片。")
        if not 0 < path.stat().st_size < 50 * 1024 * 1024:
            raise DrawingError("草稿应非空且小于 50 MB。")
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise DrawingError("请使用 PNG、JPG 或 WebP 草稿。")
            image.verify()


def safe_message(message, key):
    text = str(message).replace(key, "[密钥已隐藏]") if key else str(message)
    text = re.sub(r"https?://\S+", "[图片链接]", text)
    return re.sub(r"[A-Za-z0-9+/=_-]{48,}", "[长数据已隐藏]", text)[:800]


def request_gpt_once(request: DrawingRequest, emit: Callable[[str, str], None]):
    """GPT Image 独立调用：Images Generate/Edit API。"""
    quality = cast(Quality, request.quality)
    try:
        emit("等待接口响应", "已提交一次请求，正在等待接口返回。")
        with OpenAI(base_url=request.base_url, api_key=request.api_key,
                    timeout=request.timeout, max_retries=0) as client:
            if request.mode == "edit":
                image_path = request.image_path
                if image_path is None:
                    raise DrawingError("请先选择草稿图片。")
                with image_path.open("rb") as image:
                    response = client.images.edit(
                        image=image,
                        model=request.model,
                        prompt=request.prompt,
                        size=request.size,
                        quality=quality,
                        n=1,
                    )
            else:
                response = client.images.generate(
                    model=request.model,
                    prompt=request.prompt,
                    size=request.size,
                    quality=quality,
                    n=1,
                )
    except APITimeoutError as exc:
        raise DrawingError("接口等待超时。本轮已结束，没有再次提交；服务器可能仍在处理。") from exc
    except APIConnectionError as exc:
        raise DrawingError("连接接口失败。本轮已结束，请检查网络与接口地址。") from exc
    except APIStatusError as exc:
        info = diagnose_error(exc, request.api_key)
        details = info["details"]
        detail = "\n".join(f"{key}: {value}" for key, value in details.items()) if isinstance(details, dict) else ""
        if request.mode == "generate":
            conclusion = "本次文生图请求失败，未自动重试。"
            if exc.status_code == 503:
                conclusion += "服务端或上游暂不可用。"
        else:
            conclusion = info["conclusion"]
        raise DrawingError(f"HTTP {exc.status_code}\n{conclusion}\n{detail}") from exc
    return response


def run_once(request: DrawingRequest, emit: Callable[[str, str], None]) -> Path:
    """按系列分派一次请求，共用图片验证和保存。"""
    validate(request)
    output_dir = request.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    probe = output_dir / f".write_check_{time.time_ns()}"
    with probe.open("xb"):
        pass
    probe.unlink()
    if is_nano_banana(request.model):
        from app.gemini_backend import request_gemini_once
        response = request_gemini_once(request, emit)
    else:
        response = request_gpt_once(request, emit)

    if not response.data:
        raise DrawingError("接口已返回，但图片列表为空。")
    reported_quality = getattr(response, "quality", None)
    if reported_quality in QUALITY_OPTIONS.values():
        emit("接口已返回", f"服务端回报画质：{reported_quality}；请求画质：{request.quality}。")
    item = response.data[0]
    b64_json = item.b64_json
    image_url = item.url
    if b64_json:
        emit("保存图片", "接口已返回图片数据，正在保存。")
        try:
            data = base64.b64decode(b64_json, validate=True)
        except ValueError as exc:
            raise DrawingError("接口返回的图片 Base64 数据无法解码。") from exc
    elif image_url:
        link_file = output_dir / f"image_link_{time.time_ns()}.txt"
        link_file.write_text(image_url, encoding="utf-8")
        emit("下载图片", f"接口已返回图片链接，备份在：{link_file}\n正在下载图片。")
        try:
            if not supported_url(image_url):
                raise DrawingError("图片链接须使用 HTTPS 或指定绘图 IP。")
            download = Request(image_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
            with urlopen(download, timeout=120) as result:
                if not supported_url(result.geturl()):
                    raise DrawingError("图片链接跳转到了不支持的地址。")
                data = result.read(100 * 1024 * 1024 + 1)
            if len(data) > 100 * 1024 * 1024:
                raise DrawingError("图片超过 100 MB，未保存。")
        except (OSError, DrawingError) as exc:
            if isinstance(exc, HTTPError):
                reason = f"图片服务器返回 HTTP {exc.code}"
            elif isinstance(exc, URLError):
                reason = f"下载连接失败（{type(exc.reason).__name__}）"
            else:
                reason = safe_message(exc, request.api_key)
            raise DrawingError(f"接口已返回图片链接，但下载失败：{reason}\n链接已保存：{link_file}") from exc
    else:
        raise DrawingError("接口返回中没有图片数据或图片链接。")

    try:
        with Image.open(io.BytesIO(data)) as image:
            actual_width, actual_height = image.size
            extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(image.format or "")
            if not extension:
                raise DrawingError("返回内容不是 PNG、JPEG 或 WebP 图片。")
            image.verify()
    except OSError as exc:
        raise DrawingError("收到的内容无法解析为图片。") from exc
    emit("保存图片", "正在保存图片文件。")
    path = output_dir / f"{'edit' if request.mode == 'edit' else 'generation'}_{time.time_ns()}{extension}"
    with path.open("xb") as output:
        output.write(data)
    dimensions = f"实际返回尺寸：{actual_width} × {actual_height}"
    if not is_nano_banana(request.model) and request.size != "auto" and request.size != f"{actual_width}x{actual_height}":
        dimensions += f"；与请求的 {request.size} 不同，请检查线路尺寸支持。"
    emit("保存完成", dimensions)
    return path
