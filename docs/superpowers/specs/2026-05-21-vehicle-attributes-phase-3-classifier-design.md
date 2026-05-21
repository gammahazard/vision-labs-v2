# Vehicle Attributes — Phase 3 Classifier (v0) Design

**Date:** 2026-05-21
**Author:** Claude (with mongo)
**Builds on:** `docs/superpowers/specs/2026-05-21-vehicle-attribute-classification-design.md` (the Phase 1-5 master spec)
**Predecessor:** Phase 1 (PR #21, just merged) — per-track HD crops + null `attributes` placeholders in `metadata.json`

---

## Goal

Replace Phase 1's all-null `attributes` block in `metadata.json` with **real predictions** from a multi-head ConvNeXt-Tiny classifier trained on public datasets. No local fine-tuning required for v0 — the explicit user direction is *ship predictions, view them in Browse, decide if Labeler/local fine-tuning is needed afterward*.

Output schema (extends Phase 1's metadata.json):

```json
"attributes": {
  "color": "silver",
  "color_confidence": 0.87,
  "body_type": "sedan",
  "body_type_confidence": 0.91,
  "make": "Honda",
  "make_confidence": 0.74,
  "model": "Civic",
  "model_confidence": 0.68,
  "voting_samples": 8,
  "classifier_version": "v0-convnext-tiny-2026-05-21"
}
```

`color` / `body_type` / `make` populated on every track. `model` populated on **drive-by tracks only** (multi-view voting requires angle diversity that idle tracks lack — see §1.2).

---

## 1. Architectural decisions (locked in this design)

### 1.1 Backbone choice: ConvNeXt-Tiny everywhere for v0

The Phase 1 master spec listed YOLO-cls-small, EfficientNet-B0, ConvNeXt-Tiny, DINOv2-base, and DINOv2-large as candidates. After the v0 design pass:

| Backbone | Params | Inference VRAM (batch 8) | Latency (3090) | Stanford Cars top-1 | Available pretrained checkpoints |
|---|---|---|---|---|---|
| YOLO-cls-small | 3 M | ~200 MB | ~30 ms | ~89% | Limited Cars checkpoints |
| EfficientNet-B0 | 5 M | ~250 MB | ~50 ms | ~92% | Yes, common |
| **ConvNeXt-Tiny** | **28 M** | **~600 MB** | **~80 ms** | **~93–94%** | **Yes, well-supported via `timm`** |
| DINOv2-base | 86 M | ~1.4 GB | ~150 ms | ~95%+ (with classifier head fine-tune) | No off-the-shelf Cars head — would need self-fine-tune |
| DINOv2-large | 300 M | ~3.5 GB | ~400 ms | ~96%+ | No off-the-shelf Cars head |

**Decision: ConvNeXt-Tiny across all tiers for v0.** ~600 MB VRAM per cam fits the existing small-tier budget (6 GB cards have ~4 GB free after pose/vehicle/face detectors); pretrained Stanford Cars-196 checkpoints exist on `timm` so no self-fine-tuning needed for the make/model heads. DINOv2 deferred — needs self-fine-tuning which we don't want to take on in v0.

**v1 polish:** the original Phase 1 master spec proposed per-tier backbone (EffB0 small / ConvNeXt-T mid / DINOv2 full). v0 ships one backbone; v1 introduces the tier mapping once the v0 pipeline is validated on real cam1 footage.

### 1.2 Hierarchical task split by `event_kind`

Phase 1 classifies each flushed track as `"drive_by"` or `"idle"` in `metadata.json.event_kind` (drive-by = `vehicle_gone` with `was_idle=False`; idle = `vehicle_gone` with `was_idle=True` OR mid-life `vehicle_idle`).

| `event_kind` | Color | Body type | Make | Model |
|---|---|---|---|---|
| `drive_by` (multi-angle from front-quarter → side → rear-quarter) | ✓ predict | ✓ predict | ✓ predict | **✓ predict** |
| `idle` (single angle, redundant samples) | ✓ predict | ✓ predict | ✓ predict | ✗ skip → `null` |

The model head is gated to drive-by tracks because:
- Idle tracks have many samples but they're all the same view — voting averages redundant evidence, no fine-grained discrimination
- Drive-by tracks have ~8 samples spanning entry/mid/exit, giving multi-view voting genuine signal on grille, side silhouette, taillights
- Per the NSW Police paper, model-level accuracy is ~50–60% even on multi-view; on single-view (idle) it drops below 30% — not worth surfacing

### 1.3 Reservoir sampling replaces Phase 1's first-N buffer policy

Phase 1's `TrackBuffer.append` uses first-N capping (keep the first 8 crops, drop later ones). This was fine for the "just save crops" use case but biases toward entry frames — losing rear-quarter exit angles, which is precisely the data needed for model prediction.

**v0 introduces reservoir sampling** in `TrackBuffer.append`:

```python
def append(self, crop: bytes, yolo_conf: float, bbox: list[int]) -> None:
    self._n_seen += 1
    if len(self.crops) < self.max_crops:
        # Buffer not full yet — always keep
        self.crops.append(crop)
        self.confidences.append(yolo_conf)
        self.bboxes.append(list(bbox))
        return
    # Buffer full — reservoir replacement with decreasing probability
    j = random.randrange(self._n_seen)
    if j < self.max_crops:
        self.crops[j] = crop
        self.confidences[j] = yolo_conf
        self.bboxes[j] = list(bbox)
```

Result: each sample in the track has equal probability of being in the final buffer, regardless of when it arrived. For drive-bys of any length, the kept samples are uniformly distributed across entry/mid/exit. Phase 1's storage_test_flush expectations need updating (they currently assert specific crops are kept by first-N policy).

### 1.4 Multi-head model architecture

Single ConvNeXt-Tiny backbone, four task heads:

```python
class VehicleAttrClassifier(nn.Module):
    def __init__(self):
        super().__init__()
        self.backbone = timm.create_model('convnext_tiny', pretrained=True,
                                          num_classes=0)  # feature extractor
        feat_dim = self.backbone.num_features  # 768
        self.color_head = nn.Linear(feat_dim, len(COLOR_CLASSES))   # 10
        self.body_head  = nn.Linear(feat_dim, len(BODY_CLASSES))    # ~8
        self.make_head  = nn.Linear(feat_dim, len(MAKE_CLASSES))    # ~50
        self.model_head = nn.Linear(feat_dim, len(MODEL_CLASSES))   # 196

    def forward(self, x):
        feats = self.backbone(x)
        return {
            'color': self.color_head(feats),
            'body':  self.body_head(feats),
            'make':  self.make_head(feats),
            'model': self.model_head(feats),
        }
```

Shared backbone means one inference pass produces all four logits — total per-crop latency ~80 ms on 3090. The body head is trained on derived labels (see §2.2); the other three are trained on their respective public datasets.

### 1.5 Make-model consistency post-check

When both make and model heads exceed their confidence thresholds, verify the top model belongs to the top make's roster. Prevents "Toyota Civic"-style cross-make contradictions.

```python
# Static mapping derived from Stanford Cars-196 class names
MAKE_TO_MODELS = {
    'Honda': ['Accord', 'Civic', 'CR-V', 'Odyssey', 'Pilot', ...],
    'Toyota': ['Camry', 'Corolla', 'Highlander', ...],
    ...
}

if make_winner and model_winner:
    if model_winner not in MAKE_TO_MODELS.get(make_winner, ()):
        # Mismatch — drop the less-confident prediction
        if model_conf > make_conf:
            make_winner = None  # downgrade make instead
        else:
            model_winner = None
```

---

## 2. Training data + weights

### 2.1 Color head — VeRi-776

VeRi-776 vehicle re-identification dataset. ~50K vehicle crops labeled with 10 colors:

`yellow / orange / green / gray / red / blue / white / golden / brown / black`

These cover the practical range of consumer vehicle colors. The home-cam transfer should be good — color is largely view-invariant.

**Training:** freeze ConvNeXt-Tiny backbone (pretrained on ImageNet), train color_head with cross-entropy for ~10 epochs. ~2 hours on a 3090. Final weights checkpointed as `convnext_tiny_veri776_color.pth`.

### 2.2 Body type head — derived from Stanford Cars

Stanford Cars-196's class names embed body type (`"Honda Civic 2012 Sedan"`, `"Ford F-150 Regular Cab 2012"`). We extract body type via a static mapping built once at training time:

```python
BODY_TYPES = ('sedan', 'suv', 'coupe', 'pickup', 'van',
              'hatchback', 'convertible', 'wagon')
```

Each Stanford Cars-196 class gets a body label via regex on its name. The body_head is trained alongside the model head (same backbone fine-tune pass) using the derived labels.

### 2.3 Make head — aggregated from Stanford Cars

Stanford Cars-196's 196 classes have ~50 unique manufacturers. Each class gets a make label via string prefix:

```python
'Honda Civic 2012 Sedan' → make='Honda'
'Ford F-150 Regular Cab 2012' → make='Ford'
```

The make_head shares the backbone with the model head. Training: fine-tune the full network on Stanford Cars-196 with summed cross-entropy across the model + make + body losses (all derived from the same images, just different label projections).

### 2.4 Model head — direct Stanford Cars-196

The full 196-class output. Confidence threshold tighter (0.65 vs 0.55) since the fine-grained task is harder and we'd rather suppress weak predictions.

### 2.5 Pretrained checkpoint sourcing

A pretrained ConvNeXt-Tiny on Stanford Cars-196 is available via `timm` (e.g., `timm/convnext_tiny.stanford_cars`). The make/body heads' training run starts from this checkpoint and just trains the new heads + (optionally) fine-tunes the last few backbone blocks. Total training time ~2 hours on a 3090.

**Weights distribution:**
- We train + bundle the weights ourselves (one combined checkpoint covering all four heads)
- Released to Hugging Face Hub at e.g. `gammahazard/vision-labs-vehicle-attributes` (private to start; can mirror to a public CDN later)
- vehicle-attributes-cam{N} downloads on first boot into the new `vehicle-attribute-models:/models` shared volume (mirror of `insightface-models` pattern)
- `VEHICLE_ATTR_MODEL` env var points at the model name; defaults to `convnext_tiny_v0`

---

## 3. Inference flow

### 3.1 Container changes from Phase 1

**Dockerfile** — swap base from `python:3.11-slim` → `vision-labs-base:cuda12.8`. Note `vision-labs-base` deliberately does NOT ship PyTorch (see `services/base/Dockerfile` comment) — each service installs torch + framework on top. Mirror pose-detector's install pattern:

```dockerfile
FROM vision-labs-base:cuda12.8
WORKDIR /app
RUN pip install --no-cache-dir \
      torch torchvision \
      --index-url https://download.pytorch.org/whl/cu128
RUN pip install --no-cache-dir \
      timm==1.0.11 huggingface_hub safetensors \
      opencv-python-headless==4.13.0.92 \
      numpy>=1.26.4 Pillow>=10.4.0 redis==5.3.1
COPY service.py buffer.py storage.py classifier.py ./
RUN mkdir -p /data
CMD ["python", "-u", "service.py"]
```

Image size grows from ~200 MB (Phase 1 slim base) → ~5 GB (CUDA + PyTorch baseline). Worth it; matches pose-detector/vehicle-detector image size. The opencv + redis + numpy lines preserve Phase 1's existing crop pipeline dependencies.

**Compose blocks** — every `vehicle-attributes-cam{N}` block gains:

```yaml
    environment:
      - DETECTOR_GPU=${DETECTOR_GPU:-0}
      - CUDA_VISIBLE_DEVICES=${DETECTOR_GPU:-0}
      - CUDA_DEVICE_ORDER=PCI_BUS_ID
      - VEHICLE_ATTR_MODEL=${VEHICLE_ATTR_MODEL:-convnext_tiny_v0}
      - VEHICLE_ATTR_HF_REPO=${VEHICLE_ATTR_HF_REPO:-gammahazard/vision-labs-vehicle-attributes}
      - MIN_CROP_AREA_HD_PX=${MIN_CROP_AREA_HD_PX:-2500}
      - COLOR_CONF_THRESHOLD=${COLOR_CONF_THRESHOLD:-0.55}
      - BODY_CONF_THRESHOLD=${BODY_CONF_THRESHOLD:-0.55}
      - MAKE_CONF_THRESHOLD=${MAKE_CONF_THRESHOLD:-0.55}
      - MODEL_CONF_THRESHOLD=${MODEL_CONF_THRESHOLD:-0.65}
    volumes:
      - ./contracts:/app/contracts:ro
      - ./:/workspace:ro
      - snapshot-data:/data/snapshots
      - vehicle-attribute-models:/models
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ['${DETECTOR_GPU:-0}']
              capabilities: [gpu]
```

The compose env list grows by ~12 lines per block × 20 blocks. Use yaml anchors or a separate values file if it starts getting unwieldy.

### 3.2 Service code changes

The Phase 1 service already has the flush flow. Phase 3 inserts the classifier step:

```python
# OLD (Phase 1):
def _flush(event, buffers, snapshot_root):
    buf = buffers.pop(track_id, None)
    if buf is None: return
    flush_buffer_to_disk(buf, last_seen, event_kind, vehicle_class, snapshot_root)

# NEW (Phase 3):
def _flush(event, buffers, snapshot_root):
    buf = buffers.pop(track_id, None)
    if buf is None: return
    attributes = run_classifier_and_vote(buf, event_kind)  # NEW
    flush_buffer_to_disk(buf, last_seen, event_kind, vehicle_class,
                         snapshot_root, attributes=attributes)  # extra kwarg
```

`run_classifier_and_vote(buf, event_kind)` is a new module (`services/vehicle-attributes/classifier.py`).

**Preprocessing** before inference — ConvNeXt-Tiny expects 224×224 RGB input. The crops in `buf.crops` are JPEG bytes of variable-size HD slices (typically 80×40 to 400×200 px depending on vehicle distance). The preprocessing step:

```python
def _preprocess(jpeg_crops: list[bytes]) -> torch.Tensor:
    """Decode → resize-with-aspect → center-pad to 224×224 → normalize."""
    from torchvision import transforms
    transform = transforms.Compose([
        transforms.Resize(224, antialias=True),       # short side → 224
        transforms.CenterCrop(224),                    # square center crop
        transforms.ToTensor(),                         # → [0, 1] CHW
        transforms.Normalize(mean=[0.485, 0.456, 0.406],
                             std=[0.229, 0.224, 0.225]),  # ImageNet stats
    ])
    images = []
    for jpeg in jpeg_crops:
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)      # BGR
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(img)
        images.append(transform(pil))
    return torch.stack(images)  # BCHW, B = len(jpeg_crops)
```

ImageNet normalization stats match what ConvNeXt-Tiny was pretrained with. Center-crop after resize keeps the vehicle in the middle of the 224×224 window — works because Phase 1's bbox crops are already vehicle-centered by construction.

`classifier.py` skeleton:

```python
def run_classifier_and_vote(buf: TrackBuffer, event_kind: str) -> dict:
    """Decode crops, run inference, vote across crops, apply confidence
    thresholds. Returns the attributes dict that storage.py merges into
    metadata.json. Model head skipped when event_kind == 'idle'."""
    model = _load_model()  # cached singleton
    crops_t = _preprocess(buf.crops)  # list[bytes] → BCHW tensor
    with torch.inference_mode():
        logits = model(crops_t.cuda())  # {color, body, make, model: BxC tensors}
    probs = {task: torch.softmax(t, dim=1) for task, t in logits.items()}

    skip_model = (event_kind == 'idle')

    return {
        'color': _vote(probs['color'], buf.confidences, COLOR_CLASSES,
                       COLOR_CONF_THRESHOLD),
        'body_type': _vote(probs['body'], buf.confidences, BODY_CLASSES,
                           BODY_CONF_THRESHOLD),
        'make': _vote(probs['make'], buf.confidences, MAKE_CLASSES,
                      MAKE_CONF_THRESHOLD),
        'model': (None if skip_model else
                  _vote(probs['model'], buf.confidences, MODEL_CLASSES,
                        MODEL_CONF_THRESHOLD)),
        'voting_samples': len(buf.crops),
        'classifier_version': MODEL_VERSION,
    }
```

`_vote()` implements the weighted majority vote from the Phase 1 master spec §3.2:

```python
def _vote(per_crop_probs: torch.Tensor, yolo_confs: list[float],
         classes: list[str], threshold: float) -> tuple[str | None, float]:
    """Weighted majority vote: per-class score = sum across crops of
    (classifier_prob * yolo_confidence). Winner must exceed threshold."""
    yc = torch.tensor(yolo_confs, device=per_crop_probs.device)
    weighted = (per_crop_probs * yc.unsqueeze(1)).sum(dim=0)
    weighted = weighted / weighted.sum()  # normalize
    winner_idx = weighted.argmax().item()
    winner_conf = weighted[winner_idx].item()
    if winner_conf < threshold:
        return (None, winner_conf)
    return (classes[winner_idx], winner_conf)
```

Then the make-model consistency check from §1.5 runs as a post-step before returning the attributes dict.

### 3.3 Storage integration

`storage.flush_buffer_to_disk` takes a new optional `attributes` kwarg:

```python
def flush_buffer_to_disk(buf, last_seen, event_kind, vehicle_class,
                         snapshot_root, attributes=None):
    # ... existing dir creation + hero/angle writes ...
    meta = {
        # ... existing fields ...
        "attributes": attributes or {
            "color": None, "color_confidence": None,
            "body_type": None, "body_type_confidence": None,
            "make": None, "make_confidence": None,
            "model": None, "model_confidence": None,
        },
    }
    # ... existing json.dump ...
```

Phase 1 callers pass `attributes=None` → all-null block (unchanged). Phase 3 callers pass the predicted dict → populated block. Backward-compatible.

---

## 4. UI changes (Browse panel)

Phase 1 already renders grouped cards with class + duration + voting_samples + null attributes. Phase 3 surfaces the populated attributes:

```
┌──────────────────────────────────┐
│ [hero.jpg]                       │
│ 🚗 car                           │
├──────────────────────────────────┤
│ 4:25 PM · drive_by               │
│ 8 angles · 2.4s                  │
├──────────────────────────────────┤
│ black sedan · Honda Civic (beta) │
└──────────────────────────────────┘
```

- New attribute row at the bottom of each track card
- Show only non-null attributes (skip cleanly if all four are null)
- `(beta)` tag styled in muted gray, positioned at end of the attribute line
- Drive-by cards may show 4 attributes (color + body + make + model); idle cards show 3 (no model)

**Implementation:** small addition to `_renderDayTracks()` in `browse.js`. The data is already on the server side (we extended the `/api/browse/tracks/{date}` response to include `attributes` in Phase 1; the field has just been all-null until now).

CSS for `(beta)` tag (added to `style.css`):

```css
.track-attrs { color: #cbd5e1; font-size: 0.78rem; padding: 0 0.75rem 0.75rem; }
.track-attrs-beta { color: #64748b; font-size: 0.7rem; font-style: italic; margin-left: 0.4rem; }
```

---

## 5. Setup wizard + Cameras UI

Phase 1 already wired `detect_vehicle_attributes` into setup wizard + add-camera + edit-modal. Phase 3 doesn't change any of those — the flag's meaning is the same, just now the service does more when it's on.

**No tier-specific UI for v0** (since we ship ConvNeXt-Tiny everywhere). v1 will add a backbone selector when the tier mapping ships.

---

## 6. Per-camera angle diversity caveat

For cam1's Reolink wide-angle (~fisheye) lens, the user-confirmed view shows front-of-car at entry → side at mid → rear at exit. Good multi-view diversity. Model prediction should be feasible.

**Risks:**
- Barrel distortion at frame edges (entry + exit positions) may make Stanford-Cars-trained classifier less confident on those crops. Mid-frame crops are cleanest.
- The reservoir sampling buffer policy means the kept samples are distributed across entry/mid/exit uniformly. Mid samples will dominate the vote anyway (cleaner views, higher YOLO confidence), so the vote should naturally weight toward the undistorted center frames.
- If accuracy is weak after deploy, **v1 polish item**: add fisheye undistortion (OpenCV `cv2.fisheye.undistortImage` with empirically-calibrated K + D matrices for the specific Reolink model) before classifier input.

For non-fisheye cams, model prediction may be even better (no distortion) — or worse if the cam is perpendicular without wide angle (single-view drive-bys). v0 ships model prediction always; the confidence threshold (0.65) will naturally suppress most predictions on cams with bad geometry.

---

## 7. Acceptance criteria

Phase 3 v0 ships when:
- [ ] vehicle-attributes-cam{N} image builds with `vision-labs-base:cuda12.8` base
- [ ] Model weights download from HF Hub on first container boot
- [ ] Classifier runs on `vehicle_gone` flush, populates `metadata.json.attributes`
- [ ] `model` field stays null for `event_kind == "idle"` tracks
- [ ] Make-model consistency check fires when both pass threshold and the model belongs to a different make
- [ ] Browse cards render attribute row with `(beta)` tag, hide nulls cleanly
- [ ] Reservoir sampling lands as the new TrackBuffer policy + tests updated
- [ ] At least 50 real cam1 tracks captured after deploy; manually inspect 10 in Browse to assess transfer quality
- [ ] No regression: pre-Phase-3 tracks (existing per-track dirs with null attributes) still render in Browse

**No quantitative accuracy target for v0** — we deliberately want to view the labels in Browse and decide if local fine-tuning is needed. v1 will set targets after seeing the v0 distribution.

---

## 8. Effort estimate

| Task | Engineering | Training | Calendar |
|---|---|---|---|
| Reservoir sampling buffer fix + tests | 0.5 day | 0 | 0.5 day |
| Multi-head classifier training script (color + Cars fine-tune) | 1 day | ~2 hours on RTX 3090 | 1 day |
| Push weights to HF Hub | 0.5 day | 0 | 0.5 day |
| Service container: base swap, classifier integration, voting, consistency check | 2 days | 0 | 2 days |
| Storage.py `attributes` kwarg wiring | 0.5 day | 0 | 0.5 day |
| Browse UI attribute row + `(beta)` styling | 0.5 day | 0 | 0.5 day |
| Tests (unit: voting math, consistency check; integration: end-to-end inference with synthetic crops) | 1 day | 0 | 1 day |
| Deploy + watch cam1 for a few evenings | 0 days | 0 | ~2-3 days passive |
| **Total active engineering** | **~6 days** | ~2 hours | ~1 work-week + watching period |

---

## 8b. Risks + open questions (real, not deferred)

### Training data licensing — **acceptable for our use case**

- **Stanford Cars-196** (Krause et al., 2013) and **VeRi-776** (Liu et al., 2016) are both released for research use only. The strict interpretation restricts commercial redistribution.
- **Our use case fits the research carve-out:** vision-labs is a hobbyist/educational project, self-hosted, not sold. Distributing derived classifier weights via the project's HF Hub repo for other self-hosted research/learning users falls within the spirit of the license. HuggingFace Hub hosts thousands of Stanford-Cars-finetuned models without incident.
- **Lightweight mitigation worth doing:** README + spec acknowledge the datasets + their licenses; users with a commercial use case are pointed to alternatives (CompCars, OpenColorVehicleDB, or self-training on their own data). Weights are published with a "research/educational use only" notice. No active blocker.

If we later add a commercial offering, retrain on permissively-licensed data at that point.

### Pretrained ConvNeXt-Tiny on Stanford Cars availability

The spec assumes `timm/convnext_tiny.stanford_cars` (or equivalent) exists on HuggingFace. **Verify before relying on it.** If not available, we need to fine-tune ConvNeXt-Tiny on Stanford Cars ourselves — adds ~3-4 hours of GPU time and complexity. Either way, the COLOR head needs self-training (no public ConvNeXt-Tiny VeRi-776 checkpoints exist).

### Weights download failure handling

First-boot weights fetch from HF Hub can fail (network, rate limits, account auth). Service behavior:
1. Retry 3× with exponential backoff (1 s, 5 s, 20 s)
2. On persistent failure, exit with code 2 (non-zero — Docker `restart: on-failure` will retry the container indefinitely, useful when the issue is transient)
3. Log a clear error message including the exact HF repo + model ref attempted
4. **No fallback to "crops-only mode"** — without weights, the service has no purpose. Better to fail loudly than silently degrade.

### Phase 1 test compatibility

The reservoir sampling change in §1.3 will break `tests/test_vehicle_attributes_buffer.py::test_buffer_caps_at_max_crops` which asserts deterministic first-N retention. Update needed:

```python
def test_buffer_caps_at_max_crops():
    import random
    random.seed(42)  # deterministic for the test
    b = TrackBuffer(track_id="v", camera_id="cam1", first_seen=0.0,
                    max_crops=3)
    for i in range(10):
        b.append(crop=bytes([i]), yolo_conf=0.5, bbox=[0, 0, 1, 1])
    # Buffer always at cap regardless of policy
    assert len(b.crops) == 3
    assert b.is_full() is True
    # Reservoir-specific: NOT just the first N
    # (with random.seed(42), the kept indices are deterministic but spread)
    kept_bytes = sorted(int(c[0]) for c in b.crops)
    assert kept_bytes != [0, 1, 2], (
        "first-N retention would be [0,1,2]; reservoir picks spread samples"
    )
```

Also need a new test asserting statistical uniformity of reservoir sampling across many runs.

---

## 9. What's deferred to v1+

- **Per-tier backbone** (Eff-B0 / ConvNeXt-T / DINOv2-base) — covered by v0's `VEHICLE_ATTR_MODEL` env var but only the ConvNeXt-T weights ship with v0
- **Fisheye undistortion** for Reolink (and other wide-angle) cams — investigate if cam1 v0 accuracy is weak
- **Local fine-tuning + Labeler tab** — only if v0 results show transfer quality is poor; deferred per user direction
- **Browse filter chips by attribute** ("show me black SUVs from yesterday") — Phase 5 in the master spec
- **AI tool `query_vehicles_by_attributes`** — Phase 5
- **Telegram caption "Likely:" lines on `vehicle_idle`** — Phase 4 (after we drop the `(beta)` tag)

---

## 10. Open questions resolved by this design

| Question | Resolution |
|---|---|
| Backbone for v0? | ConvNeXt-Tiny everywhere |
| Tasks for v0? | Color + body + make (all tracks) + model (drive-by only) |
| Model = manufacturer or model-year-trim? | Manufacturer-level for `make` (~50 classes); model-year-trim flattened to model name for `model` (e.g., "Civic") via Stanford Cars naming |
| Train ourselves or use pretrained? | Pretrained ConvNeXt-T on Stanford Cars via `timm` (no self-train for make/model). Train color head ourselves on VeRi-776 (~2 hr on 3090). Body type derived deterministically. |
| Multi-head vs separate models? | Single multi-head — shared backbone, separate task heads |
| Buffer policy? | **Reservoir sampling** (changed from Phase 1's first-N) — guarantees uniform multi-view coverage |
| Inference timing? | At flush (`vehicle_gone`), not per-sample |
| Confidence thresholds? | 0.55 for color/body/make; 0.65 for model |
| Make-model contradiction handling? | Static MAKE_TO_MODELS roster check; drop the less-confident half on mismatch |
| Where do weights live? | New `vehicle-attribute-models:/models` Docker volume, lazy fetch from HF Hub on first boot |
| Service GPU? | Existing `DETECTOR_GPU` env (shared with that cam's pose/vehicle/face detectors) |
| `(beta)` tag in UI? | Yes for v0; drop in v1 when we set accuracy targets |
