/* ==========================================================================
   Live Timeline —— 统一垂直事件时间线（原生 JS，零依赖、零 CDN、零 emoji）
   --------------------------------------------------------------------------
   调研规格：docs/05_Agent_UI_设计调研与规格.md 第 C 节（方案 C）。

   核心洞见：12 个观测组件本质是**同一批 trace 的 12 种投影**。
   本文件把它们重组为**一条统一的垂直事件时间线**：
       用户提问 → 思考 → 工具调用 → 审批 → 工具结果 → 最终回答

   同时**完整保留** 11 个原视图的可达性：
     · 3 个筛选（全部 / 仅工具 / 仅审批）+ 错误筛选
     · 「耗时视图」切换 → 回到原 Trace Waterfall
     · 「全部视图」切换 → 回到原 11 视图分段控件（OBS_VIEWS）
   因此没有任何功能被删除。

   折叠动效：**CSS Grid 1fr ↔ 0fr**（零 JS 测量、零 scrollHeight 读取）。
   ========================================================================== */
'use strict';

/* ----------------------------------------------------------------------
   全局状态
   ---------------------------------------------------------------------- */
const TL = {
  /* 筛选：all | tool | approval | error */
  filter: 'all',
  /* 视图：timeline | waterfall | gallery */
  view: 'timeline',
  /* 已展开的节点 id 集合（跨重绘保持） */
  open: new Set(),
  /* 节点序号自增器 */
  seq: 0,
  /* 渲染锁：避免 SSE 高频事件导致的重绘风暴 */
  pending: false,
};

/* ----------------------------------------------------------------------
   定位器提示文案（2026-11-24，审批统一入口）
   ----------------------------------------------------------------------
   运行观测面板的「批准 / 拒绝」按钮**不再自己决定审批**，
   只是一个「跳到对话里那张审批卡片」的定位器。
   这一行文案要写清楚，否则用户会以为在面板里点了就算批了。
   （approval.js 未加载时用这个本地兜底，避免整页报错。） */
const TL_LOCATOR_HINT = (typeof APPROVAL !== 'undefined' && APPROVAL.LOCATOR_HINT)
  ? APPROVAL.LOCATOR_HINT
  : '跳转到对话中的审批卡片（审批统一在对话里完成）';

/* ----------------------------------------------------------------------
   节点模型
   ----------------------------------------------------------------------
   统一模型（对齐调研 C.3 的 TimelineNode）：
       { id, ts, kind, status, title, detail, meta, collapsible, defaultOpen }
   kind ∈ user | thinking | tool | tool_result | approval | edit | error
        | assistant | checkpoint
   status ∈ ok | run | err | wait | skip | pending
   ---------------------------------------------------------------------- */
function tlNode(o) {
  return Object.assign({
    id: '',
    ts: 0,
    kind: 'tool',
    status: 'ok',
    title: '',
    subtitle: '',
    detail: null,
    meta: null,
    collapsible: true,
    defaultOpen: false,
  }, o || {});
}

/** 节点 id：同一 trace 条目在多次重绘中必须保持同一个 id（展开态才能保持） */
function tlNodeId(e, i) {
  if (!e) return 'tl-' + i;
  return 'tl-' + (e.id ? String(e.id).replace(/[^\w-]/g, '_') : ('seq' + (e.seq || i)));
}

/* ----------------------------------------------------------------------
   trace 条目 → 节点列表
   ----------------------------------------------------------------------
   这是 12 个观测组件的统一投影入口：**一条 trace 序列 → 一串语义节点**。
   零时长事件（审批）画成菱形点，不画成时间条（反模式 E.6）。
   ---------------------------------------------------------------------- */
function tlBuildNodes() {
  const trace = (typeof OBS !== 'undefined' && OBS.trace) ? OBS.trace : [];
  const nodes = [];

  /* ---- 用户提问（作为时间线起点）---- */
  const prompt = tlPromptText();
  if (prompt) {
    nodes.push(tlNode({
      id: 'tl-user',
      ts: tlRunStartTs(),
      kind: 'user',
      status: 'ok',
      title: prompt,
      collapsible: false,
    }));
  }

  /* ---- trace 序列 ---- */
  trace.forEach((e, i) => {
    if (!e) return;
    const kind = e.kind || 'tool';
    const base = {
      id: tlNodeId(e, i),
      ts: Number(e.started_at || e.ts || 0) * 1000,
      meta: e,
    };

    if (kind === 'approval') {
      /* 审批 —— 零时长菱形节点，状态区分待批 / 已批 / 已拒 */
      const ap = tlApprovalOf(e);
      const resolved = ap && ap.resolved;
      nodes.push(tlNode(Object.assign(base, {
        kind: 'approval',
        status: resolved ? (resolved === 'approved' ? 'ok' : 'err')
          : (ap && ap.resolved_at ? 'ok' : 'wait'),
        title: e.name || (ap && ap.tool_name) || '审批',
        subtitle: e.action_summary || '',
        detail: e,
        defaultOpen: false,
      })));
      return;
    }

    if (kind === 'llm') {
      /* 模型轮次 —— 思考节点 */
      nodes.push(tlNode(Object.assign(base, {
        kind: 'thinking',
        status: e.ok === false ? 'err' : (e.ended_at ? 'ok' : 'run'),
        title: 'Agent 思考',
        subtitle: e.action_summary || '',
        detail: e,
      })));
      return;
    }

    if (e.ok === false) {
      /* 失败的工具调用 —— 错误节点，默认展开 */
      nodes.push(tlNode(Object.assign(base, {
        kind: 'error',
        status: 'err',
        title: e.name || '(工具)',
        subtitle: (e.error && e.error.message) || '执行失败',
        detail: e,
        defaultOpen: true,
      })));
      return;
    }

    /* 工具调用 —— 可折叠 */
    nodes.push(tlNode(Object.assign(base, {
      kind: 'tool',
      status: e.ended_at ? 'ok' : (e.result !== undefined && e.result !== null ? 'ok' : 'run'),
      title: e.name || '(工具)',
      subtitle: e.action_summary || '',
      detail: e,
    })));
  });

  /* ---- 最终回答 ---- */
  const answer = tlAnswerText();
  if (answer) {
    nodes.push(tlNode({
      id: 'tl-assistant',
      ts: Date.now(),
      kind: 'assistant',
      status: 'ok',
      title: answer,
      collapsible: false,
    }));
  }

  return nodes;
}

