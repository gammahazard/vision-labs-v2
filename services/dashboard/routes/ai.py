"""
routes/ai.py — AI assistant API endpoints.

PURPOSE:
    Chat with a local Qwen 3 14B model via Ollama. Supports tool/function
    calling for querying security data, sending Telegram messages,
    scheduling reminders, and managing system config.

ENDPOINTS:
    GET  /api/ai/config — Get AI assistant configuration (enabled, names)
    POST /api/ai/config — Save/update AI configuration (onboarding)
    POST /api/ai/chat   — Send message, get streamed AI response
    GET  /api/ai/status — Check if model is downloaded and loaded
    GET  /api/ai/history — Get server-side chat history
    DELETE /api/ai/history — Clear chat history
    POST /api/ai/reset  — Reset AI assistant config
    GET  /api/ai/reminders — Get upcoming reminders
    GET  /api/ai/clip/{filename} — Serve a saved video clip

MODULES:
    ai_state.py   — Shared state (DB refs, GPU flag, pending media)
    ai_tools.py   — Tool definitions + executor functions
    ai_prompts.py — System prompt builder

LLM:
    Qwen 3 14B running locally via Ollama Docker container.
    Tool calling is used for structured actions (query events, send alerts).
"""

import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

import ollama as ollama_lib

import routes as ctx
import routes.ai_state as ai_state
from routes.ai_state import set_ai_db, set_gpu_ready_flag
from routes.ai_tools import TOOLS, execute_tool
from routes.ai_prompts import build_system_context, build_system_prompt
from routes.image_gen import set_vram_mode

router = APIRouter(prefix="/api/ai", tags=["ai"])
logger = logging.getLogger("dashboard.ai")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL = "qwen3:14b"
TZ_LOCAL = ZoneInfo(os.getenv("LOCATION_TIMEZONE", "America/Toronto"))




# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------
class ConfigRequest(BaseModel):
    enabled: bool = True
    user_name: str = ""
    ai_name: str = "Atlas"


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/status")
async def get_status():
    """Check if Ollama model is downloaded AND loaded into GPU memory."""
    try:
        client = ollama_lib.Client(host=OLLAMA_HOST)
        models = client.list()
        # The ollama library returns objects with .models attribute (list of Model objs)
        model_list = getattr(models, "models", None) or []
        model_names = []
        for m in model_list:
            name = getattr(m, "model", None) or getattr(m, "name", "") or ""
            model_names.append(name)
        target = OLLAMA_MODEL.split(":")[0]
        model_downloaded = any(target in name for name in model_names)

        if not model_downloaded:
            return {"model_ready": False, "model": OLLAMA_MODEL, "status": "not_found"}

        # Model is downloaded — check if it's in GPU memory.
        # First check our flag (set by warm-up chat)
        if ai_state._model_gpu_ready:
            return {"model_ready": True, "model": OLLAMA_MODEL, "status": "ready"}

        # Flag not set yet — check Ollama's /api/ps (running models list)
        # This is faster than waiting for the warm-up chat to complete
        try:
            ps = client.ps()
            running_models = getattr(ps, "models", None) or []
            for rm in running_models:
                rm_name = getattr(rm, "model", None) or getattr(rm, "name", "") or ""
                if target in rm_name:
                    # Model is loaded in VRAM — set flag and return ready
                    ai_state._model_gpu_ready = True
                    logger.info(f"Model '{OLLAMA_MODEL}' detected in GPU memory via /api/ps")
                    return {"model_ready": True, "model": OLLAMA_MODEL, "status": "ready"}
        except Exception:
            pass  # ps() not available in older ollama versions, fall through

        return {"model_ready": False, "model": OLLAMA_MODEL, "status": "loading"}
    except Exception as e:
        logger.warning(f"Ollama status check failed: {e}")
        return {"model_ready": False, "model": OLLAMA_MODEL, "status": "offline"}


@router.get("/config")
async def get_config():
    """Get AI assistant configuration."""
    if not ai_state._ai_db:
        return {"enabled": False, "user_name": "", "ai_name": "Atlas"}
    return ai_state._ai_db.get_config()


@router.post("/config")
async def save_config(req: ConfigRequest):
    """Save AI assistant configuration (onboarding)."""
    if not ai_state._ai_db:
        return JSONResponse(status_code=503, content={"error": "AI DB not initialized"})
    return ai_state._ai_db.save_config(
        enabled=req.enabled,
        user_name=req.user_name,
        ai_name=req.ai_name,
    )


