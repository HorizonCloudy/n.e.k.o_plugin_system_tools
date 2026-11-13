"""Windows-MCP（CursorTouch/Windows-MCP）的 MCP stdio 客户端，纯标准库。

默认子命令 `uvx windows-mcp serve`。协议 = 换行分隔的 JSON-RPC（MCP stdio）。

两条加固（2026-11-13 实读上游 0.8.9 代码后落地）：
- 拉起子进程时置 POSTHOG_API_KEY=""：上游 analytics 以空 key 为关
  （infrastructure/analytics.py: API_KEY=os.environ.get("POSTHOG_API_KEY", ...)，
  空则 client=None、track_* 不发；is_feature_enabled 全包无调用方，无功能牵连）。
- 子进程挂进 KILL_ON_JOB_CLOSE 的 Job Object：宿主强杀本插件进程时，
  uvx 及其后代由内核回收，不留孤儿。
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import shutil
import subprocess
import threading
import time

DEFAULT_CMD = ["uvx", "windows-mcp", "serve"]
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "neko-system-tools", "version": "0.3.0"}
CHILD_ENV = {"POSTHOG_API_KEY": ""}

DEFAULT_BLOCKED = frozenset({"powershell", "filesystem", "registry", "process", "clipboard"})


# uvx 默认启动命令：钉死 windows-mcp 0.8.9（本插件按该版本实测）。
DEFAULT_ARGS = "--from windows-mcp==0.8.9 windows-mcp serve"


def blocked_tools(blocked_csv: str = "", allow_csv: str = "", replace: bool = False) -> frozenset:
    """默认拦截系统级破坏面。

    blocked_csv 默认是【追加】到拦截表；replace=True 才整表覆盖（旧行为）。
    allow_csv 从表中摘除。
    """
    extra = {t.strip().casefold() for t in (blocked_csv or "").split(",") if t.strip()}
    if (blocked_csv or "").strip() and replace:
        base = set(extra)
    else:
        base = set(DEFAULT_BLOCKED) | extra
    drop = {t.strip().casefold() for t in (allow_csv or "").split(",") if t.strip()}
    return frozenset(base - drop)


class WmcpError(RuntimeError):
    pass


# ---------------------------------------------------------------- job object

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def attach_kill_job(proc: subprocess.Popen) -> bool:
    """把子进程（及其后代）绑进随句柄关闭而全杀的 Job。失败返回 False，不抛。"""
    if os.name != "nt":
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        size = ctypes.sizeof(info)
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.SetInformationJobObject.restype = ctypes.c_bool
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.AssignProcessToJobObject.restype = ctypes.c_bool
        # 句柄故意不 CloseHandle：随进程退出自动关闭，触发 Job 清理。
        hjob = k32.CreateJobObjectW(None, None)
        if not hjob:
            return False
        if not k32.SetInformationJobObject(hjob, 9, ctypes.byref(info), size):  # 9 = JobObjectExtendedLimitInformation
            return False
        # Windows 的 Popen 句柄属性是 _handle（CPython 事实契约）。
        return bool(k32.AssignProcessToJobObject(hjob, proc._handle))
    except Exception:
        return False


# ---------------------------------------------------------------- protocol


def encode_msg(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=True) + "\n").encode("utf-8")


def decode_line(line: bytes) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


class WmcpClient:
    """一个常驻子进程；request 加锁，允许多入口并发；stdout/stderr 各有读线程。"""

    def __init__(self, cmd: list[str] | None = None, start_timeout: float = 240.0,
                 request_timeout: float = 120.0):
        self.cmd = list(cmd or DEFAULT_CMD)
        self.start_timeout = start_timeout
        self.request_timeout = request_timeout
        self._proc: subprocess.Popen | None = None
        self._job_attached = False
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._tools: list[dict] = []
        self._id = 0
        self._lock = threading.Lock()
        self._stderr_tail: list[str] = []

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def tools(self) -> list[dict]:
        return list(self._tools)

    @property
    def child_pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def job_attached(self) -> bool:
        return self._job_attached

    # ------------------------------------------------------------ lifecycle

    def start(self, force: bool = False) -> dict:
        with self._lock:
            if self.alive and not force:
                return {"started": True, "tools": len(self._tools)}
            return self._start_locked()

    def _start_locked(self) -> dict:
        self._stop_locked()
        exe = shutil.which(self.cmd[0])
        if exe is None:
            raise WmcpError(f"找不到命令 {self.cmd[0]!r}，先安装 uv（winget install astral-sh.uv）")
        env = {**os.environ, **CHILD_ENV}
        popen_kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, bufsize=0, env=env)
        if os.name == "nt":
            # 父进程若也在 Job 里（沙箱/启动器），需 breakaway 才能挂自己的 Job；
            # 不允许 breakaway 时退回普通启动，兜底只剩 shutdown 钩子。
            try:
                self._proc = subprocess.Popen(
                    self.cmd, creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB, **popen_kw)
            except OSError:
                self._proc = subprocess.Popen(self.cmd, **popen_kw)
        else:
            self._proc = subprocess.Popen(self.cmd, **popen_kw)
        self._job_attached = attach_kill_job(self._proc)
        self._lines = queue.Queue()
        self._stderr_tail = []
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            timeout=self.start_timeout,
        )
        self._notify("notifications/initialized", {})
        self._tools = self._request("tools/list", {}, timeout=self.request_timeout).get("tools", [])
        return {"started": True, "tools": len(self._tools)}

    def stop(self) -> dict:
        with self._lock:
            self._stop_locked()
        return {"stopped": True}

    def _stop_locked(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None
        self._job_attached = False
        self._tools = []

    # ------------------------------------------------------------ requests

    def list_tools(self) -> list[dict]:
        with self._lock:
            if not self.alive:
                self._start_locked()
            self._tools = self._request("tools/list", {}, timeout=self.request_timeout).get("tools", [])
            return list(self._tools)

    def call_tool(self, name: str, arguments: dict) -> dict:
        with self._lock:
            if not self.alive:
                self._start_locked()
            result = self._request(
                "tools/call", {"name": name, "arguments": arguments},
                timeout=self.request_timeout,
            )
        return {"is_error": bool(result.get("isError", False)), "content": result.get("content", [])}

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        assert self._proc is not None
        self._id += 1
        rid = self._id
        self._proc.stdin.write(encode_msg(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        self._proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WmcpError(
                    f"等待 {method} 响应超时（{timeout:.0f}s）。"
                    "首次运行 uvx 需拉解释器和依赖，可能要 1~2 分钟，重试即可")
            try:
                line = self._lines.get(timeout=min(remaining, 2.0))
            except queue.Empty:
                continue
            if line is None:  # EOF
                raise WmcpError(f"子进程在等 {method} 时退出；{self._stderr_hint()}")
            msg = decode_line(line)
            if msg is None or msg.get("id") != rid:
                continue
            if "error" in msg:
                err = msg["error"]
                raise WmcpError(f"{method} 失败: {err.get('message', err)}")
            return msg.get("result", {})

    def _notify(self, method: str, params: dict) -> None:
        assert self._proc is not None
        self._proc.stdin.write(encode_msg({"jsonrpc": "2.0", "method": method, "params": params}))
        self._proc.stdin.flush()

    # ------------------------------------------------------------ readers

    def _drain_stdout(self) -> None:
        assert self._proc is not None
        try:
            for line in self._proc.stdout:
                self._lines.put(line)
        finally:
            self._lines.put(None)

    def _drain_stderr(self) -> None:
        assert self._proc is not None
        for raw in self._proc.stderr:
            self._stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())
            del self._stderr_tail[:-30]

    def _stderr_hint(self) -> str:
        tail = " | ".join(self._stderr_tail[-3:])
        return f"stderr: {tail}" if tail else "子进程无 stderr 输出"
