/* ==========================================================================
   Agent平台 —— 原生 JS（无框架、无 CDN）
   Cursor / VS Code 风格工作台
   · 布局：活动栏（切侧栏视图）+ 侧栏（树视图）+ 主区（标签页 + 对话 + 输入区）
   · SSE 接收用 fetch + ReadableStream 解析 "data: " 行（需要 POST body，故不用 EventSource）
   · 引用：[来源:xxx] / [doc_id:chunk_id] → 引用 chip（小圆角标签）
   · HITL 审批卡片：批准 / 拒绝 + 审批人 + 会话级授权（session_grant）
   · 侧栏宽度可拖拽调整（宽度 / 标签页 / 活动栏选中项持久化到 localStorage）
   · Enter 发送 / Shift+Enter 换行（兼容 IME 中文输入）
   ========================================================================== */
'use strict';

const API = '';                       // 与页面同源
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

/** 全局状态 */
const state = {
  threadId: null,                    // 当前会话 thread_id
  sessionId: null,                   // 当前会话 session_id（session_grant 用）
  busy: false,
  templates: [],
  tasksTimer: null,
  toolsExpanded: false,              // 工具清单是否「展开全部」
  tasksExpanded: false,              // 任务列表是否「查看全部」
  sessionsLimit: 30,                 // 历史会话已加载条数（分页加载）
  activeView: 'explorer',            // 活动栏当前视图
  tabs: [],                          // [{id, type, title, ...}]
  activeTab: null,
  tabSeq: 0,
  model: 'gpt-oss-20b',
  mode: 'agent',                      // 交互模式：agent | ask | plan
  lastQuestion: '',                  // 最后一次提问（供「重新生成」使用）
  /** **当前挂起的审批项数量**（2026-11-24 新增）。
   *  > 0 表示有工具正**等待人工批准**（还没执行）。
   *  用于：① 用户敲「确认执行」时弹提示（对话文字不会自动放行）；
   *       ② 审批卡片高亮。 */
  pendingApprovalCount: 0,
  /** **已决策的审批项**（2026-11-24 新增，审批统一入口）。
   *  ``{ [tool_call_id]: 'approved' | 'rejected' }``
   *
   *  ⚠️ 真正的事实来源在 ``approval.js`` 的 ``APPROVAL.resolved``；
   *  这里保留一份镜像，方便本文件内快速判断「这张卡片是否已处理过」
   *  （例如页面刷新后重建卡片时不要重复渲染成「待审批」）。 */
  resolvedApprovals: {},
};

/* ----------------------------------------------------------------------
   对话空状态（welcome）
   ----------------------------------------------------------------------
   为什么要有这一段（真实故障）：
   「新建会话」把 ``#chat`` 清空后**只**留下一行 12px 的灰色系统提示
   （「已新建会话。」）→ 视觉上就是**一片空白**，用户以为功能坏了。
   此前 welcome 空状态只写在 index.html 的静态骨架里，JS 从不重建它
   → 一旦被清空就**永远回不来**。

   现在把这段 markup 收进 JS 常量（WELCOME_HTML）作为**唯一事实来源**：
   · boot() 启动时渲染一次；
   · 「新建会话」时重新渲染；
   · 对话页被意外删除后重建时也渲染。
   index.html 里的静态块会被 boot() 的渲染直接覆盖（无害冗余），
   保留它是为了让页面在 JS 尚未加载时也不至于完全空白。
   ---------------------------------------------------------------------- */
const WELCOME_HTML = ''
  + '<div class="welcome">'
  + '<svg viewBox="0 0 16 16" width="30" height="30" fill="none" stroke="currentColor"'
  + ' stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round">'
  + '<path d="M7.4 1.6 8.4 4.7l3.1 1-3.1 1-1 3.1-1-3.1-3.1-1 3.1-1z" />'
  + '<path d="M12.2 8.8l.6 1.9 1.9.6-1.9.6-.6 1.9-.6-1.9-1.9-.6 1.9-.6z" />'
  + '</svg>'
  + '<h3>Agent 对话</h3>'
  + '<p>输入问题 → Agent 调用知识库 / 工具检索 → 返回带引用 chip 的答案。<br />'
  + '   涉及写操作（report_render / notify）会弹出人工确认卡片。</p>'
  + '<p class="muted">试试：「T101_VDD_FUNCT 的上下限是多少？请引用来源。」</p>'
  + '</div>';

/**
 * 渲染对话空状态（welcome）。
 * @param {Element|null} container 目标容器；缺省为当前可见的 #chat
 * @returns {Element|null} 渲染出的 .welcome 元素（找不到容器时返回 null）
 */
function renderWelcome(container) {
  const host = container || $('#chat');
  if (!host) return null;
  host.innerHTML = WELCOME_HTML;
  return $('.welcome', host);
}

/* ----------------------------------------------------------------------
   待审批相关的辅助（2026-11-24 新增）
   ----------------------------------------------------------------------
   真实故障：工具（尤其 ws_write_file）挂在 HITL 上等批准时，
   轨迹上却显示绿色 ✓「成功」→ 用户以为「代码都写不进去了」。
   实际只是**没点批准**。这三段代码负责把「等批准」这件事说清楚。
   ---------------------------------------------------------------------- */

/** 用户输入是否像是在「催促执行 / 放行审批」 */
function looksLikeApprovalCommand(text) {
  const t = String(text || '').toLowerCase().trim();
  if (!t) return false;
  const keys = [
    '执行', '确认执行', '批准', '同意', '放行', '允许', '继续', 'go ahead',
    'proceed', 'approve', 'confirm', 'do it', '继续写', '开始写', '写入',
  ];
  return keys.some((k) => t.indexOf(k) >= 0);
}

/** 把所有待审批卡片重新高亮并滚动到视野中央 */
function highlightPendingApprovals() {
  const boxes = Array.from(document.querySelectorAll('.hitl.is-pending'));
  boxes.forEach((b) => {
    b.classList.remove('is-flash');
    /* 强制重排以重启动画 */
    void b.offsetWidth;
    b.classList.add('is-flash');
    try {
      if (typeof b.scrollIntoView === 'function') {
        b.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    } catch (e) { /* 忽略 */ }
  });
  return boxes.length;
}

/* ----------------------------------------------------------------------
   ReAct 轮次上限（2026-11-24 新增）
   ----------------------------------------------------------------------
   「max_rounds」此前只存在于「任务发布台」，**对话**流程完全用不到
   （服务端硬编码 8 轮）→ 用户抱怨「才几步就停了」。
   现在输入区有一个紧凑数字框（#chat-rounds），与任务发布台的
   #p-rounds **双向同步**：改任一处，另一处跟着变。
   ---------------------------------------------------------------------- */

/** 默认 ReAct 轮次上限（与后端 DEFAULT_MAX_ROUNDS 保持一致） */
const DEFAULT_CHAT_MAX_ROUNDS = 20;

/** 读当前的轮次上限（输入框优先，缺省 20） */
function chatMaxRounds() {
  const el = $('#chat-rounds');
  const n = el ? parseInt(el.value, 10) : NaN;
  if (!isFinite(n) || n < 1) return DEFAULT_CHAT_MAX_ROUNDS;
  return Math.min(64, n);
}

/** 设置轮次上限（同时同步任务发布台的输入框） */
function setChatMaxRounds(n) {
  const v = Math.max(1, Math.min(64, parseInt(n, 10) || DEFAULT_CHAT_MAX_ROUNDS));
  const a = $('#chat-rounds');
  if (a) a.value = String(v);
  const b = $('#p-rounds');
  if (b) b.value = String(v);
  return v;
}

/* ----------------------------------------------------------------------
   工具函数
   ---------------------------------------------------------------------- */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function api(path, opts) {
  const res = await fetch(API + path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error || data.message)) || ('HTTP ' + res.status);
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    // 带上 HTTP 状态码：调用方要区分「404/405 → 接口不存在」与「500 → 真出错」，
    // 光看 message 文本不可靠（FastAPI 的 detail 可能是任意文案）。
    err.status = res.status;
    throw err;
  }
  return data;
}

function fmtMs(ms) {
  if (ms == null) return '–';
  if (ms < 1000) return ms + ' ms';
  return (ms / 1000).toFixed(2) + ' s';
}

function fmtTime(ts) {
  if (!ts) return '–';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 相对时间（近 7 天用「x 分钟前 / x 小时前 / x 天前」，更早用绝对日期） */
function fmtRelTime(ts) {
  if (!ts) return '–';
  const sec = Math.floor(Date.now() / 1000 - Number(ts));
  if (sec < 0) return fmtTime(ts);
  if (sec < 60) return '刚刚';
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  if (sec < 86400 * 7) return `${Math.floor(sec / 86400)} 天前`;
  const d = new Date(Number(ts) * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/* ----------------------------------------------------------------------
   localStorage 持久化（宽度 / 活动栏 / 标签页）
   ---------------------------------------------------------------------- */
const LS_PREFIX = 'agentplat.';

function lsGet(key, fallback) {
  try {
    const v = window.localStorage.getItem(LS_PREFIX + key);
    return v === null ? fallback : v;
  } catch (e) { return fallback; }
}

function lsSet(key, val) {
  try { window.localStorage.setItem(LS_PREFIX + key, String(val)); } catch (e) { /* 忽略 */ }
}

function lsGetNum(key, fallback) {
  const v = parseFloat(lsGet(key, ''));
  return Number.isFinite(v) ? v : fallback;
}

/* ----------------------------------------------------------------------
   主题（浅色 / 深色）
   ----------------------------------------------------------------------
   · 默认 **深色**：CSS 的 :root 就是深色令牌，不写 data-theme 也对。
   · 浅色：给 <html> 加 data-theme="light"，CSS 侧由
     `[data-theme="light"]` 覆盖同名令牌实现（app.css §1b）。
   · 持久化：localStorage['agentplat.theme'] ∈ { 'light', 'dark' }。
   · 时机：在 DOMContentLoaded 时**立即**套用，避免刷新后先闪一下深色再跳变。
     （默认就是深色，所以只有"记住的是浅色"时才会看到一帧深色；本实现把
      读取+套用放在 boot() 的第一步，早于任何首次渲染的业务逻辑。）
   · 桌面端（pywebview / WebView2）是同一份 DOM，纯 DOM 操作即可生效。
   ---------------------------------------------------------------------- */
const THEME_KEY = 'theme';
const THEME_LIGHT = 'light';
const THEME_DARK = 'dark';

/** 当前生效的主题（'light' | 'dark'） */
function currentTheme() {
  return document.documentElement.getAttribute('data-theme') === THEME_LIGHT
    ? THEME_LIGHT
    : THEME_DARK;
}

/** 把主题写到 <html data-theme="…">；'dark' 时移除属性（回到 :root 默认值） */
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === THEME_LIGHT) {
    root.setAttribute('data-theme', THEME_LIGHT);
  } else {
    root.removeAttribute('data-theme');
  }
  root.setAttribute('data-theme-active', theme);   /* 便于调试 / 自动化断言 */
  const btn = document.getElementById('btn-theme');
  if (btn) {
    btn.setAttribute('aria-pressed', theme === THEME_LIGHT ? 'true' : 'false');
    btn.setAttribute('title', theme === THEME_LIGHT ? '切换到深色主题' : '切换到浅色主题');
  }
}

/** 初始化主题：读 localStorage → 套用 → 绑定切换按钮 */
function initTheme() {
  const saved = lsGet(THEME_KEY, THEME_DARK);
  applyTheme(saved === THEME_LIGHT ? THEME_LIGHT : THEME_DARK);

  const btn = document.getElementById('btn-theme');
  if (btn) {
    btn.addEventListener('click', () => {
      const next = currentTheme() === THEME_LIGHT ? THEME_DARK : THEME_LIGHT;
      applyTheme(next);
      lsSet(THEME_KEY, next);
    });
  }
}

/* ----------------------------------------------------------------------
   侧栏宽度拖拽（单一可拖拽侧栏）
   ---------------------------------------------------------------------- */
const RESIZER = {
  left: {
    handle: '#rs-left',
    side: '#sidebar',
    cssVar: '--sidebar-w',
    lsKey: 'leftWidth',
    min: 200,
    max: 620,
    default: 268,
    /** 左栏在左，拖拽时宽度 = 指针 x 相对容器左边 */
    calc: (px) => px,
  },
};

function applyWidth(which, px) {
  const cfg = RESIZER[which];
  const w = Math.max(cfg.min, Math.min(cfg.max, Math.round(px)));
  document.documentElement.style.setProperty(cfg.cssVar, w + 'px');
  return w;
}

function currentWidth(which) {
  const cfg = RESIZER[which];
  const el = $(cfg.side);
  if (!el) return 0;
  return el.getBoundingClientRect().width;
}

function initResizers() {
  // 恢复持久化的宽度
  applyWidth('left', lsGetNum('leftWidth', RESIZER.left.default));

  Object.keys(RESIZER).forEach((which) => {
    const cfg = RESIZER[which];
    const handle = $(cfg.handle);
    if (!handle) return;

    let dragging = false;

    const onMove = (e) => {
      if (!dragging) return;
      const app = $('#app');
      const rect = app.getBoundingClientRect();
      const px = (e.touches && e.touches[0] ? e.touches[0].clientX : e.clientX);
      const w = applyWidth(which, cfg.calc(px, rect));
      lsSet(cfg.lsKey, w);
    };

    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove('dragging');
      document.body.classList.remove('resizing');
      lsSet(cfg.lsKey, applyWidth(which, currentWidth(which)));
    };

    const start = (e) => {
      e.preventDefault();
      dragging = true;
      handle.classList.add('dragging');
      document.body.classList.add('resizing');
    };

    handle.addEventListener('mousedown', start);
    handle.addEventListener('touchstart', start, { passive: false });
    window.addEventListener('mousemove', onMove);
    window.addEventListener('touchmove', onMove, { passive: false });
    window.addEventListener('mouseup', onUp);
    window.addEventListener('touchend', onUp);
    // 双击复位
    handle.addEventListener('dblclick', () => {
      applyWidth(which, cfg.default);
      lsSet(cfg.lsKey, cfg.default);
    });
    // 键盘调整（← →）
    handle.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      const step = e.key === 'ArrowLeft' ? -16 : 16;
      const w = applyWidth(which, currentWidth(which) + step);
      lsSet(cfg.lsKey, w);
    });
  });
}

/* ----------------------------------------------------------------------
   活动栏 / 侧栏视图切换（一次只显示一个）
   ---------------------------------------------------------------------- */
const VIEWS = ['explorer', 'knowledge', 'tools', 'tasks', 'sessions', 'workspace', 'settings'];

function setActiveView(view, persist) {
  if (VIEWS.indexOf(view) < 0) view = 'explorer';
  state.activeView = view;
  $$('.act-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  VIEWS.forEach((v) => {
    const el = $('#view-' + v);
    if (el) el.hidden = (v !== view);
  });
  // 侧栏可见性（窄屏用）
  const app = $('#app');
  if (app) app.classList.remove('panel-main', 'panel-side');
  if (persist !== false) lsSet('activeView', view);
  // 视图首次进入时刷新对应数据
  if (view === 'knowledge' && typeof refreshKbDocuments === 'function') refreshKbDocuments();
  if (view === 'tools') refreshTools();
  if (view === 'tasks') refreshTasks();
  if (view === 'sessions') refreshSessions();
  if (view === 'workspace' && typeof wsRefreshTree === 'function') wsRefreshTree();
  if (view === 'settings') { refreshHealth(); refreshMetrics(); refreshModelLifecycle(true); }
}

function initActivityBar() {
  $$('.act-item').forEach((b) => {
    b.addEventListener('click', () => setActiveView(b.dataset.view));
  });
  const saved = lsGet('activeView', 'explorer');
  setActiveView(saved, false);
}

/* ----------------------------------------------------------------------
   标签页系统（VS Code 风格）
   ---------------------------------------------------------------------- */
const TAB_ICONS = {
  chat: '<svg class="tab-icon" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13 3 4.5 13.2H11L9.5 21 18 10.8h-6.5z"/></svg>',
  task: '<svg class="tab-icon" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="3" width="11" height="10" rx="1.5"/><path d="M5 6.5h6M5 9.5h4"/></svg>',
  doc: '<svg class="tab-icon" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 1.8H4.6A1.6 1.6 0 0 0 3 3.4v9.2a1.6 1.6 0 0 0 1.6 1.6h6.8a1.6 1.6 0 0 0 1.6-1.6V5.6z"/><path d="M9 1.8v3.8h4"/></svg>',
  session: '<svg class="tab-icon" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.8 8a5.2 5.2 0 1 1 1.6 3.7"/><path d="M2.6 14.6v-4.4h4.4"/><path d="M8 5.2V8l2.2 1.3"/></svg>',
  file: '<svg class="tab-icon" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 2.6h5.7L12 5.4v8a1.2 1.2 0 0 1-1.2 1.2H3.5a1.2 1.2 0 0 1-1.2-1.2V3.8a1.2 1.2 0 0 1 1.2-1.2z"/><path d="M9.1 2.7v2.8h2.8"/></svg>',
  obs: '<svg class="tab-icon" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.6 8h2.8l1.6-3.6L9.4 12l1.6-4h2.4"/></svg>',
};