@router.post("/chat")
async def chat(req: ChatRequest):
    """
    Send a message to the AI and get a streamed response.
    Handles tool calls transparently — the user sees only the final answer.
    """
    if not ai_state._ai_db:
        return JSONResponse(status_code=503, content={"error": "AI DB not initialized"})

    config = ai_state._ai_db.get_config()
    if not config.get("enabled"):
        return JSONResponse(status_code=400, content={"error": "AI assistant not enabled"})

    # Build message list with live system context
    system_context = await build_system_context()
    system_prompt = build_system_prompt(config, system_context)
    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation history (from client)
    for msg in req.history[-20:]:  # Last 20 messages for context
        if msg.get("role") in ("user", "assistant"):
            messages.append({"role": msg["role"], "content": msg["content"]})

    # Add current message
    messages.append({"role": "user", "content": req.message})

    # Save user message server-side
    ai_state._ai_db.save_message("user", req.message)

    # Configure Ollama client
    client = ollama_lib.Client(host=OLLAMA_HOST)

    # Generate unique request ID for per-request media tracking
    import uuid
    request_id = uuid.uuid4().hex
    ai_state.set_request_id(request_id)

    try:
        # First call — may include tool calls
        response = client.chat(
            model=OLLAMA_MODEL,
            messages=messages,
            tools=TOOLS,
            options={"num_ctx": 8192},
            think=False,
            keep_alive="5m",
        )

        # Handle tool calls if any
        tool_rounds = 0
        while response.message.tool_calls and tool_rounds < 5:
            tool_rounds += 1
            # Add assistant's tool call message
            messages.append(response.message)

            # Execute each tool call
            for tool_call in response.message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = tool_call.function.arguments
                logger.info(f"Tool call: {tool_name}({tool_args})")

                result = await execute_tool(tool_name, tool_args)
                messages.append({
                    "role": "tool",
                    "content": result,
                })

            # Get the next response with tool results
            response = client.chat(
                model=OLLAMA_MODEL,
                messages=messages,
                tools=TOOLS,
                options={"num_ctx": 8192},
                think=False,
                keep_alive="5m",
            )

        # Extract final response text
        reply = response.message.content or ""

        # Strip <think> blocks if Qwen includes them
        if "<think>" in reply:
            import re
            reply = re.sub(r"<think>.*?</think>\s*", "", reply, flags=re.DOTALL).strip()

        # Collect media stashed by tools during this request
        media = ai_state.collect_media(request_id)

        # Inject snapshot image if one was captured during this request
        if media["snapshot"]:
            snapshot_md = f"![Live snapshot](data:image/jpeg;base64,{media['snapshot']})"
            reply = f"{snapshot_md}\n\n{reply}"

        # Inject video clip if one was captured during this request
        if media["clip"]:
            clip_url = f"/api/ai/clip/{media['clip']}"
            clip_html = f'<video controls autoplay muted playsinline style="max-width:100%;border-radius:8px;margin:8px 0;"><source src="{clip_url}" type="video/mp4">Your browser does not support video.</video>'
            reply = f"{clip_html}\n\n{reply}"

        # Inject browse images (vehicle snapshots etc.) if any were stashed
        if media["images"]:
            img_parts = []
            for img in media["images"]:
                url = img["url"]
                cap = img.get("caption", "")
                img_parts.append(
                    f'<figure style="display:inline-block;margin:4px;">'
                    f'<img src="{url}" alt="{cap}" style="max-width:280px;border-radius:8px;cursor:pointer;" '
                    f'onclick="window.open(this.src)"/>'
                    f'<figcaption style="text-align:center;font-size:0.8em;color:#aaa;">{cap}</figcaption>'
                    f'</figure>'
                )
            gallery_html = f'<div style="display:flex;flex-wrap:wrap;gap:8px;margin:8px 0;">{" ".join(img_parts)}</div>'
            reply = f"{reply}\n\n{gallery_html}"

        # Save assistant response server-side
        ai_state._ai_db.save_message("assistant", reply)

        # Model is now loaded in VRAM — sync state so UI reflects reality
        set_vram_mode("chat")

        return {"reply": reply}

    except Exception as e:
        ai_state.collect_media(request_id)  # Clean up on error
        logger.error(f"AI chat error: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"AI unavailable: {str(e)}"},
        )



@router.get("/history")
async def get_history(limit: int = 50):
    """Get server-side chat history."""
    if not ai_state._ai_db:
        return []
    return ai_state._ai_db.get_recent_history(limit=limit)


@router.get("/clip/{filename}")
async def serve_clip(filename: str):
    """Serve a saved AI-captured video clip."""
    from fastapi.responses import FileResponse
    import re as _re
    # Sanitize filename — only allow safe characters
    if not _re.match(r'^[\w\-]+\.mp4$', filename):
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})
    filepath = os.path.join("/data/snapshots", "clips", filename)
    if not os.path.isfile(filepath):
        return JSONResponse(status_code=404, content={"error": "Clip not found"})
    return FileResponse(filepath, media_type="video/mp4")


