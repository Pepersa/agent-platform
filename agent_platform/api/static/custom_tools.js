/* ==========================================================================
   自定义工具管理 + 任务详情 —— 追加到 app.js 末尾
   （原生 JS，无框架、无 CDN）
   ========================================================================== */
'use strict';

/* ----------------------------------------------------------------------
   桌面模式判定：pywebview 注入 window.pywebview
   ---------------------------------------------------------------------- */
const IS_DESKTOP = !!(window.pywebview && window.pywebview.api !== undefined);

/* ----------------------------------------------------------------------
   内联 SVG 图标（1.5px 描边，Lucide 风格）—— 不使用 emoji / 字体图标
   ---------------------------------------------------------------------- */
const ICON_WARN =
  '<svg class="inline-ico" viewBox="0 0 16 16" width="13" height="13" fill="none"'
  + ' stroke="currentColor" stroke-width="1.5" stroke-linecap="round"'
  + ' stroke-linejoin="round"><path d="M8 2.2 14.4 13.4H1.6z"/>'
  + '<path d="M8 6.4v3.2M8 11.6h.01"/></svg>';

const ICON_OK =
  '<svg class="inline-ico" viewBox="0 0 16 16" width="13" height="13" fill="none"'
  + ' stroke="currentColor" stroke-width="1.7" stroke-linecap="round"'
  + ' stroke-linejoin="round"><path d="M3 8.4 6.4 12 13 4.6"/></svg>';

const ICON_ERR =
  '<svg class="inline-ico" viewBox="0 0 16 16" width="13" height="13" fill="none"'
  + ' stroke="currentColor" stroke-width="1.6" stroke-linecap="round"'
  + ' stroke-linejoin="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>';

/** 桌面模式下隐藏浏览器专属提示元素 */
function applyDesktopMode() {
  if (!IS_DESKTOP) return;
  document.body.classList.add('desktop-mode');
  const badge = document.getElementById('desktop-badge');
  if (badge) {
    badge.hidden = false;
    badge.title = '当前运行在 pywebview 桌面窗口中';
  }
  document.querySelectorAll('.browser-only').forEach((el) => { el.style.display = 'none'; });
}

/* ----------------------------------------------------------------------
   通用：下载文本文件
   ---------------------------------------------------------------------- */
