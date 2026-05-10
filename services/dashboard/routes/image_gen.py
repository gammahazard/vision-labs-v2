"""
routes/image_gen.py — Image generation API endpoints (ComfyUI proxy).

PURPOSE:
    Proxies image generation requests from the dashboard to a local
    ComfyUI instance. Handles prompt queueing, result polling, model
    and LoRA listing, and batch generation.

ENDPOINTS:
    POST /api/generate                    — Queue an image generation job
    GET  /api/generate/status             — ComfyUI health + queue status
    GET  /api/generate/models             — List available checkpoint models
    GET  /api/generate/loras              — List available LoRA models
    GET  /api/generate/history/{prompt_id} — Get generation result(s)
"""

import os
import json
import uuid
import random
import base64
import logging
from pathlib import Path
from datetime import datetime

import httpx
import redis as _redis
from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import FileResponse
from streams import GPU_PAUSE_KEY

logger = logging.getLogger("dashboard.generate")
router = APIRouter()

COMFYUI_HOST = os.getenv("COMFYUI_HOST", "http://comfyui:8188")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")
GENERATIONS_DIR = Path("/data/generations/images")
COMFYUI_OUTPUT_DIR = Path("/data/comfyui-output")

_vram_mode = "chat"  # "chat" = Ollama loaded, "generate" = Ollama unloaded
_gen_params = {}  # prompt_id -> generation params for metadata sidecar


def set_vram_mode(mode: str):
    """Allow other modules (e.g. ai.py) to update VRAM mode."""
    global _vram_mode
    _vram_mode = mode
_cached_default_model = None
_cached_default_model_time = 0

# Lazy Redis client for GPU pause flag
_pause_redis = None
GPU_PAUSE_TTL = 300  # 5 min safety — auto-clears if dashboard crashes mid-generation
GPU_LOCK_KEY = "gpu:generation_lock"  # Centralized lock: prevents concurrent generations


def _get_pause_redis():
    """Get or create a Redis client for the GPU pause flag."""
    global _pause_redis
    if _pause_redis is None:
        _pause_redis = _redis.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            decode_responses=True,
        )
    return _pause_redis


def _set_gpu_pause():
    """Set the GPU pause flag in Redis (with TTL safety net)."""
    try:
        _get_pause_redis().set(GPU_PAUSE_KEY, "1", ex=GPU_PAUSE_TTL)
        logger.info("GPU pause flag SET — detectors will pause inference")
    except Exception as e:
        logger.warning(f"Failed to set GPU pause flag: {e}")


def _clear_gpu_pause():
    """Clear the GPU pause flag in Redis."""
    try:
        _get_pause_redis().delete(GPU_PAUSE_KEY)
        logger.info("GPU pause flag CLEARED — detectors will resume")
    except Exception as e:
        logger.warning(f"Failed to clear GPU pause flag: {e}")


def _acquire_gpu_lock(owner: str = "image_gen") -> bool:
    """Acquire centralized GPU lock. Returns False if another generation is running."""
    try:
        acquired = _get_pause_redis().set(
            GPU_LOCK_KEY, owner, nx=True, ex=GPU_PAUSE_TTL + 60
        )
        if acquired:
            logger.info(f"GPU generation lock ACQUIRED by {owner}")
        else:
            current = _get_pause_redis().get(GPU_LOCK_KEY)
            logger.warning(f"GPU generation lock DENIED — held by '{current}'")
        return bool(acquired)
    except Exception as e:
        logger.warning(f"GPU lock acquire failed: {e}")
        return True  # Fail open


def _release_gpu_lock():
    """Release the centralized GPU generation lock."""
    try:
        _get_pause_redis().delete(GPU_LOCK_KEY)
        logger.info("GPU generation lock RELEASED")
    except Exception as e:
        logger.warning(f"GPU lock release failed: {e}")


