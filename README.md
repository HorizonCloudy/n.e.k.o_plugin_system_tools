# system_tools — PC 系统工具支持

给 N.E.K.O. 三个工具源的支持：Sysinternals Suite、图吧工具箱、Windows-MCP。

## 能力

| 入口 | 作用 |
|---|---|
| `status` | 两边现状总览（目录、工具数、注册表、安装包） |
| `sysinternals_setup` | 从本地 `SysinternalsSuite.zip` 解包到插件数据目录 |
| `sysinternals_list` | 列出套件里的 exe 工具（可过滤） |
| `sysinternals_run` | 运行一个工具：`wait=true` 捕获输出；`wait=false` 启动 GUI 返回 pid；自动加 `/accepteula` |
| `tuba_detect` | 从卸载注册表定位图吧工具箱（安装位置 / 版本 / 主程序） |
| `tuba_launch` | 启动图吧工具箱主程序 |
| `tuba_download` | 下载 202608 安装包（URL 可配置）到插件数据目录 |
| `tuba_install` | 运行安装包，`silent=true` 走 `/SILENT` |

Windows-MCP（[CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP)）经 `uvx windows-mcp serve` 子进程接入：

| 入口 | 作用 |
|---|---|
| `wmcp_status` | uvx 是否存在、子进程是否活着、工具数。不拉起 |
| `wmcp_list` | 需要时拉起子进程，列出 21 个桌面工具（blocked 标记默认拦截项） |
| `wmcp_call` | 代理调用任意未拦截工具；文本内联，截图落盘返回文件路径 |

默认拦截 `PowerShell`、`FileSystem`、`Registry`、`Process`、`Clipboard`（系统级破坏面）。确需放行：改配置 `[windows_mcp].allow`。

对话期有 `@llm_tool` `system_tools`（`action` 分发以上全部）。

## 配置（安装后在用户数据目录 config/plugin.toml 改）

- `[sysinternals].root`：现成套件目录；默认 `D:\SysinternalsSuite`。留空则只用插件解包的副本。
- `[sysinternals].zip`：官方 zip 路径；默认 `C:\Users\<你>\Downloads\SysinternalsSuite.zip`。
- `[tuba].url`：图吧工具箱安装包地址；默认 `https://apac.tualatin.club/图吧工具箱202608安装包.exe`。

## 安全边界

- 工具名只允许纯文件名，程序必须位于套件目录内（防路径逃逸）。
- 参数按列表传给 `subprocess`，不经过 shell。
- 不注入被启动的进程，不主动提权；需要管理员的工具由 Windows 自行处理 UAC。
- 下载只允许 http/https，先写 `.part` 再改名，失败即删。
- Windows-MCP 的系统级工具默认拦截；鼠标键盘类工具会真实操作桌面，由模型显式调用。
- 宿主强杀本插件进程时，`uvx` 子进程可能短暂无主；正常 stop/reload 会杀掉它。

## 自检

`python selfcheck.py`：校验解包、工具名、参数拆分、输出解码，并实测 hex2dec、注册表探测和 Windows-MCP 假服务器协议全链路。

---

包：dist\system_tools-0.3.0.neko-plugin（24111 B，sha256 67D5BC2CD946B72B92B4309AACF50DEB375D50790DE86EF846C5E6C8A52FE3F7，本地构建不入库）。