function downloadText(filename, text, mime) {
  const blob = new Blob([text], { type: mime || 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, 200);
}

/* ======================================================================
   一、自定义工具
   ====================================================================== */
const ct = {
  editing: null,          // 正在编辑的工具名（null = 新建）
  list: [],
};

/** 打开模态框 */
function openCustomToolModal(tool) {
  ct.editing = tool ? tool.name : null;
  $('#ct-title').textContent = tool ? ('编辑自定义工具：' + tool.name) : '新建自定义工具';
  $('#ct-msg').textContent = '';
  $('#ct-test-out').innerHTML = '<div class="muted sm">填好上面的参数后点「试运行」，这里会显示实际返回 JSON 或错误信息。</div>';

  $('#ct-name').value = tool ? tool.name : '';
  $('#ct-desc').value = tool ? (tool.description || '') : '';
  $('#ct-kind').value = tool ? tool.kind : 'http';
  $('#ct-perm').value = tool ? tool.permission : 'READ_ONLY';
  $('#ct-enabled').value = tool ? (tool.enabled ? 'true' : 'false') : 'true';
  $('#ct-hitl').value = tool ? (tool.hitl_required ? 'true' : 'false') : 'false';
  $('#ct-schema').value = JSON.stringify(
    tool ? tool.input_schema : { type: 'object', properties: {} }, null, 2);

  const cfg = (tool && tool.config) || {};
  $('#ct-url').value = cfg.url || '';
  $('#ct-method').value = cfg.method || 'GET';
  $('#ct-resultpath').value = cfg.result_path || '';
  $('#ct-headers').value = cfg.headers ? JSON.stringify(cfg.headers, null, 2) : '';
  $('#ct-body').value = cfg.body_template || '';
  $('#ct-scriptpath').value = cfg.script_path || '';
  $('#ct-funcname').value = cfg.function_name || '';
  $('#ct-timeout').value = cfg.timeout_s || 15;
  $('#ct-testargs').value = tool && tool.example_args
    ? JSON.stringify(tool.example_args, null, 2) : '';

  onCtKindChange();
  onCtPermChange();
  $('#ct-mask').hidden = false;
  refreshCustomTools();
}

function closeCustomToolModal() {
  $('#ct-mask').hidden = true;
  ct.editing = null;
}

function onCtKindChange() {
  const kind = $('#ct-kind').value;
  $('#ct-cfg-http').hidden = kind !== 'http';
  $('#ct-cfg-script').hidden = kind !== 'script';
}

function onCtPermChange() {
  const perm = $('#ct-perm').value;
  const forced = ['WRITE_DATA', 'EXTERNAL_SIDE_EFFECT', 'ADMIN'].indexOf(perm) >= 0;
  const note = $('#ct-hitl-note');
  if (forced) {
    note.hidden = false;
    note.innerHTML = ICON_WARN
      + ' 权限 <b>' + esc(perm) + '</b> 属于「写数据 / 对外副作用 / 管理员」级别，'
      + '按平台安全策略<b>强制开启人工确认（HITL）</b>，此项不可关闭。';
    $('#ct-hitl').value = 'true';
    $('#ct-hitl').disabled = true;
  } else {
    note.hidden = true;
    $('#ct-hitl').disabled = false;
  }
}

/** 组装 payload */
function collectCustomTool() {
  const kind = $('#ct-kind').value;
  const cfg = {};
  if (kind === 'http') {
    cfg.url = ($('#ct-url').value || '').trim();
    cfg.method = ($('#ct-method').value || 'GET').toUpperCase();
    const rp = ($('#ct-resultpath').value || '').trim();
    if (rp) cfg.result_path = rp;
    const rawHeaders = ($('#ct-headers').value || '').trim();
    if (rawHeaders) {
      try {
        const h = JSON.parse(rawHeaders);
        if (!h || typeof h !== 'object' || Array.isArray(h)) {
          throw new Error('Headers 必须是 JSON 对象');
        }
        cfg.headers = h;
      } catch (e) {
        throw new Error('Headers JSON 解析失败：' + e.message);
      }
    }
    const body = ($('#ct-body').value || '').trim();
    if (body) cfg.body_template = body;
    if ($('#ct-timeout').value) cfg.timeout_s = Number($('#ct-timeout').value) || 15;
  } else {
    cfg.script_path = ($('#ct-scriptpath').value || '').trim();
    cfg.function_name = ($('#ct-funcname').value || '').trim();
    cfg.timeout_s = Number($('#ct-timeout').value) || 15;
  }

  const schemaText = ($('#ct-schema').value || '').trim();
  let schema;
  try {
    schema = JSON.parse(schemaText || '{"type":"object","properties":{}}');
  } catch (e) {
    throw new Error('input_schema 不是合法 JSON：' + e.message);
  }
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) {
    throw new Error('input_schema 必须是 JSON 对象');
  }
  if (schema.type && schema.type !== 'object') {
    throw new Error('input_schema.type 必须是 "object"');
  }

  return {
    name: ($('#ct-name').value || '').trim(),
    description: ($('#ct-desc').value || '').trim(),
    input_schema: schema,
    kind: kind,
    config: cfg,
    permission: $('#ct-perm').value,
    hitl_required: $('#ct-hitl').value === 'true',
    enabled: $('#ct-enabled').value === 'true',
  };
}