def _get_default_model() -> str:
    """Get the first available checkpoint from ComfyUI, cached for 60s."""
    global _cached_default_model, _cached_default_model_time
    import time
    now = time.time()
    if _cached_default_model and (now - _cached_default_model_time) < 60:
        return _cached_default_model
    try:
        resp = httpx.get(
            f"{COMFYUI_HOST}/object_info/CheckpointLoaderSimple", timeout=5
        )
        if resp.status_code == 200:
            info = resp.json()
            models = info.get("CheckpointLoaderSimple", {}).get("input", {}).get(
                "required", {}
            ).get("ckpt_name", [[]])[0]
            if models:
                _cached_default_model = models[0]
                _cached_default_model_time = now
                return _cached_default_model
    except Exception:
        pass
    return _cached_default_model or "zillah.safetensors"


# ---------------------------------------------------------------------------
# Standard text-to-image workflow for ComfyUI API
# ---------------------------------------------------------------------------
def _build_txt2img_workflow(
    prompt: str,
    negative_prompt: str = "",
    model: str = "",
    width: int = 1024,
    height: int = 1024,
    steps: int = 20,
    cfg: float = 7.0,
    seed: int = -1,
    batch_size: int = 1,
    lora: str = "",
    lora_strength: float = 0.8,
) -> dict:
    """Build a ComfyUI API workflow for text-to-image generation."""
    if seed < 0:
        seed = random.randint(0, 2**32 - 1)

    workflow = {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["4", 0] if not lora else ["10", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {
                "ckpt_name": model or _get_default_model(),
            },
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": width,
                "height": height,
                "batch_size": batch_size,
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": prompt,
                "clip": ["4", 1] if not lora else ["10", 1],
            },
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": negative_prompt or "ugly, blurry, low quality, deformed",
                "clip": ["4", 1] if not lora else ["10", 1],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["3", 0],
                "vae": ["4", 2],
            },
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": "visionlabs",
                "images": ["8", 0],
            },
        },
    }

    # Add LoRA loader node if specified
    if lora:
        workflow["10"] = {
            "class_type": "LoraLoader",
            "inputs": {
                "lora_name": lora,
                "strength_model": lora_strength,
                "strength_clip": lora_strength,
                "model": ["4", 0],
                "clip": ["4", 1],
            },
        }

    return workflow


def _build_img2img_workflow(
    image_name: str,
    prompt: str,
    negative_prompt: str = "",
    model: str = "",
    steps: int = 20,
    cfg: float = 7.0,
    seed: int = -1,
    denoise: float = 0.65,
    batch_size: int = 1,
    lora: str = "",
    lora_strength: float = 0.8,
) -> dict:
    """Build a ComfyUI API workflow for image-to-image generation."""
    if seed < 0:
        seed = random.randint(0, 2**32 - 1)

    workflow = {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": denoise,
                "model": ["4", 0] if not lora else ["10", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["11", 0],  # VAEEncode output
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {
                "ckpt_name": model or _get_default_model(),
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": prompt or "high quality, detailed",
                "clip": ["4", 1] if not lora else ["10", 1],
            },
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": negative_prompt or "ugly, blurry, low quality, deformed",
                "clip": ["4", 1] if not lora else ["10", 1],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["3", 0],
                "vae": ["4", 2],
            },
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": "visionlabs_i2i",
                "images": ["8", 0],
            },
        },
        # Load the uploaded image
        "12": {
            "class_type": "LoadImage",
            "inputs": {
                "image": image_name,
            },
        },
        # Encode uploaded image to latent space
        "11": {
            "class_type": "VAEEncode",
            "inputs": {
                "pixels": ["12", 0],
                "vae": ["4", 2],
            },
        },
    }

    if lora:
        workflow["10"] = {
            "class_type": "LoraLoader",
            "inputs": {
                "lora_name": lora,
                "strength_model": lora_strength,
                "strength_clip": lora_strength,
                "model": ["4", 0],
                "clip": ["4", 1],
            },
        }

    return workflow


