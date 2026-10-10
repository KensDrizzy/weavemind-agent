"""基于轨迹证据生成诊断候选，不把工具返回或模型自述视为任务成功。"""

from collections import defaultdict
import json
from pathlib import Path
import re

from core.run_trace import current_trace, trace_setting


def read_trace(path):
    events, warnings = [], []
    with Path(path).open(encoding="utf-8") as stream:
        for index, line in enumerate(stream, 1):
            if index > 10000:
                warnings.append("轨迹超过读取上限，后续事件未读取")
                break
            try:
                item = json.loads(line)
                if (not isinstance(item, dict) or item.get("schema_version") != 1
                        or not isinstance(item.get("data"), dict) or not isinstance(item.get("seq"), int)
                        or not isinstance(item.get("event"), str)):
                    raise ValueError("不支持的事件格式")
                events.append(item)
            except (ValueError, TypeError):
                warnings.append(f"第 {index} 行损坏或版本不支持，已跳过")
    return events, warnings


def list_runs(directory=None, session_id=None, exclude_run_id=None, limit=20):
    directory = Path(directory or trace_setting("directory", ".weavemind/traces")).expanduser()
    items = []
    for path in directory.glob("*.jsonl"):
        if path.stem == exclude_run_id:
            continue
        try:
            events, _ = read_trace(path)
        except (OSError, UnicodeError):
            continue
        if not events:
            continue
        first = events[0]
        if session_id is not None and first.get("session_id") != session_id:
            continue
        # “刚才”跳过正在进行的诊断，以及之前的诊断轮次。
        if any(e["event"] == "tool.start" and e["data"].get("tool") == "DiagnoseRun" for e in events):
            continue
        end = next((e["data"] for e in reversed(events) if e["event"] == "run.end"), {})
        items.append({"run_id": path.stem, "session_id": first.get("session_id"),
                      "started_at": first.get("timestamp", 0), "goal": first["data"].get("user_input", ""),
                      "status": end.get("status", "incomplete")})
    return sorted(items, key=lambda item: item["started_at"], reverse=True)[:limit]


def diagnose_run(run_id="latest", directory=None, session_id=None):
    trace = current_trace()
    if session_id is None and trace:
        session_id = trace.session_id
    directory = Path(directory or trace_setting("directory", ".weavemind/traces")).expanduser()
    if run_id == "latest":
        runs = list_runs(directory, session_id, trace.run_id if trace else None, limit=1)
        if not runs:
            return {"error": "没有找到此前的可诊断轨迹。采集启用前的运行无法补录；可明确提供历史 run_id。"}
        run_id = runs[0]["run_id"]
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        return {"error": "run_id 必须是完整的 32 位运行 ID，或 latest。"}
    try:
        events, warnings = read_trace(directory / (run_id + ".jsonl"))
    except (OSError, UnicodeError) as exc:
        return {"error": f"无法读取轨迹：{exc}"}
    if not events:
        return {"error": "轨迹没有可读取事件", "warnings": warnings}
    return analyze(events, warnings)


