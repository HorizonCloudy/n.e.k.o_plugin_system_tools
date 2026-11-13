"""纯逻辑自检：python selfcheck.py。不需要宿主、不需要 pytest。"""
from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tools import (  # noqa: E402
    TUBA_URL_TEMPLATE,
    UNTRUSTED_TEXT_HEADER,
    danger_allowed,
    danger_tool,
    decode_output,
    download_file,
    extract_zip,
    find_tuba_exe,
    list_tools,
    run_exe,
    safe_tool_exe,
    path_is_under,
    signature_state,
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

# 安全边界纯函数
assert danger_tool("psexec64.exe") and danger_tool("PsExec.exe") and danger_tool("reg")
assert danger_tool("sdelete") and danger_tool("handle.exe") and danger_tool("RegDelNull64.exe")
assert not danger_tool("hex2dec.exe") and not danger_tool("regjump") and not danger_tool("Procexp64")
assert not danger_tool("disk2vhd64.exe")  # 磁盘成像是低风险，不在名单
assert danger_allowed("psexec64.exe", {"psexec"})
assert danger_allowed("PsExec", {"PsExec.exe"})
assert not danger_allowed("psexec", set())
ok_file = tmp / "under_ok.exe"
ok_file.write_bytes(b"x")
assert path_is_under(str(ok_file), str(tmp))
assert not path_is_under(str(tmp.parent / "other.exe"), str(tmp))
assert not path_is_under(str(r"D:\Windows\System32\notepad.exe" if sys.platform == "win32" else "/bin/ls"), str(tmp))
assert not path_is_under(str(tmp / "missing.exe"), str(tmp))
assert signature_state(Path(__file__)) in ("unsigned", "trusted", "skip") or signature_state(Path(__file__)).startswith(("untrusted", "error"))

# 下载带哈希：本地回环小服务器，不碰外网
import http.server
import socketserver
import threading


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"payload-123"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


with socketserver.TCPServer(("127.0.0.1", 0), _H) as srv:
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    got = download_file(f"http://127.0.0.1:{port}/pk.exe", tmp / "dl")
    srv.shutdown()
import hashlib as _hl
assert got["ok"] and got["sha256"] == _hl.sha256(b"payload-123").hexdigest().upper(), got
with socketserver.TCPServer(("127.0.0.1", 0), _H) as srv2:
    port2 = srv2.server_address[1]
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    bad = download_file(f"http://127.0.0.1:{port2}/pk.exe", tmp / "dl2", expected_sha256="0" * 64)
    srv2.shutdown()
assert not bad["ok"] and "SHA256 不符" in bad["error"] and "file" not in bad
with socketserver.TCPServer(("127.0.0.1", 0), _H) as srv3:
    port3 = srv3.server_address[1]
    threading.Thread(target=srv3.serve_forever, daemon=True).start()
    again = download_file(f"http://127.0.0.1:{port3}/pk.exe", tmp / "dl")
    srv3.shutdown()
assert again["ok"] and again.get("reused") is True and again["sha256"] == got["sha256"]
assert UNTRUSTED_TEXT_HEADER.startswith("⚠")

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
from windows_mcp import (  # noqa: E402
    DEFAULT_BLOCKED,
    WmcpClient,
    WmcpError,
    blocked_tools,
    decode_line,
    encode_msg,
)

assert decode_line(encode_msg({"a": 1})) == {"a": 1}
assert decode_line(b"not json") is None
assert "powershell" in blocked_tools()
assert "screenshot" not in blocked_tools()
# 追加语义：blocked 给值默认是加进表，不是替换表
assert blocked_tools("registry", "") == frozenset(set(DEFAULT_BLOCKED) | {"registry"})
assert blocked_tools("registry", "", replace=True) == frozenset({"registry"})
assert blocked_tools("", "PowerShell") == frozenset(set(DEFAULT_BLOCKED) - {"powershell"})
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
