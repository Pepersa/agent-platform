/* ==========================================================================
   工作台增强 —— 标题栏菜单 / 模型选择器 / 交互模式 / 计划卡片 / 会话摘要
   （原生 JS，无框架、无 CDN；追加在 app.js 之后加载）

   本文件负责：
     1. 文件 / 编辑 / 视图 三个下拉菜单（**每一项都真正执行动作**）
     2. 模型选择器（Cursor 风格 popover）：列出全部模型 + 增删改自定义模型
     3. 交互模式选择器（Agent / Ask / Plan），带视觉区分
     4. Plan 模式的计划卡片（执行 / 放弃）
     5. 会话摘要卡片
     6. 历史会话 → 打开 tab 并渲染完整历史消息
   ========================================================================== */
'use strict';

/* ----------------------------------------------------------------------
   菜单定义（**每一项都有真实动作**，没有 dead item）
   ---------------------------------------------------------------------- */
const MENU_DEFS = {
  file: [
    { id: 'file.new-session', label: '新建会话', hint: 'Ctrl+N',
      icon: 'plus', run: () => newChatSession() },
    { id: 'file.new-task', label: '新建任务', hint: 'Ctrl+Shift+T',
      icon: 'list-checks', run: () => openTaskDrawer(true) },
    { id: 'file.export', label: '导出当前对话', hint: 'Ctrl+S',
      icon: 'download', run: () => exportCurrentChat() },
    { id: 'file.open-kb', label: '打开知识库文档', hint: '',
      icon: 'book-open', run: () => { setActiveView('knowledge'); } },
    { id: 'file.close-tab', label: '关闭标签页', hint: 'Ctrl+W',
      icon: 'x', run: () => {
        if (state.activeTab) closeTab(state.activeTab);
      }, sep: true },
  ],
  edit: [
    { id: 'edit.copy', label: '复制', hint: 'Ctrl+C',
      icon: 'copy', run: () => copyLastAnswer() },
    { id: 'edit.clear', label: '清空当前会话', hint: '',
      icon: 'eraser', run: () => clearCurrentChat(), sep: true },
    { id: 'edit.regenerate', label: '重新生成最后一次回答', hint: '',
      icon: 'refresh-cw', run: () => regenerateLastAnswer() },
  ],
  view: [
    { id: 'view.toggle-sidebar', label: '切换侧栏显隐', hint: 'Ctrl+B',
      icon: 'panel-left', run: () => toggleSidebar() },
    { id: 'view.explorer', label: '资源管理器', hint: '',
      icon: 'files', run: () => setActiveView('explorer'), check: () => state.activeView === 'explorer' },
    { id: 'view.knowledge', label: '知识库', hint: '',
      icon: 'book-open', run: () => setActiveView('knowledge'), check: () => state.activeView === 'knowledge' },
    { id: 'view.tools', label: '工具', hint: '',
      icon: 'wrench', run: () => setActiveView('tools'), check: () => state.activeView === 'tools' },
    { id: 'view.tasks', label: '任务', hint: '',
      icon: 'list-checks', run: () => setActiveView('tasks'), check: () => state.activeView === 'tasks' },
    { id: 'view.sessions', label: '历史会话', hint: '',
      icon: 'history', run: () => setActiveView('sessions'), check: () => state.activeView === 'sessions' },
    { id: 'view.settings', label: '设置与状态', hint: '',
      icon: 'settings', run: () => setActiveView('settings'), check: () => state.activeView === 'settings' },
    { id: 'view.toggle-drawer', label: '切换任务抽屉', hint: '',
      icon: 'panel-bottom', run: () => toggleTaskDrawer(), sep: true },
    { id: 'view.reset-layout', label: '重置布局', hint: '',
      icon: 'rotate-ccw', run: () => resetLayout() },
  ],
};

/* ----------------------------------------------------------------------
   Lucide 风格图标（统一 16×16 viewBox，1.5 线宽，round cap/join）
   ---------------------------------------------------------------------- */
