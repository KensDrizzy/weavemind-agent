from tools.base import WeaveMindTool
import os


class EditTool(WeaveMindTool):
    name: str = "Edit"
    description: str = "Replace exact string in a file. Args: path, old_string, new_string"

    def _run(self, path: str, old_string: str, new_string: str) -> str:
        path = os.path.expanduser(path)
        with open(path) as f:
            content = f.read()
        if old_string not in content:
            # 幂等：中断恢复后重跑时，若目标内容已在文件中，视为已应用而非报错
            if new_string and new_string in content:
                return f"Edited: {path}（内容已是目标状态，跳过重复修改）"
            return f"Error: old_string not found in {path}"
        with open(path, "w") as f:
            f.write(content.replace(old_string, new_string, 1))
        return f"Edited: {path}"