function persistTabs() {
  try {
    /* 对话页是**常驻页**，必须**永远**在持久化列表里。
     *
     * 真实故障（2026-11-24 修复）：此前若对话页被关掉，
     * localStorage['agentplat.tabs'] 里就**不含** pg-chat；
     * 之后即便 restoreTabs 重新播种了它，状态也会被这份"脏"数据污染
     * → 新建会话彻底没反应。
     * 这里做兜底：pg-chat 永远在列表第一位。
     */
    const list = state.tabs.map((t) => ({
      id: t.id, type: t.type, title: t.title, data: t.data || null,
    }));
    if (!list.some((t) => t.id === 'pg-chat')) {
      list.unshift({ id: 'pg-chat', type: 'chat', title: '对话', data: null });
    } else {
      const i = list.findIndex((t) => t.id === 'pg-chat');
      const chatTab = list.splice(i, 1)[0];
      list.unshift(chatTab);
    }
    lsSet('tabs', JSON.stringify(list));
    lsSet('activeTab', state.activeTab || '');
  } catch (e) { /* 忽略 */ }
}

function renderTabs() {
  const box = $('#tabs');
  if (!box) return;
  box.innerHTML = '';
  state.tabs.forEach((t) => {
    const el = document.createElement('div');
    el.className = 'tab' + (t.id === state.activeTab ? ' active' : '');
    el.setAttribute('role', 'tab');
    el.dataset.tabId = t.id;
    el.innerHTML = (TAB_ICONS[t.type] || TAB_ICONS.doc)
      + '<span class="tab-label">' + esc(t.title) + '</span>'
      + '<button class="tab-close" type="button" title="关闭" aria-label="关闭标签页">'
      + '<svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>'
      + '</button>';
    el.addEventListener('click', (e) => {
      if (e.target.closest('.tab-close')) return;
      activateTab(t.id);
    });
    $('.tab-close', el).addEventListener('click', (e) => {
      e.stopPropagation();
      closeTab(t.id);
    });
    box.appendChild(el);
  });
  // 标签页栏在没有标签时也保留（至少一个对话页常驻）
  persistTabs();
}

function activateTab(id) {
  /* ---- 对话页兜底重建（2026-11-24 新增）----
   *
   * 真实故障：「新建会话」是**完全的空操作** —— 用户点了没反应。
   * 根因链条：
   *   ① closeTab('pg-chat') 把对话页元素 ``page.remove()`` 删掉了；
   *   ② activateTab('pg-chat') 因为 state.tabs 里没有 pg-chat 而**直接 return**
   *      → 不切页、不渲染；
   *   ③ ``#chat`` 已经不存在 → addSystemMsg 拿到 host=null → 直接 return
   *      → **一个像素都没变**，用户完全看不到任何反应。
   *
   * 对话页是**常驻页**（restoreTabs 每次都会重新播种它），
   * 所以这里做「兜底重建」：只要有人把页元素删了，就把它按模板重建回来。
   */
  if (id === 'pg-chat') ensureChatPage();
  const tab = state.tabs.find((t) => t.id === id);
  if (!tab) return;
  state.activeTab = id;
  // 兜底：对话页的 data-page 历史上写成 "chat"，与 tab id "pg-chat" 不一致，
  // 会导致切换 tab 时对话页被隐藏。这里同时兼容两种写法。
  $$('.page').forEach((p) => {
    const key = p.dataset.page || '';
    const isChat = p.classList.contains('page-chat');
    p.classList.toggle('active', key === id || (isChat && id === 'pg-chat'));
  });
  renderTabs();
  // 任务详情 tab 打开时按需刷新
  if (tab.type === 'task' && typeof refreshTaskTab === 'function') refreshTaskTab(tab.data);
  if (tab.type === 'doc' && typeof refreshDocTab === 'function') refreshDocTab(tab.data);
  // 运行观测面板：切回来时重绘（数据可能已随 SSE 更新）
  if (typeof obsOnTabActivated === 'function') obsOnTabActivated(id);
}

/** 对话页（pg-chat）的**模板 HTML**（boot 时从原始 DOM 抓取） */
let _chatPageTemplate = null;

/**
 * 确保对话页存在（不存在则按模板重建）。
 *
 * 为什么需要（2026-11-24 修复「新建会话完全没反应」）：
 *   closeTab('pg-chat') 曾经会把页元素 ``remove()`` 掉，
 *   而 activateTab('pg-chat') 又会因为 state.tabs 里没有它而直接 return
 *   → 「新建会话」变成**彻底的空操作**（连系统提示都发不出去，
 *      因为 ``#chat`` 已经被删了）。
 *   对话页是**常驻页**（restoreTabs 每次都重新播种），所以它必须能被重建。
 *
 * 重建时：
 *   · 用 boot 时抓下来的模板 HTML 还原结构；
 *   · 重新绑定**页内**的监听器（#btn-summarize 等）；
 *   · 把 pg-chat 重新插回 state.tabs 的**第 0 位**（保持常驻页在首位）；
 *   · 渲染空状态（welcome），避免又变成「一片空白」。
 */
function ensureChatPage() {
  const existing = $('.page[data-page="pg-chat"]');
  if (existing) return existing;
  if (!_chatPageTemplate) return null;
  const stage = $('#stage');
  if (!stage) return null;
  const holder = document.createElement('div');
  holder.innerHTML = _chatPageTemplate;
  const page = holder.firstElementChild;
  if (!page) return null;
  page.classList.remove('active');
  stage.insertBefore(page, stage.firstChild);
  /* 重新登记 tab（放到第 0 位：常驻页永远在第一位） */
  const tab = { id: 'pg-chat', type: 'chat', title: '对话', data: null };
  const idx = state.tabs.findIndex((t) => t.id === 'pg-chat');
  if (idx >= 0) state.tabs.splice(idx, 1);
  state.tabs.unshift(tab);
  /* 重新绑定页内监听器（页被重建后原来的监听器随元素一起没了） */
  rebindChatPageListeners(page);
  /* 恢复空状态 */
  renderWelcome($('#chat', page));
  const mt = $('#m-thread', page);
  if (mt && !state.threadId) mt.textContent = '（新建）';
  return page;
}

/**
 * 重新绑定**对话页内部**元素的监听器。
 * 只处理确实住在对话页里的元素（#btn-summarize）——
 * 输入区（#input / #btn-new / #chat-rounds）不在页内，不受影响。
 */
function rebindChatPageListeners(page) {
  if (!page) return;
  const sumBtn = $('#btn-summarize', page);
  if (sumBtn && typeof generateSummary === 'function') {
    sumBtn.addEventListener('click', () => generateSummary(state.threadId));
  }
}

function closeTab(id) {
  /* 对话页是**常驻页**，不可关闭。
   *
   * 真实故障（2026-11-24 修复）：此前 closeTab 会 ``page.remove()``
   * 把对话页元素从 DOM 里删掉，而 activateTab('pg-chat') 又会因为
   * state.tabs 里没有它而**直接 return** → 「新建会话」变成彻底的空操作，
   * 用户点了**完全看不到任何反应**（连系统提示都发不出去，
   * 因为 ``#chat`` 已经不存在）。
   * 对话页是常驻的（restoreTabs 每次都会重新播种它），
   * 所以这里直接拒绝关闭。
   */
  if (id === 'pg-chat') return;
  const idx = state.tabs.findIndex((t) => t.id === id);
  if (idx < 0) return;
  const page = $('.page[data-page="' + id + '"]');
  if (page) page.remove();
  state.tabs.splice(idx, 1);
  if (state.activeTab === id) {
    const next = state.tabs[Math.max(0, idx - 1)];
    state.activeTab = next ? next.id : null;
    if (next) activateTab(next.id);
    else renderTabs();
  } else {
    renderTabs();
  }
  persistTabs();
}

function openTab(type, title, data, forcedId) {
  const id = forcedId || ('pg-' + (++state.tabSeq));
  const page = document.createElement('section');
  page.className = 'page';
  page.dataset.page = id;
  if (type === 'task' || type === 'doc' || type === 'session') {
    page.classList.add('doc-page');
    page.innerHTML = '<div class="doc-page-inner"><h2>' + esc(title) + '</h2><div class="muted">加载中…</div></div>';
  } else if (type === 'file') {
    // 工作区文件页：编辑器骨架（textarea + pre 叠层）
    page.classList.add('ws-page');
    page.innerHTML = typeof wsEditorPage === 'function'
      ? wsEditorPage()
      : '<div class="muted">编辑器未就绪</div>';
    if (typeof wsBindEditorPage === 'function') wsBindEditorPage(page);
  } else if (type === 'obs') {
    // 运行观测面板（12 个观测组件的分段切换视图）
    page.classList.add('obs-tab-page');
    page.innerHTML = typeof obsPanelHtml === 'function'
      ? obsPanelHtml()
      : '<div class="muted">运行观测模块未就绪</div>';
    if (typeof obsBindPanel === 'function') obsBindPanel(page);
  } else {
    page.innerHTML = '<div class="muted">未知标签页类型</div>';
  }
  $('#stage').appendChild(page);
  const tab = { id: id, type: type, title: title, data: data || null };
  state.tabs.push(tab);
  activateTab(id);
  return tab;
}

/** 载入持久化的标签页（对话页常驻，其余按类型重建） */
function restoreTabs() {
  const chatTab = { id: 'pg-chat', type: 'chat', title: '对话', data: null };
  state.tabs = [chatTab];
  state.activeTab = 'pg-chat';
  let saved = [];
  try { saved = JSON.parse(lsGet('tabs', '[]')) || []; } catch (e) { saved = []; }
  saved.forEach((t) => {
    if (!t || t.type === 'chat') return;
    if (t.type === 'task' && t.data) openTab('task', t.title, t.data);
    else if (t.type === 'doc' && t.data) openTab('doc', t.title, t.data);
    else if (t.type === 'session' && t.data) {
      const tid = t.data.threadId;
      const sid = 'pg-sess-' + String(tid).replace(/[^a-zA-Z0-9]/g, '');
      openTab('session', t.title, t.data, sid);
    }
  });
  activateTab('pg-chat');
}

/* ----------------------------------------------------------------------
   窄屏单栏切换
   ---------------------------------------------------------------------- */
function initMobilePanels() {
  const bar = $('#mobile-bar');
  if (!bar) return;
  const mq = window.matchMedia('(max-width: 1180px)');
  const app = $('#app');

  /* **回归点（2026-11-23 修复）**：
   * 此前这里**无条件**执行 `app.classList.add('panel-main')`，
   * 而 CSS 里 `.ide.panel-main .sidebar .view { display: none !important; }`
   * **不在媒体查询内**，于是宽屏（>1180px）首屏加载时，
   * 侧栏所有视图都被 `!important` 隐藏（实测 #view-explorer 高度 = 0、
   * 侧栏整块空白），只有手动点一次活动栏图标（setActiveView 会移除
   * panel-main）后才恢复。
   * 现在改为：**仅在窄屏媒体查询命中时**才加 panel-* 类，
   * 宽屏保持无 panel-* 类 —— 与 setActiveView 的行为一致。 */
  const sync = () => {
    bar.hidden = !mq.matches;
    if (!app) return;
    if (mq.matches) {
      /* 窄屏：默认显示主区（对话） */
      app.classList.remove('panel-left', 'panel-right', 'panel-side');
      app.classList.add('panel-main');
      $$('.mbtn', bar).forEach((b) => b.classList.toggle('active', b.dataset.panel === 'main'));
    } else {
      /* 宽屏：清掉窄屏专用的单栏模式 */
      app.classList.remove('panel-left', 'panel-main', 'panel-right', 'panel-side');
    }
  };
  sync();
  if (mq.addEventListener) mq.addEventListener('change', sync);

  $$('.mbtn', bar).forEach((b) => {
    b.addEventListener('click', () => {
      $$('.mbtn', bar).forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      if (!app) return;
      app.classList.remove('panel-left', 'panel-main', 'panel-right', 'panel-side');
      app.classList.add('panel-' + b.dataset.panel);
    });
  });
}

/* ----------------------------------------------------------------------
   引用解析：把 [来源:xxx] / [doc_id:chunk_id] 抽成 chip

   与后端 agent_platform/core/citations.py 的 CITATION_RE 对齐，但更宽松：
     · 三种括号 [] ［］ 【】
     · 全角冒号 ：
     · Unicode 变体连字符
     · 两种格式：[来源:chunk_id] 与 [doc_id:chunk_id]
     · 容忍模型偶发输出 "[来源:chunk_id C-xxx]"（冒号后带空格与说明词）
   ---------------------------------------------------------------------- */
const CITE_BRACKET_RE = /[\[［【]\s*([^\]］】\n]{1,90}?)\s*[\]］】]/g;
const CITE_LABELS = ['来源', '出处', '参考', '引用', 'source', 'ref', 'citation'];

/** 归一化标识符：Unicode 变体连字符 → ASCII '-' */
function normCiteId(s) {
  return String(s || '').replace(/[‐‑‒–—−－]/g, '-');
}

/** 去掉模型偶发带的说明前缀，如 "chunk_id " */
function stripCitePrefix(s) {
  return String(s || '').replace(/^\s*(chunk_?id|chunkid)\s*[:=]?\s*/i, '').trim();
}

function parseCites(text) {
  const out = [];
  let m;
  CITE_BRACKET_RE.lastIndex = 0;
  while ((m = CITE_BRACKET_RE.exec(text)) !== null) {
    const inner = m[1];
    // 冒号切分：左=标签或 doc_id，右=chunk_id
    const sep = inner.search(/[:：]/);
    if (sep < 0) continue;
    let left = inner.slice(0, sep).trim();
    let right = inner.slice(sep + 1).trim();
    if (!right) continue;
    // 右侧可能是 "chunk_id C-xxx" 或 "C-xxx"，取最后一段像 id 的部分
    right = stripCitePrefix(right);
    if (!right) continue;
    const isLabel = !left || CITE_LABELS.indexOf(left.toLowerCase()) >= 0;
    out.push({
      raw: m[0],
      label: isLabel ? (left || '来源') : left,
      isLabel: isLabel,
      id: normCiteId(isLabel ? right : (left + ':' + right)),
      start: m.index,
      end: m.index + m[0].length,
    });
  }
  return out;
}

/* ----------------------------------------------------------------------
   文件引用 chip：把回答里的 `文件:行号` 变成可点击的跳转 chip
   ---------------------------------------------------------------------- */
const FILE_REF_RE = /([A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]{1,8})\s*[:：]\s*(\d{1,6})/g;

/** 从回答文本里抽出「文件:行号」引用 */
function parseFileRefs(text) {
  const out = [];
  const s = String(text || '');
  let m;
  FILE_REF_RE.lastIndex = 0;
  while ((m = FILE_REF_RE.exec(s)) !== null) {
    const path = m[1].replace(/\\/g, '/');
    if (!/\.[A-Za-z0-9]{1,8}$/.test(path)) continue;
    out.push({ path: path, line: Number(m[2]), raw: m[0], start: m.index, end: m.index + m[0].length });
  }
  return out;
}

/** 渲染答案 HTML：正文 + 行内引用 chip + 文件:行号 chip */
function renderAnswerHtml(answer) {
  const text = answer || '';
  const cites = parseCites(text);
  let html = '';
  let pos = 0;
  cites.forEach((c) => {
    html += esc(text.slice(pos, c.start));
    html += `<span class="cite" data-full="${esc(c.raw)}" title="${esc(c.label + ': ' + c.id)}">`
          + `${esc(c.label)}:${esc(c.id)}</span>`;
    pos = c.end;
  });
  html += esc(text.slice(pos));
  return { html: html, cites: cites };
}

/* ----------------------------------------------------------------------
   消息渲染（Cursor 风格：左对齐纯文本流，不用气泡）
   ---------------------------------------------------------------------- */
/* 注意：对话页可能被 Ctrl+W 关闭（closeTab 会 remove 整个 .page 节点），
   而 #composer 是 .page 的兄弟节点、依然可见。此时 #chat 已不存在，
   下面的 appendChild 会抛 TypeError 并把 state.busy 永久卡在 true，
   导致之后所有消息都被「上一条还在回答中」静默丢弃。
   因此统一在这里做**空宿主兜底**：查不到 #chat 就直接返回 null。 */
function addUserMsg(text) {
  const host = $('#chat');
  if (!host) return null;
  const wrap = document.createElement('div');
  wrap.className = 'msg user';
  wrap.innerHTML = '<span class="role-tag">你</span><div class="body"></div>';
  $('.body', wrap).textContent = text;
  host.appendChild(wrap);
  scrollDown();
  return wrap;
}