const ICONS = {
  'plus': '<path d="M8 3.2v9.6M3.2 8h9.6"/>',
  'x': '<path d="M4 4l8 8M12 4l-8 8"/>',
  'check': '<path d="M3 8.4 6.4 12 13 4.6"/>',
  'copy': '<rect x="5.5" y="5.5" width="8" height="8" rx="1.4"/>'
        + '<path d="M10.5 3.5v-.6a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h.6"/>',
  'refresh-cw': '<path d="M13.3 8a5.3 5.3 0 1 1-1.6-3.8"/><path d="M13.4 2.6v2.6h-2.6"/>',
  'rotate-ccw': '<path d="M2.7 8a5.3 5.3 0 1 0 1.6-3.8"/>'
              + '<path d="M2.6 2.6v2.6h2.6"/>',
  'eraser': '<path d="M6.5 13.2 2.8 9.5a1.2 1.2 0 0 1 0-1.7l5-5a1.2 1.2 0 0 1 1.7 0l3.7 3.7a1.2 1.2 0 0 1 0 1.7L9 13.2"/>'
          + '<path d="M5.5 5.5 10 10"/><path d="M6.5 13.2h6.7"/>',
  'download': '<path d="M8 2.6v7.2"/><path d="M5.2 7.2 8 10l2.8-2.8"/>'
             + '<path d="M2.8 12.2v.6a1.4 1.4 0 0 0 1.4 1.4h7.6a1.4 1.4 0 0 0 1.4-1.4v-.6"/>',
  'book-open': '<path d="M2.6 3.6A1.2 1.2 0 0 1 3.8 2.4h2.9A2.5 2.5 0 0 1 8 4.6v8.8a2.1 2.1 0 0 0-1.6-1H3.8a1.2 1.2 0 0 1-1.2-1.2z"/>'
             + '<path d="M13.4 3.6a1.2 1.2 0 0 0-1.2-1.2H9.3A2.5 2.5 0 0 0 8 4.6v8.8a2.1 2.1 0 0 1 1.6-1h2.6a1.2 1.2 0 0 0 1.2-1.2z"/>',
  'list-checks': '<path d="M10.4 4.2h3.2M10.4 8h3.2M10.4 11.8h3.2"/>'
               + '<path d="M2.6 4.2 3.6 5.2 5.4 3.4"/><path d="M2.6 8 3.6 9 5.4 7.2"/>'
               + '<path d="M2.6 11.8 3.6 12.8 5.4 11"/>',
  'wrench': '<path d="M14.1 6.3a3.8 3.8 0 0 1-4.8 4.8l-4.6 4.6a1.9 1.9 0 0 1-2.7-2.7l4.6-4.6a3.8 3.8 0 0 1 4.8-4.8L9.3 5.7l1.1 1.1z"/>',
  'history': '<path d="M2.8 8a5.2 5.2 0 1 1 1.6 3.7"/>'
           + '<path d="M2.6 14.6v-4.4h4.4"/><path d="M8 5.2V8l2.2 1.3"/>',
  'settings': '<circle cx="8" cy="8" r="2.2"/>'
             + '<path d="M12.9 10.2a1.1 1.1 0 0 0 .2 1.2l.1.1a1.3 1.3 0 1 1-1.8 1.8l-.1-.1a1.1 1.1 0 0 0-1.2-.2 1.1 1.1 0 0 0-.7 1v.2a1.3 1.3 0 1 1-2.6 0v-.1a1.1 1.1 0 0 0-.7-1 1.1 1.1 0 0 0-1.2.2l-.1.1a1.3 1.3 0 1 1-1.8-1.8l.1-.1a1.1 1.1 0 0 0 .2-1.2 1.1 1.1 0 0 0-1-.7h-.2a1.3 1.3 0 1 1 0-2.6h.1a1.1 1.1 0 0 0 1-.7 1.1 1.1 0 0 0-.2-1.2l-.1-.1a1.3 1.3 0 1 1 1.8-1.8l.1.1a1.1 1.1 0 0 0 1.2.2h.1a1.1 1.1 0 0 0 .7-1v-.2a1.3 1.3 0 1 1 2.6 0v.1a1.1 1.1 0 0 0 .7 1 1.1 1.1 0 0 0 1.2-.2l.1-.1a1.3 1.3 0 1 1 1.8 1.8l-.1.1a1.1 1.1 0 0 0-.2 1.2v.1a1.1 1.1 0 0 0 1 .7h.2a1.3 1.3 0 1 1 0 2.6h-.1a1.1 1.1 0 0 0-1 .7z"/>',
  'panel-left': '<rect x="2.4" y="2.8" width="11.2" height="10.4" rx="1.4"/>'
              + '<path d="M6.4 2.8v10.4"/>',
  'panel-bottom': '<rect x="2.4" y="2.8" width="11.2" height="10.4" rx="1.4"/>'
                 + '<path d="M2.4 10.2h11.2"/>',
  'files': '<path d="M4 2.6h4.6L10.2 4.2h3.4a1.2 1.2 0 0 1 1.2 1.2v6.4a1.2 1.2 0 0 1-1.2 1.2H4a1.2 1.2 0 0 1-1.2-1.2V3.8A1.2 1.2 0 0 1 4 2.6z"/>',
  'search': '<circle cx="7" cy="7" r="4.4"/><path d="M10.2 10.2 13.6 13.6"/>',
  'send': '<path d="M2.8 8h9.8"/><path d="M8.6 3.8 13.2 8l-4.6 4.2"/>',
  'chevron-right': '<path d="M6 3.6 10.4 8 6 12.4"/>',
  'sparkles': '<path d="M8 2.4 9.1 5.6 12.3 6.7 9.1 7.8 8 11 6.9 7.8 3.7 6.7 6.9 5.6z"/>'
             + '<path d="M12.4 10.4l.5 1.4 1.4.5-1.4.5-.5 1.4-.5-1.4-1.4-.5 1.4-.5z"/>',
};

/** 生成 16×16 的 Lucide 风格图标 SVG */
function icon(name, size) {
  const body = ICONS[name] || ICONS['chevron-right'];
  const s = size || 14;
  return '<svg class="ico" viewBox="0 0 16 16" width="' + s + '" height="' + s + '"'
    + ' fill="none" stroke="currentColor" stroke-width="1.5"'
    + ' stroke-linecap="round" stroke-linejoin="round">' + body + '</svg>';
}

/* ----------------------------------------------------------------------
   通用 popover 管理（点击外部关闭 / Esc 关闭）
   ---------------------------------------------------------------------- */
const popovers = {
  menu: { el: null, open: false, key: '' },
  model: { el: null, open: false },
  mode: { el: null, open: false },
};

function closePopover(kind) {
  const p = popovers[kind];
  if (!p || !p.el) return;
  p.el.hidden = true;
  p.open = false;
  const btn = p.trigger;
  if (btn) {
    btn.setAttribute('aria-expanded', 'false');
    btn.classList.remove('open');
  }
}

function closeAllPopovers(except) {
  Object.keys(popovers).forEach((k) => {
    if (k !== except) closePopover(k);
  });
}

function openPopover(kind, trigger, anchorRect, key) {
  const el = $(POPOVER_SEL[kind]);
  if (!el) return;
  closeAllPopovers(kind);
  el.hidden = false;
  popovers[kind].el = el;
  popovers[kind].open = true;
  popovers[kind].trigger = trigger;
  if (key) popovers[kind].key = key;
  if (trigger) {
    trigger.setAttribute('aria-expanded', 'true');
    trigger.classList.add('open');
  }
  // 定位：贴着触发按钮左下对齐，宽度固定
  const w = el.offsetWidth || 300;
  const h = el.offsetHeight || 200;
  let left = anchorRect ? anchorRect.left : 0;
  let top = anchorRect ? anchorRect.bottom + 4 : 0;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  if (left + w > vw - 8) left = Math.max(8, vw - w - 8);
  if (top + h > vh - 8) top = Math.max(8, vh - h - 8);
  el.style.left = Math.round(left) + 'px';
  el.style.top = Math.round(top) + 'px';
  el.classList.add('opening');
  setTimeout(() => el.classList.remove('opening'), 160);
}

const POPOVER_SEL = {
  menu: '#menu-popover',
  model: '#model-popover',
  mode: '#mode-popover',
};

/* ----------------------------------------------------------------------
   一、标题栏菜单
   ---------------------------------------------------------------------- */
