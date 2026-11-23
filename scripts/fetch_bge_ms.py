"""通过 ModelScope 下载 BGE 模型（国内直连，比 HuggingFace 稳）。

对应 docs\01 §2.7「Embedding 服务」「Reranker」。
用法：python scripts\fetch_bge_ms.py
"""
import os
import sys

MODEL_ROOT = os.environ.get("MODEL_ROOT", r"E:\model\models")

TARGETS = [
    # (modelscope_id,           本地目录名,        需要哪些文件)
    ("BAAI/bge-m3", "bge-m3", [
        "config.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "sentence_bert_config.json",
        "modules.json", "model.safetensors",
    ]),
    ("BAAI/bge-reranker-v2-m3", "bge-reranker-v2-m3", [
        "config.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "sentence_bert_config.json",
        "model.safetensors",
    ]),
]


def main() -> int:
    try:
        from modelscope import snapshot_download
    except ImportError:
        print("modelscope 未安装：pip install modelscope", file=sys.stderr)
        return 1

    failed = []
    for ms_id, dirname, files in TARGETS:
        dest = os.path.join(MODEL_ROOT, dirname)
        print(f"\n=== {ms_id} -> {dest} ===", flush=True)
        try:
            snapshot_download(
                ms_id,
                local_dir=dest,
                allow_file_pattern=files,
                max_workers=4,
            )
            print(f"  [OK] {ms_id}", flush=True)
        except Exception as e:                                  # noqa: BLE001
            print(f"  [FAIL] {ms_id}: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            failed.append(ms_id)

    if failed:
        print(f"\n失败: {failed}", file=sys.stderr)
        return 1
    print("\n全部完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
