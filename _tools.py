"""system_tools 纯逻辑层：解包、跑程序、下载、读注册表。

不 import plugin.sdk，可用 `python selfcheck.py` 本地核验。
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

MAX_CAPTURE_CHARS = 4000
DOWNLOAD_UA = "Mozilla/5.0 (compatible; NEKO-system-tools/0.1)"
TUBA_NAME_HINT = "图吧工具箱"
# 镜像按 图吧工具箱YYYYMM安装包.exe 命名；{ym} 由代码替换。
TUBA_URL_TEMPLATE = "https://apac.tualatin.club/%E5%9B%BE%E5%90%A7%E5%B7%A5%E5%85%B7%E7%AE%B1{ym}%E5%AE%89%E8%A3%85%E5%8C%85.exe"


def safe_tool_exe(root: Path, name: str) -> Path | None:
    """校验工具名（只允许纯文件名），返回 root 下的 exe；非法或不存在返回 None。"""
    name = (name or "").strip()
    if not name or len(name) > 64 or name != Path(name).name or "\\" in name or "/" in name:
        return None
    if not name.lower().endswith(".exe"):
        name += ".exe"
    exe = root / name
    return exe if exe.is_file() else None


def list_tools(root: Path, keyword: str = "", limit: int = 300) -> list[str]:
    kw = (keyword or "").lower()
    names = sorted(p.name for p in root.glob("*.exe") if kw in p.name.lower())
    return names[:limit]


def extract_zip(zip_path: Path, dest_dir: Path) -> dict:
    """解包 Sysinternals zip。只取条目的纯文件名写进 dest（顶层展平，天然防路径逃逸）。"""
    if not zip_path.is_file():
        return {"ok": False, "error": f"zip 不存在: {zip_path}"}
    dest_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename.replace("\\", "/")).name
            if not name or name == ".":
                continue
            with zf.open(info) as src, (dest_dir / name).open("wb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
            count += 1
    return {"ok": True, "extracted": count, "dest": str(dest_dir)}


def decode_output(data: bytes) -> str:
    if not data:
        return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("gbk", errors="replace")
    if len(text) > MAX_CAPTURE_CHARS:
        text = text[:MAX_CAPTURE_CHARS] + "\n…(截断)"
    return text


def split_args(text: str) -> list[str]:
    """参数串按 shell 风格拆分；双引号包裹整体（含空格/反斜杠路径）。"""
    if not text or not text.strip():
        return []
    out = []
    for tok in shlex.split(text, posix=False):
        if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
            tok = tok[1:-1]
        out.append(tok)
    return out


def pid_by_image(image_name: str) -> int | None:
    """tasklist 按镜像名找第一个匹配 pid；找不到返回 None。"""
    r = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
    )
    for line in (r.stdout or "").splitlines():
        parts = line.split('","')
        if len(parts) >= 2 and parts[0].strip('"').lower() == image_name.lower():
            try:
                return int(parts[1])
            except ValueError:
                pass
    return None


def shell_execute_runas(exe: Path, argv: list[str]) -> int | None:
    """runas 动词经 UAC 提权启动；用户拒绝或启动后找不到进程返回 None。

    shortcut: ShellExecuteW 不回传进程句柄，pid 靠 tasklist 按镜像名回查；
    同名多实例时取第一个。要精确句柄再换 ShellExecuteExW+正确结构体。
    """
    import ctypes
    import ctypes.wintypes as wt

    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]
    h = shell32.ShellExecuteW(None, "runas", str(exe), subprocess.list2cmdline(list(argv)), str(exe.parent), 1)
    if not h or int(h) <= 32:
        return None
    return pid_by_image(exe.name)


def run_exe(exe: Path, argv: list[str], *, wait: bool, timeout_seconds: int, accept_eula: bool) -> dict:
    cmd = [str(exe)] + list(argv)
    if accept_eula and not any(a.lower() in ("/accepteula", "-accepteula") for a in argv):
        cmd.append("/accepteula")
    if not wait:
        try:
            p = subprocess.Popen(cmd, cwd=str(exe.parent))
            return {"mode": "launched", "pid": p.pid, "tool": exe.name}
        except OSError as exc:
            if getattr(exc, "winerror", None) != 740:  # 只有"要求提权"才走 UAC
                return {"mode": "error", "tool": exe.name, "note": f"{type(exc).__name__}: {exc}"}
            pid = shell_execute_runas(exe, argv)
            if pid is None:
                return {"mode": "error", "tool": exe.name, "note": "UAC 被拒绝/取消，或提权后没找到进程"}
            return {"mode": "launched", "pid": pid, "tool": exe.name, "elevated": True,
                    "note": "普通启动需要管理员，已经 UAC 确认后启动"}
    try:
        r = subprocess.run(cmd, cwd=str(exe.parent), capture_output=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return {"mode": "timeout", "tool": exe.name, "note": f"{timeout_seconds}s 内未退出，进程已终止；GUI 工具请改用 wait=false"}
    except OSError as exc:
        return {"mode": "error", "tool": exe.name, "note": f"{type(exc).__name__}: {exc}"}
    return {
        "mode": "exited",
        "tool": exe.name,
        "exit_code": r.returncode,
        "stdout": decode_output(r.stdout),
        "stderr": decode_output(r.stderr),
    }


def _reg_str(key, field: str) -> str:
    import winreg

    try:
        value = winreg.QueryValueEx(key, field)[0]
        return str(value) if value else ""
    except OSError:
        return ""


def tuba_from_registry() -> dict | None:
    """扫卸载注册表找已安装的图吧工具箱，多条记录取 DisplayVersion 最大的一条。"""
    if sys.platform != "win32":
        return None
    import winreg

    best: dict | None = None
    targets = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, sub_key in targets:
        try:
            parent = winreg.OpenKey(hive, sub_key)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(parent)[0]
            for i in range(count):
                try:
                    with winreg.OpenKey(parent, winreg.EnumKey(parent, i)) as item:
                        display = _reg_str(item, "DisplayName")
                        if TUBA_NAME_HINT not in display:
                            continue
                        found = {
                            "display_name": display,
                            "version": _reg_str(item, "DisplayVersion"),
                            "install_location": _reg_str(item, "InstallLocation"),
                            "uninstall_string": _reg_str(item, "UninstallString"),
                        }
                except OSError:
                    continue
                if best is None or found["version"] > best["version"]:
                    best = found
        finally:
            winreg.CloseKey(parent)
    return best


def find_tuba_exe(install_location: str) -> Path | None:
    """安装目录里找主程序：优先 图吧*.exe，其次含"工具箱"的 exe；跳过卸载器。"""
    if not install_location:
        return None
    root = Path(install_location)
    if not root.is_dir():
        return None
    exes = [p for p in root.glob("*.exe") if not p.name.lower().startswith("unins")]
    for pred in (lambda p: p.name.startswith("图吧"), lambda p: "工具箱" in p.name):
        cands = [p for p in exes if pred(p)]
        if cands:
            return max(cands, key=lambda p: (p.stat().st_mtime, p.name))
    return None


def tuba_version_key(display: str) -> str:
    """版本串归一化成 YYYYMM 便于排序："2026.08"/"图吧工具箱202608"→"202608"；无匹配→空串。"""
    m = re.search(r"(20\d{2})[.\-_ ]?(\d{1,2})", display or "")
    if not m:
        return ""
    month = int(m.group(2))
    return f"{m.group(1)}{month:02d}" if 1 <= month <= 12 else ""


def tuba_candidate_months(year: int, month: int, back: int = 12) -> list[str]:
    """从给定年月起逐月往回生成 YYYYMM 候选（新→旧）。"""
    out: list[str] = []
    for _ in range(back):
        out.append(f"{year}{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out


def url_available(url: str, timeout: float = 15) -> bool:
    """只取第 1 字节的 Range GET 探测文件是否存在；200/206 都算在。失败重试一次。"""
    import time as _time

    for attempt in (0, 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_UA, "Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status in (200, 206)
        except Exception:
            if attempt == 0:
                _time.sleep(2)
    return False


def tuba_find_latest(installed_key: str, today=None, template: str = TUBA_URL_TEMPLATE,
                     probe=url_available, back: int = 12) -> dict:
    """从当月往回探测最新可达安装包，不探比已装更旧的月份。

    month="" 表示没探到任何不低于已装版本的包；newer=是否比已装新。
    """
    if "{ym}" not in template:
        return {"ok": False, "error": "url_template 必须含 {ym}"}
    if today is None:
        import datetime
        today = datetime.date.today()
    for ym in tuba_candidate_months(today.year, today.month, back):
        if installed_key and ym < installed_key:
            break
        url = template.replace("{ym}", ym)
        if probe(url):
            return {"ok": True, "month": ym, "url": url, "newer": ym > installed_key}
    return {"ok": True, "month": "", "url": "", "newer": False}


def download_file(url: str, dest_dir: Path, filename: str = "") -> dict:
    """下载文件到 dest_dir（先写 .part 再改名）。只允许 http/https。"""
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return {"ok": False, "error": "只允许 http/https 地址"}
    safe_path = urllib.parse.quote(urllib.parse.unquote(parts.path))
    fixed = urllib.parse.urlunsplit((parts.scheme, parts.netloc, safe_path, parts.query, ""))
    name = filename or Path(urllib.parse.unquote(parts.path)).name
    name = Path(name).name
    if not name or name == ".":
        return {"ok": False, "error": "无法从 URL 得到文件名"}
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(fixed, headers={"User-Agent": DOWNLOAD_UA})
    done = 0
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, part.open("wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
    except Exception as exc:
        part.unlink(missing_ok=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    part.replace(dest)
    return {"ok": True, "file": str(dest), "bytes": done, "total_bytes": total}


