"""Multi-Agent 模块测试。"""

import pytest
import json
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agents.agent_state import MultiAgentState
from agents.reviewer import parse_review_approval, create_reviewer_node, MAX_RETRIES
from agents.orchestrator import make_supervisor_node, MultiAgentOrchestrator
from agents.worker import WorkerToolEventWrapper
from agents.loader import load_agent_def, load_agents_by_role
from core.agent_loop import AgentLoop, COMPLEXITY_PROMPT
from tools.base import WeaveMindTool


# ── parse_review_approval 测试 ──────────────────────────────

class TestParseReviewApproval:
    """审查结果解析测试。"""

    def test_approved_true(self):
        approved, issues = parse_review_approval('{"approved": true, "issues": []}')
        assert approved is True
        assert issues == []

    def test_approved_false_with_issues(self):
        approved, issues = parse_review_approval('{"approved": false, "issues": ["缺少test目录"]}')
        assert approved is False
        assert "缺少test目录" in issues

    def test_empty_content(self):
        approved, issues = parse_review_approval("")
        assert approved is False
        assert len(issues) > 0

    def test_none_content(self):
        approved, issues = parse_review_approval(None)
        assert approved is False

    def test_invalid_json(self):
        approved, issues = parse_review_approval("这不是JSON")
        assert approved is False
        assert len(issues) > 0

    def test_missing_approved_field(self):
        approved, issues = parse_review_approval('{"summary": "看起来不错"}')
        assert approved is False  # 缺少 approved 字段，默认 False

    def test_approved_null(self):
        approved, issues = parse_review_approval('{"approved": null}')
        assert approved is False  # null 视为 False

    def test_json_with_surrounding_text(self):
        content = '审查结果如下：\n{"approved": true, "issues": []}\n完毕'
        approved, issues = parse_review_approval(content)
        assert approved is True

    def test_conservative_on_parse_failure(self):
        """保守策略：解析失败默认不通过。"""
        approved, issues = parse_review_approval("随机乱码文本")
        assert approved is False


# ── create_reviewer_node 测试 ──────────────────────────────

