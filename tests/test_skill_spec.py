"""Skill 系统单元测试：Anthropic Agent Skills 规范一致性。

校验目标：
- frontmatter 中 name / description 违反规范时只产生警告，不拒绝加载
- 合法的 SKILL.md 不应产生规范类警告
"""
from pathlib import Path

from skills.registry import SkillRegistry, SPEC_NAME_MAX_LEN, SPEC_DESC_MAX_LEN


def _write_skill(root: Path, name: str, body_name: str, description: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        f"name: {body_name}\n"
        f"description: {description}\n"
        "---\n"
        "正文示例。\n"
    )
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


def _registry_with_project(tmp_path: Path) -> SkillRegistry:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    project = tmp_path / "project"
    for d in (builtin, user, project):
        d.mkdir()
    reg = SkillRegistry(builtin_dir=builtin, user_dir=user, project_dir=project)
    reg.reload()
    return reg, project


def test_valid_skill_produces_no_spec_warnings(tmp_path):
    reg, project = _registry_with_project(tmp_path)
    _write_skill(project, "good-skill", "good-skill", "处理 PDF 并提取文本。当用户提到 PDF 时使用。")
    reg.reload()

    assert reg.find_skill("good-skill") is not None
    # 不应触发任何 spec 类警告
    spec_warnings = [w for w in reg.warnings if "Anthropic" in w or "规范" in w]
    assert spec_warnings == [], f"不应有 spec 警告，但收到: {spec_warnings}"


def test_invalid_name_only_warns(tmp_path):
    """name 含大写、下划线等违规字符时，只发警告，不拒绝加载。"""
    reg, project = _registry_with_project(tmp_path)
    _write_skill(project, "BadSkill", "Bad_Name", "示例描述。")
    reg.reload()

    # 仍能查到（按 name 字段查）
    assert reg.find_skill("Bad_Name") is not None
    assert any("非法字符" in w for w in reg.warnings)


def test_reserved_name_warns(tmp_path):
    reg, project = _registry_with_project(tmp_path)
    _write_skill(project, "anthropic-skill", "anthropic", "示例描述。")
    reg.reload()

    assert any("保留字" in w for w in reg.warnings)


def test_name_too_long_warns(tmp_path):
    reg, project = _registry_with_project(tmp_path)
    long_name = "a" + "b" * SPEC_NAME_MAX_LEN
    _write_skill(project, "longname", long_name, "示例。")
    reg.reload()

    assert any("name 长度" in w for w in reg.warnings)


def test_description_too_long_warns(tmp_path):
    reg, project = _registry_with_project(tmp_path)
    desc = "x" * (SPEC_DESC_MAX_LEN + 10)
    _write_skill(project, "long-desc", "long-desc", desc)
    reg.reload()

    assert any("description 长度" in w for w in reg.warnings)


def test_empty_description_warns(tmp_path):
    reg, project = _registry_with_project(tmp_path)
    _write_skill(project, "no-desc", "no-desc", "")
    reg.reload()

    assert any("description 为空" in w for w in reg.warnings)
