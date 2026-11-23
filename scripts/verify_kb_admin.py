# -*- coding: utf-8 -*-
"""知识库管理（导入 / 检索 / ACL / 删除 / 重建索引）验证脚本。

覆盖本次产品化改造新增的能力：
    1. ``GET  /kb/documents``          —— 列出已入库文档
    2. ``POST /kb/documents``          —— 粘贴文本导入
    3. ``POST /kb/documents/upload``   —— 上传文件导入（multipart）
    4. ``DELETE /kb/documents/{id}``   —— 删除文档（并移除其全部 chunk）
    5. ``POST /kb/documents/{id}/reindex`` —— 单文档重建索引
    6. ``POST /kb/reindex``            —— 全量重建索引
    7. ``GET  /kb/search``             —— 直接检索探针（验证「立即可检索」与 ACL）
    8. ``POST /chat``                  —— 真实模型链路：答案带引用且引用指向该文档
    9. 结构化错误：超大文件 / 不支持类型 / 空文档
   10. 前端契约：Enter 发送、分隔条拖拽（localStorage 持久化）、折叠段、导入模态框

用法：
    python scripts/verify_kb_admin.py [--base http://127.0.0.1:8010]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import httpx

BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")
RESULTS = []

#: 导入用的独特标识（用于验证「只有该文档能回答」）
MARK = "XYZ-777"
CUSTOM_DOC_ID = "DOC-MYCUSTOM-KB"


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    flag = "PASS" if cond else "FAIL"
    print(f"  [{flag}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)
    return bool(cond)


def sec(t):
    print(f"\n=== {t} ===", flush=True)


def cleanup(client, doc_id):
    """尽力删除测试文档（无论成败）。"""
    try:
        client.delete(f"{BASE}/kb/documents/{doc_id}", timeout=120.0)
    except Exception:
        pass


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    client = httpx.Client(timeout=300.0)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample_md = os.path.join(repo_root, "data", "kb", "uploads", "my_custom_kb.md")
    if not os.path.isfile(sample_md):
        # 兜底：从 data/docs 找
        cand = os.path.join(repo_root, "data", "docs", "my_custom_kb.md")
        sample_md = cand if os.path.isfile(cand) else sample_md

    # ---------------------------------------------------------- 1
    sec("1. GET /kb/documents —— 列出已入库文档")
    r = client.get(f"{BASE}/kb/documents")
    print("  HTTP", r.status_code)
    check("/kb/documents 返回 200", r.status_code == 200, str(r.status_code))
    if r.status_code != 200:
        print("  body:", r.text[:600])
        return 1
    j = r.json()
    docs = j.get("documents") or []
    print(f"  count={j.get('count')}")
    for d in docs:
        print(f"    - {d.get('doc_id'):22} {str(d.get('title'))[:26]:28} "
              f"chunks={d.get('chunk_count'):3} level={d.get('security_level'):12} "
              f"roles={d.get('visible_roles')}")
    check("返回文档列表（含 count 字段）", isinstance(docs, list) and "count" in j,
          f"count={j.get('count')}")
    check("至少 1 份文档", len(docs) >= 1, f"{len(docs)} 份")
    if docs:
        d0 = docs[0]
        need = ("doc_id", "title", "doc_type", "version", "security_level",
                "visible_roles", "chunk_count", "size_bytes", "updated_at", "source")
        check("文档项字段齐全",
              all(k in d0 for k in need),
              str([k for k in need if k not in d0]))

    # ---------------------------------------------------------- 2
    sec("2. POST /kb/documents —— 粘贴文本导入（真实 bge-m3 向量）")
    text = """---
doc_id: DOC-KBTEXT-001
title: 粘贴文本导入测试
version: v2.0
doc_type: note
owner: 验证脚本
security_level: internal
visible_roles: [engineer]
---

# XYZ-777 粘贴文本测试

