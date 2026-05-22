"""Train a ConvNeXt-Tiny color classifier head on VeRi-776.

Usage:
    python train_color_head.py --output ./training-output --epochs 10
"""
import argparse
import json
from pathlib import Path

import torch
import torch.nn as nn
import timm

COLOR_LABELS = ["yellow", "orange", "green", "gray", "red",
                "blue", "white", "golden", "brown", "black"]


def _load_veri776(data_dir: Path):
    """Load VeRi-776 train split. Download from:
        https://github.com/JDAI-CV/VeRidataset

    Return a torch DataLoader yielding (image_tensor, color_label_int) batches.
    Adapt to your local layout.
    """
    raise NotImplementedError(
        "Adapt this to your local VeRi-776 layout. See "
        "https://github.com/JDAI-CV/VeRidataset for the dataset + color "
        "label format."
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("./datasets/veri776"))
    ap.add_argument("--output", type=Path, default=Path("./training-output"))
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    args = ap.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    backbone = timm.create_model('convnext_tiny', pretrained=True, num_classes=0)
    for p in backbone.parameters():
        p.requires_grad = False
    backbone.train(False).to(device)

    color_head = nn.Linear(backbone.num_features, len(COLOR_LABELS)).to(device)

    train_loader = _load_veri776(args.data_dir)

    opt = torch.optim.AdamW(color_head.parameters(), lr=args.lr)
    criterion = nn.CrossEntropyLoss()

    for epoch in range(args.epochs):
        total_loss = 0.0
        n = 0
        for batch in train_loader:
            imgs, labels = batch[0].to(device), batch[1].to(device)
            with torch.no_grad():
                feats = backbone(imgs)
            logits = color_head(feats)
            loss = criterion(logits, labels)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total_loss += float(loss.item()) * imgs.size(0)
            n += imgs.size(0)
        print(f"epoch {epoch+1}/{args.epochs} — color loss {total_loss/n:.4f}")

    out_path = args.output / "color_head.pth"
    torch.save(color_head.state_dict(), out_path)
    print(f"saved color head to {out_path}")

    (args.output / "color_classes.json").write_text(json.dumps(COLOR_LABELS, indent=2))


if __name__ == "__main__":
    main()