class TestReviewerNode:
    """Reviewer 节点测试。"""

    def test_reviewer_approves(self):
        """审查通过时返回 approved。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content='{"approved": true, "issues": [], "suggestions": []}'
        )

        reviewer_node = create_reviewer_node(mock_llm)
        state = {
            "messages": [MagicMock(content="项目创建成功")],
            "retry_count": 0,
        }
        result = reviewer_node(state)

        assert result.goto == "supervisor"
        assert result.update["review_status"] == "approved"
        assert result.update["retry_count"] == 0

    def test_reviewer_rejects_with_retry(self):
        """审查不通过时重试。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content='{"approved": false, "issues": ["缺少test目录"]}'
        )

        reviewer_node = create_reviewer_node(mock_llm)
        state = {
            "messages": [MagicMock(content="项目创建成功")],
            "retry_count": 0,
        }
        result = reviewer_node(state)

        assert result.goto == "supervisor"
        assert result.update["review_status"] == "rejected"
        assert result.update["retry_count"] == 1

    def test_reviewer_max_retries_exceeded(self):
        """超过最大重试次数后保留当前结果。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content='{"approved": false, "issues": ["仍然有问题"]}'
        )

        reviewer_node = create_reviewer_node(mock_llm)
        state = {
            "messages": [MagicMock(content="项目创建成功")],
            "retry_count": MAX_RETRIES,  # 已达上限
        }
        result = reviewer_node(state)

        assert result.goto == "supervisor"
        assert result.update["review_status"] == "max_retries_exceeded"
        assert result.update["retry_count"] == 0  # 重置


# ── agent_state 测试 ──────────────────────────────────────

class TestMultiAgentState:
    """共享状态定义测试。"""

    def test_state_has_required_fields(self):
        state = MultiAgentState(
            messages=[],
            next="",
            current_task=None,
            step_results={},
            review_status=None,
            retry_count=0,
        )
        assert "messages" in state
        assert "next" in state
        assert "retry_count" in state


# ── loader 测试 ────────────────────────────────────────────

class TestAgentLoader:
    """Agent 定义加载器测试。"""

    def test_load_agent_with_role(self, tmp_path):
        md = tmp_path / "planner.md"
        md.write_text("---\nname: planner\nrole: planner\ntools: [Read]\n---\nYou are a planner.")
        defn = load_agent_def(str(md))
        assert defn["name"] == "planner"
        assert defn["role"] == "planner"
        assert defn["tools"] == ["Read"]

    def test_load_agents_by_role(self, tmp_path):
        (tmp_path / "planner.md").write_text("---\nname: p1\nrole: planner\n---\nPlan.")
        (tmp_path / "worker.md").write_text("---\nname: w1\nrole: worker\n---\nWork.")
        (tmp_path / "default.md").write_text("---\nname: d1\n---\nDefault.")

        by_role = load_agents_by_role(str(tmp_path))
        assert "planner" in by_role
        assert "worker" in by_role
        assert "default" in by_role
        assert by_role["planner"][0]["name"] == "p1"

    def test_load_agent_without_frontmatter(self, tmp_path):
        md = tmp_path / "simple.md"
        md.write_text("You are a simple agent.")
        defn = load_agent_def(str(md))
        assert defn["name"] == "simple"
        assert "simple agent" in defn["system_prompt"]


# ── make_supervisor_node 测试 ──────────────────────────────

class TestSupervisorNode:
    """Supervisor 节点测试。"""

    def test_supervisor_routes_to_agent_from_text(self):
        """首轮任务固定路由到 planner，避免多一次 LLM 调用。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="planner")

        supervisor_node = make_supervisor_node(mock_llm, ["planner", "worker-1", "reviewer"])
        state = {
            "messages": [MagicMock(content="创建项目")],
            "next": "",
        }
        result = supervisor_node(state)

        assert result.goto == "planner"
        assert result.update["next"] == "planner"
        mock_llm.invoke.assert_not_called()

    def test_supervisor_routes_from_json_with_agent_field(self):
        """Supervisor 能从 JSON 回复（字段名为 agent）中提取路由。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content='{"agent": "worker-1", "reason": "继续执行"}'
        )

        supervisor_node = make_supervisor_node(mock_llm, ["worker-1", "reviewer"])
        state = {
            "messages": [MagicMock(content="创建项目")],
            "next": "",
        }
        result = supervisor_node(state)

        assert result.goto == "worker-1"
        assert result.update["next"] == "worker-1"

    def test_supervisor_routes_from_json_with_next_agent_field(self):
        """Supervisor 能从 JSON 回复（字段名为 next_agent）中提取路由。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content='{"next_agent": "worker-1", "task": "执行操作"}'
        )

        supervisor_node = make_supervisor_node(mock_llm, ["worker-1", "reviewer"])
        state = {
            "messages": [MagicMock(content="执行任务")],
            "next": "",
        }
        result = supervisor_node(state)

        assert result.goto == "worker-1"

    def test_supervisor_finish(self):
        """Supervisor 回复 FINISH 时路由到 END。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="FINISH")

        supervisor_node = make_supervisor_node(mock_llm, ["worker-1"])
        state = {
            "messages": [MagicMock(content="任务完成")],
            "next": "",
        }
        result = supervisor_node(state)

        assert result.update["next"] == "__end__"

    def test_supervisor_finish_from_json(self):
        """Supervisor 从 JSON 中提取 FINISH。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content='{"next": "FINISH"}'
        )

        supervisor_node = make_supervisor_node(mock_llm, ["worker-1"])
        state = {
            "messages": [MagicMock(content="完成")],
            "next": "",
        }
        result = supervisor_node(state)

        assert result.update["next"] == "__end__"

    def test_supervisor_fallback_on_error(self):
        """Supervisor LLM 调用失败时回退到 FINISH。"""
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = Exception("LLM 错误")

        supervisor_node = make_supervisor_node(mock_llm, ["worker-1"])
        state = {
            "messages": [MagicMock(content="测试")],
            "next": "",
        }
        result = supervisor_node(state)

        assert result.update["next"] == "__end__"

    def test_supervisor_illegal_route(self):
        """Supervisor 路由目标非法时回退到 FINISH。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="unknown_agent")

        supervisor_node = make_supervisor_node(mock_llm, ["worker-1"])
        state = {
            "messages": [MagicMock(content="测试")],
            "next": "",
        }
        result = supervisor_node(state)

        assert result.update["next"] == "__end__"

    def test_supervisor_text_with_explanation(self):
        """Supervisor 回复带解释时能提取第一行的路由目标。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content="planner\n因为这是新任务，需要先规划"
        )

        supervisor_node = make_supervisor_node(mock_llm, ["planner", "worker-1"])
        state = {
            "messages": [MagicMock(content="新任务")],
            "next": "",
        }
        result = supervisor_node(state)

        assert result.goto == "planner"