function buildMenu(menuKey) {
  const box = $('#menu-items');
  if (!box) return;
  const defs = MENU_DEFS[menuKey] || [];
  box.innerHTML = '';
  defs.forEach((it) => {
    if (it.sep) {
      const sep = document.createElement('div');
      sep.className = 'menu-sep';
      box.appendChild(sep);
    }
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'menu-item';
    el.setAttribute('role', 'menuitem');
    el.dataset.menuId = it.id;
    const checked = it.check ? it.check() : false;
    el.innerHTML = '<span class="mi-ico">' + icon(it.icon, 14) + '</span>'
      + '<span class="mi-label">' + esc(it.label) + '</span>'
      + (it.hint ? '<span class="mi-hint">' + esc(it.hint) + '</span>' : '')
      + (checked ? '<span class="mi-check">' + icon('check', 12) + '</span>' : '');
    el.addEventListener('click', () => {
      closePopover('menu');
      try {
        it.run();
      } catch (e) {
        addSystemMsg('菜单项执行失败：' + e.message);
      }
    });
    box.appendChild(el);
  });
}

function initMenus() {
  const buttons = $$('.tb-menu-item');
  buttons.forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const key = btn.dataset.menu;
      // Cursor 行为：点同一个 → 关闭；点另一个 → 切换过去
      if (popovers.menu.open && popovers.menu.key === key) {
        closePopover('menu');
        return;
      }
      buildMenu(key);
      openPopover('menu', btn, btn.getBoundingClientRect(), key);
    });
    btn.addEventListener('mouseenter', () => {
      // 已打开时悬停切换菜单内容（Cursor 行为），但不重复定位
      if (popovers.menu.open && popovers.menu.key !== btn.dataset.menu) {
        buildMenu(btn.dataset.menu);
        popovers.menu.key = btn.dataset.menu;
      }
    });
  });

  // 点击外部关闭
  document.addEventListener('mousedown', (e) => {
    const inMenu = e.target.closest('#menu-popover');
    const inTrigger = e.target.closest('.tb-menu-item');
    if (!inMenu && !inTrigger) closePopover('menu');
    const inModel = e.target.closest('#model-popover');
    const inModelBtn = e.target.closest('#btn-model');
    if (!inModel && !inModelBtn) closePopover('model');
    const inMode = e.target.closest('#mode-popover');
    const inModeBtn = e.target.closest('#btn-mode');
    if (!inMode && !inModeBtn) closePopover('mode');
  });

  // Esc 关闭
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeAllPopovers();
    }
  });

  // Alt+F / Alt+E / Alt+V 唤起对应菜单
  document.addEventListener('keydown', (e) => {
    if (!e.altKey || e.ctrlKey || e.metaKey) return;
    const k = (e.key || '').toLowerCase();
    const map = { f: 'file', e: 'edit', v: 'view' };
    if (!map[k]) return;
    e.preventDefault();
    const btn = document.querySelector('.tb-menu-item[data-menu="' + map[k] + '"]');
    if (!btn) return;
    if (popovers.menu.open && popovers.menu.key === map[k]) {
      closePopover('menu');
      return;
    }
    buildMenu(map[k]);
    openPopover('menu', btn, btn.getBoundingClientRect(), map[k]);
  });
}

/* ----------------------------------------------------------------------
   二、菜单项的真实动作
   ---------------------------------------------------------------------- */
/** 新建会话：清空当前 tab 的消息流 */
function newChatSession() {
  state.threadId = null;
  state.sessionId = null;
  state.lastQuestion = '';
  const mt = $('#m-thread');
  if (mt) mt.textContent = '（新建）';
  /* ---- 重建空状态（welcome）----
   * 真实故障（2026-11-24 修复）：此前这里只 ``chat.innerHTML = ''``，
   * 接着只加一行 12px 灰色提示 → 视觉上就是**一片空白**，
   * 用户以为功能坏了。现在重新渲染 welcome 空状态。
   */
  if (typeof ensureChatPage === 'function') ensureChatPage();
  if (typeof renderWelcome === 'function') renderWelcome($('#chat'));
  // 新建会话 = 全新空白：输入框也要清掉
  const input = $('#input');
  if (input) {
    input.value = '';
    input.style.height = 'auto';
    input.focus();
  }
  activateTab('pg-chat');
  addSystemMsg('已新建会话。可直接输入问题开始。');
  toast('已新建会话');
}

/** 打开任务抽屉（可选强制打开） */
function openTaskDrawer(force) {
  const dr = $('#task-drawer');
  if (!dr) return;
  dr.hidden = force ? false : !dr.hidden;
  if (force) {
    const name = $('#task-name');
    if (name) { name.value = ''; name.focus(); }
    toast('已打开任务发布台');
  }
}

/** 切换任务抽屉 */
function toggleTaskDrawer() {
  const dr = $('#task-drawer');
  if (!dr) return;
  dr.hidden = !dr.hidden;
  toast(dr.hidden ? '已收起任务抽屉' : '已展开任务抽屉');
}

/** 切换侧栏显隐 */
function toggleSidebar() {
  const sb = $('#sidebar');
  if (!sb) return;
  sb.classList.toggle('collapsed');
  toast(sb.classList.contains('collapsed') ? '已隐藏侧栏' : '已显示侧栏');
}

/** 重置布局：侧栏宽度 / 活动栏视图 / 标签页 */
function resetLayout() {
  lsSet('leftWidth', 268);
  lsSet('activeView', 'explorer');
  const root = document.documentElement;
  root.style.setProperty('--sidebar-w', '268px');
  const sb = $('#sidebar');
  if (sb) sb.classList.remove('collapsed');
  state.tabs.forEach((t) => {
    if (t.id !== 'pg-chat') {
      const page = $('.page[data-page="' + t.id + '"]');
      if (page) page.remove();
    }
  });
  state.tabs = state.tabs.filter((t) => t.id === 'pg-chat');
  state.activeTab = 'pg-chat';
  activateTab('pg-chat');
  setActiveView('explorer');
  renderTabs();
  toast('布局已重置');
}

