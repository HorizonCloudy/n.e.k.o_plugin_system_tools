"""system_tools：把 Sysinternals Suite 和图吧工具箱的能力交给 N.E.K.O.。

Sysinternals 优先用现成目录，没有就从 zip 解包到插件数据目录；图吧工具箱经
卸载注册表定位，可启动主程序、探测并安装最新版。全部文件/进程操作在本插件
独立进程内完成，不注入被启动的工具，不主动提权。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Annotated

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    ui,
)

from . import _tools
from . import windows_mcp as _wmcp


@neko_plugin
class SystemToolsPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._wmcp_client: _wmcp.WmcpClient | None = None

    # ------------------------------------------------------------------ config

    async def _sec(self, name: str) -> dict:
        try:
            raw = await self.config.dump() or {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            return {}
        sec = raw.get(name)
        return sec if isinstance(sec, dict) else {}

    def _data(self, sub: str) -> Path:
        try:
            return Path(str(self.data_path(sub)))
        except Exception:
            return Path.cwd() / sub

    async def _sys_root(self) -> Path | None:
        """配置目录优先，其次插件解包目录。"""
        cfg = await self._sec("sysinternals")
        candidates = [str(cfg.get("root") or ""), str(self._data("sysinternals"))]
        for cand in candidates:
            p = Path(cand)
            if cand and p.is_dir() and next(p.glob("*.exe"), None):
                return p
        return None

    def _zip_default(self) -> Path:
        return Path.home() / "Downloads" / "SysinternalsSuite.zip"

    async def _wmcp_cfg(self) -> tuple[list[str], float]:
        cfg = await self._sec("windows_mcp")
        cmd = str(cfg.get("command") or "uvx")
        args = str(cfg.get("args") or "windows-mcp serve")
        timeout = float(cfg.get("start_timeout_seconds") or 240)
        return [cmd] + _tools.split_args(args), timeout

    def _wmcp_get(self, cmd: list[str], start_timeout: float) -> _wmcp.WmcpClient:
        c = self._wmcp_client
        if c is None or c.cmd != cmd or c.start_timeout != start_timeout:
            if c is not None:
                c.stop()
            self._wmcp_client = _wmcp.WmcpClient(cmd=cmd, start_timeout=start_timeout)
        return self._wmcp_client

    def _save_wmcp_image(self, tool: str, part: dict) -> str:
        import base64
        ext = ".png" if "png" in (part.get("mimeType") or "") else ".img"
        name = f"wmcp-{tool}-{int(time.time() * 1000)}{ext}"
        try:
            path = Path(str(self.cache_path(name)))
        except Exception:
            path = Path.cwd() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(part["data"]))
        return str(path)

    # ------------------------------------------------------------------ entries

    @plugin_entry(
        id="status",
        name="状态总览",
        description="只读汇总：Sysinternals 目录/工具数/zip 是否就位，图吧工具箱注册表信息与主程序路径，已下载的安装包。",
        timeout=60,
        llm_result_fields=["sysinternals_root", "sysinternals_tools", "sysinternals_zip_present", "tuba_installed", "tuba_main_exe", "tuba_installer"],
    )
    async def status(self):
        root = await self._sys_root()
        cfg = await self._sec("sysinternals")
        zip_cfg = Path(str(cfg.get("zip") or "")) if cfg.get("zip") else self._zip_default()
        reg = await asyncio.to_thread(_tools.tuba_from_registry)
        tuba_exe = await asyncio.to_thread(_tools.find_tuba_exe, reg["install_location"]) if reg else None
        tuba_dir = self._data("tuba")
        installers = sorted(
            tuba_dir.glob("*.exe"), key=lambda p: p.stat().st_mtime, reverse=True
        ) if tuba_dir.is_dir() else []
        return Ok({
            "sysinternals_root": str(root) if root else None,
            "sysinternals_tools": len(list(root.glob("*.exe"))) if root else 0,
            "sysinternals_zip_present": zip_cfg.is_file(),
            "tuba_installed": bool(reg),
            "tuba": reg,
            "tuba_main_exe": str(tuba_exe) if tuba_exe else None,
            "tuba_installer": str(installers[0]) if installers else None,
        })

    @plugin_entry(
        id="sysinternals_setup",
        name="解包 Sysinternals",
        description="把 Sysinternals zip 解包到插件数据目录。已配置现成目录（[sysinternals].root）时一般不需要。",
        timeout=600,
    )
    async def sysinternals_setup(
        self,
        zip_path: Annotated[str, "zip 绝对路径；空=配置值，再空=用户 Downloads\\SysinternalsSuite.zip"] = "",
    ):
        cfg = await self._sec("sysinternals")
        src = Path(zip_path or str(cfg.get("zip") or "") or str(self._zip_default()))
        if not src.is_file():
            return Err(SdkError(f"zip 不存在: {src}"))
        result = await asyncio.to_thread(_tools.extract_zip, src, self._data("sysinternals"))
        if not result.get("ok"):
            return Err(SdkError(result.get("error", "解包失败")))
        return Ok(result)

    @plugin_entry(
        id="sysinternals_list",
        name="列出 Sysinternals 工具",
        description="列出 Sysinternals 根目录下的 exe 工具名（最多 300 条），可按关键字过滤。",
        timeout=30,
    )
    async def sysinternals_list(self, keyword: Annotated[str, "过滤关键字，空=全部"] = ""):
        root = await self._sys_root()
        if not root:
            return Err(SdkError("未找到可用的 Sysinternals 目录。先调 sysinternals_setup，或改配置 [sysinternals].root。"))
        tools = await asyncio.to_thread(_tools.list_tools, root, keyword)
        return Ok({"root": str(root), "count": len(tools), "tools": tools})

    @plugin_entry(
        id="sysinternals_run",
        name="运行 Sysinternals 工具",
        description="运行一个 Sysinternals 工具。wait=true 等它退出并带回 stdout/stderr（适合命令行类）；wait=false 只启动进程返回 pid（适合 GUI 类，如 Procexp64、PingView、Tcpview）。自动追加 /accepteula。",
        timeout=180,
        llm_result_fields=["mode", "tool", "exit_code", "stdout", "stderr", "pid", "note"],
    )
    async def sysinternals_run(
        self,
        tool: Annotated[str, "工具名：纯文件名，如 hex2dec、accesschk、Procexp64（.exe 可省）"],
        args: Annotated[str, "参数串，空格分隔；双引号包住含空格的值"] = "",
        wait: Annotated[bool, "true=等待退出并捕获输出；false=只启动（GUI 工具用）"] = True,
        timeout_seconds: Annotated[int, "wait=true 时最多等多少秒"] = 20,
    ):
        root = await self._sys_root()
        if not root:
            return Err(SdkError("未找到可用的 Sysinternals 目录。先调 sysinternals_setup。"))
        exe = _tools.safe_tool_exe(root, tool)
        if exe is None:
            return Err(SdkError(f"工具不存在或名称非法: {tool}"))
        argv = _tools.split_args(args)
        capped = max(1, min(int(timeout_seconds or 20), 150))
        result = await asyncio.to_thread(
            _tools.run_exe, exe, argv, wait=wait, timeout_seconds=capped, accept_eula=True
        )
        if result.get("mode") == "error":
            return Err(SdkError(f"{result.get('tool')}: {result.get('note')}"))
        return Ok(result)

    @plugin_entry(
        id="tuba_detect",
        name="检测图吧工具箱",
        description="扫注册表卸载项，返回图吧工具箱的安装位置、版本、主程序路径和卸载命令。",
        timeout=30,
    )
    async def tuba_detect(self):
        reg = await asyncio.to_thread(_tools.tuba_from_registry)
        if not reg:
            return Err(SdkError("注册表里没有图吧工具箱的安装记录。可先 tuba_update 下载并安装最新版。"))
        exe = await asyncio.to_thread(_tools.find_tuba_exe, reg["install_location"])
        return Ok({**reg, "main_exe": str(exe) if exe else None})

    @ui.action(label="启动图吧工具箱", tone="default", group="tuba", order=10)
    @plugin_entry(
        id="tuba_launch",
        name="启动图吧工具箱",
        description="打开图吧工具箱主程序。path 留空时自动按注册表定位。",
        timeout=30,
    )
    async def tuba_launch(self, path: Annotated[str, "主程序 exe 绝对路径；空=自动检测"] = ""):
        exe = Path(path) if path else None
        if exe is None:
            reg = await asyncio.to_thread(_tools.tuba_from_registry)
            exe = await asyncio.to_thread(_tools.find_tuba_exe, reg["install_location"]) if reg else None
        if exe is None or not Path(exe).is_file():
            return Err(SdkError("找不到图吧工具箱主程序。给出 path 参数，或先 tuba_detect 看安装位置。"))
        exe = Path(exe)
        p = await asyncio.to_thread(subprocess.Popen, [str(exe)], cwd=str(exe.parent))
        return Ok({"mode": "launched", "pid": p.pid, "exe": str(exe)})

    @ui.action(label="检查并安装最新版", tone="primary", group="tuba", order=20,
               confirm="将下载并覆盖安装最新版图吧工具箱")
    @plugin_entry(
        id="tuba_update",
        name="更新图吧工具箱到最新版",
        description="下载并安装最新版：探测镜像上最新的 YYYYMM 安装包（从当月逐月往回探）；已装版本不低于最新就跳过并说明原因。有更新时下载到数据目录并静默安装（/VERYSILENT /NORESTART），装完重读注册表确认版本。force=true 时版本相同也重装；silent=false 弹安装界面由用户手点。",
        timeout=1800,
        llm_result_fields=["updated", "reason", "installed_before", "latest_month", "installer", "bytes", "exit_code", "installed_after", "note"],
    )
    async def tuba_update(
        self,
        force: Annotated[bool, "true=镜像没有更新的包也重装"] = False,
        silent: Annotated[bool, "true=静默安装并等装完回读版本"] = True,
        url_template: Annotated[str, "必须含 {ym} 的 URL 模板；空=配置 [tuba].url_template，再空=内置默认镜像"] = "",
    ):
        cfg = await self._sec("tuba")
        tpl = url_template or str(cfg.get("url_template") or "") or _tools.TUBA_URL_TEMPLATE
        reg = await asyncio.to_thread(_tools.tuba_from_registry)
        installed_key = _tools.tuba_version_key(str((reg or {}).get("version") or ""))
        found = await asyncio.to_thread(_tools.tuba_find_latest, installed_key, None, tpl)
        if not found.get("ok"):
            return Err(SdkError(found.get("error", "探测失败")))
        before = (reg or {}).get("version")
        if not found["month"]:
            if not force:
                return Ok({"updated": False, "installed_before": before, "latest_month": None,
                           "reason": f"已装 {installed_key or '无'} 不低于镜像最新，跳过"})
            return Err(SdkError("镜像按月往回探 12 个月没有可用安装包"))
        if not found["newer"] and not force:
            return Ok({"updated": False, "installed_before": before, "latest_month": found["month"],
                       "reason": f"已装 {installed_key} 即最新，跳过"})
        dl = await asyncio.to_thread(_tools.download_file, found["url"], self._data("tuba"))
        if not dl.get("ok"):
            return Err(SdkError("下载失败: " + str(dl.get("error"))))
        exe = Path(dl["file"])
        exit_code = None
        installed_after = None
        note = ""
        if silent:
            try:
                r = await asyncio.to_thread(
                    subprocess.run, [str(exe), "/VERYSILENT", "/NORESTART"], timeout=900
                )
                exit_code = r.returncode
                if exit_code != 0:
                    note = f"安装器退出码 {exit_code}，可能未完成"
            except subprocess.TimeoutExpired:
                note = "安装进程 900s 未退出，可能需要前台确认"
            reg2 = await asyncio.to_thread(_tools.tuba_from_registry)
            installed_after = (reg2 or {}).get("version")
            if not note and installed_after:
                note = f"安装后版本 {installed_after}"
        else:
            p = await asyncio.to_thread(subprocess.Popen, [str(exe)], cwd=str(exe.parent))
            note = f"交互式安装器已启动 pid={p.pid}，需用户手动点完"
        return Ok({
            "updated": True, "installed_before": before,
            "installed_before_key": installed_key, "latest_month": found["month"],
            "installer": str(exe), "bytes": dl.get("bytes"), "silent": silent,
            "exit_code": exit_code, "installed_after": installed_after, "note": note,
        })

    @plugin_entry(
        id="tuba_install",
        name="运行安装包",
        description="启动图吧工具箱安装包。path 留空取数据目录里最新的 exe；silent=true 走 Inno Setup /SILENT 静默安装。",
        timeout=1800,
    )
    async def tuba_install(
        self,
        path: Annotated[str, "安装包绝对路径；空=数据目录最新 exe"] = "",
        silent: Annotated[bool, "静默安装"] = False,
    ):
        if path:
            exe = Path(path)
        else:
            tuba_dir = self._data("tuba")
            cands = sorted(
                tuba_dir.glob("*.exe"), key=lambda p: p.stat().st_mtime, reverse=True
            ) if tuba_dir.is_dir() else []
            exe = cands[0] if cands else None
        if exe is None or not exe.is_file():
            return Err(SdkError("没找到安装包。先调 tuba_update。"))
        cmd = [str(exe)] + (["/SILENT"] if silent else [])
        p = await asyncio.to_thread(subprocess.Popen, cmd, cwd=str(exe.parent))
        return Ok({"mode": "launched", "pid": p.pid, "installer": str(exe), "silent": bool(silent)})

    # ---------------------------------------------------------------- windows_mcp

    @ui.action(label="打开工具并截屏观察", tone="default", group="operate", order=10)
    @plugin_entry(
        id="operate_tool",
        name="启动工具并截屏观察",
        description="按电脑当前情况操控软件的第一步：启动 Sysinternals 工具（source=sysinternals, name=工具名如 Procexp64/DiskView/AutoRuns/Tcpview）或图吧工具箱（source=tuba），等 GUI 出现后用 Windows-MCP Snapshot 抓界面文本和截图，返回快照文本+截图路径。之后用 wmcp_call 的 Click/Type/Shortcut/Scroll 操作界面，每步操作后再截屏确认变化。close_after=true 抓完按 pid 优雅关闭该程序（taskkill 发 WM_CLOSE，不碰其它窗口）。",
        timeout=300,
        llm_result_fields=["launched", "exe", "pid", "text", "files", "closed", "note"],
    )
    async def operate_tool(
        self,
        source: Annotated[str, "sysinternals 或 tuba"],
        name: Annotated[str, "source=sysinternals 时的工具名；source=tuba 忽略"] = "",
        wait_seconds: Annotated[int, "启动后等几秒再截屏，1~30"] = 3,
        close_after: Annotated[bool, "截屏后按 pid 优雅关闭该程序"] = False,
    ):
        if source == "sysinternals":
            root = await self._sys_root()
            if not root:
                return Err(SdkError("未找到可用的 Sysinternals 目录。先调 sysinternals_setup。"))
            exe = _tools.safe_tool_exe(root, name)
            if exe is None:
                return Err(SdkError(f"工具不存在或名称非法: {name}"))
        elif source == "tuba":
            reg = await asyncio.to_thread(_tools.tuba_from_registry)
            exe = await asyncio.to_thread(_tools.find_tuba_exe, reg["install_location"]) if reg else None
            if exe is None:
                return Err(SdkError("找不到图吧工具箱主程序。先 tuba_update 安装。"))
        else:
            return Err(SdkError("source 只能是 sysinternals 或 tuba"))
        started = await asyncio.to_thread(
            _tools.run_exe, exe, [], wait=False, timeout_seconds=1, accept_eula=True
        )
        await asyncio.sleep(max(1, min(int(wait_seconds or 3), 30)))
        snap = await self.wmcp_call("Snapshot", "")
        if not isinstance(snap, Ok):
            return snap
        closed = False
        pid = started.get("pid")
        if close_after and pid:
            r = await asyncio.to_thread(
                subprocess.run, ["taskkill", "/PID", str(pid)], capture_output=True
            )
            closed = r.returncode == 0
        v = snap.value
        note = "text 含焦点窗口与控件结构；files 是截图路径；继续操作用 wmcp_call"
        if close_after and not closed:
            note += "；窗口未能自动关闭（提权进程或已退出）"
        return Ok({
            "launched": started, "exe": str(exe), "pid": pid,
            "text": v["text"], "files": v["files"], "closed": closed, "note": note,
        })

    @plugin_entry(
        id="wmcp_status",
        name="Windows-MCP 状态",
        description="只读：uvx 是否存在、Windows-MCP 子进程是否已启动、工具数、启动命令。不拉起子进程。",
        timeout=30,
        llm_result_fields=["uvx_found", "alive", "plugin_pid", "child_pid", "job_attached", "tools", "cmd"],
    )
    async def wmcp_status(self):
        full, timeout = await self._wmcp_cfg()
        client = self._wmcp_client
        return Ok({
            "uvx_found": shutil.which(full[0]) is not None,
            "alive": bool(client and client.alive),
            "plugin_pid": os.getpid(),
            "child_pid": client.child_pid if client else None,
            "job_attached": bool(client and client.job_attached),
            "telemetry": "posthog off (empty POSTHOG_API_KEY)",
            "tools": len(client.tools) if client else 0,
            "cmd": " ".join(full),
            "start_timeout_seconds": timeout,
        })

    @plugin_entry(
        id="wmcp_list",
        name="Windows-MCP 工具列表",
        description="需要时拉起 Windows-MCP 子进程（首次 uvx 拉解释器和依赖要 1~2 分钟，超时重试即可），列出它暴露的桌面自动化工具。",
        timeout=600,
        llm_result_fields=["count", "tools"],
    )
    async def wmcp_list(self, keyword: Annotated[str, "过滤关键字，空=全部"] = ""):
        full, timeout = await self._wmcp_cfg()
        client = self._wmcp_get(full, timeout)
        try:
            tools = await asyncio.to_thread(client.list_tools)
        except _wmcp.WmcpError as exc:
            return Err(SdkError(str(exc)))
        kw = (keyword or "").lower()
        wcfg = await self._sec("windows_mcp")
        blocked = _wmcp.blocked_tools(str(wcfg.get("blocked") or ""), str(wcfg.get("allow") or ""))
        rows = [
            {"name": t.get("name", ""), "blocked": t.get("name", "").casefold() in blocked,
             "description": (t.get("description") or "")[:200]}
            for t in tools
            if kw in t.get("name", "").lower() or kw in (t.get("description") or "").lower()
        ]
        return Ok({"count": len(rows), "tools": rows[:100]})

    @ui.action(label="截取当前桌面", tone="default", group="operate", order=20,
               refresh_context=False)
    @plugin_entry(
        id="wmcp_call",
        name="调用 Windows-MCP",
        description="调用 Windows-MCP 的一个工具（鼠标/键盘/截图/窗口/应用等，会真实操作用户桌面）。args_json=参数 JSON 对象串。文本结果内联返回；截图等图片结果落盘成文件并返回路径。",
        timeout=300,
        llm_result_fields=["tool", "is_error", "text", "files"],
    )
    async def wmcp_call(
        self,
        tool: Annotated[str, "Windows-MCP 工具名，如 click / screenshot / type_text / list_windows"],
        args_json: Annotated[str, "参数 JSON 对象串，如 {\"x\":100,\"y\":200}；空=无参数"] = "",
    ):
        full, timeout = await self._wmcp_cfg()
        client = self._wmcp_get(full, timeout)
        wcfg = await self._sec("windows_mcp")
        blocked = _wmcp.blocked_tools(str(wcfg.get("blocked") or ""), str(wcfg.get("allow") or ""))
        if tool.strip().casefold() in blocked:
            return Err(SdkError(
                f"{tool.strip()} 已被默认拦截（系统级破坏面）。"
                "确需使用请在配置 [windows_mcp].allow 加入其名。"
            ))
        try:
            args = json.loads(args_json) if (args_json or "").strip() else {}
        except json.JSONDecodeError as exc:
            return Err(SdkError(f"args_json 不是合法 JSON: {exc}"))
        if not isinstance(args, dict):
            return Err(SdkError("args_json 必须是 JSON 对象"))
        try:
            result = await asyncio.to_thread(client.call_tool, tool, args)
        except _wmcp.WmcpError as exc:
            return Err(SdkError(str(exc)))
        texts, files = [], []
        for part in result["content"]:
            if part.get("type") == "text":
                texts.append(str(part.get("text", "")))
            elif part.get("type") == "image" and part.get("data"):
                files.append(await asyncio.to_thread(self._save_wmcp_image, tool, part))
        text = "\n".join(texts)[:4000]
        if result["is_error"]:
            return Err(SdkError(f"{tool}: {text or '工具报错，无文本输出'}"[:400]))
        return Ok({"tool": tool, "text": text, "files": files})

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        if self._wmcp_client is not None:
            await asyncio.to_thread(self._wmcp_client.stop)
        return Ok({"status": "stopped"})
    # --------------------------------------------------------------------- ui

    @ui.context(id="system_tools_panel", title="系统工具面板")
    async def panel_context(self):
        """只读状态，给 TSX 面板当 props.state。不拉起 wmcp 子进程。"""
        r = await self.status()
        v = r.value if isinstance(r, Ok) else {}
        full, _ = await self._wmcp_cfg()
        c = self._wmcp_client
        reg = v.get("tuba") or {}
        return {
            "sysinternals": {
                "root": v.get("sysinternals_root"),
                "tools": v.get("sysinternals_tools", 0),
                "zip_present": bool(v.get("sysinternals_zip_present")),
            },
            "tuba": {
                "installed": bool(v.get("tuba_installed")),
                "version": reg.get("version"),
                "main_exe": v.get("tuba_main_exe"),
                "installer": v.get("tuba_installer"),
            },
            "wmcp": {
                "uvx_found": shutil.which(full[0]) is not None,
                "alive": bool(c and c.alive),
                "tools": len(c.tools) if c else 0,
            },
        }

    # ----------------------------------------------------------------- llm_tool

    @llm_tool(
        name="system_tools",
        description=(
            "PC 系统工具支持。Sysinternals 套件：sys_list 列工具（keyword 可过滤）、"
            "sys_run 运行（tool=工具名如 Procexp64/pingview/accesschk/hex2dec，args=参数串，"
            "GUI 工具 wait=false 只启动，命令行工具 wait=true 等输出）、sys_setup 从 zip 解包。"
            "图吧工具箱：tuba_detect 检测安装、tuba_launch 启动、tuba_update 探测最新版并下载安装"
            "（已装即最新会跳过）、tuba_install 手动跑指定安装包。action=status 看两边现状。"
            "操控软件：operate_tool 启动 GUI（source=sysinternals/tuba）并抓界面快照+截图，"
            "再用 wmcp_call 的 Click/Type/Shortcut/Scroll 操作、Snapshot 复查。"
            "Windows-MCP 桌面自动化：wmcp_status 看就绪、wmcp_list 列工具（keyword 过滤）、"
            "wmcp_call 调用（tool=工具名，args=参数 JSON 对象串，如 {\"x\":100,\"y\":200}）；会真实操作鼠标键盘屏幕。"
            "PowerShell/FileSystem/Registry/Process/Clipboard 默认拦截，allow 配置可放行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "sys_list", "sys_run", "sys_setup",
                             "tuba_detect", "tuba_launch", "tuba_update", "tuba_install",
                             "operate_tool", "wmcp_status", "wmcp_list", "wmcp_call"],
                    "description": "要做的操作",
                },
                "tool": {"type": "string", "description": "sys_run=Sysinternals 工具名；wmcp_call=Windows-MCP 工具名"},
                "source": {"type": "string", "description": "operate_tool 的工具来源：sysinternals 或 tuba"},
                "name": {"type": "string", "description": "operate_tool 的 Sysinternals 工具名（tuba 忽略）"},
                "force": {"type": "boolean", "description": "tuba_update 版本相同也强制重装"},
                "args": {"type": "string", "description": "sys_run 的参数串；wmcp_call 的参数 JSON 对象串"},
                "wait": {"type": "boolean", "description": "sys_run 是否等输出，GUI 工具填 false"},
                "keyword": {"type": "string", "description": "sys_list 过滤关键字"},
                "zip_path": {"type": "string", "description": "sys_setup 的 zip 路径，空用默认"},
                "path": {"type": "string", "description": "tuba_launch/tuba_install 的文件路径，空自动定位"},
                "silent": {"type": "boolean", "description": "tuba_install/tuba_update 是否静默安装"},
            },
            "required": ["action"],
        },
        timeout=300.0,
    )
    async def system_tools(
        self,
        *,
        action: str = "status",
        tool: str = "",
        args: str = "",
        wait: bool = True,
        keyword: str = "",
        zip_path: str = "",
        path: str = "",
        silent: bool = False,
        source: str = "sysinternals",
        name: str = "",
        force: bool = False,
    ):
        if action == "status":
            r = await self.status()
        elif action == "sys_list":
            r = await self.sysinternals_list(keyword)
        elif action == "sys_run":
            r = await self.sysinternals_run(tool, args, wait)
        elif action == "sys_setup":
            r = await self.sysinternals_setup(zip_path)
        elif action == "tuba_detect":
            r = await self.tuba_detect()
        elif action == "tuba_launch":
            r = await self.tuba_launch(path)
        elif action == "tuba_update":
            r = await self.tuba_update(force, silent)
        elif action == "tuba_install":
            r = await self.tuba_install(path, silent)
        elif action == "operate_tool":
            r = await self.operate_tool(source, name)
        elif action == "wmcp_status":
            r = await self.wmcp_status()
        elif action == "wmcp_list":
            r = await self.wmcp_list(keyword)
        elif action == "wmcp_call":
            r = await self.wmcp_call(tool, args)
        else:
            return {"output": {"error": f"未知 action: {action}"}, "is_error": True}
        if isinstance(r, Ok):
            return {"output": r.value}
        return {"output": {"error": str(getattr(r, "error", r))}, "is_error": True}

    # ---------------------------------------------------------------- lifecycle

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        r = await self.status()
        if isinstance(r, Ok):
            v = r.value
            self.report_status({
                "status": "ready",
                "message": (
                    f"Sysinternals {'就位' if v['sysinternals_root'] else '未就位'}"
                    f"({v['sysinternals_tools']} 工具)，"
                    f"图吧工具箱{'已安装' if v['tuba_installed'] else '未安装'}"
                ),
            })
        return Ok({"status": "ready"})
