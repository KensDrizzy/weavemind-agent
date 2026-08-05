"""SessionManager 会话持久化测试。"""

import json
import os

from langchain_core.messages import AIMessage, HumanMessage

from core.session import SessionManager


def _make_manager(tmp_path):
    return SessionManager(storage_dir=str(tmp_path / "sessions"))


class TestSessionSaveResume:
    def test_roundtrip_preserves_messages(self, tmp_path):
        mgr = _make_manager(tmp_path)
        sid = mgr.create()
        conversation = [
            HumanMessage(content="帮我看看配置"),
            AIMessage(
                content="",
                tool_calls=[{"name": "Read", "args": {"path": "config.yaml"}, "id": "tc-1"}],
            ),
            AIMessage(content="当前用的是 DeepSeek 模型"),
        ]
        mgr.save(sid, conversation, {"input": 100, "output": 20, "total": 120})

        messages, meta = mgr.resume(sid)
        assert len(messages) == 3
        assert isinstance(messages[0], HumanMessage)
        assert messages[0].content == "帮我看看配置"
        assert messages[1].tool_calls[0]["name"] == "Read"
        assert messages[2].content == "当前用的是 DeepSeek 模型"
        assert meta["token_totals"]["total"] == 120
        assert meta["title"] == "帮我看看配置"
        assert meta["created_at"] > 0

    def test_resume_missing_returns_none(self, tmp_path):
        mgr = _make_manager(tmp_path)
        messages, meta = mgr.resume("nonexistent")
        assert messages is None and meta is None

    def test_save_keeps_created_at(self, tmp_path):
        mgr = _make_manager(tmp_path)
        sid = mgr.create()
        mgr.save(sid, [HumanMessage(content="第一轮")])
        _, meta1 = mgr.resume(sid)
        mgr.save(sid, [HumanMessage(content="第一轮"), AIMessage(content="回复")])
        _, meta2 = mgr.resume(sid)
        assert meta2["created_at"] == meta1["created_at"]
        assert meta2["updated_at"] >= meta1["updated_at"]

    def test_image_payload_stripped_on_save(self, tmp_path):
        mgr = _make_manager(tmp_path)
        sid = mgr.create()
        conversation = [
            HumanMessage(content=[
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA" * 1000}},
            ]),
            AIMessage(content="图里是只猫"),
        ]
        mgr.save(sid, conversation)

        # 文件里不应有 base64 数据
        with open(mgr._path(sid)) as f:
            raw = f.read()
        assert "base64" not in raw

        messages, _ = mgr.resume(sid)
        content = messages[0].content
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "看这张图"}
        assert content[1] == {"type": "text", "text": "[图片]"}


class TestSessionListAndResolve:
    def _seed(self, mgr):
        ids = []
        for i, text in enumerate(["第一个问题", "第二个问题"]):
            sid = mgr.create()
            mgr.save(sid, [HumanMessage(content=text), AIMessage(content="ok")],
                     {"input": 1, "output": 1, "total": 2})
            ids.append(sid)
            # 保证 updated_at 可区分
            import time
            time.sleep(0.01)
        return ids

    def test_list_returns_summaries_sorted_desc(self, tmp_path):
        mgr = _make_manager(tmp_path)
        ids = self._seed(mgr)
        sessions = mgr.list()
        assert len(sessions) == 2
        assert sessions[0]["updated_at"] >= sessions[1]["updated_at"]
        assert sessions[0]["id"] == ids[1]  # 最新的在前
        assert sessions[0]["message_count"] == 2
        assert sessions[0]["title"] == "第二个问题"
        assert sessions[0]["token_totals"]["total"] == 2

    def test_list_skips_legacy_metadata_files(self, tmp_path):
        mgr = _make_manager(tmp_path)
        self._seed(mgr)
        # 旧格式：只有 message_count 元数据
        with open(os.path.join(mgr.storage_dir, "legacy.json"), "w") as f:
            json.dump({"message_count": 5}, f)
        sessions = mgr.list()
        assert len(sessions) == 2
        assert all(s["id"] != "legacy" for s in sessions)

    def test_resolve_by_index_and_prefix(self, tmp_path):
        mgr = _make_manager(tmp_path)
        ids = self._seed(mgr)
        # 序号 1 = 最新（ids[1]）
        assert mgr.resolve("1") == ids[1]
        assert mgr.resolve("2") == ids[0]
        assert mgr.resolve("99") is None
        # id 前缀
        assert mgr.resolve(ids[0][:8]) == ids[0]
        assert mgr.resolve("zzz") is None
