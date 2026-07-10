"""CLI 剪贴板图片热键测试。"""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from cli.app import WeaveMindCLI
from core.multimodal.content_part import ImageBase64Part


@pytest.fixture
def cli():
    """实例化 CLI，屏蔽 prompt_toolkit 终端检测。"""
    with patch("cli.app.Console"):
        return WeaveMindCLI()


class TestClipboardHotkey:
    def test_build_user_message_uses_pending_clipboard_parts(self, cli):
        """热键预捕获的剪贴板图片应优先被使用。"""
        pending = ImageBase64Part(data="aGVsbG8=", mime_type="image/png")
        cli._pending_clipboard_parts.append(pending)

        msg = cli._build_user_message("看看 @clipboard")
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2  # text + image
        assert msg.content[1]["type"] == "image_url"
        assert pending.data not in cli._pending_clipboard_parts

    def test_build_user_message_falls_back_when_no_pending(self, cli):
        """没有预捕获图片时仍应正常工作（会实时读取剪贴板）。"""
        with patch(
            "core.multimodal.image_loader.capture_clipboard_image"
        ) as mock_capture:
            from core.multimodal.image_processor import ProcessedImage

            mock_capture.return_value = ProcessedImage(
                data="aGVsbG8=", mime_type="image/png", width=10, height=10
            )
            msg = cli._build_user_message("看看 @clipboard")
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2

    def test_hotkey_inserts_clipboard_marker(self, cli):
        """F5 热键应插入 @clipboard 标记并预捕获图片。"""
        mock_buffer = MagicMock()
        mock_event = MagicMock()
        mock_event.app.current_buffer = mock_buffer

        with patch(
            "cli.app.capture_clipboard_image"
        ) as mock_capture:
            from core.multimodal.image_processor import ProcessedImage

            mock_capture.return_value = ProcessedImage(
                data="aGVsbG8=", mime_type="image/png", width=10, height=10
            )
            # 触发 key binding handler
            bindings = cli._build_key_bindings()
            handlers = list(bindings.get_bindings_for_keys(("f5",)))
            assert len(handlers) == 1
            handlers[0].call(mock_event)

        mock_buffer.insert_text.assert_called_once_with(" @clipboard ")
        assert len(cli._pending_clipboard_parts) == 1
        assert cli._pending_clipboard_parts[0].mime_type == "image/png"

    def test_hotkey_shows_privacy_hint_once(self, cli):
        """首次使用热键应显示隐私提示。"""
        mock_buffer = MagicMock()
        mock_event = MagicMock()
        mock_event.app.current_buffer = mock_buffer

        with patch("cli.app.console.print") as mock_print, patch(
            "cli.app.capture_clipboard_image"
        ) as mock_capture:
            from core.multimodal.image_processor import ProcessedImage

            mock_capture.return_value = ProcessedImage(
                data="aGVsbG8=", mime_type="image/png", width=10, height=10
            )
            bindings = cli._build_key_bindings()
            handlers = list(bindings.get_bindings_for_keys(("f5",)))
            handlers[0].call(mock_event)

        hint_calls = [
            c for c in mock_print.call_args_list if "图片将上传" in str(c)
        ]
        assert len(hint_calls) == 1
        assert cli._has_shown_image_privacy_hint is True

    def test_hotkey_handles_capture_failure(self, cli):
        """剪贴板捕获失败时不应崩溃或插入标记。"""
        mock_buffer = MagicMock()
        mock_event = MagicMock()
        mock_event.app.current_buffer = mock_buffer

        with patch(
            "cli.app.capture_clipboard_image"
        ) as mock_capture:
            mock_capture.side_effect = RuntimeError("剪贴板中没有图片数据")
            bindings = cli._build_key_bindings()
            handlers = list(bindings.get_bindings_for_keys(("f5",)))
            handlers[0].call(mock_event)

        mock_buffer.insert_text.assert_not_called()
        assert cli._pending_clipboard_parts == []