@router.delete("/history")
async def clear_history():
    """Clear chat history."""
    if not ai_state._ai_db:
        return {"status": "ok"}
    ai_state._ai_db.clear_history()
    return {"status": "ok"}


@router.post("/reset")
async def reset_assistant():
    """Reset AI assistant — clears config and history, re-shows wizard."""
    if not ai_state._ai_db:
        return {"status": "ok"}
    ai_state._ai_db.save_config(enabled=False, user_name="", ai_name="Atlas")
    ai_state._ai_db.clear_history()
    return {"status": "ok"}


@router.get("/reminders")
async def get_reminders():
    """Get upcoming reminders."""
    if not ai_state._ai_db:
        return []
    return ai_state._ai_db.get_reminders()



# ---------------------------------------------------------------------------
# Vision Model (MiniCPM-V) — on-demand image analysis
# ---------------------------------------------------------------------------
VISION_MODEL = os.getenv("VISION_MODEL", "minicpm-v")


class VisionRequest(BaseModel):
    image: str | None = None       # single base64 image
    images: list[str] | None = None  # multiple base64 images (video frames)
    prompt: str = "Describe this image in detail."


@router.get("/vision/status")
async def get_vision_status():
    """Check if the MiniCPM-V vision model is available."""
    try:
        client = ollama_lib.Client(host=OLLAMA_HOST)
        models = client.list()
        model_list = getattr(models, "models", None) or []
        model_names = []
        for m in model_list:
            name = getattr(m, "model", None) or getattr(m, "name", "") or ""
            model_names.append(name)
        target = VISION_MODEL.split(":")[0]
        downloaded = any(target in name for name in model_names)

        if not downloaded:
            return {"available": False, "model": VISION_MODEL, "status": "not_found"}

        # Check if loaded in VRAM
        try:
            ps = client.ps()
            running = getattr(ps, "models", None) or []
            for rm in running:
                rm_name = getattr(rm, "model", None) or getattr(rm, "name", "") or ""
                if target in rm_name:
                    return {"available": True, "model": VISION_MODEL, "status": "loaded",
                            "vram": "active"}
        except Exception:
            pass

        return {"available": True, "model": VISION_MODEL, "status": "ready",
                "vram": "idle"}
    except Exception as e:
        logger.warning(f"Vision status check failed: {e}")
        return {"available": False, "model": VISION_MODEL, "status": "offline"}


@router.post("/vision")
async def analyze_image(req: VisionRequest):
    """
    Analyze image(s) using MiniCPM-V vision model.

    Accepts either a single base64 image or a list of base64 images
    (e.g. video frames). Returns the model's description.
    """
    import base64
    import asyncio
    import re

    # Collect all images — single or multiple
    raw_images = []
    if req.images:
        raw_images = req.images
    elif req.image:
        raw_images = [req.image]
    else:
        return JSONResponse(status_code=400,
                            content={"error": "No image data provided"})

    # Decode all images
    image_list = []
    for i, img_b64 in enumerate(raw_images[:8]):  # Cap at 8 frames
        try:
            decoded = base64.b64decode(img_b64)
            if len(decoded) < 100:
                continue
            image_list.append(decoded)
        except Exception:
            logger.warning(f"Failed to decode image {i}")
            continue

    if not image_list:
        return JSONResponse(status_code=400,
                            content={"error": "No valid image data"})

    is_video = len(image_list) > 1
    timeout = 60.0 if is_video else 30.0

    def _call_vision():
        client = ollama_lib.Client(host=OLLAMA_HOST)
        response = client.chat(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": req.prompt,
                "images": image_list,
            }],
            options={"num_predict": 800 if is_video else 500},
            keep_alive="5m",
        )
        text = response.message.content.strip()
        # Strip <think> tags from reasoning models
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        return text

    try:
        description = await asyncio.wait_for(
            asyncio.to_thread(_call_vision),
            timeout=timeout,
        )
        frame_info = f", {len(image_list)} frames" if is_video else ""
        logger.info(f"Vision analysis ({len(description)} chars{frame_info}): {description[:80]}...")
        return {"description": description, "model": VISION_MODEL,
                "frames": len(image_list)}
    except asyncio.TimeoutError:
        return JSONResponse(status_code=504,
                            content={"error": f"Vision model timed out ({int(timeout)}s)"})
    except Exception as e:
        logger.error(f"Vision analysis error: {e}")
        return JSONResponse(status_code=500,
                            content={"error": f"Vision model error: {str(e)}"})