function addAssistantMsg() {
  const host = $('#chat');
  if (!host) return null;
  const wrap = document.createElement('div');
  wrap.className = 'msg assistant';
  wrap.innerHTML = '<span class="role-tag">Agent</span><div class="body" data-body></div>'
    + '<div class="toolrows"></div><div class="meta"></div><div class="cites"></div>';
  host.appendChild(wrap);
  scrollDown();
  return wrap;
}

function addSystemMsg(text) {
  const host = $('#chat');
  if (!host) return null;
  const wrap = document.createElement('div');
  wrap.className = 'msg system';
  wrap.innerHTML = '<div class="body"></div>';
  $('.body', wrap).textContent = text;
  host.appendChild(wrap);
  scrollDown();
  return wrap;
}

function scrollDown() {
  const c = $('#chat');
  if (c) c.scrollTop = c.scrollHeight;
}

/** 骨架屏（loading 态取代空白等待） */
function showSkeleton(box) {
  box.innerHTML = '<div class="skeleton sk-block"></div>'
    + '<div class="skeleton sk-line med" style="margin-top:8px"></div>'
    + '<div class="skeleton sk-line short"></div>';
}

/** 打字指示器 */
function showTyping(box, label) {
  // 容器可能不存在（例如对话页被关闭 → 消息节点为 null），此时静默跳过
  if (!box) return;
  box.innerHTML = '<span class="typing"><i></i><i></i><i></i></span> '
    + '<span class="muted">' + esc(label || '思考中…') + '</span>';
}

/** 写 assistant 正文 + 元信息 + 引用 chip */
function fillAssistant(wrap, answer, meta) {
  const body = wrap ? $('[data-body]', wrap) : null;
  if (!body) return;
  const r = renderAnswerHtml(answer || '');
  const refs = parseFileRefs(answer || '');
  body.innerHTML = r.html || '<span class="muted">(空回答)</span>';

  const citesBox = $('.cites', wrap);
  citesBox.innerHTML = '';
  r.cites.forEach((c) => {
    const el = document.createElement('span');
    el.className = 'cite';
    el.dataset.full = c.raw;
    el.textContent = `${c.label}:${c.id}`;
    citesBox.appendChild(el);
  });
  // 文件:行号 chip（点击跳到编辑器对应行）
  refs.forEach((ref) => {
    const el = document.createElement('span');
    el.className = 'cite cite-file';
    el.dataset.path = ref.path;
    el.dataset.line = String(ref.line);
    el.textContent = `${ref.path}:${ref.line}`;
    el.title = '点击跳转到 ' + ref.path + ' 第 ' + ref.line + ' 行';
    el.addEventListener('click', () => {
      if (typeof wsOpenFile !== 'function') return;
      wsOpenFile(ref.path, ref.path.split('/').pop()).then(() => {
        if (typeof wsGotoLine === 'function') wsGotoLine(ref.line);
      });
    });
    citesBox.appendChild(el);
  });

  const m = meta || {};
  const metaBox = $('.meta', wrap);
  const parts = [];
  parts.push(`<span>token <b>${m.tokens_used != null ? m.tokens_used : 0}</b></span>`);
  parts.push(`<span>工具调用 <b>${m.tool_calls_used != null ? m.tool_calls_used : 0}</b></span>`);
  parts.push(`<span>耗时 <b>${fmtMs(m.duration_ms)}</b></span>`);
  parts.push(`<span>stop_reason <b>${esc(m.stop_reason || '–')}</b></span>`);
  if (m.model) parts.push(`<span>模型 <b>${esc(m.model)}</b></span>`);
  if (m.mode) parts.push(`<span>模式 <b>${esc(m.mode)}</b></span>`);
  // ---- 检索必要性标记：本轮是否走了知识库检索 ----
  // 零 emoji：用文字 + 一个内联 SVG 闪电标记表示"直接回答，未检索"。
  if (m.retrieval_needed === false) {
    parts.push('<span class="retrieval-flag skipped" title="'
      + esc(m.retrieval_reason || '该问题不依赖知识库，直接作答')
      + '">'
      + '<svg class="retrieval-bolt" viewBox="0 0 16 16" width="11" height="11" '
      + 'fill="none" stroke="currentColor" stroke-width="1.5" '
      + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M9 1L3.5 9H7.5L6.5 15L12.5 7H8.5L9 1Z"/></svg>'
      + ' 直接回答，未检索</span>');
  } else if (m.retrieval_needed === true) {
    parts.push('<span class="retrieval-flag done" title="'
      + esc(m.retrieval_reason || '该问题依赖知识库，已检索查证')
      + '">检索: 已执行</span>');
  }
  metaBox.innerHTML = parts.join('');
  scrollDown();
}

/** 工具调用折叠行（紧凑摘要行，可展开） */
function addToolRow(wrap, name, detail) {
  const box = wrap ? $('.toolrows', wrap) : null;
  if (!box) return;
  const row = document.createElement('details');
  row.className = 'tool-row';
  row.innerHTML = '<summary>'
    + '<svg class="tool-caret" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4l4 4-4 4"/></svg>'
    + '<span class="tool-name">' + esc(name) + '</span>'
    + '<span class="tool-summary-text">' + esc(detail || '') + '</span>'
    + '</summary>'
    + '<div class="tool-payload">' + esc(detail || '') + '</div>';
  box.appendChild(row);
  scrollDown();
}

/**
 * 结构化工具调用折叠块（Cursor / Claude Code 风格）。
 * 由 SSE 的 trace 事件驱动：折叠头一行摘要（序号/工具/参数摘要/耗时/状态），
 * 展开体分区展示 参数 / 结果 / 错误（JSON 语法着色）。
 */
function addTraceRow(wrap, entry) {
  if (!wrap) return false;
  if (typeof tvAddTraceRow !== 'function') return false;
  tvAddTraceRow(wrap, entry);
  scrollDown();
  return true;
}

/* ----------------------------------------------------------------------
   HITL 审批卡片
   ---------------------------------------------------------------------- */
/** 审批嵌套深度上限：后端持续返回 pending_approval 时，
 *  审批卡片会一张接一张地叠加（无限增长）。超过上限就停止继续挂卡片。 */
const HITL_MAX_DEPTH = 5;