/** 保存（新建或更新） */
async function saveCustomTool() {
  const msg = $('#ct-msg');
  msg.textContent = '';
  let payload;
  try {
    payload = collectCustomTool();
  } catch (e) {
    msg.innerHTML = '<span class="pill pill-err">' + esc(e.message) + '</span>';
    return;
  }
  if (!payload.name) {
    msg.innerHTML = '<span class="pill pill-err">请填写工具名</span>';
    return;
  }
  const btn = $('#ct-save');
  btn.disabled = true;
  try {
    if (ct.editing) {
      await api('/custom-tools/' + encodeURIComponent(ct.editing), {
        method: 'PUT', body: JSON.stringify(payload),
      });
      msg.innerHTML = '<span class="pill pill-ok">已更新</span>';
    } else {
      await api('/custom-tools', { method: 'POST', body: JSON.stringify(payload) });
      msg.innerHTML = '<span class="pill pill-ok">已创建并注册（立即生效）</span>';
    }
    await refreshCustomTools();
    refreshTools();
    // 保留模态框，让用户能继续试运行
  } catch (err) {
    msg.innerHTML = '<span class="pill pill-err">' + esc(err.message) + '</span>';
  } finally {
    btn.disabled = false;
  }
}

/** 试运行 */
async function testCustomTool() {
  const out = $('#ct-test-out');
  let args = {};
  const raw = ($('#ct-testargs').value || '').trim();
  if (raw) {
    try {
      args = JSON.parse(raw);
      if (!args || typeof args !== 'object' || Array.isArray(args)) {
        throw new Error('示例参数必须是 JSON 对象');
      }
    } catch (e) {
      out.innerHTML = '<div class="pill pill-err">示例参数 JSON 解析失败：'
        + esc(e.message) + '</div>';
      return;
    }
  }
  let payload;
  try {
    payload = collectCustomTool();
  } catch (e) {
    out.innerHTML = '<div class="pill pill-err">' + esc(e.message) + '</div>';
    return;
  }
  if (!payload.name) {
    out.innerHTML = '<div class="pill pill-err">请先填写工具名</div>';
    return;
  }
  out.innerHTML = '<div class="muted sm">试运行中…</div>';
  try {
    // 若工具已存在则直接试运行；否则先保存再试运行
    const exists = ct.list.some((t) => t.name === payload.name);
    if (!exists) {
      if (ct.editing) {
        await api('/custom-tools/' + encodeURIComponent(ct.editing), {
          method: 'PUT', body: JSON.stringify(payload),
        });
      } else {
        await api('/custom-tools', { method: 'POST', body: JSON.stringify(payload) });
        await refreshCustomTools();
        refreshTools();
      }
    }
    const res = await api('/custom-tools/' + encodeURIComponent(payload.name) + '/test', {
      method: 'POST', body: JSON.stringify({ args: args }),
    });
    if (res && res.ok) {
      out.innerHTML = '<div class="ok-badge">' + ICON_OK + ' 成功（' + (res.elapsed_ms || 0) + ' ms）</div>'
        + '<pre class="ct-json">' + esc(JSON.stringify(res.result, null, 2)) + '</pre>';
    } else {
      const err = (res && res.error) || {};
      out.innerHTML = '<div class="pill pill-err">' + ICON_ERR + ' ' + esc(err.code || 'ERROR') + '</div>'
        + '<pre class="ct-json err">' + esc(err.message || '未知错误') + '</pre>'
        + (err.hint ? '<div class="muted sm">提示：' + esc(err.hint) + '</div>' : '');
    }
  } catch (err) {
    out.innerHTML = '<div class="pill pill-err">' + ICON_ERR + ' ' + esc(err.message) + '</div>';
  }
}

