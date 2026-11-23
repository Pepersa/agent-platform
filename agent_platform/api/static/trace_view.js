/* ==========================================================================
   工具详情 + 决策轨迹（Decision Trace）—— 原生 JS，零第三方库、零 CDN
   --------------------------------------------------------------------------
   本文件负责两块用户报障能力：

   1. **工具详情可点开查看**
        · 侧栏「工具」列表里点工具名 → 主区**新开一个 tab**（Cursor 的
          「打开文件看内容」语义），展示完整契约：
          名称 / 描述全文 / 版本 / 权限 / HITL / 超时 / 重试 / 幂等
          / metadata / input_schema（**参数表 + 原始 JSON 折叠**）
          / output_schema / **OpenAI function calling 形态**（模型实际看到的）
        · 选型理由见文件末尾「为什么用 tab 而不是侧栏展开」。

   2. **Agent 决策轨迹可视化**
        · 每步工具调用渲染成「折叠块」：图标 + 工具名 + 参数摘要 + 耗时 + 状态色
        · 展开后分区展示：参数 / 结果 / 错误（JSON 语法着色）
        · 对话 tab 顶部有「决策轨迹」入口 → 打开**完整决策链时间线**
          （思考 → 决策 → 动作 → 观察 → 下一步），并显示预算消耗
        · 历史会话恢复出来的消息同样能渲染轨迹

   注意：**关于 reasoning 的处理（合规要求）**
      本模块**不展示模型的原始思维链（chain-of-thought）**。
      后端只下发 `reasoning_summary` —— 被压成前 1-2 句并硬截断的
      「可解释动作摘要」，前端标注为「模型推理摘要（截断）」。
      摘要缺失时显示「本轮模型未返回可展示的推理摘要」，**绝不伪造**。
   ========================================================================== */
'use strict';

/* ----------------------------------------------------------------------
   通用：JSON 语法着色（零依赖，纯正则 + 转义）
   ---------------------------------------------------------------------- */
const JSON_TOKENS = [
  { re: /"(?:[^"\\]|\\.)*"(?=\s*:)/g, cls: 'tok-property' },
  { re: /"(?:[^"\\]|\\.)*"/g, cls: 'tok-string' },
  { re: /-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b/g, cls: 'tok-number' },
  { re: /\b(?:true|false|null)\b/g, cls: 'tok-keyword' },
];

/** 把任意值渲染成带语法着色的 HTML（对象走 JSON pretty-print）。 */
function tvJsonHtml(value) {
  let text;
  if (typeof value === 'string') {
    text = value;
  } else {
    try {
      text = JSON.stringify(value, null, 2);
    } catch (e) {
      text = String(value);
    }
  }
  if (text == null) return '';
  // 先做 HTML 转义占位（避免着色破坏结构），再逐 token 着色
  const marks = [];
  JSON_TOKENS.forEach((rule) => {
    rule.re.lastIndex = 0;
    let m;
    while ((m = rule.re.exec(text)) !== null) {
      if (m[0] === '') { rule.re.lastIndex++; continue; }
      marks.push({ start: m.index, end: m.index + m[0].length, cls: rule.cls });
    }
  });
  marks.sort((a, b) => a.start - b.start);
  let html = '';
  let last = 0;
  marks.forEach((mk) => {
    if (mk.start < last) return;   // 重叠 → 跳过（保留先命中的）
    html += esc(text.slice(last, mk.start));
    html += '<span class="' + mk.cls + '">' + esc(text.slice(mk.start, mk.end)) + '</span>';
    last = mk.end;
  });
  html += esc(text.slice(last));
  return html;
}

/** 带复制按钮的 JSON 代码块 */
function tvJsonBlock(id, value) {
  return '<div class="tv-json-wrap">'
    + '<pre class="mono tv-json" id="' + esc(id) + '">' + tvJsonHtml(value) + '</pre>'
    + '<button type="button" class="tv-copy" data-target="' + esc(id) + '">复制</button>'
    + '</div>';
}

