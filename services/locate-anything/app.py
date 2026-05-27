"""locate-anything — open-vocabulary visual grounding service.

Wraps nvidia/LocateAnything-3B (NON-COMMERCIAL NVIDIA License — opt-in, local
only; the model lazy-downloads from HF Hub at runtime, never bundled).

POST /locate  (multipart: image=<file>, phrase=<str>, [mode], [max_dim])
  -> {"count": N, "boxes": [{"x1","y1","x2","y2"} ...],
      "points": [{"x","y"} ...], "annotated_png_b64": "...", "infer_ms": int}

GET /health -> {"ok": bool, "model_loaded": bool}

Notes baked in from the POC:
- Default generation_mode="slow": clean output (no repetition loop) AND faster,
  because it terminates instead of looping to max_new_tokens.
- The model's MoonViT attention is O(tokens^2) in memory on the sdpa fallback,
  so the frame the MODEL sees is downscaled to max_dim; boxes (normalized
  0-1000) are drawn on the full-res original.
- IoU-NMS collapses near-duplicate boxes the model can still emit.
"""
import base64
import io
import logging
import os
import re
import threading
import time

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw

logging.basicConfig(level=logging.INFO, format="%(asctime)s [locate] %(message)s")
log = logging.getLogger("locate")

MODEL = os.getenv("LOCATE_MODEL", "nvidia/LocateAnything-3B")
DEFAULT_MODE = os.getenv("LOCATE_GEN_MODE", "slow")
DEFAULT_MAX_DIM = int(os.getenv("LOCATE_MAX_DIM", "1536"))
NMS_IOU = float(os.getenv("LOCATE_NMS_IOU", "0.85"))

app = FastAPI(title="locate-anything")

# Lazy-loaded singletons (loaded on first /locate, then resident).
_model = None
_tokenizer = None
_processor = None
_load_lock = threading.Lock()
# Serialize inference — one GPU, generative model; concurrent generate() calls
# would contend and risk OOM.
_infer_lock = threading.Lock()


def _ensure_loaded():
    global _model, _tokenizer, _processor
    if _model is not None:
        return
    with _load_lock:
        if _model is not None:
            return
        import torch
        from transformers import AutoModel, AutoTokenizer, AutoProcessor
        t0 = time.time()
        log.info(f"loading {MODEL} (bf16, cuda) …")
        _tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
        _processor = AutoProcessor.from_pretrained(MODEL, trust_remote_code=True)
        m = AutoModel.from_pretrained(
            MODEL, torch_dtype=torch.bfloat16, trust_remote_code=True,
        ).to("cuda")
        m.train(False)  # inference mode
        _model = m
        log.info(f"loaded in {time.time()-t0:.0f}s; "
                 f"VRAM {torch.cuda.memory_allocated()/1e9:.1f} GB")


def _iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / float(area_a + area_b - inter)


def _nms(boxes):
    """Greedy NMS on normalized (0-1000) boxes to drop the model's near-dup
    repeats. Boxes are (x1,y1,x2,y2)."""
    kept = []
    for box in boxes:
        if all(_iou(box, k) < NMS_IOU for k in kept):
            kept.append(box)
    return kept


def _run(image: Image.Image, phrase: str, mode: str, max_dim: int) -> dict:
    import torch
    _ensure_loaded()
    W, H = image.size

    infer_image = image
    if max(W, H) > max_dim:
        s = max_dim / float(max(W, H))
        infer_image = image.resize((int(W * s), int(H * s)))

    messages = [{"role": "user", "content": [
        {"type": "image", "image": infer_image},
        {"type": "text", "text": phrase},
    ]}]
    text = _processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True)
    images, videos = _processor.process_vision_info(messages)
    inputs = _processor(text=[text], images=images, videos=videos,
                        return_tensors="pt").to("cuda")

    t0 = time.time()
    with _infer_lock, torch.no_grad():
        out = _model.generate(
            pixel_values=inputs["pixel_values"].to(torch.bfloat16),
            input_ids=inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            image_grid_hws=inputs.get("image_grid_hws"),
            tokenizer=_tokenizer,
            max_new_tokens=8192,
            generation_mode=mode,
            do_sample=False,
            use_cache=True,
        )
    infer_ms = int((time.time() - t0) * 1000)
    text_out = out if isinstance(out, str) else str(out)

    raw_boxes, points = [], []
    for block in re.findall(r"<box>(.*?)</box>", text_out, flags=re.S):
        nums = [int(x) for x in re.findall(r"-?\d+", block)]
        if len(nums) >= 4:
            raw_boxes.append(tuple(nums[:4]))
        elif len(nums) == 2:
            points.append(tuple(nums[:2]))
    boxes = _nms(raw_boxes)

    # Draw on the full-res original (coords are normalized 0-1000).
    annotated = image.copy()
    draw = ImageDraw.Draw(annotated)
    out_boxes = []
    for (x1, y1, x2, y2) in boxes:
        px = (x1 / 1000.0 * W, y1 / 1000.0 * H, x2 / 1000.0 * W, y2 / 1000.0 * H)
        draw.rectangle(px, outline=(0, 255, 0), width=max(2, W // 400))
        out_boxes.append({"x1": px[0], "y1": px[1], "x2": px[2], "y2": px[3]})
    out_points = []
    for (x, y) in points:
        cx, cy = x / 1000.0 * W, y / 1000.0 * H
        r = max(4, W // 250)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 0, 0), width=4)
        out_points.append({"x": cx, "y": cy})

    buf = io.BytesIO()
    annotated.save(buf, format="PNG")
    return {
        "count": len(out_boxes) + len(out_points),
        "boxes": out_boxes,
        "points": out_points,
        "annotated_png_b64": base64.b64encode(buf.getvalue()).decode(),
        "infer_ms": infer_ms,
        "raw_blocks": len(raw_boxes) + len(points),
    }


@app.get("/health")
def health():
    return {"ok": True, "model_loaded": _model is not None, "model": MODEL}


@app.post("/locate")
async def locate(
    image: UploadFile = File(...),
    phrase: str = Form(...),
    mode: str = Form(DEFAULT_MODE),
    max_dim: int = Form(DEFAULT_MAX_DIM),
):
    if mode not in ("slow", "hybrid", "fast"):
        return JSONResponse({"error": "mode must be slow|hybrid|fast"}, status_code=400)
    phrase = (phrase or "").strip()
    if not phrase:
        return JSONResponse({"error": "phrase required"}, status_code=400)
    try:
        raw = await image.read()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return JSONResponse({"error": "could not read image"}, status_code=400)
    try:
        return _run(img, phrase, mode, max_dim)
    except Exception as e:
        log.exception("locate failed")
        return JSONResponse({"error": f"inference failed: {type(e).__name__}"},
                            status_code=500)
