from tools.base import WeaveMindTool
import os


class WriteTool(WeaveMindTool):
    name: str = "Write"
    description: str = "Create or overwrite a file. Args: path, content"

    def _run(self, path: str, content: str) -> str:
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # 幂等：中断恢复后重跑时，内容完全相同则跳过写入，
        # 避免覆盖用户在等待期间对该文件的手动修改
        if os.path.exists(path):
            with open(path) as f:
                if f.read() == content:
                    return f"Written: {path}（内容未变化，跳过写入）"
        with open(path, "w") as f:
            f.write(content)
        return f"Written: {path}"