# ---------------------------------------------------------------------------
# POST /api/generate — Queue an image generation
# ---------------------------------------------------------------------------
@router.post("/api/generate")
async def generate_image(request_body: dict):
    """Queue an image generation job with ComfyUI."""
    lock_acquired = False
    try:
        # Check GPU lock — reject if video pipeline is running
        if not _acquire_gpu_lock("image_gen:txt2img"):
            return {"error": "GPU is busy — video pipeline is running. Please wait and try again."}
        lock_acquired = True

        # Free VRAM: unload Ollama models so ComfyUI gets full GPU memory
        # Without this, model loading takes 11+ minutes due to weight offloading
        await free_vram()

        # Pause GPU detectors while generating
        _set_gpu_pause()
        prompt_text = request_body.get("prompt", "")
        if not prompt_text:
            return {"error": "Prompt is required"}

        negative = request_body.get("negative_prompt", "")
        model = request_body.get("model", "")
        width = int(request_body.get("width", 1024))
        height = int(request_body.get("height", 1024))
        steps = int(request_body.get("steps", 20))
        cfg = float(request_body.get("cfg", 7.0))
        seed = int(request_body.get("seed", -1))
        batch_size = min(int(request_body.get("batch_size", 1)), 4)
        lora = request_body.get("lora", "")
        lora_strength = float(request_body.get("lora_strength", 0.8))

        workflow = _build_txt2img_workflow(
            prompt=prompt_text,
            negative_prompt=negative,
            model=model,
            width=width,
            height=height,
            steps=steps,
            cfg=cfg,
            seed=seed,
            batch_size=batch_size,
            lora=lora,
            lora_strength=lora_strength,
        )

        # Queue the prompt with ComfyUI
        client_id = str(uuid.uuid4())
        payload = {"prompt": workflow, "client_id": client_id}

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{COMFYUI_HOST}/prompt",
                json=payload,
                timeout=30,
            )

        if resp.status_code != 200:
            error_text = resp.text
            logger.warning(f"ComfyUI prompt queue failed: {error_text}")
            return {"error": f"ComfyUI error: {error_text}"}

        result = resp.json()
        prompt_id = result.get("prompt_id", "")
        logger.info(f"Image generation queued: {prompt_id} (batch={batch_size})")

        # Store params for metadata sidecar
        _gen_params[prompt_id] = {
            "prompt": prompt_text,
            "negative_prompt": negative,
            "model": model,
            "lora": lora,
            "lora_strength": lora_strength,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg": cfg,
            "seed": seed,
            "batch_size": batch_size,
            "timestamp": datetime.now().isoformat(),
        }

        # GPU lock stays held — released when history poll finds completion
        return {
            "prompt_id": prompt_id,
            "client_id": client_id,
            "status": "queued",
            "batch_size": batch_size,
        }

    except httpx.ConnectError:
        _clear_gpu_pause()
        if lock_acquired:
            _release_gpu_lock()
        return {"error": "ComfyUI is not running. Start it with: docker compose up -d comfyui"}
    except Exception as e:
        _clear_gpu_pause()
        if lock_acquired:
            _release_gpu_lock()
        logger.warning(f"Generate error: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# POST /api/generate/img2img — Upload an image and generate variations
# ---------------------------------------------------------------------------
@router.post("/api/generate/img2img")
async def generate_img2img(
    image: UploadFile = File(...),
    prompt: str = Form(""),
    negative_prompt: str = Form(""),
    model: str = Form(""),
    steps: int = Form(20),
    cfg: float = Form(7.0),
    seed: int = Form(-1),
    denoise: float = Form(0.65),
    batch_size: int = Form(1),
    lora: str = Form(""),
    lora_strength: float = Form(0.8),
):
    """Upload an image and generate variations using img2img."""
    lock_acquired = False
    try:
        # Check GPU lock — reject if video pipeline is running
        if not _acquire_gpu_lock("image_gen:img2img"):
            return {"error": "GPU is busy — another generation is in progress. Please wait and try again."}
        lock_acquired = True

        # Free VRAM: unload Ollama models so ComfyUI gets full GPU memory
        await free_vram()

        # Pause GPU detectors while generating
        _set_gpu_pause()
        batch_size = min(batch_size, 4)

        # Upload the image to ComfyUI
        image_bytes = await image.read()
        filename = image.filename or "upload.png"

        async with httpx.AsyncClient() as client:
            upload_resp = await client.post(
                f"{COMFYUI_HOST}/upload/image",
                files={"image": (filename, image_bytes, image.content_type or "image/png")},
                data={"overwrite": "true"},
                timeout=30,
            )

        if upload_resp.status_code != 200:
            return {"error": f"ComfyUI upload failed: {upload_resp.text}"}

        upload_result = upload_resp.json()
        comfyui_image_name = upload_result.get("name", filename)
        logger.info(f"Image uploaded to ComfyUI: {comfyui_image_name}")

        # Build img2img workflow
        workflow = _build_img2img_workflow(
            image_name=comfyui_image_name,
            prompt=prompt,
            negative_prompt=negative_prompt,
            model=model,
            steps=steps,
            cfg=cfg,
            seed=seed,
            denoise=denoise,
            batch_size=batch_size,
            lora=lora,
            lora_strength=lora_strength,
        )

        # Queue with ComfyUI
        client_id = str(uuid.uuid4())
        payload = {"prompt": workflow, "client_id": client_id}

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{COMFYUI_HOST}/prompt",
                json=payload,
                timeout=30,
            )

        if resp.status_code != 200:
            return {"error": f"ComfyUI error: {resp.text}"}

        result = resp.json()
        prompt_id = result.get("prompt_id", "")
        logger.info(f"img2img queued: {prompt_id} (denoise={denoise})")

        # Metadata sidecar
        _gen_params[prompt_id] = {
            "type": "img2img",
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "model": model,
            "lora": lora,
            "lora_strength": lora_strength,
            "denoise": denoise,
            "steps": steps,
            "cfg": cfg,
            "seed": seed,
            "source_image": filename,
            "batch_size": batch_size,
            "timestamp": datetime.now().isoformat(),
        }

        # GPU lock stays held — released when history poll finds completion
        return {
            "prompt_id": prompt_id,
            "client_id": client_id,
            "status": "queued",
            "batch_size": batch_size,
        }

    except httpx.ConnectError:
        _clear_gpu_pause()
        if lock_acquired:
            _release_gpu_lock()
        return {"error": "ComfyUI is not running. Start it with: docker compose up -d comfyui"}
    except Exception as e:
        _clear_gpu_pause()
        if lock_acquired:
            _release_gpu_lock()
        logger.warning(f"img2img error: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# GET /api/generate/history/{prompt_id} — Get generation result(s)
# ---------------------------------------------------------------------------
@router.get("/api/generate/history/{prompt_id}")
async def get_generation_result(prompt_id: str):
    """Check if a generation is complete and return all images."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{COMFYUI_HOST}/history/{prompt_id}",
                timeout=10,
            )

        if resp.status_code != 200:
            return {"status": "pending"}

        data = resp.json()
        if prompt_id not in data:
            return {"status": "pending"}

        history = data[prompt_id]

        # Check for errors
        if history.get("status", {}).get("status_str") == "error":
            error_msg = history.get("status", {}).get("messages", [])
            return {"status": "error", "error": str(error_msg)}

        # Collect ALL output images (batch support)
        outputs = history.get("outputs", {})
        images = []

        logger.info(f"History for {prompt_id}: status={history.get('status', {}).get('status_str', '?')}, output_nodes={list(outputs.keys())}")

        for node_id, node_output in outputs.items():
            logger.info(f"  Node {node_id}: keys={list(node_output.keys())}")
            if "images" not in node_output:
                continue

            for img in node_output["images"]:
                filename = img.get("filename", "")
                subfolder = img.get("subfolder", "")
                img_type = img.get("type", "output")

                async with httpx.AsyncClient() as img_client:
                    img_resp = await img_client.get(
                        f"{COMFYUI_HOST}/view",
                        params={
                            "filename": filename,
                            "subfolder": subfolder,
                            "type": img_type,
                        },
                        timeout=30,
                    )

                if img_resp.status_code == 200:
                    b64 = base64.b64encode(img_resp.content).decode("utf-8")
                    images.append({
                        "image": b64,
                        "filename": filename,
                    })

                    # Auto-save to QNAP NAS with embedded metadata
                    try:
                        GENERATIONS_DIR.mkdir(parents=True, exist_ok=True)
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        save_name = f"{ts}_{filename}"
                        save_path = GENERATIONS_DIR / save_name

                        # Embed prompt metadata into PNG text chunks
                        params = _gen_params.get(prompt_id, {})
                        if params and filename.lower().endswith(".png"):
                            try:
                                from PIL import Image
                                from PIL.PngImagePlugin import PngInfo
                                import io

                                img_obj = Image.open(io.BytesIO(img_resp.content))
                                png_info = PngInfo()

                                # A1111-compatible "parameters" field
                                a1111_str = params.get("prompt", "")
                                if params.get("negative_prompt"):
                                    a1111_str += f"\nNegative prompt: {params['negative_prompt']}"
                                a1111_str += f"\nSteps: {params.get('steps', 20)}, "
                                a1111_str += f"Sampler: euler, "
                                a1111_str += f"CFG scale: {params.get('cfg', 7.0)}, "
                                a1111_str += f"Seed: {params.get('seed', -1)}, "
                                a1111_str += f"Size: {params.get('width', 1024)}x{params.get('height', 1024)}, "
                                a1111_str += f"Model: {params.get('model', 'unknown')}"
                                if params.get("lora"):
                                    a1111_str += f", LoRA: {params['lora']} ({params.get('lora_strength', 0.8)})"

                                png_info.add_text("parameters", a1111_str)
                                png_info.add_text("prompt", params.get("prompt", ""))
                                png_info.add_text("negative_prompt", params.get("negative_prompt", ""))
                                png_info.add_text("model", params.get("model", ""))
                                png_info.add_text("seed", str(params.get("seed", -1)))
                                png_info.add_text("steps", str(params.get("steps", 20)))
                                png_info.add_text("cfg", str(params.get("cfg", 7.0)))
                                if params.get("lora"):
                                    png_info.add_text("lora", params["lora"])

                                img_obj.save(str(save_path), pnginfo=png_info)
                                logger.info(f"Saved with embedded metadata: {save_name}")
                            except Exception as embed_err:
                                # Fallback: save raw bytes without metadata
                                save_path.write_bytes(img_resp.content)
                                logger.warning(f"Metadata embed failed, saved raw: {embed_err}")
                        else:
                            save_path.write_bytes(img_resp.content)

                        # Save metadata sidecar JSON (once per prompt_id)
                        meta_path = GENERATIONS_DIR / f"{ts}_metadata.json"
                        if prompt_id in _gen_params and not meta_path.exists():
                            import json as _json
                            meta_path.write_text(_json.dumps(
                                _gen_params[prompt_id], indent=2
                            ))
                            _gen_params.pop(prompt_id, None)
                        logger.info(f"Saved generation to QNAP: {save_name}")
                    except Exception as save_err:
                        logger.warning(f"Failed to save to QNAP: {save_err}")

        if images:
            # Generation complete — resume GPU detectors and release lock
            _clear_gpu_pause()
            _release_gpu_lock()
            return {
                "status": "complete",
                "images": images,
                # Backwards compat: single image fields
                "image": images[0]["image"],
                "filename": images[0]["filename"],
            }

        return {"status": "pending"}

    except httpx.ConnectError:
        return {"error": "ComfyUI is not running"}
    except Exception as e:
        logger.warning(f"History check error: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# GET /api/generate/models — List available checkpoint models
# ---------------------------------------------------------------------------
@router.get("/api/generate/models")
async def list_models():
    """List checkpoint models available in ComfyUI."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{COMFYUI_HOST}/object_info/CheckpointLoaderSimple",
                timeout=10,
            )

        if resp.status_code != 200:
            return {"models": [], "error": "Could not fetch model list"}

        data = resp.json()
        ckpt_info = data.get("CheckpointLoaderSimple", {})
        inputs = ckpt_info.get("input", {}).get("required", {})
        model_list = inputs.get("ckpt_name", [[]])[0]

        return {"models": model_list}

    except httpx.ConnectError:
        return {"models": [], "error": "ComfyUI is not running"}
    except Exception as e:
        return {"models": [], "error": str(e)}


