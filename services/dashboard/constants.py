"""
services/dashboard/constants.py — central place for hardcoded config values.

PURPOSE:
    Single source of truth for Ollama model names, ComfyUI defaults, and
    other values that previously appeared as literals scattered across 4+
    files. Override any of these via env vars in docker-compose.yml.

USAGE:
    from constants import CHAT_MODEL, OLLAMA_KEEP_ALIVE

    response = await client.chat(
        model=CHAT_MODEL,
        keep_alive=OLLAMA_KEEP_ALIVE,
        ...
    )

WHY THIS FILE EXISTS:
    Before this module, "qwen3:14b" was hardcoded in 4 places and "5m"
    keep-alive was in 5. Changing the AI model meant grep + edit + miss
    one + debug. Now: change one env var (or one line here) and every
    call site picks it up.
"""

import os

# ---------------------------------------------------------------------------
# Ollama (LLM + vision model server)
# ---------------------------------------------------------------------------
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")

# Chat model used by the AI assistant (/api/ai/chat) and /ask Telegram command.
# Must support tool calling for the 18 dashboard tools to work.
CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "qwen3:14b")

# Vision model used for scene descriptions in notifications and /analyze command.
# Must support image input.
VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "minicpm-v")

# How long Ollama keeps the model loaded in VRAM after the last request.
# Passed to every Ollama call. Image generation explicitly sets this to 0
# to force-unload before grabbing the GPU.
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "5m")

# ---------------------------------------------------------------------------
# ComfyUI (image generation)
# ---------------------------------------------------------------------------
COMFYUI_HOST = os.getenv("COMFYUI_HOST", "http://comfyui:8188")

# Default checkpoint when the caller doesn't specify. Empty string means
# "fall back to the first available .safetensors in models/comfyui/checkpoints".
# Set this env var to lock to a specific file (e.g. "sd_xl_base_1.0.safetensors").
DEFAULT_CHECKPOINT = os.getenv("DEFAULT_COMFYUI_CHECKPOINT", "")
