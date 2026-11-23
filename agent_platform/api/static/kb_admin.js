/* ==========================================================================
   知识库管理前端 —— 用户自助导入 / 删除 / 重建索引
   （原生 JS，无框架、无 CDN）
   ========================================================================== */
'use strict';

/* ----------------------------------------------------------------------
   状态
   ---------------------------------------------------------------------- */
const kb = {
  file: null,          // 待上传的 File 对象
  tab: 'upload',       // upload | text
  busy: false,
};

/* ----------------------------------------------------------------------
   打开 / 关闭模态框
   ---------------------------------------------------------------------- */
function openKbModal() {
  kb.file = null;
  $('#kb-file').value = '';
  $('#kb-file-info').textContent = '';
  $('#kb-text').value = '';
  $('#kb-doc-title').value = '';
  $('#kb-doc-type').value = '';
  $('#kb-version').value = '';
  $('#kb-sec-level').value = 'internal';
  $('#kb-roles').value = '';
  $('#kb-product-line').value = '';
  $('#kb-modal-msg').textContent = '';
  switchKbTab('upload');
  $('#kb-mask').hidden = false;
}

function closeKbModal() {
  $('#kb-mask').hidden = true;
  kb.file = null;
}

function switchKbTab(tab) {
  kb.tab = tab;
  $('#kb-tab-upload').classList.toggle('active', tab === 'upload');
  $('#kb-tab-text').classList.toggle('active', tab === 'text');
  $('#kb-pane-upload').hidden = tab !== 'upload';
  $('#kb-pane-text').hidden = tab !== 'text';
}

/* ----------------------------------------------------------------------
   文档列表
   ---------------------------------------------------------------------- */
async function refreshKbDocuments() {
  const box = $('#kb-doc-list');
  if (!box) return;
  try {
    const res = await api('/kb/documents');
    const docs = res.documents || [];
    const sum = $('#kb-summary');
    if (sum) {
      const chunks = docs.reduce((a, d) => a + (d.chunk_count || 0), 0);
      sum.textContent = `${docs.length} 份文档 · ${chunks} 块`;
    }
    box.innerHTML = '';
    if (!docs.length) {
      box.innerHTML = '<div class="muted">知识库还是空的，点「＋ 导入文档」上传你自己的文档。</div>';
      return;
    }
    docs.forEach((d) => {
      const el = document.createElement('div');
      el.className = 'tree-row tree-row2 leaf act';
      el.title = '点击查看文档详情';
      const level = String(d.security_level || 'internal');
      const roles = (d.visible_roles || ['*']).join(', ');
      // 来源标识：目录来源 vs UI 上传来源（后端 origin 字段）
      const origin = d.origin === 'directory' ? 'directory' : 'upload';
      const originLabel = origin === 'directory' ? '目录' : '上传';
      const originTitle = origin === 'directory'
        ? ('磁盘目录：' + (d.stored_path || d.source || ''))
        : '通过 Web 工作台上传 / 粘贴（原文存在知识库中）';
      el.innerHTML =
        `<span class="row-line1">`
        + `<span class="row-title" title="${esc(d.title || '')}">${esc(d.title || d.doc_id)}</span>`
        + `<span class="row-meta">`
        + `<span class="origin ${origin}" title="${esc(originTitle)}">${originLabel}</span>`
        + `<span class="lvl ${esc(level)}">${esc(level)}</span>`
        + `</span></span>`
        + `<span class="row-line2">${esc(d.doc_id)} · ${esc(d.doc_type || 'document')}`
        + ` · ${d.chunk_count || 0} 块 · ${fmtBytes(d.size_bytes)}`
        + (d.hit_count ? ` · 命中 ${d.hit_count}` : '')
        + ` · 角色 ${esc(roles)}`
        + (d.version ? ` · v${esc(d.version)}` : '') + `</span>`
        + `<span class="row-actions">`
        + `<button class="btn btn-ghost act-reindex" type="button">重新索引</button>`
        + `<button class="btn btn-ghost act-del" type="button">删除</button>`
        + `</span>`;
      $('.act-reindex', el).addEventListener('click', (e) => {
        e.stopPropagation();
        reindexKbDocument(d.doc_id);
      });
      $('.act-del', el).addEventListener('click', (e) => {
        e.stopPropagation();
        deleteKbDocument(d.doc_id, d.title);
      });
      el.addEventListener('click', () => openKbDocTab(d));
      box.appendChild(el);
    });
  } catch (err) {
    box.innerHTML = '<div class="muted sm">加载失败：' + esc(err.message) + '</div>';
  }
}