/* ----------------------------------------------------------------------
   数据来源辅助
   ---------------------------------------------------------------------- */

/** 取本轮用户提问（来自 start 事件的 message） */
function tlPromptText() {
  if (typeof OBS === 'undefined') return '';
  return OBS.prompt || '';
}

/** 取本轮最终回答 */
function tlAnswerText() {
  if (typeof OBS === 'undefined') return '';
  return OBS.answer || '';
}

/** 本轮开始时刻（毫秒） */
function tlRunStartTs() {
  if (typeof OBS === 'undefined' || !OBS.startedAt) return 0;
  return Number(OBS.startedAt) * 1000;
}

/** 按 tool_call_id 找审批项 */
function tlApprovalOf(e) {
  if (typeof OBS === 'undefined') return null;
  const list = OBS.approvals || [];
  const id = e && e.id;
  for (let i = 0; i < list.length; i++) {
    if (list[i] && list[i].tool_call_id === id) return list[i];
  }
  return null;
}

/* ----------------------------------------------------------------------
   筛选
   ---------------------------------------------------------------------- */
const TL_FILTERS = [
  { id: 'all', label: '全部', match: () => true },
  { id: 'tool', label: '仅工具', match: (n) => n.kind === 'tool' || n.kind === 'thinking' },
  { id: 'approval', label: '仅审批', match: (n) => n.kind === 'approval' },
  { id: 'error', label: '仅错误', match: (n) => n.status === 'err' },
];

function tlFilterMatch(n) {
  const f = TL_FILTERS.find((x) => x.id === TL.filter) || TL_FILTERS[0];
  return f.match(n);
}

/* ----------------------------------------------------------------------
   渲染：主入口
   ---------------------------------------------------------------------- */
function tlRender() {
  const page = document.querySelector('.page[data-page="' + OBS.tabId + '"]');
  if (!page) return;
  const host = page.querySelector('#obs-view-timeline');
  if (!host) return;

  const nodes = tlBuildNodes();
  const shown = nodes.filter(tlFilterMatch);

  /* 空态：没有节点，或筛选后没有命中 */
  if (!shown.length) {
    host.innerHTML = nodes.length
      ? tlEmptyHtml('当前筛选下没有事件',
        '「' + ((TL_FILTERS.find((f) => f.id === TL.filter) || {}).label || '全部')
        + '」筛选没有匹配到任何节点。切回「全部」可看到本次运行的完整因果链。', 'activity')
      : tlEmptyHtml('本次运行尚无事件',
        'Live Timeline 会把一次 Agent 执行拆成一条垂直因果链：'
        + '用户提问 → 思考 → 工具调用 → 审批 → 最终回答。'
        + '发一条消息（例如「读一下 agent_platform/core/paths.py 的前 50 行并总结它的作用」），'
        + '这里会实时长出节点。', 'activity');
    return;
  }

  /* ---- 本轮最长一步（耗时比例条的基准）---- */
  let maxDur = 0;
  nodes.forEach((n) => {
    const d = Number(n.meta && n.meta.duration_ms);
    if (Number.isFinite(d) && d > maxDur) maxDur = d;
  });

  const body = shown.map((n) => tlNodeHtml(n, maxDur)).join('');
  const legend = '<div class="tl-legend">'
    + '<span class="tl-legend-item"><span class="tl-wf-legend-dot" '
    + 'style="background:var(--accent-2);height:4px"></span>模型调用</span>'
    + '<span class="tl-legend-item"><span class="tl-wf-legend-dot" '
    + 'style="background:var(--ok);height:4px"></span>工具调用</span>'
    + '<span class="tl-legend-item"><span class="tl-wf-legend-dot" '
    + 'style="background:var(--hitl);height:4px"></span>审批等待（菱形，零时长）</span>'
    + '<span class="tl-legend-note">耗时条宽度正比于该步耗时（最长一步 = 满宽）</span>'
    + '</div>';
  host.innerHTML = '<div class="tl-root">' + tlHeaderHtml(nodes) + body
    + legend + tlLiveHtml() + '</div>';
}

/* ----------------------------------------------------------------------
   渲染：sticky run header
   ---------------------------------------------------------------------- */
function tlHeaderHtml(nodes) {
  const win = (typeof OBS !== 'undefined' && OBS.contextWindow) || 32768;
  const trace = (typeof OBS !== 'undefined' && OBS.trace) ? OBS.trace : [];

  /* ---- 三段时长：wall / compute / wait ---- */
  let compute = 0, wait = 0;
  trace.forEach((e) => {
    const d = Number(e.duration_ms) || 0;
    if (e.kind === 'approval') wait += d;
    else compute += d;
  });
  const wall = tlWallMs(nodes);

  /* ---- token ---- */
  let tokens = 0;
  trace.forEach((e) => {
    const t = Number(e.tokens);
    if (Number.isFinite(t) && t > tokens) tokens = t;
  });

  /* ---- 上下文占用：百分比 + hover 明细 ---- */
  const ctx = tlContextUsage(trace, win);

  const model = (typeof state !== 'undefined' && state.model) || 'gpt-oss-20b';
  const runs = trace.length;

  return '<header class="tl-header">'
    + '<span class="tl-header-model mono">' + esc(model) + '</span>'
    + '<span class="tl-header-sep">·</span>'
    + '<span>wall <b class="mono">' + esc(obsFmtDur(wall)) + '</b></span>'
    + '<span>compute <b class="mono">' + esc(obsFmtDur(compute)) + '</b></span>'
    + '<span>wait <b class="mono">' + esc(obsFmtDur(wait)) + '</b></span>'
    + '<span class="tl-header-sep">·</span>'
    + '<span>' + runs + ' 步</span>'
    + '<span>' + (tokens ? tokens + ' tok' : 'tok 未测量') + '</span>'
    + '<span class="grow" style="flex:1 1 auto"></span>'
    + tlCtxHtml(ctx, win)
    + '</header>';
}

