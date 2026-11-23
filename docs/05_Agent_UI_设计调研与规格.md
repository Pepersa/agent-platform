# AI Agent UI 设计调研与规格

> 调研时间：2026-11-23 ｜ 目标：指导本地部署 Agent 平台（FastAPI + 原生 HTML/CSS/JS，零 npm / 零 CDN）对标 Cursor 体验的 UI 实现
>
> 全部结论基于实际联网检索的公开资料，每节末尾标注来源 URL。搜不到确切细节处明确标注「未找到确切资料」。

---

## 0. 核心结论（TL;DR）

调研了 10 个产品 + 4 套设计系统后，得到 5 条贯穿性结论：

1. **「折叠块 + 单行摘要」是行业共识的默认态**。Claude Code、Cursor、Claude.ai、Cline、shadcn AI Elements 全部默认把工具调用折叠成一行摘要（`Read 3 files` / `Called slack 3 times`），完整内容只在用户主动展开时出现。这直接否定了「工具结果直接平铺」的做法。
2. **垂直时间线是主线，横向分栏是补充**。只有 v0/Lovable 这类「产物型」产品才把预览面板并排；而 Claude Code / Cursor / Claude.ai / OpenHands 全都走单列垂直流。**多列并排在代码 Agent 场景是反模式**（信息密度过高导致主次不分）。
3. **「运行中」的视觉信号必须是双通道**：文字状态 + 动态指示器。Claude Code 用闪烁圆点（`ToolUseLoader`），Claude.ai 用三点波浪跳跃，shadcn 用 `shimmer` 类扫光，OpenHands 用 localized live activity chip。三者共同点是：**指示器位于消息流末端（而不是页面上方）**。
4. **成本 / 上下文占用的行业惯例是「百分比 + hover 展开明细」**，而不是常驻精确数字。shadcn `Context` 组件用 HoverCard + Progress；Copilot CLI 暴露 `current_context_used_percentage`；OAR 在 composer 顶部画一条随占用从绿变红的细线。
5. **审批卡必须在「事件发生的原地」**，且拒绝时必须能附带理由文本。Roo Code / Cline 的 approve/reject 都在聊天流内，拒绝带 feedback 输入框；Cursor 进一步做到「折叠的 subagent 卡片上直接显示 Allow」。

---

## A. 逐产品分析

### A.1 Cursor

**布局骨架**（据官方 changelog 与 release notes 描述）：

```
┌────────────────────────────────────────────────────────────┐
│ Titlebar:  Logo | File Edit View ...      [云图标] [头像]  │
├──────┬──────────────┬─────────────────────────────────────┤
│ Act  │  Sidebar     │  Editor Tabs                       │
│ Bar  │  Explorer /  │  ┌──────────────┬─────────────────┐  │
│      │  Search /    │  │              │                 │  │
│ 48px │  Source Ctrl │  │  Editor      │  Composer       │  │
│      │  (可折叠)    │  │  (主区)      │  / Agent Panel  │  │
│      │  268px       │  │              │  (右侧抽屉)     │  │
│      │              │  │              │  Cmd+I 唤起      │  │
├──────┴──────────────┴──┴──────────────┴─────────────────┴  │
│ Statusbar: 分支 | 行:列 | 语言 | 错误/警告 | Token/成本?      │
└────────────────────────────────────────────────────────────┘
```

- **Agent 执行过程**：Composer / Agent 侧栏（Cmd+I / Cmd+L 切换 Composer 与 Chat）内的**垂直对话流**。0.46 版本起 "Agent is ready"，统一了 Chat/Composer/Agent 三个概念为「一个智能界面」。0.43 版引入 "Composer UI in the sidebar with inline diffs" 与「agent 可自主挑选 context 并使用 terminal」。
- **工具调用卡片**：源码未公开确切 DOM。公开资料显示工具执行以**状态行**形式出现在聊天流中，子 agent 卡片可折叠；release notes 明确提到「collapsed subagent cards show Allow for pending file edits and MCP tool calls, not only terminal commands」——即**折叠态卡片上直接挂审批按钮**。
- **diff 展示**：**默认 unified（inline）单栏**，可通过 VS Code 设置 `diffEditor.renderSideBySide: true` 切到并排双栏。0.49 版后改为「对话末尾一个 `Review changes` 按钮」，点击进入内置 diff view；`Cmd/Ctrl+Click` 文件名可在主编辑器打开 inline diff。也可用 gutter 图标逐行 revert（`renderMarginRevertIcon`）。
- **审批**：命令执行会弹审批卡（视频实测需按 "command enter" 接受）。子 agent 等待时主聊天状态行显示 **"Waiting for approval" / "Waiting for response"**。Background Agent 在 agent chat 上显示 **unread badge**；macOS Dock / 菜单栏有计数。Windows 通知也带 accept/reject 按钮。
- **Checkpoint**：官方文档明确 —— Agent 在重大改动前自动创建 checkpoint，**点击聊天时间线上的任意 checkpoint 可预览并恢复文件到该状态**；也可从 `Restore Checkpoint` 按钮或消息悬停时的 `+` 按钮恢复。**Checkpoint 只回滚文件，不删除对话消息**。存储在本地，与 Git 分离。
- **成本/上下文**：Cursor 报告每轮的聚合 token 用量，需自行除以模型窗口得到百分比。**未找到 Cursor 官方文档中关于状态栏常驻成本显示的确切资料**；第三方（OAR）记录 Cursor 的上下文对话里只有纯文本 token 数，没有 bar / table / 百分比。
- **可借鉴 3 点**：
  1. **Checkpoint 作为聊天时间线上的一个节点**（而非独立面板），点击即预览 + 恢复 —— 直接可抄到本项目的 Live Timeline。
  2. **审批卡在折叠态子卡片上仍可操作**（Allow 按钮内嵌在折叠头）—— 避免「必须展开才能批」。
  3. **状态行区分三种等待语义**（等待审批 / 等待回复 / 等待子 agent），而不是笼统的 "Running"。

> 来源：https://cursor.com/docs/agent/overview ｜ https://cursor.com/changelog/0-43-x ｜ https://cursor.com/changelog/0-46-x ｜ https://cursor.com/changelog/0-50 ｜ https://cursor.com/changelog/page/16 ｜ https://cursor.com/docs/release-notes/ide ｜ https://forum.cursor.com/t/side-by-side-diffs/24844 ｜ https://forum.cursor.com/t/can-we-bring-the-inline-diff-view/146592 ｜ https://forum.cursor.com/t/cursor-doesnt-show-inline-diff-in-main-window-and-auto-approves-all-changes/154362

---

### A.2 Claude Code（终端 TUI）

**布局骨架**（终端，24/80 列自适应）：

```
┌─ transcript（滚动区，密度高）───────────────────────────┐
│ > user prompt                                            │
│ ⏺ Read(src/a.py)                          [ctrl+o]      │
│   ⎿  1→ def f(): … +42 lines (ctrl+o to expand)          │
│ ⏺ Bash(git status)                        ⏱ 1.2s        │
│   ⎿  On branch main … +8 lines (ctrl+o to expand)        │
│ ● Thinking…                                               │
│ ⏺ Write(src/b.py)                                         │
│ ⎿  Done (12ms)                                            │
│ ⏺ Response text…                                          │
├─ input ──────────────────────────────────────────────────┤
│ > █                                                        │
└──────────────────────────────────────────────────────────┘
```

- **Agent 执行过程**：**垂直流 + 分组折叠**。核心设计是 `CollapsedReadSearchContent` 组件，三条硬规则（来自反向工程文章）：
  1. **永远默认折叠**，摘要形如 `Read 3 files, searched for 2 patterns… Ctrl+O to expand`，只有 `Ctrl+O` 能展开，**不做自动展开/收起循环**；
  2. **700ms debounce**：`MIN_HINT_DISPLAY_MS = 700`，保证每个工具的提示至少显示 700ms，防止快速完成的工具在单帧内闪过；
  3. **计数只增不减**：`useRef` 记录见过的最大 count，避免重渲染时 "Reading 3 files" 退回成 "Reading 2 files"。
- **工具调用卡片**：**固定一行**。`renderToolUseMessage` 只渲染一行描述（命令文本或文件路径），不渲染原始输出；`ToolUseLoader` 用**闪烁圆点**作 spinner，从开始到完成**始终占一行**。结果超长时截断为 `… +N lines (ctrl+o to expand)`。
- **三阶段渲染**：模型发出 `tool_use` → **Phase 1** 意图渲染（注意 `input` 类型是 `Partial<Input>`，参数还在流式到达，必须能在参数不完整时渲染出 "Bash" 标签 + 部分命令文本）→ **Phase 2** 执行中进度（`onProgress` 回调）→ **Phase 3** 结果渲染（`renderToolResultMessage` 支持 `style: 'condensed'`，Grep/Glob 在非 verbose 下只显示 "Found 42 files across 3 directories"）。
- **分组渲染**：`renderGroupedToolUse` 把同类型并行工具合并。
- **MCP 调用**：默认折叠成单行 `Called slack 3 times`。
- **diff 呈现**：终端下是纯文本 diff（aider 同理）。`Ctrl+O` transcript viewer 展开；`/diff` 看上次消息以来的变更。
- **Fullscreen 模式**（research preview）：用终端 alternate screen buffer，像 vim/htop 一样只渲染当前可见消息，消除闪烁、内存恒定、支持鼠标；**点击被截断的工具结果即可展开**。展开键是 `tool_use_id`，保证 tool_use 与 tool_result **一起展开**。
- **verbose 三态**：`/config > verbose` 把 verbose 重定义为多态开关 —— 原本用于显示完整文件路径，现在用于切换「更详细」vs「更折叠」两个极端。
- **可借鉴 3 点**：
  1. **700ms 最短展示时长** —— 这是防闪烁的最省事解法，本项目 SSE 流式渲染必然遇到同样问题。
  2. **三阶段渲染（意图 → 执行中 → 结果）** —— 参数用 `Partial` 容忍流式不完整，绝不显示空白卡。
  3. **工具组「永远默认折叠 + 只有显式操作能展开」** —— 杜绝自动展开造成的视觉噪音。

> 来源：https://code.claude.com/docs/en/interactive-mode ｜ https://code.claude.com/docs/en/fullscreen ｜ https://quidproquo.cc/posts/ai/2026-09-06-coding-agent-tui-tool-result-no-flicker-en/ ｜ https://news.ycombinator.com/item?id=46981968 ｜ https://github.com/anthropics/claude-code/issues/99990 ｜ https://github.com/anthropics/claude-code/issues/32555 ｜ https://github.com/claude-code-best/claude-code/blob/632f3e19/src/components/Messages.tsx ｜ https://zhanghandong.github.io/harness-engineering-from-cc-to-ai-coding/en/part1/ch02.html

---

### A.3 Claude.ai / claude.ai/code

**布局骨架**：

```
┌───────────────────────────────────────────────────────────┐
│ Header: 模型选择 | 对话标题            [Artifact 开关]     │
├──────────────────────────────┬────────────────────────────┤
│  Chat（单列，居中 ~40em）    │  Artifact Panel            │
│                              │  ┌──────────────────────┐  │
│  user: ...                   │  │ [Preview | Code]     │  │
│                              │  │                      │  │
│  assistant:                  │  │   实时渲染 / 源码     │  │
│  ┌ Thinking ──── 3.2s ────┐ │  │                      │  │
│  │ (折叠摘要)             │ │  │                      │  │
│  └────────────────────────┘ │  └──────────────────────┘  │
│  ┌ Tool: web_search  2.1s │ │  [版本 1/3] [复制] [下载]  │
│  │  ▸ 参数 / 结果（折叠）  │ │                            │
│  └────────────────────────┘ │                            │
│  正文回答                   │                            │
│                              │                            │
├──────────────────────────────┴────────────────────────────┤
│ Composer: [模型] [上下文用量]  [输入框]  [发送]            │
└───────────────────────────────────────────────────────────┘
```

- **思考折叠块**：**四阶段微交互状态机**（来自对 Claude Desktop 生产构建的 AST 级分析）：
  ```
  [*] → Generating   : 模型开始输出思考流
  Generating → Generating : 3 点波浪跳跃 (1.9s infinite cubic-bezier(.4,0,.2,1))
  Generating → Generating : 展开互斥锁锁定 (isOpen=true，禁止折叠防抖动)
  Generating → Settled  : 思考流终结
  Settled → Collapsed    : 用户点击折叠 (Grid 1fr → 0fr, .2s)
  Collapsed → Expanded    : 用户再次点击 (Grid 0fr → 1fr)
  ```
  三点波浪的关键 CSS（`.timeline-quiet-dots`）：点尺寸 `3px`、圆点、gap `3.5px`、`top: .26em`，`animation: 1.9s cubic-bezier(.4,0,.2,1) infinite`；三个子元素 `animation-delay` 分别为 `0 / .15s / .3s`；keyframes 为 `0%,8% opacity:0 translateY(2px)` → `22% opacity:1 translateY(-2.5px)` → `36%,70% opacity:1 translateY(0)` → `90%,100% opacity:0`。
