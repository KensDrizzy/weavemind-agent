from tools.base import WeaveMindTool


class AskUserTool(WeaveMindTool):
    name: str = "AskUser"
    description: str = "Ask the user a question and wait for their answer. Args: question"

    def _run(self, question: str) -> str:
        return input(f"\n[AskUser] {question}\n> ")
