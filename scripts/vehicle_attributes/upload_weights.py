"""Convert training-output/ checkpoints to safetensors and upload them
+ the class JSONs they were trained with to HuggingFace Hub.

v0 ships TWO checkpoints (see services/vehicle-attributes/classifier.py for
why). This script reads both `.pth` files written by the training scripts,
converts to `.safetensors`, and pushes everything to the configured repo.

Usage:
    huggingface-cli login   # one-time, write token

    python scripts/vehicle_attributes/upload_weights.py \\
        --training-dir ./training-output \\
        --repo mangolover/vision-labs-vehicle-attributes
"""
import argparse
import logging
from pathlib import Path

import torch
from huggingface_hub import HfApi, create_repo
from safetensors.torch import save_file


logger = logging.getLogger("upload_weights")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def _convert_color_to_safetensors(pth_path: Path, out_path: Path) -> None:
    """color_head.pth holds only the linear-head state_dict (backbone stays
    at its ImageNet-pretrained state in inference). We re-prefix keys with
    `color_head.` so they line up with classifier.py's ColorModel wrapper.
    """
    ck = torch.load(pth_path, map_location="cpu", weights_only=False)
    head_state = ck["head_state_dict"]
    prefixed = {f"color_head.{k}": v for k, v in head_state.items()}
    save_file(prefixed, str(out_path))
    logger.info(
        f"converted {pth_path.name} → {out_path.name} "
        f"({len(prefixed)} tensors, {sum(t.numel() for t in prefixed.values())} params)"
    )


def _convert_multihead_to_safetensors(pth_path: Path, out_path: Path) -> None:
    """multihead.pth contains the full MultiHeadModel state_dict (backbone
    + 3 heads). Save as-is; classifier.py's MultiHeadModel loads strict.
    """
    ck = torch.load(pth_path, map_location="cpu", weights_only=False)
    state = ck["state_dict"]
    save_file(state, str(out_path))
    logger.info(
        f"converted {pth_path.name} → {out_path.name} "
        f"({len(state)} tensors, "
        f"{sum(t.numel() for t in state.values()) / 1e6:.1f}M params)"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--training-dir", type=Path,
                    default=Path("./training-output"))
    ap.add_argument("--repo", required=True,
                    help="HF Hub repo id, e.g. mangolover/vision-labs-vehicle-attributes")
    ap.add_argument("--color-name", default="color_head_v0",
                    help="Output basename for the color safetensors")
    ap.add_argument("--multihead-name", default="multihead_v0",
                    help="Output basename for the multi-head safetensors")
    ap.add_argument("--private", action="store_true",
                    help="Create the HF repo as private if it doesn't exist")
    args = ap.parse_args()

    td = args.training_dir
    color_pth = td / "color_head.pth"
    multi_pth = td / "multihead.pth"
    if not color_pth.exists():
        raise SystemExit(f"missing {color_pth} — run train_color_head.py first")
    if not multi_pth.exists():
        raise SystemExit(f"missing {multi_pth} — run train_multihead.py first")

    # Ensure repo exists (idempotent).
    create_repo(args.repo, repo_type="model", private=args.private,
                exist_ok=True)

    # Convert + upload both checkpoints.
    color_st = td / f"{args.color_name}.safetensors"
    multi_st = td / f"{args.multihead_name}.safetensors"
    _convert_color_to_safetensors(color_pth, color_st)
    _convert_multihead_to_safetensors(multi_pth, multi_st)

    api = HfApi()
    for f in (color_st, multi_st):
        logger.info(f"uploading {f.name} to {args.repo}")
        api.upload_file(
            path_or_fileobj=str(f),
            path_in_repo=f.name,
            repo_id=args.repo,
            repo_type="model",
        )

    # Class JSONs — uploaded under classes/ so service.py's lazy download
    # path can pull them. These are what the model was actually trained
    # against, replacing the stubs that ship in the service image.
    class_files = [
        "color_classes.json",
        "model_classes.json", "make_classes.json", "body_classes.json",
        "make_to_models.json",
    ]
    for name in class_files:
        path = td / name
        if not path.exists():
            logger.warning(f"missing {path}, skipping")
            continue
        logger.info(f"uploading classes/{name}")
        api.upload_file(
            path_or_fileobj=str(path),
            path_in_repo=f"classes/{name}",
            repo_id=args.repo,
            repo_type="model",
        )

    logger.info(f"done — repo: https://huggingface.co/{args.repo}")


if __name__ == "__main__":
    main()
