# Vehicle Attributes Phase 3 (Classifier v0) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Apply CLAUDE.md §15 verification discipline after every implementer subagent report.

**Goal:** Ship the v0 ConvNeXt-Tiny multi-head classifier that fills in Phase 1's null `metadata.json.attributes` block with predicted color + body type + make (on every track) + model (drive-by tracks only).

**Architecture:** Feature-flagged extension of the existing Phase 1 attribute service. New `classifier.py` module loads a multi-head ConvNeXt-Tiny model from HF Hub on first boot, runs inference on the buffered HD crops at `vehicle_gone` flush time, votes across crops, applies confidence thresholds + a make-model consistency check, and merges the results into the metadata.json the storage layer is about to write. Reservoir sampling replaces Phase 1's first-N buffer capping so the kept crops span the full track lifetime (entry→mid→exit angles), which matters for the model head on drive-by tracks. Gated by `ENABLE_CLASSIFIER` env so the PR can merge + deploy before trained weights exist.

**Tech Stack:** PyTorch 2.4 (`+cu128`), `timm==1.0.11`, ConvNeXt-Tiny, HuggingFace Hub. Inherits `vision-labs-base:cuda12.8` (was `python:3.11-slim` in Phase 1).

**Spec:** `docs/superpowers/specs/2026-05-21-vehicle-attributes-phase-3-classifier-design.md`

**Note on PyTorch eval mode:** all `.train(False)` calls in the code blocks below are PyTorch's idiom for "set model to evaluation mode" (the same as `.eval()` but the explicit form avoids ambiguity with Python's `eval()`). Functionally identical.

---

## File map

**Create:**
- `services/vehicle-attributes/classifier.py` — preprocessing + voting + consistency check + model loader + `run_classifier_and_vote()` entry point
- `services/vehicle_attributes/classifier.py` — pytest re-export shim (mirrors buffer/storage/service shim pattern)
- `tests/test_vehicle_attributes_classifier.py` — unit tests for preprocessing, voting, consistency check, threshold gating, run_classifier_and_vote with mocked model
- `scripts/vehicle_attributes/train_color_head.py` — train ConvNeXt-Tiny color head on VeRi-776
- `scripts/vehicle_attributes/train_multihead.py` — fine-tune full multi-head (color + body + make + model) using the timm Stanford Cars checkpoint as init for the make/body/model heads, the trained color head from `train_color_head.py` for the color head
- `scripts/vehicle_attributes/upload_weights.py` — upload the combined checkpoint to a HF Hub repo
- `scripts/vehicle_attributes/README.md` — how-to-train documentation
- `services/vehicle-attributes/classes/` directory containing:
  - `color_classes.json` — 10 VeRi-776 color labels
  - `body_classes.json` — 8 body-type labels + the Stanford Cars→body mapping
  - `make_classes.json` — ~50 unique manufacturers from Stanford Cars-196
  - `model_classes.json` — 196 Stanford Cars classes (model-level labels)
  - `make_to_models.json` — reverse mapping `{"Honda": ["Civic", "Accord", ...], ...}` used by the consistency check

**Modify:**
- `services/vehicle-attributes/Dockerfile` — swap `python:3.11-slim` base for `vision-labs-base:cuda12.8`, add explicit torch + timm install, COPY classifier.py + classes/
- `services/vehicle-attributes/requirements.txt` — add `timm==1.0.11`, `huggingface_hub`, `safetensors`
- `services/vehicle-attributes/buffer.py` — replace first-N capping in `TrackBuffer.append` with reservoir sampling
- `services/vehicle-attributes/service.py` — call `classifier.run_classifier_and_vote()` in `_flush` when `ENABLE_CLASSIFIER=1`, pass `attributes` to `flush_buffer_to_disk`
- `services/vehicle-attributes/storage.py` — accept `attributes` kwarg in `flush_buffer_to_disk`, default to all-null block (Phase 1 backward-compat)
- `tests/test_vehicle_attributes_buffer.py` — fix `test_buffer_caps_at_max_crops` for reservoir sampling (use `random.seed`)
- `tests/test_vehicle_attributes_service.py` — add ENABLE_CLASSIFIER=0 case (default, no classifier call) + ENABLE_CLASSIFIER=1 case (calls mocked classifier, attributes merged into metadata)
- `tests/test_vehicle_attributes_storage.py` — add test for non-null attributes kwarg path
- `docker-compose.yml` — across all 20 `vehicle-attributes-camN` blocks: add `ENABLE_CLASSIFIER`, `VEHICLE_ATTR_HF_REPO`, `VEHICLE_ATTR_MODEL`, threshold env vars + `vehicle-attribute-models:/models` volume; declare the `vehicle-attribute-models:` volume at the bottom of the file alongside the others
- `services/dashboard/static/js/dashboard/browse.js` — render attribute row in track cards
- `services/dashboard/static/css/style.css` — `.track-attrs` + `.track-attrs-beta` classes
- `CHANGELOG.md` — `[Unreleased] → Added` entry
- `CONTEXT.md` — extend §4.6 vehicle-attributes service entry to mention the classifier integration

---

## Task 0: Branch + baseline

**Files:** none (git ops only)

- [ ] **Step 0.1: Create the feature branch**

```bash
cd /home/mongo/projects/vision-labs
git checkout main && git pull
git checkout -b feat/vehicle-attributes-phase-3-classifier
```

- [ ] **Step 0.2: Verify clean baseline**

```bash
source .venv-test/bin/activate && pytest -q 2>&1 | tail -2
ruff check . 2>&1 | tail -1
```

Expected: `419 passed`, `All checks passed!`.

---

## Task 1: Reservoir sampling in TrackBuffer

**Files:**
- Modify: `services/vehicle-attributes/buffer.py:25-48` (`TrackBuffer` class + `append`)
- Modify: `tests/test_vehicle_attributes_buffer.py:24-39` (existing `test_buffer_caps_at_max_crops`)

The Phase 1 buffer uses first-N capping: drops new crops once full. For Phase 3's model head we want uniform statistical coverage across the track's full lifetime (entry→mid→exit angles). Reservoir sampling (Algorithm R) gives that with no extra memory.

- [ ] **Step 1.1: Read the existing buffer.py to confirm structure**

```bash
cat services/vehicle-attributes/buffer.py
```

Expected: see the `TrackBuffer` dataclass with `crops/confidences/bboxes` lists, `max_crops=8`, and `append()` that drops on `is_full()`.

- [ ] **Step 1.2: Update the failing test — `test_buffer_caps_at_max_crops`**

Open `tests/test_vehicle_attributes_buffer.py`. Replace the existing test:

```python
def test_buffer_caps_at_max_crops():
    """Reservoir sampling: buffer always at max_crops once seen > max, and
    the kept samples are spread across the input sequence (not just first N).

    With random.seed(42), the reservoir produces a deterministic-but-spread
    sample set — exact indices don't matter, but they must NOT be [0,1,2]
    (which is what first-N would keep)."""
    import random
    random.seed(42)
    b = TrackBuffer(track_id="v", camera_id="cam1", first_seen=0.0,
                    max_crops=3)
    for i in range(10):
        b.append(crop=bytes([i]), yolo_conf=0.5, bbox=[0, 0, 1, 1])
    assert len(b.crops) == 3, "buffer must stay at cap"
    assert b.is_full() is True
    # Reservoir-specific: NOT just the first N
    kept_bytes = sorted(int(c[0]) for c in b.crops)
    assert kept_bytes != [0, 1, 2], (
        "first-N retention would be [0,1,2]; reservoir picks spread samples"
    )


def test_buffer_reservoir_sampling_is_statistically_uniform():
    """Across many runs, each input index has ~equal probability of being
    in the final reservoir. Cheaper test: sum the kept indices across many
    runs and confirm the mean lands near the expected uniform mean."""
    import random
    n_runs = 200
    max_crops = 3
    n_inputs = 10
    sum_kept_indices = 0
    for run in range(n_runs):
        random.seed(run)
        b = TrackBuffer(track_id="v", camera_id="cam1", first_seen=0.0,
                        max_crops=max_crops)
        for i in range(n_inputs):
            b.append(crop=bytes([i]), yolo_conf=0.5, bbox=[0, 0, 1, 1])
        sum_kept_indices += sum(int(c[0]) for c in b.crops)
    # Uniform expectation: each index i has prob max_crops/n_inputs of being
    # kept. Sum of kept indices per run ≈ max_crops * (n_inputs-1)/2 = 3 * 4.5
    # = 13.5. Across 200 runs, ~2700.
    expected = n_runs * max_crops * (n_inputs - 1) / 2
    observed = sum_kept_indices
    # Allow ±20% drift (binomial noise across 200 runs)
    assert 0.8 * expected < observed < 1.2 * expected, (
        f"reservoir mean drift: expected ~{expected}, got {observed}"
    )
```

- [ ] **Step 1.3: Run tests to confirm they fail**

```bash
source .venv-test/bin/activate
pytest tests/test_vehicle_attributes_buffer.py -v 2>&1 | tail -15
```

Expected: 2 FAILs (the two tests above), 3 PASS (the other buffer tests are unaffected).

- [ ] **Step 1.4: Implement reservoir sampling in `TrackBuffer.append`**

Open `services/vehicle-attributes/buffer.py`. Replace the `append` method (and add an internal counter to the dataclass) so the file reads:

