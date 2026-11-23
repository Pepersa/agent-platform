/* ==========================================================================
   审批统一入口（Unified Approval Hub）
   ==========================================================================
   为什么要有这个文件（真实故障）
   ----------------------------------
   同一个 ``tool_call_id`` 曾经有 **5 个批准/拒绝按钮、分散在 3 份互不
   同步的状态里**：

     A. ``timeline.js`` 运行观测 Live Timeline —— 折叠头 2 个 + 展开区 2 个
     B. ``observability.js`` 审批卡片视图 —— 2 个
     C. ``app.js`` 对话页 HITL 卡片 —— 2 个（唯一真正可用的一套）

   A/B/C 各自维护自己的 ``resolved`` 标记，且 **A 和 B 各自直接 POST
   ``/approve``**。于是：在运行观测面板点「批准」之后：

     · 服务端已经续跑完成、文件已落盘；
     · 但对话页那张卡片仍然是 ``is-pending``（琥珀色呼吸光），
       「批准」按钮仍然可点；
     · 用户看到一张**永远批不掉的卡片** → 结论「对话里批不了」。

   本模块的职责：**把审批收敛成一条路径**
   -----------------------------------------------------------
   1. **唯一决策入口**：全站只有 ``app.js`` 的对话卡片会 POST ``/approve``。
      运行观测面板的按钮一律降级为「**定位器**」——只负责把对话卡片
      滚到视野中央并高亮，**不自己决定**。
   2. **唯一事实来源**：``APPROVAL_STATE.resolved``（cid → approved/rejected），
      任何界面都从这里读「这条到底批了没」。
   3. **广播**：任何一处决策完成后广播 ``hitl-resolved`` 窗口事件，
      所有界面（对话卡片 / Live Timeline / 审批卡片视图）订阅后各自刷新，
      状态不可能再分叉。
   ---------------------------------------------------------------------- */
'use strict';

/* ----------------------------------------------------------------------
   事实来源：cid → 决策
   ---------------------------------------------------------------------- */
/**
 * 全局唯一的审批状态表。
 *
 * 结构：``{ [tool_call_id]: 'approved' | 'rejected' }``
 *
 * ⚠️ **只有本模块可以写**（``APPROVAL.markResolved`` / ``APPROVAL.syncFromServer``）。
 * 其它界面只读 —— 这是「状态不分叉」的根本保证。
 */
const APPROVAL = {
  /** cid → 'approved' | 'rejected' */
  resolved: Object.create(null),
  /** cid → 决策摘要（停机原因 / 状态文案），用于各界面展示 */
  summaries: Object.create(null),
  /** 事件监听器（内部用，方便测试时解绑） */
  _listeners: [],
};

/**
 * 广播「这条审批已决策」。
 *
 * ⚠️ 必须走 **window 自定义事件** 而不是直接调各界面函数：
 * 各静态文件是并列加载的（app.js / observability.js / timeline.js），
 * 加载顺序不保证；用事件解耦后，谁先谁后都能收到。
 */
APPROVAL.emitResolved = function (toolCallId, approved, summary) {
  const cid = String(toolCallId || '');
  if (!cid) return;
  APPROVAL.resolved[cid] = approved ? 'approved' : 'rejected';
  APPROVAL.summaries[cid] = summary || '';
  const detail = {
    tool_call_id: cid,
    approved: !!approved,
    summary: summary || '',
  };
  try {
    window.dispatchEvent(new CustomEvent('hitl-resolved', { detail: detail }));
  } catch (err) {
    /* 老浏览器没有 CustomEvent → 退化到直接回调监听器 */
    (APPROVAL._listeners || []).forEach((fn) => {
      try { fn(detail); } catch (e) { /* 单个监听器出错不影响其它 */ }
    });
  }
};

/** 订阅审批决策事件（返回取消订阅函数） */
APPROVAL.onResolved = function (fn) {
  if (typeof fn !== 'function') return function () {};
  const handler = (ev) => { fn(ev && ev.detail ? ev.detail : ev); };
  window.addEventListener('hitl-resolved', handler);
  APPROVAL._listeners.push(fn);
  return function () {
    window.removeEventListener('hitl-resolved', handler);
  };
};

/** 某条审批是否已决策（只读） */
APPROVAL.isResolved = function (toolCallId) {
  const cid = String(toolCallId || '');
  return !!(cid && APPROVAL.resolved[cid]);
};