/* ----------------------------------------------------------------------
   文档预览（主区域 tab）
   说明：后端只有 GET /kb/documents（元信息列表），**没有**单文档正文端点，
        因此这里显示元信息卡 + 可选的可检索预览（GET /kb/search 探针）。
   ---------------------------------------------------------------------- */
async function openKbDocTab(doc) {
  let tab = null;
  try {
    tab = openTab('doc', doc.title || doc.doc_id, { docId: doc.doc_id, doc: doc });
  } catch (e) {
    return;
  }
  if (tab) refreshDocTab({ docId: doc.doc_id, doc: doc });
}

/** 刷新文档预览 tab（元信息 + 检索预览） */
function refreshDocTab(data) {
  if (!data || !data.docId) return;
  const tab = state.tabs.find((t) => t.id === state.activeTab);
  if (!tab || tab.type !== 'doc') return;
  const doc = data.doc || {};
  const page = $('.page[data-page="' + tab.id + '"]');
  if (!page) return;

  const level = String(doc.security_level || 'internal');
  const rows = [
    ['doc_id', doc.doc_id], ['类型', doc.doc_type || 'document'],
    ['版本', doc.version || '–'], ['负责人', doc.owner || '–'],
    ['密级', level], ['可见角色', (doc.visible_roles || ['*']).join(', ')],
    ['产品线', doc.product_line || '–'], ['chunk 数', doc.chunk_count || 0],
    ['大小', fmtBytes(doc.size_bytes)], ['命中次数', doc.hit_count || 0],
    ['来源', doc.origin === 'directory' ? ('目录：' + (doc.stored_path || doc.source || '')) : '工作台上传 / 粘贴'],
    ['更新时间', doc.updated_at ? String(doc.updated_at).slice(0, 19) : '–'],
  ];

  page.innerHTML = '<div class="doc-page-inner">'
    + '<h2>' + esc(doc.title || doc.doc_id || '文档') + '</h2>'
    + '<div class="muted sm">文档元信息（后端未提供单文档正文端点，故此处展示元信息与检索预览）</div>'
    + '<div class="td-kv" style="margin:12px 0 20px">'
    + rows.map((kv) => '<div class="td-kv-row"><span>' + esc(kv[0]) + '</span><b>'
      + esc(kv[1] == null ? '–' : kv[1]) + '</b></div>').join('')
    + '</div>'
    + '<h3>检索预览</h3>'
    + '<div class="muted sm" id="kb-preview-hint">正在用标题作为查询做一次知识库检索…</div>'
    + '<div id="kb-preview-box"></div>'
    + '</div>';

  // 检索预览（用 /kb/search 探针；失败不致命）
  api('/kb/search?query=' + encodeURIComponent(doc.title || doc.doc_id || '')
    + '&max_security_level=secret&top_k=5')
    .then((res) => {
      const box = $('#kb-preview-box');
      if (!box) return;
      const hits = res.hits || [];
      const mine = hits.filter((h) => h.doc_id === doc.doc_id);
      const use = mine.length ? mine : hits;
      if (!use.length) {
        box.innerHTML = '<div class="muted sm">没有检索到与该文档相关的 chunk（可能未命中或已过期）。</div>';
        return;
      }
      box.innerHTML = use.map((h, i) =>
        '<div class="tl-item"><div class="tl-dot"></div><div class="tl-body">'
        + '<div class="tl-head"><span class="tl-seq">#' + (i + 1) + '</span>'
        + '<span class="tl-name">' + esc(h.chunk_id || '') + '</span></div>'
        + '<div class="tl-sum">' + esc(h.text || '') + '</div>'
        + '</div></div>').join('');
    })
    .catch((err) => {
      const box = $('#kb-preview-box');
      if (box) box.innerHTML = '<div class="muted sm">检索预览不可用：' + esc(err.message) + '</div>';
    });
}

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return v + ' B';
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB';
  return (v / 1024 / 1024).toFixed(1) + ' MB';
}