```python
"""Per-track HD-crop buffer for vehicle-attributes Phase 1+3.

Phase 1 used first-N capping. Phase 3 switches to reservoir sampling
(Algorithm R) so the kept crops uniformly span the track's full lifetime
— necessary for the model head's multi-view voting on drive-by tracks
where the entry/mid/exit angles all carry information.
"""
import random
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrackBuffer:
    track_id: str
    camera_id: str
    first_seen: float
    crops: list[bytes] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    bboxes: list[list[int]] = field(default_factory=list)
    last_sampled_at: float = 0.0
    max_crops: int = 8
    _n_seen: int = 0  # total samples observed (for reservoir math)

    def append(self, crop: bytes, yolo_conf: float, bbox: list[int]) -> None:
        """Reservoir sampling (Algorithm R): once the buffer fills, each
        new sample has probability max_crops/_n_seen of replacing a random
        existing slot. Result: each input has equal probability of being
        in the final reservoir, regardless of arrival order.
        """
        self._n_seen += 1
        if len(self.crops) < self.max_crops:
            self.crops.append(crop)
            self.confidences.append(yolo_conf)
            self.bboxes.append(list(bbox))
            return
        # Buffer full — reservoir replace with decreasing probability
        j = random.randrange(self._n_seen)
        if j < self.max_crops:
            self.crops[j] = crop
            self.confidences[j] = yolo_conf
            self.bboxes[j] = list(bbox)

    def is_full(self) -> bool:
        return len(self.crops) >= self.max_crops

    def hero_index(self) -> Optional[int]:
        if not self.confidences:
            return None
        return max(range(len(self.confidences)),
                   key=lambda i: self.confidences[i])
```

- [ ] **Step 1.5: Run buffer tests to confirm pass**

```bash
pytest tests/test_vehicle_attributes_buffer.py -v 2>&1 | tail -10
```

Expected: 6 PASS (5 original tests still pass — they only check non-cap behavior — + the 2 modified/new reservoir tests).

- [ ] **Step 1.6: Run full suite to catch upstream consumers**

```bash
pytest -q 2>&1 | tail -3 && ruff check . 2>&1 | tail -1
```

Expected: 420 passed (was 419, +1 new reservoir uniformity test), ruff clean.

- [ ] **Step 1.7: Commit**

```bash
git add services/vehicle-attributes/buffer.py tests/test_vehicle_attributes_buffer.py
git commit -m "vehicle-attributes: reservoir sampling in TrackBuffer for uniform multi-view"
```

---

## Task 2: Dockerfile + requirements.txt updates

**Files:**
- Modify: `services/vehicle-attributes/Dockerfile`
- Modify: `services/vehicle-attributes/requirements.txt`

Swap the base image for `vision-labs-base:cuda12.8` + add explicit torch + timm + HF Hub deps. Note `vision-labs-base` deliberately doesn't ship PyTorch (per CLAUDE.md and `services/base/Dockerfile` comment); each service installs torch on top.

- [ ] **Step 2.1: Read pose-detector's Dockerfile to mirror its torch install pattern**

```bash
grep -A 30 'FROM vision-labs-base' services/pose-detector/Dockerfile
```

Expected: shows `FROM vision-labs-base:cuda12.8`, a `pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128` line, then service deps.

- [ ] **Step 2.2: Rewrite the vehicle-attributes Dockerfile**

Replace `services/vehicle-attributes/Dockerfile` with:

```dockerfile
# services/vehicle-attributes/Dockerfile
#
# PURPOSE:
#   Per-camera vehicle attribute pipeline.
#
#   Phase 1: capture HD crops on vehicle_sample events, write per-track
#   dirs with metadata.json (null attributes block) at vehicle_gone flush.
#
#   Phase 3 (this version): runs a ConvNeXt-Tiny multi-head classifier
#   (color + body + make + model heads) on the buffered crops at flush
#   time, fills the attributes block. Gated by ENABLE_CLASSIFIER env so
#   the image can be deployed before trained weights exist on HF Hub.
#
# INHERITS FROM vision-labs-base:cuda12.8 (services/base/Dockerfile).
# vision-labs-base does NOT ship PyTorch — each service installs the
# framework it needs on top. Mirror of pose-detector's pattern.

FROM vision-labs-base:cuda12.8

WORKDIR /app

# PyTorch matched to the base image's CUDA 12.8 runtime
RUN pip install --no-cache-dir \
      torch torchvision \
      --index-url https://download.pytorch.org/whl/cu128

# Service deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Source files + the static class label JSONs
COPY service.py buffer.py storage.py classifier.py ./
COPY classes/ ./classes/

# Snapshot output dir gets bind-mounted at runtime; declared here for
# image-runs-without-volume sanity.
RUN mkdir -p /data /models

CMD ["python", "-u", "service.py"]
```

- [ ] **Step 2.3: Update requirements.txt**

Replace `services/vehicle-attributes/requirements.txt` with:

```
redis==5.3.1
opencv-python-headless==4.13.0.92
Pillow>=10.4.0
numpy>=1.26.4
timm==1.0.11
huggingface_hub>=0.26
safetensors>=0.4
```

`torch` is intentionally NOT in requirements.txt — it's installed earlier in the Dockerfile via the CUDA wheel index.

- [ ] **Step 2.4: Verify the Dockerfile parses (defer real build to Task 13)**

```bash
docker compose config vehicle-attributes-cam1 2>&1 | tail -5
```

Expected: no parse error from compose.

- [ ] **Step 2.5: Commit**

```bash
git add services/vehicle-attributes/Dockerfile services/vehicle-attributes/requirements.txt
git commit -m "vehicle-attributes: swap to cuda12.8 base + torch/timm/HF deps"
```

---

## Task 3: Compose env additions + new volume

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 3.1: Confirm current shape of vehicle-attributes-cam1 block**

```bash
grep -A 20 '^  vehicle-attributes-cam1:' docker-compose.yml | head -25
```

- [ ] **Step 3.2: Patch all 20 blocks via Python**

Save this script to `/tmp/patch_compose.py` and run it:

```python
import re
from pathlib import Path

f = Path('/home/mongo/projects/vision-labs/docker-compose.yml')
text = f.read_text()

new_env_block = """      # Phase 3 classifier — disabled by default until weights exist on HF Hub
      - ENABLE_CLASSIFIER=${ENABLE_CLASSIFIER:-0}
      - VEHICLE_ATTR_HF_REPO=${VEHICLE_ATTR_HF_REPO:-gammahazard/vision-labs-vehicle-attributes}
      - VEHICLE_ATTR_MODEL=${VEHICLE_ATTR_MODEL:-convnext_tiny_v0}
      - VEHICLE_ATTR_MODELS_DIR=/models
      - COLOR_CONF_THRESHOLD=${COLOR_CONF_THRESHOLD:-0.55}
      - BODY_CONF_THRESHOLD=${BODY_CONF_THRESHOLD:-0.55}
      - MAKE_CONF_THRESHOLD=${MAKE_CONF_THRESHOLD:-0.55}
      - MODEL_CONF_THRESHOLD=${MODEL_CONF_THRESHOLD:-0.65}
      - DETECTOR_GPU=${DETECTOR_GPU:-0}
      - CUDA_VISIBLE_DEVICES=${DETECTOR_GPU:-0}
      - CUDA_DEVICE_ORDER=PCI_BUS_ID"""

new_volume_line = "      - vehicle-attribute-models:/models"

new_deploy_block = """    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ['${DETECTOR_GPU:-0}']
              capabilities: [gpu]"""

blocks = re.findall(
    r'(  vehicle-attributes-cam\d+:\n.*?)(?=\n  [a-zA-Z]|\n  # |\Z)',
    text, flags=re.DOTALL,
)
assert len(blocks) == 20, f"expected 20 blocks, found {len(blocks)}"

for block in blocks:
    new = block
    new = re.sub(
        r'(\n      - TZ=\$\{LOCATION_TIMEZONE:-America/Toronto\})\n(    volumes:)',
        r'\1\n' + new_env_block + r'\n\2',
        new,
    )
    new = re.sub(
        r'(      - snapshot-data:/data/snapshots)\n(    depends_on:)',
        r'\1\n' + new_volume_line + r'\n\2',
        new,
    )
    if 'deploy:' not in new:
        new = re.sub(
            r'(    depends_on:\n.*?\n)(    restart: )',
            r'\1' + new_deploy_block + r'\n\2',
            new, flags=re.DOTALL,
        )
    text = text.replace(block, new, 1)

text = re.sub(
    r'(\nvolumes:\n.*?  snapshot-data:\n)',
    r'\1  vehicle-attribute-models:\n',
    text, flags=re.DOTALL,
)

f.write_text(text)
print("patched")
```

Run it:

```bash
python3 /tmp/patch_compose.py
```

Expected: prints `patched`.

- [ ] **Step 3.3: Verify compose is still valid**

```bash
docker compose config --services 2>&1 | grep vehicle-attributes | wc -l
docker compose config vehicle-attributes-cam1 2>&1 | grep -E 'ENABLE_CLASSIFIER|vehicle-attribute-models' | head -5
```

Expected: `20`, and the env var + volume both appear in the resolved config.

- [ ] **Step 3.4: Sanity check pytest + ruff**

```bash
pytest -q 2>&1 | tail -2 && ruff check . 2>&1 | tail -1
```

Expected: 420 still passing, ruff clean.

- [ ] **Step 3.5: Commit**

```bash
git add docker-compose.yml
git commit -m "compose: vehicle-attributes Phase 3 env vars + models volume + GPU pinning"
```

---

## Task 4: Class label JSON files

**Files:**
- Create: `services/vehicle-attributes/classes/color_classes.json`
- Create: `services/vehicle-attributes/classes/body_classes.json`
- Create: `services/vehicle-attributes/classes/make_classes.json`
- Create: `services/vehicle-attributes/classes/model_classes.json`
- Create: `services/vehicle-attributes/classes/make_to_models.json`

Static reference files that ship with the image. The classifier loads them at startup to map argmax indices to human labels. The training scripts overwrite these with the real label set before weights are uploaded.

- [ ] **Step 4.1: Create the classes/ directory**

```bash
mkdir -p services/vehicle-attributes/classes
```

