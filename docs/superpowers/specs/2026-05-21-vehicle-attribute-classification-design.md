# Vehicle Attribute Classification — Design

**Status:** Draft / unimplemented · **Author:** Claude (with mongo) · **Date:** 2026-05-21

## Goal

Add make / body-type / color attribution to every vehicle the system tracks, surface those attributes in the dashboard event feed + Telegram captions, and rebuild the Browse > Vehicle Snapshots tab so it groups per-physical-vehicle (instead of per-event), showing a multi-angle photo strip and the estimated attributes underneath.

Concretely, after this feature ships a user looking at the events feed should see:

> 🚗 16:25 — Silver SUV (vehicle_0042) — drove by · 4 angles captured
>
> 🚗 14:00 — Red sedan idling for 30 min · likely Toyota Camry-class

…instead of the current row that just says:

> 🚗 16:25 — vehicle_detected — car (0.83 conf)

---

## 1. Why this is the right shape

### 1.1 Side-view + low-res constraints (from real data inspection 2026-05-21)

The user's cam1 setup looks across a lawn at the street ~30-50 m away. Vehicles appear in the upper-right of the sub-stream as ~80×40 px blobs partially behind tree foliage. **At sub-stream resolution they are not classifiable** — a human can't tell make/model from these crops, and a classifier won't outperform a human on the same crop.

### 1.2 The HD frame is the key resource

Two Redis-side artifacts that already exist:
- `frames:{cam}` — sub-stream, 896×512, ~10 FPS, last 1000 entries. **Detection inference runs on this.**
- `frame_hd:{cam}` — HD frame, 2304×1296, single Redis key with 5-second TTL, refreshed each main-stream frame. **No inference runs on this today — it's used only for the dashboard live view + snapshot serving.**

Scaling sub-stream bbox coords into the HD frame: `(x, y) × (2304/896, 1296/512) ≈ (×2.57, ×2.53)`. A `80×40` sub-stream vehicle crop becomes `≈206×100` in HD — large enough for fine-grained classification (most classifiers expect ≥ 224×224 input; 206×100 upscales cleanly).

### 1.3 Two complementary capture scenarios

| Scenario | Frame supply | Angle diversity | Classifier value |
|---|---|---|---|
| Idle (parked car) | Effectively unlimited (parked for minutes-hours, 10 FPS = thousands of frames) | None — single angle | Modest. Multi-frame voting defends against YOLO jitter; doesn't help with attribute disambiguation. |
| Drive-by (passing car) | 2-5 seconds × 10 FPS = 20-50 frames per track | Real — entry/middle/exit show front-quarter → side → rear-quarter | High. Each angle contributes different signal (grille for make, silhouette for body, badge for model). |

Drive-bys are the *better* training-data + voting case despite having fewer frames per track. This flips the obvious "do idle first" intuition.

---

## 2. Architecture

### 2.1 New service: `vehicle-attributes-cam{N}`

Single-purpose Python service, one container per active camera profile (mirrors the existing per-cam services).

```
                                        ┌──────────────────────────┐
                                        │ vehicle-attributes-cam1  │
camera-ingester ──XADD frames:cam1──▶┐  │  reads:                  │
                                     │  │    events:cam1 (filter   │
camera-ingester ──SETEX frame_hd:cam1─┤  │      to vehicle_*)      │
                                     │  │    frame_hd:cam1 (on     │
vehicle-detector-cam1 ──XADD          │  │      demand at vehicle_ │
                detections:vehicle:cam1┤  │      detected updates)  │
                                     │  │  writes:                 │
tracker-cam1 ──XADD events:cam1 ─────┘  │    vehicle_crops:{track} │
                                        │    vehicle_attributes:   │
                                        │      {track}             │
                                        │    /data/snapshots/      │
                                        │      vehicles/{cam}/     │
                                        │      {date}/{track_id}/  │
                                        └──────────────────────────┘
```

Why a separate service vs bolting onto the tracker:
1. GPU isolation — runs classifier inference, may compete for VRAM with face-recognizer / pose-detector
2. CPU isolation — voting + cropping is moderate work; doesn't block the tracker's per-frame update loop
3. Independent restart — model swaps + retraining cycles don't require recreating the tracker
4. Same pattern as `face-recognizer-cam{N}` (consumes detections, runs ML inference, writes attributes to Redis)

### 2.2 Per-track HD frame buffer (lives in the new service)

