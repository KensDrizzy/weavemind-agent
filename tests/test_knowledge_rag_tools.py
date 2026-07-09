"""Knowledge RAG tool registration tests."""


def test_registry_registers_knowledge_tools(monkeypatch):
    from tools.registry import ToolRegistry

    def fake_get(key, default=None):
        values = {
            "rag.enabled": False,
            "knowledge_rag.enabled": True,
            "agents.dir": ".weavemind/agents",
            "delegation.heartbeat_interval_seconds": 30,
            "delegation.stale_cycles_idle": 15,
            "delegation.stale_cycles_in_tool": 40,
        }
        return values.get(key, default)

    monkeypatch.setattr("settings.get", fake_get)
    registry = ToolRegistry(knowledge_pipeline=object())

    assert registry.get("SearchKnowledge") is not None
    assert registry.get("AskKnowledge") is not None
    assert registry.get("IndexKnowledge") is not None