function addHitlCard(wrap, approvals, threadId) {
  if (!approvals || !approvals.length) return;
  // 消息节点可能不存在（对话页被关闭），此时无处挂卡片，直接跳过
  if (!wrap) return;
  /* 递归深度守卫：服务端一直返回 pending_approval 时，
     addHitlCard 会被反复调用、卡片无限叠加。
     深度记录在消息节点的 dataset 上（同一轮审批链共享一个计数）。 */
  const depth = Number(wrap.dataset.hitlDepth || 0);
  if (depth >= HITL_MAX_DEPTH) {
    const guard = document.createElement('div');
    guard.className = 'hitl';
    guard.innerHTML = '<span class="pill pill-err">审批层级过深，已停止继续请求审批</span>';
    wrap.appendChild(guard);
    scrollDown();
    return;
  }
  // 去重：同一次请求里同一个 tool_call_id 只渲染一张卡片
  const fresh = approvals.filter((ap) => {
    const cid = ap.tool_call_id || '';
    if (!cid) return true;
    if (wrap.dataset.hitlSeen && wrap.dataset.hitlSeen.indexOf(cid) >= 0) return false;
    wrap.dataset.hitlSeen = (wrap.dataset.hitlSeen || '') + cid + ',';
    return true;
  });
  if (!fresh.length) return;
  approvals = fresh;
  /* ---- 记录「当前有待审批项」（2026-11-24 新增）----
   * 用于：用户在对话框里敲「确认执行 / go ahead」时弹提示
   * （对话文字**不会**放行审批，必须点卡片上的「批准」）。*/
  state.pendingApprovalCount = (state.pendingApprovalCount || 0) + approvals.length;
  // 深度 +1：真正渲染出卡片后才计数（去重命中时不计数）
  wrap.dataset.hitlDepth = String(depth + 1);
  const nextDepth = depth + 1;        // 供本卡片的递归重渲染继承
  const box = document.createElement('div');
  box.className = 'hitl';
  /* ---- 把 tool_call_id 挂到 DOM 上（2026-11-24，审批统一入口）----
   * 为什么需要：
   *   运行观测面板的按钮被降级为「定位器」后，需要按 cid 找到**这一张**
   *   对话卡片才能跳过去；原先卡片只在 message 节点的 dataset 上存了一份
   *   逗号拼接的弱列表（``wrap.dataset.hitlSeen``），无法精确定位。
   * 现在 ``data-cid`` 同时挂在**卡片**和**.h-actions** 两处 ——
   * 因为审批失败重试时按钮会跑到另一个 .h-actions 里，
   * 只挂一处会漏掉。 */
  const cardCid = approvals.length === 1
    ? (approvals[0].tool_call_id || '')
    : (approvals.map((a) => a.tool_call_id).filter(Boolean).join(' ') || '');

  const warnIcon = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" '
    + 'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M8 2.2 14.4 13.4H1.6z"/><path d="M8 6.4v3.2M8 11.6h.01"/></svg>';

  const cards = approvals.map((ap) => `
    <div class="h-title">${warnIcon}需要人工确认：${esc(ap.tool_name || '')}</div>
    <div class="h-row"><span>权限</span><b>${esc(ap.permission || '')}</b></div>
    <div class="h-row"><span>动作</span>${esc(ap.description || '–')}</div>
    <div class="h-row"><span>影响</span>${esc(ap.impact || '–')}</div>
    <div class="h-row"><span>可回滚</span>${ap.reversible === false ? '否（不可逆）' : '是'}</div>
    ${((ap.note || (ap.args || {}).note) ? `<div class="h-row"><span>说明</span>${esc(ap.note || (ap.args || {}).note)}</div>` : '')}
    <div class="h-row"><span>参数</span></div>
    <pre>${esc(JSON.stringify(ap.args || {}, null, 2))}</pre>
    ${(ap.diff ? (typeof wsDiffHtml === 'function' ? wsDiffHtml(ap.diff) : '') : '')}
  `).join('');

  box.innerHTML = cards + `
    <div class="h-actions" data-cid="${esc(cardCid)}">
      <input class="actor" placeholder="审批人" value="operator" />
      <label class="grant"><input type="checkbox" class="grant-chk" /> 本会话内不再询问</label>
      <button class="btn btn-ok sm act-approve" type="button">批准</button>
      <button class="btn btn-no sm act-reject" type="button">拒绝</button>
    </div>`;
  if (cardCid) box.setAttribute('data-cid', cardCid);

  /* bind(btn, approved, host)：
     host = 按钮所在的操作区（默认卡片自身的 box）。
     重试时按钮在另一个 .h-actions 里，所以要把 host 一起传下去，
     否则「审批人 / 本会话内不再询问」的取值会读到已被替换掉的旧节点。

     ⚠️ **这里是全站唯一会 POST /approve 的地方**（2026-11-24 审批统一）。
     运行观测面板（timeline.js / observability.js）的按钮已降级为
     「定位器」——只负责把对话卡片跳到眼前，**不自己决定**。
     单一 POST 路径 ⇒ 审批状态不可能分叉。 */
  const bind = (btn, approved, host) => {
    const ctl = host || box;
    btn.addEventListener('click', async () => {
      const actorEl = $('.actor', ctl);
      const grantEl = $('.grant-chk', ctl);
      const actor = (actorEl ? actorEl.value : '').trim() || 'operator';
      const grant = grantEl ? grantEl.checked : false;
      const tcids = approvals.map((a) => a.tool_call_id).filter(Boolean);
      $$('.h-actions button', ctl).forEach((b) => { b.disabled = true; });
      try {
        const res = await api('/approve', {
          method: 'POST',
          body: JSON.stringify({
            thread_id: threadId,
            tool_call_id: tcids[0],
            approved: approved,
            actor: actor,
            reason: approved ? '工作台人工批准' : '工作台人工拒绝',
            session_grant: grant,
          }),
        });
        /* 成功：把**当前操作区**（ctl）替换成结果药丸；
           重试时 ctl 是重试按钮所在的新 .h-actions，不会误改原卡片。 */
        const actBox = $('.h-actions', ctl);
        if (actBox) {
          actBox.innerHTML =
            `<span class="pill ${res.status === 'error' ? 'pill-err' : 'pill-ok'}">`
            + `已${approved ? '批准' : '拒绝'} · ${esc(res.stop_reason || res.status)}</span>`;
          /* 清理同卡片里残留的旧错误提示 / 其它重试按钮组，
             避免审批成功后还留着一堆「审批失败」和重复按钮。 */
          $$('.h-actions', box).forEach((el) => {
            if (el === actBox) return;
            if (el.parentNode) el.parentNode.removeChild(el);
          });
        }
        /* ---- 本卡片对应的待审批项已处理完（2026-11-24）----
         * 扣减全局计数，避免用户在对话框里敲「确认执行」时
         * 仍然弹「请点击批准」—— 但如果服务端又返回了新的待审批项，
         * 下面的 addHitlCard 会把它加回来。 */
        state.pendingApprovalCount = Math.max(
          0, (state.pendingApprovalCount || 0) - approvals.length);
        /* 卡片不再处于「等待批准」状态 → 去掉高亮 */
        box.classList.remove('is-pending', 'is-flash');
        /* ---- 广播审批决策（2026-11-24，审批统一入口）----
         * 写事实来源 + 派发 ``hitl-resolved`` 事件，
         * 让运行观测面板（Live Timeline / 审批卡片视图）立刻把
         * 「待审批」翻成「已批准」—— 此前它们永远停在旧状态，
         * 用户看到「面板说还没批、对话里又批不掉」的矛盾界面。 */
        const summary = res.stop_reason || res.status || '';
        tcids.forEach((cid) => {
          state.resolvedApprovals[cid] = approved ? 'approved' : 'rejected';
        });
        if (typeof APPROVAL !== 'undefined') {
          tcids.forEach((cid) => APPROVAL.emitResolved(cid, approved, summary));
        }
        const follow = addAssistantMsg();
        fillAssistant(follow, res.answer || '', {
          tokens_used: res.tokens_used,
          tool_calls_used: res.tool_calls_used,
          stop_reason: res.stop_reason,
          duration_ms: null,
        });
        /* ---- 渲染服务端返回的**新轨迹**（2026-11-24 新增）----
         * 真实故障：``/approve`` 之前**不返回 trace** → 审批完成后
         * 「决策轨迹」面板拿不到新快照，用户看到的仍是审批前那一帧
         * （ws_write_file 标着琥珀色「待审批」，实际早已落盘）。
         * 现在服务端从最终 state 重建轨迹并回传，这里推给面板。*/
        if (res.trace && res.trace.length) {
          const tid = res.thread_id || threadId;
          if (typeof tvPushTrace === 'function') {
            tvPushTrace(tid, res.trace, res.budget || null);
          }
          /* 同步到本轮消息节点的折叠行（与 SSE 的 trace 事件同构） */
          if (follow) {
            follow.dataset.tvTraced = '1';
            res.trace.forEach((entry) => { addTraceRow(follow, entry); });
          }
        }
        /* ---- 用 /approve 的返回值**主动**刷新运行观测面板 ----
         * 为什么必须主动刷新（2026-11-24，审批统一入口）：
         *   SSE 只在 /chat 期间开着，而审批走的是 /approve（另一个 HTTP 请求）
         *   → 服务端不会给面板推任何事件 → 面板永远停在审批前那一帧，
         *   显示着琥珀色「待审批」。这里把服务端回传的新轨迹/预算灌进去。 */
        if (typeof tlApplyApproveResult === 'function') {
          tlApplyApproveResult(Object.assign({}, res, {
            tool_call_id: tcids[0] || '',
          }));
        }
        if (res.status === 'pending_approval' && res.approvals && res.approvals.length) {
          /* 服务端持续返回 pending_approval 时会反复走到这里 →
             把当前审批深度继承到新的消息节点上，让 addHitlCard 的
             深度守卫能识别出「审批层级过深」并停止继续挂卡片。 */
          if (follow) follow.dataset.hitlDepth = String(nextDepth);
          addHitlCard(follow, res.approvals, res.thread_id || threadId);
        }
        // Agent 写盘后 → 刷新编辑器里打开的对应文件（标脏）
        if (approved && typeof wsRefreshAfterAgentWrite === 'function') {
          approvals.forEach((a) => {
            const p = (a.args || {}).path || (a.diff || {}).path || '';
            if (p) wsRefreshAfterAgentWrite(p);
          });
        }
        refreshSessions();
      } catch (err) {
        /* 审批失败时**保留并恢复按钮**：此前直接把 .h-actions 换成错误提示，
           按钮被移除后无法重试，用户只能刷新页面。
           这里先恢复按钮的 disabled 状态，再在错误提示旁重新渲染一组按钮。 */
        $$('.h-actions button', ctl).forEach((b) => { b.disabled = false; });
        const act = $('.h-actions', ctl);
        if (act && act.parentNode) {
          act.innerHTML =
            `<span class="pill pill-err">审批失败：${esc(err.message)}</span>`;
          /* 重新渲染按钮（错误提示在上、按钮在下），让用户可以直接重试 */
          const retry = document.createElement('div');
          retry.className = 'h-actions';
          retry.innerHTML = `
            <input class="actor" placeholder="审批人" value="${esc(actor)}" />
            <label class="grant"><input type="checkbox" class="grant-chk"${grant ? ' checked' : ''} /> 本会话内不再询问</label>
            <button class="btn btn-ok sm act-approve" type="button">重试批准</button>
            <button class="btn btn-no sm act-reject" type="button">重试拒绝</button>`;
          act.parentNode.insertBefore(retry, act.nextSibling);
          bind($('.act-approve', retry), true, retry);
          bind($('.act-reject', retry), false, retry);
        }
      }
    });
  };
  bind($('.act-approve', box), true);
  bind($('.act-reject', box), false);

  wrap.appendChild(box);
  /* ---- 让审批卡片**显眼**（2026-11-24 新增）----
   * 真实故障：工具挂在 HITL 上等批准，但用户在长对话里根本注意不到卡片，
   * 于是以为「代码都写不进去」（其实只是没点批准）。
   * 现在：① 加 ``is-pending`` 类（琥珀色高亮 + 呼吸动画）；
   *       ② 滚动到可视区中央。
   */
  box.classList.add('is-pending');
  try {
    if (typeof box.scrollIntoView === 'function') {
      box.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  } catch (e) {
    /* 某些环境不支持 scrollIntoView —— 忽略即可 */
  }
  scrollDown();
}

/* ----------------------------------------------------------------------
   审批决策广播（2026-11-24，审批统一入口）
   ----------------------------------------------------------------------
   订阅 ``hitl-resolved``：任何界面完成了审批决策，这里把对话卡片
   同步成「已处理」的样子（去掉琥珀色呼吸光 → 换成结果药丸）。

   ⚠️ 本对话卡片本身**就是**决策发起方，正常情况下事件是自己发的；
   这里处理的是「决策在别处发生」（例如历史会话恢复后由服务端状态
   判定为已批、或未来新增的入口）—— 保证对话卡片不会停在
   「永远批不掉」的 is-pending 状态。 */
if (typeof APPROVAL !== 'undefined' && typeof APPROVAL.onResolved === 'function') {
  APPROVAL.onResolved((detail) => {
    const cid = detail && detail.tool_call_id;
    if (!cid) return;
    const approved = !!(detail && detail.approved);
    const summary = (detail && detail.summary) || '';
    state.resolvedApprovals[cid] = approved ? 'approved' : 'rejected';

    const card = APPROVAL.findChatCard(cid);
    if (!card) return;
    /* 去掉「等待批准」的高亮（琥珀色呼吸光）*/
    card.classList.remove('is-pending', 'is-flash');
    /* 把操作区换成结果药丸（若还没有）*/
    const actBox = $('.h-actions', card);
    if (!actBox) return;
    if (actBox.querySelector('.pill')) return;   /* 已有结果药丸 → 不重复写 */
    const hasBtn = actBox.querySelector('.act-approve, .act-reject');
    if (!hasBtn) return;                          /* 没有按钮 → 不是决策区 */
    actBox.innerHTML =
      `<span class="pill ${approved ? 'pill-ok' : 'pill-err'}">`
      + `已${approved ? '批准' : '拒绝'} · ${esc(summary || 'done')}</span>`;
    /* 计数只在「还有按钮」的情况下减 —— 避免重复广播导致计数变负 */
    state.pendingApprovalCount = Math.max(0, (state.pendingApprovalCount || 0) - 1);
  });
}

/* ----------------------------------------------------------------------
   SSE 接收：fetch + ReadableStream（POST body，故不用 EventSource）
   ---------------------------------------------------------------------- */
/* ----------------------------------------------------------------------
   上下文占用指示器（设计文档 E.8 / F.3）
   ----------------------------------------------------------------------
   行业惯例是「**百分比 + hover 展开明细**」，而不是常驻精确数字：
     · 常驻：一条 40px 细进度条 + 百分比（≥80% 转黄 / ≥95% 转红）
     · hover：used / limit、token 分类明细
   数据来自后端 SSE 的 budget 快照（tokens_used / context_window），
   观测模块 obsOnSse 已经把这份快照收在 OBS.budget 里。
   ---------------------------------------------------------------------- */

/** 从后端 budget 快照里取「已用 token / 上下文窗口」 */
function ctxBudgetSnapshot() {
  const b = (typeof OBS !== 'undefined' && OBS && OBS.budget) || null;
  if (!b) return null;
  const used = Number(b.tokens_used);
  const win = Number(b.context_window) || Number(OBS.contextWindow) || 0;
  if (!isFinite(used) || !win) return null;
  return { used, win };
}

/** 刷新上下文占用指示器（每轮结束调一次即可，不实时跳动） */
function updateCtxMeter() {
  const meter = $('#ctx-meter');
  const fill = $('#ctx-bar-fill');
  const pct = $('#ctx-pct');
  const detail = $('#ctx-detail');
  if (!meter || !fill || !pct || !detail) return;

  const snap = ctxBudgetSnapshot();
  if (!snap) { meter.hidden = true; return; }

  const ratio = Math.max(0, Math.min(1, snap.used / snap.win));
  const pctVal = Math.round(ratio * 100);

  meter.hidden = false;
  fill.style.width = pctVal + '%';
  pct.textContent = pctVal + '%';

  /* 阈值变色：<80% 绿 / ≥80% 黄 / ≥95% 红 */
  meter.dataset.lvl = ratio >= 0.95 ? 'full' : (ratio >= 0.80 ? 'near' : 'ok');

  /* hover 明细：精确数字放这里，常驻只留百分比 */
  const b = (typeof OBS !== 'undefined' && OBS && OBS.budget) || {};
  const rows = [
    ['已用', snap.used.toLocaleString('zh-CN')],
    ['上限', snap.win.toLocaleString('zh-CN')],
  ];
  if (b.tool_calls_used != null) rows.push(['工具调用', String(b.tool_calls_used)]);
  if (b.rounds != null && b.max_rounds != null) rows.push(['轮次', b.rounds + ' / ' + b.max_rounds]);
  detail.innerHTML = '<div class="ctx-d-title">上下文占用</div>'
    + rows.map((r) => '<div class="ctx-d-row"><span>' + esc(r[0])
      + '</span><b>' + esc(r[1]) + '</b></div>').join('');
}

async function streamChat(message) {
  const t0 = Date.now();
  /* wrap / bodyEl 声明在 try 之前、赋值在 try 内部：
     创建消息容器时若 #chat 已被移除（对话页被 Ctrl+W 关闭）会得到 null，
     下面所有渲染点都要能容忍 null，否则会跳过 finally 把 busy 卡死。 */
  let wrap = null;
  let bodyEl = null;
  let answer = '';
  let meta = {};
  let planData = null;
  let threadId = state.threadId;
  let sawStep = false;
  let sawResult = false;          // 本轮是否收到过 result 事件（用于兜底合成）

  /* 运行状态行（消息流末端的双通道指示器）：
   * 文字取自**事件本身**（具体动作），不是泛化的「处理中…」；
   * 动态指示器由 .run-dots 提供。空闲 / 结束时隐藏。
   *
   * **700ms 最短展示**（设计文档 A.2 / C.3 的 MIN_HINT_DISPLAY_MS）：
   * 快速完成的工具如果立刻收起状态行会「单帧闪过」，观感上像卡顿。
   * 这里只延迟**隐藏**，不延迟文案更新 —— 文案越新越好，
   * 但指示器至少要在场 700ms，避免闪烁。 */
  const MIN_RUN_HINT_MS = 700;
  let runShownAt = 0;
  let runHideTimer = null;
  const setStreamState = (t) => {
    const el = $('#stream-state');
    if (el) el.textContent = t;
    const rs = $('#run-status');
    const rt = $('#run-text');
    if (!rs || !rt) return;
    if (runHideTimer) { clearTimeout(runHideTimer); runHideTimer = null; }
    if (t) {
      if (rs.hidden) runShownAt = Date.now();
      rt.textContent = t;
      rs.hidden = false;
    } else {
      const elapsed = Date.now() - runShownAt;
      const wait = Math.max(0, MIN_RUN_HINT_MS - elapsed);
      runHideTimer = setTimeout(() => { rs.hidden = true; }, wait);
    }
  };

  try {
    /* 创建消息容器必须放在 try **内部**：
       此前这三行在 try 之前，#chat 缺失时 addAssistantMsg() 抛错会跳过 finally，
       导致 state.busy 永久为 true（对话页被 Ctrl+W 关闭后的死锁根因）。 */
    wrap = addAssistantMsg();
    bodyEl = wrap ? $('[data-body]', wrap) : null;
    if (bodyEl) showTyping(bodyEl, '思考中…');

    const res = await fetch(API + '/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: message,
        thread_id: state.threadId,
        session_id: state.sessionId,
        stream: true,
        model: state.model || undefined,
        mode: state.mode || 'agent',
        /* **ReAct 轮次上限**（2026-11-24 新增）：
         * 此前 /chat 完全不接受这个参数 → 服务端永远用硬编码的 8 轮，
         * 用户抱怨「才几步就停了」。现在随请求下发（缺省 20）。 */
        max_rounds: chatMaxRounds(),
      }),
    });

    if (!res.ok) {
      const t = await res.text();
      throw new Error('HTTP ' + res.status + ' ' + t.slice(0, 200));
    }
    const ct = res.headers.get('content-type') || '';
    if (ct.indexOf('text/event-stream') < 0) {
      throw new Error('响应不是 text/event-stream（' + ct + '）');
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE 以空行分隔事件
      let idx;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);

        const line = raw.split('\n').find((l) => l.startsWith('data: '));
        if (!line) continue;
        const data = line.slice(6);
        if (data.trim() === '[DONE]') { setStreamState('完成'); continue; }

        let obj;
        try {
          obj = JSON.parse(data);
        } catch (e) {
          /* 解析失败不能静默吞掉：result 事件体积最大、最容易被截断，
             一旦被吞掉观测模块就永远收不到 result → Live Timeline 的转圈
             指示器不会结束、obsStopTimer() 也不会被调用。这里记录告警。 */
          console.warn('[sse] 解析失败，已跳过', data);
          continue;
        }

        /* ---- 运行观测面板：把 SSE 事件喂给观测模块 ----
         * 观测模块自己维护一份快照（trace / budget / approvals），
         * 与决策轨迹面板互不影响；数据来源优先级：
         *   trace（权威累积快照） > step（流式） > result（最终）。
         */
        if (typeof obsHandleSse === 'function') {
          try { obsHandleSse(obj); } catch (e) { /* 观测模块异常不得影响主流程 */ }
        }

        if (obj.event === 'start') {
          threadId = obj.thread_id;
          state.threadId = obj.thread_id;
          const mt = $('#m-thread');
          if (mt) mt.textContent = obj.thread_id;
          setStreamState('推理中…');
        } else if (obj.event === 'step') {
          sawStep = true;
          const bits = [];
          if (obj.tools && obj.tools.length) bits.push('调用 ' + obj.tools.join(', '));
          if (obj.stop_reason) bits.push(obj.stop_reason);
          // 状态文案取自**事件本身**（具体工具名 / 停止原因），
          // 而不是泛化的「处理中…」——这是 OpenHands live-activity chip 的核心。
          const stepLabel = bits.length ? bits.join(' · ') : ('节点 ' + (obj.node || ''));
          setStreamState(stepLabel);
          showTyping(bodyEl, stepLabel);
          if (obj.tools && obj.tools.length) {
            // 若本 step 同时带了结构化 tool_calls，则由 addTraceRow 渲染
            // （带参数 / 结果 / 耗时），不再加一条纯文本行，避免同一调用重复显示。
            // 2026-11-22 修复：此前两者同渲染，调用会显示 3 行。
            if (!obj.tool_calls || !obj.tool_calls.length) {
              addToolRow(wrap, obj.tools.join(', '), '节点 ' + (obj.node || '') + (obj.stop_reason ? ' · ' + obj.stop_reason : ''));
            }
          }
          // 结构化轨迹（后端补齐的 tool_calls 字段）：渲染成带参数/结果的折叠块
          if (obj.tool_calls && obj.tool_calls.length) {
            (obj.tool_calls || []).forEach((tc) => {
              addTraceRow(wrap, Object.assign({ seq: obj.seq }, tc));
            });
          }
        } else if (obj.event === 'trace') {
          // 完整决策轨迹快照（累积式）：更新全局缓存，并把主区折叠行同步到快照状态
          if (typeof tvPushTrace === 'function') tvPushTrace(threadId, obj.trace || [], obj.budget || null);
          if (obj.budget && typeof tvPushTrace === 'function') {
            TV_TRACE.budget = obj.budget;
          }
          // **快照是权威来源**（2026-11-22 修复）：
          // `step` 事件只带参数、不带结果，且先于 `trace` 到达；
          // 若只在首次渲染，后续快照里的「结果 / 耗时 / 错误」就永远补不上，
          // 用户展开折叠行只看到参数、看不到结果。
          // 这里改为：每次收到快照都用 upsert 语义同步折叠行
          // （同一 tool_call_id 就地更新，不新增行）。
          if (obj.trace && obj.trace.length) {
            if (wrap) wrap.dataset.tvTraced = '1';
            (obj.trace || []).forEach((entry) => {
              addTraceRow(wrap, entry);
            });
          }
        } else if (obj.event === 'approval') {
          setStreamState('等待人工确认');
          addHitlCard(wrap, obj.approvals || [], threadId);
          if (bodyEl) bodyEl.innerHTML = '<span class="muted">已挂起，等待人工确认…</span>';
        } else if (obj.event === 'plan') {
          setStreamState('已生成计划');
          if (bodyEl) bodyEl.innerHTML = '<span class="muted">已生成执行计划（未执行任何工具）</span>';
          planData = obj.plan || null;
        } else if (obj.event === 'result') {
          sawResult = true;
          meta = obj;
          answer = obj.answer || '';
          // 上下文占用：后端 result 事件带 budget 快照时立刻刷新指示器
          if (obj.budget && typeof OBS !== 'undefined' && OBS) {
            OBS.budget = obj.budget;
            if (obj.budget.context_window) OBS.contextWindow = obj.budget.context_window;
          }
          updateCtxMeter();
        }
      }
    }

    /* ---- result 事件兜底（本轮流结束但从未收到 result）----
     * result 是体积最大的事件，传输中被截断时 JSON.parse 会失败并被跳过，
     * 于是观测模块收不到 result → obsStopTimer() 不被调用、
     * OBS.finished 一直为 false → Live Timeline 的转圈指示器永远不结束。
     * 这里合成一个 result 事件喂给观测模块，确保计时器一定被停掉。 */
    if (!sawResult) {
      try {
        if (typeof obsHandleSse === 'function') {
          obsHandleSse({ event: 'result', answer: answer, thread_id: threadId });
        }
      } catch (e) {
        console.warn('[sse] 兜底合成 result 事件失败', e);
      }
    }

    if (!sawStep && !answer) {
      if (bodyEl) bodyEl.innerHTML = '<span class="muted">(无内容)</span>';
    }
    fillAssistant(wrap, answer, {
      tokens_used: meta.tokens_used,
      tool_calls_used: meta.tool_calls_used,
      stop_reason: meta.stop_reason,
      duration_ms: Date.now() - t0,
      model: meta.model || state.model || '',
      mode: meta.mode || state.mode || '',
      // 检索必要性判定（result 事件里带；缺失时为 undefined → 不显示标记）
      retrieval_needed: meta.retrieval_needed,
      retrieval_reason: meta.retrieval_reason || '',
    });
    if (meta.status === 'pending_approval' && meta.approvals && meta.approvals.length) {
      addHitlCard(wrap, meta.approvals, meta.thread_id || threadId);
    }
    // Plan 模式：渲染结构化计划卡片（执行 / 放弃）
    const plan = planData || meta.plan || null;
    if (plan && typeof addPlanCard === 'function') {
      if (bodyEl) bodyEl.innerHTML = '<span class="muted">已生成执行计划，尚未执行任何工具。</span>';
      addPlanCard(wrap, plan, meta.thread_id || threadId);
    }
    if (meta.status === 'error') {
      addSystemMsg('执行错误：' + (answer || '未知错误'));
    }
    state.threadId = meta.thread_id || threadId;
  } catch (err) {
    /* catch 体内也可能因为 #chat 缺失而再次抛错，
       所以这里再套一层 try/catch，保证原始错误不被覆盖，
       同时不会因为异常再次跳过 finally（busy 复位）。 */
    try {
      if (bodyEl) bodyEl.innerHTML = '<span class="muted">(请求失败)</span>';
      addSystemMsg('请求失败：' + err.message);
    } catch (e2) {
      console.warn('[chat] 渲染错误提示失败', e2);
    }
    console.warn('[chat] 请求失败', err);
  } finally {
    setStreamState('');
    state.busy = false;
    const sendBtn = $('#btn-send');
    if (sendBtn) sendBtn.disabled = false;
    refreshSessions();
  }
}