/** 本轮 wall time（毫秒）：从最早事件到最后事件 */
function tlWallMs(nodes) {
  if (!nodes.length) return 0;
  let t0 = Infinity, t1 = -Infinity;
  nodes.forEach((n) => {
    if (!n.ts) return;
    t0 = Math.min(t0, n.ts);
    t1 = Math.max(t1, n.ts);
  });
  if (!Number.isFinite(t0) || !Number.isFinite(t1)) return 0;
  /* 加上各节点的自身耗时（结束时刻可能晚于最后一个 started_at） */
  let extra = 0;
  nodes.forEach((n) => {
    const e = n.meta;
    if (e && e.ended_at && e.started_at) {
      extra = Math.max(extra, (Number(e.ended_at) - Number(e.started_at)) * 1000);
    }
  });
  return Math.max(0, t1 - t0) + extra;
}

/** 上下文占用估算（与 Context Map 同一口径：字符数 → 粗估 token） */
function tlContextUsage(trace, win) {
  let chars = 0;
  if (typeof OBS !== 'undefined') chars += Number(OBS.systemPromptChars || 0);
  trace.forEach((e) => {
    chars += obsCharLen(e.result) + obsCharLen(e.args)
      + obsCharLen(e.reasoning_summary) + obsCharLen(e.action_summary);
  });
  if (typeof OBS !== 'undefined') {
    (OBS.edits || []).forEach((ed) => { chars += obsCharLen(ed.diff); });
  }
  /* 字符 → token：中文 1 字 1 token、ASCII 4 字符 1 token 的混合折算（粗估） */
  const tokens = Math.round(chars * 0.6);
  const pct = win > 0 ? Math.min(100, (tokens / win) * 100) : 0;
  return { tokens: tokens, chars: chars, pct: pct };
}

/** 上下文占用控件（百分比 + hover 明细） */
function tlCtxHtml(ctx, win) {
  const cls = ctx.pct >= 95 ? ' is-full' : (ctx.pct >= 80 ? ' is-high' : '');
  const pctTxt = ctx.tokens ? ctx.pct.toFixed(1) + '%' : '未测量';
  const warn = ctx.pct >= 95
    ? '已接近上下文窗口上限，继续追问可能触发截断。'
    : (ctx.pct >= 80 ? '上下文占用偏高，建议尽快总结本轮结论。' : '上下文占用正常。');
  return '<span class="tl-ctx' + cls + '" tabindex="0"'
    + ' aria-label="上下文占用 ' + pctTxt + '，悬停查看明细">'
    + '<span class="tl-ctx-bar"><span class="tl-ctx-fill" style="width:'
    + ctx.pct.toFixed(2) + '%"></span></span>'
    + '<span class="tl-ctx-num">ctx ' + esc(pctTxt) + '</span>'
    + '<span class="tl-ctx-detail">'
    + '<span class="tl-ctx-row"><span>已用</span><b>' + ctx.tokens + ' tok</b></span>'
    + '<span class="tl-ctx-row"><span>窗口上限</span><b>' + win + ' tok</b></span>'
    + '<span class="tl-ctx-row"><span>原始体积</span><b>' + ctx.chars + ' 字符</b></span>'
    + '<span class="tl-ctx-row"><span>口径</span><b>粗估</b></span>'
    + '<span class="tl-note">' + esc(warn) + '</span>'
    + '</span></span>';
}

/* ----------------------------------------------------------------------
   渲染：单个节点
   ----------------------------------------------------------------------
   ``maxDur`` = 本轮最长一步的耗时（毫秒），用于把每个节点的
   **耗时条宽度**按比例映射 —— 宽度正比于耗时（横向时间轴比例语义
   在垂直时间线上的等价物）。
   ---------------------------------------------------------------------- */
function tlNodeHtml(n, maxDur) {
  const id = n.id || ('tl-' + (TL.seq++));
  const open = TL.open.has(id) || n.defaultOpen;
  const time = n.ts ? tlFmtClockMs(n.ts) : '';
  const dur = n.meta ? Number(n.meta.duration_ms) : NaN;
  const hasDur = Number.isFinite(dur) && dur >= 0;
  const tok = n.meta && n.meta.tokens != null ? Number(n.meta.tokens) : null;

  /* ---- 耗时比例条（宽度正比于耗时）---- */
  let durBar = '';
  if (hasDur && maxDur > 0 && n.kind !== 'approval') {
    const pct = Math.max(0, Math.min(100, (dur / maxDur) * 100));
    durBar = '<span class="tl-durbar" aria-hidden="true"'
      + ' title="耗时 ' + esc(obsFmtDur(dur)) + '（占最长一步 '
      + pct.toFixed(1) + '%）">'
      + '<span class="tl-durbar-fill" style="width:' + pct.toFixed(2) + '%"></span>'
      + '</span>';
  }

  /* ---- 折叠头 ---- */
  const head = '<header class="tl-node-head" role="button" tabindex="0"'
    + ' aria-expanded="' + (open ? 'true' : 'false') + '"'
    + ' aria-controls="' + esc(id) + '-panel"'
    + ' aria-label="' + esc(tlNodeAria(n)) + '">'
    + '<span class="tl-dot" aria-hidden="true"></span>'
    + (time ? '<time class="tl-time">' + esc(time) + '</time>' : '')
    + '<span class="tl-kind">' + esc(tlKindLabel(n)) + '</span>'
    + '<span class="tl-title">' + esc(n.title) + '</span>'
    + (n.subtitle ? '<span class="tl-title" style="flex:0 1 auto;color:var(--fg-faint)">'
      + esc(n.subtitle) + '</span>' : '')
    + tlHeadExtra(n)
    + durBar
    + (tok != null && Number.isFinite(tok) ? '<span class="tl-tok">' + tok + ' tok</span>' : '')
    + '<span class="tl-badge s-' + esc(n.status) + '">' + esc(tlStatusLabel(n)) + '</span>'
    + (hasDur ? '<span class="tl-dur">' + esc(obsFmtDur(dur)) + '</span>'
      : (n.kind === 'approval' ? '<span class="tl-dur">–</span>' : ''))
    + '<span class="tl-caret" aria-hidden="true">▶</span>'
    + '</header>';

  /* ---- 展开区 ---- */
  let panel = '';
  if (n.kind === 'user') {
    panel = '<div class="tl-bubble">' + esc(n.title) + '</div>';
  } else if (n.kind === 'assistant') {
    panel = '<div class="tl-answer">' + esc(n.title) + '</div>';
  } else if (n.kind === 'approval') {
    panel = tlApprovalPanel(n);
  } else if (n.kind === 'thinking') {
    panel = tlThinkingPanel(n);
  } else if (n.kind === 'error') {
    panel = tlErrorPanel(n);
  } else {
    panel = tlToolPanel(n);
  }

  return '<article class="tl-node" data-kind="' + esc(n.kind) + '"'
    + ' data-status="' + esc(n.status) + '"'
    + ' data-node-id="' + esc(id) + '"'
    + ' data-collapsible="' + (n.collapsible ? 'true' : 'false') + '"'
    + ' data-open="' + (open ? 'true' : 'false') + '"'
    + ' data-ts="' + (n.ts || 0) + '">'
    + head
    + '<div class="tl-panel" id="' + esc(id) + '-panel" role="region"'
    + ' aria-label="' + esc(tlKindLabel(n)) + ' 详情">'
    + '<div class="tl-panel-inner">' + panel + '</div>'
    + '</div>'
    + '</article>';
}