The tracker is **not** modified. Instead the attribute service maintains its own per-track buffer:

```python
@dataclass
class TrackBuffer:
    track_id: str          # vehicle_0042
    camera_id: str         # cam1
    crops: list[bytes]     # HD JPEG crops, capped at 8
    confidences: list[float]  # YOLO confidence at each capture
    last_sampled_at: float    # monotonic; for debounce
    first_seen: float         # tracker's vehicle_first_seen
```

The service consumes the `events:{cam}` stream (already public and consumed by the dashboard's notification poller) — looking for `vehicle_detected` to start a buffer, periodic `vehicle_detected` updates from the tracker to grow it, and `vehicle_left` / `vehicle_idle` to flush + classify.

**Wait** — the tracker today emits `vehicle_detected` once per new track, not periodically. We need a sample-trigger mechanism. Two options:

**Option A: Tracker emits new `vehicle_sample` events.** Modify the tracker to emit a low-weight `vehicle_sample` event for every active track every N frames. Pro: clean, opt-in via config. Con: adds new event type to schema.

**Option B: Attribute service subscribes directly to `detections:vehicle:{cam}` instead of `events:{cam}`.** Pro: gets every detection without tracker changes. Con: detections are pre-tracking — no track_id assigned yet. Would need to do its own IoU matching, duplicating work.

**Chosen: Option A.** New event type `vehicle_sample`, emitted by tracker every 3rd update on each active TrackedVehicle. Trivial change in `manager.py:_process_vehicle_detections`. Schema-additive (existing consumers ignore unknown event types).

```python
# In tracker manager.py, inside the matched-vehicle update branch:
veh.sample_count += 1
if veh.sample_count % SAMPLE_INTERVAL_FRAMES == 0:  # default 3
    self._emit_vehicle_sample_event(veh, timestamp)
```

### 2.3 Sampling + buffering rules

```
on vehicle_detected:
    open TrackBuffer for this track_id

on vehicle_sample (or any vehicle_detected with a track_id already buffered):
    if len(buffer.crops) < 8:
        pull frame_hd:{cam} from Redis
        if frame missing or stale → skip (will retry on next sample)
        scale bbox from sub → HD coords (×2.57 / ×2.53)
        apply 20% bbox padding (capture surrounding context — wheels, shadow, partial road)
        crop HD frame
        encode JPEG at quality 85
        if crop area > MIN_CROP_AREA (e.g. 50×50 px in HD coords):
            append to buffer.crops + buffer.confidences
            buffer.last_sampled_at = now

on vehicle_left OR vehicle_idle:
    if len(buffer.crops) == 0:
        emit empty vehicle_attributes (no inference)
        clean up
    else:
        run classifier on each crop in buffer.crops
        per-task weighted majority vote (weighted by classifier confidence × YOLO confidence)
        write vehicle_attributes:{track_id} to Redis (24h TTL)
        save crops + metadata.json to /data/snapshots/vehicles/{cam}/{date}/{track_id}/
        emit `vehicle_attributes_ready` event (consumed by Browse UI + Telegram caption gen)
        clean up buffer
```

**Tuning knobs (env-overridable):**
- `SAMPLE_INTERVAL_FRAMES` = 3 (sample every 3rd detection)
- `MAX_BUFFER_CROPS` = 8
- `MIN_CROP_AREA_HD_PX` = 50 × 50 (skip crops below this — usually means vehicle is too far / too occluded)
- `CROP_PADDING_PCT` = 0.20 (expand bbox 20% before cropping)

### 2.4 Memory + compute budget

- Per active TrackedVehicle: 8 × ~100 KB HD crops = ~800 KB
- ~5-10 active vehicles at any moment: 5-8 MB peak in the attribute service
- Classifier inference: ~50 ms per HD crop on RTX 3090 (EfficientNet-B0) or ~150 ms (DINOv2-base)
- Per drive-by: ~8 inferences × 50 ms = 400 ms total → ~30 seconds of GPU time/day at 100 events/day
- Cleared on `vehicle_left` so no leak

Trivial cost.

### 2.5 Storage layout (new, replaces flat per-event)

Today:

```
/data/snapshots/vehicles/cam1/2026-05-21/16-25-59_car.jpg    ← full sub-stream frame, per event
/data/snapshots/vehicles/cam1/2026-05-21/16-25-57_car.jpg    ← another event same drive-by
```

Proposed:

```
/data/snapshots/vehicles/cam1/2026-05-21/vehicle_0042/
    metadata.json          ← {track_id, first_seen, last_seen, attribute_mode votes, etc}
    hero.jpg               ← the highest-confidence crop, ~250 KB HD-cropped
    angle_01.jpg           ← additional captures from the track
    angle_02.jpg
    ...
    angle_07.jpg
```

`metadata.json`:

```json
{
  "track_id": "vehicle_0042",
  "camera_id": "cam1",
  "first_seen": 1779394901.5,
  "last_seen": 1779394907.2,
  "duration_seconds": 5.7,
  "event_kind": "drive_by",          // "drive_by" | "idle"
  "vehicle_class": "car",             // from YOLO COCO class
  "attributes": {
    "color": "silver",
    "color_confidence": 0.87,
    "body_type": "sedan",
    "body_type_confidence": 0.91,
    "make": "Honda",
    "make_confidence": 0.62,         // lower because side-view
    "model": null,                    // skipped — not feasible at this resolution
    "voting_samples": 8
  },
  "hero_frame_index": 3,              // angle_03 was picked as hero
  "snapshot_bbox": [728, 322, 807, 359]
}
```

Backward-compat: the existing flat per-event JPEGs can be left in place during rollout — the new directory layout coexists. A migration step (script) moves old snapshots into per-track dirs once we're confident the new pipeline works.

---

## 3. Classifier choice

### 3.1 Single multi-head model vs three separate models

**Single multi-head:** one backbone (DINOv2-base or EfficientNet-B0), three classification heads (color, body, make). Pro: one inference pass, shared feature extraction, smaller total params. Con: harder to train (multi-task loss balance), harder to swap individual heads.

**Three separate:** one classifier per task. Pro: independent retraining, easier to ship one before the others, can use different backbones if needed. Con: 3× inference cost.

**Chosen: single multi-head.** With shared backbone + 3 heads the total inference is ~50 ms per crop on a 3090, same as one model. Training is more complex but the data is shared, the backbone is fine-tuned once, and we can freeze/unfreeze heads independently when retraining.

Backbone candidates (in order of preference):
1. **YOLO-Classification small** (~3 M params) — already in the codebase via Ultralytics, fits the existing pose+vehicle detector pattern, the NSW Police paper validated it for this exact task.
2. **EfficientNet-B0** (~5 M params) — close second, slightly more accurate per the literature.
3. **DINOv2-base** (~86 M params) — strongest fine-grained but 5× more compute. Probably overkill at our scale.

### 3.2 Voting math

Per crop the classifier emits softmax distributions over (color, body, make). The voter aggregates across N crops:

```python
# Weighted majority vote, weights = classifier_confidence × yolo_confidence
votes: dict[label, weight_sum] = {}
for crop, yolo_conf in zip(buffer.crops, buffer.confidences):
    probs = classifier(crop)  # {label: prob}
    for label, prob in probs.items():
        votes[label] += prob * yolo_conf

winner = max(votes, key=votes.get)
winner_confidence = votes[winner] / sum(votes.values())
```

Three independent votes (one per task), no joint probability. If `winner_confidence` is below `MIN_CONFIDENCE_THRESHOLD` (default 0.55), the attribute is left null instead of forced — better to show no label than a wrong one.

### 3.3 Training data

Per the NSW Police paper, ~10K labeled crops per task is enough to fine-tune from a Stanford Cars / VMMRdb pretrained checkpoint. Our home-cam-angle data won't transfer perfectly, so we'd want at least 500-1000 *locally labeled* crops alongside.

Bootstrap process:
1. Build a labeling UI as a new dashboard tab — scrolls through saved HD crops, dropdowns for (color / body / make / "skip - too occluded"), saves to `/data/labels/vehicle_attributes/{crop_id}.json`.
2. Label 100-200 crops manually to seed.
3. Train a v0 model fine-tuned on Stanford Cars + the seed labels.
4. Run v0 on all unlabeled crops; reviewer accepts/corrects (active learning).
5. After ~500-1000 reviewed labels, ship v1.

**This is the real cost of the project — not the model architecture, not the inference plumbing, but the labeling effort.** Plan on 1-2 weekends of labeling work.

---

## 4. UI changes

### 4.1 Telegram caption (lowest-effort win)

`routes/notifications/alerts.py:notify_vehicle_idle` reads `vehicle_attributes:{track_id}` from Redis and appends a line to the existing caption:

Before:
> 🚗 Vehicle Idling
> • Type: car
> • Stationary: 5 min

After:
> 🚗 Vehicle Idling
> • Likely: silver Toyota sedan (87% confidence)
> • Type: car  ← keep as the YOLO-class fallback
> • Stationary: 5 min

If attributes are unavailable (classifier hasn't run yet, low confidence, or missing track), gracefully omit the "Likely" line.

### 4.2 Browse > Vehicle Snapshots — grouped cards

Replace the current flat thumbnail grid with per-track cards.

```
┌────────────────────────────────────────┐  ┌────────────────────────────────────────┐
│ 16:25:59 — silver Honda sedan          │  │ 16:38:05 — yellow bus                  │
│ drive-by · 5.7 s                       │  │ drive-by · 12 s                        │
│                                        │  │                                        │
│ ┌────────────────────────────────────┐ │  │ ┌────────────────────────────────────┐ │
│ │                                    │ │  │ │                                    │ │
│ │            HERO IMAGE              │ │  │ │            HERO IMAGE              │ │
│ │       (highest-conf crop)          │ │  │ │       (highest-conf crop)          │ │
│ │                                    │ │  │ │                                    │ │
│ └────────────────────────────────────┘ │  │ └────────────────────────────────────┘ │
│                                        │  │                                        │
│ angle strip:                           │  │ angle strip:                           │
│ [▢][▢][▢][▢][▢][▢][▢][▢]              │  │ [▢][▢][▢][▢][▢][▢]                    │
│                                        │  │                                        │
│ Color: silver (0.87)                   │  │ Color: yellow (0.99)                   │
│ Body:  sedan (0.91)                    │  │ Body:  bus (0.99)                      │
│ Make:  Honda (0.62)                    │  │ Make:  — (low confidence)              │
└────────────────────────────────────────┘  └────────────────────────────────────────┘
```

Implementation:
- New endpoint `GET /api/browse/days/{date}/vehicles` returns array of `{track_id, hero_url, angle_urls[], attributes, timing}`.
- `browse.js` rendering changes — grouped card layout instead of flat thumb grid. Reuses the existing DOMPurify-safe `_safeHtml()` pattern + event delegation we just shipped in PR #9.
- Lightbox modal already exists for clicking a thumbnail — extend it to navigate through `angle_urls` (next/prev buttons).
- Mobile responsive: cards stack 1-per-row instead of grid.

### 4.3 Filtering by attribute (future, not v1)

Once attributes are reliable, add filter chips to Browse:
- Color: white / black / silver / red / blue / …
- Body: sedan / SUV / truck / van / motorcycle / bus
- Make: Honda / Toyota / Ford / …

And an AI tool `query_vehicles_by_attributes(make, color, body, date_range)` for natural-language queries.

These are post-v1. Don't build until v1 is in production.

---

## 5. Phased rollout plan

### Phase 0 — Validation (current state, no work)
Confirm the v0.2.0 vehicle-tracking improvements (position dedup, ghost TTL, is_stationary threshold) have stabilized in production for at least a week. If track fragmentation is still spamming the event feed, this whole project waits.

### Phase 1 — Data plumbing (1 week)
- Tracker: emit `vehicle_sample` events every N detection updates (config-gated, default disabled until consumer exists).
- Attribute service skeleton: subscribes to events, maintains per-track buffer, saves crops + metadata.json to the new directory layout. No classifier yet.
- Browse UI: render grouped cards (no attributes — just multi-angle photo strips).
- Outcome: per-track grouping live, multi-angle thumbnails available, no classification yet.

### Phase 2 — Labeling tool (parallel to Phase 1)
- New dashboard tab `Labeler`: scroll through crops, dropdowns, save labels.
- Start labeling weekend 1. Goal: 500 crops labeled across color/body/make.

### Phase 3 — v0 classifier (1 week after Phase 2)
- Train YOLO-Classification small multi-head on Stanford Cars + the 500 labels.
- Wire into attribute service. Save predictions in metadata.json.
- UI shows attributes but with a "(beta)" tag.
- Continue labeling — active learning on uncertain crops.

### Phase 4 — v1 classifier + Telegram captions (1 week)
- After 1000+ labels, train v1. Drop "(beta)" tag.
- Add attribute lines to Telegram captions for `vehicle_idle`.
- Define + document the confidence threshold for showing vs hiding labels.

### Phase 5 — Filtering + AI queries (later)
- Filter chips in Browse
- `query_vehicles_by_attributes` AI tool
- Possibly attribute-conditioned notifications ("alert me when a black pickup is seen")

---

## 6. Open questions / decisions deferred

1. **What about face-recognizer pattern alignment?** The face-recognizer reads from `detections:pose:{cam}` and writes to `identity_state:{cam}`. Should the attribute service read from `detections:vehicle:{cam}` directly (closer to the face-rec pattern) instead of `events:{cam}`? Pro: every detection is sampled, no need to add `vehicle_sample` events to the schema. Con: pre-tracking — would need to do IoU on the attribute service side, duplicating work. *Decision: stick with the events-stream approach + new `vehicle_sample` event. Cleaner data contract.*

2. **Where does the classifier run?** Inside the per-cam attribute service, or a single shared classifier service (one container, multiple cameras)? Per-cam matches the existing service pattern but uses 1 GPU slot per camera. Shared is more efficient but more complex. *Decision: per-cam for v1, matches face-recognizer-cam{N} pattern. Revisit if it becomes a GPU bottleneck with many cams.*

3. **HD frame staleness.** HD has 5-second TTL. If sampling fires and the HD frame just expired (camera blip), the crop is missing for that sample. Acceptable to skip and try again on the next sample. *Decision: skip on miss, no retry within the same sample window. The 8-crop buffer is generous enough to absorb misses.*

4. **What about multi-cam tracking (same car seen by cam1 + cam2 in adjacent yards)?** Out of scope for this design. Each camera tracks independently. Cross-camera ReID is a separate, much harder problem.

5. **Labeling tool — keyboard shortcuts?** Yes. Number keys for color, letter keys for body, autocomplete for make. Labeling at 5 sec/crop × 1000 crops = ~80 minutes when well-designed. *Decision noted; spec it during Phase 2.*

6. **Backwards compatibility for the storage layout change.** Existing flat snapshots stay readable by the dashboard, new directory layout adds alongside. After v1 ships, a migration script can move old snapshots into best-effort per-track dirs by clustering on timestamp + bbox proximity. *Decision: dual-format support for v1; migration in v2.*

---

## 7. Acceptance criteria

Phase 1 done when:
- [ ] Tracker emits `vehicle_sample` events every 3 detection updates (verifiable via redis-cli XRANGE)
- [ ] `vehicle-attributes-cam1` service runs, consumes events, populates `/data/snapshots/vehicles/cam1/{date}/{track_id}/` with hero + angle crops + metadata.json
- [ ] Browse > Vehicle Snapshots shows grouped cards instead of flat thumbs
- [ ] No regression in existing event feed / Telegram (no attributes shown yet)

Phase 4 done when:
- [ ] Color label accuracy ≥ 85% on a held-out test set of 100 crops
- [ ] Body type accuracy ≥ 80%
- [ ] Make accuracy ≥ 60% (lower bar due to side-view difficulty)
- [ ] `vehicle_idle` Telegrams include the "Likely:" line when confidence ≥ 0.55, omit it otherwise
- [ ] No spurious labels — better to show nothing than something wrong

---

## 8. Effort estimate

| Phase | Engineering effort | Labeling effort | Calendar time |
|---|---|---|---|
| 0 — Validation | 0 days | 0 days | 1 week (passive wait) |
| 1 — Data plumbing | 4 days | 0 days | 1 week |
| 2 — Labeling tool | 1 day | 1 weekend | 1 week (parallel) |
| 3 — v0 classifier | 3 days | continues | 1 week |
| 4 — v1 + captions | 2 days | continues | 1 week |
| **Total to v1** | **~10 days engineering** | **~2 weekends labeling** | **~5 weeks calendar** |

Phase 5 (filtering / AI queries / attribute-conditioned notifications) is open-ended.

---

## 9. What this PR does NOT solve

- **Track fragmentation** — fast drive-bys still spawn multiple `vehicle_detected` events (see PR #17 known limitations). Each track gets its own attribute set, so the Browse UI will show two grouped cards for one physical drive-by car. The fix is Hungarian-style matching or Kalman prediction, both out of scope.
- **Class label flip during a track** — YOLO classifies a pickup truck as "car" early in the track, "truck" later. The tracker's mode-of-history class settles fine, but the first `vehicle_detected` event row in the feed still shows whichever class was first observed. Independent of attribute classification.
- **Cross-camera re-identification** — same car seen by cam1 + cam2 is two tracks.
- **Anything requiring license plates** — confirmed not visible at home-cam distances.