/** 导出当前对话（Markdown） */
async function exportCurrentChat() {
  const threadId = state.threadId;
  if (!threadId) {
    toast('当前还没有会话可导出');
    return;
  }
  try {
    const res = await api('/sessions/' + encodeURIComponent(threadId) + '/messages');
    const msgs = res.messages || [];
    if (!msgs.length) {
      toast('当前会话没有可导出的消息');
      return;
    }
    const lines = ['# 会话导出', '', '- thread_id: `' + threadId + '`',
      '- 导出时间: ' + new Date().toLocaleString('zh-CN'), '', '---', ''];
    msgs.forEach((m) => {
      const who = { user: '用户', assistant: '助手', tool: '工具', system: '系统' }
        [m.role] || m.role;
      lines.push('## ' + who + '（' + new Date((m.ts || 0) * 1000).toLocaleString('zh-CN') + '）');
      lines.push('');
      lines.push(String(m.content || '（空）'));
      if (m.citations && m.citations.length) {
        lines.push('');
        lines.push('引用：' + m.citations.map((c) => (c.chunk_id || c.id || JSON.stringify(c))).join('、'));
      }
      lines.push('');
    });
    if (typeof downloadText === 'function') {
      downloadText('session-' + threadId + '.md', lines.join('\n'), 'text/markdown;charset=utf-8');
    } else {
      const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'session-' + threadId + '.md';
      a.click();
    }
    toast('已导出当前对话（' + msgs.length + ' 条消息）');
  } catch (e) {
    toast('导出失败：' + e.message);
  }
}

/** 复制最后一次回答到剪贴板 */
async function copyLastAnswer() {
  const wraps = $$('#chat .msg.assistant');
  if (!wraps.length) {
    toast('还没有可复制的回答');
    return;
  }
  const last = wraps[wraps.length - 1];
  const body = last.querySelector('[data-body]');
  const text = body ? body.innerText : '';
  if (!text.trim()) {
    toast('最后一次回答是空的');
    return;
  }
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (e) {
    // 剪贴板 API 不可用（http / 权限）→ 降级用 execCommand
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    } catch (e2) { ok = false; }
  }
  if (ok) {
    toast('已复制最后一次回答（' + text.length + ' 字）');
    flash($('#chat .msg.assistant:last-child .body'), '已复制');
  } else {
    toast('复制失败：浏览器拒绝了剪贴板访问');
  }
}

/** 清空当前会话（消息流 + 本地状态；不删服务端历史） */
function clearCurrentChat() {
  const chat = $('#chat');
  if (!chat) return;
  /* ---- 重建空状态（welcome）----
   * 真实故障（2026-11-24 修复）：清空后只留一行 12px 灰色提示，
   * 视觉上就是**一片空白**。现在重新渲染 welcome 空状态。
   */
  if (typeof renderWelcome === 'function') renderWelcome(chat);
  state.threadId = null;
  state.sessionId = null;
  state.lastQuestion = '';
  const mt = $('#m-thread');
  if (mt) mt.textContent = '（新建）';
  activateTab('pg-chat');
  addSystemMsg('已清空当前会话视图（服务端历史仍保留，可在「历史会话」里查看）。');
  toast('已清空当前会话');
}

/** 重新生成最后一次回答（用同一 thread_id 再问一次） */
function regenerateLastAnswer() {
  const q = state.lastQuestion;
  if (!q) {
    toast('没有可重新生成的问题');
    return;
  }
  if (state.busy) {
    toast('当前正在回答中，请稍候');
    return;
  }
  addSystemMsg('正在重新生成最后一次回答…');
  send(q, { regenerate: true });
}

/* ----------------------------------------------------------------------
   三、交互模式（Agent / Ask / Plan）
   ---------------------------------------------------------------------- */
const MODES = [
  { id: 'agent', label: 'Agent',
    desc: '完整 ReAct 循环，可调用任意工具（写操作需人工确认）',
    icon: 'sparkles' },
  { id: 'ask', label: 'Ask',
    desc: '只读问答：仅允许 READ_ONLY / COMPUTE 工具，最多 1-2 轮调用',
    icon: 'search' },
  { id: 'plan', label: 'Plan',
    desc: '只规划不执行：产出结构化执行计划，不调用任何工具',
    icon: 'list-checks' },
];

function setMode(mode) {
  const m = (mode === 'ask' || mode === 'plan') ? mode : 'agent';
  state.mode = m;
  const chip = $('#btn-mode');
  if (chip) {
    chip.dataset.mode = m;
    chip.setAttribute('title', '交互模式：' + m.toUpperCase() + '（当前）');
  }
  const lbl = $('#mode-label');
  if (lbl) lbl.textContent = m.charAt(0).toUpperCase() + m.slice(1);
  const badge = $('#chat-mode-badge');
  if (badge) {
    badge.dataset.mode = m;
    badge.textContent = m.charAt(0).toUpperCase() + m.slice(1);
  }
  document.body.setAttribute('data-mode', m);
  const taskMode = $('#task-mode');
  if (taskMode) taskMode.value = m;
  lsSet('mode', m);
  return m;
}

function buildModeList() {
  const box = $('#mode-list');
  if (!box) return;
  box.innerHTML = '';
  MODES.forEach((m) => {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'mode-item' + (state.mode === m.id ? ' active' : '');
    el.dataset.mode = m.id;
    el.innerHTML = '<span class="mi-ico">' + icon(m.icon, 14) + '</span>'
      + '<span class="mode-text"><span class="mode-name">' + esc(m.label) + '</span>'
      + '<span class="mode-desc">' + esc(m.desc) + '</span></span>'
      + (state.mode === m.id ? '<span class="mi-check">' + icon('check', 12) + '</span>' : '');
    el.addEventListener('click', () => {
      setMode(m.id);
      buildModeList();
      closePopover('mode');
      toast('交互模式已切换为 ' + m.label);
    });
    box.appendChild(el);
  });
}

/* ----------------------------------------------------------------------
   四、模型选择器
   ---------------------------------------------------------------------- */
const models = {
  list: [],
  editing: null,
};

async function refreshModels() {
  try {
    const res = await api('/models');
    models.list = res.models || [];
    renderModelList();
    syncModelSelects();
    const cur = models.list.find((m) => m.name === state.model);
    const lbl = $('#model-label');
    if (lbl) lbl.textContent = cur ? (cur.label || cur.name) : (state.model || '未选择');
    const sel = $('#model-select');
    if (sel && cur) sel.value = cur.name;
  } catch (e) {
    const lbl = $('#model-label');
    if (lbl) lbl.textContent = '模型加载失败';
  }
}

