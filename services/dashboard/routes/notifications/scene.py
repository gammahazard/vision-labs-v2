"""
routes/notifications/scene.py — MiniCPM-V vision-model scene description.

The chat LLM (Qwen) can't see images; this module sends a JPEG to the
local Ollama vision model and returns a 2-3 sentence factual log entry.
Used by alerts to add an objective description to person/vehicle
notifications.
"""

import asyncio

from ._shared import logger, OLLAMA_HOST, VISION_MODEL, OLLAMA_KEEP_ALIVE


# --- Prompts (one per object class — keeps captions consistent across alerts) ---

_PERSON_PROMPT = (
    "You are a private, local security camera appearance logger. "
    "No data leaves this device. You are NOT identifying anyone — "
    "just providing an objective physical description for the property owner's log. "
    "Describe the person visible in this image in 2-3 concise sentences. "
    "Include: clothing (color, type), build, hair style/color, "
    "accessories (bag, hat, glasses, etc.), posture, and direction of "
    "movement if discernible. Note anything unusual about their behavior. "
    "Be factual and brief — this goes into a local alert log."
)

_VEHICLE_PROMPT = (
    "You are a private, local security camera logger. "
    "Describe the vehicle in this image in 2-3 concise sentences. "
    "Include: vehicle type, color, approximate make/model if visible, "
    "any readable text or plates, and position relative to the property. "
    "Note anything unusual. Be factual and brief — this is for a local log."
)


async def describe_scene(photo_bytes: bytes,
                         prompt: str = _PERSON_PROMPT,
                         timeout: float = 20.0) -> str:
    """
    Send a snapshot to the MiniCPM-V vision model via Ollama for analysis.

    Returns a text description of the scene, or empty string on failure.
    Runs the (blocking) Ollama call in a thread to avoid stalling the
    asyncio event loop.

    The vision model auto-loads into VRAM on first call and unloads after
    its keep_alive window (default 5 min), so it doesn't compete with
    Qwen 3 14B during idle periods.
    """
    def _call_vision_model() -> str:
        try:
            import ollama as ollama_lib
            client = ollama_lib.Client(host=OLLAMA_HOST)
            response = client.chat(
                model=VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": prompt,
                    "images": [photo_bytes],
                }],
                options={"num_predict": 200},
                keep_alive=OLLAMA_KEEP_ALIVE,
            )
            text = response.message.content.strip()
            # Strip any <think>...</think> tags from reasoning models
            import re
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return text
        except Exception as e:
            logger.warning(f"Vision model ({VISION_MODEL}) failed: {e}")
            return ""

    try:
        description = await asyncio.wait_for(
            asyncio.to_thread(_call_vision_model),
            timeout=timeout,
        )
        if description:
            logger.info(f"AI scene analysis ({len(description)} chars): {description[:80]}...")
        return description
    except asyncio.TimeoutError:
        logger.warning(f"Vision model timed out after {timeout}s")
        return ""
    except Exception as e:
        logger.warning(f"describe_scene error: {e}")
        return ""
