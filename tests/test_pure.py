import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _tools import (
    TUBA_URL_TEMPLATE,
    safe_tool_exe,
    split_args,
    tuba_candidate_months,
    tuba_find_latest,
    tuba_version_key,
)  # noqa: E402
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

def test_tuba_version_key() -> None:
    assert tuba_version_key("2026.08") == "202608"
    assert tuba_version_key("图吧工具箱202608") == "202608"
    assert tuba_version_key("2026.13") == ""
    assert tuba_version_key("") == ""


def test_tuba_find_latest_fake_probe() -> None:
    import datetime

    def fake_probe(u: str) -> bool:
        return "202608" in u

    r = tuba_find_latest("202512", today=datetime.date(2026, 11, 13), probe=fake_probe)
    assert r == {"ok": True, "month": "202608",
                 "url": TUBA_URL_TEMPLATE.replace("{ym}", "202608"), "newer": True}
    r = tuba_find_latest("202608", today=datetime.date(2026, 11, 13), probe=fake_probe)
    assert r["month"] == "202608" and r["newer"] is False
    assert tuba_find_latest("202608", template="no-ym")["ok"] is False


def test_tuba_candidate_months_wraps_year() -> None:
    assert tuba_candidate_months(2026, 1)[:2] == ["202601", "202512"]