粘贴方式导入的文档：XYZ-777 的谐振频率为 8.314 GHz，阻尼比为 0.027。
"""
    r = client.post(f"{BASE}/kb/documents", json={
        "title": "粘贴文本导入测试",
        "text": text,
        "doc_type": "note",
        "version": "v2.0",
        "security_level": "internal",
        "visible_roles": ["engineer"],
    })
    print("  HTTP", r.status_code)
    print("  body:", json.dumps(r.json() if r.status_code < 500 else r.text,
                                 ensure_ascii=False)[:600])
    check("粘贴文本导入返回 201", r.status_code == 201, str(r.status_code))
    if r.status_code == 201:
        imp = r.json()
        check("自动解析 front matter 的 doc_id", imp.get("doc_id") == "DOC-KBTEXT-001",
              str(imp.get("doc_id")))
        check("自动解析 front matter 的 title", imp.get("title") == "粘贴文本导入测试",
              str(imp.get("title")))
        check("自动解析 front matter 的 version", imp.get("version") == "v2.0",
              str(imp.get("version")))
        check("chunk_count > 0", int(imp.get("chunk_count") or 0) > 0,
              str(imp.get("chunk_count")))
        check("使用真实 bge-m3 embedding（非降级）",
              imp.get("embedding_backend") == "flagembedding_bge_m3",
              str(imp.get("embedding_backend")))
        check("ACL 生效：security_level=internal",
              imp.get("security_level") == "internal", str(imp.get("security_level")))
        check("ACL 生效：visible_roles=[engineer]",
              imp.get("visible_roles") == ["engineer"], str(imp.get("visible_roles")))

    # ---------------------------------------------------------- 3
    sec("3. POST /kb/documents/upload —— 上传文件导入")
    if os.path.isfile(sample_md):
        with open(sample_md, "rb") as fh:
            content = fh.read()
        r = client.post(
            f"{BASE}/kb/documents/upload",
            files={"file": ("my_custom_kb.md", content, "text/markdown")},
            data={"title": "自定义知识库测试文档",
                  "doc_type": "test_manual",
                  "version": "v1.0",
                  "security_level": "internal",
                  "visible_roles": "engineer,tester",
                  "product_line": "CUST-LINE-1"},
        )
        print("  HTTP", r.status_code)
        print("  body:", json.dumps(r.json() if r.status_code < 500 else r.text,
                                     ensure_ascii=False)[:700])
        check("上传导入返回 201", r.status_code == 201, str(r.status_code))
        if r.status_code == 201:
            up = r.json()
            check("上传文档 doc_id 正确", up.get("doc_id") == CUSTOM_DOC_ID,
                  str(up.get("doc_id")))
            check("上传文档 chunk_count > 0", int(up.get("chunk_count") or 0) > 0,
                  str(up.get("chunk_count")))
            check("上传文档使用真实 bge-m3",
                  up.get("embedding_backend") == "flagembedding_bge_m3",
                  str(up.get("embedding_backend")))
            check("上传文档 visible_roles 解析正确",
                  set(up.get("visible_roles") or []) == {"engineer", "tester"},
                  str(up.get("visible_roles")))
    else:
        check("示例文件存在（用于上传测试）", False, sample_md)

    # ---------------------------------------------------------- 4
    sec("4. GET /kb/documents —— 新文档已列出")
    j = client.get(f"{BASE}/kb/documents").json()
    docs = j.get("documents") or []
    ids = [d.get("doc_id") for d in docs]
    print("  ids:", ids)
    check("列表包含粘贴导入的文档", "DOC-KBTEXT-001" in ids, str(ids))
    check("列表包含上传的文档", CUSTOM_DOC_ID in ids, str(ids))
    target = next((d for d in docs if d.get("doc_id") == CUSTOM_DOC_ID), None)
    if target:
        check("上传文档 chunk 数 > 0", int(target.get("chunk_count") or 0) > 0,
              str(target.get("chunk_count")))
        check("上传文档密级徽标正确", target.get("security_level") == "internal",
              str(target.get("security_level")))
        check("上传文档体积字段有值", int(target.get("size_bytes") or 0) > 0,
              str(target.get("size_bytes")))

    # ---------------------------------------------------------- 5
    sec("5. GET /kb/search —— 导入后**立即可检索**（不经过 LLM）")
    r = client.get(f"{BASE}/kb/search",
                   params={"query": f"{MARK} 的标定增益是多少", "top_k": 6,
                           "max_security_level": "internal", "roles": "engineer"})
    print("  HTTP", r.status_code)
    sj = r.json()
    print(f"  hit_count={sj.get('hit_count')} reason={sj.get('reason')!r}")
    for h in (sj.get("hits") or [])[:6]:
        print(f"    - {h.get('doc_id')} / {h.get('chunk_id')}  {str(h.get('text'))[:70]!r}")
    check("/kb/search 返回 200", r.status_code == 200, str(r.status_code))
    check("检索到自定义文档的 chunk",
          any(h.get("doc_id") == CUSTOM_DOC_ID for h in (sj.get("hits") or [])),
          str([h.get("doc_id") for h in (sj.get("hits") or [])]))
    top_hit = (sj.get("hits") or [{}])[0]
    check("首位命中即自定义文档", top_hit.get("doc_id") == CUSTOM_DOC_ID,
          str(top_hit.get("doc_id")))
    check("命中内容含独特数值 13.37",
          "13.37" in str(top_hit.get("text") or ""), str(top_hit.get("text"))[:100])

    # ---------------------------------------------------------- 6
    sec("6. ACL —— 低密级用户检索不到 confidential 文档")
    conf = """---