- [ ] **Step 4.2: Write color_classes.json**

Write `services/vehicle-attributes/classes/color_classes.json`:

```json
[
  "yellow",
  "orange",
  "green",
  "gray",
  "red",
  "blue",
  "white",
  "golden",
  "brown",
  "black"
]
```

- [ ] **Step 4.3: Write body_classes.json**

Write `services/vehicle-attributes/classes/body_classes.json`:

```json
[
  "sedan",
  "suv",
  "coupe",
  "pickup",
  "van",
  "hatchback",
  "convertible",
  "wagon"
]
```

- [ ] **Step 4.4: Write make_classes.json**

```json
[
  "AM General", "Acura", "Aston Martin", "Audi", "BMW", "Bentley",
  "Bugatti", "Buick", "Cadillac", "Chevrolet", "Chrysler", "Daewoo",
  "Dodge", "Eagle", "FIAT", "Ferrari", "Fisker", "Ford", "GMC",
  "Geo", "HUMMER", "Honda", "Hyundai", "Infiniti", "Isuzu", "Jaguar",
  "Jeep", "Lamborghini", "Land Rover", "Lincoln", "MINI", "Maybach",
  "Mazda", "McLaren", "Mercedes-Benz", "Mitsubishi", "Nissan", "Plymouth",
  "Porsche", "Ram", "Rolls-Royce", "Scion", "Smart", "Spyker", "Suzuki",
  "Tesla", "Toyota", "Volkswagen", "Volvo", "smart"
]
```

- [ ] **Step 4.5: Write model_classes.json (stub)**

```bash
python3 -c "
import json
labels = [f'cars_class_{i}' for i in range(196)]
with open('services/vehicle-attributes/classes/model_classes.json', 'w') as f:
    json.dump(labels, f, indent=2)
print('wrote stub')
"
```

- [ ] **Step 4.6: Write make_to_models.json (stub)**

```bash
python3 << 'PY'
import json
data = {
    "Honda": ["Civic", "Accord", "CR-V", "Odyssey", "Pilot"],
    "Toyota": ["Camry", "Corolla", "Highlander", "4Runner", "Tundra"],
    "Ford": ["F-150", "Mustang", "Expedition", "Edge", "Escape"],
    "Chevrolet": ["Silverado", "Malibu", "Camaro", "Equinox", "Tahoe"],
}
with open('services/vehicle-attributes/classes/make_to_models.json', 'w') as f:
    json.dump(data, f, indent=2, sort_keys=True)
print('wrote stub')
PY
```

- [ ] **Step 4.7: Note the stub nature**

Create `services/vehicle-attributes/classes/STUB_NOTE.md`:

```markdown
# Stub class JSONs

These files are placeholders shipped with the initial Phase 3 commit so
the classifier module has SOMETHING to load against during unit tests
and the disabled-by-default path. They are REPLACED with the real label
lists by `scripts/vehicle_attributes/train_multihead.py` before weights
are uploaded to HF Hub.

The classifier's `_load_classes()` reads whichever version exists in
`/app/classes/` at container start. After a weights download from HF
Hub, the classes/ files in the image are overwritten by whatever the
HF Hub repo ships alongside the safetensors checkpoint.

Order in each list MATCHES the model head's argmax indices.
```

- [ ] **Step 4.8: Commit**

```bash
git add services/vehicle-attributes/classes/
git commit -m "vehicle-attributes: class label JSON stubs (training overwrites pre-upload)"
```

---

## Task 5: classifier.py — preprocessing pure function (TDD)

**Files:**
- Create: `services/vehicle-attributes/classifier.py` (stub at this task)
- Create: `services/vehicle_attributes/classifier.py` (pytest shim)
- Create: `tests/test_vehicle_attributes_classifier.py`

- [ ] **Step 5.1: Write the failing test**

Write `tests/test_vehicle_attributes_classifier.py`:

```python
"""Unit tests for vehicle-attributes classifier.py."""
import io
import json
import pytest
import numpy as np
from PIL import Image


def _make_jpeg(rgb_arr: np.ndarray) -> bytes:
    img = Image.fromarray(rgb_arr)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    return buf.getvalue()


def test_preprocess_decodes_jpegs_to_batched_tensor():
    from services.vehicle_attributes.classifier import _preprocess
    arr_a = np.zeros((100, 80, 3), dtype=np.uint8) + 128
    arr_b = np.zeros((50, 50, 3), dtype=np.uint8) + 200
    jpegs = [_make_jpeg(arr_a), _make_jpeg(arr_b)]
    t = _preprocess(jpegs)
    assert tuple(t.shape) == (2, 3, 224, 224)
    assert -3.0 < float(t.min()) < 0.0
    assert 0.0 < float(t.max()) < 3.0


def test_preprocess_empty_list_returns_empty_tensor():
    from services.vehicle_attributes.classifier import _preprocess
    t = _preprocess([])
    assert tuple(t.shape) == (0, 3, 224, 224)


def test_preprocess_rejects_invalid_jpeg_gracefully():
    from services.vehicle_attributes.classifier import _preprocess
    valid = _make_jpeg(np.zeros((100, 80, 3), dtype=np.uint8))
    invalid = b"\x00\x01\x02 not a jpeg"
    t = _preprocess([valid, invalid, valid])
    assert tuple(t.shape) == (2, 3, 224, 224)
```

- [ ] **Step 5.2: Create the pytest shim**

Write `services/vehicle_attributes/classifier.py`:

```python
"""Re-export shim — actual source lives in ../vehicle-attributes/classifier.py."""
from classifier import *  # noqa: F401,F403  (pulls from sys.path inserted in __init__)
```

- [ ] **Step 5.3: Run tests to confirm fail**

```bash
pytest tests/test_vehicle_attributes_classifier.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'classifier'`.

- [ ] **Step 5.4: Implement preprocessing in classifier.py**

Write `services/vehicle-attributes/classifier.py`:

```python
"""ConvNeXt-Tiny multi-head classifier for vehicle attributes (Phase 3 v0).

Loaded lazily on first inference call so the service can boot quickly +
fail fast if HF Hub is unreachable. Single-process singleton.

Public entry: run_classifier_and_vote(buf, event_kind) -> dict
"""
import io
import json
import logging
import os
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger("vehicle-attributes.classifier")


# ---------------------------------------------------------------------------
# Preprocessing (pure functions, no model state)
# ---------------------------------------------------------------------------

_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _preprocess(jpeg_crops: list[bytes]):
    """Decode JPEGs → resize-with-aspect → center-crop 224×224 → normalize.

    Returns a (B, 3, 224, 224) torch.Tensor with ImageNet normalization
    applied. Corrupt JPEGs are silently dropped.
    """
    import torch
    if not jpeg_crops:
        return torch.empty(0, 3, 224, 224)

    tensors = []
    for jpeg in jpeg_crops:
        try:
            arr = np.frombuffer(jpeg, dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            h, w = rgb.shape[:2]
            short = min(h, w)
            scale = 224.0 / short
            new_h = max(224, int(round(h * scale)))
            new_w = max(224, int(round(w * scale)))
            rgb = cv2.resize(rgb, (new_w, new_h),
                             interpolation=cv2.INTER_AREA)
            y0 = (new_h - 224) // 2
            x0 = (new_w - 224) // 2
            cropped = rgb[y0:y0 + 224, x0:x0 + 224]
            arr_f = cropped.astype(np.float32) / 255.0
            arr_f = (arr_f - _IMAGENET_MEAN) / _IMAGENET_STD
            arr_f = np.transpose(arr_f, (2, 0, 1))
            tensors.append(torch.from_numpy(arr_f.copy()))
        except Exception as e:
            logger.debug(f"skipping crop in preprocess: {e}")
            continue

    if not tensors:
        return torch.empty(0, 3, 224, 224)
    return torch.stack(tensors)
```

- [ ] **Step 5.5: Run preprocessing tests**

```bash
pytest tests/test_vehicle_attributes_classifier.py -v -k preprocess 2>&1 | tail -10
```

Expected: 3 PASS.

If `torch` isn't installed in `.venv-test`, install the CPU wheel:

```bash
source .venv-test/bin/activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu 2>&1 | tail -3
```

- [ ] **Step 5.6: Commit**

```bash
git add services/vehicle-attributes/classifier.py services/vehicle_attributes/classifier.py tests/test_vehicle_attributes_classifier.py
git commit -m "vehicle-attributes(classifier): preprocessing pure functions (TDD)"
```

---

## Task 6: classifier.py — voting math (TDD)

**Files:**
- Modify: `services/vehicle-attributes/classifier.py`
- Modify: `tests/test_vehicle_attributes_classifier.py`

- [ ] **Step 6.1: Add failing tests**

Append to `tests/test_vehicle_attributes_classifier.py`:

```python
def test_vote_single_strong_prediction():
    import torch
    from services.vehicle_attributes.classifier import _vote
    classes = ['A', 'B', 'C']
    probs = torch.tensor([[0.05, 0.90, 0.05]])
    yolo_confs = [0.8]
    winner, conf = _vote(probs, yolo_confs, classes, threshold=0.55)
    assert winner == 'B'
    assert conf > 0.55


def test_vote_below_threshold_returns_none():
    import torch
    from services.vehicle_attributes.classifier import _vote
    classes = ['A', 'B', 'C']
    probs = torch.tensor([[0.4, 0.35, 0.25]])
    yolo_confs = [0.7]
    winner, conf = _vote(probs, yolo_confs, classes, threshold=0.55)
    assert winner is None
    assert conf < 0.55


def test_vote_weighted_by_yolo_confidence():
    import torch
    from services.vehicle_attributes.classifier import _vote
    classes = ['A', 'B']
    probs = torch.tensor([
        [0.95, 0.05],
        [0.55, 0.45],
    ])
    yolo_confs = [0.1, 0.95]
    winner, conf = _vote(probs, yolo_confs, classes, threshold=0.55)
    assert winner == 'A'


def test_vote_empty_input_returns_none():
    import torch
    from services.vehicle_attributes.classifier import _vote
    classes = ['A', 'B']
    probs = torch.empty(0, 2)
    yolo_confs = []
    winner, conf = _vote(probs, yolo_confs, classes, threshold=0.55)
    assert winner is None
    assert conf == 0.0
```