/* ----------------------------------------------------------------------
   状态栏 / 侧栏：健康 / 工具 / 端点 / 会话
   ---------------------------------------------------------------------- */
async function refreshHealth() {
  try {
    const h = await api('/health');
    const llm = h.llm || {};
    const llmEl = $('#h-llm');
    if (llmEl) {
      llmEl.textContent = llm.reachable ? '可达' : '不可达';
      llmEl.className = 'pill ' + (llm.reachable ? 'pill-ok' : 'pill-err');
      llmEl.title = llm.base_url || '';
    }
    const llmEl2 = $('#s-llm');
    if (llmEl2) {
      llmEl2.textContent = llm.reachable ? '可达' : '不可达';
      llmEl2.className = 'pill ' + (llm.reachable ? 'pill-ok' : 'pill-err');
      llmEl2.title = llm.base_url || '';
    }

    const toolCount = (h.registry && h.registry.tool_count) || 0;
    const hitl = ((h.registry && h.registry.hitl_tools) || []).join(', ') || '无';
    const cp = (h.graph && h.graph.checkpointer) || 'memory';
    const pv = h.prompt_version || '–';
    const st = `${h.sessions || 0} / ${h.tasks || 0}`;

    const set = (sel, val) => { const el = $(sel); if (el) el.textContent = val; };
    set('#h-toolcount', toolCount + ' 个');
    set('#h-hitl', hitl);
    set('#h-cp', cp);
    set('#h-pv', pv);
    set('#h-st', st);
    set('#s-toolcount', toolCount + ' 个');
    set('#s-hitl', hitl);
    set('#s-cp', cp);
    set('#s-pv', pv);
    set('#s-st', st);

    // 标题栏状态点
    const dot = $('#tb-dot');
    const txt = $('#tb-status-text');
    if (dot) dot.className = 'status-dot ' + (llm.reachable ? 'on' : 'off');
    if (txt) txt.textContent = llm.reachable ? '服务正常' : '服务异常';

    // 模型端点（两个面板都有）
    const eps = h.endpoints || [];
    ['#h-endpoints', '#h-endpoints-2'].forEach((sel) => {
      const epBox = $(sel);
      if (!epBox) return;
      epBox.innerHTML = '';
      eps.forEach((e) => {
        const d = document.createElement('div');
        d.className = 'ep';
        d.innerHTML = `<span class="dot ${e.reachable ? 'on' : 'off'}"></span>`
          + `<span class="name">${esc(e.model)}</span>`
          + `<span class="url">${esc(e.base_url || '')}</span>`
          + (e.reachable ? '' : `<span class="pill pill-err">${esc(e.error || '不可达')}</span>`);
        epBox.appendChild(d);
      });
    });
    const okCount = eps.filter((e) => e.reachable).length;
    const epSum = $('#ep-sum');
    if (epSum) epSum.textContent = `${okCount}/${eps.length} 可达`;
    const epSum2 = $('#ep-sum-2');
    if (epSum2) epSum2.textContent = `${okCount}/${eps.length} 可达`;
  } catch (err) {
    const llmEl = $('#h-llm');
    if (llmEl) { llmEl.textContent = '检测失败'; llmEl.className = 'pill pill-err'; }
    const dot = $('#tb-dot');
    const txt = $('#tb-status-text');
    if (dot) dot.className = 'status-dot off';
    if (txt) txt.textContent = '连接失败';
  }
}

/* ----------------------------------------------------------------------
   模型服务：本地模型端点的启停（/models/lifecycle）
   ----------------------------------------------------------------------
   为什么需要这一块：
     · /health 的 endpoints[] 只回答「**连不连得上**」（被动探测），
       「模型服务」回答「**现在跑没跑 / 要不要我拉起来**」（进程语义）。
     · 启动是**慢操作**（模型加载 3~60s），POST /models/{alias}/start
       立刻返回 status:"starting"，真正的就绪信号是 GET /models/lifecycle
       里 running/reachable 翻转 —— 所以 UI 必须**轮询**。
   轮询策略（用户在意资源占用，不能无脑轮询）：
     · 只有在**存在过渡态**（有模型 starting / stopping）时才每 2s 轮询；
     · 一旦全部落定（要么 running+reachable，要么 running 但不可达，
       要么没跑）就**停掉定时器**，不再发请求；
     · 面板每次被打开、用户每次拨动开关、每次启停动作后都会主动刷新一次。
   降级：
     · 后端没重启（路由表里还没有 /models/lifecycle）时，FastAPI 会用
       /models/{name} 这类通配路由兜住路径并返回 405/404 ——
       这时不能把整个侧栏搞崩，只在面板里显示一句友好提示。
   ---------------------------------------------------------------------- */
const MSVC = {
  models: [],            // 最近一次 /models/lifecycle 的 models[]
  pending: {},           // alias -> 'starting' | 'stopping'（本地乐观状态）
  timer: null,           // 轮询句柄（只在过渡态存在）
  inflight: false,       // 防重入：上一轮请求没回来就不再发一轮
  inflightPromise: null, // 正在飞的那一轮请求（强制刷新时等它结束）
  supported: true,       // 后端是否提供该接口（404/405 → false）
  selected: [],          // 「一键启动」勾选的模型别名（持久化到 localStorage）
};

const MSVC_POLL_MS = 2000;
const MSVC_POLL_MAX = 150;   // 最多连续轮询 150 次（≈5 分钟）后放弃，防跑飞

/** 读取持久化的「要启动哪些模型」选择 */
function loadMsvcSelection() {
  const raw = lsGet('msvc.selected', '');
  const arr = String(raw || '').split(',').map((s) => s.trim()).filter(Boolean);
  MSVC.selected = arr;
  return arr;
}

/** 持久化选择（下次启动平台时沿用同一批模型） */
function saveMsvcSelection(aliases) {
  MSVC.selected = Array.isArray(aliases) ? aliases.slice() : [];
  lsSet('msvc.selected', MSVC.selected.join(','));
}

/** 停止轮询（所有模型都落定时调用） */
function stopMsvcPolling() {
  if (MSVC.timer != null) {
    clearInterval(MSVC.timer);
    MSVC.timer = null;
  }
}

/** 是否有模型处于过渡态（starting / stopping） */
function msvcHasBusy() {
  if (Object.keys(MSVC.pending).length) return true;
  return MSVC.models.some((m) => m.busy === true);
}

/**
 * 拉取模型生命周期状态并刷新面板。
 * @param {boolean} force 忽略「已有请求在飞」的**静默跳过**判定。
 *   注意：force 也不会真的并发发请求 —— inflight 期间新来的调用一律
 *   排队等下一轮，避免后端刚重启时把路由表打爆。
 */
async function refreshModelLifecycle(force) {
  if (MSVC.inflight) {
    // 已有请求在飞：普通调用直接跳过；强制调用等它回来后再补一次
    if (!force) return;
    try { await MSVC.inflightPromise; } catch (e) { /* 忽略，等下面重试 */ }
    if (MSVC.inflight) return;
  }
  MSVC.inflight = true;
  const run = (async () => {
    try {
    const data = await api('/models/lifecycle');
    MSVC.supported = true;
    const list = (data && data.models) || [];
    // 清掉已经「落定」的乐观状态：
    //   期望 starting → 看到 running 就算到位（reachable 还要再等，但那时
    //   busy 由 running && !reachable 继续覆盖，轮询不会断）；
    //   期望 stopping → 看到没在跑就算到位。
    Object.keys(MSVC.pending).forEach((alias) => {
      const m = list.find((x) => x.alias === alias);
      if (!m) { delete MSVC.pending[alias]; return; }
      if (MSVC.pending[alias] === 'starting' && m.running) delete MSVC.pending[alias];
      if (MSVC.pending[alias] === 'stopping' && !m.running) delete MSVC.pending[alias];
    });
    MSVC.models = list.map((m) => ({
      alias: m.alias,
      label: m.label || m.alias,
      port: m.port,
      device: m.device || '',
      running: !!m.running,
      reachable: !!m.reachable,
      pid: m.pid,
      managed: !!m.managed,
      ctx_size: m.ctx_size,
      ngl: m.ngl,
      threads: m.threads,
      // busy = 过渡态。两种来源：
      //   ① 本地乐观状态（我们刚点了启/停，还没收到就绪信号）；
      //   ② 进程已在监听但 /v1/models 还没响应 —— 典型的「模型还在加载」窗口。
      // 两者都会让 UI 显示转圈并禁用开关；轮询有上限（MSVC_POLL_MAX），不会跑飞。
      busy: !!(MSVC.pending[m.alias] || (m.running && !m.reachable)),
    }));
    renderModelLifecycle();
    renderMsvcPicker();
    // 全部落定 → 停轮询
    if (!msvcHasBusy()) {
      stopMsvcPolling();
    } else {
      ensureMsvcPolling();
    }
  } catch (err) {
    // 404 / 405 → 后端还没重启，不提供该接口：只提示，不影响侧栏其它功能。
    // 判定用 HTTP 状态码（err.status），**不要**靠 message 文本 ——
    // FastAPI 的 detail 可能是任意文案（这里 405 的文案就是 "Method Not Allowed"）。
    const st = err && err.status;
    const msg = String((err && err.message) || '');
    if (st === 404 || st === 405 || MSVC.supported === false) {
      MSVC.supported = false;
      stopMsvcPolling();
      renderModelLifecycleUnsupported();
    } else {
      const m = $('#msvc-msg');
      if (m) {
        m.textContent = '读取模型状态失败：' + msg;
        m.className = 'msvc-msg err';
      }
    }
  } finally {
    MSVC.inflight = false;
  }
  })();
  MSVC.inflightPromise = run;
  await run;
}

/** 确保轮询在跑（只在过渡态） */
function ensureMsvcPolling() {
  if (MSVC.timer != null) return;
  let ticks = 0;
  MSVC.timer = setInterval(() => {
    ticks += 1;
    if (ticks > MSVC_POLL_MAX) { stopMsvcPolling(); return; }
    refreshModelLifecycle(true);
  }, MSVC_POLL_MS);
}

/** 后端不支持时的降级渲染 */
function renderModelLifecycleUnsupported() {
  const list = $('#msvc-list');
  if (list) {
    list.innerHTML = '<div class="muted sm">后端未提供模型管理接口，请重启 API</div>';
  }
  const sum = $('#msvc-sum');
  if (sum) sum.textContent = '不可用';
  const msg = $('#msvc-msg');
  if (msg) { msg.textContent = ''; msg.className = 'msvc-msg muted sm'; }
  ['#btn-msvc-start-all', '#btn-msvc-stop-all'].forEach((s) => {
    const b = $(s);
    if (b) b.disabled = true;
  });
  const lab = $('#msvc-launch-label');
  if (lab) lab.textContent = '一键启动';
  const pm = $('#msvc-pick-msg');
  if (pm) {
    pm.textContent = '后端未提供模型管理接口，请重启 API';
    pm.className = 'msvc-pick-msg err';
  }
  const pl = $('#msvc-pick-list');
  if (pl) pl.innerHTML = '';
}

/** 渲染「模型服务」面板（侧栏 设置与状态） */
function renderModelLifecycle() {
  const list = $('#msvc-list');
  if (!list) return;
  if (!MSVC.models.length) {
    list.innerHTML = '<div class="muted sm">没有可管理的模型端点</div>';
  } else {
    list.innerHTML = MSVC.models.map((m) => {
      const running = m.running;
      const busy = m.busy;
      const state = busy ? 'loading' : (running && m.reachable ? 'ready' : 'off');
      // 文案要区分三种「不在就绪态」的情况：
      //   我们刚点的停止 / 我们刚点的启动 / 进程已起但模型还没加载完
      const pending = MSVC.pending[m.alias];
      const stateText = busy
        ? (pending === 'stopping' ? '停止中' : (pending === 'starting' ? '启动中' : '加载中'))
        : (running && m.reachable ? '就绪' : (running ? '加载中' : '未运行'));
      const dev = (m.device || '').toUpperCase() === 'GPU' ? 'gpu' : 'cpu';
      const devText = (m.device || '').toUpperCase() === 'GPU' ? 'GPU' : 'CPU';
      // 次要信息：端口 + 上下文 / 线程（有值才显示）
      const bits = [];
      if (m.port) bits.push(':' + m.port);
      if (m.ctx_size) bits.push('ctx ' + m.ctx_size);
      if (m.threads) bits.push(m.threads + ' 线程');
      const sub = bits.join(' · ');
      const checked = running ? 'true' : 'false';
      return '<div class="msvc-row ' + (running ? '' : 'is-off ') + (busy ? 'is-busy' : '')
        + '" data-alias="' + esc(m.alias) + '">'
        + '<span class="msvc-dot ' + state + '" title="' + esc(stateText) + '"></span>'
        + '<span class="msvc-main">'
        + '<span class="msvc-name" title="' + esc(m.label) + '">' + esc(m.label) + '</span>'
        + '<span class="msvc-sub">' + esc(sub) + '</span>'
        + '</span>'
        + '<span class="msvc-dev ' + dev + '">' + devText + '</span>'
        + '<button type="button" class="msvc-sw ' + (busy ? 'is-busy' : '') + '"'
        + ' role="switch" aria-checked="' + checked + '"'
        + (busy ? ' disabled' : '')
        + ' aria-label="' + esc(m.label + ' 开关') + '"'
        + ' title="' + esc(stateText) + '"><i class="msvc-knob"></i></button>'
        + '</div>';
    }).join('');

    // 开关事件（每次重渲染后重新绑定；行数量很少，开销可忽略）
    $$('#msvc-list .msvc-sw').forEach((sw) => {
      sw.addEventListener('click', () => {
        const row = sw.closest('.msvc-row');
        if (!row) return;
        toggleModelService(row.dataset.alias);
      });
    });
  }

  const ready = MSVC.models.filter((m) => m.running && m.reachable).length;
  const sum = $('#msvc-sum');
  if (sum) sum.textContent = MSVC.models.length ? `${ready}/${MSVC.models.length} 就绪` : '–';
  const busy = msvcHasBusy();
  ['#btn-msvc-start-all', '#btn-msvc-stop-all'].forEach((s) => {
    const b = $(s);
    if (b) b.disabled = busy || !MSVC.models.length;
  });
  const msg = $('#msvc-msg');
  if (msg) { msg.textContent = ''; msg.className = 'msvc-msg muted sm'; }
  updateMsvcLaunchLabel();
}

/** composer 上的「一键启动」按钮文案：反映当前勾选数量 */
function updateMsvcLaunchLabel() {
  const lab = $('#msvc-launch-label');
  if (!lab) return;
  // 没保存过选择时不显示计数（此时语义 = 「按默认全选启动」）
  const saved = lsGet('msvc.selected', null);
  const n = saved === null ? 0 : MSVC.selected.length;
  lab.textContent = n ? `一键启动 (${n})` : '一键启动';
}

/** 单个模型：启 / 停 */
async function toggleModelService(alias) {
  if (!alias || MSVC.pending[alias]) return;   // 过渡态禁止重复操作
  const m = MSVC.models.find((x) => x.alias === alias);
  if (!m) return;
  const wantStart = !m.running;
  // 乐观标记为过渡态：立刻禁用开关并显示转圈
  MSVC.pending[alias] = wantStart ? 'starting' : 'stopping';
  MSVC.models = MSVC.models.map((x) => x.alias === alias ? Object.assign({}, x, { busy: true }) : x);
  renderModelLifecycle();
  ensureMsvcPolling();
  try {
    const path = wantStart ? '/models/' + encodeURIComponent(alias) + '/start'
      : '/models/' + encodeURIComponent(alias) + '/stop';
    const res = await api(path, { method: 'POST' });
    const st = (res && res.status) || '';
    if (st === 'error') {
      delete MSVC.pending[alias];
      MSVC.models = MSVC.models.map((x) => x.alias === alias ? Object.assign({}, x, { busy: false }) : x);
      renderModelLifecycle();
      setMsvcMessage('操作失败：' + ((res && res.message) || '未知错误'), true);
    }
    // starting / already_running / stopped / not_running —— 都靠轮询收敛
  } catch (err) {
    delete MSVC.pending[alias];
    MSVC.models = MSVC.models.map((x) => x.alias === alias ? Object.assign({}, x, { busy: false }) : x);
    renderModelLifecycle();
    setMsvcMessage('操作失败：' + ((err && err.message) || '网络错误'), true);
  }
}