/** 节点类型的中文标签 */
function tlKindLabel(n) {
  switch (n.kind) {
    case 'user': return '用户提问';
    case 'thinking': return 'Agent 思考';
    case 'tool': return '工具调用';
    case 'tool_result': return '工具结果';
    /* 审批节点的类别名要**跟着实际状态走**（2026-11-24，审批统一入口）：
       之前这里硬编码「待审批」，于是对话页批完之后、Live Timeline 的
       徽章明明写着「已批准」，类别名却还写着「待审批」—— 用户一眼看到
       就以为还没批成功。这就是「面板说没批、对话说批了」的观感来源之一。 */
    case 'approval': {
      const ap = tlApprovalOf(n.meta) || {};
      if (ap.resolved === 'approved') return '已批准';
      if (ap.resolved === 'rejected') return '已拒绝';
      return '待审批';
    }
    case 'edit': return '文件编辑';
    case 'error': return '错误';
    case 'assistant': return '最终回答';
    case 'checkpoint': return '检查点';
    default: return '步骤';
  }
}

/** 状态徽章文案 */
function tlStatusLabel(n) {
  switch (n.status) {
    case 'ok': return '成功';
    case 'run': return '执行中';
    case 'err': return '失败';
    case 'wait': return '等待批准';
    case 'skip': return '已跳过';
    case 'pending': return '待执行';
    default: return '未知';
  }
}

/** aria-label：概括节点语义 */
function tlNodeAria(n) {
  const dur = n.meta ? Number(n.meta.duration_ms) : NaN;
  const d = Number.isFinite(dur) ? '，耗时 ' + obsFmtDur(dur) : '';
  return tlKindLabel(n) + '：' + n.title + '，状态 ' + tlStatusLabel(n) + d;
}

/** 折叠头里的额外元素（思考节点放信号条；审批节点放快捷按钮） */
function tlHeadExtra(n) {
  if (n.kind === 'thinking') {
    return '<span class="tl-sig" aria-hidden="true">'
      + tlSigBar(n) + '</span>';
  }
  if (n.kind === 'approval') {
    const ap = tlApprovalOf(n.meta) || {};
    if (ap.resolved) {
      return '<span class="tl-head-actions">'
        + '<span class="tl-badge s-' + (ap.resolved === 'approved' ? 'ok' : 'err') + '">'
        + (ap.resolved === 'approved' ? '已批准' : '已拒绝') + '</span></span>';
    }
    /* 折叠态直接挂批准 / 拒绝（Cursor 做法 —— 不展开也能批）
     * ⚠️ 这两个按钮是「**定位器**」不是「决策器」：点击后只会把对话里的
     *    审批卡片跳到眼前，**不会**替用户决定。 */
    return '<span class="tl-head-actions">'
      + '<button type="button" class="tl-head-btn ok" data-tl-approve="1"'
      + ' data-cid="' + esc(n.meta && n.meta.id ? n.meta.id : '') + '"'
      + ' title="' + esc(TL_LOCATOR_HINT) + '"'
      + ' aria-label="' + esc(TL_LOCATOR_HINT) + '">批准</button>'
      + '<button type="button" class="tl-head-btn no" data-tl-deny="1"'
      + ' data-cid="' + esc(n.meta && n.meta.id ? n.meta.id : '') + '"'
      + ' title="' + esc(TL_LOCATOR_HINT) + '"'
      + ' aria-label="' + esc(TL_LOCATOR_HINT) + '">拒绝</button>'
      + '</span>';
  }
  return '';
}

/** 思考节点的 3 级信号条（复用 .obs-sig 的高度编码：微抬 / 拉高 / 满高） */
function tlSigBar(n) {
  const e = n.meta || {};
  const dur = Number(e.duration_ms) || 0;
  const lvl = dur >= 1000 ? 3 : (dur >= 200 ? 2 : 1);
  let out = '<span class="tl-sig-bar l1"></span>';
  if (lvl >= 2) out += '<span class="tl-sig-bar l2"></span>';
  if (lvl >= 3) out += '<span class="tl-sig-bar l3"></span>';
  return out;
}

/* ----------------------------------------------------------------------
   渲染：工具调用展开区
   ---------------------------------------------------------------------- */