function renderModelList() {
  const box = $('#model-list');
  if (!box) return;
  box.innerHTML = '';
  if (!models.list.length) {
    box.innerHTML = '<div class="muted sm">没有可用模型</div>';
    return;
  }
  models.list.forEach((m) => {
    const active = m.name === state.model;
    const el = document.createElement('div');
    el.className = 'model-item' + (active ? ' active' : '');
    el.dataset.model = m.name;
    const reach = m.reachable === null ? null : !!m.reachable;
    const dotCls = reach === null ? 'dot' : (reach ? 'dot on' : 'dot off');
    const status = reach === null ? '未探测'
      : (reach ? '可达' : '不可达');
    el.innerHTML = '<span class="m-status ' + dotCls + '" title="' + esc(status)
      + (m.error ? '：' + esc(m.error) : '') + '"></span>'
      + '<span class="m-main">'
      + '<span class="m-name">' + esc(m.label || m.name)
      + (m.builtin ? '' : '<span class="m-badge">自定义</span>') + '</span>'
      + '<span class="m-url mono">' + esc(m.base_url || '') + '</span>'
      + (m.description ? '<span class="m-desc">' + esc(m.description) + '</span>' : '')
      + '</span>'
      + '<span class="m-actions">'
      + '<button type="button" class="icon-btn m-edit" title="编辑" aria-label="编辑模型">'
      + icon('settings', 13) + '</button>'
      + (m.builtin ? '' : '<button type="button" class="icon-btn m-del" title="删除" aria-label="删除模型">'
      + icon('eraser', 13) + '</button>')
      + '</span>';
    el.addEventListener('click', (e) => {
      if (e.target.closest('.m-edit') || e.target.closest('.m-del')) return;
      state.model = m.name;
      const lbl = $('#model-label');
      if (lbl) lbl.textContent = m.label || m.name;
      const sel = $('#model-select');
      if (sel) sel.value = m.name;
      const tm = $('#task-model');
      if (tm) tm.value = m.name;
      lsSet('model', m.name);
      renderModelList();
      closePopover('model');
      toast('已选择模型：' + (m.label || m.name));
    });
    const editBtn = el.querySelector('.m-edit');
    if (editBtn) {
      editBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        openModelModal(m);
      });
    }
    const delBtn = el.querySelector('.m-del');
    if (delBtn) {
      delBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!window.confirm('确定删除自定义模型「' + m.name + '」？')) return;
        try {
          await api('/models/' + encodeURIComponent(m.name), { method: 'DELETE' });
          if (state.model === m.name) state.model = 'gpt-oss-20b';
          await refreshModels();
          toast('已删除模型 ' + m.name);
        } catch (err) {
          toast('删除失败：' + err.message);
        }
      });
    }
    box.appendChild(el);
  });
}

/** 同步所有模型下拉（隐藏 select + 任务抽屉 select） */
function syncModelSelects() {
  const sel = $('#model-select');
  if (sel) {
    sel.innerHTML = '';
    models.list.forEach((m) => {
      const o = document.createElement('option');
      o.value = m.name;
      o.textContent = m.label || m.name;
      sel.appendChild(o);
    });
    if (!models.list.find((m) => m.name === state.model) && models.list.length) {
      state.model = models.list[0].name;
    }
    sel.value = state.model;
  }
  const tm = $('#task-model');
  if (tm) {
    const cur = tm.value;
    tm.innerHTML = '';
    models.list.forEach((m) => {
      const o = document.createElement('option');
      o.value = m.name;
      o.textContent = m.label || m.name;
      tm.appendChild(o);
    });
    if (models.list.find((m) => m.name === cur)) tm.value = cur;
    else if (models.list.find((m) => m.name === state.model)) tm.value = state.model;
  }
}

/* ---- 自定义模型模态框 ---- */
function openModelModal(model) {
  models.editing = model ? model.name : null;
  $('#mm-title').textContent = model ? ('编辑模型：' + model.name) : '添加自定义模型';
  $('#mm-msg').textContent = '';
  $('#mm-name').value = model ? model.name : '';
  $('#mm-name').disabled = !!model;
  $('#mm-base-url').value = model ? model.base_url : '';
  $('#mm-model').value = model ? model.model : '';
  $('#mm-desc').value = model ? (model.description || '') : '';
  $('#mm-effort').value = model ? (model.reasoning_effort || 'medium') : 'medium';
  $('#mm-temp').value = model ? (model.temperature != null ? model.temperature : 0.2) : 0.2;
  $('#mm-maxtok').value = model ? (model.max_tokens || 2048) : 2048;
  $('#mm-timeout').value = model ? (model.timeout || 180) : 180;
  $('#mm-tools').value = model ? (model.supports_tools === false ? 'false' : 'true') : 'true';
  $('#mm-mask').hidden = false;
  $('#mm-name').focus();
}

function closeModelModal() {
  $('#mm-mask').hidden = true;
  models.editing = null;
}

async function saveModel() {
  const msg = $('#mm-msg');
  const name = ($('#mm-name').value || '').trim();
  const baseUrl = ($('#mm-base-url').value || '').trim();
  if (!name) { msg.innerHTML = '<span class="pill pill-err">请填写名称</span>'; return; }
  if (!baseUrl) { msg.innerHTML = '<span class="pill pill-err">请填写 Base URL</span>'; return; }
  const payload = {
    name: name,
    base_url: baseUrl,
    model: ($('#mm-model').value || '').trim() || name,
    description: ($('#mm-desc').value || '').trim(),
    reasoning_effort: $('#mm-effort').value,
    temperature: parseFloat($('#mm-temp').value) || 0.2,
    max_tokens: parseInt($('#mm-maxtok').value, 10) || 2048,
    timeout: parseFloat($('#mm-timeout').value) || 180,
    supports_tools: $('#mm-tools').value === 'true',
  };
  const btn = $('#mm-save');
  btn.disabled = true;
  msg.textContent = '保存并探测端点…';
  try {
    let res;
    if (models.editing) {
      res = await api('/models/' + encodeURIComponent(models.editing), {
        method: 'PUT', body: JSON.stringify(payload),
      });
    } else {
      res = await api('/models', { method: 'POST', body: JSON.stringify(payload) });
    }
    await refreshModels();
    const reachable = res.reachable;
    msg.innerHTML = '<span class="pill ' + (reachable ? 'pill-ok' : 'pill-err') + '">'
      + (reachable ? '已保存，端点可达' : '已保存，但端点不可达')
      + '</span>' + (res.error ? ' ' + esc(res.error) : '');
    if (!models.editing) {
      // 新建成功后留在模态框，允许继续填
    } else {
      setTimeout(closeModelModal, 700);
    }
    toast('模型已保存：' + name);
  } catch (e) {
    msg.innerHTML = '<span class="pill pill-err">' + esc(e.message) + '</span>';
  } finally {
    btn.disabled = false;
  }
}

