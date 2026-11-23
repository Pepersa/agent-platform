"""通过 Range 断点续传下载 BGE 权重（HF 镜像 / 官方）。

背景与坑（2026-11-21 实测）：
1. BAAI/bge-m3 **只有 pytorch_model.bin（2.27GB），没有 safetensors**；
2. 直连大文件容易断流，必须 Range 续传；
3. ⚠️ **重定向到 CDN 时 Range 头可能丢失** → 服务端返回 200（整文件）而非 206（分片）。
   若此时按"追加"写，会把整文件拼到已有内容后面 → 文件损坏；
   若按"覆盖"写，会把已下的部分清零 → 无限重下。
   正确做法：**先用 HEAD 解析出最终 CDN 地址，再直接对该地址做 Range 请求**；
   CDN 地址过期（403）时重新解析。

用法：python scripts\fetch_bge.py
"""
import os
import sys
import time
import urllib.error
import urllib.request

MODEL_ROOT = os.environ.get("MODEL_ROOT", r"E:\model\models")
CHUNK = 1 << 20
UA = {"User-Agent": "Mozilla/5.0"}

# (repo_id, 本地目录名, 文件名列表)
TARGETS = [
    ("BAAI/bge-m3", "bge-m3", [
        "config.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "sentence_bert_config.json",
        "modules.json", "1_Pooling/config.json",
        "sentencepiece.bpe.model", "colbert_linear.pt", "sparse_linear.pt",
        "pytorch_model.bin",
    ]),
    ("BAAI/bge-reranker-v2-m3", "bge-reranker-v2-m3", [
        "config.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "sentence_bert_config.json",
        "pytorch_model.bin",
    ]),
]

MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]


def resolve(repo: str, filename: str, base: str) -> tuple[str, int] | None:
    """解析最终 CDN 地址与文件总大小（HEAD 请求跟随重定向）。"""
    url = f"{base}/{repo}/resolve/main/{filename}"
    try:
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length", 0))
            return r.geturl(), total
    except Exception as e:                                      # noqa: BLE001
        print(f"    [resolve fail] {base}: {type(e).__name__}: {str(e)[:70]}", flush=True)
        return None


def download(repo: str, filename: str, dest_dir: str) -> bool:
    dest = os.path.join(dest_dir, filename.replace("/", os.sep))
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  [skip] {filename} ({os.path.getsize(dest)/1e6:.1f} MB)", flush=True)
        return True

    part = dest + ".part"

    for base in MIRRORS:
        resolved = resolve(repo, filename, base)
        if not resolved:
            continue
        cdn_url, total = resolved

        for attempt in range(120):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if total and have >= total:
                os.replace(part, dest)
                print(f"  [OK] {filename} ({have/1e6:.1f} MB)", flush=True)
                return True

            # 直接对 CDN 地址发 Range，避免重定向丢头
            try:
                req = urllib.request.Request(cdn_url, headers=UA)
                if have:
                    req.add_header("Range", f"bytes={have}-")
                with urllib.request.urlopen(req, timeout=60) as r:
                    partial = r.status == 206
                    if have and not partial:
                        # 服务端忽略了 Range：只能从头下，丢弃旧进度
                        print(f"    [restart] {filename}: CDN 未返回 206，从头下载", flush=True)
                        have = 0
                    mode = "ab" if (have and partial) else "wb"
                    with open(part, mode) as f:
                        while True:
                            chunk = r.read(CHUNK)
                            if not chunk:
                                break
                            f.write(chunk)
                            have += len(chunk)
                        f.flush()
                        os.fsync(f.fileno())
            except urllib.error.HTTPError as e:
                if e.code in (403, 401, 400):
                    # CDN 地址过期 → 重新解析
                    print(f"    [expired] {filename}: {e.code}，重新解析 CDN 地址", flush=True)
                    new = resolve(repo, filename, base)
                    if new:
                        cdn_url, total = new
                    time.sleep(2)
                    continue
                print(f"    [retry {attempt}] {filename}: HTTP {e.code}", flush=True)
                time.sleep(2)
                continue
            except Exception as e:                              # noqa: BLE001
                print(f"    [retry {attempt}] {filename}: {type(e).__name__}: {str(e)[:60]}",
                      flush=True)
                time.sleep(2)
                continue

            if total and have >= total:
                os.replace(part, dest)
                print(f"  [OK] {filename} ({have/1e6:.1f} MB)", flush=True)
                return True
            print(f"    ... {filename} {have/1e6:.0f}/{total/1e6:.0f} MB, 续传", flush=True)
            time.sleep(1)

    print(f"  [FAIL] {filename}", file=sys.stderr)
    return False


def main() -> int:
    os.makedirs(MODEL_ROOT, exist_ok=True)
    failed = []
    for repo, dirname, files in TARGETS:
        dest_dir = os.path.join(MODEL_ROOT, dirname)
        print(f"\n=== {repo} -> {dest_dir} ===", flush=True)
        for fn in files:
            if not download(repo, fn, dest_dir):
                failed.append(f"{repo}/{fn}")
    if failed:
        print(f"\n失败: {failed}", file=sys.stderr)
        return 1
    print("\n全部完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
