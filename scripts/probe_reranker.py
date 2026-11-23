"""探测 bge-reranker-v2-m3 仓库的真实权重文件名与大小。

用 HEAD / Range bytes=0-0 探测 Content-Length，确认正确文件名后再下载。
用法：python scripts\probe_reranker.py
"""
from __future__ import annotations

import json
import urllib.request

REPO = "BAAI/bge-reranker-v2-m3"
MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]
UA = {"User-Agent": "Mozilla/5.0"}


def probe(base: str, filename: str):
    url = f"{base}/{REPO}/resolve/main/{filename}"
    try:
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as r:
            return {"url": r.geturl(), "length": int(r.headers.get("Content-Length", 0)),
                    "status": r.status}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def probe_range(base: str, filename: str):
    url = f"{base}/{REPO}/resolve/main/{filename}"
    try:
        req = urllib.request.Request(url, headers=UA)
        req.add_header("Range", "bytes=0-0")
        with urllib.request.urlopen(req, timeout=60) as r:
            cr = r.headers.get("Content-Range", "")
            total = int(cr.split("/")[-1]) if "/" in cr else 0
            return {"url": r.geturl(), "length": total, "status": r.status}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"https://hf-mirror.com/api/models/{REPO}", headers=UA),
            timeout=60,
        ) as r:
            info = json.loads(r.read().decode("utf-8"))
        print("=== API siblings ===")
        for s in info.get("siblings", []):
            print("  ", s.get("rfilename"))
    except Exception as exc:  # noqa: BLE001
        print("API 失败:", type(exc).__name__, exc)

    for base in MIRRORS:
        print(f"\n=== {base} ===")
        for fn in ["model.safetensors", "pytorch_model.bin", "model.safetensors.index.json"]:
            h = probe(base, fn)
            rg = probe_range(base, fn)
            print(f"  {fn}\n    HEAD : {h}\n    RANGE: {rg}")


if __name__ == "__main__":
    main()
