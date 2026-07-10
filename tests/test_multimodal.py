"""多模态模块单元测试。"""

import importlib.util
import pytest
from langchain_core.messages import HumanMessage

PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None

from core.multimodal.content_part import (
    ImageBase64Part,
    TextPart,
    content_to_text,
    estimate_base64_size,
    extract_plain_text,
    parts_to_openai,
)
from core.multimodal.image_guard import ImageGuard, ImageGuardError
from core.multimodal.image_pruner import prune_historical_image_payloads
from core.multimodal.image_reference import parse_image_refs, replace_image_refs, strip_image_refs
from core.multimodal.message_builder import build_multimodal_message, is_multimodal_message
from core.multimodal.model_capabilities import (
    message_has_image,
    messages_have_image,
    supports_vision,
)


class TestContentPart:
    def test_text_part_to_openai(self):
        part = TextPart(text="hello")
        assert part.to_openai() == {"type": "text", "text": "hello"}

    def test_image_base64_part_to_openai(self):
        part = ImageBase64Part(data="abc123", mime_type="image/png")
        block = part.to_openai()
        assert block["type"] == "image_url"
        assert block["image_url"]["url"] == "data:image/png;base64,abc123"

    def test_parts_to_openai(self):
        parts = [TextPart("hi"), ImageBase64Part("x", "image/jpeg")]
        blocks = parts_to_openai(parts)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "text"
        assert blocks[1]["type"] == "image_url"

    def test_extract_plain_text(self):
        parts = [TextPart("hello "), ImageBase64Part("x", "image/png"), TextPart("world")]
        assert extract_plain_text(parts) == "hello world"

    def test_content_to_text(self):
        assert content_to_text("plain") == "plain"
        assert content_to_text([{"type": "text", "text": "a"}, {"type": "image_url"}]) == "a"
        assert content_to_text(None) == ""

    def test_estimate_base64_size(self):
        assert estimate_base64_size(3) == 4
        assert estimate_base64_size(6) == 8


class TestMessageBuilder:
    def test_plain_text_message(self):
        msg = build_multimodal_message("hello")
        assert msg.content == "hello"
        assert not is_multimodal_message(msg)

    def test_multimodal_message(self):
        msg = build_multimodal_message("look", [ImageBase64Part("x", "image/png")])
        assert isinstance(msg.content, list)
        assert is_multimodal_message(msg)


class TestImageReference:
    def test_parse_image_path(self):
        refs = parse_image_refs("分析 @image:./shot.png")
        assert len(refs) == 1
        assert refs[0].source == "./shot.png"
        assert not refs[0].is_clipboard

    def test_parse_clipboard(self):
        refs = parse_image_refs("看看 @clipboard")
        assert len(refs) == 1
        assert refs[0].is_clipboard

    def test_parse_bracket_path(self):
        refs = parse_image_refs("@image:</path with spaces.png>")
        assert len(refs) == 1
        assert refs[0].source == "/path with spaces.png"

    def test_parse_file_protocol(self):
        refs = parse_image_refs("@image:file:///Users/foo/shot.png")
        assert len(refs) == 1
        assert refs[0].source == "/Users/foo/shot.png"

    def test_strip_image_refs(self):
        assert strip_image_refs("分析 @image:./a.png") == "分析"

    def test_replace_image_refs(self):
        assert replace_image_refs("看 @image:a.png 和 @clipboard") == "看 [图片] 和 [图片]"


class TestModelCapabilities:
    def test_supports_vision(self):
        assert supports_vision("kimi-k2.7")
        assert supports_vision("claude-sonnet-4-20250514")
        assert supports_vision("gpt-4o")
        assert not supports_vision("deepseek-v4-pro")
        assert not supports_vision("mimo-v2.5-pro")

    def test_message_has_image(self):
        msg = HumanMessage(content=[{"type": "text", "text": "a"}, {"type": "image_url"}])
        assert message_has_image(msg)
        assert messages_have_image([msg])

    def test_message_without_image(self):
        msg = HumanMessage(content="hello")
        assert not message_has_image(msg)


