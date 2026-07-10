"""多模态支持模块。"""

from core.multimodal.content_part import (
    ContentPart,
    ImageBase64Part,
    ImageUrlPart,
    TextPart,
    base64_from_pil,
    content_to_text,
    estimate_base64_size,
    extract_plain_text,
    mime_type_from_path,
    parts_to_openai,
)
from core.multimodal.image_guard import ImageGuard, ImageGuardError
from core.multimodal.image_loader import capture_clipboard_image, load_image_parts
from core.multimodal.image_pruner import prune_historical_image_payloads
from core.multimodal.image_processor import ProcessedImage, process_image, process_image_from_path
from core.multimodal.image_reference import ImageRef, parse_image_refs, replace_image_refs, strip_image_refs
from core.multimodal.message_builder import build_multimodal_message, is_multimodal_message
from core.multimodal.model_capabilities import (
    message_has_image,
    messages_have_image,
    require_vision_model,
    supports_vision,
)

__all__ = [
    "ContentPart",
    "ImageBase64Part",
    "ImageUrlPart",
    "TextPart",
    "base64_from_pil",
    "content_to_text",
    "estimate_base64_size",
    "extract_plain_text",
    "mime_type_from_path",
    "parts_to_openai",
    "ImageGuard",
    "ImageGuardError",
    "capture_clipboard_image",
    "load_image_parts",
    "prune_historical_image_payloads",
    "ProcessedImage",
    "process_image",
    "process_image_from_path",
    "ImageRef",
    "parse_image_refs",
    "replace_image_refs",
    "strip_image_refs",
    "build_multimodal_message",
    "is_multimodal_message",
    "message_has_image",
    "messages_have_image",
    "require_vision_model",
    "supports_vision",
]