/** 全部启动 */
async function startAllModelServices() {
  MSVC.pending = {};
  MSVC.models = MSVC.models.map((x) => Object.assign({}, x, { busy: true }));
  renderModelLifecycle();
  ensureMsvcPolling();
  try {
    await api('/models/start-all', { method: 'POST' });
  } catch (err) {
    stopMsvcPolling();
    MSVC.models = MSVC.models.map((x) => Object.assign({}, x, { busy: false }));
    renderModelLifecycle();
    setMsvcMessage('启动失败：' + ((err && err.message) || '网络错误'), true);
  }
}

/** 全部停止 */
async function stopAllModelServices() {
  MSVC.pending = {};
  MSVC.models = MSVC.models.map((x) => Object.assign({}, x, { busy: true }));
  renderModelLifecycle();
  ensureMsvcPolling();
  try {
    await api('/models/stop-all', { method: 'POST' });
  } catch (err) {
    stopMsvcPolling();
    MSVC.models = MSVC.models.map((x) => Object.assign({}, x, { busy: false }));
    renderModelLifecycle();
    setMsvcMessage('停止失败：' + ((err && err.message) || '网络错误'), true);
  }
}

/** 面板底部消息 */
function setMsvcMessage(text, isErr) {
  const msg = $('#msvc-msg');
  if (msg) {
    msg.textContent = text || '';
    msg.className = 'msvc-msg muted sm' + (isErr ? ' err' : '');
  }
}

/* ----------------------------------------------------------------------
   一键启动：勾选要用的模型 → 一次点击「启动勾选的 / 停掉没勾的」
   ---------------------------------------------------------------------- */

/** 渲染勾选列表（composer 的 popover） */
function renderMsvcPicker() {
  const box = $('#msvc-pick-list');
  if (!box) return;
  if (!MSVC.supported) { box.innerHTML = ''; return; }
  if (!MSVC.models.length) {
    box.innerHTML = '<div class="muted sm">没有可管理的模型端点</div>';
    return;
  }
  // 勾选状态：优先用持久化的选择；**从未保存过**时默认全选
  // （语义 = 「一键启动」先把该跑的跑起来，再按需取消勾选）
  const saved = lsGet('msvc.selected', null);
  const checkedSet = new Set(saved === null
    ? MSVC.models.map((m) => m.alias)
    : MSVC.selected);
  box.innerHTML = MSVC.models.map((m) => {
    const running = m.running;
    const state = m.busy ? 'loading' : (running && m.reachable ? 'ready' : (running ? 'loading' : 'off'));
    const stateText = m.busy ? '切换中' : (running && m.reachable ? '就绪' : (running ? '加载中' : '未运行'));
    return '<label class="msvc-pick ' + (running ? '' : 'is-off') + '">'
      + '<input type="checkbox" value="' + esc(m.alias) + '"'
      + (checkedSet.has(m.alias) ? ' checked' : '') + ' />'
      + '<span class="mp-name" title="' + esc(m.label) + '">' + esc(m.label) + '</span>'
      + '<span class="mp-state ' + state + '">' + esc(stateText) + '</span>'
      + '</label>';
  }).join('');
  // 勾选变化 → 更新「应用」按钮的计数与本地草稿
  $$('#msvc-pick-list input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener('change', updateMsvcPickSummary);
  });
  updateMsvcPickSummary();
}

/** 汇总「将启动 N 个 / 停止 M 个」，让用户点「应用」之前就知道会发生什么 */
function updateMsvcPickSummary() {
  const pm = $('#msvc-pick-msg');
  if (!pm) return;
  const picked = Array.from($$('#msvc-pick-list input[type="checkbox"]'))
    .filter((x) => x.checked).map((x) => x.value);
  const toStart = picked.filter((a) => {
    const m = MSVC.models.find((x) => x.alias === a);
    return m && !m.running;
  }).length;
  const toStop = MSVC.models.filter((m) => picked.indexOf(m.alias) < 0 && m.running).length;
  pm.textContent = '将启动 ' + toStart + ' 个 / 停止 ' + toStop + ' 个';
  pm.className = 'msvc-pick-msg';
}

/**
 * 应用勾选：勾选的启动，没勾的停止 —— 这就是「一键启动」。
 * 全不选 = 把所有模型都停掉（等价于「全部停止」）；这是合法操作，
 * 持久化后下次启动平台也不起任何模型。
 */
async function applyMsvcSelection() {
  const picked = Array.from($$('#msvc-pick-list input[type="checkbox"]'))
    .filter((x) => x.checked).map((x) => x.value);
  const all = MSVC.models.map((m) => m.alias);
  const toStart = picked.filter((a) => {
    const m = MSVC.models.find((x) => x.alias === a);
    return m && !m.running;
  });
  const toStop = all.filter((a) => picked.indexOf(a) < 0).filter((a) => {
    const m = MSVC.models.find((x) => x.alias === a);
    return m && m.running;
  });

  // 持久化选择（下次启动平台沿用；注意：**持久化不等于启动**，
  // 真正的启动动作由 deploy\start_platform.ps1 负责）
  saveMsvcSelection(picked);
  updateMsvcLaunchLabel();

  if (!toStart.length && !toStop.length) {
    setMsvcMessage('所选模型状态已符合预期', false);
    if (typeof closePopover === 'function') closePopover('model');
    return;
  }

  // 乐观进入过渡态
  toStart.forEach((a) => { MSVC.pending[a] = 'starting'; });
  toStop.forEach((a) => { MSVC.pending[a] = 'stopping'; });
  MSVC.models = MSVC.models.map((x) => {
    const busy = toStart.indexOf(x.alias) >= 0 || toStop.indexOf(x.alias) >= 0;
    return busy ? Object.assign({}, x, { busy: true }) : x;
  });
  renderModelLifecycle();
  ensureMsvcPolling();

  // 逐个启停：/models/{alias}/start 与 /models/{alias}/stop 互不影响，
  // 并发发更快，但数量很少（2 个），直接串行 Promise.all 即可
  const jobs = [];
  toStart.forEach((a) => {
    jobs.push(api('/models/' + encodeURIComponent(a) + '/start', { method: 'POST' }));
  });
  toStop.forEach((a) => {
    jobs.push(api('/models/' + encodeURIComponent(a) + '/stop', { method: 'POST' }));
  });
  try {
    await Promise.all(jobs);
    setMsvcMessage('已提交：启动 ' + toStart.length + ' 个 / 停止 ' + toStop.length + ' 个，正在加载…', false);
    if (typeof closePopover === 'function') closePopover('model');
  } catch (err) {
    setMsvcMessage('操作失败：' + ((err && err.message) || '网络错误'), true);
    refreshModelLifecycle(true);
  }
}

/**
 * 绑定「模型服务」相关的所有交互：
 *   · 侧栏面板的开关 / 全部启动 / 全部停止
 *   · composer 的「一键启动」按钮（勾选 → 应用）
 *   · 首次进入时拉一次状态；恢复持久化的勾选选择
 */
function initModelServices() {
  loadMsvcSelection();
  updateMsvcLaunchLabel();

  // 注册到 workbench.js 的通用 popover 管理里。
  // 为什么在这里注册而不是改 workbench.js：
  //   workbench.js 的 popovers / POPOVER_SEL 是顶层 const（全局词法作用域），
  //   可以从其它脚本读写；app.js 先加载，所以只能在**运行时**（函数调用时）
  //   注册，而不是在 app.js 顶层。
  // ★ 这里**不注册**新的 popover 类型：「一键启动」的勾选区直接放进
  //   既有的 #model-popover（见 index.html），避免多开一个浮层
  //   （浮层阴影数量是受控资源，ui_style_check.py 断言 ≤ 8 处）。

  // 侧栏：全部启动 / 全部停止
  const btnAll = $('#btn-msvc-start-all');
  if (btnAll) btnAll.addEventListener('click', startAllModelServices);
  const btnNone = $('#btn-msvc-stop-all');
  if (btnNone) btnNone.addEventListener('click', stopAllModelServices);

  // composer 的「一键启动」chip → 打开既有的模型选择 popover，
  // 并把焦点落在「选择启动哪些模型」那一段上
  const btnLaunch = $('#btn-msvc-launch');
  if (btnLaunch) {
    btnLaunch.addEventListener('click', (e) => {
      e.stopPropagation();
      if (typeof openPopover !== 'function') return;
      const p = typeof popovers !== 'undefined' ? popovers.model : null;
      if (p && p.open) { closePopover('model'); return; }
      refreshModelLifecycle(true);
      renderMsvcPicker();
      openPopover('model', btnLaunch, btnLaunch.getBoundingClientRect());
      const sec = $('.msvc-pick-sec');
      if (sec) sec.scrollIntoView({ block: 'nearest' });
    });
  }
  const btnPickNone = $('#btn-msvc-pick-none');
  if (btnPickNone) {
    btnPickNone.addEventListener('click', () => {
      $$('#msvc-pick-list input[type="checkbox"]').forEach((cb) => { cb.checked = false; });
      updateMsvcPickSummary();
    });
  }
  const btnApply = $('#btn-msvc-apply');
  if (btnApply) btnApply.addEventListener('click', applyMsvcSelection);

  // 首次拉一次状态（失败也无妨，面板会显示降级提示）
  refreshModelLifecycle(true);
}

/* ----------------------------------------------------------------------
   桌面版「一键启动」：标题栏入口 → 勾选模型 → 一次点击把平台拉起来
   ----------------------------------------------------------------------
   为什么要它（用户原话：「现在桌面版没有一键启动」）：
     · 侧栏的「模型服务」开关是**逐个**操作的，用户要「用哪个起哪个」；
     · composer 上的「一键启动」chip 藏在输入区里，也不算「桌面级入口」；
     · 这里在**标题栏**放一个常驻按钮 —— 任何视图下都在，点开就能
       勾选要跑哪些模型，一次点击完成「启动勾选的 / 停掉没勾的」。

   与既有的 MSVC 模块的关系：
     · MSVC 管「侧栏里的逐个开关」和「composer 里的勾选区」；
     · LAUNCH 管「标题栏的桌面级入口」，**复用同一份持久化选择**
       （localStorage key: agentplat.msvc.selected），两处行为一致。

   后端契约（本次新增）：
     · GET  /platform/status → {api, models[], kb, summary, one_click_ready}
     · POST /platform/launch {models:[...]} → {status, started[], stopped[], message}
   轮询策略：
     · 启动期间每 2 秒拉一次 /platform/status；
     · **有上限**：最多 150 次（≈5 分钟），到点就停并给出提示，
       绝不让定时器一直跑（用户强调过「不能无脑轮询」）。
   ---------------------------------------------------------------------- */
const LAUNCH = {
  models: [],        // /platform/status 的 models[]
  status: null,      // 最近一次 /platform/status 的完整响应
  reachable: true,   // API 是否可达（false → 面板显示「API 未启动」）
  busy: false,       // 启动中
  timer: null,       // 轮询句柄
  ticks: 0,          // 已轮询次数（上限保护）
  inflight: false,   // 防重入
  userTouched: false, // 用户是否手动改过勾选（改过就以面板当前状态为准）
};

const LAUNCH_POLL_MS = 2000;
const LAUNCH_POLL_MAX = 150;   // 最多 150 次 ≈ 5 分钟

/** 停止启动轮询 */
function stopLaunchPolling() {
  if (LAUNCH.timer != null) {
    clearInterval(LAUNCH.timer);
    LAUNCH.timer = null;
  }
}

/** 当前勾选的模型别名（从面板里的复选框读） */
function launchPicked() {
  return Array.from($$('#launch-list input[type="checkbox"]'))
    .filter((x) => x.checked).map((x) => x.value);
}

/** 更新标题栏按钮的忙碌态（转圈 + 文案） */
function updateLaunchButton() {
  const btn = $('#btn-launch');
  if (!btn) return;
  const spin = $('#tb-launch-spin');
  const lab = $('#tb-launch-label');
  btn.classList.toggle('is-busy', LAUNCH.busy);
  if (spin) spin.hidden = !LAUNCH.busy;
  if (lab) lab.textContent = LAUNCH.busy ? '启动中…' : '一键启动';
}

/** 面板顶部的一句话状态 */
function setLaunchSummary(text, kind) {
  const el = $('#launch-summary');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'launch-summary' + (kind ? ' ' + kind : '');
}

/** 面板底部的消息条 */
function setLaunchMsg(text, kind) {
  const el = $('#launch-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'launch-msg' + (kind ? ' ' + kind : '');
}

/** 渲染面板里的模型清单（复选框 + 就绪圆点 + 状态文字） */
function renderLaunchList() {
  const box = $('#launch-list');
  if (!box) return;
  if (!LAUNCH.reachable) {
    box.innerHTML = '<div class="muted sm">API 未启动，请先启动 API</div>';
    return;
  }
  if (!LAUNCH.models.length) {
    box.innerHTML = '<div class="muted sm">没有可管理的模型端点</div>';
    return;
  }
  // 勾选状态的优先级：
  //   ① **面板里当前的勾选**（用户正在改，别把他改到一半的选择刷掉 ——
  //      启动期间每 2 秒会重渲染一次）；
  //   ② 持久化的选择（localStorage agentplat.msvc.selected）；
  //   ③ **从未保存过**时默认只勾平台默认模型
  //      （语义 = 「一键启动」= 起该跑的那个，其余的不起）。
  // ★ 用 ``LAUNCH.userTouched`` 区分「用户主动清空」和「还没渲染过」：
  //   前者要尊重「一个都不勾」，后者才轮到持久化 / 默认值。
  const saved = lsGet('msvc.selected', null);
  const defaultAlias = (LAUNCH.status && LAUNCH.status.default) || 'gpt-oss-20b';
  let checkedSet;
  if (LAUNCH.userTouched) {
    checkedSet = new Set(launchPicked());
  } else if (saved === null) {
    checkedSet = new Set([defaultAlias]);
  } else {
    checkedSet = new Set(String(saved || '').split(',')
      .map((s) => s.trim()).filter(Boolean));
  }

  box.innerHTML = LAUNCH.models.map((m) => {
    const state = (m.running && m.reachable) ? 'ready' : (m.running ? 'loading' : 'off');
    const stateText = state === 'ready' ? '就绪' : (state === 'loading' ? '加载中' : '未运行');
    const dev = (m.device || '').toUpperCase() === 'GPU' ? 'GPU' : 'CPU';
    return '<label class="launch-item" data-alias="' + esc(m.alias) + '">'
      + '<input type="checkbox" value="' + esc(m.alias) + '"'
      + (checkedSet.has(m.alias) ? ' checked' : '') + ' />'
      + '<span class="li-dot ' + state + '"></span>'
      + '<span class="li-name" title="' + esc(m.label) + '">' + esc(m.label) + '</span>'
      + '<span class="li-state ' + state + '">' + dev + ' · ' + esc(stateText) + '</span>'
      + '</label>';
  }).join('');

  $$('#launch-list input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener('change', () => {
      // 标记「用户改过勾选」——之后重渲染要**尊重面板当前状态**，
      // 不能拿持久化 / 默认值把用户的选择刷掉。
      LAUNCH.userTouched = true;
      updateLaunchSummary();
    });
  });
  updateLaunchSummary();
}

/** 汇总「将启动 N 个 · 停止 M 个」 */
function updateLaunchSummary() {
  const picked = launchPicked();
  const toStart = LAUNCH.models.filter((m) => picked.indexOf(m.alias) >= 0 && !m.running).length;
  const toStop = LAUNCH.models.filter((m) => picked.indexOf(m.alias) < 0 && m.running).length;
  setLaunchSummary('将启动 ' + toStart + ' 个 · 停止 ' + toStop + ' 个');
  const go = $('#btn-launch-go');
  if (go) go.disabled = !LAUNCH.reachable || LAUNCH.busy;
}