- [ ] **Step 6.2: Run tests to confirm fail**

```bash
pytest tests/test_vehicle_attributes_classifier.py -v -k vote 2>&1 | tail -10
```

Expected: 4 FAILs.

- [ ] **Step 6.3: Implement `_vote`**

Append to `services/vehicle-attributes/classifier.py`:

```python
def _vote(per_crop_probs, yolo_confs: list[float],
          classes: list[str], threshold: float):
    """Weighted majority vote across crops.
    Returns (winner_label_or_None, winner_confidence).
    """
    import torch
    if per_crop_probs.numel() == 0 or not yolo_confs:
        return (None, 0.0)
    yc = torch.tensor(yolo_confs, dtype=per_crop_probs.dtype,
                      device=per_crop_probs.device)
    weighted = (per_crop_probs * yc.unsqueeze(1)).sum(dim=0)
    total = weighted.sum()
    if total <= 0:
        return (None, 0.0)
    weighted = weighted / total
    winner_idx = int(weighted.argmax().item())
    winner_conf = float(weighted[winner_idx].item())
    if winner_conf < threshold:
        return (None, winner_conf)
    return (classes[winner_idx], winner_conf)
```

- [ ] **Step 6.4: Run tests to confirm pass**

```bash
pytest tests/test_vehicle_attributes_classifier.py -v -k vote 2>&1 | tail -10
```

Expected: 4 PASS.

- [ ] **Step 6.5: Commit**

```bash
git add services/vehicle-attributes/classifier.py tests/test_vehicle_attributes_classifier.py
git commit -m "vehicle-attributes(classifier): weighted-vote helper (TDD)"
```

---

## Task 7: classifier.py — make-model consistency check (TDD)

**Files:**
- Modify: `services/vehicle-attributes/classifier.py`
- Modify: `tests/test_vehicle_attributes_classifier.py`

- [ ] **Step 7.1: Add failing tests**

Append:

```python
def test_consistency_keeps_matching_pair():
    from services.vehicle_attributes.classifier import _enforce_make_model_consistency
    make_to_models = {
        'Honda': ['Civic', 'Accord'],
        'Toyota': ['Camry', 'Corolla'],
    }
    new_make, new_model = _enforce_make_model_consistency(
        ('Honda', 0.8), ('Civic', 0.7), make_to_models,
    )
    assert new_make == ('Honda', 0.8)
    assert new_model == ('Civic', 0.7)


def test_consistency_drops_model_when_less_confident():
    from services.vehicle_attributes.classifier import _enforce_make_model_consistency
    make_to_models = {'Honda': ['Civic'], 'Toyota': ['Camry']}
    new_make, new_model = _enforce_make_model_consistency(
        ('Toyota', 0.8), ('Civic', 0.6), make_to_models,
    )
    assert new_make == ('Toyota', 0.8)
    assert new_model == (None, 0.6)


def test_consistency_drops_make_when_less_confident():
    from services.vehicle_attributes.classifier import _enforce_make_model_consistency
    make_to_models = {'Honda': ['Civic']}
    new_make, new_model = _enforce_make_model_consistency(
        ('Toyota', 0.4), ('Civic', 0.7), make_to_models,
    )
    assert new_make == (None, 0.4)
    assert new_model == ('Civic', 0.7)


def test_consistency_skips_when_either_is_none():
    from services.vehicle_attributes.classifier import _enforce_make_model_consistency
    make_to_models = {'Honda': ['Civic']}
    new_make, new_model = _enforce_make_model_consistency(
        ('Toyota', 0.8), (None, 0.4), make_to_models,
    )
    assert new_make == ('Toyota', 0.8)
    assert new_model == (None, 0.4)
    new_make, new_model = _enforce_make_model_consistency(
        (None, 0.4), ('Civic', 0.8), make_to_models,
    )
    assert new_make == (None, 0.4)
    assert new_model == ('Civic', 0.8)
```

- [ ] **Step 7.2: Run tests to confirm fail**

```bash
pytest tests/test_vehicle_attributes_classifier.py -v -k consistency 2>&1 | tail -10
```

Expected: 4 FAILs.

- [ ] **Step 7.3: Implement**

Append:

```python
def _enforce_make_model_consistency(
    make_out: tuple,
    model_out: tuple,
    make_to_models: dict,
) -> tuple:
    """Drop the less-confident of (make, model) when the predicted model
    isn't in the predicted make's roster. No-op if either is None.
    """
    make_label, make_conf = make_out
    model_label, model_conf = model_out
    if make_label is None or model_label is None:
        return (make_out, model_out)
    if model_label in make_to_models.get(make_label, ()):
        return (make_out, model_out)
    if make_conf >= model_conf:
        return (make_out, (None, model_conf))
    return ((None, make_conf), model_out)
```

- [ ] **Step 7.4: Run tests to confirm pass**

```bash
pytest tests/test_vehicle_attributes_classifier.py -v -k consistency 2>&1 | tail -10
```

Expected: 4 PASS.

- [ ] **Step 7.5: Commit**

```bash
git add services/vehicle-attributes/classifier.py tests/test_vehicle_attributes_classifier.py
git commit -m "vehicle-attributes(classifier): make-model consistency check (TDD)"
```

---

## Task 8: classifier.py — `run_classifier_and_vote` (TDD with mocked model)

**Files:**
- Modify: `services/vehicle-attributes/classifier.py`
- Modify: `tests/test_vehicle_attributes_classifier.py`

- [ ] **Step 8.1: Add failing tests**

Append:

```python
def _mock_model(num_crops: int):
    """Returns a callable that mimics the multi-head model's forward pass.
    Strong predictions on index 4 (color), 0 (body), 21 (make), 50 (model).
    """
    import torch
    def fake_forward(_x):
        out = {
            'color': torch.zeros(num_crops, 10),
            'body':  torch.zeros(num_crops, 8),
            'make':  torch.zeros(num_crops, 50),
            'model': torch.zeros(num_crops, 196),
        }
        out['color'][:, 4] = 5.0
        out['body'][:, 0] = 5.0
        out['make'][:, 21] = 5.0
        out['model'][:, 50] = 5.0
        return out
    return fake_forward


def test_run_classifier_and_vote_drive_by_predicts_all_four(monkeypatch):
    from services.vehicle_attributes import classifier as clf
    from services.vehicle_attributes.buffer import TrackBuffer
    import torch

    monkeypatch.setattr(clf, "_load_model", lambda: _mock_model(num_crops=3))
    monkeypatch.setattr(clf, "_load_classes", lambda: {
        'color': ['yellow','orange','green','gray','red','blue','white','golden','brown','black'],
        'body':  ['sedan','suv','coupe','pickup','van','hatchback','convertible','wagon'],
        'make':  ['Acura'] * 21 + ['Honda'] + ['Toyota'] * 28,
        'model': [f'm{i}' for i in range(196)],
        'make_to_models': {'Honda': ['m50']},
    })
    monkeypatch.setattr(clf, "_preprocess",
                        lambda crops: torch.zeros(len(crops), 3, 224, 224))

    buf = TrackBuffer(track_id="v_drive", camera_id="cam1", first_seen=0.0)
    for _ in range(3):
        buf.append(crop=_make_jpeg(np.zeros((100, 80, 3), dtype=np.uint8)),
                   yolo_conf=0.8, bbox=[10, 20, 50, 60])

    out = clf.run_classifier_and_vote(buf, event_kind="drive_by")
    assert out['color'] == 'red'
    assert out['color_confidence'] is not None
    assert out['body_type'] == 'sedan'
    assert out['make'] == 'Honda'
    assert out['model'] == 'm50'
    assert out['voting_samples'] == 3
    assert out['classifier_version'].startswith('v0-')


def test_run_classifier_and_vote_idle_skips_model(monkeypatch):
    from services.vehicle_attributes import classifier as clf
    from services.vehicle_attributes.buffer import TrackBuffer
    import torch

    monkeypatch.setattr(clf, "_load_model", lambda: _mock_model(num_crops=2))
    monkeypatch.setattr(clf, "_load_classes", lambda: {
        'color': ['yellow','orange','green','gray','red','blue','white','golden','brown','black'],
        'body':  ['sedan','suv','coupe','pickup','van','hatchback','convertible','wagon'],
        'make':  ['Acura'] * 21 + ['Honda'] + ['Toyota'] * 28,
        'model': [f'm{i}' for i in range(196)],
        'make_to_models': {'Honda': ['m50']},
    })
    monkeypatch.setattr(clf, "_preprocess",
                        lambda crops: torch.zeros(len(crops), 3, 224, 224))

    buf = TrackBuffer(track_id="v_idle", camera_id="cam1", first_seen=0.0)
    for _ in range(2):
        buf.append(crop=_make_jpeg(np.zeros((100, 80, 3), dtype=np.uint8)),
                   yolo_conf=0.8, bbox=[10, 20, 50, 60])

    out = clf.run_classifier_and_vote(buf, event_kind="idle")
    assert out['color'] == 'red'
    assert out['body_type'] == 'sedan'
    assert out['make'] == 'Honda'
    assert out['model'] is None
    assert out['model_confidence'] is None


def test_run_classifier_and_vote_empty_buffer_returns_all_null(monkeypatch):
    from services.vehicle_attributes import classifier as clf
    from services.vehicle_attributes.buffer import TrackBuffer

    monkeypatch.setattr(clf, "_load_model", lambda: None)
    monkeypatch.setattr(clf, "_load_classes", lambda: {
        'color': [], 'body': [], 'make': [], 'model': [], 'make_to_models': {},
    })

    buf = TrackBuffer(track_id="v_empty", camera_id="cam1", first_seen=0.0)
    out = clf.run_classifier_and_vote(buf, event_kind="drive_by")
    assert out['color'] is None
    assert out['body_type'] is None
    assert out['make'] is None
    assert out['model'] is None
    assert out['voting_samples'] == 0
```

