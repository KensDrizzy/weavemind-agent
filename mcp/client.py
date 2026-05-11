from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_core.tools import StructuredTool
import asyncio


class MCPClient:
    def __init__(self, name: str, command: str, args: list[str]):
        self.name = name
        self.params = StdioServerParameters(command=command, args=args)
        self._tools: list = []

    async def connect(self):
        async with stdio_client(self.params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                self._tools = [
                    StructuredTool.from_function(
                        func=lambda **kwargs, t=t, s=session: asyncio.run(s.call_tool(t.name, kwargs)),
                        name=t.name,
                        description=t.description or "",
                    )
                    for t in tools.tools
                ]

    def get_tools(self) -> list:
        return self._tools