- **折叠动效**：**用 CSS Grid 而非 JS 计算 scrollHeight**。`.timeline-expand { grid-template-rows: 1fr; display: grid; }`，子元素 `align-self:start; min-width:0; min-height:0;`。进入动画 `animation: .2s cubic-bezier(.19,1,.22,1)`。状态行进入 `.timeline-status-enter-quiet` = `.24s cubic-bezier(.3,.7,.4,1)`（`translateY(6px)` → 基线）；退出 `.timeline-status-exit-quiet` = `.18s ease-out`（`translateY(-4px)` + 淡出）。
- **工具调用卡片**：**紧凑状态行** —— 工具名 + 结果状态 + 耗时；输入输出藏在展开/折叠下。卡片在消息列内 **borderless**，通过 Surface 背景变化和更高的图标来指示可交互（严禁在容器内再放带边框的二级按钮）。
- **Artifact 面板**：右侧独立窗口，含实时预览 + 代码 + 版本 + 复制 + 下载。
- **卡片复位规则**：结构化卡片（代码块、Artifact 预览、MCP 授权面板）一出现，Inset / Stop 重置为 `0px`，让卡片充满列容器。
- **可借鉴 3 点**：
  1. **思考块的「展开互斥锁」** —— 流式输出时禁止折叠，避免边流边抖；本项目 SSE 流同理。
  2. **CSS Grid `1fr → 0fr` 做折叠动画** —— 零 JS 测量、无布局抖动，这是零依赖方案的最佳折叠实现。
  3. **卡片 borderless，靠背景层级区分** —— 与本项目现有 `--bg-*` 分层令牌天然契合。

> 来源：https://scrimui.dev/inspiration/claude ｜ https://openclaudeui.com/en/docs/v1/03 ｜ https://openclaudeui.com/en/docs/v1/08 ｜ https://gradientdeck.com/ai-components ｜ https://www.northbase.design/systems/claude/ai-input-output

---

### A.4 Windsurf / Cascade

**布局骨架**：

```
┌────────────────────────────────────────────────────────┐
│ Titlebar                                                │
├──────┬───────────────┬─────────────────────────────────┤
│ Act  │  Cascade      │  Editor + Web Preview           │
│ Bar  │  Panel (右)   │  ┌─────────────────────────────┐  │
│      │               │  │  Preview | Code | Logs     │  │
│      │  1. 描述任务   │  │                             │  │
│      │  2. Plan 审批  │  │  实时预览（DOM/console 回传  │  │
│      │  3. 执行进度   │  │  给 Cascade）                │  │
│      │  4. Diff 审阅  │  │                             │  │
│      │  5. Checkpoint │  └─────────────────────────────┘  │
└──────┴───────────────┴─────────────────────────────────┘
```

- **Agent 执行过程**：**Flow 面板内的多阶段流程**。关键设计是 **「先 Plan 后 Execute」两段式**：Cascade 先读现有文件、理解模式、**提出计划**（要建哪些文件、改哪些文件、用什么方案），用户审阅并可要求调整（例如 "use a tabbed layout instead"），批准后才执行。执行中进度实时流式显示（创建组件、设置路由、接 API 层）。
- **工具调用卡片**：**未找到确切资料**（Windsurf 未公开 DOM 结构）。可确认的是 Cascade 会把浏览器的 DOM、错误、日志**回传给自身**（"Windsurf's browser preview feeds the DOM, errors, and logs straight back to Cascade"），因此工具调用的信息模型至少包含这三类。
- **diff 展示**：内置 diff 审阅，强调**必须读全量 diff 而非只看正在看的那部分**（"an agent fixing one thing can quietly restructure something adjacent"）。
- **审批**：计划审批（Plan → 批准 → Execute）；每个 build-preview-fix 循环中可在预览里指出具体症状。
- **Checkpoint**：实践指南建议「在下一次大动作前保存一个 working checkpoint」（commit 或标记），以便回滚 —— 是**流程建议**而非内置 UI。
- **可借鉴 3 点**：
  1. **Plan 审批作为时间线上的一个独立节点**（不是隐式的），可以在执行前中止/修改方向。
  2. **预览把 DOM/console/log 回灌给 agent** —— 本项目的工具结果卡应能承载这三类富内容，而非只有 stdout。
  3. **同一 Flow 内保持上下文**（"Cascade remembers everything"）—— 对应本项目「同一会话内所有时间线节点共享 thread 上下文」。

> 来源：https://www.yodev.dev/t/setting-up-windsurf-a-practical-guide-to-cascade-workflows/1323 ｜ https://builderscamp.com/guides/tools/build-a-prototype-with-windsurf ｜ https://www.browsermoon.com/windsurf-templates ｜ https://flowstep.ai/mcp/windsurf

---

### A.5 Continue.dev

**布局骨架**（VS Code 侧边栏）：

```
┌────────────────────────────────────────────────────┐
│ VS Code Editor (主区)                               │
├────────────────────────────────────────────────────┤
│ Secondary Side Bar                                  │
│ ┌────────────────────────────────────────────────┐ │
│ │  [模型 ▾]  [助手 ▾]            [Context 指示]   │ │
│ ├────────────────────────────────────────────────┤ │
│ │  对话流（与 Agent 模式共用同一界面）            │ │
│ │  · 工具调用 → Continue / Cancel 审批按钮        │ │
│ ├────────────────────────────────────────────────┤ │
│ │  [🔧 Agent ▾]  @file 上下文   [输入框]  [发送]  │ │
│ └────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────┘
```

- **Agent 执行过程**：**与 Chat 模式完全共用同一界面与同一输入框**，只通过输入框下方的 **mode selector** 区分。Mode 按钮样式：`rounded-full`、`px-1.5 py-0.5`、`bg-lightgray/20`、`border-none`、`transition-colors duration-200 hover:brightness-110`。
- **模式循环**：Chat → Plan → Agent → Background → Chat（本地 agent 时跳过 background）。快捷键 **`Cmd/Ctrl + .`** 循环切换。图标通过 `ModeIcon mode={mode}` 渲染。
- **工具调用卡片**：**未找到确切 DOM**，但确认了审批按钮文案为 **`Continue` / `Cancel`**（不是 Approve/Reject），默认「Ask First」策略。工具策略三档：`Ask First`（默认）/ `Automatic` / `Do not send`。
- **可借鉴 3 点**：
  1. **模式选择器放在输入框内/紧邻输入框**，`rounded-full` + `duration-200` hover —— 直接可抄。
  2. **模式项内嵌「能力说明」提示图标**（Chat = "All tools disabled" / Plan = "Read-only/MCP tools available" / Agent = "All tools available"），并对不推荐的模型显示警告三角。
  3. **审批按钮文案差异化**（Continue/Cancel）以降低语义歧义，本项目可对应设计「允许 / 拒绝」+「始终允许」。

> 来源：https://docs.continue.dev/ide-extensions/agent/quick-start ｜ https://github.com/continuedev/continue/blob/cb273098/gui/src/components/ModeSelect/ModeSelect.tsx ｜ https://continue-docs.mintlify.app/ide-extensions/agent/how-to-customize ｜ https://continue.ghost.io/a-fresh-coat-of-code-continues-new-vs-code-interface/ ｜ https://github.com/continuedev/continue/pull/4779

---

### A.6 OpenHands (OpenDevin)

**布局骨架**：

```
┌────────────────────────────────────────────────────────┐
│ Titlebar                                                │
├──────┬───────────────┬────────────────────────────────┤
│ Act  │  Sidebar      │  Chat / Runtime                 │
│ Bar  │  (会话/文件)  │  ┌───────────────────────────┐  │
│      │               │  │ user: ...                 │  │
│      │               │  │ agent bubble (含 thought) │  │
│      │               │  │ agent bubble (含 thought) │  │
│      │               │  │ ┌───────────────────────┐ │  │
│      │               │  │ │ Action card           │ │  │
│      │               │  │ │ (observation 原地替换) │ │  │
│      │               │  │ └───────────────────────┘ │  │
│      │               │  ├───────────────────────────┤  │
│      │               │  │ [● Running: Reading file] │  │ ← live activity chip
│      │               │  ├───────────────────────────┤  │
│      │               │  │ Composer                 │  │
│      │               │  └───────────────────────────┘  │
└──────┴───────────────┴────────────────────────────────┘
```

- **Agent 执行过程**：**严格的事件溯源 + UI 投影分离**。后端产生 `ActionEvent` / `ObservationEvent` / `MessageEvent` / `StreamingDeltaEvent`，存入 `useEventStore`；`handleEventForUI` 把它们变换为 `uiEvents` 用于渲染。核心规则：
  - **Action 被同 action_id 的 Observation 原地替换**（UI 只显示 observation）；
  - **Think / Finish observation 被省略**，避免重复内容；
  - **Streaming deltas 合并进同一个不断长大的气泡**，最终消息到达时**就地协调（finalizeStreamingDeltasInPlace）**而不是追加；
  - **流式 delta 会被后续 Action 取代**（`supersedeStreamingDeltasForAction`）—— 因为 Action 卡片会自己渲染 thought，保留 delta 会导致推理显示两遍。若 Action 不自带 reasoning，则保留为 "reasoning-only bubble"；
  - **ACP tool-call 更新按 `tool_call_id` 去重**（in_progress → completed / failed 原地更新卡片）；
  - **过时 delta 丢弃**（REST 历史与 WebSocket 重放竞态导致）。
- **Live activity chip**：PR #16110 明确「现有的三点跑动指示器只说明 agent 忙，但不说明它在干什么」，改为**从未解决的原始事件派生的本地化 chip**，复用 action / ACP title 语义，不支持时回退到 `Thinking`。**该 chip 是响应式、非交互、无障碍的，且只出现在 composer 上方的 RUNNING 位置**。
- **可借鉴 3 点**：
  1. **「Action → Observation 原地替换」** —— 同一个节点从「执行中」变成「已完成」，而不是追加两条消息。
  2. **Live activity chip 文案来自事件本身**（"Reading file" / "Running tests"），而不是泛化的 "Working..."。
  3. **delta 取代机制** —— 本项目 SSE 流式文本必须做同样的「后到的持久事件取代流式预览」，否则会出现两份推理。

> 来源：https://github.com/OpenHands/OpenHands/blob/11d4ecf2/frontend/src/utils/handle-event-for-ui.ts ｜ https://github.com/OpenHands/OpenHands/blob/11d4ecf2/frontend/src/stores/use-event-store.ts ｜ https://github.com/OpenHands/OpenHands/pull/16110 ｜ https://github.com/OpenDevin/OpenDevin/pull/2818 ｜ https://deepwiki.com/OpenHands/OpenHands/8.2-event-rendering-and-message-display

---

### A.7 aider

**布局骨架**（终端）：

```
───────────────────── aider v0.77 ─────────────────────
Main model: anthropic/claude-3-7-sonnet with diff edit format, 8k think tokens
────────────────────────────────────────────────────────
v1-v2-changes.diff
> Do you see any potential bugs in this file?
────────────────────────────────────────────────────────
Apply edit to src/main.py? (Y)es/(N)o [Yes]: Yes
────────────────────────────────────────────────────────
src/main.py
@@ -12,7 +12,7 @@
-    old_line()
+    new_line()
────────────────────────────────────────────────────────
```

- **Agent 执行过程**：纯终端 REPL，**没有折叠块**。Repo map 作为**静态文本块**注入每次请求（`/map` 打印、`/map-refresh` 刷新）。
- **diff 呈现**：**模型生成的编辑以标准 unified diff 形式输出到终端**，且**无法关闭**（官方 issue #1160 明确 "There's no way to suppress showing it"）。`--show-diffs` 默认 False，仅控制 commit 时是否再显示 diff。git diff 可用 `-C10` 控制上下文行数（`/run git diff -C10 v1..v2`）。
- **Edit format 切换**：`wholefile`（整文件）/ `diff`（editblock，GPT-4o 默认）/ `udiff`（universal diff）。选择影响 diff 呈现方式。
- **上下文管理**：repo map 用**图排序算法**（文件为节点、依赖为边）挑选最相关部分，受 `--map-tokens`（默认 1k）预算约束。
- **可借鉴 3 点**：
  1. **`/diff` 与 `/map` 一等命令** —— 把「查看自上次消息以来的改动」「查看上下文构成」做成显式入口。
  2. **repo map 作为可展开的静态块** —— 本项目的 Context Map 组件应能显示「哪些文件占用了多少 token」。
  3. **diff 上下文行数可调** —— 大改动不要一次贴全。

