"""Manual retrain orchestrator for the COLOR classifier head.

WORKFLOW:
    docker compose exec vehicle-attributes-cam1 \\
        python /workspace/scripts/vehicle_attributes/retrain_attributes.py
    # IMPORTANT: use `python` (NOT `python3`) — the va image has both:
    #   python  → python3.11 with cv2/torch/timm installed
    #   python3 → python3.10 (system default), no packages
    # The Dockerfile's CMD also uses `python` for the same reason.

What it does:
    1. Walks /data/snapshots/vehicles for user-labeled tracks
    2. Reports color label count + class distribution
    3. Prompts: proceed with color retrain?
    4. Fine-tunes the color head:
        a. Loads color_head_v0.safetensors as warm-start
        b. Holds out 20% as val set
        c. Trains 8 epochs (low LR, early stop on val plateau)
        d. Reports baseline (current model on val) vs new (best epoch on val)
    5. Prompts to deploy if val acc improved (refuses deploy on regression)
    6. Writes a tiny "deploy hint" shell file the user runs on the host
       (env update + force-recreate). The script itself can't modify .env
       or restart containers — those operations live outside the va
       container's permission surface by design.

WHY COLOR ONLY (PR2):
    Color head is a standalone safetensors file with a single linear layer
    — clean to retrain in isolation. Body head lives INSIDE the multihead
    safetensors alongside make + model, so deploying a new body head means
    re-emitting a full multihead file with the new body weights merged
    against unchanged make+model weights. Separate concern → PR2b.

WHY MANUAL (vs auto-deploy):
    Each retrain is rare + irreversible enough that a human confirm makes
    sense. Auto-deploy without seeing val acc would risk a small-data
    overfit silently degrading predictions for weeks.

WHY HERE (in the va container):
    Has torch + the existing model classes + GPU access + /models bind
    mount + /data/snapshots access. Dashboard has none of those. Running
    training inside va does pause inference for the duration (~5-15 min
    depending on label count); acceptable for a manual operation.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Configure logging BEFORE we import finetune_heads — the finetune module's
# logger.info calls (per-epoch loss + accuracy) need a handler attached or
# they're silently dropped. basicConfig at import time is enough; the
# finetune module's logger inherits from the root.
logging.basicConfig(level=logging.INFO, format="%(message)s")

sys.path.insert(0, str(Path(__file__).parent))
from collect_labels import collect, summarize  # noqa: E402
from finetune_heads import finetune  # noqa: E402


def _confirm(prompt: str, default: str = "n") -> bool:
    """Prompt user for y/n with a default. Returns True for yes."""
    yn = "[Y/n]" if default == "y" else "[y/N]"
    ans = input(f"{prompt} {yn} ").strip().lower()
    if not ans:
        ans = default
    return ans in ("y", "yes")


def _print_section(title: str):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def _write_deploy_hint(hints_path: Path, hints: list[str]):
    """Write a small file telling the user what to run on the host to deploy.

    The script can't touch .env (dashboard owns that mount) or recreate
    containers (orchestrator owns the docker socket), so we emit a recipe
    instead. The user runs it from the project root on the host.
    """
    hints_path.parent.mkdir(parents=True, exist_ok=True)
    content = [
        "# To deploy the retrained head(s), run these from the project root:",
        "",
    ] + hints + [
        "",
        "# Then verify in the next track flush:",
        "#   tail -f $(docker compose logs vehicle-attributes-cam1 2>&1 -f) | grep 'classifier_version'",
        "",
    ]
    hints_path.write_text("\n".join(content))
    print(f"\n  → Deploy commands written to {hints_path}")


# Each head's per-pipeline knobs. Keeps `_train_one_head` short and lets the
# arg-parser default sensibly without N more flags. The output basename uses
# the same versioned suffix the user passes via --version-tag so both heads
# stay in lockstep.
_HEAD_PIPELINES = {
    "color": {
        "summary_count_key": "color_label_count",
        "summary_dist_key": "color_distribution",
        "default_current": "color_head_v0",
        "output_basename": "color_head",   # → color_head_v1.safetensors
        "env_var": "VEHICLE_ATTR_COLOR_MODEL",
        "rollback_default": "color_head_v0",
    },
    "body": {
        "summary_count_key": "body_label_count",
        "summary_dist_key": "body_distribution",
        "default_current": "multihead_v0",
        "output_basename": "multihead",    # → multihead_v1.safetensors (full merged file)
        "env_var": "VEHICLE_ATTR_MULTIHEAD_MODEL",
        "rollback_default": "multihead_v0",
    },
}


def _train_one_head(head_name: str, args, summary: dict, auto_yes: bool,
                    section_no: int) -> int:
    """Run a single head's pipeline. Returns process-style exit code (0 ok,
    1 fatal). Prints its own section headers."""
    cfg = _HEAD_PIPELINES[head_name]
    n_labels = summary[cfg["summary_count_key"]]
    dist = summary[cfg["summary_dist_key"]]
    print(f"  {head_name}: {n_labels} labels across {len(dist)} "
          f"class{'es' if len(dist) != 1 else ''}")
    for cls, c in list(dist.items())[:8]:
        print(f"    {cls:<25} {c:3d}")
    if len(dist) > 8:
        print(f"    … {len(dist) - 8} more classes")
    print()

    if n_labels < args.min_labels:
        print(f"  ✗ Skipping {head_name}: {n_labels} labels < "
              f"--min-labels={args.min_labels}")
        return 0

    if not auto_yes and not _confirm(
            f"\nProceed with {head_name} training?", default="y"):
        print(f"Skipped {head_name}.")
        return 0

    _print_section(f"{section_no}. Fine-tuning {head_name} head")
    current_basename = (args.current_color if head_name == "color"
                        else args.current_multihead)
    current = args.models_dir / f"{current_basename}.safetensors"
    output = args.models_dir / f"{cfg['output_basename']}_{args.version_tag}.safetensors"
    if head_name == "body" and not current.exists():
        print(f"  ✗ {current} missing — body retrain needs the current "
              f"multihead weights to merge body_head into. Run the va "
              f"container at least once so HF lazy-download populates "
              f"/models, then retry.")
        return 1
    result = finetune(
        head_name=head_name, snapshot_root=args.snapshot_root,
        classes_dir=args.classes_dir,
        current_weights=current if current.exists() else None,
        output=output, epochs=args.epochs, lr=args.lr,
    )

    _print_section(f"{section_no + 1}. {head_name} results")
    if not result.get("trained"):
        print(f"  {head_name}: NOT TRAINED — {result.get('error')}")
        return 1
    baseline = result.get("baseline_val_acc")
    best = result.get("best_val_acc")
    b_str = f"{baseline*100:.0f}%" if baseline is not None else "N/A"
    n_str = f"{best*100:.0f}%" if best is not None else "N/A"
    print(f"  {head_name}: baseline val_acc={b_str} → new val_acc={n_str}")
    print(f"  train={result['train_count']}, val={result['val_count']}")
    if baseline is None or best is None:
        print("    (no val set — too few labels for a held-out; "
              "deploy decision is yours)")
        improved = None
    elif best > baseline:
        print(f"    ✓ improvement of {(best-baseline)*100:.1f} pp")
        improved = True
    elif best == baseline:
        print("    = no change")
        improved = False
    else:
        print(f"    ✗ REGRESSION of {(baseline-best)*100:.1f} pp "
              f"— refuse to deploy")
        improved = False

    if improved is False:
        print()
        print(f"New weights left at {output}; deploy step skipped. "
              f"To try anyway, manually bump {cfg['env_var']}.")
        return 0

    # Improved or ambiguous (no val) — ask user
    deploy = auto_yes or _confirm(
        f"\n  Deploy new {head_name} weights?", default="y")
    if not deploy:
        print()
        print(f"Not deployed. Weights at {output} for later use.")
        return 0

    _print_section(f"{section_no + 2}. {head_name} deploy commands")
    hints = [
        f"# Activate new {head_name} weights",
        f"sed -i '/^{cfg['env_var']}=/d' .env",
        f"echo '{cfg['env_var']}={cfg['output_basename']}_{args.version_tag}' >> .env",
        "",
        "# Recreate the va containers so they reload from the new env",
        "docker compose up -d --force-recreate "
        "$(docker compose ps --services | grep '^vehicle-attributes-')",
    ]
    hints_path = args.models_dir / f"deploy_{head_name}_{args.version_tag}.sh"
    _write_deploy_hint(hints_path, hints)
    print()
    print("Done. To deploy:")
    print(f"  cat {hints_path}  # review")
    print(f"  bash {hints_path}  # or run the commands by hand from project root")
    print()
    print(f"Rollback (if predictions look worse): edit .env, set "
          f"{cfg['env_var']}={cfg['rollback_default']} (or any earlier "
          f"version), then force-recreate va containers.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot-root", type=Path,
                    default=Path("/data/snapshots/vehicles"))
    ap.add_argument("--classes-dir", type=Path, default=Path("/app/classes"))
    ap.add_argument("--models-dir", type=Path, default=Path("/models"))
    ap.add_argument("--version-tag", default="v1",
                    help="suffix for new model files (color_head_<TAG>.safetensors, "
                         "multihead_<TAG>.safetensors)")
    ap.add_argument("--current-color", default="color_head_v0",
                    help="filename of current color head in /models (no .safetensors)")
    ap.add_argument("--current-multihead", default="multihead_v0",
                    help="filename of current multihead in /models (no .safetensors)")
    ap.add_argument("--heads", default="color,body",
                    help="comma-separated heads to train (subset of color,body)")
    ap.add_argument("--min-labels", type=int, default=20,
                    help="refuse to train a head with fewer labels than this "
                         "(prevents wildly-overfit pseudo-models)")
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--yes", action="store_true",
                    help="answer 'y' to all prompts (for automation/testing)")
    args = ap.parse_args()

    auto_yes = args.yes
    heads = [h.strip() for h in args.heads.split(",") if h.strip()]
    for h in heads:
        if h not in _HEAD_PIPELINES:
            print(f"unknown head {h!r}; supported: {list(_HEAD_PIPELINES)}")
            return 1

    _print_section("1. Scanning labeled tracks")
    tracks = collect(args.snapshot_root)
    summary = summarize(tracks)
    print(f"Found {summary['total_labeled_tracks']} labeled tracks.")
    print()

    # Each head gets three sections allocated (Fine-tune / Results / Deploy)
    # — start at section 2 so the user sees a clean numbering.
    section_no = 2
    rc = 0
    for h in heads:
        head_rc = _train_one_head(h, args, summary, auto_yes, section_no)
        rc = head_rc if head_rc else rc
        section_no += 3
    return rc


if __name__ == "__main__":
    sys.exit(main())