# ---------------------------------------------------------------------------
# GET /api/generate/loras — List available LoRA models
# ---------------------------------------------------------------------------
@router.get("/api/generate/loras")
async def list_loras():
    """List LoRA models available in ComfyUI."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{COMFYUI_HOST}/object_info/LoraLoader",
                timeout=10,
            )

        if resp.status_code != 200:
            return {"loras": []}

        data = resp.json()
        lora_info = data.get("LoraLoader", {})
        inputs = lora_info.get("input", {}).get("required", {})
        lora_list = inputs.get("lora_name", [[]])[0]

        return {"loras": lora_list}

    except httpx.ConnectError:
        return {"loras": [], "error": "ComfyUI is not running"}
    except Exception as e:
        return {"loras": [], "error": str(e)}


# ---------------------------------------------------------------------------
# GET /api/generate/status — ComfyUI health check
# ---------------------------------------------------------------------------
@router.get("/api/generate/status")
async def comfyui_status():
    """Check ComfyUI health and queue status."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{COMFYUI_HOST}/system_stats",
                timeout=5,
            )

        if resp.status_code == 200:
            stats = resp.json()
            return {
                "online": True,
                "system_stats": stats,
            }
        return {"online": False, "error": "Bad response from ComfyUI"}

    except httpx.ConnectError:
        return {"online": False, "error": "ComfyUI is not running"}
    except Exception as e:
        return {"online": False, "error": str(e)}