> 来源：https://aider.chat/docs/repomap.html ｜ https://aider.chat/docs/usage/commands.html ｜ https://aider.chat/docs/config/options.html ｜ https://aider.chat/docs/faq.html ｜ https://github.com/Aider-AI/aider/issues/1160

---

### A.8 Cline / Roo Code

**布局骨架**：

```
┌──────────────────────────────────────────────────────┐
│ Roo Code Panel (VS Code Secondary Side Bar)           │
│ ┌──────────────────────────────────────────────────┐ │
│ │ [💬 Chat | 🧠 Architect | 🛠 Code]  ← mode 下拉   │ │
│ ├──────────────────────────────────────────────────┤ │
│ │ user: ...                                        │ │
│ │ agent: ...                                       │ │
│ │ ┌── 📌 Task Checkpoint ────────────────────────┐  │ │
│ │ │   View Differences | Restore Checkpoint      │  │ │
│ │ └──────────────────────────────────────────────┘  │ │
│ │ ┌── 💻 Execute Command ─────────────────────────┐  │ │
│ │ │   npm install                                   │  │ │
│ │ │   [✓ Approve]  [✗ Reject]  [反馈输入框]        │  │ │
│ │ └──────────────────────────────────────────────┘  │ │
│ │ agent: ...                                       │ │
│ ├──────────────────────────────────────────────────┤ │
│ │ [mode ▾]  [input........................] [Send] │ │
│ └──────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────┘
```

- **Agent 执行过程**：聊天流。**Plan & Act 双模式**（Cline）：Plan 模式**只读**（可读文件、搜索、讨论策略，不能改文件不能执行命令），Act 模式保留 Plan 的完整上下文后执行。**模式切换时对话历史完整保留**。
- **工具调用卡片**：提案型工具显示 **approve / reject 控件**；拒绝时**通常伴随反馈输入框**（反馈字段与 reject 按钮并排），用于解释原因 / 建议替代方案 / 澄清误解，AI 据此修订方案。
- **diff 展示**：**checkpoint 的 "View Differences" 打开对比视图**，显示自 checkpoint 以来的所有改动：新增行（绿）、删除行（红）、修改文件、新增/删除文件。
- **审批**：在**输入框上方的 action button area**，按对话状态上下文敏感显示。Auto-Approve 通过 permission tile 网格下拉配置：8 项权限（读文件/编辑文件/执行命令/浏览器/MCP/切换模式/子任务/回答追问），每项标注风险等级（编辑文件 = High）。全局 Enabled 开关可一键暂停/恢复所有自动审批，**各权限的选择在暂停期间保留**。权限 tile 在 Enabled 关闭时禁用。
- **Checkpoint**：作为**聊天历史中的特殊 marker**。`Task Checkpoint`（任务开始时创建，标记初始项目状态）与 `Regular Checkpoint`（文件修改前创建，用于撤销）。每个 marker 提供 **View Differences** 与 **Restore Checkpoint** 两个动作；Restore 提供 **Restore Files Only**（不动对话）或 **Restore Files & Task**（同时删除后续消息，且**弹出确认对话框，动作不可撤销**）。
- **可借鉴 3 点**：
  1. **Checkpoint 是聊天内的 marker 节点**，带两个内联动作按钮 —— 本项目 Live Timeline 的 checkpoint 节点应完全照搬这个模式。
  2. **拒绝时提供反馈输入框**（与拒绝按钮并排）—— 本项目审批卡必须支持「拒绝并说明原因」。
  3. **Auto-Approve 权限 tile 网格 + 全局暂停开关**，且暂停时保留各 tile 选择 —— 对应本项目 Autonomy Dial 的档位设计。

> 来源：https://deepwiki.com/RooCodeInc/Roo-Code-Docs/4.1.1-chat-interface-and-interaction-model ｜ https://github.com/RooCodeInc/Roo-Code/blob/main/apps/docs/docs/features/auto-approving-actions.mdx ｜ https://github.com/RooVetGit/Roo-Code/pull/866 ｜ https://docs.cline.bot/core-workflows/plan-and-act ｜ https://deepwiki.com/roovetgit/roo-code/7-tool-execution

---

### A.9 AI 观测类：LangSmith / Langfuse / Supabase / PostHog / Jaeger 系

**布局骨架**（业界标准形态）：

```
┌─────────────────────────────────────────────────────────────┐
│ Sticky Run Header: 状态 | 触发指令 | 模型 | 起始时间 |       │
│   总耗时 | 活跃计算时长 | 人工等待时长 | 记录数 | 成本 | token│
├─────────────────────────────────────────────────────────────┤
│ Aggregate Minimap（低细节全局概览，含错误与未完成项）         │
├──────────────────────────┬──────────────────────────────────┤
│  Span Tree（可展开）      │  Waterfall（共享时间轴）           │
│  ▾ agent.invoke   2.4s   │  ruler: 0s ─┬─ 5s ─┬─ 10s ─     │
│    ▾ llm.chat  1.2s      │       ▓▓▓▓▓                      │
│      ▸ tool.exec 340ms   │       ░░░░░░░░                    │
│    ▸ db.query   88ms     │            ▓▓                     │
├──────────────────────────┴──────────────────────────────────┤
│ Selected-Span Inspector: 精确时间戳 | 耗时 | 状态 |          │
│   provider/model/tool | token & cost | attributes | events  │
│   links | source evidence | raw payload                    │
└─────────────────────────────────────────────────────────────┘
```

- **Agent 执行过程**：**分层 split view**。Supabase 的 `Trace.Waterfall` 容器测量自身宽度把时间映射到像素，两列：`treeWidth` 的树 + 时间轴。`Trace.Ruler` 画时间轴并自适应可见区间的刻度，`Trace.Grid` 在每个刻度下画垂直网格线，`Trace.Cursor` 从 ruler 顶部到底部画一条 `nowMs` 的实时游标线（**live trace 就有这个游标**）。`Trace.Rows` 是虚拟化滚动列表。`Trace.Bar` 按状态着色，运行中的 span 延伸到 `nowMs`。`Trace.CollapsedChildren` 在折叠的 span 每个直接子节点中点画一个刻度。`Trace.Timing` 在 bar 之后显示时长，空间不足时显示在 bar 末端内侧。
- **规模契约**（来自一份明确的 agent trace 视觉契约文档）：
  | 记录数 | 默认呈现 |
  |---|---|
  | 0 | 诚实的空态，**绝不画合成活动** |
  | 1–12 | 紧凑时间线摘要 + 时长 + 终态证明 |
  | 13–100 | 层级 waterfall 是默认视图 |
  | 101+ | 虚拟化 waterfall + minimap + filters + 游标分页 + 默认折叠子树 |
- **时间语义诚实性**：**使用服务端记录的时间戳，绝不发明等距 bar 或客户端计时器进度**。open span 只延伸到最近观察到的服务端 checkpoint，并用**斜纹（hatching）**这种「未完成」处理方式，**不得暗示完成**。时长未知时渲染序列刻度或事件标记并标注「duration unknown」。必须区分 **wall time / active compute time / human-wait time** —— 长时间等待审批不得看起来像模型推理慢。
- **事件标记**：审批、checkpoint、重试、异常、引用、校验回执是**点或菱形**，不是伪造的时长条。
- **状态语义**（Pho Design System）：`status` 默认在有 `endedAt` 时为 `completed`、无则为 `running`；`failed` 给 bar 和点加错误色；`skipped` 画**虚线轮廓**。type（http/db/llm/tool）与 service 显示在名称后作为 hint。
- **可借鉴 3 点**：
  1. **「审批 / checkpoint 是点，不是条」** —— 零时长事件不能画成时间条，否则会误导耗时归因。本项目 Approval 节点应画成菱形标记。
  2. **区分 wall time / compute time / human-wait time** —— 审批等待时长必须单独计，否则成本归因全错。
  3. **open span 用斜纹 + 延伸到 now** —— 运行中的节点不能显示为已完成。

> 来源：https://supabase.com/design-system/docs/ui-patterns/tracing ｜ https://pho-ui.com/components/trace ｜ https://posthog.com/docs/ai-observability/traces ｜ https://langfuse.com/changelog/2026-11-20-redesigned-trace-view ｜ https://github.com/HomenShum/agentic-ui-qa/blob/main/TRACE-WATERFALL.md

---

### A.10 v0.dev / Lovable / Bolt.new

**布局骨架**（v0）：

```
┌───────────────────────────────────────────────────────────┐
│ Header: 项目名                                    [Deploy] │
├──────────────────────────┬────────────────────────────────┤
│ Chat (左, ~40%)          │  Preview (右, ~60%)             │
│                          │  ┌──────────────────────────┐  │
│ user: ...                │  │ [Preview|Code|Logs]      │  │
│                          │  │ [Desktop|Tablet|Phone]   │  │
│ ● 生成中 — agent steps    │  │                          │  │
│   streaming beside empty │  │   渲染的应用              │  │
│   preview                │  │                          │  │
│                          │  │                          │  │
│ agent: (自截图贴回聊天)   │  │  [Sandbox 状态 popover]  │  │
│  [desktop img][mobile img]│ └──────────────────────────┘  │
├──────────────────────────┴────────────────────────────────┤
│ Composer: [文件] [模型] [工具] [语音]           [Stop/发送]│
└───────────────────────────────────────────────────────────┘
```

- **Agent 执行过程**：**对话 + 实时预览的 split workspace**。生成中，agent steps 在左侧流式输出，右侧预览区保持为空；agent 会**把自己截的桌面 / 移动端截图贴回聊天**来自检。
- **产物面板规则**（Scrim UI 的 Artifact Workspace 规范，很值得抄）：
  1. **从产生它的那条回答打开产物** —— 面板是对话的产物，不是独立应用；
  2. **内容流式输出时保持面板 chrome 在最终位置**，不要中途重排；
  3. **产物版本追加式累积**，只有当读者已在最新版本时才跟随；
  4. **失败产物是面板的一种状态** —— 旁边的对话继续，最后一个好版本保留；
  5. **窄屏时产物变成 overlay**，从浮动入口访问，而不是挤成第二列。
- **版本管理**：`1/3` 分页器，previous/next、分支标记、continue-from-here，以及诚实的 generating / failed / stopped 状态。
- **Composer**：附件、模型选择、工具开关、语音，**发送按钮在生成中变成 Stop**。
- **可借鉴 3 点**：
  1. **发送按钮变 Stop**，且 Stop **保留已到达的内容** —— 直接对应本项目 SSE 中断。
  2. **产物失败不阻塞对话**，保留最后好版本 —— 本项目工具调用失败时不应清空时间线。
  3. **「版本追加式累积」+ 分页器** —— 若本项目引入 checkpoint 版本，回滚 UI 应采用这个模型。

> 来源：https://www.saasui.design/application/v0 ｜ https://scrimui.dev/patterns/artifact-workspace ｜ https://chatcn.dev/docs/components/app-builder ｜ https://chatcn.dev/docs/components/artifact-panel ｜ https://lovable.dev/guides/how-to-build-a-chatbot-ui

---

### A.11 设计系统与规范补充

**shadcn AI Elements**（2026 年新出的一组 agentic 模式，全部是 copy-paste 的组合模式）：Chat（Message Scroller + Message + Bubble + Prompt Input）、**Trace**（agent 的每个 step 都建立在 Trace 上，形状统一）、**Reasoning**（流式思考）、**Tool**（调用行 + Code Block 输出）、**Confirmation**（批准/拒绝提示）、**Context**（Hover Card + Progress bar）、**Plan**（步骤 + 进度）、**Queue**（todo 列表，分「待办/已完成」）、**Checkpoint**（对话中的可回溯点）、**Sources**、**Inline Citation**、**Model Selector**（选模型 + 选思考强度两个菜单）、**Artifact**（带预览/代码/版本/复制下载）。

