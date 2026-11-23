/* ==========================================================================
   运行观测（Observability）—— 原生 JS，零第三方库、零 CDN、零 emoji
   --------------------------------------------------------------------------
   本文件实现 12 个观测组件，全部**复用现有 tab 系统**
   （openTab / activateTab / closeTab），不另起一套导航。

   组件清单（分段控件切换）：
       P0-1  obs-waterfall   Trace Waterfall（时间瀑布，按真实时间比例定位）
       P0-2  obs-toolcards    Tool Call Card（工具调用卡，可展开 / 分组 / 重试）
       P0-3  obs-sequence     Agent Sequence（时序图，生命线 + 方向箭头）
       P1-1  obs-cost         Cost Waterfall（成本瀑布；本机推理 → 诚实降级）
       P1-2  obs-context      Context Map（上下文占用 treemap）
       P1-3  obs-approval     Approval Card 增强（风险徽标 / 倒计时 / 快捷键）
       P1-4  obs-difftimeline Diff Timeline（改动时间线 + 每文件进度条）
       P2-1  obs-timing       Agent Timing（时间信号线，三态高度编码）
       P2-2  obs-repomap      Repo Map（代码库 treemap + Agent 光标）
       P2-3  obs-autonomy     Autonomy Dial（三档自主性）
       P2-4  obs-reasoning    Reasoning Summary（推理摘要卡）

   数据来源优先级：
       1. SSE `trace` 事件（权威累积快照）→ TV_TRACE
       2. SSE `step` 事件（流式）
       3. SSE `result` 事件（最终答案 + 预算）
   ========================================================================== */
'use strict';

/* ----------------------------------------------------------------------
   全局状态
   ---------------------------------------------------------------------- */
const OBS = {
  tabId: 'pg-obs',            // 观测面板的 tab id（沿用现有 tab 系统）
  view: 'waterfall',          // 「全部视图」内的当前子视图
  trace: [],                  // 当前会话轨迹快照（与 TV_TRACE 同步）
  budget: null,               // 预算快照
  contextWindow: 32768,       // 上下文窗口上限（后端 trace 事件会覆盖）
  systemPromptChars: 0,       // system prompt 的真实字符数（后端下发；0 = 未测量）
  approvals: [],              // 本次运行的审批项（来自 SSE approval 事件）
  edits: [],                  // 改动记录（来自 HITL diff / 工作区）
  startedAt: null,            // 本次运行开始时刻（Unix 秒）
  threadId: '',               // 当前会话 id（/approve 端点需要）
  timer: null,                // 实时计时器句柄（运行中 tick）
  runStart: null,             // 当前运行的起点（用于耗时实时刷新）
  repoFiles: [],              // 代码库文件（Repo Map 用）
  repoCursor: null,           // Agent 光标位置
  repoLoaded: false,          // 代码库清单是否已拉取
  autonomy: 'agent',          // 三档自主性
  prompt: '',                 // 本轮用户提问（Live Timeline 的起点节点）
  answer: '',                 // 本轮最终回答（Live Timeline 的终点节点）
  finished: false,            // 本轮是否已结束（控制末端运行指示器）
  suppressRestore: false,     // 新建会话后抑制「恢复上一轮轨迹」
};

/* ----------------------------------------------------------------------
   定位器提示文案（2026-11-24，审批统一入口）
   ----------------------------------------------------------------------
   运行观测面板的审批按钮**不再自己决定审批**，只是「跳到对话里的
   审批卡片」的定位器。这一行文案必须写清楚，否则用户会以为在面板里
   点了就算批了 —— 那正是造成状态分叉、让用户「以为对话里批不了」的根因。
   （approval.js 未加载时用本地兜底，避免整页报错。） */
const OBS_LOCATOR_HINT = (typeof APPROVAL !== 'undefined' && APPROVAL.LOCATOR_HINT)
  ? APPROVAL.LOCATOR_HINT
  : '跳转到对话中的审批卡片（审批统一在对话里完成）';

/* ----------------------------------------------------------------------
   图标（内联 SVG，Lucide 风格 16×16 viewBox，线宽 1.5，round cap/join）
   ---------------------------------------------------------------------- */
const OBS_ICONS = {
  'activity': '<path d="M2.6 8h2.8l1.6-3.6L9.4 12l1.6-4h2.4"/>',
  'tool': '<path d="M14.1 6.3a3.8 3.8 0 0 1-4.8 4.8l-4.6 4.6a1.9 1.9 0 0 1-2.7-2.7l4.6-4.6a3.8 3.8 0 0 1 4.8-4.8L9.3 5.7l1.1 1.1z"/>',
  'route': '<circle cx="4" cy="4" r="1.8"/><circle cx="12" cy="12" r="1.8"/>'
         + '<path d="M5.6 5.1 8 7.5a2 2 0 0 1 .5 1.3v1.7"/>'
         + '<path d="M10.4 10.9 8 8.5A2 2 0 0 1 7.5 7.2v-.7"/>',
  'chevron-right': '<path d="M6 3.6 10.4 8 6 12.4"/>',
  'chevron-down': '<path d="M3.6 6 8 10.4 12.4 6"/>',
  'check': '<path d="M3 8.4 6.4 12 13 4.6"/>',
  'cross': '<path d="M4 4l8 8M12 4l-8 8"/>',
  'clock': '<circle cx="8" cy="8" r="5.6"/><path d="M8 4.8v3.4l2.2 1.3"/>',
  'alert': '<path d="M8 2.2 14.4 13.4H1.6z"/><path d="M8 6.4v3.2M8 11.6h.01"/>',
  'shield': '<path d="M8 1.8 13 3.6v4.1c0 3-2.1 5.4-5 6.5-2.9-1.1-5-3.5-5-6.5V3.6z"/>',
  'brain': '<path d="M6.2 3.2a2 2 0 0 0-2 2 2 2 0 0 0-1 3.4 2 2 0 0 0 .6 3.7 2 2 0 0 0 2.4 2.7"/>'
       + '<path d="M9.8 3.2a2 2 0 0 1 2 2 2 2 0 0 1 1 3.4 2 2 0 0 1-.6 3.7 2 2 0 0 1-2.4 2.7"/>'
       + '<path d="M8 3.4v12.2"/>',
  'coins': '<ellipse cx="8" cy="4.4" rx="5" ry="2"/><path d="M3 4.4v3.4c0 1.1 2.2 2 5 2s5-.9 5-2V4.4"/>'
       + '<path d="M3 7.8v3.6c0 1.1 2.2 2 5 2s5-.9 5-2V7.8"/>',
  'layers': '<path d="M8 1.8 14.4 5 8 8.2 1.6 5z"/><path d="M1.6 8 8 11.2 14.4 8"/>'
       + '<path d="M1.6 11 8 14.2 14.4 11"/>',
  'list': '<path d="M5.4 4h7.2M5.4 8h7.2M5.4 12h7.2M2.8 4h.01M2.8 8h.01M2.8 12h.01"/>',
  'diff': '<path d="M4.4 2.6v6.8a2 2 0 0 0 2 2h2.2"/><path d="M4.4 2.6 2.8 4.2M4.4 2.6 6 4.2"/>'
       + '<path d="M8.6 11.4 6.4 13"/><path d="M10.4 2.6v2.2M9.3 3.7h2.2"/>',
  'pulse': '<path d="M2 8h2.6l1.6-4.4L8.6 12l1.8-6 1.4 2H14"/>',
  'map': '<path d="M2.6 4.2 6 2.6l4 1.6 3.4-1.6v8.2L10 12.4l-4-1.6-3.4 1.6z"/>'
       + '<path d="M6 2.6v8.2M10 4.2v8.2"/>',
  'dial': '<path d="M2.8 11.4a5.6 5.6 0 1 1 10.4 0"/><path d="M8 11.4 10.6 7"/>',
  'doc': '<path d="M9 1.8H4.6A1.6 1.6 0 0 0 3 3.4v9.2a1.6 1.6 0 0 0 1.6 1.6h6.8a1.6 1.6 0 0 0 1.6-1.6V5.6z"/>'
      + '<path d="M9 1.8v3.8h4"/>',
  'user': '<circle cx="8" cy="5.4" r="2.6"/><path d="M3.2 13.4a4.8 4.8 0 0 1 9.6 0"/>',
  'copy': '<rect x="5.5" y="5.5" width="8" height="8" rx="1.4"/>'
        + '<path d="M10.5 3.5v-.6a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h.6"/>',
  'refresh': '<path d="M13.3 8a5.3 5.3 0 1 1-1.6-3.8"/><path d="M13.4 2.6v2.6h-2.6"/>',
  'folder': '<path d="M2.4 12.6V4.4a1 1 0 0 1 1-1h2.3L7.2 4.6h5.4a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H3.4a1 1 0 0 1-1-1z"/>',
  'file': '<path d="M3.5 2.6h5.7L12 5.4v8a1.2 1.2 0 0 1-1.2 1.2H3.5a1.2 1.2 0 0 1-1.2-1.2V3.8a1.2 1.2 0 0 1 1.2-1.2z"/>'
       + '<path d="M9.1 2.7v2.8h2.8"/>',
};

function obsIcon(name, size) {
  const body = OBS_ICONS[name] || OBS_ICONS['tool'];
  const s = size || 13;
  return '<svg class="ico" viewBox="0 0 16 16" width="' + s + '" height="' + s + '"'
    + ' fill="none" stroke="currentColor" stroke-width="1.5"'
    + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + body + '</svg>';
}

/* ----------------------------------------------------------------------
   通用工具
   ---------------------------------------------------------------------- */

/** 空态渲染器（每个组件都必须有空态，不是白屏、不是 undefined） */
function obsEmpty(title, hint, iconName) {
  return '<div class="obs-empty" data-empty="1">'
    + '<span class="obs-empty-ico">' + obsIcon(iconName || 'activity', 22) + '</span>'
    + '<span class="obs-empty-title">' + esc(title) + '</span>'
    + (hint ? '<span class="obs-empty-hint">' + esc(hint) + '</span>' : '')
    + '</div>';
}

/** 毫秒 → 可读耗时 */
function obsFmtDur(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return '–';
  if (n < 1000) return Math.round(n) + ' ms';
  if (n < 60000) return (n / 1000).toFixed(2) + ' s';
  const m = Math.floor(n / 60000);
  return m + ' 分 ' + ((n % 60000) / 1000).toFixed(0) + ' s';
}

/** 秒 → 「已等待 42s」 */
function obsFmtWait(sec) {
  const n = Math.max(0, Math.floor(Number(sec) || 0));
  if (n < 60) return '已等待 ' + n + 's';
  return '已等待 ' + Math.floor(n / 60) + 'm' + (n % 60) + 's';
}

