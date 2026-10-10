#!/usr/bin/env python3
"""离线读取 WeaveMind 轨迹；不调用模型，也不重跑原始操作。"""

import argparse
import json
from pathlib import Path
import sys

# 该内置 Skill 随 WeaveMind 仓库发布，复用与 DiagnoseRun 一致的规则。
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from core.run_diagnosis import diagnose_run, list_runs, render_report


def main():
    parser = argparse.ArgumentParser(description="只读复盘 Agent 本地执行轨迹")
    parser.add_argument("--trace-dir", help="轨迹目录，默认读取 trace.directory 配置")
    parser.add_argument("--run-id", default="latest")
    parser.add_argument("--session-id")
    parser.add_argument("--list", action="store_true", help="列出最近的非诊断运行")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = parser.parse_args()
    if args.list:
        print(json.dumps(list_runs(args.trace_dir, args.session_id), ensure_ascii=False, indent=2))
        return 0
    report = diagnose_run(args.run_id, args.trace_dir, args.session_id)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else render_report(report))
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