**AI SDK `useChat` 的 status 状态机**：`submitted`（已发送，首个 part 未到）→ `streaming`（part 到达中）→ `ready`（本轮完成）→ `error`（请求失败）。
- `submitted` 期间，线程末尾是一个 **Marker**，文字为 "Thinking"，用 `shimmer` 类扫光，`role="status"` 播报；
- busy 时**发送按钮变 Stop**，`stop()` 结束回复并**保留已到达的内容**；
- 失败时，field 上方的 Marker **变成一个按钮**，点击 `regenerate()` 从用户消息重跑；
- 线程内容在回复到达期间 `aria-busy`，让读屏器播报整轮完成而非逐词。

**Carbon for AI**：
- **AI label** 是标识 AI 存在的主入口，同时是 **explainability popover 的触发器** —— 第一层可解释性就在 AI 标签旁边；
- **不要把 Carbon for AI 样式当装饰用**，它们只用于标识 AI 生成内容；
- Chat 组件库用 `--cds-aichat-history-width`（默认 320px）控制历史面板宽度；
- 聊天模式要求：**声明 bot 身份与目的**、**反思理解**（在重大动作前）、**提供反馈机会**。

**Vercel AI SDK / chatbot 最佳实践（shadcn 官方文档）**：
- `MessageScrollerProvider autoScroll` + `scrollAnchor` 放在**用户消息**上 —— 新问题落在顶部附近，回复在其下方流入；
- `MessageScroller` 提供 `autoScroll` 跟随，但用户上滚即放手；
- `Message` 区分 `align="end"`（用户）/ agent；`Bubble variant="muted"` 用于用户气泡；
- `status` 为 `submitted` 或 `streaming` 时**禁用输入框**。

**可访问性（WCAG 2.2）**：
- **2.4.7 Focus Visible (AA)**：键盘焦点指示器必须可见；
- **2.4.11 Focus Not Obscured (Minimum, AA)** / **2.4.12 (Enhanced)**：获得焦点的组件不能被作者内容完全遮挡；
- **2.4.13 Focus Appearance (AAA)**：焦点指示区域至少与「未聚焦组件 2 CSS px 厚周边」一样大，且聚焦/未聚焦像素间**对比度至少 3:1**；
- **2.3.3 Animation from Interactions (AAA)**：交互触发的动效可被禁用；充分技术包括 **C39：使用 CSS `prefers-reduced-motion` 媒体查询阻止动效**；
- Accordion 的 `aria-expanded` / `aria-controls` **必须立即设置，不能等动画结束**；折叠内容保持 `inert`（留在页面里以便高度可动效，但内部不可聚焦/朗读）。

**折叠动效通行取值**：
| 来源 | 时长 | 缓动 |
|---|---|---|
| Chrome 官方 accordion 指南 | 300ms | `ease-in-out` |
| Radix Accordion | 300ms | `ease-out` |
| shadcn / Tremor / PsyUI | 150ms | `cubic-bezier(0.87, 0, 0.13, 1)` |
| Orvain UI | 220ms | （高度变化），hover 150ms |
| Carbon（productive standard） | 150ms（小展开） | `cubic-bezier(0.2, 0, 0.38, 0.9)` |
| Carbon（productive exit） | — | `cubic-bezier(0.2, 0, 1, 0.9)` |
| Claude.ai timeline 折叠 | 200ms | `cubic-bezier(.19, 1, .22, 1)` |
| Claude.ai 状态行进入 / 退出 | 240ms / 180ms | `cubic-bezier(.3,.7,.4,1)` / `ease-out` |
| Claude.ai 三点波浪 | 1900ms loop | `cubic-bezier(.4, 0, .2, 1)` |

**结论取值**：本项目采用 **180ms `cubic-bezier(0.2, 0, 0.38, 0.9)`**（展开，Carbon productive standard）＋ **120ms `cubic-bezier(0.4, 0, 1, 0.9)`**（收起，退出加速）＋ **900ms 线性**（spinner/扫光循环），与现有 `--transition: 120ms ease-out` 保持一致节奏。

> 来源：https://ui.shadcn.com/docs/ai-elements ｜ https://ui.shadcn.com/docs/ai-elements/base/chat ｜ https://ui.shadcn.com/docs/ai-elements/base/context ｜ https://ai-sdk.dev/docs/ai-sdk-ui/chatbot ｜ https://www.shadcn.io/ui/ai-sdk ｜ https://www.carbondesignsystem.com/building-blocks/foundations/carbon-for-ai ｜ https://chat.carbondesignsystem.com/tag/latest/docs/documents/UI_customization.Layout.html ｜ https://v10.carbondesignsystem.com/community/patterns/chatbot/usage/ ｜ https://www.w3.org/TR/WCAG22/ ｜ https://www.w3.org/WAI/WCAG22/Understanding/animation-from-interactions ｜ https://developer.chrome.com/docs/modern-web-guidance/guides/ui-components/accordion ｜ https://www.radix-ui.com/primitives/docs/components/accordion ｜ https://www.carbondesignsystem.com/building-blocks/foundations/motion/overview ｜ https://designsystem.digital.gov/components/accordion/accessibility-tests/

---

## B. 横向对比表

评分 1–5（5 = 最好）。「不适用」表示该产品形态上不存在此维度。

| 产品 | 布局密度 | 过程可见性 | 工具调用透明度 | diff 体验 | 审批体验 | 状态反馈 | 成本可见性 | 代码可编辑性 |
|---|---|---|---|---|---|---|---|---|
| **Cursor** | 5 | 4 | 4 | 4 | 5 | 4 | 2 | 5 |
| **Claude Code (TUI)** | 5 | 3 | 3 | 2 | 3 | 4 | 2 | 2 |
| **Claude.ai** | 3 | 4 | 4 | 不适用 | 4 | 5 | 2 | 2 |
| **Windsurf / Cascade** | 3 | 4 | 2 | 3 | 4 | 4 | 1 | 3 |
| **Continue.dev** | 4 | 3 | 2 | 2 | 3 | 3 | 2 | 3 |
| **OpenHands** | 3 | 5 | 4 | 3 | 3 | 5 | 3 | 3 |
| **aider** | 5 | 2 | 2 | 3 | 3 | 2 | 1 | 2 |
| **Cline / Roo Code** | 4 | 4 | 4 | 4 | 5 | 3 | 1 | 4 |
| **Langfuse / LangSmith 等** | 3 | 5 | 5 | 不适用 | 2 | 3 | 5 | 不适用 |
| **v0 / Lovable / Bolt** | 2 | 4 | 3 | 3 | 2 | 5 | 1 | 3 |

**读表要点**：
- **成本可见性只有观测类产品拿到 5 分**，其余全是 1–3。这是本项目最大的短板 —— 现有 12 个组件里有 Cost Waterfall / Context Map，但**没有常驻的百分比指示器**。
- **代码可编辑性只有 Cursor 拿 5 分**（原生编辑器 + inline diff + gutter revert）。
- **审批体验最好的是 Cursor 和 Cline/Roo**（5 分），因为它们都做到了「卡片原地 + 可折叠态可操作 + 拒绝带反馈」。
- **Claude Code 的 diff 体验只有 2 分**，但它的**信息密度 5 分** —— 这是终端约束下的取舍。

---

## C. 落地方案（三个可实施方案）

约束：**零 npm、零 CDN、零前端框架**，只能用手写 HTML/CSS/JS + FastAPI SSE。

---

### 方案 A：统一折叠卡片流（低风险渐进式改造）

**一句话**：保持现有 12 个观测组件不变，在**对话流**里把工具调用/审批/推理统一渲染成一种「单行摘要 + 可展开」的折叠卡片，把 12 个观测视图降级为「诊断视图」，主要面向调试而非主流程。

**布局**：

```
┌───────────────────────────────────────────────────────────┐
│ Titlebar                                                   │
├──────┬──────────────┬────────────────────────────────────┤
│ Act  │  Sidebar     │  Chat 主区                          │
│ Bar  │              │  ┌──────────────────────────────┐  │
│      │              │  │ user: 帮我改一下配置          │  │
│      │              │  │                              │  │
│      │              │  │ ▸ Thinking 2.3s              │  │ ← 折叠
│      │              │  │ ▾ Bash(git status)  1.2s  ✓  │  │ ← 展开
│      │              │  │    ⎿ On branch main… +8 lines│  │
│      │              │  │ ▸ Read(3 files)  340ms      │  │
│      │              │  │ ▾ ⚠ Approval needed          │  │
│      │              │  │    npm install                │  │
│      │              │  │    [允许一次][始终允许][拒绝] │  │
│      │              │  │ assistant: 已完成…            │  │
│      │              │  ├──────────────────────────────┤  │
│      │              │  │ Composer  [ctx 34%]  [发送]  │  │
├──────┴──────────────┴────────────────────────────────────┤
│ Statusbar: ▓▓▓▓░░░░ 上下文 34% | $0.12 | 12.4s | ⏸ 停止   │
└───────────────────────────────────────────────────────────┘
```

**新增 CSS 类与关键属性**：

```css
/* 折叠卡容器 —— Grid 1fr↔0fr 实现零 JS 测量的高度动画 */
.uc {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows 180ms cubic-bezier(0.2, 0, 0.38, 0.9);
  border-left: 2px solid var(--line);
}
.uc[data-open="true"] { grid-template-rows: 1fr; }
.uc > .uc-body {
  min-height: 0; min-width: 0; align-self: start;
  overflow: hidden;
  padding-left: var(--sp-3);
}

/* 单行摘要头 */
.uc-head {
  display: flex; align-items: center; gap: var(--sp-2);
  height: 22px; padding: 0 var(--sp-2);
  cursor: pointer; user-select: none;
  border-radius: var(--r-sm);
}
.uc-head:hover { background: var(--bg-4); }
.uc-head:focus-visible { outline: 1px solid var(--accent); outline-offset: -1px; }
.uc-caret { transition: transform 180ms cubic-bezier(0.2,0,0.38,0.9); color: var(--fg-faint); }
.uc[data-open="true"] .uc-caret { transform: rotate(90deg); }
.uc-title { flex: 1 1 auto; font-size: var(--fs-md); color: var(--fg-dim); }
.uc-dur   { font-size: var(--fs-xs); color: var(--fg-faint); font-variant-numeric: tabular-nums; }
.uc-badge {
  font-size: var(--fs-xs); padding: 0 5px; height: 15px; line-height: 15px;
  border-radius: var(--r-pill); border: 1px solid transparent;
}
.uc-badge.s-ok   { color: var(--ok);   background: var(--ok-soft);   border-color: rgba(63,185,80,.4); }
.uc-badge.s-err  { color: var(--err);  background: var(--err-soft);  border-color: rgba(248,81,73,.4); }
.uc-badge.s-run  { color: var(--run);  background: var(--run-soft);  border-color: var(--accent-line); }
.uc-badge.s-wait { color: var(--hitl); background: rgba(227,179,65,.14); border-color: rgba(227,179,65,.5); }

/* 结果截断：超出后渐隐 + 展开按钮 */
.uc-out {
  font-family: var(--mono); font-size: var(--fs-sm); color: var(--fg-dim);
  white-space: pre-wrap; word-break: break-word;
  max-height: 160px; overflow: hidden;
  position: relative;
}
.uc-out.is-clipped { mask-image: linear-gradient(180deg, #000 70%, transparent 100%); }
.uc-more {
  font-size: var(--fs-xs); color: var(--accent); cursor: pointer;
  background: none; border: 0; padding: 2px 0;
}

/* 思考三点 —— Claude.ai 同款参数 */
.uc-dots { display: inline-flex; gap: 3.5px; position: relative; top: .26em; }
.uc-dots > span {
  width: 3px; height: 3px; border-radius: 50%;
  background: var(--accent); opacity: 0;
  animation: 1.9s cubic-bezier(.4,0,.2,1) infinite uc-dot;
}
.uc-dots > span:nth-child(2) { animation-delay: .15s; }
.uc-dots > span:nth-child(3) { animation-delay: .3s; }
@keyframes uc-dot {
  0%,8%   { opacity: 0; transform: translateY(2px); }
  22%     { opacity: 1; transform: translateY(-2.5px); }
  36%,70% { opacity: 1; transform: translateY(0); }
  90%,100%{ opacity: 0; transform: translateY(0); }
}

/* 状态栏上下文占用条 */
.ctx-bar {
  width: 64px; height: 4px; border-radius: 2px;
  background: var(--bg-5); overflow: hidden;
}
.ctx-bar > i { display: block; height: 100%; background: var(--ok); transition: width 400ms cubic-bezier(.2,0,.38,.9), background 200ms; }
.ctx-bar[data-lvl="near"] > i { background: var(--warn); }
.ctx-bar[data-lvl="full"] > i { background: var(--err); }
```

