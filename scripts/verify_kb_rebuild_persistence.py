# -*- coding: utf-8 -*-
"""缺陷验证脚本：UI 导入 → 全量重建 → 仍在且可检索（防退化）。

覆盖：
    1. 导入 → 重建（API）→ 仍在 + 可检索
    2. 导入 → 重建（脚本）→ 仍在 + 可检索
    3. 幂等：连续两次重建，chunk 数 / 文档数一致
    4. 去重：同一 doc_id 同时存在于目录与 doc_source → chunk 不翻倍
    5. 删除不复活：删除后重建，该文档不回来
    6. 目录文档不受影响
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import httpx

BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8010").rstrip("/")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 文档目录 —— **必须与 API 读的是同一个**。
#:   API 的文档目录由 ``AGENT_DATA_DIR`` 决定（``<data_dir>/docs``）；
#:   测试要往里写「去重用例」文件，���必须写到同一个位置，
#:   否则会出现「写进了 A 目录、API 重建时扫的是 B 目录」的假失败。
DOCS_DIR = os.path.join(
    os.environ.get("AGENT_DATA_DIR", os.path.join(ROOT, "data")).rstrip("\\/"),
    "docs")
DOC_ID = "DOC-WXY-888"
MARK = "WXY-888"
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def sec(t):
    print(f"\n=== {t} ===", flush=True)


def snapshot(c):
    j = c.get(f"{BASE}/kb/documents").json()
    return {d["doc_id"]: d for d in j["documents"]}, j["stats"], j["count"]


def main():
    c = httpx.Client(timeout=900.0)

    sec("0. 基线")
    docs0, stats0, count0 = snapshot(c)
    print(f"  count={count0} total_chunks={stats0['total_chunks']}")
    for k, v in docs0.items():
        print(f"    - {k:22} origin={v['origin']:10} chunks={v['chunk_count']}")

    sec("1. 导入带独特数值的文档")
    text = f"""---
doc_id: {DOC_ID}
title: WXY-888 转速测试文档
version: v1.0
security_level: internal
visible_roles: [engineer]
---

# WXY-888 转速测试

