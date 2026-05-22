"""Upload the trained multi-head weights + class JSONs to HuggingFace Hub.

Usage:
    python upload_weights.py \\
        --checkpoint ./training-output/convnext_tiny_v0.safetensors \\
        --classes-dir services/vehicle-attributes/classes \\
        --repo gammahazard/vision-labs-vehicle-attributes
"""
import argparse
from pathlib import Path
from huggingface_hub import HfApi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", type=Path, required=True)
    ap.add_argument("--classes-dir", type=Path, required=True)
    ap.add_argument("--repo", type=str, required=True)
    args = ap.parse_args()

    api = HfApi()
    api.upload_file(
        path_or_fileobj=str(args.checkpoint),
        path_in_repo=args.checkpoint.name,
        repo_id=args.repo,
        repo_type="model",
    )
    for cls_file in args.classes_dir.glob("*.json"):
        api.upload_file(
            path_or_fileobj=str(cls_file),
            path_in_repo=f"classes/{cls_file.name}",
            repo_id=args.repo,
            repo_type="model",
        )
    print(f"uploaded {args.checkpoint.name} + classes to {args.repo}")


if __name__ == "__main__":
    main()