/** 拉一次 /platform/status 并刷新面板 */
async function refreshLaunchStatus() {
  if (LAUNCH.inflight) return;
  LAUNCH.inflight = true;
  try {
    const data = await api('/platform/status');
    LAUNCH.reachable = true;
    LAUNCH.status = data;
    LAUNCH.models = (data && data.models) || [];
    renderLaunchList();
    const ready = LAUNCH.models.filter((m) => m.running && m.reachable).length;
    setLaunchSummary((data && data.summary) || '平台就绪',
      (data && data.one_click_ready) ? 'ok' : '');
    const go = $('#btn-launch-go');
    if (go) go.disabled = LAUNCH.busy;
    // 收敛判定：勾选的模型**全部** reachable → 启动完成，停轮询
    if (LAUNCH.busy) {
      const picked = launchPicked();
      // ★ 一个都没勾 = 「把模型都停掉」——此时没有「要等就绪的模型」，
      //   立即收敛（否则会白等满 5 分钟）。
      // ★ 勾了但后端一个都没在启动（started 为空）也视为已完成：
      //   说明它们本来就都在跑，等一轮状态刷新确认即可。
      const allReady = picked.length === 0
        || picked.every((a) => {
          const m = LAUNCH.models.find((x) => x.alias === a);
          return m && m.running && m.reachable;
        });
      if (allReady) {
        stopLaunchPolling();
        LAUNCH.busy = false;
        updateLaunchButton();
        setLaunchMsg('平台已就绪：' + ready + '/' + LAUNCH.models.length + ' 个模型可用', 'ok');
        const go2 = $('#btn-launch-go');
        if (go2) go2.disabled = false;
      }
    }
  } catch (err) {
    // API 不可达 —— 明确告诉用户「先启动 API」，不要静默失败
    LAUNCH.reachable = false;
    LAUNCH.models = [];
    LAUNCH.status = null;
    renderLaunchList();
    setLaunchSummary('API 未启动，请先启动 API', 'err');
    setLaunchMsg('无法连接 API：' + ((err && err.message) || '网络错误'), 'err');
    const go = $('#btn-launch-go');
    if (go) go.disabled = true;
  } finally {
    LAUNCH.inflight = false;
  }
}

/** 启动轮询：每 2 秒一次，最多 LAUNCH_POLL_MAX 次 */
function ensureLaunchPolling() {
  if (LAUNCH.timer != null) return;
  LAUNCH.ticks = 0;
  LAUNCH.timer = setInterval(() => {
    LAUNCH.ticks += 1;
    if (LAUNCH.ticks > LAUNCH_POLL_MAX) {
      stopLaunchPolling();
      setLaunchMsg('等待超时（5 分钟）：模型可能仍在加载，请查看日志或手动启动', 'err');
      LAUNCH.busy = false;
      updateLaunchButton();
      refreshLaunchStatus();
      return;
    }
    refreshLaunchStatus();
  }, LAUNCH_POLL_MS);
}

/**
 * 执行一键启动：把勾选结果交给后端统一编排。
 * 语义与用户要求一致：**选中的启动，没选中的停掉**。
 */
async function runLaunch() {
  if (LAUNCH.busy) return;
  const picked = launchPicked();
  // ★ 一个都没勾 = 「把所有模型停掉」——
  //   与侧栏 / composer 的既有语义一致（"全不选 = 全部停掉"）。
  //   注意：**不能**把它发给 ``POST /platform/launch`` 的空列表 ——
  //   那个接口把空列表解释成「平台默认」，语义正好相反。
  if (!picked.length) {
    await launchNoModels();
    return;
  }
  // 持久化（与侧栏 / composer 共用同一个 key，两处行为一致）
  saveMsvcSelection(picked);
  updateMsvcLaunchLabel();

  LAUNCH.busy = true;
  updateLaunchButton();
  setLaunchMsg('正在提交…');
  const go = $('#btn-launch-go');
  if (go) go.disabled = true;

  try {
    const res = await api('/platform/launch', {
      method: 'POST',
      body: JSON.stringify({ models: picked }),
    });
    const started = (res && res.started) || [];
    const stopped = (res && res.stopped) || [];
    const already = (res && res.already_running) || [];
    const bits = [];
    if (started.length) bits.push('启动 ' + started.length + ' 个');
    if (stopped.length) bits.push('停止 ' + stopped.length + ' 个');
    if (already.length && !started.length) bits.push('所选模型已在运行');
    setLaunchMsg(bits.length ? bits.join(' · ') + '（加载中…）' : '状态已符合预期');
    // 轮询直到选中的模型全部 reachable（或到达 5 分钟上限）
    ensureLaunchPolling();
  } catch (err) {
    LAUNCH.busy = false;
    updateLaunchButton();
    setLaunchMsg('启动失败：' + ((err && err.message) || '网络错误'), 'err');
    refreshLaunchStatus();
  }
}

/** 「全部启动」：勾上所有模型再执行 */
async function launchAllModels() {
  $$('#launch-list input[type="checkbox"]').forEach((cb) => { cb.checked = true; });
  LAUNCH.userTouched = true;
  updateLaunchSummary();
  await runLaunch();
}

/**
 * 「全部停止」：把**所有**模型停掉。
 *
 * ★ 这里**不能**用 ``POST /platform/launch`` + 空列表：
 *   那个接口把空列表解释成「**平台默认**」（= 启动 gpt-oss-20b），
 *   与「全部停止」的语义正好相反。所以直接用既有的
 *   ``POST /models/stop-all``（逐个停，幂等）。
 */
async function launchNoModels() {
  $$('#launch-list input[type="checkbox"]').forEach((cb) => { cb.checked = false; });
  LAUNCH.userTouched = true;
  updateLaunchSummary();
  // 持久化：下次打开面板 / 桌面版时不会又把这些模型拉起来
  saveMsvcSelection([]);
  updateMsvcLaunchLabel();
  LAUNCH.busy = true;
  updateLaunchButton();
  setLaunchMsg('正在停止全部模型…');
  try {
    const res = await api('/models/stop-all', { method: 'POST' });
    const stopped = (res && res.results) || [];
    const n = stopped.filter((r) => r && r.status === 'stopped').length;
    setLaunchMsg(n ? '已停止 ' + n + ' 个模型' : '没有正在运行的模型');
  } catch (err) {
    setLaunchMsg('停止失败：' + ((err && err.message) || '网络错误'), 'err');
  }
  LAUNCH.busy = false;
  updateLaunchButton();
  refreshLaunchStatus();
}

/** 打开 / 关闭启动面板 */
function toggleLaunchPanel() {
  const panel = $('#launch-panel');
  const btn = $('#btn-launch');
  if (!panel || !btn) return;
  if (!panel.hidden) {
    panel.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    btn.classList.remove('open');
    return;
  }
  panel.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
  btn.classList.add('open');
  // 每次打开都重置「用户改过勾选」的标记 —— 让清单回到**持久化的选择**
  // （用户刚打开时想看到的应该是上次的选择，而不是上一轮遗留的临时状态）。
  LAUNCH.userTouched = false;
  // 定位：**贴着标题栏下沿**（而不是按钮下沿）——
  // 按钮在标题栏里垂直居中（34px 高的标题栏里只有 22px），
  // 按按钮下沿定位会让面板**压住标题栏 2px**，看着像是浮层漏进了标题栏。
  const r = btn.getBoundingClientRect();
  const tb = document.querySelector('.titlebar');
  const tbRect = tb ? tb.getBoundingClientRect() : null;
  const w = panel.offsetWidth || 288;
  const h = panel.offsetHeight || 240;
  let left = r.right - w;
  let top = (tbRect ? tbRect.bottom : r.bottom) + 4;
  if (left < 8) left = 8;
  if (left + w > window.innerWidth - 8) left = Math.max(8, window.innerWidth - w - 8);
  if (top + h > window.innerHeight - 8) top = Math.max(8, window.innerHeight - h - 8);
  panel.style.left = Math.round(left) + 'px';
  panel.style.top = Math.round(top) + 'px';
  panel.classList.add('opening');
  setTimeout(() => panel.classList.remove('opening'), 160);
  refreshLaunchStatus();
}

/**
 * 绑定「一键启动」的全部交互（标题栏按钮 + 面板里的按钮）。
 */
function initOneClickLaunch() {
  const btn = $('#btn-launch');
  if (btn) {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleLaunchPanel();
    });
  }
  const close = $('#launch-close');
  if (close) {
    close.addEventListener('click', () => {
      const panel = $('#launch-panel');
      if (panel) panel.hidden = true;
      if (btn) {
        btn.setAttribute('aria-expanded', 'false');
        btn.classList.remove('open');
      }
    });
  }
  const go = $('#btn-launch-go');
  if (go) go.addEventListener('click', runLaunch);
  const all = $('#btn-launch-all');
  if (all) all.addEventListener('click', launchAllModels);
  const none = $('#btn-launch-none');
  if (none) none.addEventListener('click', launchNoModels);

  // 点击面板外部关闭（与既有 popover 的 mousedown 关闭逻辑保持一致）
  document.addEventListener('mousedown', (e) => {
    const panel = $('#launch-panel');
    if (!panel || panel.hidden) return;
    if (e.target.closest('#launch-panel') || e.target.closest('#btn-launch')) return;
    panel.hidden = true;
    if (btn) {
      btn.setAttribute('aria-expanded', 'false');
      btn.classList.remove('open');
    }
  });
  // Esc 关闭
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const panel = $('#launch-panel');
    if (panel && !panel.hidden) {
      panel.hidden = true;
      if (btn) {
        btn.setAttribute('aria-expanded', 'false');
        btn.classList.remove('open');
      }
    }
  });

  updateLaunchButton();
  // 首次拉一次状态（失败也无妨，面板会显示「API 未启动」提示）
  refreshLaunchStatus();
}

async function refreshMetrics() {
  const box = $('#metrics-list');
  if (!box) return;
  try {
    const m = await api('/metrics');
    const rows = [
      ['请求总数', m.requests_total], ['对话', m.chat_total],
      ['审批', m.approve_total], ['任务', m.tasks_total],
      ['token', m.tokens_total], ['工具调用', m.tool_calls_total],
      ['错误', m.errors_total], ['平均耗时', fmtMs(m.avg_duration_ms)],
      ['会话', m.sessions], ['运行时长', fmtMs((m.uptime_seconds || 0) * 1000)],
    ];
    box.innerHTML = rows.map((r) =>
      '<div class="tree-row leaf"><span class="tree-label dim">' + esc(r[0])
      + '</span><span class="tree-val">' + esc(r[1] == null ? '–' : r[1]) + '</span></div>').join('');
  } catch (err) {
    box.innerHTML = '<div class="muted sm">加载失败</div>';
  }
}

async function refreshTools() {
  const box = $('#tools-list');
  if (!box) return;
  try {
    const t = await api('/tools');
    const count = t.count || 0;
    const cnt = $('#t-count');
    if (cnt) cnt.textContent = `${count} 个工具`;
    box.innerHTML = '';
    const all = (t.tools || []);
    const LIMIT = 12;
    const shown = state.toolsExpanded ? all : all.slice(0, LIMIT);
    if (!all.length) {
      box.innerHTML = '<div class="muted">暂无工具</div>';
    }
    shown.forEach((tool) => {
      const el = document.createElement('div');
      el.className = 'tree-row tree-row2 leaf is-clickable';
      el.innerHTML = `<span class="row-line1">`
        + `<span class="row-title mono row-title-link" title="点击查看 ${esc(tool.name)} 的完整契约（参数表 / JSON Schema / OpenAI function）">${esc(tool.name)}</span>`
        + `<span class="row-meta"><span class="perm ${esc(tool.permission)}">${esc(tool.permission)}</span>`
        + (tool.hitl_required ? '<span class="pill pill-wait">需人工确认</span>' : '')
        + `</span></span>`
        + `<span class="row-line2">${esc(tool.description || '')}</span>`;
      box.appendChild(el);
    });
    const btn = $('#btn-tools-more');
    if (btn) {
      btn.hidden = all.length <= LIMIT;
      btn.textContent = state.toolsExpanded
        ? '收起（仅显示前 12 条）'
        : `展开全部（${all.length}）`;
    }
  } catch (err) {
    box.innerHTML = '<div class="muted">工具加载失败</div>';
  }
}

/* ----------------------------------------------------------------------
   历史会话：分页加载 + 两行结构
   ---------------------------------------------------------------------- */
const SESS_PAGE = 30;                 // 每页加载的会话条数

/** 会话标题：优先用后端 title（= 首条用户消息），其次用 summary 截断 */
function sessTitleOf(it) {
  const t = (it.title || '').trim();
  if (t) return t;
  const s = (it.summary || '').trim();
  return s ? (s.length > 60 ? s.slice(0, 60) + '…' : s) : '(空会话)';
}

/** 会话副标题：摘要结论（有则显示，否则显示消息数） */
function sessSubOf(it) {
  const s = (it.summary || '').trim();
  if (s) return s.length > 80 ? s.slice(0, 80) + '…' : s;
  const n = it.message_count || 0;
  return n ? `${n} 条消息` : '无消息';
}

async function refreshSessions() {
  const box = $('#sessions-list');
  if (!box) return;
  const sum = $('#sess-summary');
  const moreBtn = $('#btn-sessions-more');
  try {
    const s = await api('/sessions');
    box.innerHTML = '';
    const list = s.sessions || [];
    const total = s.count != null ? s.count : list.length;
    if (sum) sum.textContent = total ? `${total} 个会话` : '暂无会话';
    if (!list.length) {
      box.innerHTML = '<div class="muted">暂无会话</div>';
      if (moreBtn) moreBtn.hidden = true;
      return;
    }
    const page = Math.min(state.sessionsLimit || SESS_PAGE, list.length);
    const shown = list.slice(0, page);
    shown.forEach((it) => {
      const el = document.createElement('div');
      el.className = 'tree-row tree-row2 leaf act';
      const sumState = it.has_summary || it.summary_status === 'ready'
        ? '<span class="pill pill-ok">已摘要</span>'
        : '<span class="pill">未摘要</span>';
      el.innerHTML = `<span class="row-line1">`
        + `<span class="row-title" title="${esc(sessTitleOf(it))}">${esc(sessTitleOf(it))}</span>`
        + `<span class="row-meta">${sumState}</span>`
        + `</span>`
        + `<span class="row-line2 mono">${esc(it.thread_id)} · ${esc(fmtRelTime(it.updated_at))}</span>`;
      el.title = [sessTitleOf(it), sessSubOf(it), it.thread_id]
        .filter(Boolean).join('\n');
      el.addEventListener('click', () => {
        state.threadId = it.thread_id;
        state.sessionId = it.session_id || null;
        const mt = $('#m-thread');
        if (mt) mt.textContent = it.thread_id;
        if (typeof openSessionThread === 'function') {
          // 在主区打开该会话的 tab 并渲染**完整历史消息**
          openSessionThread(it.thread_id, sessTitleOf(it));
        } else {
          activateTab('pg-chat');
        }
      });
      box.appendChild(el);
    });
    // 「加载更多」按钮（与工具 / 任务的「展开全部」模式一致）
    if (moreBtn) {
      moreBtn.hidden = page >= total;
      moreBtn.textContent = `加载更多（已显示 ${page} / ${total}）`;
    }
  } catch (err) {
    // 加载失败：**明确错误态**（不是空白），并提示可刷新
    if (sum) sum.textContent = '会话加载失败';
    box.innerHTML = `<div class="pill pill-err">加载失败：${esc(err.message)}</div>`
      + `<div class="muted sm" style="margin-top:8px">`
      + `可用右上角刷新按钮重试。</div>`;
    if (moreBtn) moreBtn.hidden = true;
  }
}

/* ----------------------------------------------------------------------
   任务发布台：模板 / 任务
   ---------------------------------------------------------------------- */
async function refreshTemplates() {
  try {
    const t = await api('/tasks/templates');
    state.templates = t.templates || [];
    const sel = $('#tpl-select');
    if (!sel) return;
    sel.innerHTML = '';
    (t.categories || []).forEach((cat) => {
      const g = document.createElement('optgroup');
      g.label = cat;
      (t.groups[cat] || []).forEach((it) => {
        const o = document.createElement('option');
        o.value = it.id;
        o.textContent = `${it.id} · ${it.query.slice(0, 40)}`;
        o.dataset.query = it.query;
        o.dataset.tags = (it.tags || []).join(',');
        g.appendChild(o);
      });
      sel.appendChild(g);
    });
    sel.addEventListener('change', onTplChange);
    onTplChange();
  } catch (err) {
    const sel = $('#tpl-select');
    if (sel) sel.innerHTML = '<option value="">模板加载失败</option>';
  }
}

function onTplChange() {
  const sel = $('#tpl-select');
  const opt = sel.selectedOptions[0];
  if (!opt || !opt.dataset) return;
  const tags = opt.dataset.tags ? opt.dataset.tags.split(',').filter(Boolean) : [];
  const d = $('#tpl-detail');
  if (d) d.textContent = `标签：${tags.join(' / ') || '–'}`;
  $('#task-name').value = opt.value;
  $('#task-desc').value = opt.dataset.query || '';
}

async function refreshTasks() {
  try {
    const t = await api('/tasks');
    const cnt = $('#task-count');
    if (cnt) cnt.textContent = `${t.count} 个任务`;
    if (typeof rerenderTaskList === 'function') {
      rerenderTaskList(t.tasks || []);
      return;
    }
    const box = $('#tasks-list');
    if (!box) return;
    box.innerHTML = '';
    if (!t.tasks || !t.tasks.length) {
      box.innerHTML = '<div class="muted">暂无任务</div>';
      return;
    }
    t.tasks.slice().reverse().forEach((tk) => {
      const el = document.createElement('div');
      el.className = 'tree-row tree-row2 leaf act';
      el.innerHTML = `<span class="row-line1">`
        + `<span class="row-title" title="${esc(tk.name)}">${esc(tk.name)}</span>`
        + `<span class="row-meta"><span class="st ${esc(tk.status)}">${esc(tk.status)}</span></span></span>`
        + `<span class="row-line2">${esc(tk.result_summary || tk.error || tk.description || '')}</span>`;
      box.appendChild(el);
    });
  } catch (err) { /* 忽略 */ }
}

