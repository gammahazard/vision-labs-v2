#!/bin/bash
# ComfyUI entrypoint — downloads required models on first startup
# if they're not already present on the host volume.
#
# Models are downloaded to /app/models/ which is bind-mounted from
# the host (./models/comfyui). This ensures they persist across
# container rebuilds and aren't lost when the volume overrides the
# Dockerfile's /app/models directory.

set -e

MODEL_DIR="/app/models"

# Ensure all required subdirectories exist
mkdir -p "$MODEL_DIR/checkpoints"
mkdir -p "$MODEL_DIR/loras"
mkdir -p "$MODEL_DIR/animatediff_models"
mkdir -p "$MODEL_DIR/animatediff_motion_lora"
mkdir -p "$MODEL_DIR/ipadapter"
mkdir -p "$MODEL_DIR/clip_vision"
mkdir -p "$MODEL_DIR/diffusion_models"
mkdir -p "$MODEL_DIR/text_encoders"
mkdir -p "$MODEL_DIR/vae"
mkdir -p "$MODEL_DIR/wan_loras"

# Symlink wan_loras into loras/ so ComfyUI's LoraLoader can find WAN LoRAs
# via the path "wan_loras/filename.safetensors"
ln -sfn "$MODEL_DIR/wan_loras" "$MODEL_DIR/loras/wan_loras" 2>/dev/null || true

# --- AnimateDiff SDXL motion model (legacy fallback) ---
AD_MODEL="$MODEL_DIR/animatediff_models/mm_sdxl_v10_beta.ckpt"
if [ ! -f "$AD_MODEL" ]; then
    echo "[entrypoint] Downloading AnimateDiff SDXL motion model (~1.2 GB)..."
    wget -q --show-progress -O "$AD_MODEL" \
        "https://huggingface.co/guoyww/animatediff/resolve/main/mm_sdxl_v10_beta.ckpt" || \
        echo "[entrypoint] WARNING: AnimateDiff download failed — video generation will not work"
else
    echo "[entrypoint] AnimateDiff motion model already present"
fi

# --- IP-Adapter Plus Face SDXL ---
IPA_MODEL="$MODEL_DIR/ipadapter/ip-adapter-plus-face_sdxl_vit-h.safetensors"
if [ ! -f "$IPA_MODEL" ]; then
    echo "[entrypoint] Downloading IP-Adapter Plus Face SDXL (~100 MB)..."
    wget -q --show-progress -O "$IPA_MODEL" \
        "https://huggingface.co/h94/IP-Adapter/resolve/main/sdxl_models/ip-adapter-plus-face_sdxl_vit-h.safetensors" || \
        echo "[entrypoint] WARNING: IP-Adapter download failed — character consistency will be unavailable"
else
    echo "[entrypoint] IP-Adapter Plus Face model already present"
fi

# --- CLIP Vision encoder (required by IP-Adapter) ---
CLIP_MODEL="$MODEL_DIR/clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"
if [ ! -f "$CLIP_MODEL" ]; then
    echo "[entrypoint] Downloading CLIP Vision encoder (~3.7 GB)..."
    wget -q --show-progress -O "$CLIP_MODEL" \
        "https://huggingface.co/h94/IP-Adapter/resolve/main/models/image_encoder/model.safetensors" || \
        echo "[entrypoint] WARNING: CLIP Vision download failed — character consistency will be unavailable"
else
    echo "[entrypoint] CLIP Vision encoder already present"
fi

# ===========================================================================
# WAN 2.1 Image-to-Video models (1.3B lightweight for RTX 3090 24GB)
# ===========================================================================

# --- WAN 2.1 T2V 1.3B diffusion model (BF16) — used as base for i2v ---
WAN_DIFF="$MODEL_DIR/diffusion_models/wan2.1_t2v_1.3B_bf16.safetensors"
if [ ! -f "$WAN_DIFF" ]; then
    echo "[entrypoint] Downloading WAN 2.1 1.3B diffusion model (~2.8 GB)..."
    wget -q --show-progress -O "$WAN_DIFF" \
        "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/diffusion_models/wan2.1_t2v_1.3B_bf16.safetensors" || \
        echo "[entrypoint] WARNING: WAN diffusion model download failed — WAN video generation will not work"
else
    echo "[entrypoint] WAN 2.1 1.3B diffusion model already present"
fi

# --- WAN text encoder (UMT5-XXL FP8) ---
WAN_TEXT="$MODEL_DIR/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
if [ ! -f "$WAN_TEXT" ]; then
    echo "[entrypoint] Downloading WAN text encoder (~9 GB)..."
    wget -q --show-progress -O "$WAN_TEXT" \
        "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors" || \
        echo "[entrypoint] WARNING: WAN text encoder download failed"
else
    echo "[entrypoint] WAN text encoder already present"
fi

# --- WAN VAE ---
WAN_VAE="$MODEL_DIR/vae/wan_2.1_vae.safetensors"
if [ ! -f "$WAN_VAE" ]; then
    echo "[entrypoint] Downloading WAN VAE (~335 MB)..."
    wget -q --show-progress -O "$WAN_VAE" \
        "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors" || \
        echo "[entrypoint] WARNING: WAN VAE download failed"
else
    echo "[entrypoint] WAN VAE already present"
fi

# --- WAN CLIP Vision (for image-to-video) ---
WAN_CLIP="$MODEL_DIR/clip_vision/clip_vision_h.safetensors"
if [ ! -f "$WAN_CLIP" ]; then
    echo "[entrypoint] Downloading WAN CLIP Vision (~1.2 GB)..."
    wget -q --show-progress -O "$WAN_CLIP" \
        "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/clip_vision_h.safetensors" || \
        echo "[entrypoint] WARNING: WAN CLIP Vision download failed"
else
    echo "[entrypoint] WAN CLIP Vision already present"
fi

echo "[entrypoint] Model check complete. Starting ComfyUI..."

# Ensure critical deps are in the correct Python (3.11, not system 3.10)
python -m pip install --quiet pyyaml requests opencv-python-headless 2>/dev/null || true

exec python main.py --listen 0.0.0.0 --port 8188

