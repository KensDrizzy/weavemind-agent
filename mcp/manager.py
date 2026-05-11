from mcp.client import MCPClient
import settings


class MCPManager:
    def __init__(self, servers: dict = None):
        cfg = servers or settings.get("mcp.servers", {})
        self._clients = {
            name: MCPClient(name, s["command"], s.get("args", []))
            for name, s in cfg.items()
        }

    async def connect_all(self):
        for client in self._clients.values():
            await client.connect()

    def get_tools(self) -> list:
        tools = []
        for client in self._clients.values():
            tools.extend(client.get_tools())
        return tools