- [ ] **Step 8.2: Run tests to confirm fail**

```bash
pytest tests/test_vehicle_attributes_classifier.py -v -k run_classifier 2>&1 | tail -10
```

Expected: 3 FAILs.

- [ ] **Step 8.3: Implement the loaders + `run_classifier_and_vote`**

Append to `services/vehicle-attributes/classifier.py`:

```python
# ---------------------------------------------------------------------------
# Model + classes loading (singletons, lazy)
# ---------------------------------------------------------------------------

_MODEL = None
_CLASSES = None

MODELS_DIR = os.environ.get("VEHICLE_ATTR_MODELS_DIR", "/models")
HF_REPO = os.environ.get("VEHICLE_ATTR_HF_REPO",
                          "gammahazard/vision-labs-vehicle-attributes")
MODEL_NAME = os.environ.get("VEHICLE_ATTR_MODEL", "convnext_tiny_v0")

COLOR_CONF = float(os.environ.get("COLOR_CONF_THRESHOLD", "0.55"))
BODY_CONF = float(os.environ.get("BODY_CONF_THRESHOLD", "0.55"))
MAKE_CONF = float(os.environ.get("MAKE_CONF_THRESHOLD", "0.55"))
MODEL_CONF = float(os.environ.get("MODEL_CONF_THRESHOLD", "0.65"))

MODEL_VERSION = f"v0-{MODEL_NAME}-2026-05-21"


def _classes_dir() -> Path:
    """The classes/ directory shipped in the container image."""
    return Path(__file__).resolve().parent / "classes"


def _load_classes() -> dict:
    """Read class label JSONs + make-to-models map. Cached singleton."""
    global _CLASSES
    if _CLASSES is not None:
        return _CLASSES
    d = _classes_dir()
    _CLASSES = {
        'color': json.loads((d / 'color_classes.json').read_text()),
        'body':  json.loads((d / 'body_classes.json').read_text()),
        'make':  json.loads((d / 'make_classes.json').read_text()),
        'model': json.loads((d / 'model_classes.json').read_text()),
        'make_to_models': json.loads((d / 'make_to_models.json').read_text()),
    }
    return _CLASSES


def _build_model_arch():
    """Construct the multi-head ConvNeXt-Tiny architecture (no weights)."""
    import torch
    import torch.nn as nn
    import timm
    classes = _load_classes()
    backbone = timm.create_model('convnext_tiny', pretrained=False,
                                  num_classes=0)
    feat_dim = backbone.num_features

    class MultiHead(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = backbone
            self.color_head = nn.Linear(feat_dim, len(classes['color']))
            self.body_head = nn.Linear(feat_dim, len(classes['body']))
            self.make_head = nn.Linear(feat_dim, len(classes['make']))
            self.model_head = nn.Linear(feat_dim, len(classes['model']))

        def forward(self, x):
            feats = self.backbone(x)
            return {
                'color': self.color_head(feats),
                'body':  self.body_head(feats),
                'make':  self.make_head(feats),
                'model': self.model_head(feats),
            }
    return MultiHead()


def _load_model():
    """Lazy-load the trained multi-head model.
    Downloads weights from HF Hub on first call.
    Raises (rather than returns None) if weights can't be obtained.
    """
    global _MODEL
    if _MODEL is not None:
        return _MODEL

    import torch
    from huggingface_hub import hf_hub_download

    target_dir = Path(MODELS_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    weights_path = target_dir / f"{MODEL_NAME}.safetensors"

    if not weights_path.exists():
        logger.info(f"downloading {MODEL_NAME} weights from {HF_REPO}")
        downloaded = hf_hub_download(
            repo_id=HF_REPO,
            filename=f"{MODEL_NAME}.safetensors",
            local_dir=str(target_dir),
        )
        if Path(downloaded) != weights_path:
            os.replace(downloaded, weights_path)

    model = _build_model_arch()
    from safetensors.torch import load_file
    state = load_file(str(weights_path))
    model.load_state_dict(state)
    model.train(False)  # set to evaluation mode (equivalent to .eval())
    if torch.cuda.is_available():
        model = model.cuda()
    _MODEL = model
    logger.info(f"loaded {MODEL_NAME} into {'cuda' if torch.cuda.is_available() else 'cpu'}")
    return _MODEL


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def run_classifier_and_vote(buf, event_kind: str) -> dict:
    """Run the multi-head classifier across all crops in the buffer,
    apply voting + thresholds + make-model consistency, return attributes
    dict for storage.py to merge into metadata.json.
    """
    import torch

    classes = _load_classes()

    if not buf.crops:
        return {
            'color': None, 'color_confidence': None,
            'body_type': None, 'body_type_confidence': None,
            'make': None, 'make_confidence': None,
            'model': None, 'model_confidence': None,
            'voting_samples': 0,
            'classifier_version': MODEL_VERSION,
        }

    model = _load_model()
    crops_t = _preprocess(buf.crops)
    if torch.cuda.is_available() and crops_t.numel() > 0:
        crops_t = crops_t.cuda()

    with torch.inference_mode():
        logits = model(crops_t)
    probs = {task: torch.softmax(t, dim=1) for task, t in logits.items()}

    color_out = _vote(probs['color'], buf.confidences, classes['color'],
                      COLOR_CONF)
    body_out = _vote(probs['body'], buf.confidences, classes['body'],
                     BODY_CONF)
    make_out = _vote(probs['make'], buf.confidences, classes['make'],
                     MAKE_CONF)
    if event_kind == 'idle':
        model_out = (None, 0.0)
    else:
        model_out = _vote(probs['model'], buf.confidences, classes['model'],
                          MODEL_CONF)

    make_out, model_out = _enforce_make_model_consistency(
        make_out, model_out, classes['make_to_models'],
    )

    return {
        'color': color_out[0],
        'color_confidence': color_out[1],
        'body_type': body_out[0],
        'body_type_confidence': body_out[1],
        'make': make_out[0],
        'make_confidence': make_out[1],
        'model': model_out[0],
        'model_confidence': model_out[1] if model_out[0] is not None else None,
        'voting_samples': len(buf.crops),
        'classifier_version': MODEL_VERSION,
    }
```

- [ ] **Step 8.4: Run all classifier tests to confirm pass**

```bash
pytest tests/test_vehicle_attributes_classifier.py -v 2>&1 | tail -15
```

Expected: 14 PASS (3 preprocess + 4 vote + 4 consistency + 3 run_classifier).

- [ ] **Step 8.5: Run full suite + ruff**

```bash
pytest -q 2>&1 | tail -3 && ruff check . 2>&1 | tail -1
```

Expected: 434 passed, ruff clean.

- [ ] **Step 8.6: Commit**

```bash
git add services/vehicle-attributes/classifier.py tests/test_vehicle_attributes_classifier.py
git commit -m "vehicle-attributes(classifier): run_classifier_and_vote entry + lazy model load (TDD)"
```

---

## Task 9: Storage.py — accept `attributes` kwarg

**Files:**
- Modify: `services/vehicle-attributes/storage.py:flush_buffer_to_disk`
- Modify: `tests/test_vehicle_attributes_storage.py`

- [ ] **Step 9.1: Add failing tests**

Append to `tests/test_vehicle_attributes_storage.py`:

```python
def test_flush_writes_attributes_when_provided(tmp_path):
    b = _seeded_buffer(2)
    attrs = {
        'color': 'red',
        'color_confidence': 0.82,
        'body_type': 'sedan',
        'body_type_confidence': 0.78,
        'make': 'Honda',
        'make_confidence': 0.71,
        'model': 'Civic',
        'model_confidence': 0.68,
        'voting_samples': 2,
        'classifier_version': 'v0-convnext_tiny_v0-2026-05-21',
    }
    flush_buffer_to_disk(b, last_seen=1779394907.2, event_kind='drive_by',
                         vehicle_class='car', snapshot_root=str(tmp_path),
                         attributes=attrs)
    track_dir = tmp_path / 'cam1' / '2026-05-21' / 'vehicle_0042'
    meta = json.loads((track_dir / 'metadata.json').read_text())
    assert meta['attributes']['color'] == 'red'
    assert meta['attributes']['make'] == 'Honda'
    assert meta['attributes']['model'] == 'Civic'
    assert meta['attributes']['classifier_version'].startswith('v0-')


def test_flush_omitted_attributes_keeps_phase1_null_block(tmp_path):
    b = _seeded_buffer(2)
    flush_buffer_to_disk(b, last_seen=1779394907.2, event_kind='drive_by',
                         vehicle_class='car', snapshot_root=str(tmp_path))
    track_dir = tmp_path / 'cam1' / '2026-05-21' / 'vehicle_0042'
    meta = json.loads((track_dir / 'metadata.json').read_text())
    assert meta['attributes']['color'] is None
    assert meta['attributes']['body_type'] is None
    assert meta['attributes']['make'] is None
    assert meta['attributes']['model'] is None
```

- [ ] **Step 9.2: Run tests to confirm fail**

```bash
pytest tests/test_vehicle_attributes_storage.py -v -k attribute 2>&1 | tail -10
```

Expected: 1 FAIL on the attributes-kwarg test.

- [ ] **Step 9.3: Update `flush_buffer_to_disk`**