function tlToolPanel(n) {
  const e = n.meta || {};
  const cid = esc(n.id);
  let out = '';

  /* ---- 参数区 ---- */
  out += '<div class="tl-zone" data-zone="args">'
    + '<div class="tl-zone-head"><span class="tl-zone-title">参数</span>'
    + '<span class="grow"></span>'
    + '<button type="button" class="tl-copy" data-tl-copy="1"'
    + ' data-target="' + cid + '-args" aria-label="复制参数 JSON">复制</button>'
    + '</div>'
    + '<pre class="tl-code" id="' + cid + '-args">' + tvJsonHtml(e.args || {}) + '</pre>'
    + '</div>';

  /* ---- 结果区（必须截断）---- */
  if (e.result !== undefined && e.result !== null) {
    const big = tlIsBigResult(e.result);
    out += '<div class="tl-zone" data-zone="result">'
      + '<div class="tl-zone-head"><span class="tl-zone-title">结果</span>'
      + '<span class="grow"></span>'
      + '<button type="button" class="tl-copy" data-tl-copy="1"'
      + ' data-target="' + cid + '-res" aria-label="复制结果 JSON">复制</button>'
      + '</div>'
      + '<pre class="tl-code' + (big ? ' is-clipped' : '') + '" id="' + cid + '-res">'
      + tvJsonHtml(e.result) + '</pre>'
      + (big ? '<button type="button" class="tl-more" data-tl-more="1"'
        + ' data-target="' + cid + '-res" aria-expanded="false">展开全部输出</button>' : '')
      + '</div>';
  } else {
    out += '<div class="tl-zone" data-zone="result">'
      + '<div class="tl-zone-head"><span class="tl-zone-title">结果</span></div>'
      + '<div class="tl-note">该调用尚未返回结果（仍在执行或已被中断）。</div>'
      + '</div>';
  }

  /* ---- 错误区 ---- */
  const err = e.error || {};
  if (err && Object.keys(err).length) {
    out += '<div class="tl-zone" data-zone="error">'
      + '<div class="tl-zone-head"><span class="tl-zone-title">错误</span></div>'
      + '<div class="tl-err">'
      + '<div><span class="tl-err-code">' + esc(err.code || 'ERROR') + '</span>'
      + '<span class="tl-err-msg">' + esc(err.message || '(无消息)') + '</span></div>'
      + (err.hint ? '<div class="tl-err-hint">' + esc(err.hint) + '</div>' : '')
      + '</div></div>';
  }

  return out;
}

/** 结果是否够大（需要截断 + 「展开全部输出」） */
function tlIsBigResult(v) {
  let s;
  try {
    s = typeof v === 'string' ? v : JSON.stringify(v, null, 2);
  } catch (e) {
    s = String(v);
  }
  if (!s) return false;
  return s.length > 400 || s.split('\n').length > 12;
}

/* ----------------------------------------------------------------------
   渲染：思考节点展开区（只显示摘要，绝不显示原始 reasoning_content）
   ---------------------------------------------------------------------- */
function tlThinkingPanel(n) {
  const e = n.meta || {};
  const sum = e.reasoning_summary || '';
  let out = '<div class="tl-zone" data-zone="reasoning">'
    + '<div class="tl-zone-head"><span class="tl-zone-title">推理摘要</span></div>';
  if (sum) {
    out += '<div class="tl-text">' + esc(sum) + '</div>';
  } else {
    out += '<div class="tl-note">本轮模型未返回可展示的推理摘要。'
      + '这是正常现象 —— 摘要缺失时不伪造任何内容。</div>';
  }
  out += '<div class="tl-note">以下为**模型推理摘要（已截断）**，不是原始思维链；'
    + '后端不把 <span class="mono">reasoning_content</span> 写入历史或 SSE。</div>';

  if (e.action_summary) {
    out += '<div class="tl-zone" data-zone="action">'
      + '<div class="tl-zone-head"><span class="tl-zone-title">动作摘要</span></div>'
      + '<div class="tl-text">' + esc(e.action_summary) + '</div></div>';
  }
  return out;
}

/* ----------------------------------------------------------------------
   渲染：错误节点展开区
   ---------------------------------------------------------------------- */
function tlErrorPanel(n) {
  const e = n.meta || {};
  const err = e.error || {};
  let out = '<div class="tl-zone" data-zone="error">'
    + '<div class="tl-zone-head"><span class="tl-zone-title">错误详情</span></div>'
    + '<div class="tl-err">'
    + '<div><span class="tl-err-code">' + esc(err.code || 'ERROR') + '</span>'
    + '<span class="tl-err-msg">' + esc(err.message || '(无消息)') + '</span></div>'
    + (err.hint ? '<div class="tl-err-hint">' + esc(err.hint) + '</div>' : '')
    + '</div></div>';
  if (e.args && Object.keys(e.args).length) {
    out += '<div class="tl-zone" data-zone="args">'
      + '<div class="tl-zone-head"><span class="tl-zone-title">参数</span></div>'
      + '<pre class="tl-code">' + tvJsonHtml(e.args) + '</pre></div>';
  }
  return out;
}

/* ----------------------------------------------------------------------
   渲染：审批节点展开区（diff 预览 + 目标参数 + 拒绝理由输入框）
   ---------------------------------------------------------------------- */
function tlApprovalPanel(n) {
  const e = n.meta || {};
  const ap = tlApprovalOf(e) || {};
  const cid = esc(e.id || '');
  const risk = obsRiskOf(ap);
  const targets = obsApprovalTargets(ap);

  let out = '<div class="appr" data-approval="1" data-cid="' + cid + '">'
    + '<div class="appr-cmd">' + esc(e.name || ap.tool_name || '(工具)') + '</div>'
    + '<div class="appr-kv"><span>权限</span><b>' + esc(ap.permission || '–') + '</b></div>'
    + '<div class="appr-kv"><span>影响</span><b>' + esc(ap.impact || '–') + '</b></div>'
    + '<div class="appr-kv"><span>可回滚</span><b>'
    + (ap.reversible === false ? '否（不可逆）' : '是') + '</b></div>'
    + '<div class="appr-kv"><span>目标</span><b>'
    + esc(targets.length ? targets.join(' · ') : '（未声明具体目标）') + '</b></div>'
    + '<div class="appr-kv"><span>风险</span><b>'
    + '<span class="tl-risk ' + esc(risk.level) + '">' + esc(risk.label) + '</span></b></div>';

  /* ---- diff 预览 ---- */
  const diff = (e.args && e.args.diff) || ap.diff;
  if (diff && typeof wsDiffHtml === 'function') {
    out += '<div class="tl-zone" data-zone="diff">'
      + '<div class="tl-zone-head"><span class="tl-zone-title">改动预览</span></div>'
      + wsDiffHtml(diff) + '</div>';
  }

  /* ---- 参数 ---- */
  out += '<div class="tl-zone" data-zone="args">'
    + '<div class="tl-zone-head"><span class="tl-zone-title">参数</span></div>'
    + '<pre class="tl-code">' + tvJsonHtml(e.args || ap.args || {}) + '</pre></div>';

  /* ---- 操作行（已解决则只显示结果）---- */
  if (ap.resolved) {
    out += '<div class="appr-actions">'
      + '<span class="tl-badge s-' + (ap.resolved === 'approved' ? 'ok' : 'err') + '">'
      + (ap.resolved === 'approved' ? '已批准' : '已拒绝') + '</span>'
      + '<span class="tl-note">' + esc(ap.resolved_summary || '') + '</span>'
      + '</div>';
  } else {
    /* ⚠️ 这两个按钮是「**定位器**」：点了只会把对话里的审批卡片跳到眼前，
     *    真正点「批准 / 拒绝」的动作发生在对话卡片上（唯一 POST 路径）。 */
    out += '<div class="appr-actions">'
      + '<button type="button" class="appr-btn primary" data-tl-approve="1"'
      + ' data-cid="' + cid + '" title="' + esc(TL_LOCATOR_HINT) + '"'
      + ' aria-label="' + esc(TL_LOCATOR_HINT) + '">批准</button>'
      + '<button type="button" class="appr-btn danger" data-tl-deny="1"'
      + ' data-cid="' + cid + '" title="' + esc(TL_LOCATOR_HINT) + '"'
      + ' aria-label="' + esc(TL_LOCATOR_HINT) + '">拒绝</button>'
      + '<span class="tl-note">审批统一在对话里完成：点这里会跳到对话中的审批卡片。</span>'
      + '</div>';
  }
  out += '</div>';
  return out;
}