/* ----------------------------------------------------------------------
   导入
   ---------------------------------------------------------------------- */
async function submitKbImport() {
  if (kb.busy) return;
  const msg = $('#kb-modal-msg');
  const btn = $('#kb-submit');
  const rolesRaw = ($('#kb-roles').value || '').trim();
  const roles = rolesRaw ? rolesRaw.split(/[,，;；\s]+/).filter(Boolean) : [];

  const payload = {
    title: ($('#kb-doc-title').value || '').trim(),
    doc_type: ($('#kb-doc-type').value || '').trim(),
    version: ($('#kb-version').value || '').trim(),
    security_level: $('#kb-sec-level').value,
    visible_roles: roles,
    product_line: ($('#kb-product-line').value || '').trim(),
  };

  kb.busy = true;
  btn.disabled = true;
  msg.textContent = '导入中（切块 + 向量化，可能需要几秒）…';

  try {
    let res;
    if (kb.tab === 'upload') {
      if (!kb.file) {
        msg.innerHTML = '<span class="pill pill-err">请先选择文件</span>';
        kb.busy = false; btn.disabled = false;
        return;
      }
      const fd = new FormData();
      fd.append('file', kb.file);
      fd.append('title', payload.title);
      fd.append('doc_type', payload.doc_type);
      fd.append('version', payload.version);
      fd.append('security_level', payload.security_level);
      fd.append('visible_roles', roles.join(','));
      fd.append('product_line', payload.product_line);
      const r = await fetch(API + '/kb/documents/upload', { method: 'POST', body: fd });
      const text = await r.text();
      let data;
      try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
      if (!r.ok) {
        const e = data || {};
        throw new Error((e.message || e.detail || ('HTTP ' + r.status))
          + (e.hint ? '（' + e.hint + '）' : ''));
      }
      res = data;
    } else {
      const text = ($('#kb-text').value || '').trim();
      if (!text) {
        msg.innerHTML = '<span class="pill pill-err">请粘贴文档内容</span>';
        kb.busy = false; btn.disabled = false;
        return;
      }
      res = await api('/kb/documents', {
        method: 'POST',
        body: JSON.stringify(Object.assign({ text: text }, payload)),
      });
    }
    msg.innerHTML = '<span class="pill pill-ok">已导入：'
      + esc(res.title || res.doc_id) + '（' + (res.chunk_count || 0) + ' 块）</span>';
    $('#kb-text').value = '';
    kb.file = null;
    $('#kb-file').value = '';
    $('#kb-file-info').textContent = '';
    refreshKbDocuments();
  } catch (err) {
    msg.innerHTML = '<span class="pill pill-err">' + esc(err.message) + '</span>';
  } finally {
    kb.busy = false;
    btn.disabled = false;
  }
}

/* ----------------------------------------------------------------------
   删除 / 重建索引
   ---------------------------------------------------------------------- */
async function deleteKbDocument(docId, title) {
  if (!window.confirm('确定从知识库删除「' + (title || docId) + '」？\n该文档的全部 chunk 将从索引中移除。')) {
    return;
  }
  const msg = $('#kb-msg');
  msg.textContent = '删除中…';
  try {
    const res = await api('/kb/documents/' + encodeURIComponent(docId), { method: 'DELETE' });
    msg.innerHTML = '<span class="pill pill-ok">已删除（移除 '
      + (res.deleted_chunks || 0) + ' 块）</span>';
    refreshKbDocuments();
  } catch (err) {
    msg.innerHTML = '<span class="pill pill-err">' + esc(err.message) + '</span>';
  }
}