/** Unix 秒 → HH:MM:SS */
function obsFmtClock(ts) {
  if (!ts) return '–';
  const d = new Date(Number(ts) * 1000);
  if (isNaN(d.getTime())) return '–';
  const p = (n) => String(n).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

/** 估算 token（字符数 / 4，英文为主的粗略口径；中文会偏大） */
function obsEstTokens(chars) {
  const n = Number(chars) || 0;
  if (!n) return 0;
  // 中文字符按 1 token、ASCII 按 1/4 token 估算。
  // 注意：入参是**字符串**（要逐字符判定 CJK），不是字符数 ——
  //    早期版本误把字符数当字符串用（n.charCodeAt 报错）。
  const s = typeof chars === 'string' ? chars : '';
  if (!s) return 0;
  let cjk = 0;
  for (let i = 0; i < s.length; i++) {
    if (s.charCodeAt(i) > 0x2e80) cjk++;
  }
  return Math.round(cjk + (s.length - cjk) / 4);
}

/** 计算字符串长度（字符数） */
function obsCharLen(v) {
  if (v == null) return 0;
  if (typeof v === 'string') return v.length;
  try {
    return JSON.stringify(v).length;
  } catch (e) {
    return String(v).length;
  }
}

/* ----------------------------------------------------------------------
   Squarified treemap 算法（自实现，不引第三方库）
   ----------------------------------------------------------------------
   参考 Bruls / Huizing / van Wijk 的「squarified treemap」：
   1. 把待布局项按面积降序排序；
   2. 逐行贪心选择，使该行内矩形的**长宽比**尽量接近 1；
   3. 每行沿当前较短的一边铺开，然后递归处理剩余区域。

   返回：[{x, y, w, h, item}]，坐标单位与传入的 width/height 相同。
   ---------------------------------------------------------------------- */
function obsSquarify(items, x, y, w, h) {
  const out = [];
  /* 面积权重取 ``it.value``；若没有则退回 ``it.tokens``。
     两个 treemap 用法不同：上下文占用用 token 估算（tokens），
     代码库图用行数（value）。

     内部统一用 ``{item, value}`` 包装做计算，最后再把原对象还原出去。 */
  const weight = (it) => {
    const v = Number(it && it.value);
    if (Number.isFinite(v) && v > 0) return v;
    const t = Number(it && it.tokens);
    if (Number.isFinite(t) && t > 0) return t;
    return 0;
  };
  const list = items
    .filter((it) => it && weight(it) > 0)
    .map((it) => ({ item: it, value: weight(it) }))
    .sort((a, b) => b.value - a.value);
  if (!list.length || w <= 0 || h <= 0) return out;

  /* --- 面积归一化 ---------------------------------------------------
     squarified treemap 是**面积驱动**的：每项的像素面积 = 其权重占总权重
     的比例 × 容器面积。

     设 k = 容器面积 / 总权重（单位权重对应的像素面积），则第 i 项的
     像素面积 A_i = k * value_i。

     每一"行"（条）沿剩余区域的**短边**铺设，设条长 L（像素）、条厚 t，
     则条内第 i 项沿长度方向占 l_i = A_i / t，且 l_i 之和 = L。
     由 A_i = t * l_i 且 ΣA_i = t * L 反推：
         t = (k * Σvalue) / L

     项 i 的长宽比 = l_i / t = value_i * L² / (k * Σvalue²)，
     故最差长宽比可写成（Bruls 判据）：
         worst = max( maxVal * L² / (k * sum²),
                      k * sum² / (minVal * L²) )
     第一项随 sum 增大而**变小**，第二项随 sum 增大而**变大**，
     二者取大存在极小值 —— 这正是"行会主动断开"的驱动力。 */
  const totalValue = list.reduce((s, r) => s + r.value, 0);
  if (totalValue <= 0) return out;
  /* 单位权重对应的像素面积；在整个布局过程中保持不变（面积守恒） */
  const k = (w * h) / totalValue;
  if (!Number.isFinite(k) || k <= 0) return out;

  let rest = list;
  let rx = x, ry = y, rw = w, rh = h;

  /* 候选行的"最差长宽比"（Bruls 判据，见上方推导）。 */
  const worst = (row, L) => {
    if (!row.length || L <= 0) return Infinity;
    const sum = row.reduce((s, r) => s + r.value, 0);
    if (sum <= 0) return Infinity;
    let maxV = 0;
    let minV = Infinity;
    for (const r of row) {
      if (r.value > maxV) maxV = r.value;
      if (r.value < minV) minV = r.value;
    }
    if (maxV <= 0 || minV <= 0) return Infinity;
    const L2 = L * L;
    const s2 = sum * sum;
    return Math.max((maxV * L2) / (k * s2), (k * s2) / (minV * L2));
  };

  while (rest.length && rw > 0.5 && rh > 0.5) {
    /* 行沿剩余区域的**短边**铺设：
       rw <= rh（区域偏高）→ 条为**横条**（高 = t，宽 = rw），
         行内各项沿宽度排开，第 i 项宽 = seg_i、高 = t；
       rw >  rh（区域偏宽）→ 条为**竖条**（宽 = t，高 = rh），
         行内各项沿高度堆叠，第 i 项高 = seg_i、宽 = t。 */
    const spreadAlongWidth = rw <= rh;
    const L = spreadAlongWidth ? rw : rh;   // 条的长度方向 = 剩余区域短边
    const thicknessSpace = spreadAlongWidth ? rh : rw; // 厚度方向剩余空间

    /* 贪心选行：只要加入下一项不会让最差长宽比变差，就继续加。 */
    let row = [];
    let best = Infinity;
    for (let i = 0; i < rest.length; i++) {
      const cand = row.concat([rest[i]]);
      const wst = worst(cand, L);
      if (row.length === 0 || wst <= best) {
        best = wst;
        row = cand;
      } else {
        break;
      }
    }
    if (!row.length) break;

    const rowSum = row.reduce((s, r) => s + r.value, 0);
    if (rowSum <= 0) break;
    /* 条厚 t = (k * rowSum) / L */
    const rowThickness = (k * rowSum) / L;
    if (!Number.isFinite(rowThickness) || rowThickness <= 0) break;

    let pos = spreadAlongWidth ? rx : ry;
    row.forEach((entry) => {
      /* 该项在条内的长度占比 = value / rowSum，乘以条长 L */
      const seg = L > 0 ? (entry.value / rowSum) * L : 0;
      if (spreadAlongWidth) {
        /* 横条：项宽 = seg，项高 = 条厚 */
        out.push({ x: pos, y: ry, w: seg, h: rowThickness, item: entry.item });
        pos += seg;
      } else {
        /* 竖条：项高 = seg，项宽 = 条厚 */
        out.push({ x: rx, y: pos, w: rowThickness, h: seg, item: entry.item });
        pos += seg;
      }
    });

    /* 厚度方向被吃掉，沿厚度轴推进剩余区域 */
    if (spreadAlongWidth) {
      ry += rowThickness;
      rh -= rowThickness;
    } else {
      rx += rowThickness;
      rw -= rowThickness;
    }
    rest = rest.slice(row.length);
  }
  return out;
}

/* ----------------------------------------------------------------------
   数据接入：从现有 trace_view.js 的 TV_TRACE 取权威快照
   ---------------------------------------------------------------------- */
function obsSyncTrace() {
  if (typeof TV_TRACE === 'undefined') return;
  /* ``TV_TRACE.current`` 是「当前会话」的权威快照（每次 trace 事件都会覆盖它），
     优先级最高；Map 里的是按 thread_id 归档的历史快照，用于会话切换时取数。

     **例外**：用户刚点了「新建会话」→ obsReset() 置了 suppressRestore，
     此时**不**从 TV_TRACE 回填（否则会把上一轮的数据又灌回来，
     空态永远出不来）。 */
  if (OBS.suppressRestore) {
    OBS.trace = [];
    return;
  }
  let rec = null;
  if (TV_TRACE.current && TV_TRACE.current.length) {
    rec = { trace: TV_TRACE.current, budget: TV_TRACE.budget || null };
  } else {
    const keys = [];
    if (OBS.threadId) keys.push(OBS.threadId);
    if (typeof state !== 'undefined' && state.threadId) keys.push(state.threadId);
    keys.push('current');
    for (const k of keys) {
      if (!k) continue;
      const r = TV_TRACE.byThread && TV_TRACE.byThread.get(k);
      if (r && (r.trace || []).length) { rec = r; break; }
      if (!rec && r) rec = r;
    }
  }
  OBS.trace = (rec && rec.trace) || TV_TRACE.current || [];
  OBS.budget = (rec && rec.budget) || TV_TRACE.budget || null;
  if (OBS.budget && OBS.budget.context_window) {
    OBS.contextWindow = OBS.budget.context_window;
  }
  if (OBS.budget && typeof OBS.budget.system_prompt_chars === 'number') {
    OBS.systemPromptChars = OBS.budget.system_prompt_chars;
  }
}

/** SSE 事件钩子：由 app.js 的 streamChat 调用 */
function obsOnSse(obj) {
  if (!obj || !obj.event) return;
  if (obj.event === 'start') {
    OBS.startedAt = obj.ts || (Date.now() / 1000);
    OBS.runStart = Date.now();
    OBS.trace = [];
    OBS.approvals = [];
    OBS.edits = [];
    OBS.answer = '';
    OBS.finished = false;
    /* 新一轮开始 → 允许再次恢复（若本轮没有 trace）*/
    OBS.suppressRestore = false;
    /* 本轮用户提问（Live Timeline 的起点节点）*/
    OBS.prompt = obj.message || '';
    /* 记下会话 id：审批时 /approve 端点需要它（与对话页 HITL 卡片同一份） */
    if (obj.thread_id) OBS.threadId = obj.thread_id;
    obsStartTimer();
  } else if (obj.event === 'trace') {
    obsSyncTrace();
    if (obj.context_window) OBS.contextWindow = obj.context_window;
    if (typeof obj.system_prompt_chars === 'number') {
      OBS.systemPromptChars = obj.system_prompt_chars;
    }
    if (obj.trace && obj.trace.length) {
      OBS.trace = obj.trace;
    }
    // 审批条目（后端 kind=approval）并入审批列表
    (obj.trace || []).forEach((e) => {
      if (e && e.kind === 'approval' && !OBS.approvals.some((a) => a.tool_call_id === e.id)) {
        OBS.approvals.push({
          tool_call_id: e.id,
          tool_name: e.name,
          permission: e.permission || '',
          impact: e.impact || '',
          reversible: e.reversible !== false,
          args: e.args || {},
          diff: (e.args && e.args.diff) || null,
          started_at: e.started_at || e.ts,
        });
      }
    });
    obsRender();
  } else if (obj.event === 'approval') {
    if (obj.thread_id) OBS.threadId = obj.thread_id;
    (obj.approvals || []).forEach((ap) => {
      if (!OBS.approvals.some((a) => a.tool_call_id === ap.tool_call_id)) {
        ap.started_at = Date.now() / 1000;
        OBS.approvals.push(ap);
        if (ap.diff) {
          OBS.edits.push({
            path: (ap.diff && ap.diff.path) || (ap.args && ap.args.path) || '(未知文件)',
            diff: ap.diff,
            ts: ap.started_at,
            tool: ap.tool_name || '',
          });
        }
      }
    });
    obsRender();
  } else if (obj.event === 'result') {
    obsStopTimer();
    OBS.finished = true;
    if (obj.answer) OBS.answer = String(obj.answer);
    if (obj.budget && obj.budget.context_window) OBS.contextWindow = obj.budget.context_window;
    if (obj.budget && typeof obj.budget.system_prompt_chars === 'number') {
      OBS.systemPromptChars = obj.budget.system_prompt_chars;
    }
    if (obj.trace && obj.trace.length) OBS.trace = obj.trace;
    obsRender();
  }
}

/** 运行中实时计时器（用于耗时实时刷新） */
function obsStartTimer() {
  obsStopTimer();
  OBS.timer = setInterval(() => {
    const page = document.querySelector('.page[data-page="' + OBS.tabId + '"]');
    if (!page || !page.classList.contains('active')) return;
    obsTickTimers(page);
  }, 1000);
}

function obsStopTimer() {
  if (OBS.timer) {
    clearInterval(OBS.timer);
    OBS.timer = null;
  }
}

/** 刷新卡片上的耗时（运行中 tick） */
function obsTickTimers(page) {
  if (!page) return;
  const now = Date.now();
  page.querySelectorAll('.obs-tc[data-running="1"]').forEach((el) => {
    const t0 = Number(el.dataset.startedMs || 0);
    if (!t0) return;
    const out = el.querySelector('.obs-tc-dur');
    if (out) out.textContent = obsFmtDur(now - t0);
  });
  page.querySelectorAll('.obs-wait[data-wait="1"]').forEach((el) => {
    const t0 = Number(el.dataset.startedMs || 0);
    if (!t0) return;
    el.textContent = obsFmtWait((now - t0) / 1000);
  });
}

/* ----------------------------------------------------------------------
   观测面板骨架 + 分段控件
   ---------------------------------------------------------------------- */

/** 12 个视图的定义（顺序即分段控件顺序） */
const OBS_VIEWS = [
  { id: 'waterfall', label: '时间瀑布', icon: 'activity', p: 'P0-1' },
  { id: 'toolcards', label: '工具调用卡', icon: 'tool', p: 'P0-2' },
  { id: 'sequence', label: '时序图', icon: 'route', p: 'P0-3' },
  { id: 'cost', label: '成本瀑布', icon: 'coins', p: 'P1-1' },
  { id: 'context', label: '上下文占用', icon: 'layers', p: 'P1-2' },
  { id: 'approval', label: '审批卡片', icon: 'shield', p: 'P1-3' },
  { id: 'difftimeline', label: '改动时间线', icon: 'diff', p: 'P1-4' },
  { id: 'timing', label: '时间信号线', icon: 'pulse', p: 'P2-1' },
  { id: 'repomap', label: '代码库图', icon: 'map', p: 'P2-2' },
  { id: 'autonomy', label: '自主性档位', icon: 'dial', p: 'P2-3' },
  { id: 'reasoning', label: '推理摘要', icon: 'brain', p: 'P2-4' },
];

/** 打开观测面板（主区新 tab，沿用现有 tab 系统） */
function obsOpenPanel() {
  const id = OBS.tabId;
  let tab = state.tabs.find((t) => t.id === id);
  if (!tab) {
    tab = openTab('obs', '运行观测', null, id);
  }
  activateTab(id);
  const page = document.querySelector('.page[data-page="' + id + '"]');
  if (!page) return;
  if (!page.querySelector('#obs-body')) {
    page.innerHTML = obsPanelHtml();
    obsBindPanel(page);
  }
  obsSyncTrace();
  obsRender();
  /* 内存里没有轨迹时（页面刷新过），从会话落盘数据恢复。
     **例外**：用户刚点了「新建会话」→ obsReset() 置了 suppressRestore，
     此时**不**恢复上一轮的轨迹（否则新会话会看到旧数据）。 */
  if (!OBS.trace.length && !OBS.suppressRestore) obsRestoreTrace();
}

/**
 * 从会话落盘数据恢复轨迹。
 *
 * 轨迹随消息的 extra.trace 一起持久化在 sessions.db 里（见 workbench.js
 * 的历史会话恢复逻辑）。页面刷新后内存缓存是空的，此时拉一次
 * ``GET /sessions/{thread_id}/messages`` 把轨迹灌回全局缓存。
 */
async function obsRestoreTrace() {
  /* 优先级：先从会话列表里找**最近一个带轨迹**的会话（has_trace=true）。
     OBS.threadId 可能残留自上一次运行（甚至指向某个没有轨迹的测试会话），
     直接用它会拉回空数据，所以它只作兜底。 */
  let tid = '';
  try {
    const res = await api('/sessions');
    const list = (res && res.sessions) || [];
    const hit = list.find((s) => s.has_trace);
    if (hit) {
      tid = hit.thread_id;
      OBS.threadId = hit.thread_id;
    }
  } catch (err) {
    /* 拿不到列表就走兜底 */
  }
  if (!tid) {
    tid = OBS.threadId
      || (typeof state !== 'undefined' && state.threadId)
      || '';
    if (!tid) return;
  }
  try {
    const res = await api('/sessions/' + encodeURIComponent(tid) + '/messages');
    const msgs = res.messages || [];
    /* 取最后一条带 trace 的助手消息 —— 它是本次运行的完整累积快照 */
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      const extra = m.extra || {};
      if (extra.trace && extra.trace.length) {
        const key = m.thread_id || tid;
        if (typeof tvSetTrace === 'function') {
          tvSetTrace(key, extra.trace, extra.budget || null);
        }
        /* tvSetTrace 只在 key 与当前会话一致时更新 TV_TRACE.current；
           从磁盘恢复时 key 往往与当前会话不同，所以这里显式对齐，
           保证 obsSyncTrace() 能取到这份数据。 */
        if (typeof TV_TRACE !== 'undefined') {
          TV_TRACE.current = extra.trace;
          if (extra.budget) TV_TRACE.budget = extra.budget;
        }
        OBS.trace = extra.trace;
        if (extra.budget) OBS.budget = extra.budget;
        break;
      }
    }
    obsSyncTrace();
    obsRender();
  } catch (err) {
    /* 拉不到就保持空态（各组件都有设计过的空态文案），不报错 */
  }
}