WXY-888 转速 7777 rpm，谐振频率 12.345 GHz，标定增益 13.37 dB。
"""
    r = c.post(f"{BASE}/kb/documents", json={"text": text})
    print("  HTTP", r.status_code)
    imp = r.json()
    print("  ", json.dumps(imp, ensure_ascii=False))
    check("导入返回 201", r.status_code == 201, str(r.status_code))
    check(f"doc_id == {DOC_ID}", imp.get("doc_id") == DOC_ID, str(imp.get("doc_id")))
    imported_chunks = int(imp.get("chunk_count") or 0)
    check("chunk_count > 0", imported_chunks > 0, str(imported_chunks))
    docs1, stats1, count1 = snapshot(c)
    check("列表含新文档", DOC_ID in docs1, str(list(docs1)))
    check("origin == upload", docs1.get(DOC_ID, {}).get("origin") == "upload",
          str(docs1.get(DOC_ID, {}).get("origin")))
    print(f"  导入后：count={count1} total_chunks={stats1['total_chunks']}")

    sec("2. 全量重建（POST /kb/reindex）")
    r = c.post(f"{BASE}/kb/reindex")
    print("  HTTP", r.status_code, r.text[:300])
    check("reindex 返回 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        print("  ", json.dumps(r.json(), ensure_ascii=False)[:400])
    docs2, stats2, count2 = snapshot(c)
    print(f"  重建后：count={count2} total_chunks={stats2['total_chunks']}")
    check(f"重建后 {DOC_ID} 仍在列表里", DOC_ID in docs2, str(list(docs2)))
    check("重建后该文档 chunk 数不变",
          docs2.get(DOC_ID, {}).get("chunk_count") == imported_chunks,
          f"{imported_chunks} vs {docs2.get(DOC_ID, {}).get('chunk_count')}")
    rs = c.get(f"{BASE}/kb/search",
               params={"query": f"{MARK} 转速 7777 rpm", "top_k": 6,
                       "max_security_level": "internal", "roles": "engineer"})
    sj = rs.json()
    print(f"  检索命中 {sj.get('hit_count')}")
    for h in (sj.get("hits") or [])[:5]:
        print(f"    - {h.get('doc_id')} {str(h.get('text'))[:70]!r}")
    check("重建后检索能命中该文档",
          any(h.get("doc_id") == DOC_ID for h in (sj.get("hits") or [])),
          str([h.get("doc_id") for h in (sj.get("hits") or [])]))

    sec("3. 全量重建（python scripts/build_index.py --rebuild）")
    env = dict(os.environ)
    env["BGE_M3_PATH"] = env.get("BGE_M3_PATH") or r"E:\model\models\bge-m3"
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, os.path.join("scripts", "build_index.py"), "--rebuild"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=900,
        encoding="utf-8", errors="replace",
    )
    print("  exit code:", p.returncode)
    for line in (p.stdout or "").splitlines():
        if any(k in line for k in ("目录来源", "doc_source 来源", "共 ", "切分完成",
                                   "chunk 总数", "文档数")):
            print("   ", line.strip())
    check("脚本 rebuild 退出码 0", p.returncode == 0, str(p.returncode))
    docs3, stats3, count3 = snapshot(c)
    print(f"  脚本重建后：count={count3} total_chunks={stats3['total_chunks']}")
    check(f"脚本 rebuild 后 {DOC_ID} 仍在列表里", DOC_ID in docs3, str(list(docs3)))
    check("脚本 rebuild 后 chunk 数不变",
          docs3.get(DOC_ID, {}).get("chunk_count") == imported_chunks,
          f"{imported_chunks} vs {docs3.get(DOC_ID, {}).get('chunk_count')}")
    rs = c.get(f"{BASE}/kb/search",
               params={"query": f"{MARK} 转速 7777 rpm", "top_k": 6,
                       "max_security_level": "internal", "roles": "engineer"})
    sj = rs.json()
    check("脚本 rebuild 后检索仍能命中",
          any(h.get("doc_id") == DOC_ID for h in (sj.get("hits") or [])),
          str([h.get("doc_id") for h in (sj.get("hits") or [])]))

    sec("4. 幂等：连续两次重建")
    r = c.post(f"{BASE}/kb/reindex")
    d4, s4, c4 = snapshot(c)
    print(f"  第 1 次：count={c4} total_chunks={s4['total_chunks']}")
    r = c.post(f"{BASE}/kb/reindex")
    d5, s5, c5 = snapshot(c)
    print(f"  第 2 次：count={c5} total_chunks={s5['total_chunks']}")
    check("两次重建 total_chunks 相同", s4["total_chunks"] == s5["total_chunks"],
          f"{s4['total_chunks']} vs {s5['total_chunks']}")
    check("两次重建文档集合相同", set(d4) == set(d5),
          str(sorted(set(d4) ^ set(d5))))
    check("两次重建 doc_source 记录数相同",
          len(d4) == len(d5), f"{len(d4)} vs {len(d5)}")

    sec("5. 去重：同一 doc_id 同时在目录与 doc_source")
    os.makedirs(DOCS_DIR, exist_ok=True)
    dup_path = os.path.join(DOCS_DIR, f"{DOC_ID}.md")
    # ★ 先清掉**上一轮残留**：上一次运行若在第 5 步之后中断
    #   （进程被杀 / 异常退出），这个文件会留在目录里 →
    #   下一轮「删除后重建不复活」必然失败（目录来源会被复活），
    #   且「目录文档数量」也多 1 个 → 3 个假失败。
    try:
        if os.path.isfile(dup_path):
            os.remove(dup_path)
            print(f"  已清理上一轮残留：{dup_path}")
    except OSError:
        pass
    with open(dup_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"  已写入 {dup_path}")
    r = c.post(f"{BASE}/kb/reindex")
    print("  reindex HTTP", r.status_code)
    d6, s6, c6 = snapshot(c)
    print(f"  去重后：count={c6} total_chunks={s6['total_chunks']}")
    print(f"  {DOC_ID} chunk_count = {d6.get(DOC_ID, {}).get('chunk_count')}")
    check("去重后 chunk 数没有翻倍",
          d6.get(DOC_ID, {}).get("chunk_count") == imported_chunks,
          f"期望 {imported_chunks}，实际 {d6.get(DOC_ID, {}).get('chunk_count')}")
    check("去重后总 chunk 数不显著增加",
          s6["total_chunks"] <= s5["total_chunks"] + imported_chunks,
          f"{s5['total_chunks']} -> {s6['total_chunks']}")
    os.remove(dup_path)
    print(f"  已删除 {dup_path}")
    # 目录里不能留下任何本测试用的文件（否则下一轮会看到「多一个目录文档」）
    try:
        if os.path.isfile(dup_path):
            os.remove(dup_path)
    except OSError:
        pass

    sec("6. 删除不复活")
    r = c.delete(f"{BASE}/kb/documents/{DOC_ID}")
    print("  DELETE HTTP", r.status_code, r.text[:200])
    check("删除返回 200", r.status_code == 200, str(r.status_code))
    d7, s7, c7 = snapshot(c)
    check("删除后列表中已无该文档", DOC_ID not in d7, str(list(d7)))
    r = c.post(f"{BASE}/kb/reindex")
    print("  reindex HTTP", r.status_code)
    d8, s8, c8 = snapshot(c)
    print(f"  重建后：count={c8} total_chunks={s8['total_chunks']}")
    check("重建后该文档**没有**复活", DOC_ID not in d8, str(list(d8)))
    rs = c.get(f"{BASE}/kb/search",
               params={"query": f"{MARK} 转速 7777 rpm", "top_k": 6,
                       "max_security_level": "internal", "roles": "engineer"})
    sj = rs.json()
    check("重建后检索也不再命中该文档",
          not any(h.get("doc_id") == DOC_ID for h in (sj.get("hits") or [])),
          str([h.get("doc_id") for h in (sj.get("hits") or [])]))

    sec("7. 目录文档不受影响")
    d9, s9, c9 = snapshot(c)
    dir_ids = [k for k, v in d9.items() if v["origin"] == "directory"]
    print(f"  目录文档 {dir_ids}")
    check("目录文档数量 == 6", len(dir_ids) == 6, str(len(dir_ids)))
    for did in dir_ids:
        check(f"{did} 仍有 chunk", int(d9[did].get("chunk_count") or 0) > 0,
              str(d9[did].get("chunk_count")))
    rs = c.get(f"{BASE}/kb/search",
               params={"query": "良率 口径 bin1", "top_k": 6,
                       "max_security_level": "internal", "roles": "engineer"})
    sj = rs.json()
    print(f"  目录文档检索命中 {sj.get('hit_count')}")
    for h in (sj.get("hits") or [])[:3]:
        print(f"    - {h.get('doc_id')} {str(h.get('text'))[:60]!r}")
    check("目录文档仍可检索",
          any(h.get("doc_id", "").startswith("DOC-YIELD") for h in (sj.get("hits") or [])),
          str([h.get("doc_id") for h in (sj.get("hits") or [])]))

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