async function publishTask() {
  const name = ($('#task-name').value || '').trim();
  const desc = ($('#task-desc').value || '').trim();
  if (!name) { addSystemMsg('请填写任务名'); return; }
  if (!desc) { addSystemMsg('请填写任务描述 / Prompt'); return; }

  const params = {
    max_rounds: parseInt($('#p-rounds').value, 10) || undefined,
    temperature: parseFloat($('#p-temp').value),
    reasoning_effort: $('#p-effort').value,
    max_tokens: parseInt($('#p-maxtok').value, 10) || undefined,
  };

  const btn = $('#btn-publish');
  btn.disabled = true;
  try {
    await api('/tasks', {
      method: 'POST',
      body: JSON.stringify({
        name: name,
        description: desc,
        prompt: desc,
        model: $('#task-model').value,
        mode: ($('#task-mode') || {}).value || 'agent',
        params: params,
      }),
    });
    addSystemMsg('任务已发布：' + name);
    refreshTasks();
  } catch (err) {
    addSystemMsg('发布失败：' + err.message);
  } finally {
    btn.disabled = false;
  }
}

/* ----------------------------------------------------------------------
   事件绑定
   ---------------------------------------------------------------------- */
function bind() {
  $('#composer').addEventListener('submit', (e) => {
    e.preventDefault();
    send();
  });

  /* ---- Enter 发送 / Shift+Enter 换行（兼容 IME 中文输入）----
     注意：isComposing 为 true 时（中文输入法组字过程中）**不能**发送，
     否则用户选词确认时会被误触发提交。 */
  $('#input').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    if (e.isComposing || e.keyCode === 229) return;   // IME 组字中 → 交给输入法
    if (e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;  // 组合键 → 换行
    e.preventDefault();
    send();
  });

  /* ---- Ctrl/Cmd+Enter 也发送 ---- */
  $('#input').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    send();
  });

  /* ---- 任务描述框：Enter 发布任务，Shift+Enter 换行 ---- */
  $('#task-desc').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    if (e.isComposing || e.keyCode === 229) return;
    if (e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;
    e.preventDefault();
    publishTask();
  });

  /* ---- ReAct 轮次上限：输入区 ↔ 任务发布台 双向同步（2026-11-24 新增）
     此前「max_rounds」只在任务发布台里有，**对话**流程完全用不到
     （服务端硬编码 8 轮）→ 用户抱怨「才几步就停了」。 */
  const roundsA = $('#chat-rounds');
  const roundsB = $('#p-rounds');
  if (roundsA) {
    roundsA.addEventListener('change', () => { setChatMaxRounds(roundsA.value); });
  }
  if (roundsB) {
    roundsB.addEventListener('change', () => { setChatMaxRounds(roundsB.value); });
  }

  /* ---- 输入框自增高 ---- */
  const input = $('#input');
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 220) + 'px';
  });

  $('#btn-new').addEventListener('click', () => {
    state.threadId = null;
    state.sessionId = null;
    const mt = $('#m-thread');
    if (mt) mt.textContent = '（新建）';
    /* ---- 先确保对话页还在（可能被误删过）----
     * activateTab('pg-chat') 内部也会做同样的兜底重建，
     * 这里提前调一次，是为了让下面 #chat 的取用必然拿到元素。
     */
    ensureChatPage();
    activateTab('pg-chat');
    /* ---- 重建空状态（welcome）----
     * 真实故障（2026-11-24 修复）：此前这里只 ``chat.innerHTML = ''``，
     * 接着 addSystemMsg 只留下一行 12px 的灰色提示
     * → 视觉上就是**一片空白**，用户以为功能坏了。
     * 现在重新渲染 welcome 空状态（图标 + 标题 + 说明 + 示例）。
     */
    renderWelcome($('#chat'));
    addSystemMsg('已新建会话。');
    if (input) input.focus();
    /* ---- 运行观测：新会话 = 清空观测快照（否则会看到上一轮的数据）---- */
    if (typeof obsReset === 'function') obsReset();
  });

  $('#btn-tpl-use').addEventListener('click', () => {
    const q = ($('#tpl-select').selectedOptions[0] || {}).dataset;
    if (q && q.query) { input.value = q.query; input.focus(); }
  });

  $('#btn-tpl-fill').addEventListener('click', () => {
    const q = ($('#tpl-select').selectedOptions[0] || {}).dataset;
    if (q && q.query) { input.value = q.query; }
    publishTask();
  });

  $('#btn-publish').addEventListener('click', publishTask);

  /* ---- 工具清单「展开全部」---- */
  const bt = $('#btn-tools-more');
  if (bt) {
    bt.addEventListener('click', () => {
      state.toolsExpanded = !state.toolsExpanded;
      refreshTools();
    });
  }

  /* ---- 任务列表「查看全部」---- */
  const bk = $('#btn-tasks-more');
  if (bk) {
    bk.addEventListener('click', () => {
      state.tasksExpanded = !state.tasksExpanded;
      refreshTasks();
    });
  }

  /* ---- 历史会话「加载更多」（每次 +30 条）---- */
  const bs = $('#btn-sessions-more');
  if (bs) {
    bs.addEventListener('click', () => {
      state.sessionsLimit = (state.sessionsLimit || SESS_PAGE) + SESS_PAGE;
      refreshSessions();
    });
  }

  /* ---- 模型选择 ---- */
  const modelSel = $('#model-select');
  if (modelSel) {
    modelSel.value = state.model;
    modelSel.addEventListener('change', () => {
      state.model = modelSel.value;
      const lbl = $('#model-label');
      if (lbl) lbl.textContent = state.model;
      const tm = $('#task-model');
      if (tm) tm.value = state.model;
    });
  }
  const btnModel = $('#btn-model');
  if (btnModel) {
    btnModel.addEventListener('click', () => {
      if (modelSel) { modelSel.value = state.model; modelSel.focus(); }
    });
  }

  /* ---- 模型服务：启停面板 + 一键启动 ---- */
  initModelServices();
  if (typeof initOneClickLaunch === 'function') initOneClickLaunch();

  /* ---- 任务发布台抽屉开关 ---- */
  const btnToggle = $('#btn-tasks-toggle');
  if (btnToggle) {
    btnToggle.addEventListener('click', () => {
      const dr = $('#task-drawer');
      if (dr) dr.hidden = !dr.hidden;
    });
  }
  const btnClose = $('#btn-task-drawer-close');
  if (btnClose) {
    btnClose.addEventListener('click', () => {
      const dr = $('#task-drawer');
      if (dr) dr.hidden = true;
    });
  }

  /* ---- 侧栏刷新按钮 ---- */
  const bindRefresh = (sel, fn) => {
    const el = $(sel);
    if (el) el.addEventListener('click', fn);
  };
  bindRefresh('#btn-refresh-health', refreshHealth);
  bindRefresh('#btn-settings-refresh', () => {
    refreshHealth(); refreshMetrics(); refreshModelLifecycle(true);
  });
  bindRefresh('#btn-tools-refresh', refreshTools);
  bindRefresh('#btn-tasks-refresh', refreshTasks);
  bindRefresh('#btn-sessions-refresh', refreshSessions);
  bindRefresh('#btn-kb-refresh', () => {
    if (typeof refreshKbDocuments === 'function') refreshKbDocuments();
  });

  /* ---- 快捷键：Ctrl/Cmd+B 侧栏、Ctrl/Cmd+W 关标签 ---- */
  document.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    const k = (e.key || '').toLowerCase();
    if (k === 'b') {
      e.preventDefault();
      const sb = $('#sidebar');
      if (sb) sb.classList.toggle('collapsed');
      return;
    }
    if (k === 'w') {
      e.preventDefault();
      /* 对话页是**常驻页**，不可关闭。
       * 真实故障（2026-11-24 修复）：此前这个分支会 closeTab('pg-chat')，
       * 把对话页元素删掉 → 之后「新建会话」变成**彻底的空操作**
       * （activateTab 直接 return、#chat 已不存在 → 什么都不会发生）。
       * 现在只关**其它**标签页。
       */
      if (state.activeTab && state.activeTab !== 'pg-chat') closeTab(state.activeTab);
      return;
    }
    if (k === 'n') {
      e.preventDefault();
      const nb = $('#btn-new');
      if (nb) nb.click();
    }
  });
}

function send(explicitText, opts) {
  if (state.busy) {
    // 不要**静默丢弃**用户的输入：提示一下并保留输入框内容
    const tip = (typeof toast === 'function')
      ? toast('上一条还在回答中，请稍候…')
      : null;
    const inp = $('#input');
    if (inp && explicitText) inp.value = String(explicitText);
    return;
  }
  const input = $('#input');
  const options = opts || {};
  let text = explicitText;
  if (text === undefined || text === null) {
    text = input.value.trim();
    if (text) {
      input.value = '';
      input.style.height = 'auto';
    }
  } else {
    text = String(text).trim();
  }
  if (!text) return;
  /* ------------------------------------------------------------------
   * **待审批时的文字提示（2026-11-24 新增）**
   * ------------------------------------------------------------------
   * 真实故障：工具（尤其是 ``ws_write_file``）挂在 HITL 上等批准，
   * 用户却在对话框里敲「执行写操作」「确认执行」「go ahead」——
   * **对话文字不会放行任何审批**，模型只会重新发起一次调用，
   * 于是用户看到「代码都写不进去」。
   *
   * 现在：检测到「有待审批项」+「用户输入像是在催执行」时，
   * 弹一个 toast 明确告诉他「要点审批卡上的『批准』按钮」，
   * 而不是让他继续在对话里空转。
   */
  if (state.pendingApprovalCount > 0 && looksLikeApprovalCommand(text)) {
    const tip = (typeof toast === 'function')
      ? toast('请点击审批卡上的「批准」按钮（对话文字不会自动放行）')
      : null;
    if (tip) {
      /* 提示已经给出，不拦截这次发送（用户可能是在聊别的），
         但要让审批卡片再次滚动到视野中央。 */
      try { highlightPendingApprovals(); } catch (e) { /* 忽略 */ }
    }
  }
  state.busy = true;
  const sendBtn = $('#btn-send');
  if (sendBtn) sendBtn.disabled = true;
  activateTab('pg-chat');
  if (!options.regenerate) {
    state.lastQuestion = text;
  }
  /* 渲染消息可能失败（例如对话页已被 Ctrl+W 关闭 → #chat 已从 DOM 移除）。
     失败时必须**复位 busy 并恢复发送按钮**，否则 state.busy 永久为 true，
     之后每条消息都会被「上一条还在回答中，请稍候…」静默丢弃。 */
  try {
    if (!options.regenerate) {
      addUserMsg(text);
    } else {
      addSystemMsg('重新生成：' + text);
    }
  } catch (err) {
    state.busy = false;
    if (sendBtn) sendBtn.disabled = false;
    console.warn('[send] 消息渲染失败，已复位 busy', err);
    return;
  }
  /* streamChat 内部已自带 try/catch/finally，但这里再兜一层 .catch，
     确保任何意外 rejection 也能复位 busy（否则会永久卡死）。 */
  Promise.resolve(streamChat(text)).catch((err) => {
    state.busy = false;
    const b = $('#btn-send');
    if (b) b.disabled = false;
    console.warn('[send] 流式请求异常，已复位 busy', err);
  });
}

/* ----------------------------------------------------------------------
   侧栏筛选框（Cursor 资源管理器顶部的搜索/过滤框）
   ----------------------------------------------------------------------
   纯**客户端**筛选：对已渲染的行做文本匹配，命中则显示、不命中则隐藏。
   好处：不改任何数据加载逻辑（refreshTools / refreshSessions 等），
   也不影响「加载更多」分页；列表刷新后自动重放当前筛选条件。
   ---------------------------------------------------------------------- */
const SIDE_FILTERS = [
  { input: '#tools-filter', list: '#tools-list' },
  { input: '#sessions-filter', list: '#sessions-list' },
  { input: '#kb-filter', list: '#kb-doc-list' },
  { input: '#explorer-filter', list: '#explorer-tree' },
  { input: '#ws-filter', list: '#ws-tree' },
];

/** 对单个列表容器应用筛选 */
function applySideFilter(listSel, q) {
  const box = $(listSel);
  if (!box) return;
  const kw = (q || '').trim().toLowerCase();
  const rows = Array.from(box.querySelectorAll('.tree-row'));
  if (!kw) {
    rows.forEach((r) => { r.style.display = ''; });
    /* 树形容器（explorer）：整棵子树恢复显示 */
    box.querySelectorAll('.tree-node').forEach((n) => { n.style.display = ''; });
    return;
  }
  /* 树形容器（explorer / workspace）：命中的是**整个子树**的文本，
     所以按 .tree-node 粒度隐藏，避免留下空的父节点。
     判据：容器自身带 .tree 类，或内部存在 .tree-node。 */
  const treeMode = box.classList.contains('tree') || !!box.querySelector('.tree-node');
  if (treeMode) {
    box.querySelectorAll('.tree-node').forEach((n) => {
      const hit = (n.textContent || '').toLowerCase().indexOf(kw) >= 0;
      n.style.display = hit ? '' : 'none';
    });
    return;
  }
  rows.forEach((r) => {
    const hit = (r.textContent || '').toLowerCase().indexOf(kw) >= 0;
    r.style.display = hit ? '' : 'none';
  });
}

/** 重放所有筛选条件（列表刷新后调用） */
function replaySideFilters() {
  SIDE_FILTERS.forEach((f) => {
    const inp = $(f.input);
    if (inp) applySideFilter(f.list, inp.value);
  });
}

/** 绑定筛选框 */
function initSideFilters() {
  SIDE_FILTERS.forEach((f) => {
    const inp = $(f.input);
    if (!inp) return;
    inp.addEventListener('input', () => applySideFilter(f.list, inp.value));
  });
  /* 列表被重新渲染后（刷新 / 加载更多）重放筛选 */
  ['#tools-list', '#sessions-list', '#kb-doc-list', '#ws-tree'].forEach((sel) => {
    const box = $(sel);
    if (!box) return;
    if (typeof MutationObserver === 'undefined') return;
    const mo = new MutationObserver(() => replaySideFilters());
    mo.observe(box, { childList: true });
  });
}

/* ----------------------------------------------------------------------
   启动
   ---------------------------------------------------------------------- */
function boot() {
  initTheme();          /* 主题优先套用，避免刷新时的配色闪烁 */
  initResizers();
  initActivityBar();
  initMobilePanels();
  initSideFilters();
  /* ---- 抓取对话页模板（2026-11-24 新增）----
   * 对话页是**常驻页**，万一被误删（closeTab / 手动 remove），
   * ensureChatPage() 要能按模板把它**重建**回来，
   * 否则「新建会话」会变成彻底的空操作。
   * 必须在任何可能改动 DOM 的初始化**之前**抓取（此时是原始骨架）。
   */
  const chatPage = $('.page[data-page="pg-chat"]');
  if (chatPage) _chatPageTemplate = chatPage.outerHTML;
  restoreTabs();
  bind();
  if (typeof bindExtra === 'function') bindExtra();
  if (typeof bindKbAdmin === 'function') bindKbAdmin();
  if (typeof bindEnhancements === 'function') bindEnhancements();
  if (typeof tvInit === 'function') tvInit();
  if (typeof obsInit === 'function') obsInit();
  if (typeof applyDesktopMode === 'function') applyDesktopMode();
  /* ---- 渲染对话空状态（welcome）----
   * 真实故障（2026-11-24 修复）：welcome 此前只写在 index.html 的静态骨架里，
   * JS 从不重建 → 「新建会话」把 #chat 清空后**永远回不来**，
   * 用户只看到一行 12px 灰色提示，视觉上就是「一片空白」。
   * 现在 WELCOME_HTML 是唯一事实来源，启动时渲染一次。
   */
  renderWelcome($('#chat'));
  refreshHealth();
  refreshTools();
  refreshSessions();
  refreshTemplates();
  refreshTasks();
  if (typeof refreshCustomTools === 'function') refreshCustomTools();
  if (typeof refreshKbDocuments === 'function') refreshKbDocuments();

  setInterval(refreshHealth, 10000);          // 实时轮询 /health
  // 任务列表轮询：只保留**一个**定时器句柄（此前重复注册了两个 3s 轮询，
  // 导致请求量翻倍，且其中一个句柄无法被 state.tasksTimer 回收）。
  state.tasksTimer = setInterval(refreshTasks, 3000);
}

document.addEventListener('DOMContentLoaded', boot);