/** 参数摘要：把入参压成一行短文本（用于折叠头） */
function tvArgsSummary(args) {
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

/* ----------------------------------------------------------------------
   通用：Lucide 风格内联 SVG 图标（16×16 viewBox，1.5 线宽）
   ---------------------------------------------------------------------- */
const TV_ICONS = {
  'tool': '<path d="M14.1 6.3a3.8 3.8 0 0 1-4.8 4.8l-4.6 4.6a1.9 1.9 0 0 1-2.7-2.7l4.6-4.6a3.8 3.8 0 0 1 4.8-4.8L9.3 5.7l1.1 1.1z"/>',
  'route': '<circle cx="4" cy="4" r="1.8"/><circle cx="12" cy="12" r="1.8"/>'
        + '<path d="M5.6 5.1 8 7.5a2 2 0 0 1 .5 1.3v1.7"/>'
        + '<path d="M10.4 10.9 8 8.5A2 2 0 0 1 7.5 7.2v-.7"/>',
  'check': '<path d="M3 8.4 6.4 12 13 4.6"/>',
  'cross': '<path d="M4 4l8 8M12 4l-8 8"/>',
  'clock': '<circle cx="8" cy="8" r="5.6"/><path d="M8 4.8v3.4l2.2 1.3"/>',
  'chevron-right': '<path d="M6 3.6 10.4 8 6 12.4"/>',
  'chevron-down': '<path d="M3.6 6 8 10.4 12.4 6"/>',
  'brain': '<path d="M6.2 3.2a2 2 0 0 0-2 2 2 2 0 0 0-1 3.4 2 2 0 0 0 .6 3.7 2 2 0 0 0 2.4 2.7"/>'
         + '<path d="M9.8 3.2a2 2 0 0 1 2 2 2 2 0 0 1 1 3.4 2 2 0 1 1-.6 3.7 2 2 0 0 1-2.4 2.7"/>'
         + '<path d="M8 3.4v12.2"/>',
  'gauge': '<path d="M2.8 11.4a5.6 5.6 0 1 1 10.4 0"/><path d="M8 11.4 10.6 7"/>',
  'copy': '<rect x="5.5" y="5.5" width="8" height="8" rx="1.4"/>'
        + '<path d="M10.5 3.5v-.6a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h.6"/>',
  'doc': '<path d="M9 1.8H4.6A1.6 1.6 0 0 0 3 3.4v9.2a1.6 1.6 0 0 0 1.6 1.6h6.8a1.6 1.6 0 0 0 1.6-1.6V5.6z"/>'
      + '<path d="M9 1.8v3.8h4"/>',
  'search': '<circle cx="7" cy="7" r="4.4"/><path d="M10.2 10.2 13.6 13.6"/>',
};

function tvIcon(name, size) {
  const body = TV_ICONS[name] || TV_ICONS['tool'];
  const s = size || 14;
  return '<svg class="ico" viewBox="0 0 16 16" width="' + s + '" height="' + s + '"'
    + ' fill="none" stroke="currentColor" stroke-width="1.5"'
    + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + body + '</svg>';
}

/* ----------------------------------------------------------------------
   三态状态判定（2026-11-24 修复：待审批被渲染成绿色 ✓ 成功）
   ----------------------------------------------------------------------
   真实故障：用户看到 ws_write_file 标着「✓ 成功」，但**文件根本没落地** ——
   因为该工具正**挂在人工审批上、尚未执行**。

   根因：旧判定是 ``const ok = entry.ok !== false``。后端当时把
   「已发起但没有结果」也算成 ``ok=true``，于是未执行 → 绿色 ✓「成功」。

   现在后端改为**三态**：
     · ok === true  → 已执行且成功（绿色 ✓「成功」）
     · ok === false → 已执行但失败（红色 ✗「失败」）
     · ok === null  → **已发起，等待人工审批**（琥珀色 ⏱「待审批」）

   前端必须显式识别第三态，**不能**再用 ``ok !== false``。
   ---------------------------------------------------------------------- */

/** 轨迹条目的三态：'success' | 'error' | 'pending' */
function tvEntryState(entry) {
  if (!entry) return 'pending';
  /* 后端给了明确的 status —— 优先采信 */
  const st = String(entry.status || '').toLowerCase();
  if (st === 'pending_approval' || st === 'pending') return 'pending';
  if (st === 'error' || st === 'failed') return 'error';
  if (st === 'success' || st === 'ok') return 'success';
  /* 没有 status 时按 ok 的三态推断（ok 为 null/undefined = 待审批） */
  if (entry.ok === null || entry.ok === undefined) {
    /* kind === 'approval' 的条目本来就是「等待确认」 */
    return 'pending';
  }
  return entry.ok === false ? 'error' : 'success';
}

/** 待审批条目的提示文案（tooltip） */
const TV_PENDING_HINT = '该工具已发起，等待你点击「批准」后才会执行';

/** 三态对应的图标：琥珀色时钟 / 绿色对勾 / 红色叉 */
function tvStateIcon(state, size) {
  if (state === 'pending') {
    return '<svg class="ico" viewBox="0 0 16 16" width="' + (size || 11) + '"'
      + ' height="' + (size || 11) + '" fill="none" stroke="currentColor"'
      + ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"'
      + ' aria-hidden="true">'
      + '<circle cx="8" cy="8" r="5.8"/><path d="M8 4.8v3.4l2.2 1.3"/></svg>';
  }
  return tvIcon(state === 'success' ? 'check' : 'cross', size);
}

/* ----------------------------------------------------------------------
   任务 1：工具详情（新开 tab）
   ---------------------------------------------------------------------- */

/** 工具详情 tab 的 id（同一工具复用同一个 tab） */
function tvToolTabId(name) {
  return 'pg-tool-' + String(name).replace(/[^a-zA-Z0-9]/g, '_');
}

/** 工具详情 tab 的 HTML 骨架 */
function tvToolPage() {
  return '<div class="tv-page" id="tv-tool-page">'
    + '<div class="tv-tool-head">'
    + '<span class="tv-tool-ico">' + tvIcon('tool', 16) + '</span>'
    + '<span class="tv-tool-name mono" id="tv-tool-name">加载中…</span>'
    + '<span class="grow"></span>'
    + '<span id="tv-tool-badges"></span>'
    + '</div>'
    + '<div class="tv-tool-body" id="tv-tool-body"><div class="muted">加载中…</div></div>'
    + '</div>';
}

/** 打开工具详情（tab 形态） */
async function tvOpenTool(name) {
  if (!name) return;
  const id = tvToolTabId(name);
  let tab = state.tabs.find((t) => t.id === id);
  if (!tab) {
    tab = openTab('tool', name, { name: name }, id);
  }
  activateTab(id);
  const page = document.querySelector('.page[data-page="' + id + '"]');
  if (!page) return;
  if (!page.querySelector('#tv-tool-body')) {
    page.innerHTML = tvToolPage();
  }
  const bodyEl = page.querySelector('#tv-tool-body');
  const nameEl = page.querySelector('#tv-tool-name');
  const badgeEl = page.querySelector('#tv-tool-badges');
  if (nameEl) nameEl.textContent = name;
  if (bodyEl) bodyEl.innerHTML = '<div class="muted">加载中…</div>';
  try {
    const res = await api('/tools/' + encodeURIComponent(name));
    const tool = res.tool || {};
    if (nameEl) nameEl.textContent = tool.name || name;
    if (badgeEl) badgeEl.innerHTML = tvToolBadges(tool);
    if (bodyEl) bodyEl.innerHTML = tvToolBodyHtml(tool, res);
    tvBindToolPage(page);
  } catch (err) {
    if (bodyEl) {
      bodyEl.innerHTML = '<div class="pill pill-err">加载失败：' + esc(err.message) + '</div>';
    }
  }
}

/** 顶部徽标：权限 / HITL / 版本 */
function tvToolBadges(tool) {
  const out = [];
  out.push('<span class="perm ' + esc(tool.permission || '') + '">'
    + esc(tool.permission || '–') + '</span>');
  if (tool.hitl_required) {
    out.push('<span class="pill pill-wait">需人工确认</span>');
  } else {
    out.push('<span class="pill pill-ok">自动执行</span>');
  }
  out.push('<span class="kind-tag">v' + esc(tool.version || '1.0.0') + '</span>');
  return out.join('');
}

/** 参数表（参数名 / 类型 / 必填 / 说明） */
function tvParamsTable(params) {
  if (!params || !params.length) {
    return '<div class="muted sm">该工具没有声明任何入参（无参数工具）。</div>';
  }
  const rows = params.map((p) => {
    const bits = [];
    if (p.default !== null && p.default !== undefined && p.default !== '') {
      bits.push('默认 ' + esc(JSON.stringify(p.default)));
    }
    if (p.enum && p.enum.length) {
      bits.push('取值 ' + esc(p.enum.map((v) => JSON.stringify(v)).join(' | ')));
    }
    if (p.minimum !== null && p.minimum !== undefined) bits.push('最小 ' + esc(p.minimum));
    if (p.maximum !== null && p.maximum !== undefined) bits.push('最大 ' + esc(p.maximum));
    if (p.example !== null && p.example !== undefined) {
      bits.push('示例 ' + esc(JSON.stringify(p.example)));
    }
    return '<tr>'
      + '<td class="mono tv-p-name">' + esc(p.name) + '</td>'
      + '<td class="mono tv-p-type">' + esc(p.type || 'any') + '</td>'
      + '<td class="tv-p-req">' + (p.required
          ? '<span class="pill pill-err">必填</span>'
          : '<span class="muted sm">可选</span>') + '</td>'
      + '<td class="tv-p-desc">' + (esc(p.description || '–')
          + (bits.length ? '<div class="tv-p-bits">' + bits.map(esc).join(' · ') + '</div>' : ''))
        + '</td>'
      + '</tr>';
  }).join('');
  return '<table class="tv-params">'
    + '<thead><tr>'
    + '<th>参数名</th><th>类型</th><th>必填</th><th>说明</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table>';
}

/** 详情正文 */
function tvToolBodyHtml(tool, res) {
  const meta = tool.metadata || {};
  const metaRows = Object.keys(meta).length
    ? Object.keys(meta).map((k) => '<div class="tv-kv">'
        + '<span>' + esc(k) + '</span><b>' + esc(tvMetaValue(meta[k])) + '</b></div>').join('')
    : '<div class="muted sm">（该工具没有额外 metadata）</div>';

  const outSchema = tool.output_schema && Object.keys(tool.output_schema).length
    ? tvJsonBlock('tv-json-out', tool.output_schema)
    : '<div class="muted sm">（该工具未声明 output_schema）</div>';

  const openaiFn = res.openai_function || tool.openai_function || null;

  return ''
    /* ---- 描述全文（不截断） ---- */
    + '<section class="tv-sec">'
    + '<h4>说明</h4>'
    + '<div class="tv-desc">' + esc(tool.description || '（无描述）') + '</div>'
    + '</section>'

    /* ---- 基本契约 ---- */
    + '<section class="tv-sec">'
    + '<h4>基本契约</h4>'
    + '<div class="tv-kv"><span>名称</span><b class="mono">' + esc(tool.name || '') + '</b></div>'
    + '<div class="tv-kv"><span>版本</span><b class="mono">' + esc(tool.version || '–') + '</b></div>'
    + '<div class="tv-kv"><span>权限</span><b class="mono">' + esc(tool.permission || '–') + '</b></div>'
    + '<div class="tv-kv"><span>需要人工确认</span><b>'
      + (tool.hitl_required ? '是（写操作会挂起等待审批）' : '否') + '</b></div>'
    + '<div class="tv-kv"><span>超时</span><b class="mono">'
      + esc(tool.timeout_ms != null ? tool.timeout_ms + ' ms' : '–') + '</b></div>'
    + '<div class="tv-kv"><span>最大重试</span><b class="mono">'
      + esc(tool.max_retries != null ? tool.max_retries : '–') + '</b></div>'
    + '<div class="tv-kv"><span>幂等</span><b>'
      + (tool.idempotent ? '是（重复调用无副作用）' : '否（重复调用会产生副作用）') + '</b></div>'
    + '</section>'

    /* ---- 入参：参数表 + 原始 JSON ---- */
    + '<section class="tv-sec">'
    + '<h4>入参参数（input_schema）</h4>'
    + tvParamsTable(res.params || tool.params)
    + '<details class="tv-raw"><summary>查看原始 JSON Schema</summary>'
    + tvJsonBlock('tv-json-input', tool.input_schema || {})
    + '</details>'
    + '</section>'

    /* ---- 出参 ---- */
    + '<section class="tv-sec">'
    + '<h4>出参（output_schema）</h4>'
    + outSchema
    + '</section>'

    /* ---- metadata ---- */
    + '<section class="tv-sec">'
    + '<h4>元信息（metadata：影响范围 / 可回滚性 / 审批要求等）</h4>'
    + '<div class="tv-kv-list">' + metaRows + '</div>'
    + '</section>'

    /* ---- OpenAI function calling 形态 ---- */
    + '<section class="tv-sec">'
    + '<h4>模型实际看到的定义（OpenAI function calling）</h4>'
    + '<div class="muted sm">下面是该工具下发給模型时的完整 JSON —— '
      + '模型据此决定「要不要调、调时传什么参数」。</div>'
    + tvJsonBlock('tv-json-openai', openaiFn || {})
    + '</section>'

    /* ---- 审计 ---- */
    + '<section class="tv-sec">'
    + '<h4>审计</h4>'
    + '<div class="muted sm">该工具的执行会写入以下审计动作：'
    + esc((res.audit_actions || ['tool_call']).join('、')) + '</div>'
    + '</section>';
}

/** metadata 值的可读化 */
function tvMetaValue(v) {
  if (v === null || v === undefined) return '–';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** 详情页内的交互（复制 JSON） */
function tvBindToolPage(page) {
  if (!page || page.dataset.tvBound === '1') return;
  page.dataset.tvBound = '1';
  page.addEventListener('click', (e) => {
    const btn = e.target.closest('.tv-copy');
    if (!btn) return;
    const target = btn.dataset.target;
    const pre = page.querySelector('#' + target);
    if (!pre) return;
    const text = pre.textContent || '';
    copyText(text).then(() => {
      btn.classList.add('done');
      const old = btn.getAttribute('data-label') || btn.textContent;
      btn.setAttribute('data-label', old);
      btn.textContent = '已复制';
      setTimeout(() => { btn.classList.remove('done'); btn.textContent = old; }, 1400);
    });
  });
}

/* ----------------------------------------------------------------------
   任务 2：决策轨迹
   ---------------------------------------------------------------------- */
/** 全局轨迹缓存：{threadId: {trace: [], budget: {}}} */
const TV_TRACE = {
  byThread: new Map(),
  current: [],
  budget: null,
  /** **按 kind 精确统计的条目数**（2026-11-24 新增）。
   *  轨迹里混着 llm / tool / approval 三种条目 —— 拿总长度当「轮次」
   *  会导致「9/8」这种越界显示。轮次 = llm 条目数，工具调用 = tool 条目数。*/
  counts: { llm: 0, tool: 0, approval: 0 },
};

/** 按 kind 统计轨迹条目数（llm=轮次，tool=工具调用，approval=审批等待） */
function tvCountByKind(trace) {
  const c = { llm: 0, tool: 0, approval: 0 };
  (trace || []).forEach((e) => {
    const k = String((e && e.kind) || 'tool');
    if (k === 'llm') c.llm += 1;
    else if (k === 'approval') c.approval += 1;
    else c.tool += 1;
  });
  return c;
}

/** 把一条轨迹项累积进当前会话 */
function tvPushTrace(threadId, trace, budget) {
  if (!trace) return;
  const key = threadId || state.threadId || 'current';
  TV_TRACE.byThread.set(key, { trace: trace, budget: budget || null });
  TV_TRACE.current = trace;
  TV_TRACE.counts = tvCountByKind(trace);
  if (budget) TV_TRACE.budget = budget;
}

/** 供外部（历史会话恢复）灌入轨迹 */
function tvSetTrace(threadId, trace, budget) {
  TV_TRACE.byThread.set(threadId || 'current', { trace: trace || [], budget: budget || null });
  TV_TRACE.counts = tvCountByKind(trace);
  if (!threadId || threadId === state.threadId) {
    TV_TRACE.current = trace || [];
    if (budget) TV_TRACE.budget = budget;
  }
}

/** 取某会话的轨迹 */
function tvGetTrace(threadId) {
  const key = threadId || state.threadId || 'current';
  const rec = TV_TRACE.byThread.get(key);
  return rec ? rec.trace : [];
}

/* ---- 单步工具调用折叠块 ---- */

/**
 * 渲染一个工具调用折叠行（Cursor / Claude Code 风格）。
 * @param {HTMLElement} wrap 消息容器
 * @param {Object} entry 轨迹项 {seq,name,args,result,ok,error,duration_ms,...}
 */
function tvAddTraceRow(wrap, entry) {
  if (!wrap || !entry) return;
  const box = wrap.querySelector('.toolrows') || (function () {
    const b = document.createElement('div');
    b.className = 'toolrows';
    wrap.appendChild(b);
    return b;
  })();

  /* ---- 去重（2026-11-22 修复）----
   * 同一个工具调用会被两个来源各渲染一次：
   *   a) SSE `step` 事件里的 `tool_calls`（只有参数、没有结果）
   *   b) SSE `trace` 事件里的累积快照（参数 + 结果，是权威来源）
   * 再加上 `addToolRow` 生成的纯文本行，同一次调用会显示 3 行。
   * 修复：以 tool_call_id 为主键在 .toolrows 内去重；
   *      若已存在则**就地补全**（把结果补进已有行），不新增行。
   */
  const cid = String(entry.id || entry.tool_call_id || '');
  const key = cid || ('#' + (entry.seq || ''));
  const existing = Array.from(box.querySelectorAll('.tv-trace-row')).find((r) => {
    return (r.dataset.cid || '') === key;
  });
  if (existing) {
    // 已有行：若这次带来了结果，就补一个「输出结果」分区
    if (entry.result !== undefined && entry.result !== null) {
      const body = existing.querySelector('.tv-trace-body');
      if (body && !body.querySelector('.tv-tb-sec[data-sec="result"]')) {
        const div = document.createElement('div');
        div.className = 'tv-tb-sec';
        div.dataset.sec = 'result';
        div.innerHTML = '<div class="tv-tb-label">输出结果</div>'
          + '<pre class="mono tv-json tv-json-box">' + tvJsonHtml(entry.result) + '</pre>';
        body.appendChild(div);
      }
    }
    /* ---- 状态升级（2026-11-24）：「待审批」→ 已有结果 → 改成成功/失败 ----
     * 同一条调用会先以「待审批」渲染（无结果），批准后再推一次带结果的快照。
     * 这里就地更新状态图标 / 药丸 / 行样式，避免同一行先琥珀后变绿却不刷新。
     */
    const prevState = existing.dataset.tvState || '';
    const newState = tvEntryState(entry);
    if (newState !== prevState) {
      existing.dataset.tvState = newState;
      existing.classList.remove('is-error', 'is-pending');
      if (newState === 'error') existing.classList.add('is-error');
      if (newState === 'pending') existing.classList.add('is-pending');
      const st = existing.querySelector('.tv-status');
      if (st) {
        st.className = 'tv-status ' + (newState === 'success' ? 'ok'
          : (newState === 'pending' ? 'wait' : 'err'));
        st.title = (newState === 'success' ? '执行成功'
          : (newState === 'pending' ? TV_PENDING_HINT : '执行失败'));
        st.innerHTML = tvStateIcon(newState, 11);
      }
      const pill = existing.querySelector('.tv-state-pill');
      if (pill) {
        pill.className = 'pill tv-state-pill ' + (newState === 'success' ? 'pill-ok'
          : (newState === 'pending' ? 'pill-wait' : 'pill-err'));
        pill.textContent = (newState === 'success' ? '成功'
          : (newState === 'pending' ? '待审批' : '失败'));
      }
    }
    return;
  }

  const name = entry.name || '(工具)';
  /* ---- 三态（2026-11-24）：成功 / 失败 / 待审批 ----
   * 旧判定 `entry.ok !== false` 会把「已发起但未执行」算成成功 →
   * 渲染成绿色 ✓「成功」，用户以为文件已写好，实际根本没落地。
   */
  const state = tvEntryState(entry);
  const isPending = state === 'pending';
  const isError = state === 'error';
  const argsSummary = tvArgsSummary(entry.args || {});
  const dur = entry.duration_ms != null ? tvFmtDur(entry.duration_ms) : '';

  const row = document.createElement('details');
  row.className = 'tool-row tv-trace-row'
    + (isError ? ' is-error' : (isPending ? ' is-pending' : ''));
  row.dataset.seq = String(entry.seq || '');
  row.dataset.cid = cid || ('#' + (entry.seq || ''));
  row.dataset.tvState = state;

  /* ---- 折叠头：一行紧凑摘要 ---- */
  let head = '<summary>'
    + '<svg class="tool-caret" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4l4 4-4 4"/></svg>'
    + '<span class="tv-seq mono">' + esc('#' + (entry.seq || 0)) + '</span>'
    + '<span class="tv-status ' + (state === 'success' ? 'ok' : (isPending ? 'wait' : 'err'))
      + '" title="' + (state === 'success' ? '执行成功'
        : (isPending ? TV_PENDING_HINT : '执行失败')) + '">'
      + tvStateIcon(state, 11) + '</span>'
    + '<span class="tool-name">' + esc(name) + '</span>'
    + (argsSummary ? '<span class="tool-summary-text tv-args-sum">' + esc(argsSummary) + '</span>' : '')
    + '<span class="grow"></span>'
    + (dur ? '<span class="tv-dur mono">' + esc(dur) + '</span>' : '')
    /* ---- 三态药丸：成功 / 失败 / 待审批（琥珀色）---- */
    + '<span class="pill tv-state-pill ' + (state === 'success' ? 'pill-ok'
      : (isPending ? 'pill-wait' : 'pill-err')) + '">'
      + (state === 'success' ? '成功' : (isPending ? '待审批' : '失败')) + '</span>'
    + '</summary>';

  /* ---- 展开体：分区 ---- */
  let body = '<div class="tv-trace-body">';

  /* ---- 待审批提示（2026-11-24 新增）----
   * 这一步**还没执行** —— 必须明确告诉用户「要点了批准才会跑」，
   * 否则用户看到「文件写好了」却发现磁盘上什么都没有。
   */
  if (isPending) {
    body += '<div class="tv-tb-sec tv-tb-pending">'
      + '<div class="tv-tb-label">' + tvStateIcon('pending', 11) + '待审批</div>'
      + '<div class="tv-tb-text">' + esc(TV_PENDING_HINT) + '</div>'
      + '<div class="tv-tb-hint">该工具已发起，但**尚未执行**；'
      + '在对话中回复文字不会自动放行，请点击审批卡片上的「批准」按钮。</div>'
      + '</div>';
  }

  /* 思考（推理摘要，截断标注） */
  if (entry.reasoning_summary) {
    body += '<div class="tv-tb-sec tv-tb-think">'
      + '<div class="tv-tb-label">' + tvIcon('brain', 11) + '模型推理摘要（截断）</div>'
      + '<div class="tv-tb-text">' + esc(entry.reasoning_summary) + '</div>'
      + '</div>';
  }
  /* 动作说明（可解释的动作摘要，非思维链） */
  if (entry.action_summary) {
    body += '<div class="tv-tb-sec">'
      + '<div class="tv-tb-label">动作</div>'
      + '<div class="tv-tb-text">' + esc(entry.action_summary) + '</div>'
      + '</div>';
  }
  /* 参数 */
  body += '<div class="tv-tb-sec">'
    + '<div class="tv-tb-label">输入参数</div>'
    + '<pre class="mono tv-json tv-json-box">' + tvJsonHtml(entry.args || {}) + '</pre>'
    + '</div>';
  /* 结果 */
  if (entry.result !== undefined && entry.result !== null) {
    body += '<div class="tv-tb-sec" data-sec="result">'
      + '<div class="tv-tb-label">输出结果</div>'
      + '<pre class="mono tv-json tv-json-box">' + tvJsonHtml(entry.result) + '</pre>'
      + '</div>';
  }
  /* 错误 */
  if (entry.error && Object.keys(entry.error).length) {
    const e = entry.error;
    body += '<div class="tv-tb-sec tv-tb-err">'
      + '<div class="tv-tb-label">错误</div>'
      + '<div class="tv-tb-text"><span class="mono tv-err-code">' + esc(e.code || '') + '</span> '
      + esc(e.message || '') + '</div>'
      + (e.hint ? '<div class="tv-tb-hint">' + esc(e.hint) + '</div>' : '')
      + '</div>';
  }
  body += '</div>';

  row.innerHTML = head + body;
  box.appendChild(row);
}

/** 耗时格式化 */
function tvFmtDur(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n)) return '';
  if (n < 1000) return Math.round(n) + ' ms';
  return (n / 1000).toFixed(2) + ' s';
}