async function reindexKbDocument(docId) {
  const msg = $('#kb-msg');
  msg.textContent = '重建索引中…';
  try {
    const res = await api('/kb/documents/' + encodeURIComponent(docId) + '/reindex',
      { method: 'POST' });
    msg.innerHTML = '<span class="pill pill-ok">已重建（' + (res.chunk_count || 0) + ' 块）</span>';
    refreshKbDocuments();
  } catch (err) {
    msg.innerHTML = '<span class="pill pill-err">' + esc(err.message) + '</span>';
  }
}

async function reindexAllKb() {
  if (!window.confirm('全量重建索引会**清空现有索引**，然后按 data/docs 目录 + 知识库中已导入的文档重新接入。\n你通过本工作台上传 / 粘贴的文档会被保留。\n确定继续？')) {
    return;
  }
  const msg = $('#kb-msg');
  msg.textContent = '全量重建中（清空 → 解析 → 向量化 → 写入），请稍候…';
  const btn = $('#btn-kb-reindex-all');
  if (btn) btn.disabled = true;
  try {
    const res = await api('/kb/reindex', { method: 'POST' });
    msg.innerHTML = '<span class="pill pill-ok">已重建：'
      + (res.doc_count || 0) + ' 份文档 / ' + (res.chunk_count || 0) + ' 块</span>';
    refreshKbDocuments();
  } catch (err) {
    msg.innerHTML = '<span class="pill pill-err">' + esc(err.message) + '</span>';
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ----------------------------------------------------------------------
   事件绑定
   ---------------------------------------------------------------------- */
function bindKbAdmin() {
  const imp = $('#btn-kb-import');
  if (imp) imp.addEventListener('click', openKbModal);
  const imp2 = $('#btn-kb-import-2');
  if (imp2) imp2.addEventListener('click', openKbModal);

  const mask = $('#kb-mask');
  if (mask) {
    mask.addEventListener('click', (e) => { if (e.target === mask) closeKbModal(); });
  }
  const cl = $('#kb-close');
  if (cl) cl.addEventListener('click', closeKbModal);
  const cn = $('#kb-cancel');
  if (cn) cn.addEventListener('click', closeKbModal);
  const sb = $('#kb-submit');
  if (sb) sb.addEventListener('click', submitKbImport);

  const tu = $('#kb-tab-upload');
  if (tu) tu.addEventListener('click', () => switchKbTab('upload'));
  const tt = $('#kb-tab-text');
  if (tt) tt.addEventListener('click', () => switchKbTab('text'));

  // ---- 拖拽上传 ----
  const dz = $('#kb-dropzone');
  const fileInput = $('#kb-file');
  if (dz && fileInput) {
    dz.addEventListener('click', () => fileInput.click());
    dz.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
    });
    fileInput.addEventListener('change', () => {
      if (fileInput.files && fileInput.files[0]) pickKbFile(fileInput.files[0]);
    });
    ['dragenter', 'dragover'].forEach((ev) => {
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    });
    ['dragleave', 'drop'].forEach((ev) => {
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('dragover'); });
    });
    dz.addEventListener('drop', (e) => {
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files[0]) pickKbFile(dt.files[0]);
    });
  }

  // ---- 全量重建 ----
  const ra = $('#btn-kb-reindex-all');
  if (ra) ra.addEventListener('click', reindexAllKb);
}

/** 选中文件 */
function pickKbFile(f) {
  kb.file = f;
  const name = f.name || '';
  const size = f.size || 0;
  $('#kb-file-info').innerHTML = '已选择：<b>' + esc(name) + '</b>（' + fmtBytes(size) + '）';
  // 文件名（无扩展名）作为标题默认值
  const titleInput = $('#kb-doc-title');
  if (titleInput && !titleInput.value) {
    titleInput.value = name.replace(/\.[^.]+$/, '');
  }
  switchKbTab('upload');
}