/** 刷新自定义工具列表 */
async function refreshCustomTools() {
  const box = $('#ct-list');
  try {
    const res = await api('/custom-tools');
    ct.list = res.tools || [];
    box.innerHTML = '';
    if (!ct.list.length) {
      box.innerHTML = '<div class="muted sm">还没有自定义工具。</div>';
      return;
    }
    ct.list.forEach((t) => {
      const el = document.createElement('div');
      el.className = 'ct-item';
      el.innerHTML =
        `<div class="ct-head">`
        + `<span class="t-name">${esc(t.name)}</span>`
        + `<span class="perm ${esc(t.permission)}">${esc(t.permission)}</span>`
        + `<span class="kind-tag">${esc(t.kind)}</span>`
        + (t.enabled ? '' : '<span class="pill pill-wait">已停用</span>')
        + (t.hitl_required ? '<span class="pill pill-wait">需人工确认</span>' : '')
        + `</div>`
        + `<div class="t-desc">${esc(t.description || '（无描述）')}</div>`
        + (t.hitl_forced_reason
            ? `<div class="muted sm">${esc(t.hitl_forced_reason)}</div>` : '')
        + `<div class="ct-actions">`
        + `<button class="btn btn-ghost sm act-edit" type="button">编辑</button>`
        + `<button class="btn btn-ghost sm act-toggle" type="button">${t.enabled ? '停用' : '启用'}</button>`
        + `<button class="btn btn-ghost sm act-del" type="button">删除</button>`
        + `</div>`;
      $('.act-edit', el).addEventListener('click', () => openCustomToolModal(t));
      $('.act-toggle', el).addEventListener('click', async () => {
        try {
          await api('/custom-tools/' + encodeURIComponent(t.name) + '/toggle'
            + (t.enabled ? '?enabled=false' : '?enabled=true'), { method: 'POST' });
          refreshCustomTools();
          refreshTools();
        } catch (err) { addSystemMsg('操作失败：' + err.message); }
      });
      $('.act-del', el).addEventListener('click', async () => {
        if (!window.confirm('确定删除自定义工具「' + t.name + '」？')) return;
        try {
          await api('/custom-tools/' + encodeURIComponent(t.name), { method: 'DELETE' });
          refreshCustomTools();
          refreshTools();
        } catch (err) { addSystemMsg('删除失败：' + err.message); }
      });
      box.appendChild(el);
    });
  } catch (err) {
    box.innerHTML = '<div class="muted sm">加载失败：' + esc(err.message) + '</div>';
  }
}

/* ======================================================================
   二、任务详情
   ====================================================================== */
let tdCurrent = null;

/**
 * 打开任务详情。
 * - 优先在主区域新开一个 tab（Cursor 风格），并同步刷新抽屉内容；
 * - 若 tab 打开失败（例如页面结构异常），退化为只开抽屉。
 */
async function openTaskDetail(taskId) {
  tdCurrent = taskId;
  const title = '任务详情：' + taskId;
  $('#td-title').textContent = title;
  $('#td-msg').textContent = '加载中…';

  let tab = null;
  try {
    tab = openTab('task', title, { taskId: taskId });
  } catch (e) {
    tab = null;
  }
  if (!tab) {
    $('#td-mask').hidden = false;
  }
  $('#td-basic').innerHTML = '<div class="muted sm">加载中…</div>';
  try {
    const d = await api('/tasks/' + encodeURIComponent(taskId));
    $('#td-msg').textContent = '';
    renderTaskDetail(d);
    if (tab) renderTaskTab(d, tab.id);
  } catch (err) {
    $('#td-msg').innerHTML = '<span class="pill pill-err">' + esc(err.message) + '</span>';
    if (tab) {
      const page = $('.page[data-page="' + tab.id + '"]');
      if (page) {
        page.innerHTML = '<div class="doc-page-inner"><h2>' + esc(title)
          + '</h2><div class="pill pill-err">加载失败：' + esc(err.message) + '</div></div>';
      }
    }
  }
}

