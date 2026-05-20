"""Memory 系统测试。"""

from core.memory import LongTermMemory


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