/** 面板 HTML 骨架
 *
 * **Live Timeline 是主视图**（调研 C 节方案 C）：12 个观测组件重组为
 * 一条统一的垂直事件时间线。原 11 个视图**全部保留**，通过
 * 「全部视图」按钮切换到原分段控件（功能一项没删）。
 */
function obsPanelHtml() {
  const filters = (typeof TL_FILTERS !== 'undefined' ? TL_FILTERS : []).map((f) => {
    return '<button type="button" class="tl-filter" data-tl-filter="' + esc(f.id) + '"'
      + ' aria-pressed="' + (f.id === 'all' ? 'true' : 'false') + '"'
      + ' aria-label="筛选：' + esc(f.label) + '">' + esc(f.label) + '</button>';
  }).join('');

  const viewBtns = [
    { id: 'timeline', label: '时间线', icon: 'activity' },
    { id: 'waterfall', label: '耗时视图', icon: 'pulse' },
    { id: 'gallery', label: '全部视图', icon: 'layers' },
  ].map((v) => {
    return '<button type="button" class="tl-filter tl-view-btn" data-tl-view="' + esc(v.id) + '"'
      + ' aria-pressed="' + (v.id === 'timeline' ? 'true' : 'false') + '"'
      + ' aria-label="切换到' + esc(v.label) + '">'
      + obsIcon(v.icon, 11) + '<span>' + esc(v.label) + '</span></button>';
  }).join('');

  return '<div class="obs-page" id="obs-root">'
    + '<header class="obs-head">'
    + '<span class="obs-title">运行观测</span>'
    + '<span class="obs-sub" id="obs-status">本次运行尚无数据</span>'
    + '<span class="grow"></span>'
    + '<div class="tl-filter-group" role="group" aria-label="时间线筛选">' + filters + '</div>'
    + '<span class="tl-header-sep">|</span>'
    + '<div class="tl-filter-group" role="group" aria-label="观测视图切换">' + viewBtns + '</div>'
    + '<button type="button" class="obs-btn" id="obs-refresh" title="重新读取轨迹快照">'
    + obsIcon('refresh', 12) + '<span>刷新</span></button>'
    + '</header>'
    + '<div class="obs-body" id="obs-body">'
    + '<section class="obs-view is-active" id="obs-view-timeline" role="tabpanel"'
    + ' aria-label="Live Timeline 统一事件时间线"></section>'
    + '</div>'
    + '<footer class="tl-tools" id="obs-timeline-tools">'
    + '<span id="tl-tools-hint">Live Timeline 把 12 个观测组件投影成一条垂直因果链</span>'
    + '<span class="grow"></span>'
    + '</footer>'
    + '</div>';
}

/** 「全部视图」——原 11 个观测视图的分段控件（一个都没删）
 *
 * **可达性兼容**：Live Timeline 是主视图，但 12 个原观测组件全部保留在这里，
 * 通过「全部视图」按钮切换即可到达。视图容器 id 由 ``'obs-view-' + v.id``
 * 动态拼接（见 OBS_VIEWS 定义），与原实现保持一致。
 */
function obsGalleryHtml(page) {
  const segs = OBS_VIEWS.map((v) => {
    return '<button type="button" class="obs-seg-btn" role="tab" data-obs-view="' + v.id + '"'
      + ' aria-selected="' + (v.id === OBS.view ? 'true' : 'false') + '"'
      + ' title="' + esc(v.p + ' · ' + v.label) + '">'
      + obsIcon(v.icon, 12) + '<span>' + esc(v.label) + '</span></button>';
  }).join('');

  const panes = OBS_VIEWS.map((v) => {
    return '<section class="obs-view' + (v.id === OBS.view ? ' is-active' : '') + '"'
      + ' id="obs-view-' + v.id + '" role="tabpanel" aria-label="' + esc(v.label) + '"></section>';
  }).join('');

  return '<div class="obs-page" id="obs-gallery">'
    + '<div class="obs-head"><span class="obs-title">全部观测视图</span>'
    + '<span class="obs-sub">12 个观测组件（Live Timeline 是它们的统一投影）</span>'
    + '<span class="grow"></span>'
    + '<div class="obs-seg" role="tablist" aria-label="观测视图切换">' + segs + '</div>'
    + '</div>'
    + '<div class="obs-body" id="obs-gallery-body">' + panes + '</div>'
    + '</div>';
}

/** 绑定面板交互（筛选切换 / 视图切换 / 刷新 / 复制 / 展开全部） */
function obsBindPanel(page) {
  if (!page || page.dataset.obsBound === '1') return;
  page.dataset.obsBound = '1';

  page.addEventListener('click', (e) => {
    /* ---- Live Timeline 筛选 ---- */
    const tf = e.target.closest('.tl-filter[data-tl-filter]');
    if (tf) {
      tlSwitchFilter(tf.getAttribute('data-tl-filter'));
      return;
    }
    /* ---- Live Timeline 视图切换（时间线 / 耗时视图 / 全部视图）---- */
    const tv = e.target.closest('.tl-view-btn[data-tl-view]');
    if (tv) {
      tlSwitchView(tv.getAttribute('data-tl-view'));
      return;
    }
    /* ---- 「全部视图」里的原分段控件 ---- */
    const seg = e.target.closest('.obs-seg-btn');
    if (seg) {
      obsSwitchView(page, seg.dataset.obsView);
      return;
    }
    /* ---- 复制按钮 ---- */
    const copy = e.target.closest('.obs-tc-copy');
    if (copy) {
      const target = page.querySelector('#' + copy.dataset.target);
      if (!target) return;
      const text = target.textContent || '';
      copyText(text).then(() => {
        copy.classList.add('done');
        const old = copy.getAttribute('data-label') || copy.textContent;
        copy.setAttribute('data-label', old);
        copy.textContent = '已复制';
        setTimeout(() => { copy.classList.remove('done'); copy.textContent = old; }, 1400);
      });
      return;
    }
    /* ---- 工具卡「重试」---- */
    const retry = e.target.closest('.obs-tc-retry');
    if (retry) {
      obsRetryCall(retry);
      return;
    }
    /* ---- 分组「展开全部」---- */
    const grp = e.target.closest('.obs-tc-group-toggle');
    if (grp) {
      const box = grp.closest('.obs-tc-group');
      if (box) {
        const open = box.dataset.open === '1';
        box.dataset.open = open ? '0' : '1';
        box.querySelectorAll('details.obs-tc').forEach((d) => { d.open = !open; });
        grp.textContent = open ? '展开全部' : '收起';
        grp.setAttribute('aria-expanded', open ? 'false' : 'true');
      }
      return;
    }
    /* ---- 瀑布条 → 定位到工具卡 ---- */
    const bar = e.target.closest('.obs-wf-bar');
    if (bar) {
      obsFocusToolCall(bar.dataset.cid);
      return;
    }
    /* ---- 时序图消息 / 参与者 → 打开对应卡片 ---- */
    const msg = e.target.closest('.obs-seq-msg');
    if (msg) {
      obsFocusToolCall(msg.dataset.cid);
      return;
    }
    const part = e.target.closest('.obs-seq-part');
    if (part) {
      obsFocusToolCall(part.dataset.cid);
      return;
    }
    /* ---- treemap 单元 → 展开明细 ---- */
    const cell = e.target.closest('.obs-tm-cell');
    if (cell) {
      obsShowContextDetail(page, cell.dataset.kind);
    }
  });

  const refresh = page.querySelector('#obs-refresh');
  if (refresh) {
    refresh.addEventListener('click', () => {
      obsSyncTrace();
      obsRender();
    });
  }
}

/** 切换子视图（「全部视图」内的原 11 个视图） */
function obsSwitchView(page, viewId) {
  if (!OBS_VIEWS.some((v) => v.id === viewId)) return;
  OBS.view = viewId;
  page.querySelectorAll('.obs-seg-btn').forEach((b) => {
    b.setAttribute('aria-selected', b.dataset.obsView === viewId ? 'true' : 'false');
  });
  page.querySelectorAll('#obs-gallery-body .obs-view').forEach((s) => {
    s.classList.toggle('is-active', s.id === 'obs-view-' + viewId);
  });
  /* 在「全部视图」里就地渲染对应组件 */
  if (TL.view === 'gallery') {
    tlRenderGallerySub(page);
    return;
  }
  obsRender();
}

/** 总渲染入口
 *
 * **主视图是 Live Timeline**（统一垂直事件时间线）；
 * 「耗时视图」复用原 Trace Waterfall；「全部视图」保留原 11 个组件。
 * 三条路径下 12 个组件的可达性都保留。
 */
function obsRender() {
  const page = document.querySelector('.page[data-page="' + OBS.tabId + '"]');
  if (!page) return;
  obsSyncTrace();
  const status = page.querySelector('#obs-status');
  const n = OBS.trace.length;
  if (status) {
    if (!n) {
      status.textContent = '本次运行尚无数据';
    } else {
      const durs = OBS.trace.map((e) => Number(e.duration_ms) || 0);
      const total = durs.reduce((a, b) => a + b, 0);
      status.textContent = n + ' 个步骤 · 工具耗时合计 ' + obsFmtDur(total);
    }
  }
  /* 渲染 Live Timeline（或耗时视图 / 全部视图） */
  if (typeof tlRenderPanel === 'function') {
    tlRenderPanel(page);
    return;
  }
  /* 兜底：timeline.js 未加载时仍渲染原视图 */
  const host = page.querySelector('#obs-view-' + OBS.view);
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
    repomap: obsRenderRepoMap,
    autonomy: obsRenderAutonomy,
    reasoning: obsRenderReasoning,
  };
  const fn = renderers[OBS.view];
  host.innerHTML = fn ? fn() : '';
  obsBindView(page, OBS.view);
  /* 代码库图需要异步拉文件清单（第一次进入时） */
  if (OBS.view === 'repomap' && !OBS.repoLoaded) {
    obsLoadRepoMap(page);
  }
}

/* ----------------------------------------------------------------------
   订阅审批决策广播（2026-11-24，审批统一入口）
   ----------------------------------------------------------------------
   对话页批完 → 这里立刻把「待审批」翻成「已批准 / 已拒绝」并重绘。
   此前审批走 ``/approve``（独立 HTTP 请求）时 SSE 是关着的，
   面板因此永远停在审批前那一帧（琥珀色「待审批」），
   用户以为没批成功、或以为要在另一个面板里再批一次。 */
if (typeof APPROVAL !== 'undefined' && typeof APPROVAL.onResolved === 'function') {
  APPROVAL.onResolved((detail) => {
    const cid = detail && detail.tool_call_id;
    if (!cid) return;
    const list = OBS.approvals || [];
    let changed = false;
    list.forEach((ap) => {
      if (!ap || ap.tool_call_id !== cid) return;
      const next = (detail.approved ? 'approved' : 'rejected');
      if (ap.resolved !== next) changed = true;
      ap.resolved = next;
      ap.resolved_summary = (detail.summary || '') || ap.resolved_summary || '';
    });
    if (changed) obsRender();
  });
}

/** 拉取代码库文件清单（Repo Map 数据源） */
async function obsLoadRepoMap(page) {  OBS.repoLoaded = true;
  try {
    const res = await api('/workspace/repomap');
    if (res && Array.isArray(res.files)) {
      obsSyncRepoFiles(res.files);
      /* 标记 Agent 编辑过的文件（来自本次运行的改动记录） */
      const edited = new Set((OBS.edits || []).map((e) => e.path));
      OBS.repoFiles.forEach((f) => {
        if (edited.has(f.path)) f.edited = true;
      });
      /* 异步回填后要重画当前视图。
         **注意**：观测面板有两层「激活态」——
           · 外层 tab：``.page.active``（页面级）
           · 内层子视图：``.obs-view.is-active``（「全部视图」画廊里的当前组件）
         只判外层会漏掉「在画廊里切到 Repo Map」的情况，导致 treemap 一直空着。
         这里两层都判，并直接重画对应容器。 */
      const host = page && page.querySelector('#obs-view-repomap');
      const needPaint = !page
        || page.classList.contains('active')
        || page.classList.contains('is-active')
        || (host && host.classList.contains('is-active'));
      if (needPaint && host) {
        host.innerHTML = obsRenderRepoMap();
        obsBindView(page, 'repomap');
      }
    }
  } catch (err) {
    OBS.repoLoaded = false;
    const host = page && page.querySelector('#obs-view-repomap');
    if (host) {
      host.innerHTML = obsEmpty('代码库清单加载失败',
        '无法读取工作区文件清单：' + (err && err.message ? err.message : '未知错误')
        + '。请确认工作区根目录可访问后重试。', 'map');
    }
  }
}

/** 每个视图内的额外绑定（展开 / 计时器标记等） */
function obsBindView(page, view) {
  if (view === 'toolcards') {
    /* 运行中的卡片打标记，让计时器能 tick */
    page.querySelectorAll('.obs-tc[data-state="run"]').forEach((el) => {
      const t0 = el.dataset.startedMs;
      if (t0) el.dataset.startedMs = t0;
    });
  }
  if (view === 'approval') {
    obsBindApprovalKeys(page);
  }
}

/* ======================================================================
   P0-1 Trace Waterfall（时间瀑布）
   ----------------------------------------------------------------------
   **按真实开始/结束时间定位**：条形的 left = (started - t0) / (t1 - t0)，
   width = (ended - started) / (t1 - t0)。不是等宽列表。
   ====================================================================== */
