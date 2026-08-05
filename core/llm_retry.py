"""LLM 调用的可重试错误分类与指数退避。

可重试（临时性故障）：HTTP 429 限流、500/502/503/504 服务端错误、网络超时、连接重置。
不可重试（确定性错误）：401 认证失败、400 参数错误、SSL 错误、JSON 解析失败等——
这类错误重试多少次结果都一样。

退避策略：基础 500ms 翻倍、上限 30s、±20% 抖动（打散并发重试峰值）；
服务端返回 Retry-After 时优先采用服务端的等待时间。
"""

import logging
import random
import re
import time

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
BASE_DELAY = 0.5
MAX_DELAY = 30.0
JITTER = 0.2

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 网络类临时故障的关键词（匹配异常类型名 + 消息）
_TRANSIENT_HINTS = (
    "timeout", "timed out", "connection", "reset", "eof",
    "temporarily", "temporary", "rate limit", "overloaded",
)
# 确定性错误的关键词（优先于临时故障判断）
_PERMANENT_HINTS = (
    "ssl", "certificate", "authentication", "api key",
    "invalid", "json", "decode",
)


def _extract_status_code(exc: Exception) -> int | None:
    """从 openai/anthropic SDK 异常中提取 HTTP 状态码。"""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    m = re.search(r"\b(4\d\d|5\d\d)\b", str(exc))
    return int(m.group(1)) if m else None


def _extract_retry_after(exc: Exception) -> float | None:
    """从异常携带的响应头中提取 Retry-After（秒）。"""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        for key, value in headers.items():
            if key.lower() == "retry-after":
                return float(value)
    except (TypeError, ValueError):
        return None
    return None


def is_retryable(exc: Exception) -> bool:
    """判断异常是否属于可重试的临时性故障。"""
    code = _extract_status_code(exc)
    if code is not None:
        return code in _RETRYABLE_STATUS

    text = f"{type(exc).__name__} {exc}".lower()
    # 确定性错误优先判否（如 SSL 错误消息里可能含 connection 字样）
    if any(hint in text for hint in _PERMANENT_HINTS):
        return False
    return any(hint in text for hint in _TRANSIENT_HINTS)


def compute_delay(attempt: int, exc: Exception | None = None) -> float:
    """计算第 attempt 次重试（从 1 开始）前的等待秒数。"""
    retry_after = _extract_retry_after(exc) if exc is not None else None
    if retry_after is not None:
        return min(retry_after, MAX_DELAY)
    delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
    jitter = delay * JITTER
    return delay + random.uniform(-jitter, jitter)


def call_with_retry(
    fn,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep=time.sleep,
    description: str = "LLM 调用",
):
    """同步重试包装：仅对可重试错误退避重试，确定性错误立即抛出。

    注意：仅适用于无副作用或已确认可重放的调用（如 LLM 非流式请求）。
    流式输出已开始后不能用本函数重试（会造成用户可见的重复输出）。
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            if attempt >= max_attempts or not is_retryable(e):
                raise
            delay = compute_delay(attempt, e)
            logger.warning(
                "%s失败（可重试，第 %d/%d 次）：%s；%.1fs 后重试",
                description, attempt, max_attempts, e, delay,
            )
            sleep(delay)