# ── MultiAgentOrchestrator 集成测试 ──────────────────────

class TestMultiAgentOrchestrator:
    """编排器集成测试（使用 mock LLM）。"""

    def _make_mock_llm(self, responses: list = None):
        """创建 mock LLM，支持多次调用返回不同结果。"""
        mock = MagicMock()
        if responses:
            mock.invoke.side_effect = responses
        else:
            mock.invoke.return_value = MagicMock(content="FINISH")
        return mock

    def test_orchestrator_builds_graph(self):
        """编排器能成功构建 StateGraph。"""
        mock_llm = self._make_mock_llm()
        mock_registry = MagicMock()
        mock_registry.get_langchain_tools.return_value = []

        orchestrator = MultiAgentOrchestrator(
            llm=mock_llm,
            tool_registry=mock_registry,
            num_workers=0,
        )
        assert orchestrator.graph is not None

    def test_orchestrator_run_returns_state(self):
        """编排器 run() 返回包含必要字段的状态。"""
        mock_llm = self._make_mock_llm()
        mock_registry = MagicMock()
        mock_registry.get_langchain_tools.return_value = []

        orchestrator = MultiAgentOrchestrator(
            llm=mock_llm,
            tool_registry=mock_registry,
            num_workers=0,
        )
        result = orchestrator.run("测试任务")
        assert "messages" in result
        assert "step_results" in result

    def test_local_code_task_restricts_worker_web_tools(self):
        """本地代码分析任务不应把 WebSearch/WebFetch 暴露给 worker。"""
        mock_llm = self._make_mock_llm()
        available = {
            "SearchCode",
            "Read",
            "Glob",
            "Grep",
            "Task",
            "BatchDelegate",
            "WebSearch",
            "WebFetch",
        }
        mock_registry = MagicMock()
        mock_registry.get.side_effect = lambda name: object() if name in available else None
        mock_registry.get_langchain_tools.return_value = []

        orchestrator = MultiAgentOrchestrator(
            llm=mock_llm,
            tool_registry=mock_registry,
        )

        tools = orchestrator._select_worker_tools(
            "检索一下weavemind的代码，然后分析multi-agent的subagent怎么实现"
        )

        assert "SearchCode" in tools
        assert "Read" in tools
        assert "Grep" in tools
        assert "WebSearch" not in tools
        assert "WebFetch" not in tools

    def test_non_code_task_keeps_worker_tools_unrestricted(self):
        """非本地代码任务不使用本地代码工具白名单。"""
        mock_llm = self._make_mock_llm()
        mock_registry = MagicMock()
        mock_registry.get.return_value = object()
        mock_registry.get_langchain_tools.return_value = []

        orchestrator = MultiAgentOrchestrator(
            llm=mock_llm,
            tool_registry=mock_registry,
        )

        assert orchestrator._select_worker_tools("搜索今天的 AI 新闻并总结") is None

    def test_external_project_query_does_not_trigger_local_code_mode(self):
        """开源项目/API 文档这类外部资料任务不应被“项目/代码”误判成本地代码任务。"""
        mock_llm = self._make_mock_llm()
        mock_registry = MagicMock()
        mock_registry.get.return_value = object()
        mock_registry.get_langchain_tools.return_value = []

        orchestrator = MultiAgentOrchestrator(
            llm=mock_llm,
            tool_registry=mock_registry,
        )

        assert orchestrator._select_worker_tools("搜索某个开源项目的 API 文档和代码示例") is None

    def test_local_repo_alias_with_code_intent_triggers_local_code_mode(self):
        """当前仓库名加代码意图仍应进入本地代码模式。"""
        mock_llm = self._make_mock_llm()
        available = {"SearchCode", "Read", "Glob", "Grep"}
        mock_registry = MagicMock()
        mock_registry.get.side_effect = lambda name: object() if name in available else None
        mock_registry.get_langchain_tools.return_value = []

        orchestrator = MultiAgentOrchestrator(
            llm=mock_llm,
            tool_registry=mock_registry,
        )

        tools = orchestrator._select_worker_tools("分析一下 weavemind 的代码结构")

        assert tools == ["SearchCode", "Read", "Glob", "Grep"]

    def test_reuses_graph_when_worker_tool_signature_is_unchanged(self):
        """连续同类任务不应重复构建 LangGraph。"""
        mock_llm = self._make_mock_llm()
        available = {"SearchCode", "Read", "Glob", "Grep"}
        mock_registry = MagicMock()
        mock_registry.get.side_effect = lambda name: object() if name in available else None
        mock_registry.get_langchain_tools.return_value = []

        orchestrator = MultiAgentOrchestrator(
            llm=mock_llm,
            tool_registry=mock_registry,
        )

        with patch("agents.orchestrator.create_worker_node", return_value=lambda state: {}):
            with patch.object(orchestrator, "_build_graph", wraps=orchestrator._build_graph) as build_graph:
                orchestrator._configure_graph_for_task("分析一下 weavemind 的代码结构")
                orchestrator._configure_graph_for_task("检索一下 weavemind 的实现逻辑")

        assert build_graph.call_count == 1

    def test_reuses_initial_graph_for_unrestricted_task(self):
        """初始图已经是普通工具集，普通任务不应先额外重建一次。"""
        mock_llm = self._make_mock_llm()
        mock_registry = MagicMock()
        mock_registry.get.return_value = object()
        mock_registry.get_langchain_tools.return_value = []

        orchestrator = MultiAgentOrchestrator(
            llm=mock_llm,
            tool_registry=mock_registry,
        )

        with patch.object(orchestrator, "_build_graph", wraps=orchestrator._build_graph) as build_graph:
            orchestrator._configure_graph_for_task("搜索今天的 AI 新闻并总结")

        assert build_graph.call_count == 0

    def test_rebuilds_graph_when_worker_tool_signature_changes(self):
        """从普通任务切到本地代码任务时需要重建图以注入新的工具白名单。"""
        mock_llm = self._make_mock_llm()
        available = {"SearchCode", "Read", "Glob", "Grep"}
        mock_registry = MagicMock()
        mock_registry.get.side_effect = lambda name: object() if name in available else None
        mock_registry.get_langchain_tools.return_value = []

        orchestrator = MultiAgentOrchestrator(
            llm=mock_llm,
            tool_registry=mock_registry,
        )
        orchestrator._configure_graph_for_task("搜索今天的 AI 新闻并总结")

        with patch("agents.orchestrator.create_worker_node", return_value=lambda state: {}):
            with patch.object(orchestrator, "_build_graph", wraps=orchestrator._build_graph) as build_graph:
                orchestrator._configure_graph_for_task("分析一下 weavemind 的代码结构")

        assert build_graph.call_count == 1

    def test_local_code_task_adds_planner_local_repo_rules(self):
        """本地代码任务的 planner 也必须被约束，不能规划公网同名项目检索。"""
        mock_llm = self._make_mock_llm([MagicMock(content="1. 使用 SearchCode 检索本地 agents/")])
        mock_registry = MagicMock()
        mock_registry.get.return_value = object()
        mock_registry.get_langchain_tools.return_value = []
        orchestrator = MultiAgentOrchestrator(
            llm=mock_llm,
            tool_registry=mock_registry,
            num_workers=0,
        )
        orchestrator._current_worker_tool_names = ["SearchCode", "Read", "Glob", "Grep"]
        planner_node = orchestrator._make_planner_node()

        planner_node({
            "messages": [
                HumanMessage(content="检索一下weavemind的代码，然后分析multi-agent的subagent怎么实现")
            ],
            "next": "",
            "current_task": None,
            "step_results": {},
            "review_status": None,
            "retry_count": 0,
        })

        sent_messages = mock_llm.invoke.call_args.args[0]
        system_prompt = sent_messages[0].content
        assert "当前本地项目" in system_prompt
        assert "不要规划去 GitHub、GitLab、搜索引擎或互联网" in system_prompt
        assert "当前本地仓库路径" in system_prompt
        assert "SearchCode" in system_prompt


