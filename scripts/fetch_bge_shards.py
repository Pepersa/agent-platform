"""BGE 权重的多线程分片下载（每个线程下一段 Range），显著提升不稳定链路下的速度。

背景：单连接下载 HF CDN 大文件在部分网络下会反复停滞（实测 10MB/s 后掉到 0）。
把文件切成 N 段并行拉，最后拼接，规避单连接 stall。

用法：python scripts\fetch_bge_shards.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

MODEL_ROOT = os.environ.get("MODEL_ROOT", r"E:\model\models")
UA = {"User-Agent": "Mozilla/5.0"}
CHUNK = 1 << 20

TARGETS = [
    ("BAAI/bge-m3", "bge-m3", "pytorch_model.bin"),
    ("BAAI/bge-reranker-v2-m3", "bge-reranker-v2-m3", "pytorch_model.bin"),
]
MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]
THREADS = int(os.environ.get("BGE_SHARD_THREADS", "8"))

_lock = threading.Lock()
_progress = {"done": 0}


def resolve(repo: str, filename: str, base: str, retries: int = 8):
    """解析最终 CDN 地址与文件大小。

    HEAD 可能被 CDN 拒绝（403/405），降级用 GET + Range: bytes=0-0 探测。
    """
    url = f"{base}/{repo}/resolve/main/{filename}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA, method="HEAD")
            with urllib.request.urlopen(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length", 0))
                if total:
                    return r.geturl(), total
        except Exception:                                     # noqa: BLE001
            pass
        # 降级：GET Range 探测
        try:
            req = urllib.request.Request(url, headers=UA)
            req.add_header("Range", "bytes=0-0")
            with urllib.request.urlopen(req, timeout=60) as r:
                cr = r.headers.get("Content-Range", "")
                if "/" in cr:
                    total = int(cr.split("/")[-1])
                    if total:
                        return r.geturl(), total
        except Exception:                                     # noqa: BLE001
            pass
        time.sleep(3 * (attempt + 1))
    return None


def fetch_range(cdn_url: str, start: int, end: int, out_path: str, idx: int,
                total: int, retries: int = 400) -> bool:
    """下载 [start, end] 区间到独立分片文件。

    CDN 会对同一 IP 的并发连接限流，实测 8 线程会在 ~135MB 处集体 stall。
    策略：检测到 stall（字节数不变）就指数退避后重连，退避上限 60s。
    """
    part = f"{out_path}.shard{idx}"
    want = end - start + 1
    have = os.path.getsize(part) if os.path.exists(part) else 0
    stall_count = 0
    backoff = 2.0

    for _ in range(retries):
        if have >= want:
            break
        try:
            req = urllib.request.Request(cdn_url, headers=UA)
            req.add_header("Range", f"bytes={start + have}-{end}")
            with urllib.request.urlopen(req, timeout=45) as r:
                if r.status != 206:
                    return False      # 服务端忽略 Range，放弃分片
                before = have
                with open(part, "ab" if have else "wb") as f:
                    while True:
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                    f.flush()
                    os.fsync(f.fileno())
                if have > before:
                    stall_count = 0
                    backoff = 2.0
                    continue
                stall_count += 1
        except Exception:                                     # noqa: BLE001
            stall_count += 1

        # 停滞或异常 → 退避后重连
        wait = min(backoff, 60.0)
        if stall_count % 10 == 1:
            print(f"    shard{idx} stall@{have / 1e6:.0f}MB, 退避 {wait:.0f}s", flush=True)
        time.sleep(wait)
        backoff = min(backoff * 1.5, 60.0)

    if have < want:
        return False
    with _lock:
        _progress["done"] += 1
        print(f"    shard {idx + 1}/{total} done "
              f"({_progress['done'] / total * 100:.0f}%)", flush=True)
    return True


def download(repo: str, dirname: str, filename: str) -> bool:
    dest_dir = os.path.join(MODEL_ROOT, dirname)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)

    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  [skip] {dirname}/{filename}", flush=True)
        return True

    for base in MIRRORS:
        try:
            cdn_url, total = resolve(repo, filename, base)
        except Exception as e:                                 # noqa: BLE001
            print(f"    [resolve fail] {base}: {type(e).__name__}", flush=True)
            continue
        if total <= 0:
            continue

        n = THREADS
        seg = (total + n - 1) // n
        ranges = []
        for i in range(n):
            s = i * seg
            e = min(total - 1, s + seg - 1)
            if s <= e:
                ranges.append((s, e, i))

        _progress["done"] = 0
        print(f"  {dirname}/{filename}: {total / 1e6:.0f} MB, "
              f"{len(ranges)} 分片, {base}", flush=True)

        ok = True
        with ThreadPoolExecutor(max_workers=len(ranges)) as ex:
            futs = [ex.submit(fetch_range, cdn_url, s, e, dest, i, len(ranges))
                    for s, e, i in ranges]
            for f in futs:
                if not f.result():
                    ok = False

        if ok:
            with open(dest, "wb") as out:
                for i in range(len(ranges)):
                    p = f"{dest}.shard{i}"
                    with open(p, "rb") as f:
                        while True:
                            b = f.read(CHUNK)
                            if not b:
                                break
                            out.write(b)
                    os.remove(p)
            size = os.path.getsize(dest)
            if size == total:
                print(f"  [OK] {dirname}/{filename} ({size / 1e6:.1f} MB)", flush=True)
                return True
            print(f"    [size mismatch] {size} != {total}", flush=True)
            os.remove(dest)
        else:
            print(f"    [shard fail] {dirname}/{filename}", flush=True)

    print(f"  [FAIL] {dirname}/{filename}", file=sys.stderr)
    return False


def main() -> int:
    failed = []
    for repo, dirname, filename in TARGETS:
        print(f"\n=== {repo}/{filename} ===", flush=True)
        if not download(repo, dirname, filename):
            failed.append(f"{repo}/{filename}")
    if failed:
        print(f"\n失败: {failed}", file=sys.stderr)
        return 1
    print("\n全部完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
