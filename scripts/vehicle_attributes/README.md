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