class DummyWorkerTool(WeaveMindTool):
    name: str = "Dummy"
    description: str = "Dummy tool. Args: value"

    def _run(self, value: str) -> str:
        return f"ok:{value}"


class TestWorkerToolEventWrapper:
    """Worker 工具事件包装测试。"""

    def test_emits_pre_and_post_tool_events(self):
        hook_manager = MagicMock()
        tool = WorkerToolEventWrapper(
            DummyWorkerTool(),
            hook_manager=hook_manager,
            agent_name="worker-1",
        )

        result = tool._run(value="x")

        assert result == "ok:x"
        hook_manager.emit.assert_any_call("PreToolUse", {
            "agent": "worker-1",
            "tool": "Dummy",
            "args": {"value": "x"},
        })
        hook_manager.emit.assert_any_call("PostToolUse", {
            "agent": "worker-1",
            "tool": "Dummy",
            "args": {"value": "x"},
            "result": "ok:x",
        })


# ── classify_complexity 测试 ──────────────────────────────────


class TestClassifyComplexity:
    """测试 AgentLoop.classify_complexity 方法。"""

    def _make_agent_loop(self, mock_llm_response: str):
        """创建一个 mock 了 LLM 的 AgentLoop 实例。"""
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = mock_llm_response
        mock_llm.invoke.return_value = mock_response
        mock_llm.stream.return_value = iter([])

        mock_registry = MagicMock()
        mock_registry.get_langchain_tools.return_value = []
        mock_policy = MagicMock()
        mock_hooks = MagicMock()
        mock_memory = MagicMock()

        with patch("core.agent_loop.create_llm", return_value=mock_llm):
            loop = AgentLoop(
                tool_registry=mock_registry,
                permission_policy=mock_policy,
                hook_manager=mock_hooks,
                memory=mock_memory,
            )
        return loop

    def test_simple_task(self):
        """简单任务应返回 simple。"""
        loop = self._make_agent_loop("simple")
        with patch("core.llm_factory.create_llm") as mock_create:
            mock_resp = MagicMock()
            mock_resp.content = "simple"
            mock_create.return_value.invoke.return_value = mock_resp
            result = loop.classify_complexity("列出当前目录下的文件")
        assert result == "simple"

    def test_complex_task(self):
        """复杂任务应返回 complex。"""
        loop = self._make_agent_loop("complex")
        with patch("core.llm_factory.create_llm") as mock_create:
            mock_resp = MagicMock()
            mock_resp.content = "complex"
            mock_create.return_value.invoke.return_value = mock_resp
            result = loop.classify_complexity("创建一个 Spring Boot 项目，写一个 HelloController")
        assert result == "complex"

    def test_complex_keyword_in_sentence(self):
        """回复中包含 complex 关键词应识别为复杂。"""
        loop = self._make_agent_loop("simple")
        with patch("core.llm_factory.create_llm") as mock_create:
            mock_resp = MagicMock()
            mock_resp.content = "This is a complex task"
            mock_create.return_value.invoke.return_value = mock_resp
            result = loop.classify_complexity("创建项目")
        assert result == "complex"

    def test_error_fallback_to_simple(self):
        """LLM 调用失败时应保守返回 simple。"""
        loop = self._make_agent_loop("simple")
        with patch("core.llm_factory.create_llm") as mock_create:
            mock_create.return_value.invoke.side_effect = Exception("API error")
            result = loop.classify_complexity("创建项目")
        assert result == "simple"

    def test_uses_configured_provider(self):
        """验证复杂度判断使用 config.yaml 中配置的 classifier 模型。"""
        import settings
        loop = self._make_agent_loop("simple")
        with patch("core.llm_factory.create_llm") as mock_create:
            mock_resp = MagicMock()
            mock_resp.content = "simple"
            mock_create.return_value.invoke.return_value = mock_resp
            loop.classify_complexity("测试任务")
            # 应使用 config.yaml 中 team.classifier_provider/model 配置
            # 不硬编码具体 provider/model，避免随配置漂移；只校验"读了配置"
            expected_provider = settings.get("team.classifier_provider", loop.provider)
            expected_model = settings.get("team.classifier_model", loop.model)
            mock_create.assert_called_once_with(provider=expected_provider, model=expected_model)


