"""下载 BGE 检索模型权重到本地（避免运行时外网拉取）。

对应 docs\01 §2.7「Embedding 服务」「Reranker」。
用法：python scripts\download_models.py
"""
import os
import sys

from huggingface_hub import snapshot_download

TARGET_ROOT = os.environ.get("MODEL_ROOT", r"E:\model\models")

# 排除不需要的推理格式（ONNX / OpenVINO / TF），只留 PyTorch
IGNORE = [
    "*.onnx", "*.onnx_data", "onnx/*", "onnx_*/*",
    "*openvino*", "*.h5", "*.tflite", "*tf_model*", "*.ot", "*.msgpack",
]


def fetch(repo: str) -> str:
    name = repo.split("/")[-1]
    dest = os.path.join(TARGET_ROOT, name)
    print(f"[download] {repo} -> {dest}", flush=True)
    path = snapshot_download(
        repo_id=repo,
        local_dir=dest,
        ignore_patterns=IGNORE,
    )
    print(f"[OK] {repo}: {path}", flush=True)
    return path


def main() -> int:
    repos = [
        "BAAI/bge-m3",                # Embedding（dense + sparse，1024 维，中英混合）
        "BAAI/bge-reranker-v2-m3",    # Reranker（交叉编码器精排）
    ]
    failed = []
    for repo in repos:
        try:
            fetch(repo)
        except Exception as e:                      # noqa: BLE001
            print(f"[FAIL] {repo}: {type(e).__name__}: {e}", file=sys.stderr)
            failed.append(repo)
    if failed:
        print(f"\n下载失败: {failed}", file=sys.stderr)
        print("检索链将降级为 hash 伪向量模式（语义质量不可用）。", file=sys.stderr)
        return 1
    print("\n全部模型就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
