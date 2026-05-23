"""Walk per-track snapshot dirs and emit a manifest of user-labeled crops.

Reads every `metadata.json` under {SNAPSHOT_ROOT}/{cam}/{date}/{vehicle_*}/
and pulls out tracks where `user_labels` is populated by the dashboard's
Phase 4 labeling UI. Returns a list of (image_path, labels) pairs that
the finetune script consumes.

Why this is its own module + script (instead of inline in finetune):
  - Reusable from the orchestrator (retrain_attributes.py) AND from a
    future dashboard endpoint that wants to surface label counts in the
    UI ("234 tracks labeled · 87 color · 102 body · 41 make · 8 model").
  - Easy to unit-test the walk + filter logic without spinning up torch.

Default SNAPSHOT_ROOT is `/data/snapshots/vehicles` — the path mounted
inside the vehicle-attributes container via the qnap-snapshots volume.
Override with `--snapshot-root` for testing against a tmp tree.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger("collect_labels")


@dataclass
class LabeledTrack:
    """One labeled track's data — what the finetune script consumes."""
    image_path: Path            # hero.jpg (or angle_*.jpg if hero missing)
    color: str | None           # one of color_classes.json or None
    body_type: str | None       # one of body_classes.json or None
    make: str | None            # free text or None
    model: str | None           # free text or None
    skipped: bool               # if true, exclude from training
    track_id: str               # for logging / debugging
    camera: str
    date: str


def collect(snapshot_root: Path) -> list[LabeledTrack]:
    """Walk the snapshot tree and return every track with a user_labels block.

    Skipped tracks are excluded — they're not training data.
    """
    out: list[LabeledTrack] = []
    if not snapshot_root.is_dir():
        logger.warning(f"snapshot root does not exist: {snapshot_root}")
        return out

    for cam_dir in snapshot_root.iterdir():
        if not cam_dir.is_dir():
            continue
        # Date dir names are YYYY-MM-DD (10 chars). The legacy flat layout
        # also had date dirs directly under snapshot_root, but the modern
        # per-camera layout puts them under cam_dir. We only look at
        # per-camera layout because Phase 1 vehicle-attributes always uses it.
        for date_dir in cam_dir.iterdir():
            if not date_dir.is_dir() or len(date_dir.name) != 10:
                continue
            for track_dir in date_dir.iterdir():
                if not track_dir.is_dir() or not track_dir.name.startswith("vehicle_"):
                    continue
                meta_path = track_dir / "metadata.json"
                if not meta_path.exists():
                    continue
                try:
                    meta = json.loads(meta_path.read_text())
                except (OSError, ValueError) as e:
                    logger.warning(f"unreadable {meta_path}: {e}")
                    continue
                ul = meta.get("user_labels")
                if not ul:
                    continue
                if ul.get("skipped"):
                    continue  # not training data
                # Pick hero.jpg if present, else first angle_*.jpg
                hero = track_dir / "hero.jpg"
                if hero.exists():
                    img_path = hero
                else:
                    angles = sorted(track_dir.glob("angle_*.jpg"))
                    if not angles:
                        logger.debug(f"no crops in {track_dir}, skipping")
                        continue
                    img_path = angles[0]
                out.append(LabeledTrack(
                    image_path=img_path,
                    color=ul.get("color") or None,
                    body_type=ul.get("body_type") or None,
                    make=ul.get("make") or None,
                    model=ul.get("model") or None,
                    skipped=False,
                    track_id=meta.get("track_id", track_dir.name),
                    camera=cam_dir.name,
                    date=date_dir.name,
                ))
    return out


def summarize(tracks: list[LabeledTrack]) -> dict:
    """Compute per-field counts. Useful for the UI + a sanity check before
    training (e.g., refuse to train color if all labels are white)."""
    color_counts = Counter(t.color for t in tracks if t.color)
    body_counts = Counter(t.body_type for t in tracks if t.body_type)
    make_counts = Counter(t.make for t in tracks if t.make)
    model_counts = Counter(t.model for t in tracks if t.model)
    return {
        "total_labeled_tracks": len(tracks),
        "color_label_count": sum(color_counts.values()),
        "body_label_count": sum(body_counts.values()),
        "make_label_count": sum(make_counts.values()),
        "model_label_count": sum(model_counts.values()),
        "color_distribution": dict(color_counts.most_common()),
        "body_distribution": dict(body_counts.most_common()),
        "make_distribution": dict(make_counts.most_common()),
        "model_distribution": dict(model_counts.most_common()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--snapshot-root", type=Path,
        default=Path("/data/snapshots/vehicles"),
    )
    ap.add_argument("--json", action="store_true",
                    help="emit the manifest as JSON to stdout")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    tracks = collect(args.snapshot_root)
    summary = summarize(tracks)

    if args.json:
        print(json.dumps({
            "summary": summary,
            "tracks": [
                {
                    "image_path": str(t.image_path),
                    "color": t.color, "body_type": t.body_type,
                    "make": t.make, "model": t.model,
                    "track_id": t.track_id, "camera": t.camera, "date": t.date,
                }
                for t in tracks
            ],
        }, indent=2))
        return

    print(f"Found {summary['total_labeled_tracks']} labeled tracks "
          f"(across all cameras, all dates).")
    print()
    for head in ("color", "body", "make", "model"):
        n = summary[f"{head}_label_count"]
        dist = summary[f"{head}_distribution"]
        print(f"  {head:<6} labels: {n:3d}")
        for cls, c in list(dist.items())[:8]:
            print(f"    {cls:<25} {c:3d}")
        if len(dist) > 8:
            print(f"    … {len(dist) - 8} more classes")
        print()


if __name__ == "__main__":
    main()