/** 取某条审批的决策结果（只读） */
APPROVAL.getResolved = function (toolCallId) {
  const cid = String(toolCallId || '');
  return cid ? (APPROVAL.resolved[cid] || null) : null;
};

/**
 * 从服务端返回的审批列表**同步**事实来源。
 *
 * 用途：页面刷新后从会话历史恢复审批卡片时，已批过的项不该再显示
 * 「待审批」。服务端给的审批项里可能带 ``approved`` 字段
 * （命中审计表 / 会话落盘数据）。
 */
APPROVAL.syncFromServer = function (list) {
  (list || []).forEach((ap) => {
    if (!ap) return;
    const cid = String(ap.tool_call_id || '');
    if (!cid) return;
    if (ap.approved === true || ap.resolved === 'approved') {
      APPROVAL.resolved[cid] = 'approved';
    } else if (ap.approved === false || ap.resolved === 'rejected') {
      APPROVAL.resolved[cid] = 'rejected';
    }
  });
};

/* ----------------------------------------------------------------------
   对话卡片定位（唯一的「决策界面」）
   ---------------------------------------------------------------------- */
/**
 * 找到某个 tool_call_id 对应的**对话审批卡片**。
 *
 * ⚠️ 卡片可能在重试时被**替换**（按钮跑到另一个 ``.h-actions`` 里），
 * 所以查找时同时看卡片根节点和操作区两处的 ``data-cid``。
 */
APPROVAL.findChatCard = function (toolCallId) {
  const cid = String(toolCallId || '');
  if (!cid) return null;
  const esc = (typeof cssEscape === 'function')
    ? cssEscape
    : (v) => String(v == null ? '' : v).replace(/["\\]/g, '\\$&');
  let el = null;
  try {
    el = document.querySelector('.hitl[data-cid="' + esc(cid) + '"]');
  } catch (err) {
    el = null;
  }
  if (el) return el;
  /* 退化：卡片可能被替换成只有 .h-actions 的形态 */
  try {
    el = document.querySelector('.h-actions[data-cid="' + esc(cid) + '"]');
  } catch (err) {
    el = null;
  }
  return el;
};

/**
 * 把「跳到对话里的审批卡片」这件事做成一个统一入口。
 *
 * 运行观测面板的「批准 / 拒绝」按钮**只做这一件事**：
 * 把对话卡片滚进视野 + 高亮 + 聚焦「批准」按钮。
 *
 * ⚠️ **绝不**在这里 POST ``/approve`` —— 那是对话卡片按钮的专属职责。
 * 只有一条 POST 路径，状态才不会分叉。
 *
 * @returns {boolean} 是否找到了对话卡片（找不到时调用方可提示用户）
 */
APPROVAL.focusChatCard = function (toolCallId, opts) {
  const cid = String(toolCallId || '');
  const card = APPROVAL.findChatCard(cid);
  if (!card) return false;
  const o = opts || {};

  /* 滚到视野中央（而不是最底部 —— 审批卡片通常在消息流中间） */
  try {
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  } catch (err) {
    try { card.scrollIntoView(); } catch (e) { /* 忽略 */ }
  }

  /* 高亮：复用对话卡片既有的 is-flash 样式。
   * ⚠️ **绝不能**动 ``is-pending`` —— 定位器**没有**做任何决策，
   *    卡片仍然是「真的还在等批准」，去掉 is-pending 会让用户以为
   *    这条已经被处理过了（正是本次要消灭的「状态不符」）。 */
  card.classList.add('is-flash');
  setTimeout(() => { card.classList.remove('is-flash'); }, 2400);

  /* 聚焦「批准」按钮，方便用户直接回车 —— 但**不自动点击** */
  const btn = card.querySelector('.act-approve') || card.querySelector('.act-reject');
  if (btn && !o.noFocus) {
    try { btn.focus({ preventScroll: true }); } catch (err) { /* 忽略 */ }
  }
  return true;
};

/**
 * 让某个界面的审批按钮**降级为定位器**。
 *
 * 统一的 title 文案告诉用户「点这里会跳到对话里批」，避免用户以为
 * 在面板里点了就等于批了（那正是旧实现造成状态分叉的根源）。
 */
APPROVAL.LOCATOR_HINT = '跳转到对话中的审批卡片（审批统一在对话里完成）';

if (typeof window !== 'undefined') {
  window.APPROVAL = APPROVAL;
}