function obsRenderWaterfall() {
  const trace = OBS.trace || [];
  if (!trace.length) {
    return obsEmpty('本次运行尚无工具调用',
      '发一条需要检索或工具调用的问题（例如「T101_VDD_FUNCT 的上下限是多少？」），'
      + '这里会出现按真实时间比例定位的横向条形。', 'activity');
  }

  /* ---- 计算时间轴范围 ---- */
  let t0 = Infinity, t1 = -Infinity;
  trace.forEach((e) => {
    const s = Number(e.started_at || e.ts || 0);
    const en = Number(e.ended_at || 0) || (Number(e.duration_ms) ? s + Number(e.duration_ms) / 1000 : 0);
    if (s) { t0 = Math.min(t0, s); t1 = Math.max(t1, en || s); }
  });
  if (!Number.isFinite(t0) || !Number.isFinite(t1) || t1 <= t0) {
    t0 = t0 === Infinity ? 0 : t0;
    t1 = t0 + 1;
  }
  const span = Math.max(t1 - t0, 0.001);

  /* ---- 最慢的一步 ---- */
  let slowest = -1;
  let slowestDur = -1;
  trace.forEach((e, i) => {
    const d = Number(e.duration_ms) || 0;
    if (d > slowestDur) { slowestDur = d; slowest = i; }
  });

  /* ---- 刻度（5 等分）---- */
  const ticks = [];
  for (let i = 0; i <= 4; i++) {
    const p = i / 4;
    const at = t0 + span * p;
    ticks.push('<span class="obs-wf-tick" style="left:' + (p * 100).toFixed(3) + '%">'
      + esc(obsFmtClock(at)) + '</span>');
  }

  /* ---- 行 ---- */
  const rows = trace.map((e, i) => {
    const kind = e.kind || 'tool';
    const s = Number(e.started_at || e.ts || 0) || t0;
    const dur = Number(e.duration_ms);
    const en = Number(e.ended_at || 0) || (Number.isFinite(dur) && dur >= 0 ? s + dur / 1000 : s);
    const left = Math.max(0, ((s - t0) / span) * 100);
    let width = Math.max(0, ((en - s) / span) * 100);
    /* 极短的步骤给一个最小可见宽度，但**记录真实宽度**在 data-* 上 */
    const visualWidth = Math.max(width, 0.8);
    const state = e.ok === false ? 'err' : (e.kind === 'approval' ? 'wait' : 'ok');
    const durTxt = Number.isFinite(dur) ? obsFmtDur(dur) : '–';
    const tok = (e.tokens != null && e.tokens !== undefined) ? e.tokens : null;
    const isSlow = (i === slowest && slowestDur > 0);

    const bars = [];
    if (kind === 'approval') {
      bars.push('<span class="obs-pill warn">等待审批</span>');
    }
    if (tok != null) {
      bars.push('<span class="obs-wf-tok">' + tok + ' tok</span>');
    }
    bars.push('<span class="obs-wf-dur">' + esc(durTxt) + '</span>');
    if (isSlow) {
      bars.unshift('<span class="obs-wf-slowest">最慢</span>');
    }

    const name = esc(e.name || '(工具)');
    /* 条形太窄（<3%）时把文字移到条形右侧，避免糊成一团 */
    const narrowCls = visualWidth < 3 ? ' is-narrow' : '';
    return '<div class="obs-wf-row">'
      + '<div class="obs-wf-label" title="' + esc(name + ' · ' + durTxt) + '">'
      + obsIcon(kind === 'llm' ? 'brain' : (kind === 'approval' ? 'shield' : 'tool'), 12)
      + '<span class="obs-wf-name">' + name + '</span></div>'
      + '<div class="obs-wf-track">'
      + '<button type="button" class="obs-wf-bar kind-' + kind + ' state-' + state
      + (isSlow ? ' is-slowest' : '') + narrowCls + '"'
      + ' style="left:' + left.toFixed(3) + '%;width:' + visualWidth.toFixed(3) + '%"'
      + ' data-cid="' + esc(e.id || ('#' + e.seq)) + '"'
      + ' data-seq="' + esc(String(e.seq || i + 1)) + '"'
      + ' data-started="' + s.toFixed(3) + '"'
      + ' data-ended="' + en.toFixed(3) + '"'
      + ' data-dur="' + (Number.isFinite(dur) ? dur : '') + '"'
      + ' data-real-width="' + width.toFixed(4) + '"'
      + ' title="' + esc(name + ' · ' + durTxt + (tok != null ? ' · ' + tok + ' tok' : '')) + '">'
      + '<span class="obs-wf-bar-txt">' + bars.join('') + '</span>'
      + '</button>'
      + '</div></div>';
  }).join('');

  const foot = '<div class="obs-wf-foot">'
    + '<span class="obs-wf-legend"><span class="obs-wf-legend-dot k-llm"></span>模型调用</span>'
    + '<span class="obs-wf-legend"><span class="obs-wf-legend-dot k-tool"></span>工具调用</span>'
    + '<span class="obs-wf-legend"><span class="obs-wf-legend-dot k-approval"></span>审批等待</span>'
    + '<span>条形按真实开始/结束时间定位（横向可滚动）</span>'
    + '<span class="obs-hint">最慢一步：' + (slowestDur > 0
        ? esc((trace[slowest] && trace[slowest].name) || '–') + ' · ' + obsFmtDur(slowestDur)
        : '本次运行暂无耗时数据') + '</span>'
    + '</div>';

  return '<div class="obs-wf">'
    + '<div class="obs-wf-scroll"><div class="obs-wf-inner">'
    + '<div class="obs-wf-axis">' + ticks.join('') + '</div>'
    + rows
    + '</div></div>'
    + foot
    + '</div>';
}

/* ======================================================================
   P0-2 Tool Call Card（工具调用卡）
   ====================================================================== */
function obsRenderToolCards() {
  const trace = (OBS.trace || []).filter((e) => (e.kind || 'tool') === 'tool');
  if (!trace.length) {
    return obsEmpty('本次运行尚无工具调用',
      '工具调用卡会把每次调用折叠成一行：状态点、耗时、参数摘要；'
      + '展开后可见着色后的参数与结果，并可复制。', 'tool');
  }

  /* ---- 按工具名分组 ---- */
  const groups = new Map();
  trace.forEach((e) => {
    const name = e.name || '(工具)';
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(e);
  });

  const out = [];
  groups.forEach((items, name) => {
    const total = items.reduce((a, e) => a + (Number(e.duration_ms) || 0), 0);
    const errCount = items.filter((e) => e.ok === false).length;
    const head = '<div class="obs-tc-group-head" role="button" tabindex="0"'
      + ' aria-label="工具 ' + esc(name) + ' 共 ' + items.length + ' 次调用">'
      + obsIcon('tool', 12)
      + '<span class="obs-tc-group-name">' + esc(name) + '</span>'
      + '<span class="obs-tc-count">×' + items.length + '</span>'
      + (errCount ? '<span class="obs-pill err">' + errCount + ' 失败</span>' : '')
      + '<span class="grow"></span>'
      + '<span class="obs-pill">合计 ' + esc(obsFmtDur(total)) + '</span>'
      + (items.length > 1
          ? '<button type="button" class="obs-btn obs-tc-group-toggle" aria-expanded="false">展开全部</button>'
          : '')
      + '</div>';

    const cards = items.map((e) => obsToolCardHtml(e)).join('');
    out.push('<div class="obs-tc-group" data-open="0">' + head + cards + '</div>');
  });

  return out.join('');
}

/** 单张工具调用卡 */
function obsToolCardHtml(e) {
  const cid = e.id || ('#' + e.seq);
  const ok = e.ok !== false;
  const dur = Number(e.duration_ms);
  const hasDur = Number.isFinite(dur);
  const state = ok ? 'ok' : 'err';
  const argsSummary = obsArgsSummary(e.args || {});
  const err = e.error || {};

  const head = '<summary>'
    + '<svg class="obs-tc-caret" viewBox="0 0 16 16" width="12" height="12" fill="none"'
    + ' stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M6 4l4 4-4 4"/></svg>'
    + '<span class="obs-dot ' + state + '" title="' + (ok ? '执行成功' : '执行失败') + '"></span>'
    + '<span class="obs-tc-name">' + esc(e.name || '(工具)') + '</span>'
    + (argsSummary ? '<span class="obs-tc-sum">' + esc(argsSummary) + '</span>' : '<span class="grow"></span>')
    + (e.tokens != null ? '<span class="obs-pill">' + e.tokens + ' tok</span>' : '')
    + '<span class="obs-tc-dur">' + esc(hasDur ? obsFmtDur(dur) : '–') + '</span>'
    + '</summary>';

  let body = '<div class="obs-tc-body">';

  /* ---- 参数区 ---- */
  body += '<div class="obs-tc-zone" data-zone="args">'
    + '<div class="obs-tc-zone-head"><span class="obs-tc-zone-title">参数</span>'
    + '<button type="button" class="obs-tc-copy" data-target="obs-args-' + esc(cid) + '"'
    + ' aria-label="复制参数 JSON">复制</button></div>'
    + '<pre class="obs-tc-json" id="obs-args-' + esc(cid) + '">'
    + tvJsonHtml(e.args || {}) + '</pre>'
    + '</div>';

  /* ---- 结果区 ---- */
  if (e.result !== undefined && e.result !== null) {
    body += '<div class="obs-tc-zone" data-zone="result">'
      + '<div class="obs-tc-zone-head"><span class="obs-tc-zone-title">结果</span>'
      + '<button type="button" class="obs-tc-copy" data-target="obs-res-' + esc(cid) + '"'
      + ' aria-label="复制结果 JSON">复制</button></div>'
      + '<pre class="obs-tc-json" id="obs-res-' + esc(cid) + '">'
      + tvJsonHtml(e.result) + '</pre>'
      + '</div>';
  } else {
    body += '<div class="obs-tc-zone" data-zone="result">'
      + '<div class="obs-tc-zone-head"><span class="obs-tc-zone-title">结果</span></div>'
      + '<div class="obs-hint">该调用尚未返回结果（仍在执行或已被中断）。</div>'
      + '</div>';
  }

  /* ---- 错误区 ---- */
  if (err && Object.keys(err).length) {
    body += '<div class="obs-tc-zone" data-zone="error">'
      + '<div class="obs-tc-zone-head"><span class="obs-tc-zone-title">错误</span></div>'
      + '<div class="obs-tc-err">'
      + '<div><span class="obs-tc-err-code">' + esc(err.code || 'ERROR') + '</span>'
      + '<span class="obs-tc-err-msg">' + esc(err.message || '(无消息)') + '</span></div>'
      + (err.hint ? '<div class="obs-tc-err-hint">' + esc(err.hint) + '</div>' : '')
      + (err.retryable
          ? '<button type="button" class="obs-tc-retry" data-cid="' + esc(cid) + '"'
            + ' data-tool="' + esc(e.name || '') + '">重试该调用</button>'
          : '<div class="obs-hint">该错误标记为不可重试（retryable=false），'
            + '请按上面的 hint 处理。</div>')
      + '</div></div>';
  }

  body += '</div>';

  return '<details class="obs-tc' + (ok ? '' : ' is-error') + '" data-cid="' + esc(cid) + '"'
    + ' data-state="' + state + '">' + head + body + '</details>';
}

/** 参数摘要（折叠头一行） */
function obsArgsSummary(args) {
  if (!args || typeof args !== 'object') return '';
  const keys = Object.keys(args);
  if (!keys.length) return '';
  const parts = keys.slice(0, 3).map((k) => {
    let v = args[k];
    if (v === null || v === undefined) v = 'null';
    if (typeof v === 'object') v = Array.isArray(v) ? ('[' + v.length + ' 项]') : '{…}';
    let s = String(v);
    if (s.length > 40) s = s.slice(0, 40) + '…';
    return k + '=' + s;
  });
  if (keys.length > 3) parts.push('…+' + (keys.length - 3));
  return parts.join(' · ');
}

/** 重试工具调用（后端无独立重试端点 → 诚实提示 + 记录） */
function obsRetryCall(btn) {
  const cid = btn.dataset.cid || '';
  const tool = btn.dataset.tool || '';
  btn.disabled = true;
  btn.textContent = '后端未提供重试端点';
  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = '重试该调用';
  }, 2400);
  const page = document.querySelector('.page[data-page="' + OBS.tabId + '"]');
  if (page) {
    const tip = document.createElement('div');
    tip.className = 'obs-hint';
    tip.style.marginTop = '6px';
    tip.textContent = '当前后端没有暴露「按 tool_call_id 重试」的端点，'
      + '因此这里只标记了待重试的调用（' + (tool || cid) + '）。'
      + '如需重试，请在对话里重新提问，或直接编辑受影响的数据。';
    const card = btn.closest('.obs-tc');
    if (card) card.appendChild(tip);
  }
}

/* ======================================================================
   P0-3 Agent Sequence（时序图）
   ----------------------------------------------------------------------
   参与者：用户 / Agent / 每个被调用的工具（横向排开）。
   生命线：竖直虚线。消息：调用=实线向右、回复=虚线向左。
   ====================================================================== */