/** 把任务详情渲染进主区域的 tab（复用抽屉里的同一套渲染逻辑） */
function renderTaskTab(d, tabId) {
  const page = $('.page[data-page="' + tabId + '"]');
  if (!page) return;
  const basic = [
    ['状态', d.status], ['模型', d.model], ['耗时', fmtMs(d.duration_ms)],
    ['token', d.tokens_used], ['工具调用', d.tool_calls_used],
    ['终止原因', d.stop_reason || '–'], ['创建时间', fmtTime(d.created_at)],
    ['开始时间', fmtTime(d.started_at)], ['结束时间', fmtTime(d.finished_at)],
    ['thread_id', d.thread_id || '–'], ['session_id', d.session_id || '–'],
  ];
  const trace = d.tool_trace || [];
  const sse = d.sse_events || [];
  const ans = d.answer || '';
  const r = renderAnswerHtml(ans);

  page.innerHTML = '<div class="doc-page-inner">'
    + '<h2>' + esc(d.name || d.task_id || '任务') + '</h2>'
    + '<div class="muted sm mono">' + esc(d.task_id || d.id || '') + '</div>'
    + '<div class="td-kv" style="margin:12px 0 20px">'
    + basic.map((kv) => '<div class="td-kv-row"><span>' + esc(kv[0]) + '</span><b>'
      + esc(kv[1] == null ? '–' : kv[1]) + '</b></div>').join('')
    + '</div>'
    + '<h3>Prompt</h3><pre class="td-pre">' + esc(d.prompt || '（空）') + '</pre>'
    + '<h3>完整答案</h3>'
    + (ans ? '<div class="md-view">' + renderMarkdown(ans) + '</div>'
      + (r.cites.length ? '<div class="cites">' + r.cites.map(
        (c) => '<span class="cite" data-full="' + esc(c.raw) + '" title="'
          + esc(c.label + ': ' + c.id) + '">' + esc(c.label) + ':' + esc(c.id)
          + '</span>').join('') + '</div>' : '')
      : '<div class="muted">（无答案）</div>')
    + '<h3>工具调用轨迹 (' + trace.length + ')</h3>'
    + (trace.length ? '<div class="td-timeline">' + trace.map((it) =>
      '<div class="tl-item ' + (it.ok === false ? 'bad' : 'ok') + '">'
      + '<div class="tl-dot"></div><div class="tl-body">'
      + '<div class="tl-head"><span class="tl-seq">#' + esc(it.seq) + '</span>'
      + '<span class="tl-name">' + esc(it.name || '') + '</span>'
      + (it.ok === false ? '<span class="pill pill-err">失败</span>' : '') + '</div>'
      + '<div class="tl-label">参数</div><pre class="tl-pre">'
      + esc(JSON.stringify(it.args || {}, null, 2)) + '</pre>'
      + '<div class="tl-label">结果摘要</div><div class="tl-sum">'
      + esc(it.result_summary || '（无）') + '</div>'
      + '<details class="tl-more"><summary>展开完整结果</summary><pre class="tl-pre">'
      + esc(JSON.stringify(it.result, null, 2)) + '</pre></details>'
      + '</div></div>').join('') + '</div>'
      : '<div class="muted sm">本次任务没有工具调用。</div>')
    + (d.error ? '<h3>错误详情</h3><pre class="td-pre err">' + esc(d.error) + '</pre>' : '')
    + '<h3>SSE 事件序列 (' + sse.length + ')</h3>'
    + (sse.length ? '<div class="td-sse">' + sse.map((e, i) =>
      '<div class="sse-row"><span class="sse-i">' + (i + 1) + '</span>'
      + '<span class="sse-ev">' + esc(e.event || '') + '</span>'
      + '<span class="sse-node">' + esc(e.node || '') + '</span>'
      + '<span class="sse-tools">' + esc((e.tools || []).join(', ')) + '</span></div>').join('')
      + '</div>' : '<div class="muted sm">（无 SSE 事件）</div>')
    + '</div>';
}

/** tab 重新激活时按需刷新任务详情 */
function refreshTaskTab(data) {
  if (!data || !data.taskId) return;
  const tab = state.tabs.find((t) => t.id === state.activeTab);
  if (!tab || tab.type !== 'task') return;
  api('/tasks/' + encodeURIComponent(data.taskId))
    .then((d) => renderTaskTab(d, tab.id))
    .catch(() => { /* 忽略 */ });
}

function closeTaskDetail() {
  $('#td-mask').hidden = true;
  tdCurrent = null;
}

