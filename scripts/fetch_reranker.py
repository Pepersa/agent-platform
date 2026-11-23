"""补齐 bge-reranker-v2-m3 的 model.safetensors（真实文件名已探测确认）。

要点：
  - 仓库真实权重名是 ``model.safetensors``（不是 pytorch_model.bin），大小 2271071852 字节。
  - 目录里已存在的 ``model.safetensors.incomplete`` 若是同一文件的前缀（用
    safetensors 头校验），直接复用为断点，避免重下 693MB。
  - 剩余部分按 N 段并行 Range 下载，每段独立分片文件，支持断点续传；
    遇 stall（字节数不变）或异常时指数退避重连。
  - 所有分片完成后按序拼接为 model.safetensors，校验字节数与 safetensors 头，
    最后删除 .incomplete 与 .shard* 残留。

用法：python scripts\\fetch_reranker.py
"""
from __future__ import annotations

import json
import os
import struct
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REPO = "BAAI/bge-reranker-v2-m3"
FILENAME = "model.safetensors"
DEST_DIR = os.environ.get("BGE_RERANKER_DIR", r"E:\model\models\bge-reranker-v2-m3")
MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]
UA = {"User-Agent": "Mozilla/5.0"}
CHUNK = 1 << 20
THREADS = int(os.environ.get("BGE_SHARD_THREADS", "8"))

_lock = threading.Lock()
_progress = {"done": 0}


def log(msg: str) -> None:
    print(msg, flush=True)


def resolve(base: str, retries: int = 10):
    """解析最终 CDN 地址与总大小（HEAD 失败则退化为 Range 探测）。"""
    url = f"{base}/{REPO}/resolve/main/{FILENAME}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA, method="HEAD")
            with urllib.request.urlopen(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length", 0))
                if total:
                    return r.geturl(), total
        except Exception:  # noqa: BLE001
            pass
        try:
            req = urllib.request.Request(url, headers=UA)
            req.add_header("Range", "bytes=0-0")
            with urllib.request.urlopen(req, timeout=60) as r:
                cr = r.headers.get("Content-Range", "")
                if "/" in cr:
                    total = int(cr.split("/")[-1])
                    if total:
                        return r.geturl(), total
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3 * (attempt + 1))
    return None, 0


def fetch_range(resolve_url: str, start: int, end: int, part_path: str,
                idx: int, total_parts: int, retries: int = 500) -> bool:
    """下载 [start, end] 到独立分片文件，支持断点续传与指数退避。"""
    want = end - start + 1
    have = os.path.getsize(part_path) if os.path.exists(part_path) else 0
    if have > want:                      # 分片损坏（过长）→ 重来
        os.remove(part_path)
        have = 0
    stall_count = 0
    backoff = 2.0
    last_report = time.time()

    for _ in range(retries):
        if have >= want:
            break
        try:
            req = urllib.request.Request(resolve_url, headers=UA)
            req.add_header("Range", f"bytes={start + have}-{end}")
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status != 206:
                    # CDN 忽略 Range 或重定向后状态异常 → 重新 resolve
                    stall_count += 1
                    time.sleep(min(backoff, 60.0))
                    backoff = min(backoff * 1.5, 60.0)
                    continue
                before = have
                with open(part_path, "ab" if have else "wb") as f:
                    while True:
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                        if time.time() - last_report > 15:
                            last_report = time.time()
                            with _lock:
                                log(f"    shard{idx} {have / 1e6:.0f}/{want / 1e6:.0f} MB "
                                    f"({have / want * 100:.0f}%)")
                    f.flush()
                    os.fsync(f.fileno())
                if have > before:
                    stall_count = 0
                    backoff = 2.0
                    continue
                stall_count += 1
        except Exception:  # noqa: BLE001
            stall_count += 1

        wait = min(backoff, 60.0)
        if stall_count % 5 == 1:
            log(f"    shard{idx} stall@{have / 1e6:.0f}MB, 退避 {wait:.0f}s")
        time.sleep(wait)
        backoff = min(backoff * 1.5, 60.0)

    if have < want:
        log(f"    shard{idx} 最终失败 {have}/{want}")
        return False
    with _lock:
        _progress["done"] += 1
        log(f"    shard {_progress['done']}/{total_parts} 完成")
    return True


def safetensors_header(path: str, nbytes: int = 16) -> dict | None:
    """读取 safetensors 头并校验是合法 JSON + 含 tensor 名称。"""
    try:
        with open(path, "rb") as f:
            raw = f.read(8)
            (hlen,) = struct.unpack("<Q", raw)
            if not (0 < hlen < 10_000_000):
                return None
            head = f.read(hlen)
        meta = json.loads(head.decode("utf-8"))
        if not isinstance(meta, dict):
            return None
        names = [k for k in meta if k != "__metadata__"]
        if not names:
            return None
        return {"header_len": hlen, "tensors": len(names),
                "sample": names[:3]}
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    os.makedirs(DEST_DIR, exist_ok=True)
    dest = os.path.join(DEST_DIR, FILENAME)
    incomplete = dest + ".incomplete"

    if os.path.exists(dest):
        log(f"[skip] {dest} 已存在 ({os.path.getsize(dest)} 字节)")
        return 0

    total = 0
    for base in MIRRORS:
        cdn_url, size = resolve(base)
        if size:
            total = size
            log(f"resolve {base} -> {size} 字节")
            resolve_url = f"{base}/{REPO}/resolve/main/{FILENAME}"
            break
    if not total:
        log("无法解析文件大小，失败。")
        return 1

    # ---- 复用 .incomplete 前缀 ----
    prefix_len = 0
    if os.path.exists(incomplete):
        size_inc = os.path.getsize(incomplete)
        hdr = safetensors_header(incomplete)
        if hdr and size_inc < total:
            prefix_len = size_inc
            log(f"复用 .incomplete 前缀 {size_inc} 字节，头校验通过 {hdr}")
        else:
            log(f".incomplete 校验不通过（hdr={hdr}），丢弃重下")

    start = prefix_len
    remaining = total - start
    n = THREADS
    seg = (remaining + n - 1) // n
    ranges = []
    for i in range(n):
        s = start + i * seg
        e = min(total - 1, s + seg - 1)
        if s <= e:
            ranges.append((s, e, i))

    _progress["done"] = 0
    log(f"待下载 {remaining / 1e6:.0f} MB，{len(ranges)} 分片")

    ok = True
    with ThreadPoolExecutor(max_workers=len(ranges)) as ex:
        futs = [ex.submit(fetch_range, resolve_url, s, e, f"{dest}.shard{i}", i, len(ranges))
                for s, e, i in ranges]
        for f in futs:
            if not f.result():
                ok = False

    if not ok:
        log("分片下载未全部成功，保留分片供续传。")
        return 1

    # ---- 拼接 ----
    log("拼接中 ...")
    with open(dest, "wb") as out:
        if prefix_len:
            with open(incomplete, "rb") as f:
                while True:
                    b = f.read(CHUNK)
                    if not b:
                        break
                    out.write(b)
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
    if size != total:
        log(f"[size mismatch] {size} != {total}")
        os.remove(dest)
        return 1

    hdr = safetensors_header(dest)
    if not hdr:
        log("[header invalid] 拼接结果头非法")
        return 1
    log(f"[OK] {dest} ({size} 字节) 头校验 {hdr}")

    if os.path.exists(incomplete):
        os.remove(incomplete)
        log("已删除残留 .incomplete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