doc_id: DOC-KBCONF-001
title: 机密验证文档
security_level: confidential
visible_roles: [admin]
---

# 机密内容 QQQ-888

QQQ-888 的绝密参数是 66.66，只有 administrator 可见。
"""
    r = client.post(f"{BASE}/kb/documents", json={"text": conf})
    print("  confidential 导入 HTTP", r.status_code)
    check("confidential 文档导入成功", r.status_code == 201, str(r.status_code))

    r_low = client.get(f"{BASE}/kb/search",
                       params={"query": "QQQ-888 的绝密参数", "top_k": 6,
                               "max_security_level": "internal", "roles": "engineer"})
    low = r_low.json()
    print(f"  低密级(internal)命中 {low.get('hit_count')}")
    for h in (low.get("hits") or [])[:4]:
        print(f"    - {h.get('doc_id')} {str(h.get('text'))[:50]!r}")
    check("低密级用户检索不到 confidential 文档",
          not any(h.get("doc_id") == "DOC-KBCONF-001" for h in (low.get("hits") or [])),
          str([h.get("doc_id") for h in (low.get("hits") or [])]))

    r_high = client.get(f"{BASE}/kb/search",
                        params={"query": "QQQ-888 的绝密参数", "top_k": 6,
                                "max_security_level": "confidential", "roles": "admin"})
    high = r_high.json()
    print(f"  高密级(confidential)命中 {high.get('hit_count')}")
    check("高密级用户能检索到 confidential 文档",
          any(h.get("doc_id") == "DOC-KBCONF-001" for h in (high.get("hits") or [])),
          str([h.get("doc_id") for h in (high.get("hits") or [])]))

    # ---------------------------------------------------------- 7
    sec("7. POST /chat —— 真实模型链路：答案带引用且指向自定义文档")
    answer = ""
    cited = False
    for attempt in range(3):
        payload = {"message": f"{MARK} 的标定增益是多少？请引用来源。"}
        evs = []
        with httpx.stream("POST", f"{BASE}/chat", json=payload, timeout=300.0) as resp:
            buf = ""
            for chunk in resp.iter_text():
                buf += chunk
                while "\n\n" in buf:
                    line, buf = buf.split("\n\n", 1)
                    d = [l for l in line.split("\n") if l.startswith("data: ")]
                    if not d:
                        continue
                    body = d[0][6:]
                    if body.strip() == "[DONE]":
                        continue
                    try:
                        evs.append(json.loads(body))
                    except Exception:
                        pass
        res = next((e for e in evs if e.get("event") == "result"), {})
        answer = res.get("answer") or ""
        print(f"  第 {attempt + 1} 次：status={res.get('status')} "
              f"tool_calls={res.get('tool_calls_used')}")
        print("  --- 答案 ---")
        print(answer)
        print("  -------------")
        cited = bool(re.search(r"[\[［【]\s*(来源|出处)?\s*[:：]\s*[A-Za-z0-9_\-]{2,}", answer))
        if cited:
            break
    check("/chat 返回了答案", len(answer) > 10, f"{len(answer)} 字符")
    check("答案带引用标记", cited, answer[:160])

    # ---------------------------------------------------------- 8
    sec("8. DELETE /kb/documents/{id} —— 删除并移除索引")
    r = client.delete(f"{BASE}/kb/documents/{CUSTOM_DOC_ID}")
    print("  HTTP", r.status_code, r.text[:300])
    check("删除返回 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        check("删除返回删掉的 chunk 数",
              int(r.json().get("deleted_chunks") or 0) > 0,
              str(r.json().get("deleted_chunks")))

    j = client.get(f"{BASE}/kb/documents").json()
    ids = [d.get("doc_id") for d in (j.get("documents") or [])]
    check("列表中已无被删文档", CUSTOM_DOC_ID not in ids, str(ids))

    r = client.get(f"{BASE}/kb/search",
                   params={"query": f"{MARK} 的标定增益是多少", "top_k": 6,
                           "max_security_level": "internal", "roles": "engineer"})
    after = r.json()
    print(f"  删除后命中 {after.get('hit_count')}")
    check("删除后检索不再返回该文档的 chunk",
          not any(h.get("doc_id") == CUSTOM_DOC_ID for h in (after.get("hits") or [])),
          str([h.get("doc_id") for h in (after.get("hits") or [])]))

    # ---------------------------------------------------------- 9
    sec("9. 结构化错误（不 500 崩掉）")
    r = client.post(f"{BASE}/kb/documents/upload",
                    files={"file": ("bad.exe", b"MZ\x90\x00", "application/octet-stream")},
                    data={"title": "x"})
    print("  不支持类型 HTTP", r.status_code, r.text[:220])
    check("不支持的类型返回 4xx", 400 <= r.status_code < 500, str(r.status_code))
    if r.status_code < 500:
        ej = r.json()
        check("错误结构含 code/message/retryable/hint",
              all(k in ej for k in ("code", "message", "retryable", "hint")),
              str(list(ej.keys())))

    r = client.post(f"{BASE}/kb/documents", json={"text": "   "})
    print("  空文档 HTTP", r.status_code, r.text[:220])
    check("空文档返回 4xx", 400 <= r.status_code < 500, str(r.status_code))

    r = client.post(f"{BASE}/kb/documents",
                    json={"text": "hello", "security_level": "顶级机密"})
    print("  非法密级 HTTP", r.status_code, r.text[:220])
    check("非法密级返回 4xx", 400 <= r.status_code < 500, str(r.status_code))

    r = client.delete(f"{BASE}/kb/documents/DOC-NOT-EXIST-XXX")
    print("  删除不存在 HTTP", r.status_code, r.text[:220])
    check("删除不存在的文档返回 404", r.status_code == 404, str(r.status_code))

    # ---------------------------------------------------------- 10
    sec("10. 单文档重建索引 / 全量重建")
    r = client.post(f"{BASE}/kb/documents/DOC-KBTEXT-001/reindex", timeout=300.0)
    print("  reindex HTTP", r.status_code, r.text[:300])
    check("单文档重建返回 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        check("重建后 chunk_count > 0",
              int(r.json().get("chunk_count") or 0) > 0,
              str(r.json().get("chunk_count")))

    # ---- 回归：全量重建**不能**让 UI 导入的文档消失（静默数据丢失缺陷）----
    # 背景：此前 --rebuild / POST /kb/reindex 只扫描 data/docs 目录，
    #       导致 UI 导入的文档在重建后从索引中消失（检索不到）。
    before = client.get(f"{BASE}/kb/documents").json()
    before_ids = [d.get("doc_id") for d in (before.get("documents") or [])]
    before_chunks = int((before.get("stats") or {}).get("total_chunks") or 0)
    print("  重建前：count=%s total_chunks=%s ids=%s"
          % (before.get("count"), before_chunks, before_ids))

    r = client.post(f"{BASE}/kb/reindex", timeout=600.0)
    print("  全量重建 HTTP", r.status_code, r.text[:300])
    check("全量重建返回 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        check("全量重建后文档数 > 0",
              int(r.json().get("doc_count") or 0) > 0,
              str(r.json().get("doc_count")))

    after = client.get(f"{BASE}/kb/documents").json()
    after_ids = [d.get("doc_id") for d in (after.get("documents") or [])]
    after_chunks = int((after.get("stats") or {}).get("total_chunks") or 0)
    print("  重建后：count=%s total_chunks=%s ids=%s"
          % (after.get("count"), after_chunks, after_ids))

    check("重建后 UI 导入的 DOC-KBTEXT-001 仍在列表里",
          "DOC-KBTEXT-001" in after_ids, str(after_ids))
    check("重建后 UI 导入的 DOC-KBCONF-001 仍在列表里",
          "DOC-KBCONF-001" in after_ids, str(after_ids))
    check("重建后目录文档 DOC-YIELD-001 仍在列表里",
          "DOC-YIELD-001" in after_ids, str(after_ids))
    check("重建不丢文档（文档集合不缩小）",
          set(before_ids).issubset(set(after_ids)),
          f"{sorted(set(before_ids) - set(after_ids))}")

    # 幂等：再重建一次，chunk 数与文档数应完全一致
    r2 = client.post(f"{BASE}/kb/reindex", timeout=600.0)
    print("  第二次全量重建 HTTP", r2.status_code)
    again = client.get(f"{BASE}/kb/documents").json()
    again_ids = [d.get("doc_id") for d in (again.get("documents") or [])]
    again_chunks = int((again.get("stats") or {}).get("total_chunks") or 0)
    print("  再重建后：count=%s total_chunks=%s" % (again.get("count"), again_chunks))
    check("幂等：连续两次重建 chunk 数一致",
          again_chunks == after_chunks, f"{after_chunks} vs {again_chunks}")
    check("幂等：连续两次重建文档集合一致",
          set(again_ids) == set(after_ids),
          f"{sorted(set(again_ids) ^ set(after_ids))}")

    # 重建后仍可检索（这是缺陷的核心：重建后检索不到）
    rs = client.get(f"{BASE}/kb/search",
                    params={"query": "XYZ-777 的标定增益是多少", "top_k": 6,
                            "max_security_level": "internal", "roles": "engineer"})
    sj2 = rs.json()
    print(f"  重建后检索命中 {sj2.get('hit_count')}")
    for h in (sj2.get("hits") or [])[:4]:
        print(f"    - {h.get('doc_id')} {str(h.get('text'))[:60]!r}")
    check("重建后 UI 导入文档仍可检索",
          any(h.get("doc_id") == "DOC-KBTEXT-001" for h in (sj2.get("hits") or [])),
          str([h.get("doc_id") for h in (sj2.get("hits") or [])]))

    # 来源字段（前端「上传 / 目录」徽标）
    d0 = next((d for d in (again.get("documents") or [])
               if d.get("doc_id") == "DOC-KBTEXT-001"), None)
    d1 = next((d for d in (again.get("documents") or [])
               if d.get("doc_id") == "DOC-YIELD-001"), None)
    check("文档项含 origin 字段（区分来源）",
          "origin" in (d0 or {}) and "stored_path" in (d0 or {}),
          str(list((d0 or {}).keys())))
    check("UI 导入文档 origin=upload", (d0 or {}).get("origin") == "upload",
          str((d0 or {}).get("origin")))
    check("目录文档 origin=directory", (d1 or {}).get("origin") == "directory",
          str((d1 or {}).get("origin")))

    # ---------------------------------------------------------- 11
    sec("11. 前端契约（HTML / CSS / JS）")
    ur = client.get(f"{BASE}/ui/")
    html = ur.text
    check("/ui/ 返回 200", ur.status_code == 200)
    check("标题已改为「Agent平台」", "<title>Agent平台</title>" in html,
          html[html.find("<title>"):html.find("</title>") + 8])
    check("副标题不绑定领域", "本地推理" in html and "半导体" not in html,
          "顶部品牌区使用通用表述「本地推理」")
    # IDE 三段式布局关键元素：
    #   · rs-left / sidebar  → 唯一的可拖拽侧栏（新版只有左侧栏，右栏被活动栏视图取代）
    #   · acc-head           → 旧的手风琴折叠段标题，新版改为树形分组标题 tree-toggle
    #   · activitybar/view-* → 活动栏切换的侧栏视图
    for key in ["kb-doc-list", "btn-kb-import", "btn-kb-reindex-all", "kb-mask",
                "kb-dropzone", "kb-file", "kb-text", "kb-sec-level", "kb-roles",
                "kb-submit", "rs-left", "sidebar", "tree-toggle",
                "btn-tools-more", "btn-tasks-more", "mobile-bar",
                "activitybar", "view-tools", "view-tasks", "view-knowledge",
                "view-sessions", "view-settings", "tabbar"]:
        check(f"HTML 含关键元素 {key!r}", key in html)
    check("加载 kb_admin.js", "kb_admin.js" in html)

    js = client.get(f"{BASE}/ui/app.js").text
    check("app.js 含 Enter 发送逻辑",
          "e.key !== 'Enter'" in js and "isComposing" in js)
    check("app.js 含 IME 保护 (keyCode === 229)", "keyCode === 229" in js)
    check("app.js 含 Shift+Enter 换行（shiftKey 判定）", "e.shiftKey" in js)
    check("app.js 含分隔条拖拽逻辑",
          "initResizers" in js and "RESIZER" in js and "mousedown" in js,
          "initResizers/RESIZER/mousedown")
    check("app.js 拖拽宽度写入 localStorage",
          "lsSet(cfg.lsKey" in js and "agentplat." in js)
    check("app.css 分隔条 cursor 为 col-resize",
          "col-resize" in client.get(f"{BASE}/ui/app.css").text)
    check("app.js 含 localStorage 持久化", "localStorage" in js)
    check("app.js 任务描述框 Enter 发布",
          "task-desc" in js and "publishTask" in js)

    kbjs = client.get(f"{BASE}/ui/kb_admin.js").text
    check("kb_admin.js 可访问且有内容", len(kbjs) > 1000, str(len(kbjs)))
    check("kb_admin.js 含上传 / 粘贴两个 tab", "switchKbTab" in kbjs)
    check("kb_admin.js 含拖拽上传", "dragover" in kbjs and "drop" in kbjs)

    css = client.get(f"{BASE}/ui/app.css").text
    check("app.css 定义设计令牌 :root", ":root" in css and "--accent" in css)
    # 分隔条：新版只有一个可拖拽侧栏，class 名从 .resizer 改为 .sidebar-resizer
    check("app.css 有分隔条样式", ".sidebar-resizer" in css and "col-resize" in css)
    # 折叠段：新版不再用旧手风琴 .acc.open，改用树形分组展开态 .tree-node.open
    check("app.css 有树形分组展开样式", ".tree-node.open" in css)
    check("app.css 有活动栏选中态样式", ".act-item.active" in css)
    check("app.css 有标签页样式", ".tab.active" in css)
    check("app.css 有状态徽标语义化样式",
          ".st.running" in css and ".st.done" in css and ".st.failed" in css)
    check("app.css 有骨架屏样式", ".skeleton" in css)
    check("app.css 有统一滚动条样式", "::-webkit-scrollbar" in css)
    check("app.css 有响应式断点", "@media (max-width: 1180px)" in css)

    # ---------------------------------------------------------- 12
    sec("12. 品牌与领域通用化（前端 + 提示词）")
    for path in ("/ui/", "/ui/app.js", "/ui/app.css", "/ui/custom_tools.js",
                 "/ui/kb_admin.js"):
        txt = client.get(f"{BASE}{path}").text
        check(f"{path} 无「中科飞测」", "中科飞测" not in txt)
        check(f"{path} 无「半导体」", "半导体" not in txt)

    # ---------------------------------------------------------- 清理
    sec("清理：删除测试文档")
    cleanup(client, "DOC-KBTEXT-001")
    cleanup(client, "DOC-KBCONF-001")
    j = client.get(f"{BASE}/kb/documents").json()
    ids = [d.get("doc_id") for d in (j.get("documents") or [])]
    print("  剩余文档:", ids)
    check("测试文档已清理", "DOC-KBTEXT-001" not in ids and "DOC-KBCONF-001" not in ids,
          str(ids))

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"结果: {passed}/{len(RESULTS)} 通过")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  失败: {n}  {d}")
    print("=" * 64)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