**动效取值**：
| 场景 | duration | easing |
|---|---|---|
| 折叠展开 | 180ms | `cubic-bezier(0.2, 0, 0.38, 0.9)` |
| 折叠收起 | 120ms | `cubic-bezier(0.4, 0, 1, 0.9)` |
| caret 旋转 | 180ms | `cubic-bezier(0.2, 0, 0.38, 0.9)` |
| 三点波浪 | 1900ms loop | `cubic-bezier(.4, 0, .2, 1)` |
| 上下文条 | 400ms | `cubic-bezier(.2, 0, .38, .9)` |
| 状态切换（状态行进/出） | 240ms in / 180ms out | `cubic-bezier(.3,.7,.4,1)` / `ease-out` |

**需新增/改动文件**：
| 文件 | 改动 | 类型 |
|---|---|---|
| `static/tokens.css` | 新增，全部令牌 + `.uc*` 卡片样式 | 新增 |
| `static/unified-card.js` | 新增，`UnifiedCard` 渲染器（从 SSE 事件生成折叠卡） | 新增 |
| `static/app.css` | `:root` 中 `@import './tokens.css'` 或手工合并 | 改动 |
| `static/app.js` | 对话流插入 UnifiedCard；加「全部折叠 / 全部展开」按钮 | 改动 |
| `static/index.html` | 状态栏加 ctx-bar + cost 元素 | 改动 |
| `api/routes/chat.py` | SSE 事件统一为 `{kind, title, status, dur_ms, payload}` 结构 | 改动 |

**工作量估算**：**12–16 小时**（含 SSE 事件规范化 4h、卡片渲染器 4h、状态栏 2h、样式 2h、联调 4h）

**风险点**：
1. SSE 事件结构若后端未统一，前端要做大量兼容性判断 —— 建议**先改后端**再做前端；
2. 现有 12 个观测组件与新卡片流会**信息重复**（同一批数据渲染两遍），需要明确「观测面板 = 诊断视图」的定位文案；
3. 折叠状态在虚拟滚动下会丢失（若后续加虚拟列表），本方案暂不涉及。

---

### 方案 B：双栏「活动 / 产物」工作区

**一句话**：左侧是 Agent 活动流（垂直时间线），右侧是产物面板（diff / 文件 / 预览），完全对标 v0/Lovable 与 Claude.ai Artifact 模式。

**布局**：

```
┌───────────────────────────────────────────────────────────────┐
│ Titlebar                                                       │
├──────┬──────────────────────┬─────────────────────────────────┤
│ Act  │  Activity 流          │  Artifact 面板                │
│ Bar  │  (40%)                │  (60%)                         │
│      │                       │  ┌───────────────────────────┐ │
│      │  ▸ Thinking 2.3s      │  │ [Diff | Files | Preview] │ │
│      │  ▾ Bash  1.2s ✓       │  ├───────────────────────────┤ │
│      │    ⎿ output           │  │                           │ │
│      │  ▸ Read(3)  340ms     │  │   src/app.py              │ │
│      │  ▾ ⚠ Approval         │  │   @@ -12,7 +12,9 @@       │ │
│      │    [允许][拒绝]       │  │   - old()                 │ │
│      │  ▸ Edit app.py        │  │   + new()                 │ │
│      │    → 右侧显示 diff    │  │   + more()                │ │
│      │                       │  │                           │ │
│      │                       │  ├───────────────────────────┤ │
│      │                       │  │ [撤销改动][全部接受]      │ │
├──────┴──────────────────────┴─────────────────────────────────┤
│ Composer  [ctx 34%]  [发送→Stop]                              │
└───────────────────────────────────────────────────────────────┘
```

**新增 CSS 类与关键属性**：

```css
/* 双栏容器 —— resizer 用 CSS resize 或已有 JS 拖拽 */
.ws-split {
  display: grid;
  grid-template-columns: minmax(320px, var(--split-a, 40%)) 1fr;
  gap: 0;
}
.ws-split > .ws-left  { border-right: 1px solid var(--line); overflow-y: auto; }
.ws-split > .ws-right { overflow: hidden; display: flex; flex-direction: column; }

/* 产物面板头部 —— 预览/代码/文件三个 tab */
.art-head {
  display: flex; align-items: center; gap: var(--sp-1);
  height: var(--viewhead-h); padding: 0 var(--sp-2);
  background: var(--bg-3); border-bottom: 1px solid var(--line);
  flex: none;                     /* chrome 位置在流式期间保持不变 */
}
.art-tab {
  height: 20px; padding: 0 var(--sp-2); border-radius: var(--r-sm);
  font-size: var(--fs-md); color: var(--fg-dim);
  background: transparent; border: 0; cursor: pointer;
  transition: background 120ms ease-out, color 120ms ease-out;
}
.art-tab[aria-selected="true"] { background: var(--bg-5); color: var(--fg); }
.art-tab:focus-visible { outline: 1px solid var(--accent); outline-offset: -1px; }
.art-body { flex: 1 1 auto; overflow: auto; }

/* diff 渲染 —— 并排双栏 */
.d2 {
  display: grid; grid-template-columns: 1fr 1fr;
  font-family: var(--mono); font-size: var(--fs-sm);
  line-height: 1.55;
}
.d2-side { min-width: 0; }
.d2-gutter {
  width: 40px; text-align: right; padding-right: var(--sp-2);
  color: var(--fg-faint); user-select: none;
  background: var(--bg-1); border-right: 1px solid var(--line-soft);
  font-variant-numeric: tabular-nums;
}
.d2-line { padding: 0 var(--sp-2); white-space: pre-wrap; word-break: break-word; }
.d2-line.add { background: rgba(63,185,80,.12); }
.d2-line.del { background: rgba(248,81,73,.12); }
.d2-line.add .d2-gutter { background: rgba(63,185,80,.22); color: var(--ok); }
.d2-line.del .d2-gutter { background: rgba(248,81,73,.22); color: var(--err); }
.d2-line.hunk { background: var(--bg-3); color: var(--fg-dim); }

/* 窄屏降级：右侧变 overlay（不是挤压的第二列） */
@media (max-width: 900px) {
  .ws-split { grid-template-columns: 1fr; }
  .ws-split > .ws-right {
    position: fixed; inset: var(--titlebar-h) 0 0 0;
    background: var(--bg-2); z-index: 40;
    transform: translateX(100%);
    transition: transform 200ms cubic-bezier(.2,0,.38,.9);
  }
  .ws-split[data-art="open"] > .ws-right { transform: translateX(0); }
}
```

**动效取值**：产物面板滑入 200ms `cubic-bezier(.2,0,.38,.9)`；窄屏 overlay 滑出 150ms `cubic-bezier(.2,0,1,.9)`；tab 切换 120ms `ease-out`。

**需新增/改动文件**：
| 文件 | 改动 | 类型 |
|---|---|---|
| `static/artifact-panel.js` | 新增，右侧面板控制器 + tab 状态 | 新增 |
| `static/diff-view.js` | 新增，diff 解析与并排/统一渲染 | 新增 |
| `static/diff-view.css` | 新增（或并入 app.css） | 新增 |
| `static/app.css` | `.ws-split` / `.art-*` 样式 | 改动 |
| `static/index.html` | 主区改为双栏骨架 | 改动 |
| `static/app.js` | 工具调用 Edit/Write 事件 → 驱动右侧面板 | 改动 |
| `api/routes/files.py` | 新增，读取 diff 内容接口 | 新增 |

**工作量估算**：**20–28 小时**（diff 渲染器 8h、双栏骨架 6h、面板控制器 5h、窄屏降级 3h、联调 6h）

**风险点**：
1. **与现有「工作区」标签页功能重叠** —— 需要决定是改造现有 workspace 视图还是新建，容易造成两套代码编辑器；
2. **diff 数据量大时性能风险** —— 1000+ 行 diff 必须虚拟化，否则会卡；
3. **窄屏降级路径容易漏测** —— overlay 模式与双栏模式是两套布局，需分别验收；
4. 现有 12 个观测组件与右侧面板的信息重复度最高，可能要**废弃其中 2–3 个**（Diff Timeline / Repo Map 与新面板高度重合）。

---

### 方案 C（核心推荐）：Live Timeline —— 统一垂直事件时间线

**一句话**：把 12 个可观测性组件重组为**一条统一的垂直事件时间线**（用户提问 → 思考 → 工具调用 → 审批 → 最终回答），每个节点可折叠展开，并把每类组件的数据以「同一节点的不同视图」呈现，取代现在的 11 个平级 tab。

**核心洞见**：现在的 12 个视图是**同一批事件的 12 种切面**（waterfall / toolcards / sequence / cost / context / difftimeline / timing 本质都是 trace 的不同投影）。用户想看「刚才那次运行发生了什么」，正确答案是**一条时间线**，而不是 11 个需要切换的 tab。时间线本身就是观测类的「Waterfall + Tree」形态（Supabase/Jaeger 业界标准），只是把 trace 的 span 换成了 agent 的语义事件。

**布局**：

```
┌──────────────────────────────────────────────────────────────────────┐
│ Titlebar                                                              │
├──────┬───────────────┬──────────────────────────────────────────────┤
│ Act  │  Sidebar      │  Live Timeline                              │
│ Bar  │               │  ┌────────────────────────────────────────┐  │
│      │               │  │ ● run 2026-11-23 14:02 · model=claude  │  │ ← sticky header
│      │               │  │   wall 12.4s · compute 9.1s · wait 3.3s│  │
│      │               │  │   $0.084 · 18.2k tok · ctx 34%         │  │
│      │               │  ├────────────────────────────────────────┤  │
│      │               │  │                                        │  │
│      │               │  │  ┌─ 14:02:01 ─ user ─────────────────┐  │  │
│      │               │  │  │ 帮我改一下配置                    │  │  │
│      │               │  │  └──────────────────────────────────┘  │  │
│      │               │  │                                        │  │
│      │               │  │  ● 14:02:01  Thinking          2.3s   │  │  ← 节点头（单行）
│      │               │  │  │  ▾ (展开)                         │  │  │
│      │               │  │  │    我需要先确认配置文件的位置…    │  │  │
│      │               │  │                                        │  │
│      │               │  │  ● 14:02:03  Bash            1.2s ✓ │  │  ← 工具节点
│      │               │  │  │  ▸ git status --short              │  │  │
│      │               │  │  │  │ M src/config.py                  │  │  │
│      │               │  │  │  │  … +8 lines                       │  │  │
│      │               │  │                                        │  │
│      │               │  │  ◆ 14:02:04  Approval needed            │  │  ← 菱形（零时长事件）
│      │               │  │  │  ⚠ npm install --save-dev pkg       │  │  │
│      │               │  │  │    [允许一次] [始终允许] [拒绝]     │  │  │
│      │               │  │                                        │  │
│      │               │  │  ● 14:02:05  Edit src/config.py 340ms✓│  │
│      │               │  │  │  ▾ (展开) → 内嵌 diff 视图         │  │  │
│      │               │  │                                        │  │
│      │               │  │  ● 14:02:06  assistant                  │  │
│      │               │  │  │  已完成配置修改，共 2 个文件。      │  │
│      │               │  │                                        │  │
│      │               │  ├────────────────────────────────────────┤  │
│      │               │  │ [全部折叠] [仅看错误] [只看工具] ▓▓░ 34% │  │ ← 底部工具条
│      │               │  │ Composer  [发送 ⇧⏎ / ⏎]                │  │
├──────┴───────────────┴──────────────────────────────────────────────┤
│ Statusbar: ⏱ 12.4s | $0.084 | 18.2k tok | ctx 34% | ● running      │
└──────────────────────────────────────────────────────────────────────┘
```

#### C.1 DOM 骨架

```html
<!-- 一个时间线节点 = 一个 <details> 派生的自定义结构 -->
<article class="tl-node" data-kind="tool" data-status="ok" data-ts="1732345323000">
  <header class="tl-node-head" tabindex="0" role="button"
          aria-expanded="false" aria-controls="tl-b-4">
    <span class="tl-rail" aria-hidden="true"></span>          <!-- 左侧轨道 -->
    <span class="tl-dot" aria-hidden="true"></span>            <!-- 状态点 -->
    <time class="tl-time mono">14:02:03</time>
    <span class="tl-kind">Bash</span>
    <span class="tl-title">git status --short</span>
    <span class="tl-badge s-ok">✓</span>
    <span class="tl-dur mono">1.2s</span>
    <span class="tl-caret" aria-hidden="true">▸</span>
  </header>
  <div class="tl-panel" id="tl-b-4" role="region" aria-labelledby="tl-h-4">
    <div class="tl-panel-inner">
      <div class="tl-out mono">M src/config.py
… +8 lines</div>
      <button class="tl-more" type="button">展开全部输出</button>
    </div>
  </div>
</article>
```