/* ----------------------------------------------------------------------
   五、Plan 计划卡片
   ---------------------------------------------------------------------- */
function addPlanCard(wrap, plan, threadId) {
  const box = document.createElement('div');
  box.className = 'plan-card';
  const steps = plan.steps || [];
  const riskCls = (r) => (r === '高' ? 'risk-high' : (r === '中' ? 'risk-mid' : 'risk-low'));
  box.innerHTML =
    '<div class="plan-head">'
    + '<span class="plan-ico">' + icon('list-checks', 14) + '</span>'
    + '<span class="plan-title">执行计划</span>'
    + '<span class="muted sm">（尚未执行任何工具）</span>'
    + '</div>'
    + (plan.goal ? '<div class="plan-goal">' + esc(plan.goal) + '</div>' : '')
    + '<ol class="plan-steps">'
    + steps.map((s, i) => '<li class="plan-step">'
        + '<span class="ps-idx">' + (i + 1) + '</span>'
        + '<span class="ps-body">'
        + '<span class="ps-title">' + esc(s.title || '') + '</span>'
        + (s.detail ? '<span class="ps-detail">' + esc(s.detail) + '</span>' : '')
        + (s.tools && s.tools.length
            ? '<span class="ps-tools">' + s.tools.map((t) =>
                '<span class="ps-tool mono">' + esc(t) + '</span>').join('') + '</span>'
            : '')
        + '</span>'
        + '<span class="ps-risk ' + riskCls(s.risk) + '">' + esc(s.risk || '低') + '</span>'
        + '</li>').join('')
    + '</ol>'
    + ((plan.risks && plan.risks.length)
        ? '<div class="plan-risks"><span class="pr-title">风险点</span>'
          + plan.risks.map((r) => '<span class="pr-item">' + esc(r) + '</span>').join('')
          + '</div>' : '')
    + ((plan.questions && plan.questions.length)
        ? '<div class="plan-questions"><span class="pr-title">待确认</span>'
          + plan.questions.map((q) => '<span class="pr-item">' + esc(q) + '</span>').join('')
          + '</div>' : '')
    + '<div class="plan-actions">'
    + '<button type="button" class="btn btn-ok sm act-run-plan">执行此计划</button>'
    + '<button type="button" class="btn btn-ghost sm act-drop-plan">放弃</button>'
    + '</div>';

  const runBtn = box.querySelector('.act-run-plan');
  const dropBtn = box.querySelector('.act-drop-plan');
  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    dropBtn.disabled = true;
    box.querySelector('.plan-actions').innerHTML =
      '<span class="pill pill-info">正在以 Agent 模式执行该计划…</span>';
    await executePlan(plan, threadId, box);
  });
  dropBtn.addEventListener('click', () => {
    box.querySelector('.plan-actions').innerHTML =
      '<span class="pill">已放弃该计划</span>';
    box.classList.add('dropped');
    addSystemMsg('已放弃该执行计划。');
  });
  wrap.appendChild(box);
  scrollDown();
}

/** 以 agent 模式真正执行计划（同一 thread_id） */
async function executePlan(plan, threadId, box) {
  const goal = plan.goal || state.lastQuestion || '执行刚才的计划';
  const stepsText = (plan.steps || [])
    .map((s, i) => `${i + 1}. ${s.title}${s.detail ? '（' + s.detail + '）' : ''}`
      + (s.tools && s.tools.length ? ' 用到工具：' + s.tools.join('、') : ''))
    .join('\n');
  const prompt = '请按以下执行计划逐步完成目标「' + goal + '」：\n'
    + stepsText + '\n\n请开始执行。';
  // UI 上也要切回 Agent 模式（否则用户看到的是 Plan 芯片，实际跑的是 Agent）
  const prevMode = state.mode;
  if (typeof setMode === 'function') setMode('agent');
  try {
    const res = await api('/chat', {
      method: 'POST',
      body: JSON.stringify({
        message: prompt,
        thread_id: threadId || state.threadId,
        session_id: state.sessionId,
        model: state.model,
        mode: 'agent',
        stream: false,
      }),
    });
    const follow = addAssistantMsg();
    fillAssistant(follow, res.answer || '', {
      tokens_used: res.tokens_used,
      tool_calls_used: res.tool_calls_used,
      stop_reason: res.stop_reason,
      duration_ms: null,
      model: res.model || state.model || '',
      mode: res.mode || 'agent',
    });
    if (res.status === 'pending_approval' && res.approvals && res.approvals.length) {
      addHitlCard(follow, res.approvals, res.thread_id || threadId);
    }
    if (box) {
      box.querySelector('.plan-actions').innerHTML =
        '<span class="pill ' + (res.status === 'error' ? 'pill-err' : 'pill-ok') + '">'
        + '计划已执行（' + esc(res.stop_reason || res.status) + '）</span>';
    }
    refreshSessions();
  } catch (e) {
    if (box) {
      box.querySelector('.plan-actions').innerHTML =
        '<span class="pill pill-err">执行失败：' + esc(e.message) + '</span>';
    }
  }
}

/* ----------------------------------------------------------------------
   六、会话摘要卡片
   ---------------------------------------------------------------------- */
async function generateSummary(threadId) {
  const tid = threadId || state.threadId;
  if (!tid) {
    toast('当前还没有会话，无法生成摘要');
    return;
  }
  const btn = $('#btn-summarize');
  if (btn) btn.disabled = true;
  addSystemMsg('正在用本地模型生成会话摘要…');
  try {
    const res = await api('/sessions/' + encodeURIComponent(tid) + '/summarize',
      { method: 'POST' });
    renderSummaryCard(res);
    addSystemMsg('摘要已生成并持久化。');
    toast('摘要已生成');
  } catch (e) {
    addSystemMsg('摘要生成失败：' + e.message);
    toast('摘要生成失败：' + e.message);
  } finally {
    if (btn) btn.disabled = false;
    refreshSessions();
  }
}