class TestShouldAutoTeam:
    """测试 WeaveMindCLI._should_auto_team 方法。"""

    def _make_cli(self, mock_llm_response: str = "simple"):
        """创建一个 mock 了 LLM 的 CLI 实例。"""
        from cli.app import WeaveMindCLI

        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = mock_llm_response
        mock_llm.invoke.return_value = mock_response
        mock_llm.stream.return_value = iter([])

        with patch("core.llm_factory.create_llm", return_value=mock_llm):
            cli = WeaveMindCLI()
        return cli

    def test_auto_detect_enabled_complex(self):
        """auto_detect 开启且任务复杂时应返回 True。"""
        cli = self._make_cli("complex")
        with patch("settings.get") as mock_settings:
            mock_settings.side_effect = lambda key, default=None: {
                "team.auto_detect": True,
            }.get(key, default)
            with patch("core.llm_factory.create_llm") as mock_create:
                mock_resp = MagicMock()
                mock_resp.content = "complex"
                mock_create.return_value.invoke.return_value = mock_resp
                assert cli._should_auto_team("创建一个项目") is True

    def test_auto_detect_enabled_simple(self):
        """auto_detect 开启但任务简单时应返回 False。"""
        cli = self._make_cli("simple")
        with patch("settings.get") as mock_settings:
            mock_settings.side_effect = lambda key, default=None: {
                "team.auto_detect": True,
            }.get(key, default)
            with patch("core.llm_factory.create_llm") as mock_create:
                mock_resp = MagicMock()
                mock_resp.content = "simple"
                mock_create.return_value.invoke.return_value = mock_resp
                assert cli._should_auto_team("列出文件") is False

    def test_auto_detect_disabled(self):
        """auto_detect 关闭时应始终返回 False。"""
        cli = self._make_cli("complex")
        with patch("settings.get") as mock_settings:
            mock_settings.side_effect = lambda key, default=None: {
                "team.auto_detect": False,
            }.get(key, default)
            assert cli._should_auto_team("创建一个项目") is False

    def test_plan_mode_has_priority_over_auto_team(self):
        """/plan 显式开启时，复杂任务不应被自动切到 Multi-Agent。"""
        from cli.app import WeaveMindCLI

        cli = WeaveMindCLI.__new__(WeaveMindCLI)
        cli.plan_mode = True
        cli.team_mode = False
        cli.conversation = []
        cli.hitl_handler = MagicMock()
        cli.hitl_handler.is_enabled.return_value = True
        cli._has_shown_hitl_hint = True
        cli._has_shown_rag_hint = True
        cli.rag_pipeline = None
        cli.stream_details_expanded = False
        cli.stream_renderer = MagicMock()
        cli.stream_renderer.has_streamed_answer = False
        cli.session_manager = MagicMock()
        cli.session_manager.create.return_value = "test-session"
        cli._should_auto_team = MagicMock(return_value=True)
        cli._run_multi_agent = MagicMock()
        cli.agent_loop = MagicMock()
        cli.agent_loop.stream_with_history = MagicMock(return_value=iter([
            {"execute_plan": {"messages": [AIMessage(content="计划执行完成")]}}
        ]))

        cli._run_agent("创建一个项目并运行测试")

        cli._should_auto_team.assert_not_called()
        cli._run_multi_agent.assert_not_called()
        cli.agent_loop.stream_with_history.assert_called_once()

    def test_multi_agent_summary_prints_full_result(self):
        """最终汇总应打印完整 worker 输出，而不是 200 字符预览。"""
        from cli.app import WeaveMindCLI

        long_result = "A" * 260 + "TAIL"

        cli = WeaveMindCLI.__new__(WeaveMindCLI)
        cli.agent_loop = MagicMock()
        cli.agent_loop.llm = MagicMock()
        cli.agent_loop.tool_registry = MagicMock()
        cli.agent_loop.permission_policy = MagicMock()
        cli.agent_loop.hook_manager = MagicMock()
        cli.agent_loop.memory = MagicMock()
        cli.stream_details_expanded = False
        cli.stream_renderer = MagicMock()

        fake_orchestrator = MagicMock()
        fake_orchestrator.stream.return_value = iter([
            {"worker-1": {"step_results": {"worker-1": long_result}}},
        ])

        with patch("agents.orchestrator.MultiAgentOrchestrator", return_value=fake_orchestrator):
            with patch("cli.app.console") as mock_console:
                cli._run_multi_agent("测试任务")

        rendered = "\n".join(str(call.args[0]) for call in mock_console.print.call_args_list if call.args)
        assert "TAIL" in rendered
        assert long_result in rendered

    def test_multi_agent_summary_keeps_worker_result_when_reviewer_is_last_event(self):
        """stream 最后一个事件可能是 reviewer，CLI 仍应保留 worker 完整输出。"""
        from cli.app import WeaveMindCLI

        worker_result = "完整 worker 输出"

        cli = WeaveMindCLI.__new__(WeaveMindCLI)
        cli.agent_loop = MagicMock()
        cli.agent_loop.llm = MagicMock()
        cli.agent_loop.tool_registry = MagicMock()
        cli.agent_loop.permission_policy = MagicMock()
        cli.agent_loop.hook_manager = MagicMock()
        cli.agent_loop.memory = MagicMock()
        cli.stream_details_expanded = False
        cli.stream_renderer = MagicMock()

        fake_orchestrator = MagicMock()
        fake_orchestrator.stream.return_value = iter([
            {"worker-1": {"step_results": {"worker-1": worker_result}}},
            {"reviewer": {"review_status": "approved"}},
        ])

        with patch("agents.orchestrator.MultiAgentOrchestrator", return_value=fake_orchestrator):
            with patch("cli.app.console") as mock_console:
                cli._run_multi_agent("测试任务")

        rendered = "\n".join(str(call.args[0]) for call in mock_console.print.call_args_list if call.args)
        assert "📋 执行结果汇总" in rendered
        assert worker_result in rendered