function obsRenderSequence() {
  const trace = OBS.trace || [];
  if (!trace.length) {
    return obsEmpty('本次运行尚无参与者',
      '时序图会把「你 → Agent → 每个工具」画成生命线与箭头；'
      + '发一条消息后即可看到调用与回复的方向。', 'route');
  }

  /* ---- 参与者 ---- */
  const parts = [];
  const seen = new Map();
  parts.push({ id: 'user', name: '你', role: '用户', color: 'var(--fg-faint)' });
  seen.set('user', true);
  parts.push({ id: 'agent', name: 'Agent', role: '模型', color: 'var(--accent)' });
  seen.set('agent', true);
  trace.forEach((e) => {
    if ((e.kind || 'tool') === 'approval') return;
    const key = e.name || '(工具)';
    if (seen.has(key)) return;
    seen.set(key, true);
    parts.push({ id: key, name: key, role: '工具', color: 'var(--accent-2)' });
  });

  const COL_W = 150;
  const HEAD_H = 46;
  const ROW_H = 30;
  const totalW = parts.length * COL_W;
  const msgs = [];

  /* ---- 消息构造 ---- */
  trace.forEach((e) => {
    const kind = e.kind || 'tool';
    const toolName = e.name || '(工具)';
    const fromId = (kind === 'approval') ? 'agent' : 'agent';
    const toId = (kind === 'approval') ? 'user' : (seen.has(toolName) ? toolName : 'agent');
    const fromIdx = Math.max(0, parts.findIndex((p) => p.id === fromId));
    const toIdx = Math.max(0, parts.findIndex((p) => p.id === toId));
    const dur = Number(e.duration_ms);
    const label = kind === 'approval'
      ? ('审批 ' + toolName)
      : (toolName + (Number.isFinite(dur) ? ' · ' + obsFmtDur(dur) : ''));

    /* 调用（向右，实线）*/
    msgs.push({
      type: 'call', from: fromIdx, to: toIdx,
      label: label, cid: e.id || ('#' + e.seq),
      row: msgs.length,
    });
    /* 回复（向左，虚线）—— 有结果才有回复 */
    if (e.result !== undefined && e.result !== null) {
      msgs.push({
        type: 'reply', from: toIdx, to: fromIdx,
        label: '回复', cid: e.id || ('#' + e.seq),
        row: msgs.length,
      });
    }
  });

  const bodyH = Math.max(ROW_H * (msgs.length + 1), 180);

  /* ---- 生命线 ---- */
  const lifelines = parts.map((p, i) => {
    const x = i * COL_W + COL_W / 2;
    return '<span class="obs-seq-lifeline" style="left:' + x + 'px"></span>';
  }).join('');

  /* ---- 活动条（每个参与者从首条消息到最后一条消息）---- */
  const actives = [];
  parts.forEach((p, i) => {
    const rows = msgs.filter((m) => m.from === i || m.to === i).map((m) => m.row);
    if (!rows.length) return;
    const x = i * COL_W + COL_W / 2 - 3;
    const y = HEAD_H + Math.min.apply(null, rows) * ROW_H + 4;
    const h = (Math.max.apply(null, rows) - Math.min.apply(null, rows) + 1) * ROW_H - 8;
    actives.push('<span class="obs-seq-active" style="left:' + x + 'px;top:' + y + 'px;height:' + h + 'px"></span>');
  });
  const activesHtml = actives.join('');

  /* ---- 消息箭头 ---- */
  const msgHtml = msgs.map((m) => {
    const x1 = m.from * COL_W + COL_W / 2;
    const x2 = m.to * COL_W + COL_W / 2;
    const y = HEAD_H + m.row * ROW_H + 12;
    const left = Math.min(x1, x2);
    const width = Math.max(2, Math.abs(x2 - x1));
    const isCall = m.type === 'call';
    /* 箭头头部 */
    const dir = m.to > m.from ? 1 : -1;
    const headX = m.to > m.from ? x2 - 5 : x2;
    const head = '<path d="M' + (m.to > m.from ? x2 - 5 : x2 + 5) + ' ' + (y - 3)
      + ' l5 3 l-5 3" fill="none" stroke="' + (isCall ? 'var(--accent)' : 'var(--fg-faint)')
      + '" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>';
    const line = isCall
      ? '<span class="obs-seq-msg-line" style="left:0;width:' + width + 'px"></span>'
      : '<span class="obs-seq-msg-line" style="left:0;width:' + width + 'px"></span>';
    return '<span class="obs-seq-msg ' + m.type + '" role="button" tabindex="0"'
      + ' data-cid="' + esc(m.cid) + '"'
      /* 客观可断言：方向（x1→x2）与类型（call=实线向右 / reply=虚线向左） */
      + ' data-kind="' + (isCall ? 'call' : 'reply') + '"'
      + ' data-x1="' + x1.toFixed(2) + '"'
      + ' data-x2="' + x2.toFixed(2) + '"'
      + ' data-dir="' + (m.to > m.from ? 'right' : (m.to < m.from ? 'left' : 'same')) + '"'
      + ' aria-label="' + esc(m.label + '（' + (isCall ? '调用' : '回复') + '）') + '"'
      + ' title="' + esc(m.label) + '"'
      + ' style="left:' + left + 'px;top:' + (y - 10) + 'px;width:' + width + 'px;height:20px">'
      + line
      + '<svg class="obs-seq-arrow" viewBox="0 0 ' + Math.max(width, 6) + ' 20" width="'
      + Math.max(width, 6) + '" height="20" fill="none" style="position:absolute;left:0;top:0">'
      + '<path d="M' + (m.to > m.from ? width - 5 : 5) + ' 10 l5 0" stroke="'
      + (isCall ? 'var(--accent)' : 'var(--fg-faint)') + '" stroke-width="1.5" stroke-linecap="round"/>'
      + '<path d="M' + (m.to > m.from ? width - 5 : 5) + ' 7 l5 3 l-5 3" fill="none" stroke="'
      + (isCall ? 'var(--accent)' : 'var(--fg-faint)') + '" stroke-width="1.5"'
      + ' stroke-linecap="round" stroke-linejoin="round"/>'
      + '</svg>'
      + '<span class="obs-seq-msg-head">' + esc(m.label) + '</span>'
      + '</span>';
  }).join('');

  /* ---- 并行 / 重试的嵌套框 ---- */
  /* 同一轮内连续发出的调用视为一个「并行组」 */
  const frames = [];
  let i = 0;
  while (i < msgs.length) {
    const m = msgs[i];
    if (m.type !== 'call') { i++; continue; }
    /* 找同一 from 的连续调用 */
    let j = i + 1;
    while (j < msgs.length && msgs[j].type === 'call' && msgs[j].from === m.from) j++;
    if (j - i >= 2) {
      const first = msgs[i];
      const last = msgs[j - 1];
      const xs = msgs.slice(i, j).map((x) => x.from * COL_W + COL_W / 2)
        .concat(msgs.slice(i, j).map((x) => x.to * COL_W + COL_W / 2));
      const fx = Math.min.apply(null, xs) - 14;
      const fw = Math.max.apply(null, xs) - fx + 14;
      const fy = HEAD_H + first.row * ROW_H + 2;
      const fh = (last.row - first.row + 1) * ROW_H + 4;
      frames.push('<span class="obs-seq-frame" style="left:' + fx + 'px;top:' + fy
        + 'px;width:' + fw + 'px;height:' + fh + 'px">'
        + '<span class="obs-seq-frame-label">并行 / 连续调用 ×' + (j - i) + '</span></span>');
    }
    i = j;
  }

  /* ---- 参与者表头 ---- */
  const head = '<div class="obs-seq-head" style="width:' + totalW + 'px">'
    + parts.map((p, i) => {
      const cid = p.id === 'agent' || p.id === 'user' ? '' : '';
      return '<button type="button" class="obs-seq-part" style="width:' + COL_W + 'px"'
        + ' data-cid="' + esc(cid) + '"'
        + ' aria-label="参与者 ' + esc(p.name) + '"'
        + ' title="' + esc(p.name + ' · ' + p.role) + '">'
        + '<span class="obs-seq-part-bar" style="background:' + p.color + '"></span>'
        + '<span class="obs-seq-part-name">' + esc(p.name) + '</span>'
        + '<span class="obs-seq-part-role">' + esc(p.role) + '</span>'
        + '</button>';
    }).join('')
    + '</div>';

  const legend = '<div class="obs-seq-legend">'
    + '<span class="obs-wf-legend"><span class="obs-wf-legend-dot k-llm"></span>调用（实线，向右）</span>'
    + '<span class="obs-wf-legend"><span class="obs-wf-legend-dot" '
    + 'style="border-top:1px dashed var(--fg-faint)"></span>回复（虚线，向左）</span>'
    + '<span>生命线上的竖条 = 参与者工作期间</span>'
    + '<span>点击消息可跳到对应的工具调用卡</span>'
    + '</div>';

  return '<div class="obs-seq">'
    + '<div class="obs-seq-scroll"><div class="obs-seq-inner" style="width:' + totalW + 'px;height:'
    + (HEAD_H + bodyH) + 'px">'
    + head
    + '<div style="position:relative;height:' + bodyH + 'px">'
    + lifelines
    + activesHtml
    + frames.join('')
    + msgHtml
    + '</div>'
    + '</div></div>'
    + legend
    + '</div>';
}

/* ======================================================================
   P1-1 Cost Waterfall（成本瀑布）
   ----------------------------------------------------------------------
   **诚实降级**：本项目没有价格表（models.json 不含单价），
   本机 llama-server 推理**无 API 成本** → 用 token + 耗时做可观测指标，
   明确标注「本机推理，无 API 成本」。绝不编造金额。
   ====================================================================== */
function obsRenderCost() {
  const trace = (OBS.trace || []).filter((e) => (e.kind || 'tool') !== 'approval');
  if (!trace.length) {
    return obsEmpty('本次运行尚无可计量的步骤',
      '成本瀑布会按 token 与耗时列出每次模型 / 工具调用，'
      + '并把最贵的一步置顶。本机推理没有 API 费用，这里展示的是可观测指标。', 'coins');
  }

  const model = state.model || 'gpt-oss-20b';
  const rows = trace.map((e) => {
    const tok = Number(e.tokens) || 0;
    const dur = Number(e.duration_ms) || 0;
    /* 「开销」= token 为主、耗时为辅的**可观测指标**，不是金额 */
    const cost = tok * 1000 + dur;
    return { e: e, tok: tok, dur: dur, cost: cost };
  });
  const maxCost = Math.max.apply(null, rows.map((r) => r.cost)) || 1;

  const list = rows.slice().sort((a, b) => b.cost - a.cost).map((r, i) => {
    const name = r.e.name || '(工具)';
    const pct = (r.cost / maxCost) * 100;
    const dur = Number.isFinite(r.dur) ? obsFmtDur(r.dur) : '–';
    const tok = r.tok ? (r.tok + ' tok') : '– tok';
    return '<div class="obs-cost-row' + (i === 0 ? ' is-top' : '') + '">'
      + '<span class="obs-cost-rank">#' + (i + 1) + '</span>'
      + obsIcon(r.e.kind === 'llm' ? 'brain' : 'tool', 12)
      + '<span class="obs-cost-name" title="' + esc(name) + '">' + esc(name) + '</span>'
      + '<span class="obs-cost-model" title="本轮使用的模型">' + esc(model) + '</span>'
      + '<span class="obs-cost-bar-wrap">'
      + '<span class="obs-cost-bar" style="width:' + pct.toFixed(2) + '%"></span></span>'
      + '<span class="obs-cost-metric">' + esc(tok) + '</span>'
      + '<span class="obs-cost-metric">' + esc(dur) + '</span>'
      + '</div>';
  }).join('');

  const totalTok = rows.reduce((a, r) => a + r.tok, 0);
  const totalDur = rows.reduce((a, r) => a + r.dur, 0);

  const note = '<div class="obs-cost-note">'
    + '<b>本机推理，无 API 成本。</b>'
    + '本项目 <span class="mono">data/models.json</span> 不含模型单价表，'
    + '且推理跑在本地 llama-server（:8080 / :8081）上，因此<b>不显示任何金额</b>。'
    + '上表用 <b>token 数</b> 与 <b>耗时</b> 作为可观测指标（排序键 = token×1000 + 耗时毫秒），'
    + '最贵的一步已置顶。'
    + '<br />本轮合计：' + totalTok + ' token · ' + obsFmtDur(totalDur) + '。'
    + '</div>';

  return '<div class="obs-sec-head"><h3 class="obs-sec-title">成本瀑布</h3>'
    + '<span class="obs-sec-note">按 token 与耗时排序 · 最贵的一步置顶</span></div>'
    + '<div class="obs-cost">' + list + note + '</div>';
}

/* ======================================================================
   P1-2 Context Map（上下文占用 treemap）
   ----------------------------------------------------------------------
   **诚实说明**：体积按**字符数估算**（token 数按字符粗估，非精确分词）。
   ====================================================================== */
