# Vision Labs Audit — Complete

Started 2026-05-17 evening, finished 2026-05-18. All 11 components audited end-to-end; Tier 1 fixes shipped on each. This file is now a record, not a queue.

---

## Components audited (all ✅)

| # | Component | Day | Tier 1 fixes shipped |
|---|---|---|---|
| 1 | Face recognizer | 1 | Cleanups (module Redis client, log demotion), gender+age detection, manual override, backfill 76/83 |
| 2 | Pose detector + action classifier | 1 | Sitting/crouch fix, scaled thresholds, partial-keypoint, docstring drift |
| 3 | Vehicle detector + tracker vehicle | 1 | idle re-alert, vehicle_left event, bicycle class, class mode-update, scaled stationary, detection_frame TTL |
| 4 | Person tracker | 1 | Identity flip protection, single-event flow, action cooldown, direction smoothing, dead-zone identity, num_people filter |
| 5 | Frame pipeline (ingester/recorder/ws) | 1 | Retention strptime guard, stderr DEVNULL, **HOTFIX recorder REDIS_HOST crash loop** |
| 6 | Dashboard backend | 2 | event_id traversal regex, rec-cache LRU+async ffmpeg, metrics cursor seed, unknowns camera_id thread-through, faces URL-encode, deleted clips.py orphan, ollama warmup async, config validation, zones PUT re-validate, DEFAULT_CONFIG unified, cameras.py validation |
| 7 | Orchestrator | 2 | Reconcile mutex, Redis sentinel (don't tear-down-all), HW_PROBE_IMAGE → CUDA 12.8, SIGTERM handler, RTSP cred scrub, audit maxlen 2000, request_id echo |
| 8 | Setup wizard | 2 | Probe timeout 60→150s, registry retry, _write_state EBUSY fallback, POSE/VEHICLE_MODEL regex, tighter setup-gate, gpuMode dead-code |
| 9 | AI assistant | 2 | Chat → asyncio.to_thread + 60s timeout, ContextVar request_id, capture_snapshot per-camera STATE_KEY, VISION_MODEL empty-check, browse_vehicles date regex, send_telegram rate limit, schedule_reminder cap, get_system_status redaction, build_system_context cache |
| 10 | Telegram pipeline | 2 | Per-camera cooldown keys, HTML escape captions, concurrent broadcast, 429 retry-after, _save_telegram_media subdir fix, unauthorized log scrub, env-admin opt-in, photo 8MB cap, /timelapse listdir guard |
| 11 | Prometheus + Grafana | 2 | Bind to 127.0.0.1, storage size cap, fixed misleading comment, dashboard auto-refresh 15s, added vehicle_left/idle/action_changed/vehicles panels, **node-exporter container + Host row in Grafana (disk/CPU/memory)** |

### Bonus work (not in original queue)
- ✅ **Pi mediamtx tuning** — `-preset veryfast -crf 28` eliminated 5× bitrate variance; ~12 % Pi CPU
- ✅ **Pi ONVIF responder** — custom Python service at `/home/raj/onvif_responder.py`, advertises as "Logitech G-Series Webcam"; URL-decode fix added to our scanner
- ✅ **Containers tab on monitoring** — new `routes/containers.py` + orchestrator publishes `orchestrator:containers` snapshot every reconcile (60 s TTL); read-only listing with "↗ Open Portainer" link (Portainer blocks iframes via CSP)
- ✅ **Mobile defensive CSS** — `overflow-wrap`, sidebar/panel shrink, narrow-phone media query
- ✅ **Docker image cleanup** — pruned ~67 GB (legacy unsuffixed images, ComfyUI orphan, cam3 leftovers, build cache)
- ✅ **Stale doc / comment sweep** — cameras.py / cameras.js / cameras.html Phase 7b refs corrected; constants.py ComfyUI ref removed; CONTEXT.md updated with node-exporter + containers route + Portainer-no-iframe note

---

## Deferred items from individual audits

Things we identified as theoretical or low-impact and consciously didn't fix. Pick up if a related symptom appears.

### Face recognizer
- Near-miss suppression at cosine 0.27 (face_db.py:258) — low impact in practice
- Frame drift on face crop (XREVRANGE count=1 vs frame_number lookup) — negligible without backlog
- Person bbox upward expansion before face crop — uncertain net benefit
- det_score two-tier gate (0.5 vs 0.75) — calibration question
- Vectorize match() — only matters past ~200 angles
- TTL on identity_state:{cam} — stale-name guard if recognizer crashes

### Pose detector
- Frame share between pose + face-recognizer (both decode same JPEG)
- Batch xreadgroup with count=10 — only matters under burst
- Extract shared YOLO loop into `services/_shared/` (~150 LOC dedup)
- TTL on detection_frame:pose (vehicle already has it)

### Vehicle
- Drop embedded frame_bytes in stream — saves ~50 MB Redis/busy cam
- Vehicle direction estimation (persons have it)
- Stationary check: 5-sample minimum → seconds-based

### Person tracker
- Re-association on re-entry (ghost buffer) — eliminates "Alice left / Alice appeared" pairs
- Hungarian/ByteTrack assignment — only matters with multiple people close together
- Same-second snapshot-key collision — mostly redundant after single-event flow

### Frame pipeline
- "Stream stale" UI signal when ingester dies
- HD_TARGET_FPS hot-reload (sub-stream reloads, HD doesn't)
- Audio stripping in recorder (`-an`)
- Dashboard WebSocket frame sharing across connections
- Recorder mid-life registry re-read

### Dashboard backend
- Auth weakness: 4-char min password, no rate limit, default admin/admin — important if exposing beyond LAN
- Cookie `secure=False` — wrong if behind TLS
- /api/vehicles/snapshot/{key:path} trusts any Redis key
- get_system_status returns full config hash (RTSP creds risk now mitigated by redaction in get_system_status of AI tools — but the routes endpoint still does)
- node_exporter / cadvisor / face match metrics / notification failure counter — optional further coverage

### Orchestrator
- CONFIG_APPLY_ALLOWED_SERVICES half-dead under profile-gated layout — needs a thoughtful rework
- Probe single-flight lock — rare in practice
- `compose up` 180 s timeout — may bite on first-boot image pulls; observe during reinstall

### Setup wizard
- localStorage wizard persistence — UX nice-to-have
- "Reset wizard" button — would save `docker exec` for testers
- Dual-GPU recommendation review (architecture call; CUDA 12.8 across the board now)

### AI assistant
- Tool result truncation when > 8 KB (context bloat)
- ai.db connection pooling
- reminder polling indexes (sent, trigger_time)
- /data/snapshots/clips/ cleanup poller — currently unbounded
- StreamingResponse refactor for /chat
- bump num_ctx to 16K+ if real chat conversations start hitting context limits

### Telegram pipeline
- Verdict-button feature — docstrings reference it; either wire up or remove doc claim
- Camera-prefix ambiguity silent fallback to primary
- Offset disk persistence (survives Redis outage)

### Prometheus / Grafana
- Grafana binding to 127.0.0.1 + dashboard proxy — would require building a Grafana proxy in the dashboard
- cadvisor for container restart / OOM tracking
- face-recognizer match-rate counter
- recorder segment-write counter
- vl_gpu_pause_active panel — metric exists, no panel currently uses it (defined-but-unused)

---

## Pre-reinstall checklist (when ready)

- [x] All 11 components audited
- [x] All Tier 1 fixes shipped + verified
- [x] All known regressions on this branch caught (recorder REDIS_HOST hotfix)
- [ ] Commit the changes (suggested groupings below)
- [ ] **Backup** before wipe: `bash scripts/backup.sh` + `mv vl-backup-*.tar.gz ~/`
- [ ] Verify backup contains face-data + auth-data + redis-data + qnap-*
- [ ] Optionally: dry-run install on a different host first (user will test in person)
- [ ] Wipe: `docker compose --profile cam1 --profile cam2 down -v && docker rmi $(docker images "vision-labs*" -q) && docker image prune -a -f`
- [ ] Reinstall via `scripts/install-linux.sh`
- [ ] Restore: `bash scripts/restore.sh ~/vl-backup-*.tar.gz`
- [ ] Add cam1 + cam2 via the wizard (Pi auto-discoverable now via ONVIF responder)
- [ ] Verify gender chips, single-event flow, vehicle_left events, action labels
- [ ] Verify monitoring tab shows Grafana + Containers + Host row data

## Suggested commit groupings (clean history)

1. `audit: face recognizer + gender/age detection`
2. `audit: action classifier rewrite (sitting/crouch + scaled thresholds + partial keypoints)`
3. `audit: vehicle tracker fixes (idle reset, vehicle_left, bicycle, class mode)`
4. `audit: person tracker fixes (identity flip protection, single-event flow, action cooldown)`
5. `audit: frame pipeline fixes + recorder REDIS_HOST hotfix`
6. `ONVIF: scope URL-decode + Pi onvif_responder + mediamtx CRF tuning`
7. `audit: dashboard backend fixes (path traversal, rec-cache eviction, metrics cursor, validation)`
8. `audit: orchestrator hardening (mutex, sentinel, CUDA 12.8, SIGTERM, cred scrub)`
9. `audit: setup wizard timeout + validation`
10. `audit: AI assistant hardening (to_thread, ContextVar, rate limits, redaction)`
11. `audit: Telegram pipeline (per-cam cooldown, HTML escape, parallel broadcast, 429)`
12. `monitoring: localhost bind, storage cap, node-exporter, containers tab`
13. `docs: AUDIT_QUEUE final + CONTEXT updates + stale comment cleanup`

---

End of audit. Reinstall whenever you're ready.