class TestImagePruner:
    def test_prune_keeps_last(self):
        img_block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
        msg1 = HumanMessage(content=[{"type": "text", "text": "a"}, img_block])
        msg2 = HumanMessage(content=[{"type": "text", "text": "b"}, img_block])
        result = prune_historical_image_payloads([msg1, msg2], keep_last_n_rounds=1)
        # msg1 保留文本，图片被替换为占位；msg2 完整保留
        assert result[0].content == [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "[图片已省略，参见上文描述]"},
        ]
        assert result[1].content == [{"type": "text", "text": "b"}, img_block]

    def test_prune_no_images(self):
        msg = HumanMessage(content="hello")
        result = prune_historical_image_payloads([msg], keep_last_n_rounds=1)
        assert result[0].content == "hello"


@pytest.mark.skipif(not PIL_AVAILABLE, reason="需要 Pillow")
class TestImageProcessor:
    def test_process_small_rgb_image(self, tmp_path):
        from PIL import Image

        from core.multimodal.image_processor import process_image

        img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        processed = process_image(img)
        assert processed.mime_type == "image/png"
        assert processed.width == 100
        assert processed.height == 100
        assert len(processed.data) > 0

    def test_process_alpha_image_gets_flattened(self, tmp_path):
        from PIL import Image

        from core.multimodal.image_processor import process_image

        img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
        processed = process_image(img)
        # flatten 后作为 PNG 输出
        assert processed.mime_type in {"image/png", "image/jpeg"}
        assert processed.width == 100

    def test_resize_large_image(self):
        from PIL import Image

        from core.multimodal.image_processor import process_image, MAX_IMAGE_DIMENSION

        img = Image.new("RGB", (MAX_IMAGE_DIMENSION * 2, MAX_IMAGE_DIMENSION * 2), color=(0, 255, 0))
        processed = process_image(img)
        assert max(processed.width, processed.height) <= MAX_IMAGE_DIMENSION


class TestImageGuard:
    def test_validate_allows_project_image(self, tmp_path):
        guard = ImageGuard(project_root=tmp_path)
        img = tmp_path / "shot.png"
        from PIL import Image

        image = Image.new("RGB", (10, 10), color=(255, 0, 0))
        image.save(img, format="PNG")
        validated = guard.validate(img)
        assert validated == img.resolve()

    def test_rejects_path_traversal(self, tmp_path):
        guard = ImageGuard(project_root=tmp_path)
        outside = tmp_path.parent / "outside.png"
        from PIL import Image

        image = Image.new("RGB", (10, 10), color=(255, 0, 0))
        image.save(outside, format="PNG")
        try:
            with pytest.raises(ImageGuardError, match="不在允许范围内"):
                guard.validate(outside)
        finally:
            outside.unlink(missing_ok=True)

    def test_rejects_unsupported_mime(self, tmp_path):
        guard = ImageGuard(project_root=tmp_path)
        txt = tmp_path / "malicious.txt"
        txt.write_text("not an image")
        with pytest.raises(ImageGuardError, match="不支持的图片类型"):
            guard.validate(txt)

    def test_rejects_oversized(self, tmp_path):
        guard = ImageGuard(project_root=tmp_path, max_file_size=1)
        img = tmp_path / "big.png"
        from PIL import Image

        image = Image.new("RGB", (100, 100), color=(255, 0, 0))
        image.save(img, format="PNG")
        with pytest.raises(ImageGuardError, match="图片过大"):
            guard.validate(img)

    def test_allows_whitelisted_dir(self, tmp_path):
        whitelisted = tmp_path / "assets"
        whitelisted.mkdir()
        guard = ImageGuard(project_root=tmp_path / "other", allowed_dirs=[whitelisted])
        img = whitelisted / "pic.png"
        from PIL import Image

        image = Image.new("RGB", (10, 10), color=(255, 0, 0))
        image.save(img, format="PNG")
        assert guard.validate(img) == img.resolve()