/** 构造摘要卡片元素（对话页与历史会话页共用） */
function buildSummaryCard(summary) {
  const box = document.createElement('div');
  box.className = 'summary-card';
  box.innerHTML =
    '<div class="sum-head">' + icon('sparkles', 14)
    + '<span class="sum-title">会话摘要</span>'
    + (summary.model ? '<span class="muted sm mono">' + esc(summary.model) + '</span>' : '')
    + '</div>'
    + '<div class="sum-conclusion">' + esc(summary.conclusion || '（无结论）') + '</div>'
    + ((summary.key_points || []).length
        ? '<div class="sum-sec"><span class="sum-sec-t">关键要点</span><ul class="sum-list">'
          + summary.key_points.map((p) => '<li>' + esc(p) + '</li>').join('')
          + '</ul></div>' : '')
    + ((summary.documents || []).length
        ? '<div class="sum-sec"><span class="sum-sec-t">涉及文档 / 引用</span>'
          + '<div class="sum-chips">'
          + summary.documents.map((d) => '<span class="cite">' + esc(d) + '</span>').join('')
          + '</div></div>' : '')
    + ((summary.open_questions || []).length
        ? '<div class="sum-sec"><span class="sum-sec-t">待办 / 未解决</span><ul class="sum-list">'
          + summary.open_questions.map((q) => '<li>' + esc(q) + '</li>').join('')
          + '</ul></div>' : '');
  return box;
}

function renderSummaryCard(summary) {
  const wrap = addAssistantMsg();
  wrap.classList.add('summary-msg');
  const box = buildSummaryCard(summary);
  wrap.appendChild(box);
  scrollDown();
  return wrap;
}

/* ----------------------------------------------------------------------
   七、历史会话 → 打开 tab 并渲染完整历史
   ---------------------------------------------------------------------- */
async function openSessionThread(threadId, title) {
  const tabId = 'pg-sess-' + String(threadId).replace(/[^a-zA-Z0-9]/g, '');
  let tab = state.tabs.find((t) => t.id === tabId);
  let page = $('.page[data-page="' + tabId + '"]');
  if (!page) {
    tab = openTab('session', '会话 ' + (title || threadId).slice(0, 18),
      { threadId: threadId }, tabId);
    page = $('.page[data-page="' + tabId + '"]');
  }
  state.threadId = threadId;
  const mt = $('#m-thread');
  if (mt) mt.textContent = threadId;
  if (page) {
    page.innerHTML = '<div class="doc-page-inner">'
      + '<h2>' + esc(title || ('会话 ' + threadId)) + '</h2>'
      + '<div class="muted sm mono">' + esc(threadId) + '</div>'
      + '<div class="sess-actions">'
      + '<button type="button" class="btn btn-primary sm act-sess-summary">生成摘要</button>'
      + '<button type="button" class="btn btn-ghost sm act-sess-continue">在此会话继续提问</button>'
      + '</div>'
      + '<div id="sess-summary"></div>'
      + '<div id="sess-history" class="sess-history"><div class="muted">加载中…</div></div>'
      + '</div>';
    // 「生成摘要」按钮（作用于本会话）
    const sBtn = page.querySelector('.act-sess-summary');
    if (sBtn) {
      sBtn.addEventListener('click', async () => {
        sBtn.disabled = true;
        sBtn.textContent = '生成中…';
        try {
          const res = await api('/sessions/' + encodeURIComponent(threadId) + '/summarize',
            { method: 'POST', body: JSON.stringify({}) });
          const holder = page.querySelector('#sess-summary');
          if (holder) {
            holder.innerHTML = '';
            holder.appendChild(buildSummaryCard(res.summary || res));
          }
        } catch (e) {
          const holder = page.querySelector('#sess-summary');
          if (holder) {
            holder.innerHTML = '<div class="pill pill-err">摘要失败：' + esc(e.message) + '</div>';
          }
        } finally {
          sBtn.disabled = false;
          sBtn.textContent = '生成摘要';
        }
      });
    }
    // 「继续对话」：切到对话页并沿用同一 thread_id
    const cBtn = page.querySelector('.act-sess-continue');
    if (cBtn) {
      cBtn.addEventListener('click', () => {
        activateTab('pg-chat');
        addSystemMsg('已切到对话页，将以 thread_id=' + threadId + ' 继续提问。');
        const input = $('#input');
        if (input) input.focus();
      });
    }
  }
  activateTab(tabId);
  try {
    const res = await api('/sessions/' + encodeURIComponent(threadId) + '/messages');
    const msgs = res.messages || [];
    const box = $('#sess-history');
    if (!box) return;
    if (!msgs.length) {
      box.innerHTML = '<div class="muted">该会话没有已持久化的消息。</div>';
      return;
    }
    box.innerHTML = '';
    msgs.forEach((m) => box.appendChild(renderHistoryMessage(m)));
    const notice = document.createElement('div');
    notice.className = 'sess-notice';
    notice.textContent = res.notice || '';
    box.appendChild(notice);
    // 「继续对话」按钮：用同一 thread_id 续跑
    const cont = document.createElement('div');
    cont.className = 'sess-continue';
    cont.innerHTML = '<button type="button" class="btn btn-primary sm act-continue">'
      + '在此会话继续提问</button>'
      + '<span class="muted sm">（用同一 thread_id 续跑；重启后无法从历史中间断点续跑 ReAct）</span>';
    cont.querySelector('.act-continue').addEventListener('click', () => {
      activateTab('pg-chat');
      addSystemMsg('已切到对话页，将以 thread_id=' + threadId + ' 继续提问。');
      const input = $('#input');
      if (input) input.focus();
    });
    box.appendChild(cont);
  } catch (e) {
    const box = $('#sess-history');
    if (box) box.innerHTML = '<div class="pill pill-err">加载失败：' + esc(e.message) + '</div>';
  }
}

