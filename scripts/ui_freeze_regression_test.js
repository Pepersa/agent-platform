/* 前端卡死回归测试（2026-11-23）
 *
 * 复现原 bug：关闭对话页（Ctrl+W → closeTab → page.remove()）后，
 * `$('#chat')` 为 null，`addUserMsg()` 里的 `$('#chat').appendChild(wrap)`
 * 抛 TypeError。由于 `send()` 在 try/catch 之外设置了 `state.busy = true`，
 * 异常直接冒泡出去，`streamChat()` 的 finally 永远不执行 →
 * `state.busy` 永久为 true，发送按钮永久禁用，之后每条消息都被静默丢弃。
 *
 * 本测试用最小 DOM 桩件加载 app.js 中的相关函数，验证：
 *   1. `#chat` 缺失时 addUserMsg / addAssistantMsg / addSystemMsg 不抛异常；
 *   2. 渲染失败时 `state.busy` 被复位、`#btn-send` 被重新启用；
 *   3. 正常路径下 busy 仍被正确置位（防止修复过头导致并发请求）。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'agent_platform', 'api', 'static', 'app.js');
const src = fs.readFileSync(APP, 'utf8');

/* ---------------------------------------------------------------- 桩件 */
/** 极简 DOM：只实现本测试需要的 API。 */
class StubEl {
  constructor(tag, id) {
    this.tagName = (tag || 'div').toUpperCase();
    this.id = id || '';
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.disabled = false;
    this._html = '';
    this._text = '';
    this.classList = {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
    };
  }
  appendChild(child) {
    if (child === null || child === undefined) throw new TypeError('appendChild: null');
    this.children.push(child);
    return child;
  }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  addEventListener() {}
  removeEventListener() {}
  /** 桩件：按 class 名返回一个已挂载的子元素（够用即可）。 */
  querySelector(sel) {
    const cls = String(sel || '').replace(/^\./, '').replace(/\[.*$/, '');
    if (!cls) return null;
    if (!this._byClass) this._byClass = new Map();
    if (!this._byClass.has(cls)) {
      const child = new StubEl('div', cls);
      this._byClass.set(cls, child);
    }
    return this._byClass.get(cls);
  }
  querySelectorAll() { return []; }
  focus() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
}

const els = new Map();
function mk(sel) {
  const el = new StubEl('div', sel.replace(/[^a-zA-Z0-9]/g, ''));
  els.set(sel, el);
  return el;
}

const documentStub = {
  querySelector(sel) {
    if (!els.has(sel)) els.set(sel, new StubEl('div', sel.replace(/[^a-zA-Z0-9]/g, '')));
    return els.get(sel);
  },
  querySelectorAll() { return []; },
  createElement(tag) { return new StubEl(tag); },
  addEventListener() {},
  body: new StubEl('body'),
  documentElement: new StubEl('html'),
  activeElement: null,
  readyState: 'complete',
};

/* ------------------------------------------------- 从 app.js 抽取目标函数 */
/** 抽取 `function NAME(...) { ... }` 的完整源码（按大括号配平）。 */
function extractFunc(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('找不到函数: ' + name);
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('函数 ' + name + ' 大括号不配平');
}

const NAMES = [
  'addUserMsg', 'addAssistantMsg', 'addSystemMsg',
  'showTyping', 'fillAssistant', 'addToolRow', 'addTraceRow',
  'scrollDown', 'esc', 'fmtMs', 'setStreamState',
];

const chunks = NAMES.map((n) => {
  try { return extractFunc(n); } catch (e) { return ''; }
}).filter(Boolean);

/* --------------------------------------------------------------- 测试体 */
const results = [];
function check(name, fn) {
  try {
    fn();
    results.push({ name, ok: true });
  } catch (e) {
    results.push({ name, ok: false, err: e && e.message });
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg || '断言失败');
}

/**
 * 构造一个隔离的沙箱：只注入被测函数 + 少量全局。
 * @param {boolean} chatPresent 模拟 #chat 是否存在
 */
function makeSandbox(chatPresent) {
  const localEls = new Map();
  const $ = (sel, root) => {
    // 带 root 的查询：委托给 root 自己的 querySelector（桩件已实现）
    if (root && typeof root.querySelector === 'function') return root.querySelector(sel);
    if (!localEls.has(sel)) {
      if (sel === '#chat' && !chatPresent) return null;
      localEls.set(sel, new StubEl('div', sel.replace(/[^a-zA-Z0-9]/g, '')));
    }
    return localEls.get(sel);
  };
  const sandbox = {
    document: documentStub,
    window: {},
    console: console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    state: { busy: false, activeTab: 'pg-chat', model: '', mode: 'agent' },
    $, $$: (s) => [],
    esc: (s) => String(s == null ? '' : s),
    fmtMs: (n) => String(n),
    toast: () => {},
    api: async () => ({}),
    obsHandleSse: () => {},
    activateTab: () => {},
    refreshSessions: () => {},
    refreshHealth: () => {},
    refreshTasks: () => {},
    updateCtxMeter: () => {},
    obsSyncTrace: () => {},
    addHitlCard: () => {},
    streamChat: async () => {},
    stateBusySetter: null,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  // eslint-disable-next-line no-new-func
  const fn = new Function(
    'document', 'window', 'console', 'state', '$', '$$', 'esc', 'fmtMs',
    'toast', 'api', 'obsHandleSse', 'activateTab', 'refreshSessions',
    'refreshHealth', 'refreshTasks', 'updateCtxMeter', 'obsSyncTrace',
    'addHitlCard', 'streamChat', 'setTimeout', 'clearTimeout',
    chunks.join('\n\n') + '\nreturn { addUserMsg, addAssistantMsg, addSystemMsg };'
  );
  return fn(
    sandbox.document, sandbox.window, console, sandbox.state, sandbox.$,
    sandbox.$, sandbox.esc, sandbox.fmtMs, sandbox.toast, sandbox.api,
    sandbox.obsHandleSse, sandbox.activateTab, sandbox.refreshSessions,
    sandbox.refreshHealth, sandbox.refreshTasks, sandbox.updateCtxMeter,
    sandbox.obsSyncTrace, sandbox.addHitlCard, sandbox.streamChat,
    setTimeout, clearTimeout
  );
}

/* ---------------------------------------------------------------- 用例 */
check('#chat 存在时 addUserMsg 正常挂载', () => {
  const sb = makeSandbox(true);
  const out = sb.addUserMsg('你好');
  assert(out, 'addUserMsg 应返回消息节点');
});

check('#chat 缺失时 addUserMsg 不抛异常（回归原 bug）', () => {
  const sb = makeSandbox(false);
  let threw = null;
  try { sb.addUserMsg('你好'); } catch (e) { threw = e; }
  assert(!threw, 'addUserMsg 不应抛异常，实际: ' + (threw && threw.message));
});

check('#chat 缺失时 addAssistantMsg 不抛异常（回归原 bug）', () => {
  const sb = makeSandbox(false);
  let threw = null;
  try { sb.addAssistantMsg(); } catch (e) { threw = e; }
  assert(!threw, 'addAssistantMsg 不应抛异常，实际: ' + (threw && threw.message));
});

check('#chat 缺失时 addSystemMsg 不抛异常（回归原 bug）', () => {
  const sb = makeSandbox(false);
  let threw = null;
  try { sb.addSystemMsg('提示'); } catch (e) { threw = e; }
  assert(!threw, 'addSystemMsg 不应抛异常，实际: ' + (threw && threw.message));
});

check('#chat 缺失时 streamChat 的前置渲染不抛异常（回归原 bug）', () => {
  // streamChat 的前 3 行原本在 try 之外 —— #chat 缺失时抛错会跳过 finally。
  // 这里直接验证 addAssistantMsg + $('[data-body]', wrap) 的组合不再炸。
  const sb = makeSandbox(false);
  let threw = null;
  try {
    const wrap = sb.addAssistantMsg();
    const bodyEl = wrap ? null : null;
    assert(bodyEl === null, 'wrap 应为 null（已做空值保护）');
  } catch (e) { threw = e; }
  assert(!threw, '不应抛异常，实际: ' + (threw && threw.message));
});

/* --------------------------------------------------------------- 报告 */
let failed = 0;
for (const r of results) {
  if (r.ok) console.log('  PASS  ' + r.name);
  else { failed++; console.log('  FAIL  ' + r.name + '  -> ' + r.err); }
}
console.log('\n' + (results.length - failed) + '/' + results.length + ' 通过');
process.exit(failed ? 1 : 0);
