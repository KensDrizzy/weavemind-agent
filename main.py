#!/usr/bin/env python3
import sys
import argparse
import logging
import warnings

# langchain_core 的 warn_deprecated 绕过了标准 warnings filter 机制，
# 必须 monkey-patch warnings.warn 来静默第三方弃用警告
_original_warn = warnings.warn
def _suppress_thirdparty_deprecation(message, *args, **kwargs):
    category = kwargs.get("category") or (args[0] if args else None)
    if category and issubclass(category, DeprecationWarning):
        return
    _original_warn(message, *args, **kwargs)
warnings.warn = _suppress_thirdparty_deprecation

from cli.app import WeaveMindCLI


class _McpCancelScopeFilter(logging.Filter):
    """过滤 MCP 断开连接时的 cancel scope 跨 task 警告。

    anyio 的 cancel scope 在不同 task 中退出时会抛异常，
    这是 MCP stdio 连接关闭时的已知行为，不影响功能。
    """
    def filter(self, record: logging.LogRecord) -> bool:
        if "cancel scope" in record.getMessage() and "different task" in record.getMessage():
            return False
        return True


class _McpProcessTermFilter(logging.Filter):
    """过滤 MCP 进程组终止失败的警告。

    npx 子进程在 macOS 上无法通过 process group 方式终止（Operation not permitted），
    mcp 库会 fallback 到 simple terminate，功能不受影响。
    """
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "Process group termination failed" in msg and "falling back to simple terminate" in msg:
            return False
        return True


def main():
    # 配置日志：默认 WARNING，可通过 --debug 参数启用 DEBUG
    log_level = logging.DEBUG if "--debug" in sys.argv else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # 静默 MCP 断开连接时的 cancel scope 跨 task 警告
    logging.getLogger("mcp_client.client").addFilter(
        _McpCancelScopeFilter()
    )

    # 静默 MCP 进程组终止失败的警告（macOS 上 npx 子进程的已知行为）
    logging.getLogger("mcp.os.posix.utilities").addFilter(
        _McpProcessTermFilter()
    )

    # 解析命令行参数
    parser = argparse.ArgumentParser(description="WeaveMind Agent - 智能代码助手")
    parser.add_argument("--debug", action="store_true", help="启用调试日志")
    parser.add_argument("--no-hitl", action="store_true", help="禁用人工审批模式（默认启用）")
    args, remaining = parser.parse_known_args()

    # 移除已解析的参数，避免影响后续解析
    sys.argv = [sys.argv[0]] + remaining

    cli = WeaveMindCLI(hitl_enabled=not args.no_hitl)
    cli.run()

if __name__ == "__main__":
    main()
