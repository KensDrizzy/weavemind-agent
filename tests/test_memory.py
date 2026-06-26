"""Memory 系统测试。"""

import json

from core.memory import LongTermMemory, MemoryEntry


def test_long_term_memory_updates_highly_similar_fact(tmp_path):
    """相似度超过阈值时，新记忆应替换旧记忆而不是新增。"""
    memory = LongTermMemory(str(tmp_path / "long_term.json"))

    old_content = "用户偏好使用 JDK 17。请优先使用 Maven。"
    new_content = "用户偏好使用 JDK 17。请优先使用 Maven"

    assert memory.store(old_content) is True
    original_entry = memory.get_all()[0]

    assert memory.store(new_content, metadata={"source": "test"}) is True

    entries = memory.get_all()
    assert len(entries) == 1
    assert entries[0].id == original_entry.id
    assert entries[0].content == new_content
    assert entries[0].metadata["updated_from"] == old_content
    assert entries[0].metadata["update_similarity"] > 0.85
    assert entries[0].metadata["source"] == "test"


def test_long_term_memory_keeps_distinct_fact(tmp_path):
    """相似度不超过阈值时，长期记忆仍然新增。"""
    memory = LongTermMemory(str(tmp_path / "long_term.json"))

    assert memory.store("用户偏好使用 JDK 17") is True
    assert memory.store("项目使用 Chroma 做向量检索") is True

    assert len(memory.get_all()) == 2


def test_search_bumps_access_count_and_persists(tmp_path):
    """检索命中应递增 access_count、更新 last_access 并立即持久化。"""
    path = str(tmp_path / "long_term.json")
    memory = LongTermMemory(path)
    memory.store("项目使用 Chroma 做向量检索")

    results = memory.search("Chroma 向量")
    assert len(results) == 1
    assert results[0].access_count == 1
    assert results[0].last_access > 0

    # 落盘后，新实例应读到统计字段
    reloaded = LongTermMemory(path)
    entry = reloaded.get_all()[0]
    assert entry.access_count == 1
    assert entry.last_access > 0


def test_legacy_entry_without_new_fields_loads(tmp_path):
    """老版本数据没有 access_count/importance 字段时，应能正常加载并使用默认值。"""
    path = tmp_path / "long_term.json"
    legacy = [{
        "id": "abc",
        "content": "老格式条目",
        "type": "fact",
        "timestamp": 1700000000.0,
        "token_count": 5,
        "metadata": {},
    }]
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    memory = LongTermMemory(str(path))
    entries = memory.get_all()
    assert len(entries) == 1
    assert entries[0].access_count == 0
    assert entries[0].importance == 1.0


def test_importance_multiplier_boosts_ranking(tmp_path):
    """高重要度条目应在检索时排在普通条目前面。"""
    memory = LongTermMemory(str(tmp_path / "long_term.json"))
    memory.store("普通的 Chroma 笔记")
    memory.store("关键的 Chroma 决策")

    # 把第二条手动标记为高重要度
    entries = memory.get_all()
    target = next(e for e in entries if "关键" in e.content)
    target.importance = 3.0

    results = memory.search("Chroma")
    assert len(results) >= 2
    assert "关键" in results[0].content