/* ----------------------------------------------------------------------
   渲染：运行中指示器（位于消息流末端）
   ---------------------------------------------------------------------- */
function tlLiveHtml() {
  if (!tlIsRunning()) return '';
  const label = tlLiveLabel();
  return '<div class="tl-live" role="status" aria-live="polite">'
    + '<span class="tl-dots" aria-hidden="true"><span></span><span></span><span></span></span>'
    + '<span>' + esc(label) + '</span></div>';
}

/** 本轮是否还在跑 */
function tlIsRunning() {
  if (typeof OBS === 'undefined') return false;
  return !!(OBS.runStart && !OBS.finished);
}

/** 运行中状态文案（要具体，来自事件本身 —— 不泛化成 "Working..."） */
function tlLiveLabel() {
  const trace = (typeof OBS !== 'undefined' && OBS.trace) ? OBS.trace : [];
  for (let i = trace.length - 1; i >= 0; i--) {
    const e = trace[i];
    if (!e) continue;
    if (e.kind === 'approval') return '等待你的审批：' + (e.name || '操作');
    if (e.kind === 'llm') return '正在思考…';
    if (e.name) return '正在执行：' + e.name;
  }
  return '正在处理…';
}

/* ----------------------------------------------------------------------
   空态
   ---------------------------------------------------------------------- */
function tlEmptyHtml(title, hint, iconName) {
  return '<div class="tl-empty" data-empty="1">'
    + '<span class="tl-empty-ico">' + obsIcon(iconName || 'activity', 22) + '</span>'
    + '<span class="tl-empty-title">' + esc(title) + '</span>'
    + (hint ? '<span class="tl-empty-hint">' + esc(hint) + '</span>' : '')
    + '</div>';
}

/* ----------------------------------------------------------------------
   时间格式化
   ---------------------------------------------------------------------- */
function tlFmtClockMs(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || !n) return '';
  const d = new Date(n);
  if (isNaN(d.getTime())) return '';
  const p = (x) => String(x).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

/* ======================================================================
   交互绑定
   ====================================================================== */

/** 绑定时间线交互（只绑一次） */
function tlBind(page) {
  if (!page || page.dataset.tlBound === '1') return;
  page.dataset.tlBound = '1';

  /* ---- 点击：折叠 / 复制 / 展开全部 / 审批 ---- */
  page.addEventListener('click', (e) => {
    /* ---- 审批按钮（优先级最高，避免被折叠头拦截）---- */
    const ap = e.target.closest && e.target.closest('[data-tl-approve],[data-tl-deny]');
    if (ap) {
      e.preventDefault();
      e.stopPropagation();
      const cid = ap.getAttribute('data-cid') || '';
      const approved = ap.hasAttribute('data-tl-approve');
      tlSubmitApproval(cid, approved, page);
      return;
    }

    /* ---- 复制 ---- */
    const cp = e.target.closest && e.target.closest('[data-tl-copy]');
    if (cp) {
      e.preventDefault();
      e.stopPropagation();
      const target = page.querySelector('#' + cssEscape(cp.getAttribute('data-target') || ''));
      if (!target) return;
      const old = cp.textContent;
      copyText(target.textContent || '').then(() => {
        cp.textContent = '已复制';
        cp.classList.add('done');
        setTimeout(() => { cp.classList.remove('done'); cp.textContent = old; }, 1400);
      });
      return;
    }

    /* ---- 「展开全部输出」---- */
    const more = e.target.closest && e.target.closest('[data-tl-more]');
    if (more) {
      e.preventDefault();
      e.stopPropagation();
      const box = page.querySelector('#' + cssEscape(more.getAttribute('data-target') || ''));
      if (!box) return;
      const clipped = box.classList.toggle('is-clipped');
      more.textContent = clipped ? '展开全部输出' : '收起输出';
      more.setAttribute('aria-expanded', clipped ? 'false' : 'true');
      return;
    }

    /* ---- 折叠头 ---- */
    const head = e.target.closest && e.target.closest('.tl-node-head');
    if (head) {
      const node = head.closest('.tl-node');
      if (!node) return;
      if (node.getAttribute('data-collapsible') === 'false') return;
      tlToggleNode(node);
      return;
    }
  });

  /* ---- 键盘：Enter / Space 切换折叠；A / D 审批 ---- */
  page.addEventListener('keydown', (e) => {
    const node = e.target.closest && e.target.closest('.tl-node');
    if (!node) return;

    /* A / D 快捷键：仅在审批节点且焦点在节点内时生效
     * ⚠️ 与按钮一样，这只是「**定位器**」：跳到对话里的审批卡片，
     *    真正决策仍在对话卡片上完成（唯一 POST 路径）。 */
    const key = (e.key || '').toLowerCase();
    if (key === 'a' || key === 'd') {
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea') return;
      if (node.getAttribute('data-kind') !== 'approval') return;
      if (node.getAttribute('data-status') !== 'wait') return;
      e.preventDefault();
      tlSubmitApproval(tlCidOf(node), key === 'a', page);
      return;
    }

    /* Enter / Space 切换折叠 */
    if (key === 'enter' || key === ' ' || e.key === 'Spacebar') {
      if (node.getAttribute('data-collapsible') === 'false') return;
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'button') return;
      if (!e.target.classList || !e.target.classList.contains('tl-node-head')) return;
      e.preventDefault();
      tlToggleNode(node);
    }
  });
}