function obsRenderContext() {
  const trace = OBS.trace || [];
  const win = OBS.contextWindow || 32768;

  /* ---- 统计各来源体积（字符数）---- */
  const sources = [];
  /** 入参是**字符数**（不是字符串）。token 估算按「中文 1 字 1 token、
   *  ASCII 4 字符 1 token」的混合比例折算 —— 精确分词需要后端 tokenizer，
   *  这里只给**量级**，UI 上也明确标注是估算。 */
  const push = (kind, name, chars, detail) => {
    if (!chars) return;
    const n = Number(chars) || 0;
    if (!n) return;
    /* 保守按「全中文」折算上限：中文占 1/4、ASCII 占 3/4 时的 token 数，
     * 落在 [n/4, n] 之间，取中间值作为代表值。 */
    const tokens = Math.round(n * 0.6);
    sources.push({ kind: kind, name: name, chars: n, tokens: tokens, detail: detail || '' });
  };

  /* system prompt：后端未直接下发 → 用「工具 schema + 固定提示」估算
     （本平台的 system prompt 由 prompt.py 组装，量级与工具定义同源） */
  const toolSchemas = obsEstimateToolSchemaChars();
  push('tools', '工具 schema', toolSchemas,
    '模型实际看到的 function calling 定义（按已注册工具的 name/description/参数表估算）');

  /* 工具结果 */
  const resultChars = trace.reduce((a, e) => a + obsCharLen(e.result), 0);
  push('results', '工具结果', resultChars,
    trace.length + ' 次工具调用的返回体（JSON 字符数）');

  /* 对话轮次：用户问题 + 模型推理摘要 + 动作摘要 */
  const historyChars = trace.reduce((a, e) => {
    return a + obsCharLen(e.reasoning_summary) + obsCharLen(e.action_summary);
  }, 0) + (OBS.startedAt ? 0 : 0);
  push('history', '推理 / 动作摘要', historyChars,
    '每步的推理摘要与动作摘要（**已截断**，不含原始思维链）');

  /* 文件引用：工作区改动里引用的文件 */
  const fileChars = OBS.edits.reduce((a, ed) => a + obsCharLen(ed.diff), 0);
  push('files', '文件 / 改动', fileChars,
    OBS.edits.length + ' 次改动涉及的文件内容（diff 字符数）');

  /* system prompt：后端下发**真实字符数**（2026-11-22 新增）。
     拿不到时（0）不编造 —— 显示「未测量」而不是瞎估。 */
  const sysChars = Number(OBS.systemPromptChars || 0);
  if (sysChars) {
    push('system', 'system prompt', sysChars,
      '后端下发的**实测** system prompt 字符数（非前端估算）');
  }

  if (!sources.length) {
    return obsEmpty('本次运行尚无上下文数据',
      '上下文占用 treemap 会按来源统计体积（system prompt / 工具 schema / '
      + '工具结果 / 对话轮次 / 文件引用），并对比上下文窗口上限 '
      + win + ' token。', 'layers');
  }

  /* ---- 布局（squarified treemap）---- */
  const W = 100, H = 100;
  const rects = obsSquarify(sources, 0, 0, W, H);

  const cells = rects.map((r) => {
    const it = r.item;
    const pct = it.tokens / win * 100;
    const title = it.name + ' · ' + it.chars + ' 字符 · 约 ' + it.tokens + ' token';
    return '<button type="button" class="obs-tm-cell k-' + it.kind + '"'
      + ' data-kind="' + esc(it.kind) + '" data-name="' + esc(it.name) + '"'
      + ' style="left:' + (r.x / W * 100).toFixed(3) + '%;top:' + (r.y / H * 100).toFixed(3)
      + '%;width:' + (r.w / W * 100).toFixed(3) + '%;height:' + (r.h / H * 100).toFixed(3) + '%"'
      + ' title="' + esc(title) + '" aria-label="' + esc(title) + '">'
      + '<span class="obs-tm-cell-name">' + esc(it.name) + '</span>'
      + (r.h / H * 100 > 9
          ? '<span class="obs-tm-cell-size">' + it.tokens + ' tok · ' + pct.toFixed(1) + '%</span>'
          : '')
      + '</button>';
  }).join('');

  const totalTokens = sources.reduce((a, s) => a + s.tokens, 0);
  const totalChars = sources.reduce((a, s) => a + s.chars, 0);
  const pct = Math.min(100, totalTokens / win * 100);
  const fillCls = pct > 100 ? ' is-over' : (pct > 80 ? ' is-high' : '');

  const rows = sources.map((s) => {
    return '<div class="obs-tm-detail-row"><span>' + esc(s.name) + '</span>'
      + '<b>' + s.chars + ' 字符</b>'
      + '<span>≈ ' + s.tokens + ' token</span></div>';
  }).join('');

  const total = '<div class="obs-ctx-total">'
    + '<span class="obs-ctx-num">约 ' + totalTokens + ' / ' + win + ' token</span>'
    + '<span class="obs-ctx-bar"><span class="obs-ctx-fill' + fillCls
    + '" style="width:' + pct.toFixed(2) + '%"></span></span>'
    + '<span class="obs-ctx-num">' + pct.toFixed(1) + '%</span>'
    + '<span class="obs-hint">共 ' + totalChars + ' 字符'
    + '（体积按**字符数**估算，token 为粗估，非精确分词结果）</span>'
    + '</div>';

  const detail = '<div class="obs-tm-detail" id="obs-ctx-detail">'
    + '<div class="obs-sec-head"><h3 class="obs-sec-title">来源明细</h3>'
    + '<span class="obs-sec-note">点击上方任意色块可高亮对应来源</span></div>'
    + rows
    + '</div>';

  return '<div class="obs-sec-head"><h3 class="obs-sec-title">上下文占用</h3>'
    + '<span class="obs-sec-note">窗口上限 ' + win + ' token（llama-server -c 32768）</span></div>'
    + total
    + '<div class="obs-tm" role="group" aria-label="上下文来源 treemap">' + cells + '</div>'
    + detail;
}

/** 估算工具 schema 字符数（前端按已知工具清单近似，量级够用） */
function obsEstimateToolSchemaChars() {
  let total = 0;
  if (typeof state !== 'undefined' && Array.isArray(state.tools)) {
    total = state.tools.reduce((a, t) => {
      return a + obsCharLen(t.description) + obsCharLen(t.input_schema);
    }, 0);
  }
  if (!total) {
    /* 兜底：没缓存工具清单时用一个保守常量（不冒充真实测量） */
    return 0;
  }
  return total;
}

/** 点击 treemap 单元格 → 高亮明细行 */
function obsShowContextDetail(page, kind) {
  if (!page) return;
  const detail = page.querySelector('#obs-ctx-detail');
  if (!detail) return;
  detail.querySelectorAll('.obs-tm-detail-row').forEach((row) => {
    const nm = row.querySelector('span');
    const hit = nm && nm.textContent.indexOf(
      kind === 'tools' ? '工具 schema'
        : kind === 'results' ? '工具结果'
          : kind === 'history' ? '推理'
            : kind === 'files' ? '文件'
              : 'system') >= 0;
    row.style.color = hit ? 'var(--fg)' : '';
    if (hit) row.style.fontWeight = '600';
    else row.style.fontWeight = '';
  });
}

/* ======================================================================
   P1-3 Approval Card 增强
   ====================================================================== */
function obsRenderApproval() {
  const list = OBS.approvals || [];
  if (!list.length) {
    return obsEmpty('本次运行没有待审批的操作',
      '当 Agent 要执行写操作（WRITE_DATA / EXTERNAL_SIDE_EFFECT / ADMIN）时，'
      + '这里会显示风险徽标、影响目标预览与等待时长。', 'shield');
  }

  const cards = list.map((ap, i) => {
    const risk = obsRiskOf(ap);
    const t0 = Number(ap.started_at || 0) * 1000;
    const waitMs = t0 ? (Date.now() - t0) : 0;
    const targets = obsApprovalTargets(ap);
    /* 已解决（批准/拒绝过）的卡片折叠成一行 */
    const resolved = ap.resolved;
    const cid = esc(ap.tool_call_id || '');

    if (resolved) {
      const ok = resolved === 'approved';
      return '<div class="obs-tc obs-tc-resolved" data-approval="1"'
        + ' data-cid="' + cid + '" tabindex="0"'
        + ' aria-label="已' + (ok ? '批准' : '拒绝') + '的审批项 ' + esc(ap.tool_name || '') + '">'
        + '<div class="obs-tc-zone-head" style="padding:var(--sp-2) var(--sp-3)">'
        + obsIcon(ok ? 'check' : 'cross', 13)
        + '<span class="obs-tc-name">' + esc(ap.tool_name || '(工具)') + '</span>'
        + '<span class="obs-pill ' + (ok ? 'ok' : 'err') + '">'
        + (ok ? '已批准' : '已拒绝') + '</span>'
        + '<span class="obs-resolved-sum">' + esc(ap.resolved_summary || '') + '</span>'
        + '</div></div>';
    }

    return '<div class="obs-tc" data-approval="1" data-cid="' + cid + '" tabindex="0"'
      + ' aria-label="审批项 ' + esc(ap.tool_name || '') + '">'
      + '<div class="obs-tc-zone-head" style="padding:var(--sp-2) var(--sp-3)">'
      + obsIcon('shield', 13)
      + '<span class="obs-tc-name">' + esc(ap.tool_name || '(工具)') + '</span>'
      + '<span class="obs-risk ' + risk.level + '">' + esc(risk.label) + '</span>'
      + '<span class="obs-wait" data-wait="1" data-started-ms="' + t0 + '">'
      + esc(obsFmtWait(waitMs / 1000)) + '</span>'
      + '<span class="grow"></span>'
      + '<span class="obs-kbd" title="焦点在卡片内时按 A 批准">A</span>'
      + '<span class="obs-kbd" title="焦点在卡片内时按 D 拒绝">D</span>'
      + '</div>'
      + '<div class="obs-tc-body">'
      + '<div class="obs-kv"><span>权限</span><b>' + esc(ap.permission || '–') + '</b></div>'
      + '<div class="obs-kv"><span>影响</span><b>' + esc(ap.impact || '–') + '</b></div>'
      + '<div class="obs-kv"><span>可回滚</span><b>'
      + (ap.reversible === false ? '否（不可逆）' : '是') + '</b></div>'
      + '<div class="obs-kv"><span>目标预览</span><b>'
      + (targets.length ? esc(targets.join(' · ')) : '（未声明具体目标）') + '</b></div>'
      + '<div class="obs-tc-zone" data-zone="args">'
      + '<div class="obs-tc-zone-head"><span class="obs-tc-zone-title">参数</span>'
      + '<button type="button" class="obs-tc-copy" data-target="obs-ap-' + i + '"'
      + ' aria-label="复制审批参数 JSON">复制</button></div>'
      + '<pre class="obs-tc-json" id="obs-ap-' + i + '">'
      + tvJsonHtml(ap.args || {}) + '</pre>'
      + '</div>'
      + (ap.diff && typeof wsDiffHtml === 'function'
          ? '<div class="obs-tc-zone" data-zone="result">'
            + '<div class="obs-tc-zone-head"><span class="obs-tc-zone-title">改动预览</span></div>'
            + wsDiffHtml(ap.diff)
            + '</div>'
          : '')
      + '<div class="obs-ap-actions">'
      + '<span class="grow"></span>'
      /* ⚠️ 这两个按钮是「**定位器**」：点击后只会把对话里的审批卡片
       *    跳到视野中央并高亮，**不会**替用户决定审批。
       *    全站只有对话页 HITL 卡片会 POST /approve（唯一决策入口）。 */
      + '<button type="button" class="obs-btn obs-btn-ok" data-obs-approve="1"'
      + ' data-cid="' + cid + '" title="' + esc(OBS_LOCATOR_HINT) + '"'
      + ' aria-label="' + esc(OBS_LOCATOR_HINT) + '">在对话中处理</button>'
      + '<button type="button" class="obs-btn obs-btn-no" data-obs-deny="1"'
      + ' data-cid="' + cid + '" title="' + esc(OBS_LOCATOR_HINT) + '"'
      + ' aria-label="' + esc(OBS_LOCATOR_HINT) + '">在对话中处理</button>'
      + '</div>'
      + '</div></div>';
  }).join('');

  return '<div class="obs-sec-head"><h3 class="obs-sec-title">审批卡片</h3>'
    + '<span class="obs-sec-note">风险徽标 · 目标预览 · 等待时长 · 快捷键 A / D</span></div>'
    + '<div class="obs-hint" style="margin-bottom:var(--sp-2)">'
    + '快捷键仅在焦点位于卡片内时生效（避免抢占全局快捷键）。'
    + '风险分级依据工具 permission 与是否可回滚推导，**不代表后端安全评级**。</div>'
    + cards;
}

/** 风险分级：按 permission / 可回滚 / 影响范围推导 */
function obsRiskOf(ap) {
  const perm = String(ap.permission || '').toUpperCase();
  if (perm === 'ADMIN' || perm === 'EXTERNAL_SIDE_EFFECT') {
    return { level: 'high', label: '高风险 · ' + (perm || 'ADMIN') };
  }
  if (perm === 'WRITE_DATA') {
    return { level: 'mid', label: '中风险 · 写数据' };
  }
  if (ap.reversible === false) {
    return { level: 'mid', label: '中风险 · 不可逆' };
  }
  if (perm === 'COMPUTE') {
    return { level: 'low', label: '低风险 · 计算' };
  }
  return { level: 'low', label: '低风险 · 只读' };
}

/** 目标预览：从参数里抽出会被影响的对象 */
function obsApprovalTargets(ap) {
  const args = ap.args || {};
  const out = [];
  const keys = ['path', 'file', 'filename', 'channel', 'table', 'url', 'target',
    'doc_id', 'sheet', 'recipient'];
  keys.forEach((k) => {
    if (args[k] !== undefined && args[k] !== null && args[k] !== '') {
      out.push(k + '=' + String(args[k]));
    }
  });
  if (ap.diff && ap.diff.path) out.push('diff=' + ap.diff.path);
  return out;
}

/** 审批卡片快捷键：A=批准 D=拒绝（仅焦点在卡片内）
 *
 * ⚠️ **2026-11-24 审批统一入口**：这里的 A / D 与按钮**不再自己决定审批**，
 * 只是把对话里的审批卡片跳到眼前（定位器）。
 * 全站只有 ``app.js`` 的对话 HITL 卡片会 POST ``/approve``，
 * 因此审批状态只有一个来源，不会再出现「面板说没批、对话说批了」的矛盾。
 */
function obsBindApprovalKeys(page) {
  const root = page.querySelector('#obs-view-approval');
  if (!root || root.dataset.keysBound === '1') return;
  root.dataset.keysBound = '1';

  /* ---- 按钮点击 ---- */
  root.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('[data-obs-approve],[data-obs-deny]');
    if (!btn) return;
    const cid = btn.getAttribute('data-cid') || '';
    const approved = btn.hasAttribute('data-obs-approve');
    obsSubmitApproval(cid, approved, root);
  });

  /* ---- 键盘：A / D ---- */
  root.addEventListener('keydown', (e) => {
    if (e.key !== 'a' && e.key !== 'A' && e.key !== 'd' && e.key !== 'D') return;
    /* 只有焦点在审批卡片内才生效 —— 避免抢全局快捷键 */
    const card = e.target.closest && e.target.closest('.obs-tc[data-approval="1"]');
    if (!card) return;
    /* 输入框里打字时不触发 */
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea') return;
    e.preventDefault();
    const cid = card.getAttribute('data-cid') || '';
    const approved = e.key.toLowerCase() === 'a';
    obsSubmitApproval(cid, approved, root);
  });
}

