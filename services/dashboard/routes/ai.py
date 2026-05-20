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

import asyncio
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

router = APIRouter(prefix="/api/ai", tags=["ai"])
logger = logging.getLogger("dashboard.ai")

from constants import CHAT_MODEL as OLLAMA_MODEL, OLLAMA_KEEP_ALIVE, OLLAMA_HOST
from contracts.tz import TZ_LOCAL  # validated single source of truth




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
    if not OLLAMA_MODEL:
        return {"model_ready": False, "model": "", "status": "disabled"}
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
    if not OLLAMA_MODEL:
        return JSONResponse(status_code=503, content={"error": "AI chat is disabled on this hardware tier (CHAT_MODEL is empty). Set CHAT_MODEL in .env to enable."})

    if not ai_state._ai_db:
        return JSONResponse(status_code=503, content={"error": "AI DB not initialized"})

    config = ai_state._ai_db.get_config()
    if not config.get("enabled"):
        return JSONResponse(status_code=400, content={"error": "AI assistant not enabled"})

    # Build message list with live system context
    system_context = await build_system_context()
    system_prompt = build_system_prompt(config, system_context)
    messages = [{"role": "system", "content": system_prompt}]

    # Analytical questions ("busiest hour", "what time", "show me the clip from X")
    # have to start fresh. Qwen 14B is weak at overriding a poisoned history —
    # if a previous turn fabricated a "11pm-12am / 95 detections / click here"
    # answer, the model parrots it on the next turn regardless of system-prompt
    # rules telling it not to. So: for these specific question patterns, drop
    # the conversation history entirely and rely on the current turn's tools.
    msg_lower = (req.message or "").lower()
    _ANALYTICAL_KEYWORDS = (
        "busiest", "busy hour", "peak hour", "what hour", "which hour",
        "what time", "time of day", "hourly", "hour-by-hour",
    )
    _DVR_KEYWORDS = (
        "clip", "video", "recording", "footage", "dvr", "playback",
    )
    is_hourly_question = any(k in msg_lower for k in _ANALYTICAL_KEYWORDS)
    is_dvr_question = any(k in msg_lower for k in _DVR_KEYWORDS)
    drop_history = is_hourly_question or is_dvr_question

    if not drop_history:
        # Normal turn: include last 6 messages of history (3 turns each side).
        for msg in req.history[-6:]:
            if msg.get("role") in ("user", "assistant"):
                messages.append({"role": msg["role"], "content": msg["content"]})
    # When `drop_history` is True we deliberately send NO prior turns. The
    # system prompt + the current user message + the live tool catalog are
    # the only context. This is the only reliable way to stop the model
    # from regurgitating fabricated hourly numbers / fake "click here" links
    # from earlier in the conversation.

    # If the question requires a specific tool that earlier turns might have
    # skipped, inject a one-shot hard reminder right before the user's message.
    # This is belt-and-braces alongside the dropped history.
    hard_reminders: list[str] = []
    if is_hourly_question:
        hard_reminders.append(
            "This question is about hour-of-day / busiest-time. "
            "You MUST call query_event_patterns with analysis_type='hourly'. "
            "query_events_by_date does NOT have hourly data — its `total_events` "
            "and `by_type` are daily totals only. Read `busiest_hour`, `top_hours`, "
            "`hourly_breakdown`, `by_identity_per_hour` from query_event_patterns' "
            "response. Do NOT extrapolate hourly counts from daily totals."
        )
    if is_dvr_question:
        hard_reminders.append(
            "This question requests a DVR clip / recording / video. "
            "You MUST call find_dvr_segment to get a real deep_link URL. "
            "Render the URL it returns as a markdown link: "
            "`[Open the clip](<deep_link>)`. NEVER write 'click here' without "
            "a real URL behind it. If the user asked for 'the clip from "
            "yesterday's busiest hour', chain TWO calls: first "
            "query_event_patterns to find the hour, then find_dvr_segment "
            "with that hour as `time`."
        )
    if hard_reminders:
        messages.append({
            "role": "system",
            "content": "URGENT — read before answering the next user message:\n\n"
                       + "\n\n".join(f"• {r}" for r in hard_reminders),
        })

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

    # Single-turn Ollama call helper. The ollama python client is SYNC, so
    # we wrap it in asyncio.to_thread to keep the FastAPI event loop free
    # for other requests during the 5-30s the model is thinking. We also
    # bound each turn to 60s so a stuck Ollama (GPU OOM, model swap) can't
    # hang the request indefinitely.
    def _chat_turn():
        return client.chat(
            model=OLLAMA_MODEL,
            messages=messages,
            tools=TOOLS,
            options={"num_ctx": 8192},
            think=False,
            keep_alive=OLLAMA_KEEP_ALIVE,
        )

    try:
        # First call — may include tool calls
        response = await asyncio.wait_for(
            asyncio.to_thread(_chat_turn), timeout=60.0
        )

        # Handle tool calls if any. We capture each (name, args, result_json)
        # so the response can include the raw tool data — the user can flip
        # a "show tool data" toggle to verify the AI's claims against ground
        # truth (mitigation for Qwen hallucinations on count/identity questions).
        tool_calls_log: list[dict] = []
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
                # Stash for the API response. Cap each result at 8 KB so a
                # huge tool dump (e.g. 50 events worth of fields) doesn't
                # bloat the chat history beyond reason.
                truncated = result if len(result) <= 8192 else result[:8192] + "...[truncated]"
                tool_calls_log.append({
                    "name": tool_name,
                    "args": tool_args if isinstance(tool_args, dict) else {},
                    "result": truncated,
                })

            # Get the next response with tool results
            response = await asyncio.wait_for(
                asyncio.to_thread(_chat_turn), timeout=60.0
            )

        # Extract final response text
        reply = response.message.content or ""

        # Strip <think> blocks if Qwen includes them
        if "<think>" in reply:
            import re
            reply = re.sub(r"<think>.*?</think>\s*", "", reply, flags=re.DOTALL).strip()

        # Programmatic enforcement: if the user asked for a DVR link/clip and
        # the model wrote about a clip but didn't call find_dvr_segment, force
        # one more tool round. Qwen 14B's tool-use discipline isn't strong
        # enough to reliably call a 3rd tool even with hard system prompts,
        # so we close the gap server-side.
        import re as _re
        called_dvr = any(t["name"] == "find_dvr_segment" for t in tool_calls_log)
        mentions_clip_text = bool(_re.search(
            r"(open the clip|view the (clip|recording|footage)|click here|see the (clip|footage))",
            reply, _re.IGNORECASE
        ))
        has_real_dvr_link = "/ai.html?tab=recordings" in reply
        if (is_dvr_question and not called_dvr
                and (mentions_clip_text or not has_real_dvr_link)
                and tool_rounds < 5):
            logger.info(
                "DVR enforcement: re-prompting model — user asked for a clip "
                "but find_dvr_segment was not called."
            )
            # Attach the prior reply to the conversation as the assistant's
            # last turn so the model can see what it said, then prepend a
            # corrective system instruction and run one more round.
            messages.append({"role": "assistant", "content": reply})
            messages.append({
                "role": "system",
                "content": (
                    "Your previous reply mentioned a clip/recording but did "
                    "NOT include a real DVR URL. You MUST call "
                    "find_dvr_segment now to get a deep_link. If you already "
                    "know the busiest hour from query_event_patterns, pass "
                    "it as the `time` arg (formatted HH:MM, e.g. 20:00). "
                    "Camera should be the one with the highest count from "
                    "top_hours[0].per_camera.\n\n"
                    "Then respond again, keeping ALL the content of your "
                    "previous reply (detection counts, busiest-hour numbers, "
                    "top hours, etc.) VERBATIM, and appending the markdown "
                    "link `[Open the clip in the DVR tab](<deep_link>)` at "
                    "the end. Do NOT shorten or replace your earlier "
                    "answer — only ADD the link."
                ),
            })
            response = await asyncio.wait_for(
                asyncio.to_thread(_chat_turn), timeout=60.0
            )
            # Run another tool-call round if the model now calls find_dvr_segment.
            while response.message.tool_calls and tool_rounds < 5:
                tool_rounds += 1
                messages.append(response.message)
                for tool_call in response.message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = tool_call.function.arguments
                    logger.info(f"DVR re-prompt tool call: {tool_name}({tool_args})")
                    result = await execute_tool(tool_name, tool_args)
                    messages.append({"role": "tool", "content": result})
                    truncated = result if len(result) <= 8192 else result[:8192] + "...[truncated]"
                    tool_calls_log.append({
                        "name": tool_name,
                        "args": tool_args if isinstance(tool_args, dict) else {},
                        "result": truncated,
                    })
                response = await asyncio.wait_for(
                    asyncio.to_thread(_chat_turn), timeout=60.0
                )
            reply = response.message.content or reply
            if "<think>" in reply:
                reply = _re.sub(r"<think>.*?</think>\s*", "", reply, flags=_re.DOTALL).strip()

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

        return {"reply": reply, "tool_calls": tool_calls_log}

    except asyncio.TimeoutError:
        ai_state.collect_media(request_id)
        logger.warning("AI chat turn timed out (60s)")
        return JSONResponse(
            status_code=504,
            content={"error": "AI took too long to respond. Try a simpler question or check Ollama health."},
        )
    except Exception as e:
        ai_state.collect_media(request_id)  # Clean up on error
        logger.exception(f"AI chat error: {e}")
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
# Use the canonical VISION_MODEL from constants — single source of truth.
# (Previously this file shadowed it with a different env var name
# `VISION_MODEL` vs constants' `OLLAMA_VISION_MODEL` — silent config split.)
from constants import VISION_MODEL


class VisionRequest(BaseModel):
    image: str | None = None       # single base64 image
    images: list[str] | None = None  # multiple base64 images (video frames)
    prompt: str = "Describe this image in detail."


@router.get("/vision/status")
async def get_vision_status():
    """Check if the MiniCPM-V vision model is available."""
    # If the tier env disables vision (small/mid tiers can set
    # VISION_MODEL="" to skip the multimodal model), short-circuit
    # cleanly. The previous `any("" in name for ...)` test was always
    # True, so an empty model name would be reported as "available."
    if not VISION_MODEL or not VISION_MODEL.strip():
        return {"available": False, "model": "", "status": "disabled"}
    try:
        client = ollama_lib.Client(host=OLLAMA_HOST)
        models = client.list()
        model_list = getattr(models, "models", None) or []
        model_names = []
        for m in model_list:
            name = getattr(m, "model", None) or getattr(m, "name", "") or ""
            model_names.append(name)
        target = VISION_MODEL.split(":")[0]
        downloaded = bool(target) and any(target in name for name in model_names)

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
            keep_alive=OLLAMA_KEEP_ALIVE,
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