/* ---- 决策轨迹面板（完整决策链） ---- */

/** 打开「决策轨迹」面板（主区新 tab） */
function tvOpenTracePanel() {
  const id = 'pg-trace';
  let tab = state.tabs.find((t) => t.id === id);
  if (!tab) {
    tab = openTab('trace', '决策轨迹', null, id);
  }
  activateTab(id);
  const page = document.querySelector('.page[data-page="' + id + '"]');
  if (!page) return;
  page.innerHTML = tvTracePanelHtml();
  tvBindTracePanel(page);
  tvRenderTracePanel(page);
}

/** 面板骨架 */
function tvTracePanelHtml() {
  return '<div class="tv-page tv-trace-page">'
    + '<div class="tv-tool-head">'
    + '<span class="tv-tool-ico">' + tvIcon('route', 16) + '</span>'
    + '<span class="tv-tool-name">决策轨迹</span>'
    + '<span class="grow"></span>'
    + '<button type="button" class="btn btn-ghost sm tv-refresh-trace">刷新</button>'
    + '</div>'
    + '<div class="tv-trace-budget" id="tv-trace-budget"></div>'
    + '<div class="tv-trace-list" id="tv-trace-list"></div>'
    + '<div class="tv-trace-audit">'
    + '<button type="button" class="btn btn-ghost sm tv-audit-btn">查看审计记录</button>'
    + '<div id="tv-audit-out" class="tv-audit-out"></div>'
    + '</div>'
    + '</div>';
}

