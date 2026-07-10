"""图片预处理 — 大小控制、alpha flatten、格式转换。"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow 是运行时依赖，允许无 Pillow 导入
    Image = None  # type: ignore[assignment]

from core.multimodal.content_part import estimate_base64_size, mime_type_from_path

logger = logging.getLogger(__name__)

# API 传输的是 base64 字符串，按 4/3 膨胀比估算
API_IMAGE_MAX_BASE64_SIZE = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 2000
JPEG_QUALITIES = [0.85, 0.70, 0.55, 0.40, 0.25]


@dataclass(frozen=True)
class ProcessedImage:
    """预处理后的图片。"""

    data: str  # base64 payload
    mime_type: str
    width: int
    height: int


def _has_alpha(image: Image.Image) -> bool:
    """判断图片是否含透明通道。"""
    return image.mode in ("RGBA", "P", "LA") and (
        image.mode == "RGBA" or "transparency" in image.info
    )


def _flatten_alpha(image: Image.Image) -> Image.Image:
    """把透明通道合成到白色背景上。"""
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[3])
        return background
    if image.mode == "LA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[1])
        return background
    if image.mode == "P":
        return image.convert("RGBA").convert("RGB")
    return image.convert("RGB")


def _resize_to_fit(image: Image.Image, max_dim: int) -> Image.Image:
    """等比缩放到最大边不超过 max_dim。"""
    width, height = image.size
    if width <= max_dim and height <= max_dim:
        return image
    ratio = min(max_dim / width, max_dim / height)
    new_size = (int(width * ratio), int(height * ratio))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def _encode_png(image: Image.Image) -> bytes:
    """把图片编码为 PNG 字节。"""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _encode_jpeg(image: Image.Image, quality: float) -> bytes:
    """把图片编码为指定质量的 JPEG 字节。"""
    buffer = io.BytesIO()
    rgb_image = image.convert("RGB")
    rgb_image.save(buffer, format="JPEG", quality=int(quality * 100), optimize=True)
    return buffer.getvalue()


def process_image(
    image: Image.Image,
    mime_hint: str = "image/png",
    max_base64_size: int = API_IMAGE_MAX_BASE64_SIZE,
    max_dimension: int = MAX_IMAGE_DIMENSION,
) -> ProcessedImage:
    """对图片做预处理，返回符合 API 传输要求的 base64。"""
    if Image is None:
        raise ImportError("图片处理需要 Pillow，请运行 pip install Pillow")

    # 先统一转成 RGB/RGBA，避免模式异常
    if image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        image = image.convert("RGB")

    # 超大图先缩放，减少后续计算
    image = _resize_to_fit(image, max_dimension)

    # 1) 小图且无 alpha → 直接 PNG
    if not _has_alpha(image):
        png_bytes = _encode_png(image.convert("RGB"))
        if estimate_base64_size(len(png_bytes)) <= max_base64_size:
            return ProcessedImage(
                data=base64.b64encode(png_bytes).decode("ascii"),
                mime_type="image/png",
                width=image.width,
                height=image.height,
            )

    # 2) 有 alpha 但不大 → 白底 flatten 后 PNG
    if _has_alpha(image):
        flattened = _flatten_alpha(image)
        flattened = _resize_to_fit(flattened, max_dimension)
        png_bytes = _encode_png(flattened)
        if estimate_base64_size(len(png_bytes)) <= max_base64_size:
            return ProcessedImage(
                data=base64.b64encode(png_bytes).decode("ascii"),
                mime_type="image/png",
                width=flattened.width,
                height=flattened.height,
            )
        # flatten 后仍然过大，落到第 3 层用 JPEG
        image = flattened
    else:
        image = image.convert("RGB")

    # 3) 超大了 → 等比缩放后逐级 JPEG 降质
    image = _resize_to_fit(image, max_dimension)
    for quality in JPEG_QUALITIES:
        jpeg_bytes = _encode_jpeg(image, quality)
        if estimate_base64_size(len(jpeg_bytes)) <= max_base64_size:
            return ProcessedImage(
                data=base64.b64encode(jpeg_bytes).decode("ascii"),
                mime_type="image/jpeg",
                width=image.width,
                height=image.height,
            )

    # 兜底：用最低质量再试一次
    jpeg_bytes = _encode_jpeg(image, JPEG_QUALITIES[-1])
    logger.warning(
        f"图片压缩后仍可能超过 {max_base64_size} base64 上限，"
        f"尺寸 {image.width}x{image.height}"
    )
    return ProcessedImage(
        data=base64.b64encode(jpeg_bytes).decode("ascii"),
        mime_type="image/jpeg",
        width=image.width,
        height=image.height,
    )


def process_image_from_path(path: str, mime_hint: str | None = None) -> ProcessedImage:
    """从文件路径加载并处理图片。"""
    if Image is None:
        raise ImportError("图片处理需要 Pillow，请运行 pip install Pillow")

    mime = mime_hint or mime_type_from_path(path)
    with Image.open(path) as img:
        # 提前加载像素数据，避免后续模式转换出错
        img.load()
        return process_image(img.copy(), mime)
