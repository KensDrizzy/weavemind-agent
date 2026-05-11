#!/usr/bin/env python3
import sys
import argparse
import logging
from cli.app import WeaveMindCLI

def main():
    # 配置日志：默认 WARNING，可通过 --debug 参数启用 DEBUG
    log_level = logging.DEBUG if "--debug" in sys.argv else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
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