# ---------------------------------------------------------------------------
# POST /api/generate/cancel — Interrupt ComfyUI's current generation
# ---------------------------------------------------------------------------
@router.post("/api/generate/cancel")
async def cancel_generation():
    """Interrupt the current ComfyUI generation and clear pending queue."""
    try:
        async with httpx.AsyncClient() as client:
            # 1. Interrupt the currently running generation
            resp = await client.post(f"{COMFYUI_HOST}/interrupt", timeout=5)

            # 2. Clear ALL pending queue items so stale jobs don't block next sweep
            try:
                queue_resp = await client.get(f"{COMFYUI_HOST}/queue", timeout=5)
                if queue_resp.status_code == 200:
                    queue_data = queue_resp.json()
                    pending = queue_data.get("queue_pending", [])
                    if pending:
                        # Delete all pending items from ComfyUI queue
                        pending_ids = [item[1] for item in pending if len(item) > 1]
                        if pending_ids:
                            await client.post(
                                f"{COMFYUI_HOST}/queue",
                                json={"delete": pending_ids},
                                timeout=5,
                            )
                            logger.info(f"Cleared {len(pending_ids)} pending queue items from ComfyUI")
            except Exception as qe:
                logger.warning(f"Failed to clear ComfyUI queue: {qe}")

        # Evict stale _gen_params entries (older than 1 hour) to prevent leaks
        _evict_stale_gen_params()

        # Resume GPU detectors and release lock
        _clear_gpu_pause()
        _release_gpu_lock()

        return {"success": True, "status_code": resp.status_code}
    except Exception as e:
        return {"error": str(e)}