**变体（12 类节点）**：
- `data-kind="user"` —— 用户提问，**不折叠**（`data-collapsible="false"`），右对齐气泡样式；
- `data-kind="thinking"` —— 思考块，流式时显示三点，完成后折叠为 "Thought for 2.3s"；
- `data-kind="tool"` —— 工具调用，可折叠；
- `data-kind="approval"` —— **菱形点**（零时长），展开后是审批卡；
- `data-kind="edit"` —— 文件编辑，展开后是内嵌 diff；
- `data-kind="error"` —— 错误，**默认展开**（`data-default-open="true"`），左侧红色轨；
- `data-kind="assistant"` —— 最终回答，不折叠，Markdown 渲染；
- `data-kind="checkpoint"` —— 检查点节点（借鉴 Cursor / Roo Code），展开后有 View Differences / Restore 按钮。

#### C.2 CSS 变量与关键类

```css
/* ---- 时间线骨架 ---- */
.tl {
  position: relative;
  padding: var(--sp-3) var(--sp-4) var(--sp-6) var(--sp-4);
}
.tl::before {                       /* 左侧竖轨 */
  content: ""; position: absolute; left: 22px; top: 0; bottom: 0;
  width: 1px; background: var(--line);
}

/* ---- 节点 ---- */
.tl-node { position: relative; padding-left: 34px; margin-bottom: var(--sp-1); }
.tl-node-head {
  display: flex; align-items: center; gap: var(--sp-2);
  min-height: 24px; padding: 2px var(--sp-2);
  border-radius: var(--r-sm); cursor: pointer; user-select: none;
  transition: background 120ms ease-out;
}
.tl-node-head:hover { background: var(--bg-3); }
.tl-node-head:focus-visible { outline: 1px solid var(--accent); outline-offset: -1px; }

.tl-dot {
  position: absolute; left: 18px; top: 8px;
  width: 9px; height: 9px; border-radius: 50%;
  background: var(--bg-4); border: 2px solid var(--bg-2);
  box-shadow: 0 0 0 1px var(--line);
  flex: none;
}
.tl-time { font-size: var(--fs-xs); color: var(--fg-faint); font-variant-numeric: tabular-nums; }
.tl-kind { font-size: var(--fs-sm); color: var(--fg); font-weight: 500; }
.tl-title {
  flex: 1 1 auto; min-width: 0; font-size: var(--fs-md); color: var(--fg-dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tl-dur { font-size: var(--fs-xs); color: var(--fg-faint); font-variant-numeric: tabular-nums; }

/* 状态着色 —— 语义色只用于状态点与徽章，不污染正文 */
.tl-node[data-status="ok"]    .tl-dot { background: var(--ok); }
.tl-node[data-status="err"]   .tl-dot { background: var(--err); }
.tl-node[data-status="run"]   .tl-dot { background: var(--run); animation: tl-pulse 1.6s ease-in-out infinite; }
.tl-node[data-status="wait"]  .tl-dot { background: var(--hitl); }
.tl-node[data-status="skip"]  .tl-dot { background: var(--bg-5); box-shadow: 0 0 0 1px var(--line-strong); }

/* 审批节点 —— 菱形，零时长事件不画成条 */
.tl-node[data-kind="approval"] .tl-dot {
  border-radius: 2px; transform: rotate(45deg); width: 10px; height: 10px; left: 17.5px;
  background: var(--hitl); box-shadow: 0 0 0 1px rgba(227,179,65,.5);
}

/* 折叠 —— CSS Grid 1fr↔0fr，零 JS 测量 */
.tl-panel {
  display: grid; grid-template-rows: 0fr;
  transition: grid-template-rows 180ms cubic-bezier(0.2, 0, 0.38, 0.9);
}
.tl-node[data-open="true"] .tl-panel { grid-template-rows: 1fr; }
.tl-panel-inner {
  min-height: 0; min-width: 0; overflow: hidden; align-self: start;
  padding: var(--sp-1) 0 var(--sp-2) 0;
}
.tl-node[data-collapsible="false"] .tl-panel { grid-template-rows: 1fr; }
.tl-caret { color: var(--fg-faint); transition: transform 180ms cubic-bezier(.2,0,.38,.9); }
.tl-node[data-open="true"] .tl-caret { transform: rotate(90deg); }

/* 状态行进/出 —— Claude.ai 同款参数 */
.tl-status-enter { animation: tl-enter 240ms cubic-bezier(.3,.7,.4,1); }
.tl-status-exit  { animation: tl-exit 180ms ease-out; }
@keyframes tl-enter { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes tl-exit  { from { opacity: 1; transform: none; } to { opacity: 0; transform: translateY(-4px); } }

/* 三点波浪（thinking 流式） */
.tl-dots { display: inline-flex; gap: 3.5px; position: relative; top: .26em; }
.tl-dots > span {
  width: 3px; height: 3px; border-radius: 50%; background: var(--accent); opacity: 0;
  animation: 1.9s cubic-bezier(.4,0,.2,1) infinite tl-dot;
}
.tl-dots > span:nth-child(2) { animation-delay: .15s; }
.tl-dots > span:nth-child(3) { animation-delay: .3s; }
@keyframes tl-dot {
  0%,8% { opacity:0; transform: translateY(2px); }
  22% { opacity:1; transform: translateY(-2.5px); }
  36%,70% { opacity:1; transform: translateY(0); }
  90%,100% { opacity:0; transform: translateY(0); }
}

/* 运行中的节点 —— 未完成用斜纹（诚实语义） */
.tl-node[data-status="run"] .tl-rail {
  background: repeating-linear-gradient(45deg,
    var(--accent-line) 0 4px, transparent 4px 8px);
  animation: tl-stripe 800ms linear infinite;
}
@keyframes tl-stripe { to { background-position: 8px 0; } }

/* 展开面板内的内容容器 */
.tl-out {
  font-family: var(--mono); font-size: var(--fs-sm); color: var(--fg-dim);
  background: var(--bg-1); border: 1px solid var(--line-soft);
  border-radius: var(--r-md); padding: var(--sp-2);
  max-height: 220px; overflow: auto;
  white-space: pre-wrap; word-break: break-word;
}
.tl-out.is-clipped {
  max-height: 96px; overflow: hidden;
  mask-image: linear-gradient(180deg, #000 60%, transparent 100%);
}

/* 审批卡 —— 按钮在内联，容器内不放带边框的二级按钮 */
.appr {
  border: 1px solid rgba(227,179,65,.4); background: rgba(227,179,65,.08);
  border-radius: var(--r-md); padding: var(--sp-2) var(--sp-3);
}
.appr-cmd { font-family: var(--mono); font-size: var(--fs-sm); color: var(--fg); margin-bottom: var(--sp-2); }
.appr-actions { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap; }
.appr-btn {
  height: 22px; padding: 0 var(--sp-3); border-radius: var(--r-sm);
  font-size: var(--fs-md); cursor: pointer; border: 1px solid var(--line-strong);
  background: var(--bg-4); color: var(--fg);
  transition: background 120ms ease-out, border-color 120ms ease-out;
}
.appr-btn:hover { background: var(--bg-5); }
.appr-btn:focus-visible { outline: 1px solid var(--accent); outline-offset: 1px; }
.appr-btn.primary { background: var(--accent); border-color: var(--accent); color: #0a0a0a; font-weight: 600; }
.appr-btn.primary:hover { filter: brightness(1.1); }
.appr-btn.danger:hover { border-color: var(--err); color: var(--err); }
.appr-reason {
  height: 22px; flex: 1 1 180px; min-width: 160px;
  background: var(--bg-1); border: 1px solid var(--line);
  border-radius: var(--r-sm); color: var(--fg); font-size: var(--fs-md);
  padding: 0 var(--sp-2);
}
.appr-reason:focus-visible { outline: 1px solid var(--accent); outline-offset: -1px; }

/* sticky run header */
.tl-header {
  position: sticky; top: 0; z-index: 5;
  background: var(--bg-2); border-bottom: 1px solid var(--line);
  padding: var(--sp-2) var(--sp-4);
  display: flex; align-items: center; gap: var(--sp-3);
  font-size: var(--fs-md); color: var(--fg-dim);
}
.tl-header .mono { font-variant-numeric: tabular-nums; }

/* 底部工具条 */
.tl-tools {
  position: sticky; bottom: 0; z-index: 5;
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-4);
  background: var(--bg-2); border-top: 1px solid var(--line);
  font-size: var(--fs-sm); color: var(--fg-faint);
}
.tl-filter {
  height: 20px; padding: 0 var(--sp-2); border-radius: var(--r-sm);
  background: transparent; border: 1px solid transparent; color: var(--fg-dim);
  font-size: var(--fs-sm); cursor: pointer; transition: all 120ms ease-out;
}
.tl-filter[aria-pressed="true"] { background: var(--accent-soft); color: var(--accent); border-color: var(--accent-line); }
.tl-filter:focus-visible { outline: 1px solid var(--accent); outline-offset: -1px; }

/* 降低动效偏好 —— WCAG 2.3.3 / C39 */
@media (prefers-reduced-motion: reduce) {
  .tl-panel, .tl-caret { transition: none; }
  .tl-dots > span, .tl-node[data-status="run"] .tl-dot,
  .tl-node[data-status="run"] .tl-rail { animation: none; }
  .tl-dots > span { opacity: 1; transform: none; }
  .tl-node[data-status="run"] .tl-dot { opacity: .6; }
}
```

#### C.3 JS 契约（伪代码）

```js
// Timeline 节点统一模型 —— 12 个观测组件全部投影到这一种
// { id, ts, kind, status, title, detail, meta, children }
function TimelineNode(o) {
  return Object.assign({
    id: '', ts: 0, kind: 'tool', status: 'ok',
    title: '', detail: null, meta: null, children: [],
    collapsible: true, defaultOpen: false
  }, o);
}

// SSE 事件 → 节点（统一入口）
const EVENT_TO_NODE = {
  user:        e => TimelineNode({ kind:'user', status:'ok', title:e.text, collapsible:false }),
  thinking:    e => TimelineNode({ kind:'thinking', status:'run', title:'Thinking', meta:{dur:e.dur_ms} }),
  tool_call:   e => TimelineNode({ kind:'tool', status:'run', title:e.name, detail:e.args }),
  tool_result: e => TimelineNode({ kind:'tool', status:e.error?'err':'ok', detail:e.output, meta:{dur:e.dur_ms} }),
  approval:    e => TimelineNode({ kind:'approval', status:'wait', title:e.tool, detail:e.args, defaultOpen:true }),
  file_edit:   e => TimelineNode({ kind:'edit', status:'ok', title:e.path, detail:e.diff }),
  error:       e => TimelineNode({ kind:'error', status:'err', title:e.message, defaultOpen:true }),
  assistant:   e => TimelineNode({ kind:'assistant', status:'ok', title:e.text, collapsible:false }),
  checkpoint:  e => TimelineNode({ kind:'checkpoint', status:'ok', title:e.label, meta:e }),
};

// 关键：Action → Observation 原地替换（OpenHands 模式）
function upsertNode(nodes, ev) {
  const n = EVENT_TO_NODE[ev.kind]?.(ev); if (!n) return nodes;
  if (ev.action_id) {                       // 同 action_id 原地更新
    const i = nodes.findIndex(x => x.action_id === ev.action_id);
    if (i >= 0) { nodes[i] = Object.assign(nodes[i], n, { id: nodes[i].id }); return nodes; }
  }
  nodes.push(n); return nodes;
}

// 700ms 最短展示 —— 防闪烁（Claude Code 模式）
const MIN_HINT_MS = 700;
function scheduleHint(node, render) {
  const wait = Math.max(0, MIN_HINT_MS - (Date.now() - node.ts));
  setTimeout(render, wait);
}
```

#### C.4 12 个组件 → 时间线的映射

