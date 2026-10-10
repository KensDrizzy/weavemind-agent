"""只读诊断入口，自动选择当前会话上一轮运行。"""

import json
from typing import Literal, Type
from pydantic import BaseModel, Field

from tools.base import WeaveMindTool


class DiagnoseRunInput(BaseModel):
    run_id: str = Field(default="latest", description="完整运行 ID，latest 表示当前会话此前最近一次非诊断运行")
    action: Literal["diagnose", "list"] = Field(default="diagnose", description="诊断或列出当前会话最近的运行")


class DiagnoseRunTool(WeaveMindTool):
    name: str = "DiagnoseRun"
    description: str = "只读分析 Agent 执行轨迹，用于复盘刚才为什么反复调工具、执行失败或没有完成任务。返回事件证据与诊断候选，不重跑任务。"
    args_schema: Type[BaseModel] = DiagnoseRunInput

    def _run(self, run_id="latest", action="diagnose"):
        from core.run_trace import current_trace
        from core.run_diagnosis import diagnose_run, list_runs, render_report
        trace = current_trace()
        if action == "list":
            return json.dumps(list_runs(session_id=trace.session_id if trace else None,
                                        exclude_run_id=trace.run_id if trace else None), ensure_ascii=False)
        return render_report(diagnose_run(run_id))