class TestConversationCompaction:
    """测试 CLI 短期对话历史的压缩优先策略。"""

    def test_compacts_before_trimming_when_message_limit_exceeded(self):
        from cli.app import MAX_CONVERSATION_MESSAGES, WeaveMindCLI

        cli = WeaveMindCLI.__new__(WeaveMindCLI)
        cli.conversation = [
            HumanMessage(content=f"message {i}")
            for i in range(MAX_CONVERSATION_MESSAGES + 1)
        ]
        compacted = [SystemMessage(content="[对话历史摘要]")] + cli.conversation[-6:]
        cli.agent_loop = MagicMock()
        cli.agent_loop.compactor.compact.return_value = compacted

        cli._compact_conversation_history()

        cli.agent_loop.compactor.compact.assert_called_once()
        assert cli.conversation == compacted

    def test_trims_as_fallback_when_compaction_does_not_reduce_messages(self):
        from cli.app import MAX_CONVERSATION_MESSAGES, WeaveMindCLI

        cli = WeaveMindCLI.__new__(WeaveMindCLI)
        original = [
            HumanMessage(content=f"message {i}")
            for i in range(MAX_CONVERSATION_MESSAGES + 5)
        ]
        cli.conversation = list(original)
        cli.agent_loop = MagicMock()
        cli.agent_loop.compactor.compact.return_value = list(original)

        cli._compact_conversation_history()

        cli.agent_loop.compactor.compact.assert_called_once()
        assert len(cli.conversation) == MAX_CONVERSATION_MESSAGES
        assert cli.conversation == original[-MAX_CONVERSATION_MESSAGES:]