function renderTaskDetail(d) {
  // ---- 基本信息 ----
  const basic = [
    ['状态', d.status], ['模型', d.model], ['耗时', fmtMs(d.duration_ms)],
    ['token', d.tokens_used], ['工具调用', d.tool_calls_used],
    ['终止原因', d.stop_reason || '–'], ['创建时间', fmtTime(d.created_at)],
    ['开始时间', fmtTime(d.started_at)], ['结束时间', fmtTime(d.finished_at)],
    ['thread_id', d.thread_id || '–'], ['session_id', d.session_id || '–'],
  ];
  $('#td-basic').innerHTML = basic.map(
    (kv) => '<div class="td-kv-row"><span>' + esc(kv[0]) + '</span><b>'
      + esc(kv[1] == null ? '–' : kv[1]) + '</b></div>').join('')
    + '<pre class="td-pre small">参数：' + esc(JSON.stringify(d.params || {}, null, 2)) + '</pre>';

  // ---- Prompt ----
  $('#td-prompt').textContent = d.prompt || '（空）';

  // ---- 完整答案（Markdown 渲染 + 引用高亮） ----
  const ans = d.answer || '';
  if (!ans) {
    $('#td-answer').innerHTML = '<div class="muted">（无答案）</div>';
  } else {
    const r = renderAnswerHtml(ans);
    $('#td-answer').innerHTML = '<div class="md-view">' + renderMarkdown(ans) + '</div>'
      + (r.cites.length
        ? '<div class="cites">' + r.cites.map(
            (c) => '<span class="cite" data-full="' + esc(c.raw) + '" title="'
              + esc(c.label + ': ' + c.id) + '">' + esc(c.label) + ':' + esc(c.id)
              + '</span>').join('') + '</div>'
        : '');
  }

  // ---- 工具调用轨迹（时间线） ----
  const trace = d.tool_trace || [];
  $('#td-trace-count').textContent = trace.length ? `(${trace.length})` : '';
  const tbox = $('#td-trace');
  tbox.innerHTML = '';
  if (!trace.length) {
    tbox.innerHTML = '<div class="muted sm">本次任务没有工具调用。</div>';
  } else {
    trace.forEach((it) => {
      const item = document.createElement('div');
      item.className = 'tl-item ' + (it.ok === false ? 'bad' : 'ok');
      item.innerHTML =
        `<div class="tl-dot"></div>`
        + `<div class="tl-body">`
        + `<div class="tl-head"><span class="tl-seq">#${esc(it.seq)}</span>`
        + `<span class="tl-name">${esc(it.name || '')}</span>`
        + (it.ok === false ? '<span class="pill pill-err">失败</span>' : '')
        + `</div>`
        + `<div class="tl-label">参数</div>`
        + `<pre class="tl-pre">${esc(JSON.stringify(it.args || {}, null, 2))}</pre>`
        + `<div class="tl-label">结果摘要</div>`
        + `<div class="tl-sum">${esc(it.result_summary || '（无）')}</div>`
        + `<details class="tl-more"><summary>展开完整结果</summary>`
        + `<pre class="tl-pre">${esc(JSON.stringify(it.result, null, 2))}</pre></details>`
        + `</div>`;
      tbox.appendChild(item);
    });
  }

  // ---- 错误 ----
  if (d.error) {
    $('#td-error-sec').hidden = false;
    $('#td-error').textContent = d.error;
  } else {
    $('#td-error-sec').hidden = true;
  }

  // ---- SSE 事件序列 ----
  const sse = d.sse_events || [];
  $('#td-sse-count').textContent = sse.length ? `(${sse.length})` : '';
  $('#td-sse').innerHTML = sse.length
    ? sse.map((e, i) => '<div class="sse-row"><span class="sse-i">'
      + (i + 1) + '</span><span class="sse-ev">' + esc(e.event || '') + '</span>'
      + '<span class="sse-node">' + esc(e.node || '') + '</span>'
      + '<span class="sse-tools">' + esc((e.tools || []).join(', ')) + '</span></div>').join('')
    : '<div class="muted sm">（无 SSE 事件）</div>';

  // ---- 原始 JSON ----
  $('#td-raw').textContent = JSON.stringify(d, null, 2);
}