def _evict_stale_gen_params(max_age_seconds: int = 3600):
    """Remove _gen_params entries older than max_age_seconds."""
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(seconds=max_age_seconds)
    stale_ids = []
    for pid, params in _gen_params.items():
        ts_str = params.get("timestamp", "")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts < cutoff:
                    stale_ids.append(pid)
            except (ValueError, TypeError):
                stale_ids.append(pid)  # can't parse → evict
    for pid in stale_ids:
        _gen_params.pop(pid, None)
    if stale_ids:
        logger.info(f"Evicted {len(stale_ids)} stale _gen_params entries")


# ---------------------------------------------------------------------------
# VRAM Management — Unload/reload Ollama to free GPU memory
# ---------------------------------------------------------------------------
@router.post("/api/generate/vram/free")
async def free_vram():
    """Unload all Ollama models from VRAM to free GPU memory for generation."""
    global _vram_mode
    try:
        # Get list of loaded models
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{OLLAMA_HOST}/api/ps", timeout=5)

        if resp.status_code == 200:
            models_data = resp.json()
            loaded = models_data.get("models", [])

            # Unload each loaded model by setting keep_alive to 0
            for m in loaded:
                model_name = m.get("name", "")
                if model_name:
                    async with httpx.AsyncClient() as client:
                        await client.post(
                            f"{OLLAMA_HOST}/api/generate",
                            json={"model": model_name, "keep_alive": 0},
                            timeout=10,
                        )
                    logger.info(f"Unloaded Ollama model: {model_name}")

        _vram_mode = "generate"
        return {"status": "ok", "mode": "generate", "message": "Ollama models unloaded from VRAM"}

    except httpx.ConnectError:
        _vram_mode = "generate"
        return {"status": "ok", "mode": "generate", "message": "Ollama not running — VRAM already free"}
    except Exception as e:
        logger.warning(f"VRAM free error: {e}")
        return {"status": "error", "error": str(e)}


