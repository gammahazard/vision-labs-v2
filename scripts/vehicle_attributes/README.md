# Vehicle attributes — training v0 weights

One-time training pipeline that produces `convnext_tiny_v0.safetensors`
for upload to `gammahazard/vision-labs-vehicle-attributes` on HF Hub.

## Requirements

- NVIDIA GPU with ~8 GB VRAM (3070+, 4070+)
- ~50 GB free disk for the datasets
- ~2-4 hours of GPU time end-to-end

## One-time setup

```bash
pip install torch torchvision timm==1.0.11 huggingface_hub safetensors \
            datasets pillow numpy
```

## Step 1: train the color head on VeRi-776

```bash
python scripts/vehicle_attributes/train_color_head.py \
       --output ./training-output --epochs 10
```

Runtime: ~90 min on RTX 3090. Outputs `color_head.pth`.

## Step 2: train the multi-head model on Stanford Cars-196

```bash
python scripts/vehicle_attributes/train_multihead.py \
       --color-head-checkpoint ./training-output/color_head.pth \
       --output ./training-output --epochs 15
```

Runtime: ~2 hr on RTX 3090. Outputs `convnext_tiny_v0.safetensors` AND
regenerates the class JSONs in `services/vehicle-attributes/classes/`.

## Step 3: upload

```bash
python scripts/vehicle_attributes/upload_weights.py \
       --checkpoint ./training-output/convnext_tiny_v0.safetensors \
       --classes-dir services/vehicle-attributes/classes \
       --repo gammahazard/vision-labs-vehicle-attributes
```

Requires `huggingface-cli login` first.

## Step 4: flip the flag

Edit `.env`:

```
ENABLE_CLASSIFIER=1
```

```bash
docker compose build vehicle-attributes-cam1
docker compose up -d --force-recreate vehicle-attributes-cam1
```

First boot downloads weights from HF Hub into the `vehicle-attribute-models` volume.

---

# Per-operator retrain (Phase 4) — fine-tune color + body on your own labels

The v0 weights above are trained on public datasets (VeRi-776, Stanford
Cars-196). They're a decent generic baseline, but every install sees its
own cameras, angles, and lighting. This flow lets you label real tracks
from your own cameras and fine-tune the **color** and **body** heads on
them — no GPU box or dataset download needed, it runs inside the live
`vehicle-attributes-cam1` container.

> **make/model are intentionally NOT retrained.** They're long-tail +
> free-text; a residential camera sees too few distinct examples. The
> labeling UI still accepts make/model (stored in metadata.json) but
> nothing trains on them. Focus your labeling on color + body.

## The loop: label → retrain → deploy

### 1. Label (ongoing, in the dashboard)

Browse tab → pick a day → **"📸 Vehicle crops taken"** modal. Per track:
set **color** + **body** from the dropdowns (make/model optional free
text), hit **Save**. Use **✕** to drop a single bad crop, **Delete
track** for an unsalvageable one, **Skip** for tracks you can't judge.

Weight your effort toward the **starved classes** — labeling 40 more
white pickups barely moves accuracy; 10 more of a thin class (e.g. a
rare color, or van/hatchback for body) moves it a lot.

### 2. (Optional) Check how many labels you've banked

```bash
docker compose exec vehicle-attributes-cam1 \
  python /workspace/scripts/vehicle_attributes/collect_labels.py
```

Prints the per-head, per-class distribution. Rule of thumb: wait until
**~20–30 new labels** since the last retrain. The held-out val set is
small (~20% of labels), so fewer than that is mostly noise.

### 3. Run the retrain (interactively, in your own terminal)

```bash
docker compose exec vehicle-attributes-cam1 \
  python /workspace/scripts/vehicle_attributes/retrain_attributes.py
```

No flags needed — defaults are `--heads color,body`, `--classes-dir
/app/classes`, `--epochs 8`, `--min-labels 20`, 20% held-out val. What
it does, per head:

1. Scans labels, prints the distribution.
2. Prompts **"Proceed with <head> training? [Y/n]"** → `y`. Trains
   ~5 min, prints per-epoch val accuracy.
3. Prints **"<head>: baseline X% → new Y%"** and whether it improved.
   **If it regressed it refuses to deploy** — that's the safety gate.
4. If it improved, prompts **"Deploy new <head> weights? [Y/n]"**.
   `n` = just save the new weights to `/models/` for now; `y` = also
   write the env var + a `/models/deploy_<head>_v1.sh` recipe.

Scope to one head with `--heads color` or `--heads body` if you only
want to train one.

**Two gotchas:**
- Use `python`, **not** `python3` — only `python` has cv2/torch/timm in
  that container.
- Run it **without** `-T` or any input pipe so the `y/n` prompts work.

### 4. Activate the new weights

This is the step that actually changes live predictions — needed even if
you answered `y` to deploy, because the running container holds the old
weights in memory until recreated:

```bash
docker compose up -d --force-recreate vehicle-attributes-cam1
```

New weights load on the next vehicle detection.

### Rollback

If predictions look worse, edit `.env` and point back at v0:

```
VEHICLE_ATTR_COLOR_MODEL=color_head_v0
VEHICLE_ATTR_MULTIHEAD_MODEL=multihead_v0
```

then `docker compose up -d --force-recreate vehicle-attributes-cam1`.
v0 is always available from HF Hub.

## How it works under the hood

- **Each retrain starts fresh from v0**, not from your last v1 — it's
  "fine-tune the public baseline on *all* my labels to date." This avoids
  compounding overfit across runs, and means it overwrites the
  `color_head_v1.safetensors` / `multihead_v1.safetensors` files each
  time. Re-run as often as you like.
- **Color** is a frozen ImageNet backbone + linear head → output is a
  standalone `color_head_v1.safetensors`.
- **Body** lives inside the multihead alongside make + model, on the
  Stanford-Cars-fine-tuned backbone. The retrain fine-tunes only the body
  head, then re-emits a full `multihead_v1.safetensors` with `body_head.*`
  replaced and `backbone` / `make_head` / `model_head` passed through
  bit-identical (`finetune_heads._save_merged_multihead`).
- Class-weighted cross-entropy handles class imbalance; warm-start + low
  LR (1e-4) + early stopping keep a small label set from overfitting.

Scripts: `collect_labels.py` (walks metadata.json for user_labels),
`finetune_heads.py` (the actual training), `retrain_attributes.py` (the
orchestrator CLI you run).