/**
 * 轻量 Markdown 渲染（不引第三方库）：
 * 标题 / 粗体 / 行内代码 / 代码块 / 无序列表 / 引用 / 表格（简化）。
 */
function renderMarkdown(text) {
  const lines = String(text || '').split(/\r?\n/);
  let out = '';
  let inCode = false;
  let inTable = false;
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = esc(raw);
    if (/^\s*```/.test(raw)) {
      if (inCode) { out += '</code></pre>'; inCode = false; }
      else { out += '<pre class="md-code"><code>'; inCode = true; }
      continue;
    }
    if (inCode) { out += line + '\n'; continue; }
    // 表格行
    if (/^\s*\|.*\|\s*$/.test(raw)) {
      const cells = raw.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      if (/^[\s|:-]+$/.test(raw.trim())) {
        inTable = true;
        continue;   // 分隔行
      }
      if (!inTable) { out += '<table class="md-table"><tr>'; }
      else { out += '<tr>'; }
      cells.forEach((c) => { out += '<td>' + inlineMd(c) + '</td>'; });
      out += '</tr>';
      if (!inTable) { out += '</table>'; }
      continue;
    }
    if (inTable && !/^\s*\|/.test(raw)) {
      // 表格结束（上面的分支已闭合）
      inTable = false;
    }
    const h = raw.match(/^(#{1,6})\s+(.*)$/);
    if (h) { out += '<h' + h[1].length + '>' + inlineMd(esc(h[2])) + '</h' + h[1].length + '>'; continue; }
    if (/^\s*[-*]\s+/.test(raw)) {
      out += '<div class="md-li">• ' + inlineMd(line.replace(/^\s*[-*]\s+/, '')) + '</div>';
      continue;
    }
    if (/^\s*>\s?/.test(raw)) {
      out += '<div class="md-quote">' + inlineMd(line.replace(/^\s*>\s?/, '')) + '</div>';
      continue;
    }
    if (!raw.trim()) { out += '<div class="md-gap"></div>'; continue; }
    out += '<div class="md-p">' + inlineMd(line) + '</div>';
  }
  if (inCode) out += '</code></pre>';
  return out;
}

/** 行内 Markdown（粗体 / 行内代码） */
function inlineMd(text) {
  return String(text || '')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|\s)\*([^*\n]+)\*/g, '$1<em>$2</em>');
}

/* ----------------------------------------------------------------------
   任务导出
   ---------------------------------------------------------------------- */
async function exportTask(format) {
  if (!tdCurrent) return;
  const msg = $('#td-msg');
  msg.textContent = '导出中…';
  try {
    const url = '/tasks/' + encodeURIComponent(tdCurrent) + '/export?format=' + format;
    const res = await fetch(API + url);
    if (!res.ok) {
      const t = await res.text();
      throw new Error('HTTP ' + res.status + ' ' + t.slice(0, 200));
    }
    if (format === 'json') {
      const data = await res.json();
      const name = (data.name || tdCurrent).replace(/[^\w\-.]/g, '_');
      downloadText(name + '.json', JSON.stringify(data, null, 2), 'application/json');
    } else {
      const text = await res.text();
      const name = tdCurrent.replace(/[^\w\-.]/g, '_');
      downloadText(name + '.md', text, 'text/markdown;charset=utf-8');
    }
    msg.innerHTML = '<span class="pill pill-ok">已导出 ' + esc(format.toUpperCase()) + '</span>';
  } catch (err) {
    msg.innerHTML = '<span class="pill pill-err">' + esc(err.message) + '</span>';
  }
}

/* ----------------------------------------------------------------------
   任务列表：筛选 / 排序 / 点击查看详情
   ---------------------------------------------------------------------- */
function taskFilterAndSort(tasks) {
  const status = ($('#task-status-filter') || {}).value || '';
  const sort = ($('#task-sort') || {}).value || 'new';
  let list = (tasks || []).slice();
  if (status) list = list.filter((t) => t.status === status);
  list.sort((a, b) => {
    const ta = a.created_at || 0, tb = b.created_at || 0;
    return sort === 'old' ? ta - tb : tb - ta;
  });
  return list;
}

/** 重绘任务列表（供筛选/排序变化时调用） */
function rerenderTaskList(tasks) {
  const box = $('#tasks-list');
  if (!box) return;
  box.innerHTML = '';
  const list = taskFilterAndSort(tasks);
  const LIMIT = 12;
  const shown = state.tasksExpanded ? list : list.slice(0, LIMIT);
  if (!list.length) {
    box.innerHTML = '<div class="muted">没有符合条件的任务</div>';
  } else {
    shown.forEach((tk) => {
      const el = document.createElement('div');
      el.className = 'tree-row tree-row2 leaf act';
      el.title = '点击查看任务详情';
      el.innerHTML = `<span class="row-line1">`
        + `<span class="row-title" title="${esc(tk.name)}">${esc(tk.name)}</span>`
        + `<span class="row-meta"><span class="st ${esc(tk.status)}">${esc(tk.status)}</span></span></span>`
        + `<span class="row-line2">${esc(tk.result_summary || tk.error || tk.description || '')}</span>`
        + `<span class="row-line2">${esc(tk.model || '–')} · ${fmtMs(tk.duration_ms)} · token ${tk.tokens_used || 0} · 工具 ${tk.tool_calls_used || 0}</span>`;      el.addEventListener('click', () => openTaskDetail(tk.id || tk.task_id));
      box.appendChild(el);
    });
  }
  // 「查看全部」按钮
  const btn = $('#btn-tasks-more');
  if (btn) {
    btn.hidden = list.length <= LIMIT;
    btn.textContent = state.tasksExpanded
      ? '收起（仅显示最近 12 条）'
      : `查看全部（${list.length}）`;
  }
}

/* ----------------------------------------------------------------------
   事件绑定（新增）
   ---------------------------------------------------------------------- */
function bindExtra() {
  // 自定义工具
  const btn = document.getElementById('btn-custom-tool');
  if (btn) btn.addEventListener('click', () => openCustomToolModal(null));
  const btn2 = document.getElementById('btn-custom-tool-2');
  if (btn2) btn2.addEventListener('click', () => openCustomToolModal(null));
  const mk = document.getElementById('ct-mask');
  if (mk) {
    mk.addEventListener('click', (e) => { if (e.target === mk) closeCustomToolModal(); });
  }
  const cc = document.getElementById('ct-close');
  if (cc) cc.addEventListener('click', closeCustomToolModal);
  const cn = document.getElementById('ct-cancel');
  if (cn) cn.addEventListener('click', closeCustomToolModal);
  const cs = document.getElementById('ct-save');
  if (cs) cs.addEventListener('click', saveCustomTool);
  const ctb = document.getElementById('ct-test');
  if (ctb) ctb.addEventListener('click', testCustomTool);
  const ck = document.getElementById('ct-kind');
  if (ck) ck.addEventListener('change', onCtKindChange);
  const cp = document.getElementById('ct-perm');
  if (cp) cp.addEventListener('change', onCtPermChange);

  // 任务详情
  const td = document.getElementById('td-mask');
  if (td) {
    td.addEventListener('click', (e) => { if (e.target === td) closeTaskDetail(); });
  }
  const tdc = document.getElementById('td-close');
  if (tdc) tdc.addEventListener('click', closeTaskDetail);
  const tdc2 = document.getElementById('td-close2');
  if (tdc2) tdc2.addEventListener('click', closeTaskDetail);
  const tj = document.getElementById('td-export-json');
  if (tj) tj.addEventListener('click', () => exportTask('json'));
  const tm = document.getElementById('td-export-md');
  if (tm) tm.addEventListener('click', () => exportTask('md'));

  // 任务筛选 / 排序
  const sf = document.getElementById('task-status-filter');
  if (sf) sf.addEventListener('change', () => refreshTasks());
  const so = document.getElementById('task-sort');
  if (so) so.addEventListener('change', () => refreshTasks());
}
