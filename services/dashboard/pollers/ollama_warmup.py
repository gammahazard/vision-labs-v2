"""
services/dashboard/pollers/ollama_warmup.py — pull + warm the chat model at startup.

PURPOSE:
    1. On first startup, pull the chat model from Ollama (~9.3 GB for Qwen 3 14B).
       Subsequent startups skip the pull when the model is already cached.
    2. Send a real chat message to force the model into GPU memory. Without
       this, the first user chat would take 10+ seconds while the model
       cold-loads from disk.
    3. Save the warm-up message + reply to ai.db so the user sees them on
       first dashboard load (gives a "system just restarted" breadcrumb).
    4. Signal `set_gpu_ready_flag(True)` so /api/ai/status reflects readiness.

RELATIONSHIPS:
    - Reads model name from: constants.CHAT_MODEL
    - Talks to: Ollama at constants.OLLAMA_HOST
    - Writes to: routes.ai_state._ai_db (chat history)
    - Signals: routes.ai_state.set_gpu_ready_flag

WHY A POLLER (one-shot, not a loop):
    `asyncio.create_task` just runs the coroutine once. There's no loop —
    the function returns after warm-up and the task terminates. It lives
    in `pollers/` because it's scheduled the same way as the loops, and
    keeping it next to them keeps server.py's startup() tidy.
"""

import asyncio
import logging
import re

logger = logging.getLogger("dashboard.ollama_warmup")


async def warm_ollama():
    """Pull and warm the chat model. Saves the warm-up exchange to ai.db."""
    import ollama as ollama_lib
    from constants import CHAT_MODEL, OLLAMA_HOST, OLLAMA_KEEP_ALIVE
    from routes.ai import set_gpu_ready_flag

    host = OLLAMA_HOST
    model = CHAT_MODEL

    if not model:
        logger.info("CHAT_MODEL is empty — AI chat disabled on this hardware tier; skipping ollama warmup")
        # Signal "ready" so /api/ai/status reports a stable state rather than
        # an infinite "warming up" the UI would display.
        set_gpu_ready_flag(True)
        return

    await asyncio.sleep(10)  # Wait for other GPU services to finish CUDA init

    try:
        client = ollama_lib.Client(host=host)
        loop = asyncio.get_event_loop()
        # Both client.list() and client.pull() are synchronous network calls
        # that can block for minutes on first-boot model download. Run them
        # in the default executor so the FastAPI event loop keeps serving
        # other requests (Telegram polling, /api/events, etc.) during the pull.
        models = await loop.run_in_executor(None, client.list)
        model_names = [m.model for m in models.models] if models.models else []
        if not any(model in name for name in model_names):
            logger.info(f"Pulling AI model '{model}' (~9.3 GB, first-time download)...")
            await loop.run_in_executor(None, client.pull, model)
            logger.info(f"AI model '{model}' downloaded successfully")
        else:
            logger.info(f"AI model '{model}' already available")

        # Warm-up: send a real chat message to force the model into GPU memory.
        # This message + reply are saved to chat history so the user sees it.
        logger.info(f"Warming up AI model '{model}' (loading into GPU memory)...")

        # Access the AI DB that was set up by startup
        from routes.ai_state import _ai_db
        startup_msg = "⚡ System restart detected — loading AI model into memory..."

        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, lambda: client.chat(
                model=model,
                messages=[{"role": "user", "content": "The system just restarted. Confirm you are loaded and ready in one short sentence."}],
                options={"num_predict": 30, "num_ctx": 8192},
                think=False,
                keep_alive=OLLAMA_KEEP_ALIVE,
            ))
            # ollama library returns objects, not dicts
            reply = getattr(resp.message, "content", "") or "Model loaded and ready."
            # Strip <think> blocks from Qwen 3
            reply = re.sub(r"<think>.*?</think>\s*", "", reply, flags=re.DOTALL).strip()
            if not reply:
                reply = "Model loaded and ready."
            logger.info(f"AI model '{model}' loaded into GPU memory — ready for chat")

            # Signal that the model is now in GPU memory
            set_gpu_ready_flag(True)

            # Save both messages to chat history so user sees them
            if _ai_db:
                _ai_db.save_message("system", startup_msg)
                _ai_db.save_message("assistant", f"✅ {reply}")
        except Exception as warm_err:
            logger.warning(f"Warm-up chat failed (model may still load on first use): {warm_err}")
            if _ai_db:
                _ai_db.save_message("system", startup_msg)
                _ai_db.save_message("assistant", "⚠️ Model is still loading — it will be ready when you send your first message.")
    except Exception as e:
        logger.warning(f"Failed to pull AI model: {e} (AI chat will be unavailable until model is pulled)")
