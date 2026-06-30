import pytest
import tempfile, os
from tools.builtin.read import ReadTool
from tools.builtin.write import WriteTool
from tools.builtin.edit import EditTool
from tools.builtin.bash import BashTool
from tools.builtin.glob import GlobTool
from tools.builtin.grep import GrepTool


def test_write_and_read(tmp_path):
    path = str(tmp_path / "test.txt")
    WriteTool()._run(path=path, content="hello world")
    result = ReadTool()._run(path=path)
    assert "hello world" in result


def test_edit(tmp_path):
    path = str(tmp_path / "test.txt")
    WriteTool()._run(path=path, content="foo bar")
    EditTool()._run(path=path, old_string="foo", new_string="baz")
    result = ReadTool()._run(path=path)
    assert "baz bar" in result


def test_bash():
    result = BashTool()._run(command="echo hello")
    assert "hello" in result


def test_glob(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    result = GlobTool()._run(pattern="**/*.py", root=str(tmp_path))
    assert "a.py" in result


def test_grep(tmp_path):
    (tmp_path / "f.py").write_text("def my_func(): pass")
    result = GrepTool()._run(pattern="my_func", path=str(tmp_path))
    assert "my_func" in result


def test_grep_accepts_glob_include(tmp_path):
    (tmp_path / "f.py").write_text("def my_func(): pass")
    (tmp_path / "f.txt").write_text("my_func in text")

    result = GrepTool()._run(pattern="my_func", path=str(tmp_path), glob="*.py")

    assert "f.py" in result
    assert "f.txt" not in result