/** 绑定面板事件 */
function tvBindTracePanel(page) {
  if (!page || page.dataset.tvBound === '1') return;
  page.dataset.tvBound = '1';
  const refresh = page.querySelector('.tv-refresh-trace');
  if (refresh) {
    refresh.addEventListener('click', () => tvRenderTracePanel(page));
  }
  const auditBtn = page.querySelector('.tv-audit-btn');
  if (auditBtn) {
    auditBtn.addEventListener('click', () => tvLoadAudit(page));
  }
}

/** 渲染轨迹面板内容 */
function tvRenderTracePanel(page) {
  const listEl = page.querySelector('#tv-trace-list');
  const budgetEl = page.querySelector('#tv-trace-budget');
  if (!listEl) return;
  const trace = tvGetTrace(state.threadId);
  const budget = TV_TRACE.budget
    || (TV_TRACE.byThread.get(state.threadId || 'current') || {}).budget;

  /* ---- 预算消耗 ---- */
  if (budgetEl) {
    budgetEl.innerHTML = tvBudgetHtml(budget, trace.length);
  }

  if (!trace.length) {
    listEl.innerHTML = '<div class="muted">本次会话还没有工具调用记录。'
      + '发一条需要检索或工具调用的问题（例如「T101_VDD_FUNCT 的上下限是多少？」），'
      + '这里就会出现完整的决策链。</div>';
    return;
  }

  listEl.innerHTML = '';
  trace.forEach((entry, idx) => {
    const item = document.createElement('div');
    /* ---- 三态（2026-11-24）：成功 / 失败 / 待审批 ----
     * 旧判定 `entry.ok === false` 只区分成功与失败，「已发起但未执行」
     * 会落进「成功」分支 → 绿色 ✓「成功」→ 用户以为文件已写好。
     */
    const state = tvEntryState(entry);
    const isPending = state === 'pending';
    const isError = state === 'error';
    item.className = 'tv-trace-item'
      + (isError ? ' is-error' : (isPending ? ' is-pending' : ''));
    item.dataset.seq = String(entry.seq || (idx + 1));

    const dur = entry.duration_ms != null ? tvFmtDur(entry.duration_ms) : '–';

    item.innerHTML = ''
      /* ---- 时间线左侧：序号 + 状态 ---- */
      + '<div class="tv-tl-mark">'
      + '<span class="tv-tl-dot ' + (state === 'success' ? 'ok' : (isPending ? 'wait' : 'err'))
        + '" title="' + (state === 'success' ? '执行成功'
          : (isPending ? TV_PENDING_HINT : '执行失败')) + '">'
      + tvStateIcon(state, 11) + '</span>'
      + (idx < trace.length - 1 ? '<span class="tv-tl-line"></span>' : '')
      + '</div>'
      /* ---- 时间线右侧：内容 ---- */
      + '<div class="tv-tl-body">'
      + '<div class="tv-tl-head">'
      + '<span class="tv-seq mono">步骤 ' + esc(entry.seq || (idx + 1)) + '</span>'
      + '<span class="tool-name">' + esc(entry.name || '(工具)') + '</span>'
      + '<span class="tv-dur mono">' + esc(dur) + '</span>'
      + '<span class="pill ' + (state === 'success' ? 'pill-ok'
        : (isPending ? 'pill-wait' : 'pill-err')) + '">'
      + (state === 'success' ? '成功' : (isPending ? '待审批' : '失败')) + '</span>'
      + '</div>'
      /* ---- 待审批提示（2026-11-24 新增）---- */
      + (isPending
          ? '<div class="tv-tl-sec tv-tl-pending">'
            + '<span class="tv-tl-tag">' + tvStateIcon('pending', 10) + '待审批</span>'
            + '<span class="tv-tl-text">' + esc(TV_PENDING_HINT) + '</span>'
            + '</div>'
          : '')
      /* ---- 思考 ---- */
      + (entry.reasoning_summary
          ? '<div class="tv-tl-sec tv-tl-think">'
            + '<span class="tv-tl-tag">' + tvIcon('brain', 10) + '思考</span>'
            + '<span class="tv-tl-text">' + esc(entry.reasoning_summary) + '</span>'
            + '<span class="tv-tl-note">（模型推理摘要，已截断）</span>'
            + '</div>'
          : '<div class="tv-tl-sec tv-tl-think tv-tl-none">'
            + '<span class="tv-tl-tag">' + tvIcon('brain', 10) + '思考</span>'
            + '<span class="tv-tl-text muted">本轮模型未返回可展示的推理摘要</span>'
            + '</div>')
      /* ---- 决策 ---- */
      + '<div class="tv-tl-sec">'
      + '<span class="tv-tl-tag">决策</span>'
      + '<span class="tv-tl-text">选择调用 <b class="mono">'
      + esc(entry.name || '(工具)') + '</b></span>'
      + '</div>'
      /* ---- 动作 ---- */
      + '<div class="tv-tl-sec">'
      + '<span class="tv-tl-tag">动作</span>'
      + '<span class="tv-tl-text">' + esc(entry.action_summary || '–') + '</span>'
      + '</div>'
      /* ---- 参数 ---- */
      + '<div class="tv-tl-sec tv-tl-args">'
      + '<span class="tv-tl-tag">参数</span>'
      + '<div class="tv-tl-json"><pre class="mono tv-json">'
      + tvJsonHtml(entry.args || {}) + '</pre></div>'
      + '</div>'
      /* ---- 观察 ---- */
      + (entry.result !== undefined && entry.result !== null
          ? '<div class="tv-tl-sec">'
            + '<span class="tv-tl-tag">观察</span>'
            + '<div class="tv-tl-json"><pre class="mono tv-json tv-json-box">'
            + tvJsonHtml(entry.result) + '</pre></div>'
            + '</div>'
          : '')
      /* ---- 错误 ---- */
      + (entry.error && Object.keys(entry.error).length
          ? '<div class="tv-tl-sec tv-tl-err">'
            + '<span class="tv-tl-tag">错误</span>'
            + '<span class="tv-tl-text mono">' + esc(entry.error.code || '') + '</span> '
            + '<span class="tv-tl-text">' + esc(entry.error.message || '') + '</span>'
            + '</div>'
          : '')
      /* ---- 下一步 ---- */
      + (idx < trace.length - 1
          ? '<div class="tv-tl-sec tv-tl-next">'
            + '<span class="tv-tl-tag">下一步</span>'
            + '<span class="tv-tl-text muted">基于上一步结果继续判断 → 步骤 '
            + esc((trace[idx + 1].seq) || (idx + 2)) + '</span>'
            + '</div>'
          : '<div class="tv-tl-sec tv-tl-next">'
            + '<span class="tv-tl-tag">下一步</span>'
            + '<span class="tv-tl-text muted">本轮决策链结束，模型据此给出最终答案</span>'
            + '</div>')
      + '</div>';

    listEl.appendChild(item);
  });
}