@router.post("/api/generate/vram/restore")
async def restore_vram():
    """Ping Ollama to reload the default model back into VRAM."""
    global _vram_mode
    try:
        # Just send a simple generate request to trigger model loading
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": "qwen3:14b", "prompt": "hi", "stream": False},
                timeout=120,
            )
        _vram_mode = "chat"
        logger.info("Ollama model reloaded into VRAM")
        return {"status": "ok", "mode": "chat", "message": "Ollama model reloaded"}

    except httpx.ConnectError:
        return {"status": "error", "error": "Ollama is not running"}
    except Exception as e:
        logger.warning(f"VRAM restore error: {e}")
        return {"status": "error", "error": str(e)}


@router.get("/api/generate/vram/mode")
async def get_vram_mode():
    """Get current VRAM mode (chat or generate).
    Self-heals: if Ollama shows the chat model running, correct stale state."""
    global _vram_mode
    if _vram_mode == "generate":
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{OLLAMA_HOST}/api/ps", timeout=3)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                for m in models:
                    if "qwen3" in m.get("name", "").lower():
                        _vram_mode = "chat"
                        logger.info("VRAM mode self-healed: Qwen3 detected in VRAM")
                        break
        except Exception:
            pass
    return {"mode": _vram_mode}


# ---------------------------------------------------------------------------
# Gallery — Browse past generated images
# ---------------------------------------------------------------------------
@router.get("/api/generate/gallery")
async def list_gallery(limit: int = 100, offset: int = 0):
    """List generated images from ComfyUI output and QNAP, newest first."""
    images = []

    # Scan ComfyUI output directory
    for scan_dir in [COMFYUI_OUTPUT_DIR, GENERATIONS_DIR]:
        if not scan_dir.exists():
            continue
        for f in scan_dir.rglob("*"):
            if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                try:
                    stat = f.stat()
                    images.append({
                        "filename": f.name,
                        "path": str(f),
                        "source": "comfyui" if "comfyui" in str(scan_dir) else "qnap",
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    })
                except OSError:
                    continue

    # Deduplicate by filename (prefer QNAP copy)
    seen = {}
    for img in images:
        if img["filename"] not in seen or img["source"] == "qnap":
            seen[img["filename"]] = img
    images = list(seen.values())

    # Sort newest first
    images.sort(key=lambda x: x["modified"], reverse=True)

    total = len(images)
    page = images[offset : offset + limit]

    return {
        "images": [
            {
                "filename": img["filename"],
                "source": img["source"],
                "size_kb": round(img["size"] / 1024, 1),
                "modified": img["modified"],
            }
            for img in page
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/api/generate/gallery/image/{filename}")
async def get_gallery_image(filename: str):
    """Serve a generated image by filename."""
    # Security: prevent path traversal
    safe_name = Path(filename).name
    if safe_name != filename:
        return {"error": "Invalid filename"}

    # Check ComfyUI output first, then QNAP
    for scan_dir in [COMFYUI_OUTPUT_DIR, GENERATIONS_DIR]:
        # Check root and subdirectories
        for f in scan_dir.rglob(safe_name):
            if f.is_file():
                media_type = "image/png"
                if f.suffix.lower() in (".jpg", ".jpeg"):
                    media_type = "image/jpeg"
                elif f.suffix.lower() == ".webp":
                    media_type = "image/webp"
                return FileResponse(str(f), media_type=media_type)

    return {"error": "Image not found"}


@router.get("/api/generate/gallery/metadata/{filename}")
async def get_image_metadata(filename: str):
    """Extract prompt/generation metadata from a PNG image."""
    import json as _json
    from PIL import Image as PILImage

    safe_name = Path(filename).name
    if safe_name != filename:
        return {"error": "Invalid filename"}

    # Find the image file
    filepath = None
    for scan_dir in [COMFYUI_OUTPUT_DIR, GENERATIONS_DIR]:
        for f in scan_dir.rglob(safe_name):
            if f.is_file():
                filepath = f
                break
        if filepath:
            break

    if not filepath:
        return {"error": "Image not found"}

    meta = {"filename": filename, "size": None}

    try:
        img = PILImage.open(str(filepath))
        meta["size"] = f"{img.width}x{img.height}"

        # ComfyUI stores metadata in PNG text chunks
        info = img.info or {}

        # Try 'prompt' key (ComfyUI standard)
        if "prompt" in info:
            try:
                prompt_data = _json.loads(info["prompt"])
                # Parse ComfyUI node graph to extract useful info
                for node_id, node in prompt_data.items():
                    class_type = node.get("class_type", "")
                    inputs = node.get("inputs", {})

                    if class_type == "KSampler":
                        meta["steps"] = inputs.get("steps")
                        meta["cfg"] = inputs.get("cfg")
                        meta["seed"] = inputs.get("seed")
                        meta["sampler"] = inputs.get("sampler_name")

                    elif class_type in ("CLIPTextEncode",):
                        text = inputs.get("text", "")
                        if text and not meta.get("negative_prompt"):
                            # Heuristic: shorter or "negative" texts are negative prompts
                            if meta.get("prompt_text"):
                                meta["negative_prompt"] = text
                            else:
                                meta["prompt_text"] = text

                    elif class_type in ("CheckpointLoaderSimple",):
                        meta["model"] = inputs.get("ckpt_name", "")

                    elif class_type in ("LoraLoader",):
                        meta["lora"] = inputs.get("lora_name", "")
                        meta["lora_strength"] = inputs.get("strength_model")
            except Exception:
                meta["raw_prompt"] = info["prompt"][:500]

        # Fallback: check for 'parameters' key (A1111 / some other UIs)
        elif "parameters" in info:
            meta["raw_parameters"] = info["parameters"][:800]

        img.close()
    except Exception as e:
        meta["error"] = f"Could not read metadata: {str(e)}"

    return meta


# ---------------------------------------------------------------------------
# Prompt History — Server-side storage (syncs across devices)
# ---------------------------------------------------------------------------
PROMPT_HISTORY_PATH = Path("/data/prompt_history.json")


def _load_prompt_history() -> list:
    """Load prompt history from disk."""
    if PROMPT_HISTORY_PATH.exists():
        try:
            return json.loads(PROMPT_HISTORY_PATH.read_text())
        except Exception:
            return []
    return []


def _save_prompt_history(history: list):
    """Save prompt history to disk."""
    PROMPT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_HISTORY_PATH.write_text(json.dumps(history, indent=2))


@router.get("/api/generate/prompt-history")
async def get_prompt_history():
    """Get all prompt history entries."""
    return {"history": _load_prompt_history()}


@router.post("/api/generate/prompt-history")
async def add_prompt_history(entry: dict):
    """Add a new prompt history entry."""
    history = _load_prompt_history()
    history.insert(0, entry)
    if len(history) > 50:
        history = history[:50]
    _save_prompt_history(history)
    return {"ok": True, "count": len(history)}


@router.delete("/api/generate/prompt-history")
async def clear_prompt_history():
    """Clear all prompt history."""
    _save_prompt_history([])
    return {"ok": True}
