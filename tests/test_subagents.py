from agents.loader import load_agent_def
from agents.subagent import _infer_provider
import tempfile, os


def test_load_agent_def(tmp_path):
    md = tmp_path / "test-agent.md"
    md.write_text("---\nname: test\ntools: [Read]\n---\nYou are a test agent.")
    defn = load_agent_def(str(md))
    assert defn["name"] == "test"
    assert defn["tools"] == ["Read"]
    assert "test agent" in defn["system_prompt"]


class TestInferProvider:
    """测试 _infer_provider 模型名到 provider 的推断。"""

    def test_deepseek(self):
        assert _infer_provider("deepseek-v4-pro") == "deepseek"
        assert _infer_provider("deepseek-chat") == "deepseek"

    def test_claude(self):
        assert _infer_provider("claude-haiku-4-5-20251001") == "anthropic"
        assert _infer_provider("claude-sonnet-4-6") == "anthropic"

    def test_gpt(self):
        assert _infer_provider("gpt-4o") == "openai"
        assert _infer_provider("o1-preview") == "openai"

    def test_mimo(self):
        assert _infer_provider("mimo-v2.5-pro") == "mimo"

    def test_unknown_defaults_to_openai(self):
        assert _infer_provider("some-unknown-model") == "openai"