| 现有组件 | 在时间线中的位置 | 改造方式 |
|---|---|---|
| Trace Waterfall | 时间线整体（横向时间轴 → 纵向事件流） | **保留**，作为时间线右上角的「耗时视图」切换 |
| Tool Call Card | `kind="tool"` 节点 | 直接复用节点渲染器，删除独立 tab |
| Agent Sequence | 时间线的层级缩进（子 agent 缩进 1 级） | 由 `children` 表达 |
| Cost Waterfall | **sticky run header** 的 `$0.084` + 点击展开的 per-node 成本 | 降级为 header 的一部分 |
| Context Map | sticky header 的 `ctx 34%` + 悬停 popover | 降级 |
| Approval Card | `kind="approval"` 菱形节点 | 直接复用节点渲染器 |
| Diff Timeline | `kind="edit"` 节点内嵌 diff | 合并进节点展开区 |
| Agent Timing | 每个节点的 `tl-dur` + sticky header 的三段时长 | 合并进节点元信息 |
| Repo Map | 工具调用 `Read` 的参数展开区 | 降级为节点详情 |
| Autonomy Dial | Composer 旁的模式选择器（借鉴 Continue 的 rounded-full 按钮） | 移到 Composer |
| Reasoning Summary | `kind="thinking"` 节点 | 直接复用节点渲染器 |

**结果**：11 个 tab → **1 个 Live Timeline + 3 个筛选按钮 + 1 个耗时视图切换**。

#### C.5 动效取值汇总

| 场景 | duration | easing | 备注 |
|---|---|---|---|
| 节点展开 | 180ms | `cubic-bezier(0.2, 0, 0.38, 0.9)` | Carbon productive standard |
| 节点收起 | 120ms | `cubic-bezier(0.4, 0, 1, 0.9)` | Carbon productive exit |
| caret 旋转 | 180ms | `cubic-bezier(0.2, 0, 0.38, 0.9)` | 与展开同步 |
| 三点波浪 | 1900ms loop | `cubic-bezier(.4, 0, .2, 1)` | Claude.ai 原值 |
| 状态行进入 | 240ms | `cubic-bezier(.3, .7, .4, 1)` | Claude.ai 原值 |
| 状态行退出 | 180ms | `ease-out` | Claude.ai 原值 |
| 运行中点脉冲 | 1600ms loop | `ease-in-out` | opacity 0.55↔1 |
| 运行中斜纹 | 800ms loop | `linear` | 背景位移 8px |
| 上下文条 | 400ms | `cubic-bezier(.2, 0, .38, .9)` | |
| hover 反馈 | 120ms | `ease-out` | 与现有 `--transition` 一致 |
| 工具提示最短展示 | 700ms（**非动效，是逻辑延时**） | — | Claude Code `MIN_HINT_DISPLAY_MS` |

#### C.6 需新增/改动文件

| 文件 | 改动 | 类型 | 预估行数 |
|---|---|---|---|
| `static/tokens.css` | 全部设计令牌（见 D 节）+ 时间线变量 | **新增** | ~140 |
| `static/timeline.css` | `.tl-*` / `.appr-*` 全部时间线样式 | **新增** | ~260 |
| `static/timeline.js` | `TimelineNode` 模型 + 渲染器 + SSE 订阅 + 筛选 | **新增** | ~420 |
| `static/app.css` | 删除被时间线取代的旧观测样式；`:root` 合并 `tokens.css` | 改动 | -180 / +20 |
| `static/observability.js` | 剥离 11 个视图的渲染逻辑，仅保留 trace 存储与 SSE 桥接 | 改动 | -900 / +60 |
| `static/observability.css` | 大部分被 `timeline.css` 取代 | 改动 | -240 |
| `static/index.html` | 观测 tab 换成 `<div class="tl" id="live-timeline">`；Composer 加 Autonomy 按钮 | 改动 | ~30 |
| `static/app.js` | 对话区与时间线联动；点节点 → 滚动定位 | 改动 | ~80 |
| `api/routes/chat.py` | **SSE 事件契约统一**（kind/ts/action_id/status/dur_ms/payload） | 改动 | ~120 |
| `api/routes/trace.py` | 新增：时间线节点快照接口（刷新后可恢复） | 新增 | ~90 |

**工作量估算**：**30–40 小时**

| 子项 | 小时 |
|---|---|
| SSE 事件契约统一（后端） | 6 |
| `tokens.css` 令牌重构 | 3 |
| `timeline.js` 渲染器 + 折叠 + 筛选 + SSE 接线 | 12 |
| `timeline.css` 样式 | 5 |
| 12 组件映射与旧代码剥离 | 5 |
| 审批卡 + diff 内嵌 + checkpoint 节点 | 6 |
| 联调 / 深色主题 / 无障碍验收 | 5 |

**风险点**：
1. **SSE 事件契约是最大风险** —— 后端若不能统一出 `{kind, ts, action_id, status, dur_ms, payload}`，前端会写满兼容分支。**必须先改后端**，这是前置依赖。
2. **现有 `observability.js` 约 1600 行 + `observability.css` 244 行会被大幅削减**，重构时容易引入回归；建议保留旧 tab 一个版本做 A/B 对照再删。
3. **长会话的性能**：节点数超过 500 时 DOM 会明显变慢；需要节点回收策略（只保留最近 N 个展开状态 + 虚拟化未展开部分）。第一版可先做「折叠的节点不渲染 body」（`content-visibility: auto`）来缓解。
4. **时间线取代 11 个 tab 后，原本依赖观测面板的用户会找不到功能** —— 需要保留入口（如「耗时视图」按钮切换到 Waterfall），并在 header 里说明。
5. **approval 节点做成菱形后，用户可能不知道要点击** —— 必须配合 sticky header 上的 `⚠ 1 项待审批` 角标 + 状态栏提醒。

---

## D. 设计令牌表（深色 IDE 主题）

可直接替换现有 `app.css` 的 `:root`。在现有基础上**新增**了折叠/时间线/语义状态/上下文占用所需的令牌，并把动效时长细化。

```css
:root {
  /* ===== 1. 背景层级（6 级，外→内） ===== */
  --bg-0: #141414;   /* 应用外层 / 标题栏 */
  --bg-1: #181818;   /* 侧栏 / 活动栏 / 代码块底 */
  --bg-2: #1f1f1f;   /* 编辑器主区 / 对话底 */
  --bg-3: #252526;   /* 标签栏 / 悬浮提示 / hover */
  --bg-4: #2a2a2c;   /* 输入框 / 按钮 / 展开面板 */
  --bg-5: #333336;   /* 模态框 / 菜单 / 浮层 */

  /* ===== 2. 文本层级（5 级） ===== */
  --fg:        #cccccc;  /* 正文 */
  --fg-strong: #ffffff;  /* 强调正文 / 代码 */
  --fg-dim:    #8b8b8b;  /* 次要 / 工具标题 */
  --fg-faint:  #6e6e6e;  /* 时间戳 / 辅助 */
  --fg-mute:   #5a5a5a;  /* 禁用 */

  /* ===== 3. 边框 ===== */
  --line:        #2e2e2e;   /* 默认分隔线 */
  --line-soft:   #262626;   /* 弱分隔（代码块内） */
  --line-strong: #3c3c3c;   /* 按钮边框 / hover */

  /* ===== 4. 强调色 ===== */
  --accent:      #4d9fff;
  --accent-2:    #7c5cff;
  --accent-soft: rgba(77, 159, 255, .14);
  --accent-line: rgba(77, 159, 255, .45);

  /* ===== 5. 语义色 ===== */
  --ok:      #3fb950;  --ok-soft:  rgba(63, 185, 80, .14);
  --warn:    #d29922;  --warn-soft:rgba(210, 153, 34, .14);
  --err:     #f85149;  --err-soft: rgba(248, 81, 73, .14);
  --run:     #4d9fff;  --run-soft: rgba(77, 159, 255, .14);
  --hitl:    #e3b341;  --hitl-soft:rgba(227, 179, 65, .14);
  --skip:    #6e6e6e;  --skip-soft:rgba(110, 110, 110, .12);

  /* diff 专用（新增） */
  --diff-add:      rgba(63, 185, 80, .12);
  --diff-add-gut:  rgba(63, 185, 80, .24);
  --diff-del:      rgba(248, 81, 73, .12);
  --diff-del-gut:  rgba(248, 81, 73, .24);
  --diff-hunk:     #252526;

  /* ===== 6. 间距刻度（4px 栅格） ===== */
  --sp-0: 0;   --sp-1: 4px;   --sp-2: 8px;  --sp-3: 12px;
  --sp-4: 16px; --sp-5: 20px;  --sp-6: 24px; --sp-8: 32px;

  /* ===== 7. 字号刻度 ===== */
  --fs-xs:  10.5px;  /* 时间戳 / 徽标 */
  --fs-sm:  11px;    /* 代码 / 次要 */
  --fs-md:  12px;    /* 工具标题 / 摘要 */
  --fs-base:13px;    /* 正文 */
  --fs-lg:  14px;    /* 强调 */
  --fs-xl:  16px;    /* 标题 */

  /* ===== 8. 圆角 ===== */
  --r-xs: 2px;   /* 树行 / chip */
  --r-sm: 2px;   /* 按钮（Cursor 规范） */
  --r-md: 4px;   /* 面板 / 输入框 */
  --r-lg: 6px;   /* 模态框 */
  --r-pill: 999px;

  /* ===== 9. 阴影 ===== */
  --sh-sm: 0 2px 8px rgba(0,0,0,.30);
  --sh-md: 0 8px 28px rgba(0,0,0,.45);
  --sh-lg: 0 16px 48px rgba(0,0,0,.55);

  /* ===== 10. 动效时长（新增分级） ===== */
  --dur-instant:  0ms;                                    /* 即时（reduced-motion） */
  --dur-fast:     120ms;  /* hover / 颜色 */
  --dur-base:     180ms;  /* 折叠展开 / caret */
  --dur-slow:     240ms;  /* 状态行进入 */
  --dur-slower:   400ms;  /* 进度条 */
  --dur-loop:     900ms;  /* spinner 扫光 */
  --dur-wave:    1900ms;  /* 三点波浪 */
  --dur-pulse:   1600ms;  /* 运行中脉冲 */
  --dur-stripe:   800ms;  /* 运行中斜纹 */

  /* ===== 11. 缓动函数 ===== */
  --ease-standard: cubic-bezier(0.2, 0, 0.38, 0.9);   /* Carbon productive standard */
  --ease-exit:     cubic-bezier(0.2, 0, 1, 0.9);      /* Carbon productive exit */
  --ease-soft:     cubic-bezier(0.3, 0.7, 0.4, 1);     /* Claude 状态行进入 */
  --ease-spring:   cubic-bezier(0.19, 1, 0.22, 1);     /* Claude 折叠 */
  --ease-quart:    cubic-bezier(0.87, 0, 0.13, 1);     /* shadcn accordion */
  --ease-out:      ease-out;                            /* 通用 */
  --ease-linear:   linear;                              /* 循环动画 */

  /* 组合式（最常用） */
  --t-collapse-in:  var(--dur-base) var(--ease-standard);
  --t-collapse-out: var(--dur-fast) var(--ease-exit);
  --t-hover:        var(--dur-fast) var(--ease-out);

  /* ===== 12. 布局尺寸 ===== */
  --titlebar-h: 34px;  --activitybar-w: 48px;  --sidebar-w: 268px;
  --tabbar-h: 28px;    --viewhead-h: 32px;    --treerow-h: 22px;
  --resizer-w: 5px;
  --timeline-rail-x: 22px;   /* 时间线竖轨 X */
  --timeline-node-pad: 34px; /* 节点左缩进 */

  /* ===== 13. 字体栈 ===== */
  --mono: ui-monospace, "Cascadia Code", "Cascadia Mono", "SF Mono",
          Menlo, Consolas, "Liberation Mono", monospace;
  --sans: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei UI",
          "PingFang SC", "Hiragino Sans GB", sans-serif;

  /* ===== 14. 模式强调色 ===== */
  --mode-agent: #4d9fff;
  --mode-ask:   #3fb8c4;
  --mode-plan:  #a78bfa;

  /* ===== 15. 上下文占用阈值（新增） ===== */
  --ctx-near: 0.80;   /* ≥80% 变黄 */
  --ctx-full: 0.95;   /* ≥95% 变红 */
}
```

