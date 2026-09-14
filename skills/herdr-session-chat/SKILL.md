---
name: herdr-session-chat
description: "Session chat:通过 herdr pane 把任务派发给新的子会话(免确认 claude)并行执行,子会话完成后经 herdr 通道把结果汇报回主会话。非阻塞:主会话不等结果,继续和用户对话。当运行在 Herdr 内(HERDR_ENV=1)且用户想把任务分派出去、开几个 pane 让子会话干活/汇报、提到 session-chat、派活、委派子任务时使用。普通终端里不适用。"
---

# Session Chat — Herdr pane 间的聊天式委派

把 herdr 当成会话间的聊天通道:主会话(你)开 pane 起免确认的 claude 子会话,把任务作为"消息"发过去;子会话干完活把结果作为"消息"发回来。全程异步,没有一处阻塞等待。

## 前提

```bash
test "${HERDR_ENV:-}" = 1
```

不满足就说明当前不在 Herdr 内,告诉用户并停止。满足时可配合 herdr skill 使用;本 skill 自包含所需命令,不依赖 herdr skill 已加载。

## 你的身份

- `$HERDR_PANE_ID` 是你所在 pane 的 ID,**也是子会话回信给你的地址**。
- 可选自查:`herdr agent get "$HERDR_PANE_ID"` 能返回你自己的 agent 信息;若你的 pane 未被识别为 agent(少数情况),回信仍可送达 pane,但收割兜底会失效——此时改用纯文件交接(见兜底)。

## 派生一个 worker

```bash
# 1. 目录(回信详情文件放这里)
mkdir -p /tmp/session-chat

# 2. 邻居 pane 开一个兄弟 pane;先看几何:宽→right,窄或高→down
herdr pane layout --pane "$HERDR_PANE_ID"
herdr pane split --current --direction right --cwd "$PWD" --no-focus
# 从 .result.pane.pane_id 读取新 pane ID

# 3. 起免确认 claude(名字唯一,匹配 [a-z][a-z0-9_-]{0,31},建议 sc- 前缀)
herdr agent start sc-<任务名> --kind claude --pane <pane-id> -- --dangerously-skip-permissions

# 4. 发任务:一次提交,不带 --wait,发完立即返回继续当前对话
#    ⚠️ 回信目标必须写成你的 pane ID 字面值(如 w1:p3),不要留 $HERDR_PANE_ID 字样:
#    任务文本会被粘贴进 worker 的输入框,任何未展开的变量都会在 worker 的 shell 里
#    展开——变成 worker 自己的 pane ID,回信就发给自己了。用双引号让变量在此处展开,
#    发送前自查:任务文本里不应再出现 "$HERDR" 字样。
herdr agent prompt sc-<任务名> "<任务描述>

规则:
- 自主完成到底,不要中途停下来等确认。
- 完成后把完整结果(改动摘要、结论、关键文件)写入 /tmp/session-chat/<任务名>.md
- 写签收单 /tmp/session-chat/pending/<你的pane-ID>.json——文件名用你环境变量 HERDR_PANE_ID 的值(注意:不要字面引用变量名),内容:{"target":"<主会话-pane-ID-字面值>","summary":"<一行结果摘要>","detail":"/tmp/session-chat/<任务名>.md"}
- 然后回信(原样执行,目标是主会话 pane 的 ID 字面值):
  herdr agent prompt <你的-pane-ID-字面值> '[session-chat] <任务名>: <一行结果摘要> 详情: /tmp/session-chat/<任务名>.md'
- 回信成功后删除签收单文件——它只在回信失败时留给守护插件代投。
- 若回信返回 agent_blocked,等 30 秒重试一次;仍失败就保留签收单,不再重试,守护插件会接管投递。"
```

要点:

- `agent start` 会等到 worker 真正就绪才返回(默认 30s)。返回 `agent_not_ready` 说明启动中就被阻塞:用 `herdr agent read <pane-id> --source recent-unwrapped --lines 40` 看卡在哪,然后分类处理——**机械性确认**(文件夹信任、任务范围内的权限询问)可以代按,但代按前必须核对弹窗内容与派发任务一致,且权限选择取最小授权(选"仅本次",拒绝会话级放行);**拿不准或超出任务范围的审批**,问用户。
- 派发后 30~60 秒做一次 `agent get` 体检,发现 blocked 按上述分类处理,直到它回到 working/idle/done。未装传输插件时,周期体检是防止 worker 无声卡住的主要手段。
- `--dangerously-skip-permissions` 必传,否则 worker 会停在权限弹窗前没人应答。若它被环境策略禁用,向用户报告,不要绕过。
- 多个 worker 并行时:每个 worker 一个 pane、一个唯一名字;避免连续同方向 split 把 pane 挤得不可用(先 right 再 down 交替,或用 --ratio)。
- **派发时必须落登记表** `/tmp/session-chat/manifest.json`,格式:`{"main_pane":"<你的-pane-ID>","tasks":[{"name","pane_id","task","dispatched_at","status":"outstanding"}]}`。它是传输守护层(herdr 插件)的路由依据,字段保持稳定;每收到一条回信并核实后,把对应项 status 更新为 done;worker 消失时标 failed。不要用它的 status 做实时判断——实时判断以 pending 签收单和 `agent get` 为准。
- worker 继承 pane 的 cwd 和环境;新 claude 会话自带全局 skills 和 `HERDR_*` 变量,能直接执行回信命令。

## 收信与多轮对话

- worker 的回信会作为一段以 `[session-chat]` 开头的输入出现在你的输入框。你空闲时它立即开启新 turn;你正在工作时它排队,本 turn 结束后到达。收到后:**读取详情文件 → 核实内容 → 更新 manifest 对应项 status → 用一两句话向用户汇报**,不要原样粘贴。多路并行时按"已完成 N/共 M"的方式汇报部分完成状态。
- 详情文件是事实来源。settled 状态或一行摘要都不代表工作真的落盘了——先 `herdr agent get sc-<任务名>` 确认 worker 还在,再读文件验证。
- 追问/追加指令:对同一个名字再发一次 `herdr agent prompt sc-<任务名> "..."`(同样不带 --wait)。worker 干完会再次回信。
- 用户问进度时(非阻塞):`herdr agent get sc-<任务名>` 看状态,`herdr agent read sc-<任务名> --source recent-unwrapped --lines 60` 看最新输出。

## 兜底收割

回信迟迟没来、或用户直接要结果时才用:

```bash
herdr agent wait sc-<任务名> --timeout 300000   # 会阻塞,仅在用户明确等待时用
herdr agent read sc-<任务名> --source recent-unwrapped --lines 120
```

回信被 `agent_blocked` 拒收的情况:结果会留在文件里,直接读文件即可。若你的 pane 未被识别为 agent 导致回信无处可去,告诉用户手动查看 `/tmp/session-chat/` 下的文件。

worker 消失(`herdr agent get` 返回 agent_not_found):如实向用户报告任务未完成,**不要擅自重派**;把 manifest 对应项 status 标为 failed,是否重派由用户决定。

## 纪律

- 派活永远不带 `--wait`。阻塞等待只允许出现在"用户此刻就在等这个结果"的场景。
- worker pane 是你开的,但默认**保留不关**——用户要能看见子会话画面。仅当用户明确要求时才 `herdr pane close`。
- 不替 worker 回答任何确认/提问弹窗:先 `agent get` + `agent read` 弄清它卡在哪,再问用户。
- 不要对不相关的既有 pane 派任务;只动你为本次委派新开的 pane。
- 任务文本里写清三件事,缺一不可:**结果文件**承载完整内容、**签收单**(pending)让守护插件在回信失败时接管、**回信**负责即时通知。
