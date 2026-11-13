"""纯逻辑自检：python selfcheck.py。不需要宿主、不需要 pytest。"""
from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tools import (  # noqa: E402
    TUBA_URL_TEMPLATE,
    decode_output,
    download_file,
    extract_zip,
    find_tuba_exe,
    list_tools,
    run_exe,
    safe_tool_exe,
    split_args,
    tuba_candidate_months,
    tuba_find_latest,
    tuba_from_registry,
    tuba_version_key,
)

tmp = Path(tempfile.mkdtemp())

# 工具名校验
(tmp / "hex2dec.exe").write_bytes(b"x")
assert safe_tool_exe(tmp, "hex2dec") == tmp / "hex2dec.exe"
assert safe_tool_exe(tmp, "hex2dec.exe") == tmp / "hex2dec.exe"
assert safe_tool_exe(tmp, "..\\evil.exe") is None
assert safe_tool_exe(tmp, "sub/evil.exe") is None
assert safe_tool_exe(tmp, "missing.exe") is None
assert safe_tool_exe(tmp, "") is None
assert safe_tool_exe(tmp, "a" * 70) is None

# 列表与过滤
assert list_tools(tmp, "hex") == ["hex2dec.exe"]

# 参数拆分
assert split_args("") == []
assert split_args('-q "C:\\path with space" -s') == ["-q", "C:\\path with space", "-s"]

# 输出解码
assert decode_output("你好".encode("gbk")) == "你好"
assert decode_output(b"ok") == "ok"
assert decode_output(b"x" * 9999).endswith("…(截断)")

# zip 解包：展平 + 只落文件
src = tmp / "src.zip"
with zipfile.ZipFile(src, "w") as zf:
    zf.writestr("a.exe", "1")
    zf.writestr("sub/dir/b.exe", "2")
    zf.writestr("C:\\evil\\evil.exe", "3")
r = extract_zip(src, tmp / "dest")
assert r["ok"] and r["extracted"] == 3, r
assert (tmp / "dest" / "a.exe").is_file()
assert (tmp / "dest" / "b.exe").is_file()
assert not (tmp / "dest" / "sub").exists()

# 下载参数校验（不真联网）
assert not download_file("ftp://x/y.exe", tmp)["ok"]
assert not download_file("https://invalid.invalid/x.exe", tmp)["ok"]

# 最新版探测：纯函数 + 假 probe，不联网
assert tuba_version_key("2026.08") == "202608"
assert tuba_version_key("图吧工具箱202608") == "202608"
assert tuba_version_key("v2026.8") == "202608"
assert tuba_version_key("") == ""
assert tuba_version_key("2026.13") == ""
assert tuba_candidate_months(2026, 1)[:3] == ["202601", "202512", "202511"]
assert len(tuba_candidate_months(2026, 12, 12)) == 12
import datetime  # noqa: E402

seen = []


def fake_probe(u):
    seen.append(u)
    return "202608" in u


r = tuba_find_latest("202512", today=datetime.date(2026, 11, 13), probe=fake_probe)
assert r["ok"] and r["month"] == "202608" and r["newer"] is True, r
assert r["url"] == TUBA_URL_TEMPLATE.replace("{ym}", "202608")
r = tuba_find_latest("202608", today=datetime.date(2026, 11, 13), probe=fake_probe)
assert r["ok"] and r["month"] == "202608" and r["newer"] is False, r
r = tuba_find_latest("", today=datetime.date(2026, 11, 13), probe=lambda u: False)
assert r["ok"] and r["month"] == "" and r["newer"] is False, r
assert tuba_find_latest("202608", template="https://x/y.exe")["ok"] is False

# 真跑 Sysinternals（本机）
suite = Path(r"D:\SysinternalsSuite")
if (suite / "hex2dec.exe").is_file():
    out = run_exe(suite / "hex2dec.exe", ["0x2A"], wait=True, timeout_seconds=15, accept_eula=True)
    assert out["mode"] == "exited" and "42" in out["stdout"], out
    gui = run_exe(suite / "hex2dec.exe", [], wait=False, timeout_seconds=1, accept_eula=True)
    assert gui["mode"] == "launched" and gui["pid"] > 0
bad = tmp / "badgui.exe"
bad.write_bytes(b"not a pe")
assert run_exe(bad, [], wait=False, timeout_seconds=1, accept_eula=True)["mode"] == "error"

# 真读注册表（本机）
reg = tuba_from_registry()
if reg:
    assert "图吧工具箱" in reg["display_name"], reg
    exe = find_tuba_exe(reg["install_location"])
    assert exe is None or exe.name.startswith("图吧"), exe

# Windows-MCP 协议栈：假 stdio 服务器全链路（initialize → tools/list → tools/call → kill）
FAKE = (
    "import sys, json, os\n"
    "for raw in sys.stdin:\n"
    "    msg = json.loads(raw)\n"
    "    m = msg.get('method')\n"
    "    if m == 'initialize':\n"
    "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{"
    "'protocolVersion':'2024-11-05','capabilities':{},"
    "'serverInfo':{'name':'fake','version':'0'}}}), flush=True)\n"
    "    elif m == 'tools/list':\n"
    "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{"
    "'tools':[{'name':'echo','description':'echo back'}]}}), flush=True)\n"
    "    elif m == 'tools/call':\n"
    "        if msg['params'].get('arguments', {}).get('env_probe'):\n"
    "            txt = 'POSTHOG_API_KEY=' + repr(os.environ.get('POSTHOG_API_KEY'))\n"
    "        else:\n"
    "            txt = 'pong'\n"
    "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{"
    "'content':[{'type':'text','text':txt}],'isError':False}}), flush=True)\n"
)
fake = tmp / "fake_wmcp.py"
fake.write_text(FAKE, encoding="utf-8")
from windows_mcp import WmcpClient, WmcpError, blocked_tools, decode_line, encode_msg  # noqa: E402

assert decode_line(encode_msg({"a": 1})) == {"a": 1}
assert decode_line(b"not json") is None
assert "powershell" in blocked_tools()
assert "screenshot" not in blocked_tools()
assert blocked_tools("registry", "") == frozenset({"registry"})
assert blocked_tools("", "PowerShell") == frozenset({"filesystem", "registry", "process", "clipboard"})
assert blocked_tools("", "powershell,filesystem,registry,process,clipboard") == frozenset()

client = WmcpClient(cmd=[sys.executable, str(fake)], start_timeout=20, request_timeout=20)
info = client.start()
assert info == {"started": True, "tools": 1}, info
assert client.call_tool("echo", {"x": 1}) == {"is_error": False, "content": [{"type": "text", "text": "pong"}]}
assert client.call_tool("echo", {"env_probe": True})["content"][0]["text"] == "POSTHOG_API_KEY=''", "空 key 必须传进子进程 env"
assert client.job_attached or sys.platform != "win32", "Job Object 应挂上"
assert client.child_pid and client.child_pid > 0
assert client.list_tools()[0]["name"] == "echo"
client.stop()
assert not client.alive
try:
    WmcpClient(cmd=["definitely-not-a-command-xyz"]).start()
    raise AssertionError("应报找不到命令")
except WmcpError as exc:
    assert "找不到命令" in str(exc), exc

print("selfcheck OK: wmcp-proto+env+job |", "sysinternals" if suite.is_dir() else "no-suite", "|", reg and reg["display_name"])
