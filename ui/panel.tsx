import { Page, Card, Stack, Grid, Text, StatCard, ActionButton, RefreshButton, Input } from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type State = {
  sysinternals?: { root?: string; tools?: number; zip_present?: boolean }
  tuba?: { installed?: boolean; version?: string; main_exe?: string; installer?: string }
  wmcp?: { uvx_found?: boolean; alive?: boolean; tools?: number }
}

export default function Panel(props: PluginSurfaceProps<State>) {
  const { state, actions, api } = props
  const find = (id: string) => actions.find((a) => a.id === id) as HostedAction | undefined
  const launch = find("tuba_launch")
  const update = find("tuba_update")
  const operate = find("operate_tool")
  const snapshot = find("wmcp_call")
  const [toolName, setToolName] = props.useLocalState("tool_name", "Procexp64")

  return (
    <Page title="系统工具" subtitle="Sysinternals · 图吧工具箱 · Windows-MCP 桌面自动化">
      <Grid cols={4}>
        <StatCard label="Sysinternals 工具" value={state.sysinternals?.tools ?? 0} />
        <StatCard label="图吧工具箱" value={state.tuba?.installed ? state.tuba?.version || "已安装" : "未安装"} />
        <StatCard label="Windows-MCP" value={state.wmcp?.alive ? `已就绪 ${state.wmcp?.tools ?? 0} 工具` : state.wmcp?.uvx_found ? "未启动" : "缺 uvx"} />
        <StatCard label="Sysinternals zip" value={state.sysinternals?.zip_present ? "在位" : "缺失"} />
      </Grid>

      <Card title="图吧工具箱">
        <Stack gap={8}>
          {update ? (
            <ActionButton action={update} label="下载并安装最新版" values={{ force: false, silent: true }} />
          ) : (
            <Text>未暴露 tuba_update 动作</Text>
          )}
          <Stack gap={8}>
            {launch ? <ActionButton action={launch} label="启动主程序" /> : null}
            {update ? (
              <ActionButton
                action={update}
                label="下载最新版并弹安装界面（手动点）"
                values={{ force: true, silent: false }}
              />
            ) : null}
          </Stack>
          <Text>状态每点一次按钮自动刷新；截图在插件缓存目录。</Text>
        </Stack>
      </Card>

      <Card title="按屏幕情况操控软件">
        <Stack gap={8}>
          <Text>先在 Sysinternals 或图吧工具箱里打开一个 GUI，抓界面快照；之后在对话里让猫娘用 Click / Type / Shortcut 逐步操作。</Text>
          <Stack gap={8}>
            <Input value={toolName} placeholder="Sysinternals 工具名，如 Procexp64" onChange={setToolName} />
            {operate ? (
              <ActionButton action={operate} label="打开该工具并截屏" values={{ source: "sysinternals", name: toolName }} />
            ) : null}
            <Grid cols={3}>
              {operate
                ? ["DiskView", "Tcpview", "AutoRuns"].map((n) => (
                    <ActionButton
                      key={n}
                      action={operate}
                      label={`打开 ${n}`}
                      values={{ source: "sysinternals", name: n }}
                    />
                  ))
                : null}
            </Grid>
            {operate ? <ActionButton action={operate} label="打开图吧工具箱并截屏" values={{ source: "tuba" }} /> : null}
          </Stack>
          {snapshot ? (
            <ActionButton
              action={snapshot}
              label="截取当前桌面"
              values={{ tool: "Snapshot", args_json: "" }}
              refresh={false}
            />
          ) : null}
          <RefreshButton label="重新读取状态" onRefresh={() => api.refresh()} />
        </Stack>
      </Card>
    </Page>
  )
}
