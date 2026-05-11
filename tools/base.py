from langchain_core.tools import BaseTool
from abc import abstractmethod


class WeaveMindTool(BaseTool):
    @abstractmethod
    def _run(self, **kwargs): ...

    async def _arun(self, **kwargs):
        return self._run(**kwargs)
