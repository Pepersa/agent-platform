/* ==========================================================================
   工作区（Workspace）—— 文件树 / 代码编辑器 / 搜索
   · 纯原生实现：零第三方库、零 CDN、零构建工具
   · 编辑器：<textarea>（透明文字 + 可见光标）叠在 <pre><code>（高亮 HTML）之上
   · 语法高亮：自写轻量 tokenizer（Python / JS / TS / JSON / YAML / HTML / CSS /
     Markdown / Shell），Python 规则最完整
   ========================================================================== */
'use strict';

/* ----------------------------------------------------------------------
   状态
   ---------------------------------------------------------------------- */
const WS = {
  root: '',
  expanded: new Set(),          // 已展开的目录路径
  children: new Map(),          // path -> entries[]
  dirty: new Set(),             // 有未保存修改的 tab id
  files: new Map(),             // path -> {path, content, encoding, language, mtime, size}
  editors: new Map(),           // path -> {path, language, mtime, original}（按路径分桶）
  activePath: '',               // 当前激活文件页的路径
  searchTimer: null,
  lastSearch: '',
};

/* ----------------------------------------------------------------------
   基础工具
   ---------------------------------------------------------------------- */
function wsMsg(text, kind) {
  const el = $('#ws-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'view-msg sm ' + (kind || 'muted');
}

function wsIcon(ext, type) {
  if (type === 'dir') {
    return '<svg class="ti" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"'
      + ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M2.4 12.6V4.4a1 1 0 0 1 1-1h2.3L7.2 4.6h5.4a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H3.4a1 1 0 0 1-1-1z"/></svg>';
  }
  return '<svg class="ti" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"'
    + ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M3.5 2.6h5.7L12 5.4v8a1.2 1.2 0 0 1-1.2 1.2H3.5a1.2 1.2 0 0 1-1.2-1.2V3.8a1.2 1.2 0 0 1 1.2-1.2z"/>'
    + '<path d="M9.1 2.7v2.8h2.8"/></svg>';
}

function wsFmtSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function wsFmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

/* ======================================================================
   语法高亮 tokenizer
   ----------------------------------------------------------------------
   设计：**逐行**扫描（不做跨行状态机），因此实现极简且不会因
   未闭合字符串把整篇文件染色。规则用「一条正则 + 优先级」实现：
   先把行首的注释 / 字符串 / 关键字等用正则切分，再把剩余部分当普通文本。

   优先级（从高到低）：
     1) 注释（Python 的 # 、JS 的双斜杠）
     2) 字符串（单引号 / 双引号 / 反引号）
     3) 数字
     4) 关键字 / 内置类型
     5) 函数调用标识符
     6) 普通标识符 / 文本
   ====================================================================== */
const WS_TOKENS = {
  python: {
    lineComment: /#.*$/,
    keywords: ['False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
      'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
      'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is', 'lambda',
      'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try', 'while',
      'with', 'yield', 'match', 'case'],
    builtins: ['abs', 'all', 'any', 'bool', 'bytes', 'dict', 'dir', 'enumerate',
      'filter', 'float', 'format', 'frozenset', 'getattr', 'hasattr', 'int',
      'isinstance', 'len', 'list', 'map', 'max', 'min', 'next', 'object', 'open',
      'print', 'range', 'repr', 'reversed', 'round', 'set', 'setattr', 'sorted',
      'str', 'sum', 'super', 'tuple', 'type', 'zip', 'self', 'cls', '__init__'],
    // 常量（True / False / None）—— 单独一类，颜色与关键字区分
    constants: ['True', 'False', 'None'],
  },
  javascript: {
    lineComment: /\/\/.*$/,
    blockComment: /\/\*[\s\S]*?\*\//,
    keywords: ['async', 'await', 'break', 'case', 'catch', 'class', 'const',
      'continue', 'debugger', 'default', 'delete', 'do', 'else', 'export',
      'extends', 'finally', 'for', 'function', 'if', 'import', 'in', 'instanceof',
      'let', 'new', 'of', 'return', 'static', 'super', 'switch', 'this', 'throw',
      'try', 'typeof', 'var', 'void', 'while', 'yield'],
    builtins: ['Array', 'Boolean', 'Date', 'Error', 'JSON', 'Map', 'Math', 'Number',
      'Object', 'Promise', 'Set', 'String', 'console', 'document', 'false', 'null',
      'true', 'undefined', 'window'],
  },
  json: {
    lineComment: null,
    keywords: ['true', 'false', 'null'],
    builtins: [],
  },
  yaml: {
    lineComment: /#.*$/,
    keywords: ['true', 'false', 'null', 'yes', 'no', 'on', 'off'],
    builtins: [],
  },
  html: { lineComment: null, keywords: [], builtins: [] },
  css: {
    lineComment: null,
    blockComment: /\/\*[\s\S]*?\*\//,
    keywords: ['important', 'inherit', 'initial', 'unset', 'none', 'auto'],
    builtins: [],
  },
  markdown: { lineComment: null, keywords: [], builtins: [] },
  shell: {
    lineComment: /#.*$/,
    keywords: ['if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'do', 'done',
      'case', 'esac', 'function', 'return', 'export', 'local', 'source', 'echo',
      'cd', 'ls', 'cat', 'set', 'unset'],
    builtins: [],
  },
};

/* HTML / CSS / Markdown 走**块级**规则（正则整块匹配），不走关键词切分 */
const WS_BLOCK_RULES = {
  html: [
    { re: /<!--[\s\S]*?-->/g, cls: 'tok-comment' },
    { re: /<\/?[A-Za-z][\w:-]*/g, cls: 'tok-tag' },
    { re: /"[^"]*"|'[^']*'/g, cls: 'tok-string' },
  ],
  css: [
    { re: /\/\*[\s\S]*?\*\//g, cls: 'tok-comment' },
    { re: /"[^"]*"|'[^']*'/g, cls: 'tok-string' },
    { re: /#[0-9a-fA-F]{3,8}\b/g, cls: 'tok-number' },
    { re: /[.#]?[A-Za-z][\w-]*(?=\s*\{)/g, cls: 'tok-selector' },
    { re: /[a-z-]+(?=\s*:)/g, cls: 'tok-property' },
    { re: /\b\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|s|ms|deg)?\b/g, cls: 'tok-number' },
  ],
  markdown: [
    { re: /^#{1,6}[^\n]*/gm, cls: 'tok-heading' },
    { re: /`[^`\n]+`/g, cls: 'tok-string' },
    { re: /\*\*[^*\n]+\*\*/g, cls: 'tok-bold' },
    { re: /^\s*[-*+]\s/gm, cls: 'tok-keyword' },
    { re: /\[[^\]\n]*\]\([^)\n]*\)/g, cls: 'tok-link' },
  ],
  json: [
    { re: /"(?:[^"\\]|\\.)*"(?=\s*:)/g, cls: 'tok-property' },
    { re: /"(?:[^"\\]|\\.)*"/g, cls: 'tok-string' },
    { re: /\b-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b/g, cls: 'tok-number' },
    { re: /\b(?:true|false|null)\b/g, cls: 'tok-keyword' },
  ],
};

/** 把一行文本切成 token 序列（返回 {cls, text} 列表） */
function wsTokenizeLine(text, lang) {
  const out = [];
  const rules = WS_TOKENS[lang] || WS_TOKENS.python;
  const blockRules = WS_BLOCK_RULES[lang];
  let rest = text;

  const pushPlain = (s) => { if (s) out.push({ cls: '', text: s }); };

  // ---- 块级语言：正则整块替换 ----
  if (blockRules) {
    let idx = 0;
    let matched = false;
    const marks = [];
    blockRules.forEach((rule) => {
      rule.re.lastIndex = 0;
      let m;
      while ((m = rule.re.exec(text)) !== null) {
        if (m[0] === '') { rule.re.lastIndex++; continue; }
        marks.push({ start: m.index, end: m.index + m[0].length, cls: rule.cls });
      }
    });
    marks.sort((a, b) => a.start - b.start);
    let last = 0;
    marks.forEach((mk) => {
      if (mk.start < last) return;
      pushPlain(text.slice(last, mk.start));
      out.push({ cls: mk.cls, text: text.slice(mk.start, mk.end) });
      last = mk.end;
      matched = true;
    });
    if (matched) {
      pushPlain(text.slice(last));
      return out;
    }
  }

  // ---- 行级语言：注释 → 字符串 → 数字 → 关键字 → 标识符 ----
  const cmtRe = rules.lineComment;
  let commentAt = -1;
  if (cmtRe) {
    const m = cmtRe.exec(rest);
    if (m) commentAt = m.index;
  }
  const body = commentAt >= 0 ? rest.slice(0, commentAt) : rest;
  const tail = commentAt >= 0 ? rest.slice(commentAt) : '';

  const kw = new Set(rules.keywords || []);
  const bi = new Set(rules.builtins || []);
  const co = new Set(rules.constants || []);

  // 扫描 body：字符串 / 数字 / 标识符
  let i = 0;
  let plain = '';
  let prevWord = '';
  const flushPlain = () => { pushPlain(plain); plain = ''; };
  while (i < body.length) {
    const ch = body[i];
    if (ch === '"' || ch === "'" || ch === '`') {
      // 字符串：找到配对的结束引号（不跨行）
      let j = i + 1;
      while (j < body.length) {
        if (body[j] === '\\') { j += 2; continue; }
        if (body[j] === ch) { j++; break; }
        j++;
      }
      flushPlain();
      out.push({ cls: 'tok-string', text: body.slice(i, Math.min(j, body.length)) });
      i = Math.min(j, body.length);
      prevWord = '';
      continue;
    }
    if (/[0-9]/.test(ch) && !/[A-Za-z_]/.test(body[i - 1] || '')) {
      let j = i;
      while (j < body.length && /[0-9a-fA-FxXoObB._]/.test(body[j])) j++;
      flushPlain();
      out.push({ cls: 'tok-number', text: body.slice(i, j) });
      i = j;
      prevWord = '';
      continue;
    }
    if (/[A-Za-z_]/.test(ch)) {
      let j = i;
      while (j < body.length && /[A-Za-z0-9_]/.test(body[j])) j++;
      const word = body.slice(i, j);
      flushPlain();
      let cls = '';
      if (co.has(word)) cls = 'tok-const';
      else if (kw.has(word)) cls = 'tok-keyword';
      else if (bi.has(word)) cls = 'tok-builtin';
      else if (prevWord === 'class') cls = 'tok-class';
      else if (body[j] === '(') cls = 'tok-func';
      else if (/^[A-Z][A-Za-z0-9_]*$/.test(word)) cls = 'tok-class';
      out.push({ cls: cls, text: word });
      prevWord = word;
      i = j;
      continue;
    }
    plain += ch;
    i++;
  }
  flushPlain();
  if (tail) out.push({ cls: 'tok-comment', text: tail });
  return out;
}

/** 生成高亮 HTML（逐行） */
function wsHighlight(code, lang) {
  const lines = String(code == null ? '' : code).split('\n');
  const html = [];
  for (let i = 0; i < lines.length; i++) {
    const toks = wsTokenizeLine(lines[i], lang);
    let inner = '';
    toks.forEach((t) => {
      inner += t.cls
        ? '<span class="' + t.cls + '">' + esc(t.text) + '</span>'
        : esc(t.text);
    });
    html.push(inner || '');
  }
  return html;
}

/* ======================================================================
   文件树
   ====================================================================== */
async function wsLoadRoot() {
  try {
    const info = await api('/workspace/info');
    WS.root = info.root || '';
    const el = $('#ws-root-line');
    if (el) {
      el.textContent = WS.root;
      el.title = '工作区根目录：' + WS.root;
    }
  } catch (err) {
    wsMsg('工作区不可用：' + err.message, 'err');
  }
}

/** 渲染某个目录节点的子项 */
async function wsRenderDir(path, container) {
  let data;
  try {
    data = await api('/workspace/tree?path=' + encodeURIComponent(path || '')
      + '&depth=1');
  } catch (err) {
    container.innerHTML = '<div class="muted sm">加载失败：' + esc(err.message) + '</div>';
    return;
  }
  const entries = data.entries || [];
  WS.children.set(path || '', entries);
  container.innerHTML = '';
  if (!entries.length) {
    container.innerHTML = '<div class="muted sm">（空目录）</div>';
    return;
  }
  entries.forEach((e) => {
    container.appendChild(wsTreeNode(e));
  });
}

function wsTreeNode(entry) {
  const row = document.createElement('div');
  row.className = 'tree-node';
  const isDir = entry.type === 'dir';
  row.setAttribute('data-path', entry.path);

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'tree-row tree-toggle' + (isDir ? '' : ' leaf');
  btn.dataset.path = entry.path;
  btn.dataset.type = entry.type;

  const caret = isDir
    ? '<svg class="tw" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"'
      + ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M6 4l4 4-4 4"/></svg>'
    : '<span class="tw ws-tw-space"></span>';
  const meta = isDir ? '' :
    '<span class="ws-meta">' + esc(wsFmtSize(entry.size)) + ' · ' + esc(wsFmtTime(entry.mtime)) + '</span>';

  btn.innerHTML = caret + wsIcon(entry.ext, entry.type)
    + '<span class="tree-label">' + esc(entry.name) + '</span>' + meta;

  // 行内 hover 操作
  if (isDir) {
    const acts = document.createElement('span');
    acts.className = 'ws-row-acts';
    acts.innerHTML =
      '<button type="button" class="icon-btn ws-act" data-act="new" title="在此目录新建文件" aria-label="新建文件">'
      + '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor"'
      + ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M8 3.4v9.2M3.4 8h9.2"/></svg></button>';
    btn.appendChild(acts);
  } else {
    const acts = document.createElement('span');
    acts.className = 'ws-row-acts';
    acts.innerHTML =
      '<button type="button" class="icon-btn ws-act" data-act="rename" title="重命名" aria-label="重命名">'
      + '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor"'
      + ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M11.2 2.8 13.2 4.8 5.6 12.4 2.8 13.2 3.6 10.4z"/></svg></button>'
      + '<button type="button" class="icon-btn ws-act" data-act="delete" title="删除" aria-label="删除">'
      + '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor"'
      + ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M3 4.4h10M6.4 4.4V3h3.2v1.4M4.4 4.4l.6 8.2h6l.6-8.2"/></svg></button>';
    btn.appendChild(acts);
  }

  row.appendChild(btn);

  if (isDir) {
    const kids = document.createElement('div');
    kids.className = 'tree-children';
    kids.hidden = !WS.expanded.has(entry.path);
    // **展开状态的双写**：hidden 属性是「唯一事实来源」（CSS 按 [hidden] 折叠），
    // .open 类只驱动箭头旋转 / 图标高亮等装饰效果。
    // 回归点：此前只切 hidden、不加 .open，导致 CSS 的
    // `.tree-node:not(.open) > .tree-children { display: none }` 恒成立，
    // 目录展开后子节点**全部不可见**（用户报障「文件树消失」）。
    if (!kids.hidden) row.classList.add('open');
    row.appendChild(kids);
    btn.addEventListener('click', (e) => {
      if (e.target.closest('.ws-act')) return;
      wsToggleDir(entry.path, kids, row);
    });
  } else {
    btn.addEventListener('click', (e) => {
      if (e.target.closest('.ws-act')) return;
      wsOpenFile(entry.path, entry.name);
    });
  }
  return row;
}

async function wsToggleDir(path, kidsEl, nodeEl) {
   if (WS.expanded.has(path)) {
    WS.expanded.delete(path);
    kidsEl.hidden = true;
    if (nodeEl) nodeEl.classList.remove('open');
    return;
  }
  WS.expanded.add(path);
  kidsEl.hidden = false;
  if (nodeEl) nodeEl.classList.add('open');
  if (!WS.children.has(path)) {
    kidsEl.innerHTML = '<div class="muted sm">加载中…</div>';
    await wsRenderDir(path, kidsEl);
  }
}

async function wsRefreshTree() {
  const tree = $('#ws-tree');
  if (!tree) return;
  WS.children.clear();
  tree.innerHTML = '';
  const rootKids = document.createElement('div');
  rootKids.className = 'tree-children';
  rootKids.hidden = false;
  tree.appendChild(rootKids);
  await wsRenderDir('', rootKids);
}

/* ----------------------------------------------------------------------
   文件行内操作（新建 / 重命名 / 删除）
   ---------------------------------------------------------------------- */
function wsParentPath(path) {
  if (!path) return '';
  const i = path.lastIndexOf('/');
  return i < 0 ? '' : path.slice(0, i);
}

async function wsNewFile(dirPath) {
  const name = window.prompt('新建文件（相对 ' + (dirPath || '工作区根') + '）', 'new_file.py');
  if (!name) return;
  const path = dirPath ? dirPath + '/' + name : name;
  wsMsg('正在新建 ' + path + ' …');
  try {
    const res = await api('/workspace/file', {
      method: 'POST',
      body: JSON.stringify({ path: path, content: '', note: '前端新建文件' }),
    });
    await wsApplyChange(res);
    wsMsg('已新建 ' + path, 'ok');
    await wsRefreshTree();
    wsOpenFile(path, name);
  } catch (err) {
    wsMsg('新建失败：' + err.message, 'err');
  }
}

async function wsRenameFile(path) {
  const oldName = path.split('/').pop();
  const name = window.prompt('重命名文件', oldName);
  if (!name || name === oldName) return;
  const dir = wsParentPath(path);
  const newPath = dir ? dir + '/' + name : name;
  try {
    const data = await api('/workspace/file?path=' + encodeURIComponent(path));
    const res = await api('/workspace/file', {
      method: 'POST',
      body: JSON.stringify({
        path: newPath, content: data.content,
        expected_mtime: data.mtime, note: '重命名自 ' + path,
      }),
    });
    await wsApplyChange(res);
    wsMsg('已重命名为 ' + newPath, 'ok');
    await wsRefreshTree();
  } catch (err) {
    wsMsg('重命名失败：' + err.message, 'err');
  }
}

async function wsDeleteFile(path) {
  if (!window.confirm('确认删除 ' + path + ' ？\n（删除会走审批流程，落盘后可通过撤销回滚）')) return;
  wsMsg('正在删除 ' + path + ' …');
  try {
    const res = await api('/workspace/file', {
      method: 'POST',
      body: JSON.stringify({ path: path, content: '', note: '删除文件 ' + path }),
    });
    await wsApplyChange(res);
    wsMsg('已删除 ' + path, 'ok');
    await wsRefreshTree();
  } catch (err) {
    wsMsg('删除失败：' + err.message, 'err');
  }
}

/* ----------------------------------------------------------------------
   改动应用（diff 确认 → 落盘）
   ---------------------------------------------------------------------- */
function wsDiffHtml(diff) {
  if (!diff || !diff.lines || !diff.lines.length) return '';
  const rows = diff.lines.map((r) => {
    if (r.op === 'skip') {
      return '<div class="ws-diff-row ws-diff-skip" data-count="' + esc(r.count || 0) + '">'
        + esc(r.label || ('展开 ' + (r.count || 0) + ' 行')) + '</div>';
    }
    const cls = r.op === 'add' ? 'ws-diff-add' : (r.op === 'del' ? 'ws-diff-del' : 'ws-diff-ctx');
    const sign = r.op === 'add' ? '+' : (r.op === 'del' ? '-' : ' ');
    const num = r.op === 'add' ? (r.new || '') : (r.old || r.new || '');
    return '<div class="ws-diff-row ' + cls + '">'
      + '<span class="ws-diff-num">' + esc(num) + '</span>'
      + '<span class="ws-diff-sign">' + sign + '</span>'
      + '<span class="ws-diff-text">' + (esc(r.text || '').replace(/ /g, '&nbsp;')) + '</span>'
      + '</div>';
  }).join('');
  return '<div class="ws-diff">'
    + '<div class="ws-diff-head">'
    + '<span class="ws-diff-path">' + esc(diff.path || '') + '</span>'
    + '<span class="ws-diff-stat"><b class="add">+' + (diff.added || 0) + '</b>'
    + ' <b class="del">-' + (diff.removed || 0) + '</b></span>'
    + '</div>'
    + '<div class="ws-diff-body">' + rows + '</div></div>';
}

/** 展示 diff 确认对话框，用户确认后才落盘 */
function wsConfirmDiff(change) {
  return new Promise((resolve) => {
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.id = 'ws-diff-mask';
    mask.innerHTML =
      '<div class="modal modal-ws" role="dialog" aria-modal="true" aria-labelledby="ws-diff-title">'
      + '<header class="modal-head">'
      + '<h3 id="ws-diff-title">确认代码改动</h3>'
      + '<button id="ws-diff-close" class="modal-x" type="button" title="关闭" aria-label="关闭">'
      + '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"'
      + ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>'
      + '</button></header>'
      + '<div class="modal-body">' + wsDiffHtml(change.diff) + '</div>'
      + '<footer class="modal-foot">'
      + '<span id="ws-diff-msg" class="muted sm">确认后才会写入磁盘；写操作可撤销。</span>'
      + '<div class="row">'
      + '<button id="ws-diff-cancel" class="btn btn-ghost sm" type="button">取消</button>'
      + '<button id="ws-diff-ok" class="btn btn-primary sm" type="button">确认写入</button>'
      + '</div></footer></div>';
    document.body.appendChild(mask);

    const close = (ok) => {
      if (mask.parentNode) mask.parentNode.removeChild(mask);
      resolve(ok);
    };
    mask.querySelector('#ws-diff-ok').addEventListener('click', () => close(true));
    mask.querySelector('#ws-diff-cancel').addEventListener('click', () => close(false));
    mask.querySelector('#ws-diff-close').addEventListener('click', () => close(false));
    // 折叠行 → 展开（前端展开被跳过的行需要重新取完整 diff，这里做简单提示）
    mask.querySelectorAll('.ws-diff-skip').forEach((el) => {
      el.addEventListener('click', () => {
        el.classList.toggle('expanded');
        el.textContent = el.classList.contains('expanded')
          ? '（完整 diff 请在审批卡片中查看）' : el.dataset.count
            ? '展开 ' + el.dataset.count + ' 行' : el.textContent;
      });
    });
  });
}

/** 应用一次改动：先 diff 确认，再 POST /workspace/apply */
async function wsApplyChange(change) {
  const ok = await wsConfirmDiff(change);
  if (!ok) {
    try { await api('/workspace/change/' + encodeURIComponent(change.change_id), { method: 'DELETE' }); } catch (e) { /* 忽略 */ }
    throw new Error('用户取消了本次改动');
  }
  const res = await api('/workspace/apply', {
    method: 'POST',
    body: JSON.stringify({ change_id: change.change_id, approved: true, actor: 'operator' }),
  });
  return res;
}

/* ======================================================================
   编辑器
   ====================================================================== */
function wsEditorPage() {
  return '<div class="ws-editor" id="ws-editor">'
    + '<div class="ws-editor-gutter" id="ws-gutter"></div>'
    + '<div class="ws-editor-main">'
    + '<pre class="ws-editor-pre" id="ws-pre"><code id="ws-code"></code></pre>'
    + '<textarea class="ws-editor-input" id="ws-input" spellcheck="false"'
    + ' autocomplete="off" autocapitalize="off" wrap="off"></textarea>'
    + '</div>'
    + '<div class="ws-find" id="ws-find" hidden>'
    + '<input id="ws-find-input" class="input" type="text" placeholder="查找" />'
    + '<span id="ws-find-count" class="muted sm"></span>'
    + '<button id="ws-find-close" class="icon-btn" type="button" title="关闭查找" aria-label="关闭查找">'
    + '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor"'
    + ' stroke-width="1.6" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg></button>'
    + '</div></div>';
}

/** 取某个路径的编辑器状态（**按路径分桶**）。
 *
 *  **回归点（2026-11-22 修复）**：此前 WS.editor 是**单个全局单例**，
 *  打开第二个文件时会把第一个文件的 path / original / mtime **覆盖掉**：
 *  于是回到第一个 tab 时，保存会以「另一个文件」的 expected_mtime 提交，
 *  触发 409 WS_FILE_CHANGED，或者把 A 的内容写进 B。
 *  现改为按 path 存多份状态；WS.editor 只作为「当前激活文件」的快捷视图。
 */
function wsEditorState(path) {
  const p = path || (WS.activePath || '');
  if (!p) {
    if (!WS.editor) {
      WS.editor = { path: '', language: 'text', mtime: 0, original: '' };
    }
    return WS.editor;
  }
  if (!WS.editors) WS.editors = new Map();
  let st = WS.editors.get(p);
  if (!st) {
    st = { path: p, language: 'text', mtime: 0, original: '' };
    WS.editors.set(p, st);
  }
  WS.activePath = p;
  return st;
}

/** 取当前激活的**文件页**内的元素。
 *  回归点（2026-11-22 修复）：此前各函数用 `$('#ws-input')` 这类全局选择器，
 *  多文件 tab 同时存在时只会命中**第一个**同名元素（可能属于未激活的 tab），
 *  导致高亮层 / 行号栏渲染到错误的 tab，或滚动不同步 → 「代码看不见」。
 *  统一走本helper，作用域限定在激活页内。 */
function wsActivePage() {
  return document.querySelector('.page.ws-page.active');
}

/** 取当前激活文件页所对应的**文件路径**（用于按路径分桶的编辑器状态）。
 *  通过 tab id（pg-file-<sanitized path>）反查原始 path。 */
function wsActivePath() {
  const page = wsActivePage();
  if (!page) return WS.activePath || '';
  const id = page.dataset.page || '';
  const tab = state.tabs.find((t) => t.id === id);
  if (tab && tab.data && tab.data.path) return tab.data.path;
  return WS.activePath || '';
}

/** 渲染高亮 + 行号 */
function wsRenderEditor() {
  const page = wsActivePage();
  const st = wsEditorState(wsActivePath());
  const input = page ? page.querySelector('#ws-input') : null;
  const code = page ? page.querySelector('#ws-code') : null;
  const gutter = page ? page.querySelector('#ws-gutter') : null;
  if (!input || !code || !gutter) return;
  const text = input.value;
  const lines = wsHighlight(text, st.language);
  code.innerHTML = lines.join('\n');
  const n = Math.max(1, lines.length);
  let g = '';
  for (let i = 1; i <= n; i++) g += '<div class="ws-ln" data-line="' + i + '">' + i + '</div>';
  gutter.innerHTML = g;
  wsSyncScroll();
}

/** textarea 与 pre 的滚动同步。
 *  **回归点（2026-11-22 修复）**：此前用 `$('#ws-input')`（全局 querySelector），
 *  多个文件 tab 同时打开时只会拿到**第一个** #ws-input，
 *  于是新打开的 tab 滚动时高亮层不动 → 出现「代码看不见 / 空白」。
 *  现改为定位到**当前激活的文件页**内的叠层元素。 */
function wsSyncScroll() {
  const page = wsActivePage();
  const input = page ? page.querySelector('#ws-input') : null;
  const pre = page ? page.querySelector('#ws-pre') : null;
  const gutter = page ? page.querySelector('#ws-gutter') : null;
  if (!input || !pre || !gutter) return;
  pre.scrollTop = input.scrollTop;
  pre.scrollLeft = input.scrollLeft;
  gutter.scrollTop = input.scrollTop;
}

/** Tab / Shift+Tab 缩进；括号引号自动配对 */
function wsEditorKeydown(e) {
  const input = e.target;
  if (e.key === 'Tab') {
    e.preventDefault();
    const start = input.selectionStart;
    const end = input.selectionEnd;
    const value = input.value;
    if (e.shiftKey) {
      // 反缩进：删掉光标所在行行首的缩进
      const lineStart = value.lastIndexOf('\n', start - 1) + 1;
      const prefix = value.slice(lineStart, lineStart + 4);
      const m = prefix.match(/^[ \t]{1,4}/);
      if (m && start >= lineStart) {
        const cut = m[0].length;
        input.value = value.slice(0, lineStart) + value.slice(lineStart + cut);
        input.selectionStart = input.selectionEnd = Math.max(lineStart, start - cut);
      }
    } else {
      input.value = value.slice(0, start) + '    ' + value.slice(end);
      input.selectionStart = input.selectionEnd = start + 4;
    }
    wsOnInput();
    return;
  }
  // 括号 / 引号自动配对
  const pairs = { '(': ')', '[': ']', '{': '}', '"': '"', "'": "'", '`': '`' };
  if (pairs[e.key] && !e.ctrlKey && !e.metaKey) {
    const input2 = e.target;
    const start = input2.selectionStart;
    const end = input2.selectionEnd;
    if (start === end) {
      const close = pairs[e.key];
      const value = input2.value;
      const next = value[start] || '';
      // 右侧已配对 → 跳过
      if (next === close) {
        e.preventDefault();
        input2.value = value.slice(0, start) + close + value.slice(end);
        input2.selectionStart = input2.selectionEnd = start + 1;
        wsOnInput();
        return;
      }
      e.preventDefault();
      input2.value = value.slice(0, start) + e.key + close + value.slice(end);
      input2.selectionStart = input2.selectionEnd = start + 1;
      wsOnInput();
    }
  }
}

function wsOnInput() {
  const page = wsActivePage();
  const input = page ? page.querySelector('#ws-input') : null;
  const st = wsEditorState(wsActivePath());
  if (!input) return;
  wsRenderEditor();
  const dirty = input.value !== st.original;
  if (st.path) {
    if (dirty) WS.dirty.add(st.path); else WS.dirty.delete(st.path);
  }
  wsUpdateTabTitle();
}

function wsUpdateTabTitle() {
  const st = wsEditorState(wsActivePath());
  if (!st.path) return;
  const tab = state.tabs.find((t) => t.type === 'file' && t.data && t.data.path === st.path);
  if (!tab) return;
  const base = st.path.split('/').pop();
  tab.title = (WS.dirty.has(st.path) ? '● ' : '') + base;
  renderTabs();
}

/* ----------------------------------------------------------------------
   打开文件 → 新开 tab
   ---------------------------------------------------------------------- */
function wsFileTabId(path) {
  return 'pg-file-' + path.replace(/[^a-zA-Z0-9]/g, '_');
}

/** 给一个文件 tab 页面绑定编辑器事件（幂等） */
function wsBindEditorPage(page) {
  if (!page || page.dataset.wsBound === '1') return;
  const input = page.querySelector('#ws-input');
  if (!input) return;
  page.dataset.wsBound = '1';
  input.addEventListener('input', wsOnInput);
  input.addEventListener('scroll', wsSyncScroll);
  input.addEventListener('keydown', wsEditorKeydown);
  const gutter = page.querySelector('#ws-gutter');
  if (gutter) {
    gutter.addEventListener('click', (e) => {
      const ln = e.target.closest('.ws-ln');
      if (!ln) return;
      wsGotoLine(Number(ln.dataset.line || 1));
    });
  }
  const findBox = page.querySelector('#ws-find');
  const findClose = page.querySelector('#ws-find-close');
  const findInput = page.querySelector('#ws-find-input');
  if (findClose && findBox) {
    findClose.addEventListener('click', () => { findBox.hidden = true; });
  }
  if (findInput) {
    findInput.addEventListener('input', (e) => wsFindCount(e.target.value));
    findInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        wsFindNext(e.shiftKey ? -1 : 1);
      } else if (e.key === 'Escape') {
        if (findBox) findBox.hidden = true;
      }
    });
  }
}

async function wsOpenFile(path, name) {
  const id = wsFileTabId(path);
  let tab = state.tabs.find((t) => t.id === id);
  if (!tab) {
    tab = openTab('file', name || path.split('/').pop(), { path: path }, id);
  }
  activateTab(id);
  const page = document.querySelector('.page[data-page="' + id + '"]');
  if (!page) return;
  wsBindEditorPage(page);
  try {
    const data = await api('/workspace/file?path=' + encodeURIComponent(path));
    WS.files.set(path, data);
    const st = wsEditorState(path);
    st.path = path;
    st.language = data.language || 'text';
    st.mtime = data.mtime || 0;
    st.original = data.content || '';
    const input = page.querySelector('#ws-input');
    if (input) {
      input.value = st.original;
      input.scrollTop = 0;
      input.scrollLeft = 0;
    }
    wsRenderEditor();
    WS.dirty.delete(path);
    wsUpdateTabTitle();
    wsMsg('已打开 ' + path + '（' + (data.line_count || 0) + ' 行）', 'ok');
  } catch (err) {
    wsMsg('打开失败：' + err.message, 'err');
  }
}

/** 跳到指定行 */
function wsGotoLine(lineNo) {
  const page = wsActivePage();
  const input = page ? page.querySelector('#ws-input') : null;
  if (!input) return;
  const lines = input.value.split('\n');
  let pos = 0;
  for (let i = 0; i < Math.min(lineNo - 1, lines.length); i++) pos += lines[i].length + 1;
  input.focus();
  input.setSelectionRange(pos, pos + (lines[lineNo - 1] || '').length);
  const lineH = parseFloat(getComputedStyle(input).lineHeight) || 19;
  input.scrollTop = Math.max(0, (lineNo - 4) * lineH);
  wsSyncScroll();
}

/** 查找 */
function wsFindCount(q) {
  const page = wsActivePage();
  const el = page ? page.querySelector('#ws-find-count') : null;
  if (!el) return;
  const input = page ? page.querySelector('#ws-input') : null;
  if (!q) { el.textContent = ''; return; }
  const text = input ? input.value : '';
  const n = text.split(q).length - 1;
  el.textContent = n ? (n + ' 处匹配') : '无匹配';
}

function wsFindNext(dir) {
  const page = wsActivePage();
  const input = page ? page.querySelector('#ws-input') : null;
  const q = page ? page.querySelector('#ws-find-input') : null;
  if (!input || !q || !q.value) return;
  const text = input.value;
  const needle = q.value;
  let idx;
  if (dir < 0) {
    idx = text.lastIndexOf(needle, Math.max(0, input.selectionStart - 1));
    if (idx < 0) idx = text.lastIndexOf(needle);
  } else {
    idx = text.indexOf(needle, input.selectionEnd);
    if (idx < 0) idx = text.indexOf(needle);
  }
  if (idx < 0) { wsFindCount(needle); return; }
  input.focus();
  input.setSelectionRange(idx, idx + needle.length);
  wsFindCount(needle);
}

/* ----------------------------------------------------------------------
   保存（走审批流程）
   ---------------------------------------------------------------------- */
async function wsSave() {
  const page = wsActivePage();
  const input = page ? page.querySelector('#ws-input') : null;
  const st = wsEditorState(wsActivePath());
  if (!st.path || !input) return;
  if (input.value === st.original) {
    wsMsg('没有需要保存的修改', 'ok');
    return;
  }
  wsMsg('正在生成改动…');
  let res;
  try {
    res = await api('/workspace/file', {
      method: 'POST',
      body: JSON.stringify({
        path: st.path,
        content: input.value,
        expected_mtime: st.mtime,
        note: '工作台手工编辑',
      }),
    });
  } catch (err) {
    wsMsg('保存失败：' + err.message, 'err');
    return;
  }
  try {
    await wsApplyChange(res);
  } catch (err) {
    wsMsg('已取消保存', 'muted');
    return;
  }
  // 落盘后同步 mtime / original
  try {
    const data = await api('/workspace/file?path=' + encodeURIComponent(st.path));
    st.original = data.content;
    st.mtime = data.mtime;
    WS.dirty.delete(st.path);
    wsUpdateTabTitle();
    wsMsg('已保存 ' + st.path, 'ok');
  } catch (err) {
    wsMsg('保存后刷新失败：' + err.message, 'err');
  }
}

/* ----------------------------------------------------------------------
   搜索
   ---------------------------------------------------------------------- */
async function wsSearch(q) {
  const box = $('#ws-search-results');
  const meta = $('#ws-search-meta');
  if (!box) return;
  if (!q) { box.innerHTML = ''; if (meta) meta.textContent = ''; return; }
  WS.lastSearch = q;
  box.innerHTML = '<div class="muted sm">搜索中…</div>';
  let data;
  try {
    data = await api('/workspace/search?q=' + encodeURIComponent(q) + '&max=100');
  } catch (err) {
    box.innerHTML = '<div class="muted sm">搜索失败：' + esc(err.message) + '</div>';
    return;
  }
  const hits = data.hits || [];
  if (meta) {
    meta.textContent = hits.length + ' 处命中 / 扫描 ' + (data.scanned_files || 0)
      + ' 个文件' + (data.truncated ? '（已截断）' : '');
  }
  if (!hits.length) {
    box.innerHTML = '<div class="muted sm">未找到匹配</div>';
    return;
  }
  // 按文件分组
  const groups = new Map();
  hits.forEach((h) => {
    if (!groups.has(h.path)) groups.set(h.path, []);
    groups.get(h.path).push(h);
  });
  box.innerHTML = '';
  groups.forEach((list, path) => {
    const g = document.createElement('div');
    g.className = 'ws-search-group';
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'ws-search-file';
    head.innerHTML = '<span class="ws-search-fname">' + esc(path.split('/').pop()) + '</span>'
      + '<span class="ws-search-fpath">' + esc(path) + '</span>'
      + '<span class="ws-search-fcount">' + list.length + '</span>';
    head.addEventListener('click', () => wsOpenFile(path, path.split('/').pop()));
    g.appendChild(head);
    list.slice(0, 20).forEach((h) => {
      const row = document.createElement('div');
      row.className = 'ws-search-hit';
      row.innerHTML = '<span class="ws-search-ln">' + h.line + '</span>'
        + '<span class="ws-search-text">' + esc(h.text.trim().slice(0, 160)) + '</span>';
      row.addEventListener('click', () => {
        wsOpenFile(path, path.split('/').pop()).then(() => wsGotoLine(h.line));
      });
      g.appendChild(row);
    });
    box.appendChild(g);
  });
}

/* ----------------------------------------------------------------------
   与 Agent 联动：把选中内容发给 Agent
   ---------------------------------------------------------------------- */
function wsSendSelectionToAgent() {
  const page = wsActivePage();
  const input = page ? page.querySelector('#ws-input') : null;
  const st = wsEditorState(wsActivePath());
  if (!input || !st.path) { wsMsg('没有选中的内容', 'muted'); return; }
  const sel = input.value.substring(input.selectionStart, input.selectionEnd);
  if (!sel.trim()) { wsMsg('请先在编辑器里选中一段文本', 'muted'); return; }
  const lineNo = input.value.slice(0, input.selectionStart).split('\n').length;
  const msg = '以下是文件 ' + st.path + ' 第 ' + lineNo + ' 行起的代码片段：\n\n```\n'
    + sel + '\n```\n\n请基于这段代码…';
  const composer = $('#input');
  if (composer) {
    composer.value = (composer.value ? composer.value + '\n\n' : '') + msg;
    composer.focus();
    composer.dispatchEvent(new Event('input'));
    wsMsg('已把选中的代码填入输入框（' + sel.length + ' 字符）', 'ok');
  }
}

/* ----------------------------------------------------------------------
   初始化
   ---------------------------------------------------------------------- */
function wsInit() {
  // 活动栏视图切换时刷新文件树
  const btnSearch = $('#btn-ws-search-toggle');
  if (btnSearch) {
    btnSearch.addEventListener('click', () => {
      const box = $('#ws-search-box');
      if (!box) return;
      box.hidden = !box.hidden;
      if (!box.hidden) {
        const inp = $('#ws-search-input');
        if (inp) inp.focus();
      }
    });
  }
  const btnRefresh = $('#btn-ws-refresh');
  if (btnRefresh) btnRefresh.addEventListener('click', () => wsRefreshTree());
  const btnNew = $('#btn-ws-new');
  if (btnNew) btnNew.addEventListener('click', () => wsNewFile(''));

  const searchInput = $('#ws-search-input');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      if (WS.searchTimer) clearTimeout(WS.searchTimer);
      WS.searchTimer = setTimeout(() => wsSearch(searchInput.value.trim()), 350);
    });
    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        if (WS.searchTimer) clearTimeout(WS.searchTimer);
        wsSearch(searchInput.value.trim());
      }
    });
  }

  // 行内操作（新建 / 重命名 / 删除）—— 事件委托
  document.addEventListener('click', (e) => {
    const act = e.target.closest('.ws-act');
    if (!act) return;
    e.stopPropagation();
    const row = act.closest('.tree-row');
    if (!row) return;
    const path = row.dataset.path || '';
    const kind = act.dataset.act;
    if (kind === 'new') wsNewFile(path);
    else if (kind === 'rename') wsRenameFile(path);
    else if (kind === 'delete') wsDeleteFile(path);
  });

  // 全局快捷键：Ctrl+S 保存 / Ctrl+F 查找
  document.addEventListener('keydown', (e) => {
    const active = document.activeElement;
    const inEditor = active && active.id === 'ws-input';
    if (!inEditor) return;
    if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
      e.preventDefault();
      wsSave();
    } else if ((e.ctrlKey || e.metaKey) && (e.key === 'f' || e.key === 'F')) {
      e.preventDefault();
      const page = wsActivePage();
      const box = page ? page.querySelector('#ws-find') : null;
      if (box) {
        box.hidden = false;
        const fi = page.querySelector('#ws-find-input');
        if (fi) { fi.focus(); fi.select(); }
      }
    }
  });

  wsLoadRoot();
}

/** Agent 写盘后刷新打开的文件（标脏） */
function wsRefreshAfterAgentWrite(path) {
  if (!path) return;
  const st = wsEditorState(path);
  if (st.path !== path) return;
  const page = wsActivePage();
  const input = page ? page.querySelector('#ws-input') : null;
  if (!input) return;
  api('/workspace/file?path=' + encodeURIComponent(path)).then((data) => {
    st.original = data.content;
    st.mtime = data.mtime;
    input.value = data.content;
    wsRenderEditor();
    WS.dirty.add(path);
    wsUpdateTabTitle();
    wsMsg('Agent 已修改 ' + path + '，内容已刷新（请复核后保存）', 'warn');
  }).catch(() => { /* 忽略 */ });
}

/** 兼容旧调用名 */
function wsOnExternalChange(path) {
  wsRefreshAfterAgentWrite(path);
}

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => {
    try { wsInit(); } catch (e) { /* 忽略 */ }
  });
}
