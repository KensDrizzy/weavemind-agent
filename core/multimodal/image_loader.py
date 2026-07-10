"""图片加载器 — 从本地路径或剪贴板加载图片并预处理。"""

from __future__ import annotations

import base64
import logging
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from core.multimodal.content_part import ImageBase64Part, ImageUrlPart
from core.multimodal.image_guard import ImageGuard, ImageGuardError
from core.multimodal.image_processor import ProcessedImage, process_image, process_image_from_path
from core.multimodal.image_reference import ImageRef, ImageSourceType
from core import audit

logger = logging.getLogger(__name__)

CLIPBOARD_TIMEOUT_SECONDS = 8


def load_image_parts(
    refs: Iterable[ImageRef],
    guard: ImageGuard | None = None,
) -> list[ImageBase64Part]:
    """把一组图片引用转成可发送给 LLM 的 ImageBase64Part。"""
    guard = guard or ImageGuard()
    parts: list[ImageBase64Part] = []
    for ref in refs:
        source_type = "clipboard" if ref.is_clipboard else "path"
        try:
            if ref.is_clipboard:
                processed = capture_clipboard_image()
            else:
                validated_path = guard.validate(ref.source)
                processed = process_image_from_path(str(validated_path))
            part = ImageBase64Part(
                data=processed.data,
                mime_type=processed.mime_type,
            )
            size_bytes = len(processed.data) * 3 // 4
            audit.log_image_load(
                source_type=source_type,
                mime_type=processed.mime_type,
                size_bytes=size_bytes,
                path=None if ref.is_clipboard else ref.source,
                success=True,
                extra={"width": processed.width, "height": processed.height},
            )
            parts.append(part)
        except Exception as e:
            audit.log_image_load(
                source_type=source_type,
                mime_type="unknown",
                size_bytes=0,
                path=None if ref.is_clipboard else ref.source,
                success=False,
                error=str(e),
            )
            raise
    return parts


def load_image_part_from_path(path: str | Path, guard: ImageGuard | None = None) -> ImageBase64Part:
    """从单个路径加载图片。"""
    guard = guard or ImageGuard()
    try:
        validated_path = guard.validate(path)
        processed = process_image_from_path(str(validated_path))
        size_bytes = len(processed.data) * 3 // 4
        audit.log_image_load(
            source_type="path",
            mime_type=processed.mime_type,
            size_bytes=size_bytes,
            path=str(validated_path),
            success=True,
            extra={"width": processed.width, "height": processed.height},
        )
        return ImageBase64Part(data=processed.data, mime_type=processed.mime_type)
    except Exception as e:
        audit.log_image_load(
            source_type="path",
            mime_type="unknown",
            size_bytes=0,
            path=str(path),
            success=False,
            error=str(e),
        )
        raise


def capture_clipboard_image() -> ProcessedImage:
    """从剪贴板抓取图片并预处理。"""
    system = platform.system()
    if system == "Darwin":
        return _capture_clipboard_macos()
    return _capture_clipboard_pil()


def _capture_clipboard_macos() -> ProcessedImage:
    """macOS 通过 osascript 读取剪贴板 PNG / TIFF 原始字节。"""
    script = '''
on run argv
    set outputPath to item 1 of argv
    try
        set pngData to (the clipboard as «class PNGf»)
    on error errMsg
        try
            set tiffData to (the clipboard as «class TIFF»)
            do shell script "/usr/bin/sips -s format png " & quoted form of outputPath & " --out " & quoted form of outputPath
            return
        on error
            error "剪贴板里没有 PNG/TIFF 图片数据"
        end try
    end try
    set fh to open for access (POSIX file outputPath as string) with write permission
    try
        set eof of fh to 0
        write pngData to fh
        close access fh
    on error errMsg
        try
            close access fh
        end try
        error errMsg
    end try
end run
'''
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        output_path = tmp.name

    try:
        proc = subprocess.Popen(
            ["/usr/bin/osascript", "-", output_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = proc.communicate(
            script.encode("utf-8"), timeout=CLIPBOARD_TIMEOUT_SECONDS
        )
        if proc.returncode != 0:
            raise RuntimeError(f"剪贴板读取失败: {stderr.decode('utf-8', errors='ignore')}")
        return process_image_from_path(output_path, mime_hint="image/png")
    finally:
        try:
            Path(output_path).unlink(missing_ok=True)
        except OSError:
            pass


def _capture_clipboard_pil() -> ProcessedImage:
    """Linux/Windows 通过 PIL.ImageGrab 读取剪贴板。"""
    try:
        from PIL import ImageGrab
    except ImportError as e:
        raise RuntimeError(
            "当前环境无法读取剪贴板，请安装 Pillow 并在图形界面下运行。"
        ) from e

    image = ImageGrab.grabclipboard()
    if image is None:
        raise RuntimeError("剪贴板中没有图片数据")
    if isinstance(image, list):
        # 某些平台返回文件列表，取第一个图片文件
        for p in image:
            if isinstance(p, str) and Path(p).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                return process_image_from_path(p)
        raise RuntimeError("剪贴板中没有支持的图片文件")

    return process_image(image, mime_hint="image/png")
