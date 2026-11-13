import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _tools import safe_tool_exe, split_args  # noqa: E402
from windows_mcp import blocked_tools  # noqa: E402


def test_safe_tool_exe_rejects_traversal(tmp_path: Path) -> None:
    (tmp_path / "hex2dec.exe").write_bytes(b"x")
    assert safe_tool_exe(tmp_path, "hex2dec") == tmp_path / "hex2dec.exe"
    assert safe_tool_exe(tmp_path, "..\\evil.exe") is None
    assert safe_tool_exe(tmp_path, "sub/evil.exe") is None
    assert safe_tool_exe(tmp_path, "") is None


def test_split_args() -> None:
    assert split_args('-q "C:\\a b" -s') == ["-q", "C:\\a b", "-s"]


def test_blocked_tools() -> None:
    assert "powershell" in blocked_tools()
    assert blocked_tools("", "PowerShell") == frozenset({"filesystem", "registry", "process", "clipboard"})