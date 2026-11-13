"""system_tools：把 Sysinternals Suite 和图吧工具箱的能力交给 N.E.K.O.。

Sysinternals 优先用现成目录，没有就从 zip 解包到插件数据目录；图吧工具箱经
卸载注册表定位，可启动主程序、下载并运行安装包。全部文件/进程操作在本插件
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
            return Err(SdkError("注册表里没有图吧工具箱的安装记录。可先 tuba_download + tuba_install。"))
        exe = await asyncio.to_thread(_tools.find_tuba_exe, reg["install_location"])
        return Ok({**reg, "main_exe": str(exe) if exe else None})

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

    @plugin_entry(
        id="tuba_download",
        name="下载图吧工具箱安装包",
        description="从配置的 URL（默认 202608 安装包）下载安装包到插件数据目录 tuba/。文件数百 MB，耗时较长。",
        timeout=1800,
    )
    async def tuba_download(
        self,
        url: Annotated[str, "安装包 URL；空=配置 [tuba].url"] = "",
        filename: Annotated[str, "保存文件名；空=取 URL 文件名"] = "",
    ):
        cfg = await self._sec("tuba")
        use = url or str(cfg.get("url") or "")
        if not use:
            return Err(SdkError("未给 URL 且配置 [tuba].url 为空"))
        result = await asyncio.to_thread(_tools.download_file, use, self._data("tuba"), filename)
        if not result.get("ok"):
            return Err(SdkError(result.get("error", "下载失败")))
        return Ok(result)

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
            return Err(SdkError("没找到安装包。先调 tuba_download。"))
        cmd = [str(exe)] + (["/SILENT"] if silent else [])
        p = await asyncio.to_thread(subprocess.Popen, cmd, cwd=str(exe.parent))
        return Ok({"mode": "launched", "pid": p.pid, "installer": str(exe), "silent": bool(silent)})

    # ---------------------------------------------------------------- windows_mcp

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
    # ----------------------------------------------------------------- llm_tool

    @llm_tool(
        name="system_tools",
        description=(
            "PC 系统工具支持。Sysinternals 套件：sys_list 列工具（keyword 可过滤）、"
            "sys_run 运行（tool=工具名如 Procexp64/pingview/accesschk/hex2dec，args=参数串，"
            "GUI 工具 wait=false 只启动，命令行工具 wait=true 等输出）、sys_setup 从 zip 解包。"
            "图吧工具箱：tuba_detect 检测安装、tuba_launch 启动、tuba_download 下载安装包、"
            "tuba_install 运行安装包（silent=true 静默）。action=status 看两边现状。"
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
                             "tuba_detect", "tuba_launch", "tuba_download", "tuba_install",
                             "wmcp_status", "wmcp_list", "wmcp_call"],
                    "description": "要做的操作",
                },
                "tool": {"type": "string", "description": "sys_run=Sysinternals 工具名；wmcp_call=Windows-MCP 工具名"},
                "args": {"type": "string", "description": "sys_run 的参数串；wmcp_call 的参数 JSON 对象串"},
                "wait": {"type": "boolean", "description": "sys_run 是否等输出，GUI 工具填 false"},
                "keyword": {"type": "string", "description": "sys_list 过滤关键字"},
                "zip_path": {"type": "string", "description": "sys_setup 的 zip 路径，空用默认"},
                "path": {"type": "string", "description": "tuba_launch/tuba_install 的文件路径，空自动定位"},
                "silent": {"type": "boolean", "description": "tuba_install 是否静默安装"},
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
        elif action == "tuba_download":
            r = await self.tuba_download("", "")
        elif action == "tuba_install":
            r = await self.tuba_install(path, silent)
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