**降级动效**（放在文件末尾，全局生效）：

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .01ms !important;
    scroll-behavior: auto !important;
  }
}
```

---

## E. 反模式清单

### E.1 工具结果不截断，直接撑爆页面
**现象**：一次 `Read` 一个 3000 行文件，或 `Bash` 输出 10MB，整个对话区被顶飞，滚动条失效，用户永远看不到 agent 的最终回答。
**正确做法**：
- 默认显示**首 5–8 行 + `… +N lines`**，剩余部分在展开时按需加载；
- 用 `max-height` + `mask-image` 渐隐（**不是硬裁剪**，否则用户不知道下面还有内容）；
- 折叠块的展开必须是**显式用户动作**（Claude Code 的 `Ctrl+O`、shadcn 的展开按钮），**禁止自动展开**；
- 超过阈值的输出提供「下载完整结果」按钮（shadcn Artifact 面板的做法）。

### E.2 diff 不分栏 / 不带行号
**现象**：把 unified diff 的原始文本直接塞进 `<pre>`，红绿行混在一起，没有行号，用户无法判断改动位置。
**正确做法**：
- **默认 unified 单栏**（Cursor 官方默认），但**必须**：① 有行号列（右侧，tabular-nums）；② 有 hunk 头 `@@ -12,7 +12,9 @@`；③ 有 `+/−` 前缀或 gutter 底色区分增删；④ 可切换并排双栏；
- 行号列用 `font-variant-numeric: tabular-nums` 保证对齐；
- 大改动默认**折叠未变更区域**（`hideUnchangedRegions`，VS Code 已内置此概念）。

### E.3 加载状态没有视觉信号
**现象**：用户点了发送，什么都没发生，只有一个空白的输入框，直到 30 秒后回答突然出现。
**正确做法**：
- **双通道**：状态文字（"Thinking" / "Reading 3 files" / "Running tests"）**+** 动态指示器（三点波浪或 shimmer）；
- 指示器必须**位于消息流末端**（而不是页面顶部固定条），因为那里才是「最新进展」；
- 状态文案要**具体**（来自事件本身），不要泛化成 "Working..."（OpenHands PR #16110 的核心论点）；
- 最小展示时长 700ms，避免快速完成的工具一闪而过（Claude Code `MIN_HINT_DISPLAY_MS`）。

### E.4 流式文本与最终消息重复显示
**现象**：agent 的思考过程在流式阶段显示了一遍，持久化事件到达后又追加了完整的一份，用户看到两份一样的推理。
**正确做法**：
- 引入**「后到的持久事件取代流式预览」**机制：流式 delta 合并进同一个气泡；持久化 Action 到达时，**移除与之匹配的流式 delta**（OpenHands `supersedeStreamingDeltasForAction`）；
- 同理，**Action 与 Observation 要原地替换**，不要追加两条（OpenHands `handleEventForUI`）；
- 流式期间若 Action 自身不带 reasoning，保留为「仅推理」气泡，不要整段丢弃。

### E.5 审批卡做成模态弹窗，或藏得太深
**现象**：agent 要执行 `rm -rf`，弹出一个覆盖全屏的模态框，打断了用户的其他工作；或者反过来，审批按钮藏在一个需要先展开三层才能看到的折叠块里。
**正确做法**：
- 审批卡**就地渲染在事件发生的那一行**，不弹模态（Claude.ai / Roo Code 模式）；
- **折叠态的卡片上也要能直接批准**（Cursor 的 "Allow for pending file edits and MCP tool calls" on collapsed subagent cards）；
- 拒绝时**必须提供理由输入框**，与拒绝按钮并排 —— 让 agent 能根据反馈调整（Roo Code）；
- 有**全局角标**提醒：状态栏 `⚠ 1 项待审批` + 应用角标（Cursor 的 unread badge 模式）；
- 危险命令（递归删除、force、wildcard）**永远不能自动批准**（Cursor 3.24 的 auto-review 策略）。

### E.6 零时长事件伪装成时间条
**现象**：在 waterfall 里把「等待审批」画成一条横跨 3 分钟的时间条，用户误以为是模型推理慢。
**正确做法**：
- **审批 / checkpoint / 重试 / 异常**画成**点或菱形标记**，不是时长条（agent trace 视觉契约）；
- **严格区分 wall time / active compute time / human-wait time**，三个数都在 header 里显示；
- 未完成的节点用**斜纹（hatching）+ 延伸到 now** 表示，不得显示为已完成；
- **绝不用客户端定时器伪造进度条**，时间必须来自服务端时间戳。

### E.7 卡片内嵌套带边框的二级按钮
**现象**：在一个已经带边框的工具卡里，再放一个带边框的「详情」按钮，视觉上像两个同级的独立对象，用户不知道该点哪个。
**正确做法**：
- **容器内的内联按钮一律 `border: none; background: transparent`**，靠背景变化和更高的图标层级表达可交互性（Claude Desktop 的明确规则）；
- 真正的按钮（允许 / 拒绝）放在卡片**底部独立的 action row**，而不是散落在参数区；
- 需要强调的操作用填充色（`primary`）而非更多边框来区分。

### E.8 成本与上下文占用「要么完全没有，要么塞满屏幕」
**现象**：要么完全不显示成本，用户跑了 50 轮才惊呼；要么在侧栏放一个巨大的成本面板，占了三分之一宽度，但没人天天看。
**正确做法**：
- **常驻位置是 composer / 状态栏**，显示**百分比**而不是精确数字（shadcn `Context` = HoverCard + Progress；Copilot CLI `current_context_used_percentage`）；
- **精确数字放 hover 弹层**：`used / limit`、`$cost`、`token 分类`（system / tools / messages）；
- 上下文条要有**阈值变色**（<80% 绿 / ≥80% 黄 / ≥95% 红），并说明到顶后会怎样（OAR / aparté 的 `data-status` 模式）；
- 每轮结束更新一次即可，不要实时跳动（除 Copilot CLI 那种 live 场景）。

---

## F. 立即可做的三件事（按性价比排序）

### 1. 给 SSE 事件定一个统一契约（后端，最高优先级）
**理由**：方案 A/B/C 全部依赖它。Claude Code 用三阶段渲染（意图 → 执行中 → 结果）、OpenHands 用 Action/Observation 原地替换，都建立在「事件有稳定 kind + action_id + 状态迁移」之上。目前若事件结构不统一，前端任何时间线/卡片改造都会写满兼容分支，返工成本极高。
**具体做**：`{ kind, ts, action_id, status, dur_ms, title, payload }`，`kind ∈ {user, thinking, tool_call, tool_result, approval, file_edit, error, assistant, checkpoint}`，`status ∈ {run, ok, err, wait, skip}`。**这一步是方案 C 的前置依赖，不做的话后面全部要返工。**

### 2. 工具调用卡片改为「单行摘要 + 折叠展开 + 结果截断」
**理由**：这是 Claude Code、Claude.ai、shadcn AI Elements、Cline 的**共同做法**，也是调研中出现的**最普遍反模式**（不截断撑爆页面）的直接解法。改动局限在 CSS + 一个渲染函数（CSS Grid `1fr ↔ 0fr` 即可，零 JS 测量），**2–4 小时即可见效**，且是方案 C 的组成部分不是浪费。
**具体做**：单行显示「工具名 + 关键参数 + 耗时 + 状态徽章」；展开区用 `max-height: 96px` + `mask-image` 渐隐 + 「展开全部输出」按钮；参数流式不完整时用 `Partial` 容忍，绝不显示空白卡。

### 3. 在状态栏加常驻的「上下文占用 % + 成本 + 三段时长」
**理由**：横向对比表里**成本可见性是本项目最大的短板**（除观测类产品外，全行业都在 2–3 分）。而且 shadcn `Context` 已经给出了现成规范：状态栏放百分比，hover 出 `used / limit` 明细。这是一处**纯增量**改动（不动现有 12 个组件），**2–3 小时**就能显著补齐能力短板。同时把 wall / compute / wait 三个时长分开显示，直接解决「等待审批被误判为推理慢」的归因问题。

**不建议立刻做的**：方案 B 的双栏产物面板 —— 它与现有「工作区」视图功能重叠，且需要 20–28 小时；等方案 C 的时间线跑通、`Edit` 节点能内嵌 diff 之后，再把右侧面板作为时间线的「展开视图」叠加上去，成本会低得多。

---

## G. 来源清单

**Cursor**
1. https://cursor.com/docs/agent/overview
2. https://cursor.com/changelog/0-43-x
3. https://cursor.com/changelog/0-46-x
4. https://cursor.com/changelog/0-50
5. https://cursor.com/changelog/page/16
6. https://cursor.com/docs/release-notes/ide
7. https://forum.cursor.com/t/side-by-side-diffs/24844
8. https://forum.cursor.com/t/can-we-bring-the-inline-diff-view/146592
9. https://forum.cursor.com/t/cursor-doesnt-show-inline-diff-in-main-window-and-auto-approves-all-changes/154362

**Claude Code / Claude.ai**
10. https://code.claude.com/docs/en/interactive-mode
11. https://code.claude.com/docs/en/fullscreen
12. https://quidproquo.cc/posts/ai/2026-09-06-coding-agent-tui-tool-result-no-flicker-en/
13. https://news.ycombinator.com/item?id=46981968
14. https://github.com/anthropics/claude-code/issues/99990
15. https://github.com/anthropics/claude-code/issues/32555
16. https://github.com/claude-code-best/claude-code/blob/632f3e19/src/components/Messages.tsx
17. https://zhanghandong.github.io/harness-engineering-from-cc-to-ai-coding/en/part1/ch02.html
18. https://scrimui.dev/inspiration/claude
19. https://openclaudeui.com/en/docs/v1/03
20. https://openclaudeui.com/en/docs/v1/08
21. https://gradientdeck.com/ai-components
22. https://www.northbase.design/systems/claude/ai-input-output

**Windsurf / Continue / OpenHands / aider / Cline**
23. https://www.yodev.dev/t/setting-up-windsurf-a-practical-guide-to-cascade-workflows/1323
24. https://builderscamp.com/guides/tools/build-a-prototype-with-windsurf
25. https://www.browsermoon.com/windsurf-templates
26. https://flowstep.ai/mcp/windsurf
27. https://docs.continue.dev/ide-extensions/agent/quick-start
28. https://github.com/continuedev/continue/blob/cb273098/gui/src/components/ModeSelect/ModeSelect.tsx
29. https://continue-docs.mintlify.app/ide-extensions/agent/how-to-customize
30. https://continue.ghost.io/a-fresh-coat-of-code-continues-new-vs-code-interface/
31. https://github.com/OpenHands/OpenHands/blob/11d4ecf2/frontend/src/utils/handle-event-for-ui.ts
32. https://github.com/OpenHands/OpenHands/pull/16110
33. https://deepwiki.com/OpenHands/OpenHands/8.2-event-rendering-and-message-display
34. https://aider.chat/docs/repomap.html
35. https://aider.chat/docs/usage/commands.html
36. https://aider.chat/docs/config/options.html
37. https://github.com/Aider-AI/aider/issues/1160
38. https://deepwiki.com/RooCodeInc/Roo-Code-Docs/4.1.1-chat-interface-and-interaction-model
39. https://github.com/RooCodeInc/Roo-Code/blob/main/apps/docs/docs/features/auto-approving-actions.mdx
40. https://docs.cline.bot/core-workflows/plan-and-act

**观测类 / 构建类**
41. https://supabase.com/design-system/docs/ui-patterns/tracing
42. https://pho-ui.com/components/trace
43. https://posthog.com/docs/ai-observability/traces
44. https://langfuse.com/changelog/2026-11-20-redesigned-trace-view
45. https://github.com/HomenShum/agentic-ui-qa/blob/main/TRACE-WATERFALL.md
46. https://www.saasui.design/application/v0
47. https://scrimui.dev/patterns/artifact-workspace
48. https://chatcn.dev/docs/components/app-builder
49. https://lovable.dev/guides/how-to-build-a-chatbot-ui

**设计系统 / 可访问性**
50. https://ui.shadcn.com/docs/ai-elements
51. https://ui.shadcn.com/docs/ai-elements/base/chat
52. https://ui.shadcn.com/docs/ai-elements/base/context
53. https://ai-sdk.dev/docs/ai-sdk-ui/chatbot
54. https://www.shadcn.io/ui/ai-sdk
55. https://www.carbondesignsystem.com/building-blocks/foundations/carbon-for-ai
56. https://www.carbondesignsystem.com/building-blocks/foundations/motion/overview
57. https://chat.carbondesignsystem.com/tag/latest/docs/documents/UI_customization.Layout.html
58. https://v10.carbondesignsystem.com/community/patterns/chatbot/usage/
59. https://www.w3.org/TR/WCAG22/
60. https://www.w3.org/WAI/WCAG22/Understanding/animation-from-interactions
61. https://developer.chrome.com/docs/modern-web-guidance/guides/ui-components/accordion
62. https://www.radix-ui.com/primitives/docs/components/accordion
63. https://designsystem.digital.gov/components/accordion/accessibility-tests/

**上下文 / token 显示**
64. https://github.com/EncodeTS/copilot-statusline/
65. https://oar.sh/docs/usage/context-window/
66. https://ai.honestui.com/docs/components/context-meter
67. https://apartejs.dev/components/context-gauge/