/** 定位到对话中的审批卡片（**不** POST /approve） */
function obsSubmitApproval(cid, approved, root) {
  if (!cid) return;
  const ap = (OBS.approvals || []).find((a) => a.tool_call_id === cid);
  if (!ap) return;

  /* 已决策 → 同步面板状态（万一面板错过了一次广播）*/
  if (ap.resolved) { obsRender(); return; }

  /* 事实来源说已决策（对话页刚批过）→ 同步面板，不重复决策 */
  if (typeof APPROVAL !== 'undefined' && APPROVAL.isResolved(cid)) {
    ap.resolved = APPROVAL.getResolved(cid);
    ap.resolved_summary = (typeof APPROVAL.summaries !== 'undefined'
      && APPROVAL.summaries[cid]) || '';
    obsRender();
    return;
  }

  /* ---- 跳到对话里的审批卡片 ---- */
  const found = (typeof APPROVAL !== 'undefined')
    ? APPROVAL.focusChatCard(cid, { noFocus: false })
    : false;

  if (!found) {
    /* 对话卡片不存在 → 明确告诉用户去哪里批，而不是在面板里盲批
       （盲批会重新制造出两套状态，正是本次要消灭的问题）。 */
    if (root) {
      const card = root.querySelector('.obs-tc[data-cid="' + cssEscape(cid) + '"]');
      if (card) {
        let tip = card.querySelector('[data-obs-ap-tip]');
        if (!tip) {
          tip = document.createElement('div');
          tip.className = 'obs-hint';
          tip.setAttribute('data-obs-ap-tip', '1');
          tip.style.padding = 'var(--sp-2) var(--sp-3)';
          card.appendChild(tip);
        }
        tip.textContent = '这张审批需要在「对话」里完成：当前对话页没有对应的审批卡片。';
        tip.classList.add('err');
      }
    }
    return;
  }

  /* 面板侧只做「提示」，不改变审批状态 */
  if (root) {
    const card = root.querySelector('.obs-tc[data-cid="' + cssEscape(cid) + '"]');
    if (card) {
      let tip = card.querySelector('[data-obs-ap-tip]');
      if (!tip) {
        tip = document.createElement('div');
        tip.className = 'obs-hint';
        tip.setAttribute('data-obs-ap-tip', '1');
        tip.style.padding = 'var(--sp-2) var(--sp-3)';
        card.appendChild(tip);
      }
      tip.textContent = '已跳转到对话中的审批卡片，请在那里点击「'
        + (approved ? '批准' : '拒绝') + '」。';
      tip.classList.remove('err');
    }
  }
}