/** 从节点取 tool_call_id */
function tlCidOf(node) {
  if (!node) return '';
  const btn = node.querySelector('[data-tl-approve],[data-tl-deny]');
  if (btn && btn.getAttribute('data-cid')) return btn.getAttribute('data-cid');
  const ap = node.querySelector('.appr[data-cid]');
  if (ap) return ap.getAttribute('data-cid') || '';
  return '';
}

/** 切换节点展开态（数据在 data-open，CSS 负责动效） */
function tlToggleNode(node) {
  const open = node.getAttribute('data-open') === 'true';
  const id = node.getAttribute('data-node-id');
  if (open) TL.open.delete(id);
  else TL.open.add(id);
  node.setAttribute('data-open', open ? 'false' : 'true');
  const head = node.querySelector('.tl-node-head');
  if (head) head.setAttribute('aria-expanded', open ? 'false' : 'true');
}

/* ----------------------------------------------------------------------
   审批定位（**不再**在这里 POST /approve）
   ----------------------------------------------------------------------
   为什么改（2026-11-24，审批统一入口）：

   真实故障：运行观测面板里点「批准」会**自己** POST /approve，
   但对话页那张卡片的状态**不会**跟着变 —— 卡片仍是琥珀色呼吸光的
   「待批准」，按钮仍可点。用户看到的现象是「面板能批、对话里批不掉」，
   于是干脆跑到面板去批；再叠加面板状态与对话状态互相矛盾，
   整个审批体验非常割裂。

   现在：**全站只有对话页的 HITL 卡片会 POST /approve**。
   面板上的按钮降级为「**定位器**」——
     · 把对话里那张审批卡片滚到视野中央并高亮；
     · 焦点落在「批准」按钮上，用户按回车即可；
     · **绝不**替用户做决定。
   这样既保留了「面板也能看到待批项并一键跳过去」的便利，
   又保证了审批状态只有一个来源、只有一条 POST 路径。 */
async function tlSubmitApproval(cid, approved, page) {
  if (!cid) return;
  const list = (typeof OBS !== 'undefined' && OBS.approvals) ? OBS.approvals : [];
  const ap = list.find((a) => a.tool_call_id === cid);
  if (!ap) return;

  /* 已决策 → 直接把面板状态翻过去（万一面板错过了一次广播）*/
  if (ap.resolved) { tlRender(); return; }

  /* 事实来源说已决策（对话页刚批过）→ 同步面板，不重复决策 */
  if (typeof APPROVAL !== 'undefined' && APPROVAL.isResolved(cid)) {
    ap.resolved = APPROVAL.getResolved(cid);
    ap.resolved_summary = (typeof APPROVAL.summaries !== 'undefined'
      && APPROVAL.summaries[cid]) || '';
    tlRender();
    return;
  }

  /* ---- 定位到对话里的审批卡片 ---- */
  const found = (typeof APPROVAL !== 'undefined')
    ? APPROVAL.focusChatCard(cid, { noFocus: false })
    : false;

  if (!found) {
    /* 对话卡片不存在（对话页被关闭 / 本次运行来自别的入口）。
       此时给出明确提示，而不是让用户在面板里「盲批」——
       盲批会重新制造出两套状态。 */
    tlShowTip(page, '这张审批需要在「对话」里完成：'
      + '当前对话页没有对应的审批卡片，请切回对话页查看。', true);
    return;
  }

  /* 面板侧只做「提示 + 高亮」，不改变审批状态 */
  tlShowTip(page, '已跳转到对话中的审批卡片，请在那里点击「'
    + (approved ? '批准' : '拒绝') + '」。', false);
}

/** 在时间线底部显示一条提示 */
function tlShowTip(page, text, isErr) {
  if (!page) return;
  const host = page.querySelector('#obs-view-timeline');
  if (!host) return;
  let tip = host.querySelector('[data-tl-tip]');
  if (!tip) {
    tip = document.createElement('div');
    tip.setAttribute('data-tl-tip', '1');
    tip.className = 'tl-note';
    tip.style.padding = 'var(--sp-2) var(--sp-4)';
    host.appendChild(tip);
  }
  tip.textContent = text;
  tip.style.color = isErr ? 'var(--err)' : 'var(--fg-dim)';
  setTimeout(() => { if (tip && tip.parentNode) tip.parentNode.removeChild(tip); }, 5200);
}

/* ======================================================================
   与观测面板的集成
   ====================================================================== */

/**
 * 渲染 Live Timeline（或其它视图）。
 *
 * **兼容要求**：11 个原观测视图全部保留 —— 通过「耗时视图」与「全部视图」
 * 两个切换按钮可达，功能一项没删。
 */
function tlRenderPanel(page) {
  if (!page) return;
  const host = page.querySelector('#obs-view-timeline');
  if (!host) return;
  if (TL.view === 'timeline') {
    tlRender();
  } else if (TL.view === 'waterfall') {
    /* 耗时视图：复用原 Trace Waterfall 渲染器（功能保留） */
    host.innerHTML = '<div class="tl-viewport is-active">' + obsRenderWaterfall() + '</div>';
  } else {
    /* 全部视图：复用原分段控件 + 11 个视图 */
    host.innerHTML = '<div class="tl-viewport is-active">' + obsGalleryHtml(page) + '</div>';
    /* 立即渲染当前子视图 */
    tlRenderGallerySub(page);
  }
  tlBind(page);
}

/** 「全部视图」内部的子视图渲染 */
function tlRenderGallerySub(page) {
  const host = page.querySelector('#obs-gallery-body #obs-view-' + OBS.view)
    || page.querySelector('#obs-view-' + OBS.view);
  if (!host) return;
  const renderers = {
    waterfall: obsRenderWaterfall,
    toolcards: obsRenderToolCards,
    sequence: obsRenderSequence,
    cost: obsRenderCost,
    context: obsRenderContext,
    approval: obsRenderApproval,
    difftimeline: obsRenderDiffTimeline,
    timing: obsRenderTiming,
    autonomy: obsRenderAutonomy,
    repomap: obsRenderRepoMap,
    reasoning: obsRenderReasoning,
  };
  const fn = renderers[OBS.view];
  host.innerHTML = fn ? fn() : '';
  obsBindView(page, OBS.view);
  if (OBS.view === 'repomap' && !OBS.repoLoaded) obsLoadRepoMap(page);
}

