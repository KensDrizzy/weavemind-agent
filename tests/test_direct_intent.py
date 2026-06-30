from cli.direct_intent import DirectIntentHandler


class DummyGrepTool:
    name = "Grep"

    def invoke(self, args):
        return f"./demo.py:1:{args['pattern']}"


class RegistryStub:
    def __init__(self):
        self.tools = {"Grep": DummyGrepTool()}

    def get(self, name):
        return self.tools.get(name)


def test_simple_grep_intent_still_uses_fast_path():
    handler = DirectIntentHandler(RegistryStub())

    result = handler.handle("搜索 my_func 代码")

    assert result is not None
    assert "my_func" in result


def test_analysis_request_falls_back_to_llm():
    handler = DirectIntentHandler(RegistryStub())

    result = handler.handle(
        "检索一下weavemind的代码，然后分析一下multi-agent的subagent是怎么实现的"
    )

    assert result is None


def test_explanation_request_falls_back_to_llm():
    handler = DirectIntentHandler(RegistryStub())

    result = handler.handle("搜索相关代码并解释 subagent 为什么这样设计")

    assert result is None