In `services/vehicle-attributes/storage.py`, change the function signature and metadata construction:

```python
from typing import Optional


def flush_buffer_to_disk(
    buf: TrackBuffer,
    last_seen: float,
    event_kind: str,
    vehicle_class: str,
    snapshot_root: str,
    attributes: Optional[dict] = None,
) -> None:
    """Write the buffer to /data/snapshots/vehicles/{cam}/{date}/{track_id}/.

    `attributes`: optional dict from the Phase 3 classifier. When provided,
    becomes the `attributes` block in metadata.json. When None (Phase 1
    behavior + Phase 3 with ENABLE_CLASSIFIER=0), the block is all-null.
    """
    if not buf.crops:
        logger.debug(f"Flush {buf.track_id}: empty buffer, skipping")
        return

    date_str = _date_str_from_first_seen(buf.first_seen)
    track_dir = os.path.join(snapshot_root, buf.camera_id, date_str,
                             buf.track_id)
    os.makedirs(track_dir, exist_ok=True)

    hero_idx = buf.hero_index()

    hero_path = os.path.join(track_dir, "hero.jpg")
    with open(hero_path, "wb") as fh:
        fh.write(buf.crops[hero_idx])

    angle_n = 1
    for i, crop in enumerate(buf.crops):
        if i == hero_idx:
            continue
        angle_path = os.path.join(track_dir, f"angle_{angle_n:02d}.jpg")
        with open(angle_path, "wb") as fh:
            fh.write(crop)
        angle_n += 1

    if attributes is None:
        attributes = {
            "color": None, "color_confidence": None,
            "body_type": None, "body_type_confidence": None,
            "make": None, "make_confidence": None,
            "model": None, "model_confidence": None,
        }

    meta = {
        "track_id": buf.track_id,
        "camera_id": buf.camera_id,
        "first_seen": buf.first_seen,
        "last_seen": last_seen,
        "duration_seconds": round(last_seen - buf.first_seen, 2),
        "event_kind": event_kind,
        "vehicle_class": vehicle_class,
        "hero_frame_index": hero_idx,
        "voting_samples": len(buf.crops),
        "attributes": attributes,
        "snapshot_bbox": buf.bboxes[hero_idx],
    }
    meta_path = os.path.join(track_dir, "metadata.json")
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)

    logger.info(
        f"Flushed {buf.track_id} → {track_dir} "
        f"({len(buf.crops)} crops, hero=angle_{hero_idx}, kind={event_kind})"
    )
```

- [ ] **Step 9.4: Run storage tests + full suite**

```bash
pytest tests/test_vehicle_attributes_storage.py -v 2>&1 | tail -10
pytest -q 2>&1 | tail -2 && ruff check . 2>&1 | tail -1
```

Expected: storage tests all pass (4 original + 2 new = 6), full suite 436, ruff clean.

- [ ] **Step 9.5: Commit**

```bash
git add services/vehicle-attributes/storage.py tests/test_vehicle_attributes_storage.py
git commit -m "vehicle-attributes(storage): accept attributes kwarg for Phase 3 classifier output"
```

---

## Task 10: Service.py — wire classifier into the flush path

**Files:**
- Modify: `services/vehicle-attributes/service.py:_flush`
- Modify: `tests/test_vehicle_attributes_service.py`

- [ ] **Step 10.1: Add failing tests**

Append to `tests/test_vehicle_attributes_service.py`:

```python
def test_flush_does_not_call_classifier_when_disabled(monkeypatch, tmp_path):
    """ENABLE_CLASSIFIER=0 (default Phase 1 behavior): _flush writes
    null-attributes metadata, classifier module is NOT loaded."""
    from services.vehicle_attributes import service as svc
    from services.vehicle_attributes.buffer import TrackBuffer

    monkeypatch.setenv("ENABLE_CLASSIFIER", "0")
    import importlib
    importlib.reload(svc)

    buf = TrackBuffer(track_id="vG", camera_id="cam1", first_seen=1779394901.5)
    buf.append(crop=b"\xff\xd8jpg", yolo_conf=0.85, bbox=[10, 20, 50, 60])
    buffers = {"vG": buf}

    classifier_called = {'flag': False}
    def _trip(*_a, **_k):
        classifier_called['flag'] = True
        return {}
    monkeypatch.setattr("services.vehicle_attributes.classifier.run_classifier_and_vote", _trip)

    event = {
        "event_type": "vehicle_gone",
        "vehicle_id": "vG",
        "camera_id": "cam1",
        "timestamp": "1779394907.2",
        "vehicle_class": "car",
        "was_idle": "False",
    }
    svc.handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                     snapshot_root=str(tmp_path))

    assert classifier_called['flag'] is False
    import json as _json
    meta = _json.loads(
        (tmp_path / 'cam1' / '2026-05-21' / 'vG' / 'metadata.json').read_text()
    )
    assert meta['attributes']['color'] is None


def test_flush_calls_classifier_when_enabled(monkeypatch, tmp_path):
    from services.vehicle_attributes import service as svc
    from services.vehicle_attributes.buffer import TrackBuffer

    monkeypatch.setenv("ENABLE_CLASSIFIER", "1")
    import importlib
    importlib.reload(svc)

    buf = TrackBuffer(track_id="vH", camera_id="cam1", first_seen=1779394901.5)
    buf.append(crop=b"\xff\xd8jpg", yolo_conf=0.85, bbox=[10, 20, 50, 60])
    buffers = {"vH": buf}

    expected_attrs = {
        'color': 'red', 'color_confidence': 0.8,
        'body_type': 'sedan', 'body_type_confidence': 0.75,
        'make': 'Honda', 'make_confidence': 0.7,
        'model': None, 'model_confidence': None,
        'voting_samples': 1,
        'classifier_version': 'v0-test-2026-05-21',
    }
    monkeypatch.setattr(
        "services.vehicle_attributes.classifier.run_classifier_and_vote",
        lambda _buf, _kind: expected_attrs,
    )

    event = {
        "event_type": "vehicle_gone",
        "vehicle_id": "vH",
        "camera_id": "cam1",
        "timestamp": "1779394907.2",
        "vehicle_class": "car",
        "was_idle": "False",
    }
    svc.handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                     snapshot_root=str(tmp_path))

    import json as _json
    meta = _json.loads(
        (tmp_path / 'cam1' / '2026-05-21' / 'vH' / 'metadata.json').read_text()
    )
    assert meta['attributes']['color'] == 'red'
    assert meta['attributes']['make'] == 'Honda'
```

- [ ] **Step 10.2: Run tests to confirm fail**

```bash
pytest tests/test_vehicle_attributes_service.py -v -k classifier 2>&1 | tail -10
```

Expected: 1 PASS (the disabled case happens to pass) + 1 FAIL.

- [ ] **Step 10.3: Update service.py's `_flush`**

In `services/vehicle-attributes/service.py`, add a module-level env read near the other env reads (look for the `MAX_BUFFER_CROPS = ...` line):

```python
ENABLE_CLASSIFIER = os.environ.get("ENABLE_CLASSIFIER", "0") == "1"
```

Then replace `_flush` with:

```python
def _flush(event: dict, buffers: dict,
           snapshot_root: str) -> None:
    track_id = event.get("vehicle_id", "")
    buf = buffers.pop(track_id, None)
    if buf is None:
        return
    last_seen = float(event.get("timestamp", "0") or 0)
    if event.get("event_type") == "vehicle_idle":
        event_kind = "idle"
    elif event.get("was_idle") == "True":
        event_kind = "idle"
    else:
        event_kind = "drive_by"

    attributes = None
    if ENABLE_CLASSIFIER:
        try:
            # Lazy import — Phase 1 service starts fast when classifier
            # is disabled (no torch import until first inference call).
            from classifier import run_classifier_and_vote
            attributes = run_classifier_and_vote(buf, event_kind)
        except Exception as e:
            logger.exception(
                f"classifier failed for {track_id}, falling back to null attrs: {e}"
            )
            attributes = None

    flush_buffer_to_disk(
        buf,
        last_seen=last_seen,
        event_kind=event_kind,
        vehicle_class=event.get("vehicle_class", ""),
        snapshot_root=snapshot_root,
        attributes=attributes,
    )
```

- [ ] **Step 10.4: Run tests + full suite**

```bash
pytest tests/test_vehicle_attributes_service.py -v 2>&1 | tail -15
pytest -q 2>&1 | tail -2 && ruff check . 2>&1 | tail -1
```

Expected: all service tests pass, 438 total, ruff clean.

- [ ] **Step 10.5: Commit**

```bash
git add services/vehicle-attributes/service.py tests/test_vehicle_attributes_service.py
git commit -m "vehicle-attributes(service): wire classifier into flush, gated by ENABLE_CLASSIFIER"
```

---

## Task 11: Training scripts

**Files:**
- Create: `scripts/vehicle_attributes/train_color_head.py`
- Create: `scripts/vehicle_attributes/train_multihead.py`
- Create: `scripts/vehicle_attributes/upload_weights.py`
- Create: `scripts/vehicle_attributes/README.md`

Scripts the user runs once on a GPU box. NOT in the runtime image. Dataset-loading parts intentionally have `NotImplementedError` placeholders because VeRi-776 and Stanford Cars layouts vary by download method — the user adapts to their local layout.

- [ ] **Step 11.1: Create the directory**

```bash
mkdir -p scripts/vehicle_attributes
```

- [ ] **Step 11.2: Write README.md**

Write `scripts/vehicle_attributes/README.md`:

```markdown
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

First boot downloads weights from HF Hub into the
`vehicle-attribute-models` volume.
```

- [ ] **Step 11.3: Write train_color_head.py (with dataset-loader stub)**

Write `scripts/vehicle_attributes/train_color_head.py`:

```python
"""Train a ConvNeXt-Tiny color classifier head on VeRi-776.

Usage:
    python train_color_head.py --output ./training-output --epochs 10
"""
import argparse
import json
from pathlib import Path

import torch
import torch.nn as nn
import timm

COLOR_LABELS = ["yellow", "orange", "green", "gray", "red",
                "blue", "white", "golden", "brown", "black"]


def _load_veri776(data_dir: Path):
    """Load VeRi-776 train split. Download from:
        https://github.com/JDAI-CV/VeRidataset

    Adapt this to your local VeRi-776 layout. The function should return
    a torch DataLoader yielding (image_tensor, color_label_int) batches.
    """
    raise NotImplementedError(
        "Adapt this to your local VeRi-776 layout. See "
        "https://github.com/JDAI-CV/VeRidataset for the dataset + color "
        "label format."
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("./datasets/veri776"))
    ap.add_argument("--output", type=Path, default=Path("./training-output"))
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    args = ap.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    backbone = timm.create_model('convnext_tiny', pretrained=True, num_classes=0)
    for p in backbone.parameters():
        p.requires_grad = False
    backbone.train(False).to(device)

    color_head = nn.Linear(backbone.num_features, len(COLOR_LABELS)).to(device)

    train_loader = _load_veri776(args.data_dir)

    opt = torch.optim.AdamW(color_head.parameters(), lr=args.lr)
    criterion = nn.CrossEntropyLoss()

    for epoch in range(args.epochs):
        total_loss = 0.0
        n = 0
        for batch in train_loader:
            imgs, labels = batch[0].to(device), batch[1].to(device)
            with torch.no_grad():
                feats = backbone(imgs)
            logits = color_head(feats)
            loss = criterion(logits, labels)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total_loss += float(loss.item()) * imgs.size(0)
            n += imgs.size(0)
        print(f"epoch {epoch+1}/{args.epochs} — color loss {total_loss/n:.4f}")

    out_path = args.output / "color_head.pth"
    torch.save(color_head.state_dict(), out_path)
    print(f"saved color head to {out_path}")

    (args.output / "color_classes.json").write_text(json.dumps(COLOR_LABELS, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 11.4: Write train_multihead.py (skeleton with dataset stub)**

Write `scripts/vehicle_attributes/train_multihead.py`:

```python
"""Fine-tune ConvNeXt-Tiny multi-head on Stanford Cars-196.

Loads color head from train_color_head.py output, trains body/make/model
heads jointly on Stanford Cars, exports combined safetensors.

Usage:
    python train_multihead.py \\
        --color-head-checkpoint ./training-output/color_head.pth \\
        --output ./training-output --epochs 15
"""
import argparse
from pathlib import Path

import torch
import torch.nn as nn
import timm


BODY_KEYWORDS = {
    'sedan': 'sedan',
    'suv': 'suv',
    'coupe': 'coupe',
    'pickup': 'pickup',
    'cab': 'pickup',
    'van': 'van',
    'hatchback': 'hatchback',
    'convertible': 'convertible',
    'wagon': 'wagon',
}
BODY_FALLBACK = 'sedan'


def extract_make(class_name: str) -> str:
    return class_name.split()[0]