/* ----------------------------------------------------------------------
   筛选 / 视图切换
   ---------------------------------------------------------------------- */
function tlSwitchFilter(filterId) {
  TL.filter = filterId;
  const page = document.querySelector('.page[data-page="' + OBS.tabId + '"]');
  if (!page) return;
  page.querySelectorAll('.tl-filter').forEach((b) => {
    b.setAttribute('aria-pressed', b.getAttribute('data-tl-filter') === filterId ? 'true' : 'false');
  });
  tlRenderPanel(page);
}

function tlSwitchView(viewId) {
  TL.view = viewId;
  const page = document.querySelector('.page[data-page="' + OBS.tabId + '"]');
  if (!page) return;
  page.querySelectorAll('.tl-view-btn').forEach((b) => {
    b.setAttribute('aria-pressed', b.getAttribute('data-tl-view') === viewId ? 'true' : 'false');
  });
  tlRenderPanel(page);
}

/**
 * 在 Live Timeline 里定位并展开某个 tool_call_id 对应的节点。
 * 返回 true 表示定位成功（调用方无需再兜底）。
 */
function tlFocusNode(cid) {
  if (!cid) return false;
  const page = document.querySelector('.page[data-page="' + OBS.tabId + '"]');
  if (!page) return false;
  /* 切回时间线视图 */
  if (TL.view !== 'timeline') {
    TL.view = 'timeline';
    page.querySelectorAll('.tl-view-btn').forEach((b) => {
      b.setAttribute('aria-pressed', b.getAttribute('data-tl-view') === 'timeline' ? 'true' : 'false');
    });
    tlRender();
  }
  /* 节点 id 由 tool_call_id 派生；先按 data-node-id 找 */
  let node = page.querySelector('.tl-node[data-node-id="' + cssEscape('tl-' + String(cid).replace(/[^\w-]/g, '_')) + '"]');
  if (!node) {
    /* 兜底：按 meta 里的 tool_call_id 文本匹配 */
    const all = Array.from(page.querySelectorAll('.tl-node[data-kind="tool"]'));
    node = all.find((n) => {
      const m = n.__tlMeta;
      return m && String(m.id) === String(cid);
    }) || null;
  }
  if (!node) {
    /* 目标被当前筛选过滤掉了 → 切到「全部」再试 */
    if (TL.filter !== 'all') {
      TL.filter = 'all';
      page.querySelectorAll('.tl-filter[data-tl-filter]').forEach((b) => {
        b.setAttribute('aria-pressed', b.getAttribute('data-tl-filter') === 'all' ? 'true' : 'false');
      });
      tlRender();
      return tlFocusNode(cid);
    }
    return false;
  }
  /* 展开并滚动定位 */
  const id = node.getAttribute('data-node-id');
  TL.open.add(id);
  node.setAttribute('data-open', 'true');
  const head = node.querySelector('.tl-node-head');
  if (head) head.setAttribute('aria-expanded', 'true');
  node.scrollIntoView({ block: 'center' });
  node.classList.add('tl-enter');
  setTimeout(() => { node.classList.remove('tl-enter'); }, 400);
  return true;
}

/* ----------------------------------------------------------------------
   订阅审批决策广播（2026-11-24，审批统一入口）
   ----------------------------------------------------------------------
   对话页批完 → 这里立刻把「待审批」翻成「已批准 / 已拒绝」并重绘。
   此前 Live Timeline 只在 SSE 有新事件时重绘，而 SSE 只在 /chat 期间
   开着 —— 审批走的是 /approve（另一个 HTTP 请求），面板因此永远停在
   审批前那一帧（琥珀色「待审批」），用户以为没批成功。 */
if (typeof APPROVAL !== 'undefined' && typeof APPROVAL.onResolved === 'function') {
  APPROVAL.onResolved((detail) => {
    const cid = detail && detail.tool_call_id;
    if (!cid) return;
    const list = (typeof OBS !== 'undefined' && OBS.approvals) ? OBS.approvals : [];
    let changed = false;
    list.forEach((ap) => {
      if (!ap || ap.tool_call_id !== cid) return;
      const next = (detail.approved ? 'approved' : 'rejected');
      if (ap.resolved !== next) changed = true;
      ap.resolved = next;
      ap.resolved_summary = (detail.summary || '') || ap.resolved_summary || '';
    });
    if (changed) tlRender();
  });
}

/* ----------------------------------------------------------------------
   用 /approve 的返回值刷新面板（2026-11-24，审批统一入口）
   ----------------------------------------------------------------------
   ``/approve`` 现在会返回 ``trace``（重建的决策轨迹）+ ``budget``，
   以及 ``approvals``（续跑后新的待审批项，若有）。
   SSE 在审批期间是关着的，所以这里必须**主动**把新快照灌进 OBS，
   否则面板会一直显示审批前那一帧。 */
function tlApplyApproveResult(res) {
  if (!res) return;
  if (typeof OBS === 'undefined') return;
  /* 1) 新轨迹 → 覆盖本轮快照 */
  if (res.trace && res.trace.length) {
    OBS.trace = res.trace;
  }
  /* 2) 预算快照（轮次 / token）*/
  if (res.budget) {
    OBS.budget = res.budget;
    if (res.budget.context_window) OBS.contextWindow = res.budget.context_window;
    if (typeof res.budget.system_prompt_chars === 'number') {
      OBS.systemPromptChars = res.budget.system_prompt_chars;
    }
  }
  /* 3) 本轮已结束 */
  OBS.finished = true;
  if (res.answer) OBS.answer = String(res.answer);
  /* 4) 审批项：把已决策的标成 resolved；续跑若又挂起新的审批项则追加 */
  const resolvedCid = (res && res.tool_call_id) || '';
  (OBS.approvals || []).forEach((ap) => {
    if (ap && ap.tool_call_id === resolvedCid && !ap.resolved) {
      ap.resolved = 'approved';
    }
  });
  tlRender();
  /* 5) 观测面板（审批卡片视图等）也要刷新 */
  if (typeof obsRender === 'function') obsRender();
}