/** 预算消耗展示 */
function tvBudgetHtml(budget, traceLen) {
  const b = budget || {};
  /* ---- 轮次 / 工具调用的兜底口径（2026-11-24 修正）----
   * 旧实现：`rounds = b.rounds != null ? b.rounds : traceLen`，
   *          `maxRounds = b.max_rounds || 8`。
   * 问题：
   *   1) trace 里混着 llm / tool / approval 三种条目，拿**总长度**当轮次
   *      → 出现「9/8」这种**越界**显示（9 条轨迹 ≠ 9 轮）；
   *   2) maxRounds 硬编码 8，而后端默认已是 20 → 预算条永远显示 8。
   * 现在：优先用后端给的真实值（后端已按 kind 精确统计），
   *       前端兜底也只按 kind 数 llm / tool 条目。
   */
  const llmCount = (TV_TRACE.counts && TV_TRACE.counts.llm) || 0;
  const toolCount = (TV_TRACE.counts && TV_TRACE.counts.tool) || 0;
  const fallbackRounds = llmCount || 0;
  const fallbackCalls = toolCount || 0;
  const rounds = b.rounds != null ? b.rounds : fallbackRounds;
  const maxRounds = b.max_rounds || 20;
  const tokens = b.tokens_used || 0;
  const maxTokens = b.max_tokens || 60000;
  const calls = b.tool_calls_used != null ? b.tool_calls_used : fallbackCalls;
  const maxCalls = b.max_tool_calls || 24;

  const bar = (used, max) => {
    const pct = max > 0 ? Math.min(100, Math.round((used / max) * 100)) : 0;
    return '<span class="tv-budget-bar"><span class="tv-budget-fill" style="width:'
      + pct + '%"></span></span><span class="tv-budget-num mono">' + used + ' / ' + max + '</span>';
  };

  return '<div class="tv-budget">'
    + '<div class="tv-budget-item"><span class="tv-budget-label">'
    + tvIcon('route', 11) + 'ReAct 轮次</span>' + bar(rounds, maxRounds) + '</div>'
    + '<div class="tv-budget-item"><span class="tv-budget-label">'
    + tvIcon('gauge', 11) + 'token</span>' + bar(tokens, maxTokens) + '</div>'
    + '<div class="tv-budget-item"><span class="tv-budget-label">'
    + tvIcon('tool', 11) + '工具调用</span>' + bar(calls, maxCalls) + '</div>'
    + '</div>';
}