def extract_body(class_name: str) -> str:
    low = class_name.lower()
    for kw, label in BODY_KEYWORDS.items():
        if kw in low:
            return label
    return BODY_FALLBACK


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--color-head-checkpoint", type=Path, required=True)
    ap.add_argument("--data-dir", type=Path,
                    default=Path("./datasets/stanford_cars"))
    ap.add_argument("--output", type=Path, default=Path("./training-output"))
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-4)
    args = ap.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    # See README. Adapt to your Stanford Cars layout.
    raise NotImplementedError(
        "Adapt this script to your local Stanford Cars-196 layout. The "
        "skeleton above is the multi-head architecture; you need to plug "
        "in your DataLoader, the joint loss function (sum of cross-entropy "
        "for body/make/model — color head stays frozen from step 1), and "
        "the safetensors export of the final combined model."
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 11.5: Write upload_weights.py**

Write `scripts/vehicle_attributes/upload_weights.py`:

```python
"""Upload the trained multi-head weights + class JSONs to HuggingFace Hub.

Usage:
    python upload_weights.py \\
        --checkpoint ./training-output/convnext_tiny_v0.safetensors \\
        --classes-dir services/vehicle-attributes/classes \\
        --repo gammahazard/vision-labs-vehicle-attributes
"""
import argparse
from pathlib import Path
from huggingface_hub import HfApi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", type=Path, required=True)
    ap.add_argument("--classes-dir", type=Path, required=True)
    ap.add_argument("--repo", type=str, required=True)
    args = ap.parse_args()

    api = HfApi()
    api.upload_file(
        path_or_fileobj=str(args.checkpoint),
        path_in_repo=args.checkpoint.name,
        repo_id=args.repo,
        repo_type="model",
    )
    for cls_file in args.classes_dir.glob("*.json"):
        api.upload_file(
            path_or_fileobj=str(cls_file),
            path_in_repo=f"classes/{cls_file.name}",
            repo_id=args.repo,
            repo_type="model",
        )
    print(f"uploaded {args.checkpoint.name} + classes to {args.repo}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 11.6: Commit**

```bash
git add scripts/vehicle_attributes/
git commit -m "vehicle-attributes: training scripts + upload pipeline + README

These scripts are run once (offline, on a GPU box) to produce the
weights uploaded to HF Hub. NOT part of the runtime image. Engineer
running them adapts the dataset-loading stubs to their local VeRi-776
+ Stanford Cars layouts."
```

---

## Task 12: Browse UI — attribute row + (beta) tag

**Files:**
- Modify: `services/dashboard/static/js/dashboard/browse.js`
- Modify: `services/dashboard/static/css/style.css`
- Modify: HTML files that reference browse.js or style.css — bump cache-bust

- [ ] **Step 12.1: Find existing card template**

```bash
grep -B 2 -A 15 'class="track-card"' services/dashboard/static/js/dashboard/browse.js
```

Expected: existing template that renders hero, class pill, time, voting_samples, angle thumbnails.

- [ ] **Step 12.2: Add attribute row to the card template**

In `services/dashboard/static/js/dashboard/browse.js`, locate `_renderDayTracks`. Insert this row inside the `<div class="track-card">` template — before the closing `</div>` of the card:

```javascript
            <div class="track-attrs">
                ${_formatAttrs(t.attributes)}
            </div>
```

Then in the same file, add the helper function (place it near the top with other helpers):

```javascript
function _formatAttrs(attrs) {
    if (!attrs) return '';
    const color = attrs.color || '';
    const body = attrs.body_type || '';
    const make = attrs.make || '';
    const model = attrs.model || '';
    if (!color && !body && !make && !model) return '';
    const left = [color, body].filter(Boolean).join(' ');
    const right = [make, model].filter(Boolean).join(' ');
    let combined = left;
    if (right) combined += (left ? ' · ' : '') + right;
    return `${combined} <span class="track-attrs-beta">(beta)</span>`;
}
```

- [ ] **Step 12.3: Add CSS**

Append to `services/dashboard/static/css/style.css`:

```css
/* Phase 3 vehicle-attributes — attribute row on per-track cards */
.track-attrs {
    color: #cbd5e1;
    font-size: 0.78rem;
    padding: 0 0.75rem 0.75rem;
}
.track-attrs-beta {
    color: #64748b;
    font-size: 0.7rem;
    font-style: italic;
    margin-left: 0.4rem;
}
```

- [ ] **Step 12.4: Bump cache-busts**

```bash
cd /home/mongo/projects/vision-labs
grep -E 'browse\.js\?v=|style\.css\?v=' services/dashboard/static/*.html | head -10
```

Determine the current `?v=` numbers for browse.js and style.css. Bump them by 1 each in every HTML file that references them:

```bash
# Adjust to the actual current numbers
sed -i 's|browse\.js?v=6|browse.js?v=7|g' \
    services/dashboard/static/index.html \
    services/dashboard/static/single.html
sed -i 's|style\.css?v=6|style.css?v=7|g' \
    services/dashboard/static/*.html
```

- [ ] **Step 12.5: Sanity check**

```bash
pytest -q 2>&1 | tail -2 && ruff check . 2>&1 | tail -1
```

Expected: 438 still passing, ruff clean.

- [ ] **Step 12.6: Commit**

```bash
git add services/dashboard/static/js/dashboard/browse.js services/dashboard/static/css/style.css services/dashboard/static/index.html services/dashboard/static/single.html
git commit -m "ui(browse): render attribute row with (beta) tag on track cards"
```

---

## Task 13: Live verification (disabled-by-default path)

**Files:** none

CLAUDE.md §15 mandates live verification after deploying any service-affecting change. We build the new image + recreate the cam1 container + confirm it stays up with `ENABLE_CLASSIFIER=0` (default — should match Phase 1 behavior exactly).

- [ ] **Step 13.1: Build the new image**

```bash
cd /home/mongo/projects/vision-labs
docker compose build vehicle-attributes-cam1 2>&1 | tail -5
```

Expected: `Image vision-labs-vehicle-attributes-cam1 Built`. Image grows substantially (~5 GB) due to torch + CUDA wheel. If build fails on a pip resolver conflict (Phase 1 hit this), check `services/vehicle-attributes/requirements.txt` and verify torch+timm+numpy versions match.

- [ ] **Step 13.2: Recreate the container**

```bash
docker compose up -d --force-recreate --no-deps vehicle-attributes-cam1 2>&1 | tail -3
```

- [ ] **Step 13.3: Confirm it stays up**

```bash
sleep 6
docker compose ps --format '{{.Name}}\t{{.Status}}' | grep vehicle-attributes-cam1
```

Expected: `Up X seconds`. If it bounces, check `docker compose logs vehicle-attributes-cam1`.

- [ ] **Step 13.4: Confirm classifier was NOT loaded**

```bash
docker compose logs --since=30s vehicle-attributes-cam1 2>&1 | grep -E 'subscribed|exiting|loaded|classifier|downloading' | head -5
```

Expected: see "subscribing to events:cam1" but NOT "loaded classifier" or "downloading weights" (since classifier is disabled by default).

- [ ] **Step 13.5: Synthesize a vehicle_gone event + verify null attributes**

```bash
REDIS_PW=$(grep '^REDIS_PASSWORD=' /home/mongo/projects/vision-labs/.env | cut -d= -f2)
TS=$(python3 -c 'import time; print(time.time())')
docker exec vision-labs-redis-1 redis-cli -a "$REDIS_PW" --no-auth-warning XADD events:cam1 '*' \
    camera_id cam1 event_type vehicle_detected timestamp "$TS" \
    bbox '[100,200,250,310]' vehicle_class car vehicle_confidence 0.85 \
    vehicle_id vehicle_P3TEST vehicle_first_seen "${TS%.*}" >/dev/null
sleep 1
TS2=$(python3 -c "import time; print(time.time())")
docker exec vision-labs-redis-1 redis-cli -a "$REDIS_PW" --no-auth-warning XADD events:cam1 '*' \
    camera_id cam1 event_type vehicle_sample timestamp "$TS2" \
    bbox '[110,210,260,320]' vehicle_class car vehicle_confidence 0.88 \
    vehicle_id vehicle_P3TEST vehicle_first_seen "${TS%.*}" >/dev/null
sleep 1
docker exec vision-labs-redis-1 redis-cli -a "$REDIS_PW" --no-auth-warning XADD events:cam1 '*' \
    camera_id cam1 event_type vehicle_gone timestamp "$TS2" \
    bbox '[110,210,260,320]' vehicle_class car vehicle_confidence 0.88 \
    vehicle_id vehicle_P3TEST vehicle_first_seen "${TS%.*}" was_idle False >/dev/null
sleep 2
docker exec vision-labs-vehicle-attributes-cam1-1 cat \
    /data/snapshots/vehicles/cam1/$(date +%Y-%m-%d)/vehicle_P3TEST/metadata.json
```

Expected: `metadata.json` has `"attributes": { ..., "color": null, "make": null, ... }`. Confirms disabled-by-default path matches Phase 1.

- [ ] **Step 13.6: Document the enabled-path manual test for the PR description**

The enabled-path test (`ENABLE_CLASSIFIER=1` → real model download + inference) requires trained weights on HF Hub. Until Task 11's scripts are run end-to-end (model trained + uploaded), the enabled path is verified by unit tests (Tasks 5-10 use mocked models). The PR description includes the manual procedure:

```
ENABLE_CLASSIFIER=1 docker compose up -d --force-recreate vehicle-attributes-cam1
docker compose logs -f vehicle-attributes-cam1
# Expect: "downloading convnext_tiny_v0 weights from gammahazard/..."
# then "loaded convnext_tiny_v0 into cuda"
# then on the next vehicle_gone: attributes populated in metadata.json
```

No commit for this step.

---

## Task 14: CHANGELOG + CONTEXT updates

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `CONTEXT.md`

- [ ] **Step 14.1: Add CHANGELOG entry**

In `CHANGELOG.md` under `## [Unreleased]` → `### Added`, append:

```markdown
- **Vehicle attributes Phase 3 (v0 classifier)** — ConvNeXt-Tiny multi-head fills `metadata.json.attributes` with color + body + make (always) + model (drive-by tracks only). Gated by `ENABLE_CLASSIFIER` env (default 0) so the image deploys before trained weights exist on HF Hub. Reservoir sampling replaces Phase 1's first-N TrackBuffer policy. Training scripts at `scripts/vehicle_attributes/`. *Requires vehicle-attributes rebuild + HF Hub-hosted weights for enabled path.*
```

- [ ] **Step 14.2: Extend CONTEXT.md §4.6**

In `CONTEXT.md` §4.6 vehicle-attributes service entry, append:

```markdown
- **Phase 3 classifier integration:** when `ENABLE_CLASSIFIER=1`, the flush path calls `classifier.run_classifier_and_vote(buf, event_kind)` before writing metadata.json. Lazy model load from the `vehicle-attribute-models:/models` volume (downloaded from HF Hub `gammahazard/vision-labs-vehicle-attributes` on first boot). Multi-head ConvNeXt-Tiny: color (10 classes, VeRi-776), body (8 derived from Cars-196), make (~50 makes), model (196 classes). Model head skipped for `event_kind == "idle"`. Weighted voting (yolo_conf × classifier_prob), confidence thresholds 0.55 / 0.55 / 0.55 / 0.65, make-model consistency check.
- **Reservoir sampling** (Phase 3) in `TrackBuffer.append` replaces Phase 1's first-N capping — kept samples are statistically uniform across the track's lifetime, important for drive-by multi-view voting.
- **Training pipeline** at `scripts/vehicle_attributes/` — one-time offline GPU run that produces `convnext_tiny_v0.safetensors`, uploaded to HF Hub for production lazy-fetch.
```

- [ ] **Step 14.3: Sanity check + commit**

```bash
pytest -q 2>&1 | tail -2 && ruff check . 2>&1 | tail -1
git add CHANGELOG.md CONTEXT.md
git commit -m "docs: Phase 3 classifier integration notes for CHANGELOG + CONTEXT"
```

---

## Task 15: Push branch + open PR

**Files:** none

- [ ] **Step 15.1: Push the branch**

```bash
git push -u origin feat/vehicle-attributes-phase-3-classifier 2>&1 | tail -3
```

- [ ] **Step 15.2: Open the PR**

```bash
gh pr create --title "feat(vehicle-attributes): Phase 3 v0 ConvNeXt-Tiny classifier (disabled by default)" --body "$(cat <<'EOF'
## Summary
- New `services/vehicle-attributes/classifier.py` runs a ConvNeXt-Tiny multi-head classifier (color + body + make + model) at `vehicle_gone` flush, fills the previously-null `attributes` block in `metadata.json`.
- Gated by `ENABLE_CLASSIFIER` env (default `0`). The PR ships disabled — same behavior as Phase 1 — so deploy doesn't depend on trained weights being on HF Hub yet.
- Reservoir sampling replaces Phase 1's first-N TrackBuffer policy → kept crops uniformly span the track's lifetime (essential for drive-by multi-view voting on the model head).
- New `vehicle-attribute-models` Docker volume + HuggingFace Hub lazy download on first inference call.
- Browse cards render an attribute row with `(beta)` tag (visible only when attributes are non-null).
- Training pipeline at `scripts/vehicle_attributes/` (color head on VeRi-776, multi-head fine-tune on Stanford Cars-196, upload to HF Hub).

## Spec
`docs/superpowers/specs/2026-05-21-vehicle-attributes-phase-3-classifier-design.md`

## Plan
`docs/superpowers/plans/2026-05-21-vehicle-attributes-phase-3-implementation.md`

## Test plan
- [x] ~440 pytest passing (+~20 new tests across buffer/storage/service/classifier)
- [x] ruff clean
- [x] Image builds with `vision-labs-base:cuda12.8` + torch + timm
- [x] Container stays up with `ENABLE_CLASSIFIER=0` (disabled path verified live)
- [x] Synthetic `vehicle_gone` event writes metadata.json with null attributes (Phase 1 compat preserved)
- [ ] Reviewer: after weights upload, set `ENABLE_CLASSIFIER=1`, rebuild, confirm first boot downloads from HF Hub, verify next real drive-by populates `metadata.attributes`
- [ ] Reviewer: hard-refresh Browse → see `(beta)`-tagged attribute row on cards once enabled path runs

## To deploy after merge
1. Run training scripts at `scripts/vehicle_attributes/` to produce `convnext_tiny_v0.safetensors`
2. Upload to `gammahazard/vision-labs-vehicle-attributes` HF repo (public so first-boot download works without auth)
3. Set `ENABLE_CLASSIFIER=1` in `.env`
4. `docker compose build vehicle-attributes-cam1` + `docker compose up -d --force-recreate vehicle-attributes-cam1`
5. Watch first drive-by — `metadata.attributes` should be populated

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" 2>&1 | tail -3
```

---

## Self-review checklist

- [x] **Spec coverage:** every spec section maps to at least one task. Training run + weights upload deferred to manual post-merge step (Task 11 ships scripts, PR description documents the procedure). PR ships disabled by design.
- [x] **Placeholder scan:** the training scripts contain `NotImplementedError` in their `_load_*` functions — intentional and documented in the README + task descriptions (dataset layout depends on user's local download).
- [x] **Type consistency:** `attributes` dict keys match across `run_classifier_and_vote`, `storage.flush_buffer_to_disk`, the test assertions, and the Browse `_formatAttrs` JS helper.
- [x] **Function signatures match:** `_preprocess(crops)`, `_vote(probs, yolo_confs, classes, threshold)`, `_enforce_make_model_consistency(make_out, model_out, make_to_models)`, `_load_model()`, `_load_classes()`, `run_classifier_and_vote(buf, event_kind)`, `flush_buffer_to_disk(buf, last_seen, event_kind, vehicle_class, snapshot_root, attributes=None)`.
- [x] **Phase 1 backward-compat preserved:** `ENABLE_CLASSIFIER=0` (default) → service skips classifier → null attributes block → Phase 1 metadata.json shape is identical.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-21-vehicle-attributes-phase-3-implementation.md`.** Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task + two-stage review between tasks, fast iteration. CLAUDE.md §15 verification discipline applied after every implementer report.

**2. Inline Execution** — execute tasks in this session with batch checkpoints.

**Which approach?**