/** 简易 CSS 属性值转义（用于选择器拼装） */
function cssEscape(v) {
  return String(v == null ? '' : v).replace(/["\\]/g, '\\$&');
}

/* ======================================================================
   P1-4 Diff Timeline（改动时间线）
   ====================================================================== */
function obsRenderDiffTimeline() {
  const edits = OBS.edits || [];
  if (!edits.length) {
    return obsEmpty('本次运行没有文件改动',
      '改动时间线会把 Agent 的每次编辑按时间排列，'
      + '每个文件配一条进度条；悬停可看该次编辑的 diff。', 'diff');
  }

  /* ---- 按文件分组 ---- */
  const byFile = new Map();
  edits.forEach((ed, i) => {
    const p = ed.path || '(未知文件)';
    if (!byFile.has(p)) byFile.set(p, []);
    byFile.get(p).push(Object.assign({ idx: i }, ed));
  });

  const files = [];
  byFile.forEach((items, path) => {
    const added = items.reduce((a, it) => a + ((it.diff && it.diff.added) || 0), 0);
    const removed = items.reduce((a, it) => a + ((it.diff && it.diff.removed) || 0), 0);
    const last = items[items.length - 1];
    const progress = Math.min(100, items.length / Math.max(items.length, edits.length) * 100);
    files.push('<button type="button" class="obs-dtl-file" data-file="' + esc(path) + '"'
      + ' aria-label="文件 ' + esc(path) + '，' + items.length + ' 次改动">'
      + '<span class="obs-dtl-file-name" title="' + esc(path) + '">' + esc(path) + '</span>'
      + '<span class="obs-dtl-file-bar"><span class="obs-dtl-file-fill" style="width:'
      + progress.toFixed(1) + '%"></span></span>'
      + '<span class="obs-hint">' + items.length + ' 次 · +' + added + ' / -' + removed
      + ' · 最后 ' + esc(obsFmtClock(last.ts)) + '</span>'
      + '</button>');
  });

  /* ---- 时间线（按时间排列的所有编辑）---- */
  const timeline = edits.slice().sort((a, b) => (a.ts || 0) - (b.ts || 0)).map((ed, i) => {
    const d = ed.diff || {};
    const added = d.added || 0;
    const removed = d.removed || 0;
    const body = (typeof wsDiffHtml === 'function' && d.lines && d.lines.length)
      ? wsDiffHtml(d)
      : '<div class="obs-hint">该次改动没有可渲染的行级 diff。</div>';
    return '<details class="obs-dtl-edit" data-file="' + esc(ed.path || '') + '"'
      + (i === 0 ? ' open' : '') + '>'
      + '<summary class="obs-dtl-edit-head" style="cursor:pointer;list-style:none">'
      + '<span class="obs-pill">#' + (i + 1) + '</span>'
      + '<span class="mono">' + esc(ed.path || '(未知文件)') + '</span>'
      + '<span class="obs-pill ok">+' + added + '</span>'
      + '<span class="obs-pill err">-' + removed + '</span>'
      + '<span class="grow"></span>'
      + '<span>' + esc(obsFmtClock(ed.ts)) + '</span>'
      + '<span>' + esc(ed.tool || '') + '</span>'
      + '</summary>'
      + '<div class="obs-dtl-edit-body">' + body + '</div>'
      + '</details>';
  }).join('');

  return '<div class="obs-sec-head"><h3 class="obs-sec-title">改动时间线</h3>'
    + '<span class="obs-sec-note">每个文件一条进度条 · 悬停查看该次编辑的 diff</span></div>'
    + '<div class="obs-dtl">'
    + '<div class="obs-dtl-files">' + files.join('') + '</div>'
    + '<div class="obs-dtl-edits">' + timeline + '</div>'
    + '</div>';
}

/* ======================================================================
   P2-1 Agent Timing（时间信号线）
   ----------------------------------------------------------------------
   三态高度编码：思考 = 微抬(4px) / 调用工具 = 拉高(14px) / 等待你 = 琥珀虚线(2px)
   ====================================================================== */
function obsRenderTiming() {
  const trace = OBS.trace || [];
  if (!trace.length) {
    return obsEmpty('本次运行尚无时间信号',
      '时间信号线把每一步画成高度不同的信号：思考微抬、调用工具拉高、'
      + '等待审批是琥珀色虚线。', 'pulse');
  }

  let t0 = Infinity, t1 = -Infinity;
  trace.forEach((e) => {
    const s = Number(e.started_at || e.ts || 0);
    const d = Number(e.duration_ms) || 0;
    if (s) { t0 = Math.min(t0, s); t1 = Math.max(t1, s + d / 1000); }
  });
  if (!Number.isFinite(t0)) t0 = 0;
  if (!Number.isFinite(t1) || t1 <= t0) t1 = t0 + 1;
  const span = Math.max(t1 - t0, 0.001);

  const ticks = [];
  for (let i = 0; i <= 4; i++) {
    const p = i / 4;
    ticks.push('<span class="obs-wf-tick" style="left:' + (p * 100).toFixed(3) + '%">'
      + esc(obsFmtClock(t0 + span * p)) + '</span>');
  }

  const bars = trace.map((e) => {
    const s = Number(e.started_at || e.ts || 0) || t0;
    const d = Number(e.duration_ms) || 0;
    const left = Math.max(0, ((s - t0) / span) * 100);
    const width = Math.max(0.6, (d / 1000 / span) * 100);
    const kind = e.kind === 'approval' ? 'wait' : (e.kind === 'llm' ? 'think' : 'tool');
    const label = kind === 'wait' ? '等待审批' : (kind === 'think' ? '思考' : (e.name || '工具'));
    return '<div class="obs-sig-row">'
      + '<div class="obs-sig-label">' + esc(e.name || '(步骤)') + '</div>'
      + '<div class="obs-sig-track">'
      + '<span class="obs-sig-bar ' + kind + '" style="left:' + left.toFixed(3)
      + '%;width:' + width.toFixed(3) + '%" title="' + esc(label + ' · ' + obsFmtDur(d)) + '">'
      + '</span>'
      + '<span class="obs-sig-lab" style="left:' + left.toFixed(3) + '%">'
      + esc(kind === 'tool' ? label : '') + '</span>'
      + '</div></div>';
  }).join('');

  const legend = '<div class="obs-sig-legend">'
    + '<span class="obs-wf-legend"><span class="obs-wf-legend-dot" '
    + 'style="background:var(--accent-2);height:4px"></span>思考（微抬 4px）</span>'
    + '<span class="obs-wf-legend"><span class="obs-wf-legend-dot" '
    + 'style="background:var(--accent);height:12px"></span>调用工具（拉高 14px）</span>'
    + '<span class="obs-wf-legend"><span class="obs-wf-legend-dot" '
    + 'style="background:transparent;border-top:2px dashed var(--hitl);height:0"></span>'
    + '等待你（琥珀虚线）</span>'
    + '</div>';

  return '<div class="obs-sec-head"><h3 class="obs-sec-title">时间信号线</h3>'
    + '<span class="obs-sec-note">三态高度编码：思考 / 工具 / 等待</span></div>'
    + '<div class="obs-sig"><div class="obs-sig-scroll"><div class="obs-sig-inner">'
    + '<div class="obs-sig-axis">' + ticks.join('') + '</div>'
    + bars
    + '</div></div>' + legend + '</div>';
}

/* ======================================================================
   P2-2 Repo Map（代码库 treemap + Agent 光标）
   ====================================================================== */
function obsRenderRepoMap() {
  const files = OBS.repoFiles || [];
  if (!files.length) {
    return obsEmpty('尚未载入代码库文件树',
      '打开「工作区」视图后，这里会把代码库按文件夹 / 文件画成 treemap，'
      + '文件块按行数定尺寸，Agent 编辑过的文件从下往上填充颜色。', 'map');
  }

  /* ---- 构造层级（目录 → 文件）---- */
  const dirs = new Map();
  files.forEach((f) => {
    const dir = f.dir || '';
    if (!dirs.has(dir)) dirs.set(dir, []);
    dirs.get(dir).push(f);
  });
  const items = [];
  dirs.forEach((list, dir) => {
    const dirLines = list.reduce((a, f) => a + (f.lines || 0), 0);
    items.push({
      kind: 'dir', name: dir || '(根目录)', value: Math.max(dirLines, 1),
      children: list,
    });
  });

  const W = 100, H = 100;
  const rects = obsSquarify(items, 0, 0, W, H);

  const cells = [];
  rects.forEach((r) => {
    const it = r.item;
    const x = r.x / W * 100;
    const y = r.y / H * 100;
    const w = r.w / W * 100;
    const h = r.h / H * 100;
    cells.push('<div class="obs-repo-cell is-dir" style="left:' + x.toFixed(3)
      + '%;top:' + y.toFixed(3) + '%;width:' + w.toFixed(3) + '%;height:' + h.toFixed(3) + '%"'
      + ' title="' + esc(it.name + ' · ' + it.children.length + ' 个文件') + '">'
      + '<span class="obs-repo-cell-name">' + esc(it.name) + '</span></div>');
    /* 文件块：按行数在目录块内再排一层 */
    const inner = obsSquarify(
      it.children.map((f) => ({
        kind: 'file', name: f.name, value: Math.max(f.lines || 1, 1),
        path: f.path, edited: !!f.edited,
      })),
      r.x, r.y, r.w, r.h);
    inner.forEach((fr) => {
      const fx = fr.x / W * 100;
      const fy = fr.y / H * 100;
      const fw = fr.w / W * 100;
      const fh = fr.h / H * 100;
      const f = fr.item;
      const edited = f.edited ? ' is-edited' : '';
      cells.push('<button type="button" class="obs-repo-cell is-file' + edited + '"'
        + ' style="left:' + fx.toFixed(3) + '%;top:' + fy.toFixed(3) + '%;width:' + fw.toFixed(3)
        + '%;height:' + fh.toFixed(3) + '%"'
        + ' data-path="' + esc(f.path || '') + '"'
        + ' title="' + esc(f.name + ' · ' + f.value + ' 行'
          + (f.edited ? ' · Agent 已编辑' : '')) + '">'
        + '<span class="obs-repo-cell-name">' + esc(f.name) + '</span></button>');
      if (f.edited) {
        cells.push('<span class="obs-repo-cursor" style="left:' + (fx + fw - 6).toFixed(3)
          + '%;top:' + (fy + fh - 6).toFixed(3) + '%" title="Agent 编辑光标"></span>');
      }
    });
  });

  const totalLines = files.reduce((a, f) => a + (f.lines || 0), 0);
  const editedCount = files.filter((f) => f.edited).length;

  const side = '<div class="obs-repo-side">'
    + '<div class="obs-repo-stat"><span>文件总数</span><b>' + files.length + '</b></div>'
    + '<div class="obs-repo-stat"><span>总行数</span><b>' + totalLines + '</b></div>'
    + '<div class="obs-repo-stat"><span>Agent 已编辑</span><b>' + editedCount + '</b></div>'
    + '<div class="obs-repo-stat"><span>目录数</span><b>' + dirs.size + '</b></div>'
    + '<div class="obs-hint" style="margin-top:var(--sp-2)">'
    + '文件块面积按**行数**定尺寸（treemap 面积正比于面积）。'
    + 'Agent 编辑过的文件用绿色底并带光标点。</div>'
    + '</div>';

  return '<div class="obs-sec-head"><h3 class="obs-sec-title">代码库图</h3>'
    + '<span class="obs-sec-note">按行数定尺寸 · 绿色 = Agent 已编辑</span></div>'
    + '<div class="obs-repo">'
    + '<div class="obs-repo-tree" role="group" aria-label="代码库 treemap">' + cells.join('') + '</div>'
    + side
    + '</div>';
}

/* ======================================================================
   P2-3 Autonomy Dial（三档自主性）
   ====================================================================== */
const OBS_DIAL = [
  { id: 'ask', label: '先问你', desc: '每次写操作前都弹出人工确认（Ask / 严格 HITL）。' },
  { id: 'plan', label: '只规划', desc: '只生成执行计划，不动手（Plan 模式）。' },
  { id: 'agent', label: '自己做', desc: 'Agent 自主执行，遇到高风险动作才挂起审批（Agent 模式）。' },
];

function obsRenderAutonomy() {
  /* ---- 档位定义缺失时的空态（不猜默认档位）---- */
  if (!OBS_DIAL || !OBS_DIAL.length) {
    return obsEmpty('尚无可用的自主性档位',
      '自主性档位需要三档（先问你 / 只规划 / 自己做）配置。'
      + '当前环境没有读到档位定义，无法渲染旋钮。', 'dial');
  }
  const cur = OBS.autonomy || state.mode || 'agent';
  const idx = Math.max(0, OBS_DIAL.findIndex((d) => d.id === cur));
  const segs = OBS_DIAL.map((d, i) => {
    return '<button type="button" class="obs-dial-seg' + (i === idx ? ' is-on' : '')
      + (i === 1 ? ' mid' : '') + '" role="radio" aria-checked="' + (i === idx ? 'true' : 'false')
      + '" data-dial="' + d.id + '" title="' + esc(d.desc) + '">' + esc(d.label) + '</button>';
  }).join('');
  const knobLeft = (idx / (OBS_DIAL.length - 1)) * 100;
  const d = OBS_DIAL[idx];

  return '<div class="obs-sec-head"><h3 class="obs-sec-title">自主性档位</h3>'
    + '<span class="obs-sec-note">映射到现有 mode（agent / ask / plan）与 HITL 策略</span></div>'
    + '<div class="obs-dial" role="radiogroup" aria-label="自主性档位">'
    + '<div class="obs-dial-track">'
    + segs
    + '<span class="obs-dial-knob" style="left:calc(' + knobLeft.toFixed(2) + '% - 1px)"></span>'
    + '</div>'
    + '<div class="obs-dial-desc">'
    + '<span class="obs-dial-note">' + esc(d.desc) + '</span>'
    + '<span class="obs-dial-note">当前模式：<b class="mono">'
    + esc(state.mode || 'agent') + '</b>；本轮 HITL 策略：'
    + (OBS.approvals && OBS.approvals.length
        ? '<b>已挂起 ' + OBS.approvals.length + ' 项待审批</b>'
        : '<b>本轮无待审批</b>')
    + '</span>'
    + '</div>'
    + '<div class="obs-hint">切换档位会同步设置对话页的交互模式'
    + '（Ask / Plan / Agent），与后端 <span class="mono">mode</span> 参数一致。</div>'
    + '</div>';
}

/** 绑定自主性档位切换 */
function obsBindAutonomy(page) {
  const root = page.querySelector('#obs-view-autonomy');
  if (!root || root.dataset.dialBound === '1') return;
  root.dataset.dialBound = '1';
  root.addEventListener('click', (e) => {
    const seg = e.target.closest('.obs-dial-seg');
    if (!seg) return;
    const id = seg.dataset.dial;
    OBS.autonomy = id;
    if (typeof setMode === 'function') setMode(id);
    obsRender();
  });
}

/* ======================================================================
   P2-4 Reasoning Summary（推理摘要卡）
   ----------------------------------------------------------------------
   **合规边界**：只展示后端下发的 `reasoning_summary`（已被压成前 1-2 句并截断），
   绝不展示原始思维链（`reasoning_content` 不入历史、不进 SSE）。
   ====================================================================== */
function obsRenderReasoning() {
  const trace = OBS.trace || [];
  const withReason = trace.filter((e) => e.reasoning_summary);
  if (!trace.length) {
    return obsEmpty('本次运行尚无推理摘要',
      '推理摘要卡按类型着色显示每类耗时（思考 / 工具 / 结果）。'
      + '只展示后端截断后的**可解释动作摘要**，不展示原始思维链。', 'brain');
  }
  if (!withReason.length) {
    return obsEmpty('本轮模型未返回可展示的推理摘要',
      '这是正常现象 —— 摘要缺失时我们**不伪造**任何内容。'
      + '下方仍会显示本轮各类步骤的耗时分布。', 'brain');
  }

  /* ---- 按类型分段 ---- */
  const segs = [
    { k: 'think', name: '思考', ms: 0 },
    { k: 'tool', name: '工具', ms: 0 },
    { k: 'result', name: '结果', ms: 0 },
    { k: 'wait', name: '等待', ms: 0 },
    { k: 'err', name: '失败', ms: 0 },
  ];
  trace.forEach((e) => {
    const d = Number(e.duration_ms) || 0;
    if (e.kind === 'approval') segs[3].ms += d;
    else if (e.kind === 'llm') segs[0].ms += d;
    else if (e.ok === false) segs[4].ms += d;
    else segs[1].ms += d;
    if (e.result !== undefined && e.result !== null) segs[2].ms += d;
  });
  const total = segs.reduce((a, s) => a + s.ms, 0) || 1;

  const bar = segs.map((s) => {
    const pct = (s.ms / total) * 100;
    if (pct <= 0) return '';
    return '<span class="obs-rs-seg k-' + s.k + '" style="width:' + pct.toFixed(2) + '%"'
      + ' title="' + esc(s.name + ' · ' + obsFmtDur(s.ms)) + '"></span>';
  }).join('');

  const legend = segs.map((s) => {
    if (s.ms <= 0) return '';
    return '<span class="obs-rs-legend-item">'
      + '<span class="obs-wf-legend-dot" style="background:var(--'
      + (s.k === 'think' ? 'accent-2' : s.k === 'tool' ? 'accent'
        : s.k === 'result' ? 'ok' : s.k === 'err' ? 'err' : 'hitl') + ')"></span>'
      + esc(s.name) + ' ' + esc(obsFmtDur(s.ms)) + '</span>';
  }).join('');

  const summaries = withReason.slice(0, 8).map((e, i) => {
    return '<details class="obs-tc" data-cid="' + esc(e.id || ('#' + e.seq)) + '">'
      + '<summary>'
      + '<svg class="obs-tc-caret" viewBox="0 0 16 16" width="12" height="12" fill="none"'
      + ' stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M6 4l4 4-4 4"/></svg>'
      + '<span class="obs-dot ok"></span>'
      + '<span class="obs-tc-name">步骤 ' + esc(String(e.seq || i + 1)) + '</span>'
      + '<span class="obs-tc-sum">' + esc(e.reasoning_summary) + '</span>'
      + '<span class="obs-tc-dur">'
      + esc(Number.isFinite(Number(e.duration_ms)) ? obsFmtDur(e.duration_ms) : '–') + '</span>'
      + '</summary>'
      + '<div class="obs-tc-body">'
      + '<div class="obs-hint">以下为**模型推理摘要（已截断）**，'
      + '不是原始思维链；后端不把 <span class="mono">reasoning_content</span> 写入历史或 SSE。</div>'
      + '<div class="obs-tb-text" style="margin-top:var(--sp-2)">'
      + esc(e.reasoning_summary) + '</div>'
      + '</div></details>';
  }).join('');

  return '<div class="obs-sec-head"><h3 class="obs-sec-title">推理摘要</h3>'
    + '<span class="obs-sec-note">按类型着色的分段条 · 只展示截断后的可解释摘要</span></div>'
    + '<div class="obs-rs">'
    + '<div class="obs-rs-bar" role="img" aria-label="各类步骤耗时分布">' + bar + '</div>'
    + '<div class="obs-rs-legend">' + legend + '</div>'
    + '<div style="margin-top:var(--sp-2)">'
    + '<div class="obs-rs-key"><span>参与模型</span><b>'
    + esc(state.model || 'gpt-oss-20b') + '</b></div>'
    + '<div class="obs-rs-key"><span>本轮总耗时（各步之和）</span><b>'
    + esc(obsFmtDur(total)) + '</b></div>'
    + '<div class="obs-rs-key"><span>有摘要的步骤数</span><b>'
    + withReason.length + ' / ' + trace.length + '</b></div>'
    + '</div>'
    + '</div>'
    + '<div style="margin-top:var(--sp-3)">' + summaries + '</div>';
}

/* ======================================================================
   跨组件动作
   ====================================================================== */

/** 从瀑布 / 时序图跳到工具调用
 *
 * **Live Timeline 是主视图**：优先在时间线里定位对应节点并展开它；
 * 若该节点不在当前筛选下，则临时切到「全部」筛选再定位。
 */
function obsFocusToolCall(cid) {
  if (!cid) return;
  const page = document.querySelector('.page[data-page="' + OBS.tabId + '"]');
  if (!page) return;

  /* ---- 优先在 Live Timeline 里定位 ---- */
  if (typeof tlFocusNode === 'function') {
    if (tlFocusNode(cid)) return;
  }
  /* ---- 兜底：切到「全部视图」的工具调用卡 ---- */
  if (typeof tlSwitchView === 'function') tlSwitchView('gallery');
  OBS.view = 'toolcards';
  if (typeof tlRenderPanel === 'function') {
    tlRenderPanel(page);
    setTimeout(() => {
      const card = page.querySelector('.obs-tc[data-cid="' + cssEscape(cid) + '"]');
      if (card) {
        card.open = true;
        card.scrollIntoView({ block: 'center' });
        card.style.borderColor = 'var(--accent)';
        setTimeout(() => { card.style.borderColor = ''; }, 1200);
      }
    }, 80);
    return;
  }
  obsSwitchView(page, 'toolcards');
  setTimeout(() => {
    const card = page.querySelector('.obs-tc[data-cid="' + cssEscape(cid) + '"]');
    if (card) {
      card.open = true;
      card.scrollIntoView({ block: 'center' });
      card.style.borderColor = 'var(--accent)';
      setTimeout(() => { card.style.borderColor = ''; }, 1200);
    }
  }, 60);
}

/* ======================================================================
   初始化
   ---------------------------------------------------------------------- */
function obsInit() {
  /* ---- 入口按钮：对话 tab 顶部 ---- */
  const head = document.querySelector('.chat-head');
  if (head && !head.querySelector('#btn-obs-view')) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'head-btn';
    btn.id = 'btn-obs-view';
    btn.title = '打开运行观测面板（时间瀑布 / 工具调用卡 / 时序图 等 12 个视图）';
    btn.setAttribute('aria-label', '打开运行观测面板');
    btn.innerHTML = obsIcon('activity', 13) + '<span>运行观测</span>';
    btn.addEventListener('click', () => obsOpenPanel());
    const status = head.querySelector('#m-status');
    if (status && status.parentNode) {
      status.parentNode.insertBefore(btn, status);
    } else {
      head.appendChild(btn);
    }
  }

  /* ---- 自主性档位的额外绑定（视图切换后再绑）---- */
  const page = document.querySelector('.page[data-page="' + OBS.tabId + '"]');
  if (page) obsBindAutonomy(page);
}

/** 供 app.js 在 SSE 事件处调用 */
function obsHandleSse(obj) {
  obsOnSse(obj);
}

/** 供 app.js 在切换到观测 tab 时调用（重绘 + 补绑） */
function obsOnTabActivated(id) {
  if (id !== OBS.tabId) return;
  const page = document.querySelector('.page[data-page="' + OBS.tabId + '"]');
  if (!page) return;
  obsBindAutonomy(page);
  obsRender();
}

/** 新建会话时清空观测快照（否则会看到上一轮的轨迹） */
function obsReset() {
  OBS.trace = [];
  OBS.budget = null;
  OBS.approvals = [];
  OBS.edits = [];
  OBS.startedAt = null;
  OBS.runStart = null;
  OBS.prompt = '';
  OBS.answer = '';
  OBS.finished = false;
  /* 后端下发的实测 system prompt 字符数也属于「上一轮」——
     不清掉的话上下文占用 treemap 会残留旧数据，空态出不来。 */
  OBS.systemPromptChars = 0;
  /* 新建会话：不恢复上一轮的轨迹（否则新会话会看到上一轮的数据）*/
  OBS.suppressRestore = true;
  obsStopTimer();
  /* 权威快照也在 trace_view.js 里，一并清空 ——
     否则 obsSyncTrace() 会把上一轮的 TV_TRACE.current 又灌回来。 */
  if (typeof TV_TRACE !== 'undefined') {
    TV_TRACE.current = [];
    TV_TRACE.budget = null;
  }
  const page = document.querySelector('.page[data-page="' + OBS.tabId + '"]');
  if (page) {
    const status = page.querySelector('#obs-status');
    if (status) status.textContent = '本次运行尚无数据';
    if (typeof tlRenderPanel === 'function') {
      tlRenderPanel(page);
      /* 「全部视图」画廊里的子视图也要重画，否则留着上一轮的内容 */
      if (typeof tlRenderGallerySub === 'function') tlRenderGallerySub(page);
      return;
    }
    const host = page.querySelector('#obs-view-' + OBS.view);
    if (host) {
      const renderers = {
        waterfall: obsRenderWaterfall, toolcards: obsRenderToolCards,
        sequence: obsRenderSequence, cost: obsRenderCost,
        context: obsRenderContext, approval: obsRenderApproval,
        difftimeline: obsRenderDiffTimeline, timing: obsRenderTiming,
        repomap: obsRenderRepoMap, autonomy: obsRenderAutonomy,
        reasoning: obsRenderReasoning,
      };
      const fn = renderers[OBS.view];
      host.innerHTML = fn ? fn() : '';
    }
  }
}

/** 从工作区文件树同步文件清单（Repo Map 用） */
function obsSyncRepoFiles(files) {
  if (!Array.isArray(files) || !files.length) return;
  OBS.repoFiles = files.map((f) => ({
    path: f.path || '',
    name: f.name || (f.path || '').split('/').pop(),
    dir: f.dir || ((f.path || '').includes('/')
      ? f.path.slice(0, f.path.lastIndexOf('/')) : ''),
    lines: Number(f.lines || f.size_lines || 0) || 0,
    edited: !!f.edited,
  }));
}