/** 拉取审计记录（与同一次执行对齐） */
async function tvLoadAudit(page) {
  const out = page.querySelector('#tv-audit-out');
  const btn = page.querySelector('.tv-audit-btn');
  if (!out) return;
  if (btn) btn.disabled = true;
  out.innerHTML = '<div class="muted sm">加载中…</div>';
  try {
    const res = await api('/audit?limit=30&thread_id=' + encodeURIComponent(state.threadId || ''));
    const rows = res.records || [];
    if (!rows.length) {
      out.innerHTML = '<div class="muted sm">没有找到该会话的审计记录'
        + '（审计后端：' + esc(res.mode || 'unknown') + '）。</div>';
      return;
    }
    out.innerHTML = '<table class="tv-audit-table"><thead><tr>'
      + '<th>时间</th><th>动作</th><th>目标</th><th>状态</th><th>耗时</th>'
      + '</tr></thead><tbody>'
      + rows.map((r) => '<tr>'
        + '<td class="mono tv-audit-ts">' + esc(tvAuditTs(r.ts)) + '</td>'
        + '<td class="mono">' + esc(r.action || '') + '</td>'
        + '<td class="mono">' + esc(r.target || '') + '</td>'
        + '<td>' + esc(r.status || '') + '</td>'
        + '<td class="mono">' + esc(r.duration_ms != null ? r.duration_ms + 'ms' : '–') + '</td>'
        + '</tr>').join('')
      + '</tbody></table>';
  } catch (err) {
    out.innerHTML = '<div class="pill pill-err">审计加载失败：' + esc(err.message) + '</div>';
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** 审计时间戳可读化 */
function tvAuditTs(ts) {
  if (!ts) return '–';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return String(ts).slice(0, 19).replace('T', ' ');
  const p = (n) => String(n).padStart(2, '0');
  return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':'
    + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

/* ----------------------------------------------------------------------
   与既有前端的对接
   ---------------------------------------------------------------------- */

/**
 * 侧栏工具列表 → 点击工具名打开详情。
 * 列表行本身是静态 div（历史行为不变），只是把**工具名**变成可点击。
 */
function tvBindToolList() {
  const box = $('#tools-list');
  if (!box || box.dataset.tvBound === '1') return;
  box.dataset.tvBound = '1';
  box.addEventListener('click', (e) => {
    const title = e.target.closest('.row-title');
    if (!title) return;
    const name = title.textContent || '';
    if (!name) return;
    e.stopPropagation();
    tvOpenTool(name);
  });
}

/** 复制文本（Clipboard API + 降级） */
function copyText(text) {
  const t = String(text == null ? '' : text);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(t).catch(() => tvCopyFallback(t));
  }
  return Promise.resolve(tvCopyFallback(t));
}

function tvCopyFallback(text) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

/** 初始化：绑定工具列表点击 + 对话头「决策轨迹」入口 */
function tvInit() {
  tvBindToolList();

  /* 「决策轨迹」入口（对话 tab 头部） */
  const head = $('.chat-head');
  if (head && !head.querySelector('#btn-trace-view')) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'head-btn';
    btn.id = 'btn-trace-view';
    btn.title = '查看本次会话的完整决策轨迹（思考 → 决策 → 动作 → 观察）';
    btn.setAttribute('aria-label', '查看决策轨迹');
    btn.innerHTML = tvIcon('route', 13) + '<span>决策轨迹</span>';
    btn.addEventListener('click', () => tvOpenTracePanel());
    const status = head.querySelector('#m-status');
    if (status && status.parentNode) {
      status.parentNode.insertBefore(btn, status);
    } else {
      head.appendChild(btn);
    }
  }
}

/* ----------------------------------------------------------------------
   为什么用「主区新开 tab」而不是「侧栏内展开」
   ----------------------------------------------------------------------
   1. **信息量不对等**：工具详情包含 input_schema 全文、OpenAI function
      calling 形态、metadata —— 侧栏只有 268px，展开后必须滚动，
      反而看不清；而主区是编辑区，宽度充裕，适合放「可读的契约文档」。
   2. **与 Cursor 语义一致**：Cursor 里点文件是「打开一个编辑器 tab」，
      可以关掉、可以和对话 / 任务 / 工作区文件并存、来回切换对照。
   3. **可对比**：想同时看 kb_search 与 ws_edit_file 两个工具的 schema 时，
      两个 tab 并排切换即可；侧栏展开只能一次看一个。
   4. **不打断上下文**：侧栏展开会把「工具列表」挤变形，破坏资源管理器
      的树形结构；tab 是隔离的。

   注意：但「快速看一眼」的需求也要满足 —— 侧栏列表里仍然显示
   权限徽标 + HITL 标记 + 描述首行，用户扫一眼就能判断；
   想看细节再点开 tab。
   ====================================================================== */