def analyze(events, warnings=None):
    findings = []
    starts = {e["data"].get("span_id"): e for e in events if e["event"] == "tool.start"}
    ends = [e for e in events if e["event"] == "tool.end"]
    end = next((e for e in reversed(events) if e["event"] == "run.end"), None)
    warnings = list(warnings or [])

    def add(category, certainty, summary, evidence, next_step):
        findings.append({"category": category, "certainty": certainty, "summary": summary,
                         "evidence": [e["seq"] for e in evidence], "next_step": next_step})

    empty = [e for e in ends if e["data"].get("status") == "empty"]
    if empty:
        add("retrieval_failure", "observed", "检索返回空结果；尚不能确定是索引、查询还是资料缺失。", empty,
            "核对检索范围和索引状态，用精确符号或原文短句检索；不要据此断言资料不存在。")
    errors = [e for e in ends if e["data"].get("status") == "error"]
    errors += [e for e in events if e["event"] == "hook.PostToolUse" and e["data"].get("error")]
    if errors:
        add("tool_failure", "observed", "工具调用报错或被审批/权限拒绝。", errors,
            "先查看最早的具体错误，再区分参数、依赖、权限和外部服务问题。")
    llm_errors = [e for e in events if e["event"] == "llm.end" and e["data"].get("status") == "error"]
    if llm_errors:
        add("model_failure", "observed", "记录到模型调用错误；后续重试可能已经恢复。", llm_errors,
            "检查错误后的模型调用和最终运行状态，避免把已恢复的尝试当作最终根因。")
    plan_errors = [e for e in events if e["event"] in {"hook.PlanError", "task.skipped"}
                   or e["event"] == "task.end" and e["data"].get("status") in {"failed", "skipped"}
                   or e["event"] == "hook.PlanExecuteEnd" and e["data"].get("status") == "failed"]
    if plan_errors:
        add("planning_failure", "observed", "计划生成/执行失败，或依赖任务被跳过；不一定是规划模型本身的错误。", plan_errors,
            "沿 plan_id、task_id、dependency 找到首个失败任务，区分直接原因和依赖传播。")
    empty_tasks = [e for e in events if e["event"] == "task.end" and not e["data"].get("tool")
                   and e["data"].get("status") == "completed"]
    if empty_tasks:
        add("planning_failure", "observed", "计划任务没有指定工具，执行器将其跳过并标记为完成。", empty_tasks,
            "核对任务是否需要真实操作；完成状态本身不能作为执行证据。")
    groups = defaultdict(list)
    for e in ends:
        start = starts.get(e["data"].get("span_id"))
        if start and start["data"].get("signature"):
            groups[(start["data"].get("tool"), start["data"]["signature"])].append(e)
    for (tool, _), group in groups.items():
        if len(group) >= 3:
            add("repeated_calls", "hypothesis", f"{tool} 的相同参数摘要调用了 {len(group)} 次，可能没有推进任务。", group,
                "比较每次结果是否变化；正常轮询、脱敏或摘要截断也可能出现同签名，不能直接判定死循环。")
    stops = [e for e in events if e["event"] == "run.stop"]
    if stops:
        add("execution_limit", "observed", "运行因停滞检测或迭代上限停止。", stops,
            "回看停止前的工具参数与结果，确认是否一直重复失败或没有切换策略。")
    if end:
        data = end["data"]
        if data.get("status") == "finished" and (not data.get("output_present") or data.get("pending_tool_calls")):
            add("closing_failure", "observed", "图执行结束时没有有效最终答复，或仍有未执行的工具请求。", [end],
                "检查结束路由、空响应处理和停止条件；结束运行不等于完成用户任务。")
        if data.get("status") in {"error", "cancelled"}:
            add("interrupted", "observed", f"运行状态为 {data['status']}。", [end],
                "核对异常或取消来源；用户取消不应归咎于 Agent 推理失败。")
        if data.get("dropped_events"):
            warnings.append(f"事件上限导致 {data['dropped_events']} 条事件未记录，统计不完整")
        if data.get("review_status") in {"rejected", "max_retries_exceeded"}:
            reviews = [e for e in events if e["event"] == "review.result"]
            add("closing_failure", "observed", "团队运行已结束，但最终审查未通过。", reviews[-1:] or [end],
                "查看审查反馈和最后一次 Worker 产出，不能把团队退出视为验收通过。")
        # 只标记需要核验的收尾矛盾，不能用关键词直接裁定虚假成功。
        text = data.get("final_response", "")
        if (errors or plan_errors) and re.search(r"全部完成|全部成功|修复完成|已修复|all tests passed", text, re.I):
            add("closing_failure", "hypothesis", "最终答复声称成功，但轨迹包含失败，需要核对恢复或验收证据。", [end] + (errors + plan_errors)[:3],
                "寻找后续成功重试、测试或交付物；若已恢复，应排除此候选。")
    else:
        warnings.append("缺少 run.end：可能仍在运行、进程退出或日志不完整，不能直接判定崩溃")
    completed_spans = {e["data"].get("span_id") for e in ends}
    if set(starts) - completed_spans:
        warnings.append("存在缺少结束事件的工具调用，时长和结果无法确定")
    timeline = [{"seq": e["seq"], "event": e["event"], "elapsed_ms": e.get("elapsed_ms"), "data": e["data"]}
                for e in events if e["event"] not in {"graph.step", "graph.config", "chain.start", "chain.end", "llm.start"}]
    return {"run_id": events[0].get("run_id"), "session_id": events[0].get("session_id"),
            "goal": events[0]["data"].get("user_input", ""),
            "status": end["data"].get("status") if end else "incomplete",
            "task_success": "unknown", "warnings": warnings,
            "counts": {"tool_calls": len(starts), "tool_errors": len([e for e in ends if e["data"].get("status") == "error"]),
                       "empty_retrievals": len(empty), "model_calls": sum(e["event"] == "llm.start" for e in events)},
            "findings": findings, "timeline": timeline[-60:],
            "timeline_omitted": max(0, len(timeline) - 60)}


def render_report(report):
    if report.get("error"):
        return report["error"]
    lines = [f"# Agent 运行复盘：{report['run_id']}", f"目标：{report['goal']}",
             f"运行状态：{report['status']}；任务是否完成：尚未独立验证。",
             f"统计：{json.dumps(report['counts'], ensure_ascii=False)}", "", "## 诊断候选"]
    for finding in report["findings"]:
        refs = ", ".join(f"#{seq}" for seq in finding["evidence"][:12])
        label = "事实" if finding["certainty"] == "observed" else "待验证"
        lines.append(f"- [{label}/{finding['category']}] {finding['summary']} 证据：{refs}\n  下一步：{finding['next_step']}")
    if not report["findings"]:
        lines.append("没有命中已知诊断规则；这不代表任务已经成功。请核对最终答复和验收产物。")
    for warning in report["warnings"]:
        lines.append(f"- 证据限制：{warning}")
    lines.extend(["", "## 事件时间线（摘要）"])
    for event in report["timeline"]:
        lines.append(f"- #{event['seq']} {event['event']}: {json.dumps(event['data'], ensure_ascii=False)}")
    if report["timeline_omitted"]:
        lines.append(f"省略了较早的 {report['timeline_omitted']} 条时间线事件；证据序号对应原 JSONL 的 seq。")
    return "\n".join(lines)
