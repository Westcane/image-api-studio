"""Nano Banana 原生 generateContent 调用；不使用 OpenAI Images 接口。"""

import base64
import time
from typing import Callable
from urllib.parse import quote, urlsplit, urlunsplit

import httpx2
from openai.types import Image, ImagesResponse

from app.drawing_backend import DrawingError, DrawingRequest, safe_message


def gemini_endpoint(base_url: str, model: str) -> str:
    parsed = urlsplit(base_url)
    prefix = parsed.path.rstrip("/")
    for suffix in ("/v1beta", "/v1"):
        if prefix.endswith(suffix):
            prefix = prefix[:-len(suffix)]
            break
    path = f"{prefix}/v1beta/models/{quote(model, safe='')}:generateContent"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def gemini_payload(request: DrawingRequest) -> dict[str, object]:
    parts: list[dict[str, object]] = [{"text": request.prompt}]
    if request.mode == "edit":
        if request.image_path is None:
            raise DrawingError("请先选择草稿图片。")
        from PIL import Image as PILImage
        with PILImage.open(request.image_path) as image:
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(image.format or "")
        if mime is None:
            raise DrawingError("不支持这张草稿的图片格式。")
        parts.append({"inlineData": {"mimeType": mime,
                       "data": base64.b64encode(request.image_path.read_bytes()).decode("ascii")}})
    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "candidateCount": 1,
            "imageConfig": {"imageSize": request.gemini_resolution,
                            "aspectRatio": request.gemini_aspect_ratio},
        },
    }


def request_gemini_once(request: DrawingRequest, emit: Callable[[str, str], None]) -> ImagesResponse:
    url = gemini_endpoint(request.base_url, request.model)
    payload = gemini_payload(request)
    headers = {"x-goog-api-key": request.api_key, "Content-Type": "application/json"}
    if urlsplit(url).hostname != "generativelanguage.googleapis.com":
        headers["Authorization"] = f"Bearer {request.api_key}"
    emit("等待 Gemini 响应", f"Nano Banana 原生调用 · {request.gemini_resolution} · {request.gemini_aspect_ratio} · 仅一次")
    try:
        # HTTPX 默认不重试；不跟随重定向，避免再次发送 POST 或泄露密钥。
        with httpx2.Client(timeout=request.timeout, follow_redirects=False) as client:
            response = client.post(url, headers=headers, json=payload)
    except httpx2.TimeoutException as exc:
        raise DrawingError("Gemini 请求超时，本轮结束；服务器可能仍在处理。") from exc
    except httpx2.RequestError as exc:
        raise DrawingError(f"Gemini 连接失败（{type(exc).__name__}），本轮结束。") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise DrawingError(f"Gemini 返回 HTTP {response.status_code}，响应不是 JSON。") from exc
    if not isinstance(data, dict):
        raise DrawingError("Gemini 响应结构异常，应为 JSON 对象。")
    if not response.is_success or data.get("error"):
        error = data.get("error", {})
        message = error.get("message", "服务商未提供错误详情") if isinstance(error, dict) else str(error)
        raise DrawingError(f"Gemini HTTP {response.status_code}\n{safe_message(message, request.api_key)}\n未自动重试或切换接口。")
    texts = []
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if part.get("thought"):
                continue
            inline = part.get("inlineData", part.get("inline_data", {}))
            if inline.get("data") and inline.get("mimeType", inline.get("mime_type", "")).startswith("image/"):
                return ImagesResponse(created=int(time.time()), data=[Image(b64_json=inline["data"])])
            if part.get("text"):
                texts.append(part["text"])
    reason = data.get("promptFeedback", {}).get("blockReason")
    detail = safe_message(reason or " ".join(texts) or "响应中没有图片数据", request.api_key)
    raise DrawingError(f"Gemini 已响应，但没有生成图片。\n{detail}")