/** 渲染一条历史消息（与实时流一致：引用 chip + 工具折叠行） */
function renderHistoryMessage(m) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + (m.role === 'user' ? 'user' : (m.role === 'assistant' ? 'assistant' : 'system'));
  if (m.role === 'assistant') {
    wrap.innerHTML = '<span class="role-tag">Agent</span><div class="body" data-body></div>'
      + '<div class="toolrows"></div><div class="meta"></div><div class="cites"></div>';
  } else if (m.role === 'user') {
    wrap.innerHTML = '<span class="role-tag">你</span><div class="body"></div>';
  } else {
    wrap.innerHTML = '<div class="body"></div>';
  }
  const bodyEl = $('[data-body]', wrap) || $('.body', wrap);
  const r = renderAnswerHtml(m.content || '');
  if (bodyEl) bodyEl.innerHTML = r.html || '<span class="muted">(空)</span>';
  const citesBox = $('.cites', wrap);
  if (citesBox) {
    (m.citations || []).forEach((c) => {
      const id = c.chunk_id || c.id || '';
      if (!id) return;
      const el = document.createElement('span');
      el.className = 'cite';
      el.textContent = '来源:' + id;
      citesBox.appendChild(el);
    });
  }
  const toolsBox = $('.toolrows', wrap);
  if (toolsBox && m.tool_calls && m.tool_calls.length) {
    m.tool_calls.forEach((tc) => {
      // 优先渲染结构化轨迹块（参数/结果/耗时/错误分区）；
      // 旧数据缺字段时回退到纯文本折叠行。
      if (typeof addTraceRow === 'function' && tc.args !== undefined) {
        addTraceRow(wrap, tc);
      } else {
        addToolRow(wrap, tc.name || '(工具)',
          JSON.stringify(tc.args || {}, null, 2));
      }
    });
  }
  // 历史会话恢复：把落盘的 trace / budget 灌回全局缓存，
  // 这样「决策轨迹」面板对历史会话也能展示完整决策链。
  const extra = m.extra || {};
  if (extra.trace && extra.trace.length) {
    if (typeof tvSetTrace === 'function') tvSetTrace(m.thread_id || null, extra.trace, extra.budget || null);
  }
  const metaBox = $('.meta', wrap);
  if (metaBox) {
    const e = m.extra || {};
    const bits = [];
    if (e.tokens_used != null) bits.push('token <b>' + e.tokens_used + '</b>');
    if (e.tool_calls_used != null) bits.push('工具调用 <b>' + e.tool_calls_used + '</b>');
    if (e.stop_reason) bits.push('stop_reason <b>' + esc(e.stop_reason) + '</b>');
    if (e.mode) bits.push('模式 <b>' + esc(e.mode) + '</b>');
    if (e.model) bits.push('模型 <b>' + esc(e.model) + '</b>');
    if (m.ts) bits.push('<b>' + fmtTime(m.ts) + '</b>');
    metaBox.innerHTML = bits.join('');
  }
  return wrap;
}

/* ----------------------------------------------------------------------
   八、轻量 toast（菜单动作的真实反馈）
   ---------------------------------------------------------------------- */
function toast(msg) {
  let box = $('#toast-box');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toast-box';
    box.className = 'toast-box';
    document.body.appendChild(box);
  }
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 200);
  }, 2200);
}

/** 文本闪烁（复制成功的即时反馈） */
function flash(el, text) {
  if (!el) return;
  const old = el.style.position;
  el.style.position = 'relative';
  const tag = document.createElement('span');
  tag.className = 'flash-tag';
  tag.textContent = text;
  el.appendChild(tag);
  setTimeout(() => tag.remove(), 1400);
  el.style.position = old;
}

/* ----------------------------------------------------------------------
   九、绑定
   ---------------------------------------------------------------------- */
let _enhancementsBound = false;

function bindEnhancements() {
  // 幂等保护：app.js 的 boot() 与本文件的 initEnhancements() 都会调用本函数，
  // 重复绑定会让同一个按钮出现两个 click 监听器（第二个会把刚打开的 popover 关掉）。
  if (_enhancementsBound) return;
  _enhancementsBound = true;
  // 模式 chip
  const btnMode = $('#btn-mode');
  if (btnMode) {
    btnMode.addEventListener('click', (e) => {
      e.stopPropagation();
      if (popovers.mode.open) { closePopover('mode'); return; }
      buildModeList();
      openPopover('mode', btnMode, btnMode.getBoundingClientRect());
    });
  }
  const modeClose = $('#mode-popover-close');
  if (modeClose) modeClose.addEventListener('click', () => closePopover('mode'));

  // 模型 chip
  const btnModel = $('#btn-model');
  if (btnModel) {
    btnModel.addEventListener('click', (e) => {
      e.stopPropagation();
      if (popovers.model.open) { closePopover('model'); return; }
      refreshModels();
      openPopover('model', btnModel, btnModel.getBoundingClientRect());
    });
  }
  const modelClose = $('#model-popover-close');
  if (modelClose) modelClose.addEventListener('click', () => closePopover('model'));

  // 添加自定义模型
  const btnAdd = $('#btn-add-model');
  if (btnAdd) {
    btnAdd.addEventListener('click', () => {
      closePopover('model');
      openModelModal(null);
    });
  }
  // 模态框
  const mask = $('#mm-mask');
  if (mask) {
    mask.addEventListener('click', (e) => { if (e.target === mask) closeModelModal(); });
  }
  const mc = $('#mm-close');
  if (mc) mc.addEventListener('click', closeModelModal);
  const mcn = $('#mm-cancel');
  if (mcn) mcn.addEventListener('click', closeModelModal);
  const ms = $('#mm-save');
  if (ms) ms.addEventListener('click', saveModel);

  // 摘要按钮
  const sumBtn = $('#btn-summarize');
  if (sumBtn) sumBtn.addEventListener('click', () => generateSummary(state.threadId));

  // 隐藏的 select 仍然作为状态同步点（保留向后兼容的 DOM 契约）
  const modelSel = $('#model-select');
  if (modelSel) {
    modelSel.addEventListener('change', () => {
      state.model = modelSel.value;
      const lbl = $('#model-label');
      if (lbl) lbl.textContent = state.model;
      const tm = $('#task-model');
      if (tm) tm.value = state.model;
    });
  }

  // 快捷键：Ctrl+S 导出 / Ctrl+Shift+T 新建任务
  document.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    const k = (e.key || '').toLowerCase();
    if (k === 's' && !e.shiftKey) {
      e.preventDefault();
      exportCurrentChat();
    } else if (k === 't' && e.shiftKey) {
      e.preventDefault();
      openTaskDrawer(true);
    }
  });
}

/* ----------------------------------------------------------------------
   启动
   ---------------------------------------------------------------------- */
(function bootEnhancements() {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initEnhancements);
  } else {
    initEnhancements();
  }

  function initEnhancements() {
    // 恢复持久化的模式 / 模型
    setMode(lsGet('mode', 'agent'));
    state.model = lsGet('model', 'gpt-oss-20b');
    initMenus();
    bindEnhancements();
    refreshModels();
    setInterval(refreshModels, 30000);   // 定期刷新模型可达性
  }
})();
