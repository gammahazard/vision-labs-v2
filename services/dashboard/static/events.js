/**
 * services/dashboard/static/events.js — Event feed logic.
 *
 * PURPOSE:
 *   Polls the /api/events endpoint and renders new events in the feed panel.
 *   Tracks seen event IDs to avoid duplicates.
 *   Shows clickable photo thumbnails (face photos for known users,
 *   camera snapshots for unknowns) with a lightbox modal.
 *
 * RELATIONSHIPS:
 *   - REST: /api/events (event stream from tracker)
 *   - REST: /api/faces (face photo cache)
 *   - REST: /api/events/{id}/snapshot (camera snapshots)
 *   - HTML: #eventList, #eventCount in index.html
 */

// ---------------------------------------------------------------------------
// DOM Elements
// ---------------------------------------------------------------------------
const eventList = document.getElementById("eventList");
const eventCount = document.getElementById("eventCount");

// ---------------------------------------------------------------------------
// Camera filter (multi-camera)
// ---------------------------------------------------------------------------
// Resolution order:
//   1. ?camera=<id> URL param         → that camera (or "all" for aggregate)
//   2. window.EVENT_FEED_CAMERA       → page-level override (set by single.html
//                                       to "primary"/<id>, by index.html to "all")
//   3. fallback                       → "" (backend defaults to primary)
//
// "all" means aggregate-across-all-cameras (used by the home page).
// Anything else is treated as a specific camera id and passed verbatim.
const _cameraFilter = (() => {
    try {
        const fromUrl = new URLSearchParams(window.location.search).get("camera");
        if (fromUrl) return fromUrl;
    } catch (e) { /* SSR/edge */ }
    return window.EVENT_FEED_CAMERA || "";
})();
// Aggregate feed is when filter is empty (no scope set) OR explicitly "all".
const _isAggregateFeed = !_cameraFilter || _cameraFilter === "all";

// Camera-name cache (id -> friendly name) for badge rendering on aggregate feeds
let _cameraNameCache = {};
async function _refreshCameraNameCache() {
    try {
        const res = await fetch("/api/cameras");
        const data = await res.json();
        const cams = data.cameras || data || [];
        const out = {};
        for (const c of cams) {
            if (c && c.id) out[c.id] = c.name || c.id;
        }
        _cameraNameCache = out;
    } catch (e) { /* silent */ }
}

// ---------------------------------------------------------------------------
// Face photo cache — maps person name → face_id for inline photos
// ---------------------------------------------------------------------------
let _faceIdCache = {};  // { "Alice": 1, "Bob": 4, ... }
let _faceIdCacheTime = 0;

async function _refreshFaceIdCache() {
    const now = Date.now();
    if (now - _faceIdCacheTime < 30000 && Object.keys(_faceIdCache).length > 0) return;
    try {
        const res = await fetch("/api/faces");
        const data = await res.json();
        const faces = data.faces || [];
        _faceIdCache = {};
        for (const f of faces) {
            // Keep the first (lowest) ID per name — that's the primary enrollment
            if (!_faceIdCache[f.name]) _faceIdCache[f.name] = f.id;
        }
        _faceIdCacheTime = now;
    } catch (e) { /* silent */ }
}

// ---------------------------------------------------------------------------
// Event Feed — Poll for new events + paginate older ones
// ---------------------------------------------------------------------------
// Page sizing & pagination behavior are page-specific:
//   window.EVENT_FEED_PAGE_SIZE  → events per request (default 30)
//   window.EVENT_FEED_LOAD_MORE  → if true, show a "Load older" button and
//                                  remove the in-memory DOM cap
// Home page: 100 + load-more. Detail pages: 30, no load-more.
const _pageSize = parseInt(window.EVENT_FEED_PAGE_SIZE, 10) || 30;
const _loadMoreEnabled = window.EVENT_FEED_LOAD_MORE === true;

let knownEventIds = new Set();
let _hasMore = true;  // server tells us if older events remain
let _olderInflight = false;  // prevent overlapping "Load older" requests

// ---------------------------------------------------------------------------
// Event-type filter — groups raw event_type strings into UI categories.
// Pills above the feed let the user narrow what's visible. Pure client-side;
// doesn't change what's fetched, just hides rows that don't match.
// ---------------------------------------------------------------------------
const EVENT_FILTERS = [
    { key: "all",      label: "All",         types: null },
    { key: "faces",    label: "🔗 Faces",    types: ["face_reconciled", "face_enrolled", "person_identified"] },
    { key: "people",   label: "🚶 People",   types: ["person_appeared", "person_left", "action_changed"] },
    { key: "vehicles", label: "🚗 Vehicles", types: ["vehicle_detected", "vehicle_idle"] },
    { key: "alerts",   label: "🚨 Alerts",   types: ["unauthorized_access"] },
];
let _activeFilterKey = "all";
let _searchQuery = "";  // lower-cased; "" = no filter

function _eventMatchesFilter(eventType) {
    const f = EVENT_FILTERS.find(x => x.key === _activeFilterKey);
    if (!f || !f.types) return true;
    return f.types.includes(eventType);
}

function _eventMatchesSearch(itemEl) {
    if (!_searchQuery) return true;
    // Search across title, meta, identity, action, zone — all already cached
    // in the dataset when we rendered the row, so this is allocation-free.
    const hay = [
        itemEl.dataset.eventTitle,
        itemEl.dataset.eventMeta,
        itemEl.dataset.identityName,
        itemEl.dataset.personId,
        itemEl.dataset.action,
        itemEl.dataset.zone,
        itemEl.dataset.cameraId,
    ].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(_searchQuery);
}

function _applyFilterToDom() {
    let visible = 0;
    for (const ch of eventList.children) {
        if (!ch.classList || !ch.classList.contains("event-item")) continue;
        const match = _eventMatchesFilter(ch.dataset.eventType) && _eventMatchesSearch(ch);
        ch.style.display = match ? "" : "none";
        if (match) visible++;
    }
    if (eventCount) eventCount.textContent = `${visible} events`;
}

function _wireEventSearch() {
    const input = document.getElementById("eventSearchInput");
    if (!input || input.dataset.wired === "1") return;
    input.dataset.wired = "1";
    // Debounce — re-applying the filter on every keystroke is fine at 100s
    // of events but we still avoid hammering layout while typing fast.
    let timer = null;
    input.addEventListener("input", () => {
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => {
            _searchQuery = (input.value || "").trim().toLowerCase();
            _applyFilterToDom();
        }, 120);
    });
}

function _setActiveFilter(key) {
    _activeFilterKey = key;
    const bar = document.getElementById("eventFilterBar");
    if (bar) {
        for (const btn of bar.querySelectorAll(".event-filter-pill")) {
            const active = btn.dataset.filterKey === key;
            btn.classList.toggle("active", active);
            btn.setAttribute("aria-selected", active ? "true" : "false");
        }
    }
    _applyFilterToDom();
}

function _renderEventFilterBar() {
    const bar = document.getElementById("eventFilterBar");
    if (!bar || bar.dataset.rendered === "1") return;
    bar.innerHTML = EVENT_FILTERS.map(f => `
        <button type="button" role="tab" class="event-filter-pill${f.key === _activeFilterKey ? " active" : ""}"
                data-filter-key="${f.key}"
                aria-selected="${f.key === _activeFilterKey}"
                style="padding:5px 10px;font-size:12px;font-weight:500;border-radius:14px;cursor:pointer;
                       border:1px solid rgba(96,165,250,0.35);
                       background:${f.key === _activeFilterKey ? "rgba(96,165,250,0.25)" : "rgba(96,165,250,0.06)"};
                       color:${f.key === _activeFilterKey ? "#bfdbfe" : "#94a3b8"};
                       transition:all 0.15s ease;
                       -webkit-tap-highlight-color:transparent;">${f.label}</button>
    `).join("");
    bar.dataset.rendered = "1";
    bar.addEventListener("click", (e) => {
        const btn = e.target.closest(".event-filter-pill");
        if (btn) _setActiveFilter(btn.dataset.filterKey);
    });
}

/**
 * Fetch recent events and render new ones in the feed.
 * Only shows events we haven't seen before (tracked by event ID).
 */
async function pollEvents() {
    try {
        _renderEventFilterBar();
        _wireEventSearch();
        if (_isAggregateFeed && Object.keys(_cameraNameCache).length === 0) {
            await _refreshCameraNameCache();
        }
        const camParam = _cameraFilter ? `&camera=${encodeURIComponent(_cameraFilter)}` : "";
        const response = await fetch(`/api/events?count=${_pageSize}${camParam}`);
        const data = await response.json();
        const events = data.events || [];

        // Find new events; render in chronological order (oldest first), so
        // insertBefore(firstChild) leaves the newest event at the very top.
        let hasNew = false;
        for (const evt of events.slice().reverse()) {
            if (knownEventIds.has(evt.id)) continue;
            knownEventIds.add(evt.id);
            hasNew = true;
            renderEvent(evt, /*append=*/false);
        }

        if (hasNew) {
            const emptyState = eventList.querySelector(".empty-state");
            if (emptyState) emptyState.remove();
        }

        // Always check the load-older button on every poll, not just when
        // there are new events. The first poll on a stale browser session
        // can otherwise leave the button hidden if the previous session's
        // state machine got into a weird place.
        _showLoadOlderBtnIfReady();

        // Re-apply the filter (also sets the visible count). Done after
        // render so newly-inserted items get hidden if they don't match.
        _applyFilterToDom();
    } catch (err) {
        console.error("Event poll error:", err);
    }
}

/** Count of actual event rows in the DOM (excludes empty-state placeholder). */
function _domEventCount() {
    let n = 0;
    for (const ch of eventList.children) {
        if (ch.classList && ch.classList.contains("event-item")) n++;
    }
    return n;
}

/**
 * Fetch the next page of OLDER events using the bottom-most event's id as
 * a cursor. Appends results below the current list. The server falls
 * through to the JSONL journal on disk once the in-memory Redis stream
 * is exhausted, so this can keep paginating into deep history.
 */
async function loadOlderEvents() {
    if (!_loadMoreEnabled || !_hasMore || _olderInflight) return;
    const btn = document.getElementById("loadOlderBtn");
    const oldestId = _bottomEventId();
    if (!oldestId) return;

    _olderInflight = true;
    if (btn) { btn.disabled = true; btn.textContent = "Loading older events…"; }

    try {
        const camParam = _cameraFilter ? `&camera=${encodeURIComponent(_cameraFilter)}` : "";
        const url = `/api/events?count=${_pageSize}&before=${encodeURIComponent(oldestId)}${camParam}`;
        const res = await fetch(url);
        const data = await res.json();
        const events = data.events || [];

        // Server returns newest-first; appendChild preserves that order at the
        // bottom of the list (older below older).
        let added = 0;
        for (const evt of events) {
            if (knownEventIds.has(evt.id)) continue;
            knownEventIds.add(evt.id);
            renderEvent(evt, /*append=*/true);
            added++;
        }

        if (data.has_more === false || events.length < _pageSize) _hasMore = false;
        // Re-apply the active filter so newly-loaded items respect it.
        _applyFilterToDom();
    } catch (err) {
        console.error("Load older error:", err);
    } finally {
        _olderInflight = false;
        if (btn) {
            btn.disabled = !_hasMore;
            btn.textContent = _hasMore ? `↓ Load older ${_pageSize} events` : "No more events";
            if (!_hasMore) btn.classList.add("exhausted");
        }
    }
}

/**
 * Fallback for older face_reconciled events that don't carry the per-row
 * promoted_face_ids list. Fetches the full /api/faces gallery, filters to
 * rows matching `personName`, and renders them in the same grid as the
 * absorbed-faces view. No similarity scores (we don't have them for older
 * events) — just the photos.
 */
async function _loadFallbackEnrolledAngles(personName, gridEl, statsEl) {
    if (!gridEl) return;
    try {
        const res = await fetch("/api/faces");
        const data = await res.json();
        const all = data.faces || [];
        const matches = all.filter(f => (f.name || "").toLowerCase() === (personName || "").toLowerCase());
        if (matches.length === 0) {
            gridEl.innerHTML = `<div style="grid-column:1/-1;color:#64748b;font-size:12px;text-align:center;padding:12px;">${personName} no longer has enrolled angles.</div>`;
            if (statsEl) statsEl.textContent = "";
            return;
        }
        if (statsEl) statsEl.textContent = `${matches.length} enrolled angle(s)`;

        const INITIAL = 12;
        const tile = (f) => {
            const photoUrl = `/api/faces/${f.id}/photo`;
            const caption = `${personName} · enrolled`.replace(/'/g, "\\'");
            return `
                <div class="event-face-tile" style="display:flex;flex-direction:column;align-items:stretch;gap:3px;">
                    <img src="${photoUrl}" alt="${caption}"
                        style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px;background:#111;cursor:pointer;border:1px solid #2d3748;"
                        onclick="_openEventPhoto('${photoUrl}', '${caption}')"
                        onerror="this.style.opacity='0.3';this.style.background='#1a2235';">
                </div>`;
        };

        gridEl.innerHTML = matches.slice(0, INITIAL).map(tile).join("");
        const remaining = matches.slice(INITIAL);
        if (remaining.length > 0) {
            const moreBtn = document.createElement("button");
            moreBtn.type = "button";
            moreBtn.textContent = `Show ${remaining.length} more →`;
            moreBtn.style.cssText = "grid-column:1/-1;padding:8px 12px;margin-top:4px;border-radius:6px;border:1px dashed rgba(96,165,250,0.4);background:rgba(96,165,250,0.08);color:#60a5fa;font-size:12px;font-weight:500;cursor:pointer;font-family:inherit;";
            moreBtn.onclick = () => {
                moreBtn.remove();
                const extra = document.createElement("div");
                extra.style.cssText = "display:contents;";
                extra.innerHTML = remaining.map(tile).join("");
                gridEl.appendChild(extra);
            };
            gridEl.appendChild(moreBtn);
        }
        gridEl.style.gridTemplateColumns = "repeat(auto-fill, minmax(64px, 1fr))";
    } catch (err) {
        gridEl.innerHTML = `<div style="grid-column:1/-1;color:#94a3b8;font-size:12px;text-align:center;padding:12px;">Couldn't load enrolled angles (${err.message || "network error"}).</div>`;
        if (statsEl) statsEl.textContent = "";
    }
}

function _bottomEventId() {
    const items = eventList.querySelectorAll(".event-item");
    if (items.length === 0) return null;
    return items[items.length - 1].dataset.eventId || null;
}

function _showLoadOlderBtnIfReady() {
    if (!_loadMoreEnabled) return;
    const btn = document.getElementById("loadOlderBtn");
    if (btn && _domEventCount() > 0 && _hasMore) {
        btn.style.display = "";
    }
}

/**
 * Render a single event in the event feed.
 *
 * Display fields come from the server (`evt.render`, computed by
 * event_renderer.py). This keeps the frontend and Telegram bot in
 * lockstep — new event types are wired in one Python module, not two.
 */
function renderEvent(evt, append = false) {
    const item = document.createElement("div");
    const r = evt.render || {};
    const icon = r.icon || "📌";
    const title = r.title || evt.event_type || "Event";
    const meta = r.subtitle || "";
    const cssExtra = r.css_classes || "";

    item.className = `event-item ${cssExtra}`.trim();

    // -----------------------------------------------------------------------
    // Photo thumbnail (the server tells us which kind to use)
    // -----------------------------------------------------------------------
    let photoHtml = "";
    let isFaceEvent = false;
    const ph = r.photo;
    if (ph) {
        let photoUrl = "";
        const caption = (ph.caption || "Snapshot").replace(/'/g, "\\'");

        if (ph.kind === "face" && ph.identity_name && _faceIdCache[ph.identity_name]) {
            // Known person — use enrolled face photo from the cache
            const fid = _faceIdCache[ph.identity_name];
            photoUrl = `/api/faces/${fid}/photo`;
            isFaceEvent = true;
        } else if ((ph.kind === "face" || ph.kind === "event_snapshot") && ph.event_id) {
            // Fall back to the event's saved snapshot on disk
            const camQ = ph.camera_id ? `?camera=${encodeURIComponent(ph.camera_id)}` : "";
            photoUrl = `/api/events/${encodeURIComponent(ph.event_id)}/snapshot${camQ}`;
            if (ph.kind === "face") isFaceEvent = true;
        } else if (ph.kind === "vehicle" && ph.snapshot_key) {
            photoUrl = `/api/vehicles/snapshot/${encodeURIComponent(ph.snapshot_key)}`;
        }

        if (photoUrl) {
            photoHtml = `<img class="event-photo" src="${photoUrl}" alt="${caption}" onclick="_openEventPhoto('${photoUrl}', '${caption}')" onerror="this.style.display='none'">`;
        }
    }

    // AI scene analysis (if available)
    let aiHtml = "";
    if (evt.ai_description) {
        const desc = evt.ai_description.replace(/</g, "&lt;").replace(/>/g, "&gt;");
        aiHtml = `<div class="event-ai-desc" style="font-size:11px; color:#a78bfa; margin-top:4px; font-style:italic; line-height:1.4; opacity:0.9;">🤖 ${desc}</div>`;
    }

    // Camera badge — only shown on aggregate feeds, to distinguish events
    // from different cameras at a glance. Hidden on per-camera pages.
    let cameraBadgeHtml = "";
    if (_isAggregateFeed && evt.camera_id) {
        const camName = _cameraNameCache[evt.camera_id] || evt.camera_id;
        cameraBadgeHtml = `<span class="event-camera-badge" title="${evt.camera_id}" style="display:inline-block;font-size:10px;font-weight:600;background:rgba(96,165,250,0.18);color:#60a5fa;padding:2px 6px;border-radius:8px;margin-right:6px;letter-spacing:0.02em;">📷 ${camName}</span>`;
    }

    item.innerHTML = `
        <span class="event-icon">${icon}</span>
        ${photoHtml}
        <div class="event-content">
            <div class="event-title">${cameraBadgeHtml}${title}</div>
            <div class="event-meta">${meta}</div>
            ${aiHtml}
        </div>
    `;

    // Make event item clickable to show detail modal
    item.style.cursor = "pointer";
    item.dataset.eventId = evt.id;
    item.dataset.eventTitle = title;
    item.dataset.eventMeta = meta;
    item.dataset.eventType = evt.event_type;
    item.dataset.identityName = evt.identity_name || "";
    item.dataset.personId = evt.person_id || "";
    item.dataset.zone = evt.zone || "";
    item.dataset.action = evt.action || "";
    item.dataset.cameraId = evt.camera_id || "";
    item.dataset.timestamp = evt.timestamp || "";
    item.dataset.duration = evt.duration || "";
    item.dataset.direction = evt.direction || "";
    // Vehicle-specific fields — surface in the detail modal for vehicle_*.
    item.dataset.vehicleClass = evt.vehicle_class || "";
    item.dataset.vehicleConfidence = evt.vehicle_confidence || "";
    // face_reconciled detail payload — JSON-encoded arrays from the server.
    // Picked up by _openEventDetail to render the absorbed-thumbnails grid.
    if (evt.promoted_face_ids) item.dataset.promotedFaceIds = evt.promoted_face_ids;
    if (evt.similarities) item.dataset.similarities = evt.similarities;
    if (evt.count) item.dataset.count = evt.count;
    item.addEventListener("click", function (e) {
        // Don't open modal if clicking the photo thumbnail (it has its own lightbox)
        if (e.target.classList.contains("event-photo")) return;
        _openEventDetail(this.dataset);
    });

    // Place the new item: top for live updates, bottom for "Load older" pages
    if (append) {
        eventList.appendChild(item);
    } else {
        eventList.insertBefore(item, eventList.firstChild);
    }

    // DOM cap: only enforce on pages without "Load older" (detail pages).
    // On the home page we keep everything loaded so the user can scroll back
    // through the full paginated history.
    if (!_loadMoreEnabled) {
        while (_domEventCount() > _pageSize) {
            eventList.removeChild(eventList.lastChild);
        }
    }

    // Refresh face cache in background for future events
    if (isFaceEvent) _refreshFaceIdCache();
}

// ---------------------------------------------------------------------------
// Event Detail Modal
// ---------------------------------------------------------------------------
let _eventDetailId = null;

function _openEventDetail(data) {
    _eventDetailId = data.eventId;
    const modal = document.getElementById("eventDetailModal");
    const titleEl = document.getElementById("eventDetailTitle");
    const metaEl = document.getElementById("eventDetailMeta");
    const snapshot = document.getElementById("eventDetailSnapshot");
    const nameRow = document.getElementById("eventDetailNameRow");
    const statusEl = document.getElementById("eventDetailStatus");
    const facesWrap = document.getElementById("eventDetailFacesWrap");
    const facesGrid = document.getElementById("eventDetailFacesGrid");
    const facesHeading = document.getElementById("eventDetailFacesHeading");
    const facesStats = document.getElementById("eventDetailFacesStats");

    titleEl.textContent = `📋 ${data.eventTitle}`;

    const isFaceMatched = data.eventType === "face_reconciled";
    const isVehicle = data.eventType === "vehicle_detected" || data.eventType === "vehicle_idle";

    // Build metadata display — same base for all events, with vehicle/face
    // extras conditionally appended.
    let metaHtml = `<div>${data.eventMeta}</div>`;
    if (data.identityName) metaHtml += `<div>🏷️ Identity: <strong>${data.identityName}</strong></div>`;
    if (isFaceMatched && !data.identityName && data.personId) {
        // face_reconciled events stash the matched name in person_id.
        metaHtml += `<div>🏷️ Person: <strong>${data.personId}</strong></div>`;
    }
    if (data.cameraId) {
        // Resolve the raw camera id (e.g. "cam2") to the friendly display
        // name (e.g. "basement") via the same cache that drives the row
        // badge. Falls back to the id if the cache hasn't populated yet
        // or this camera isn't in the registry.
        const camLabel = _cameraNameCache[data.cameraId] || data.cameraId;
        const idHint = camLabel !== data.cameraId
            ? ` <span style="color:#64748b;font-weight:normal;font-size:11px;">(${data.cameraId})</span>`
            : "";
        metaHtml += `<div>📷 Camera: <strong>${camLabel}</strong>${idHint}</div>`;
    }
    if (data.zone) metaHtml += `<div>📍 Zone: <strong>${data.zone}</strong></div>`;
    if (data.action && data.action !== "unknown") metaHtml += `<div>🎬 Action: <strong>${data.action}</strong></div>`;
    if (data.direction) metaHtml += `<div>🧭 Direction: <strong>${data.direction}</strong></div>`;
    if (isVehicle) {
        if (data.vehicleClass) metaHtml += `<div>🚗 Type: <strong>${data.vehicleClass}</strong></div>`;
        if (data.vehicleConfidence) {
            const confNum = parseFloat(data.vehicleConfidence);
            const confTxt = isNaN(confNum) ? data.vehicleConfidence : `${(confNum * 100).toFixed(0)}%`;
            metaHtml += `<div>📊 Confidence: <strong>${confTxt}</strong></div>`;
        }
        if (data.duration && data.duration !== "0") {
            metaHtml += `<div>⏱️ Duration: <strong>${data.duration}s</strong></div>`;
        }
    }
    metaEl.innerHTML = metaHtml;

    // ----- face_reconciled: render absorbed-faces grid -----
    if (facesWrap) facesWrap.style.display = "none";
    if (facesGrid) facesGrid.innerHTML = "";
    if (isFaceMatched && facesWrap && facesGrid) {
        let faceIds = [];
        let sims = [];
        try { if (data.promotedFaceIds) faceIds = JSON.parse(data.promotedFaceIds); } catch (_) { }
        try { if (data.similarities) sims = JSON.parse(data.similarities); } catch (_) { }
        const personName = data.identityName || data.personId || "match";

        if (faceIds.length === 0) {
            // Older event (pre-enrichment). We don't know which specific
            // pictures this event absorbed — per-row detail wasn't logged
            // until recently. Try to recover the count from the action
            // text ("Absorbed 3 unknown(s)…") and explain explicitly that
            // we can only show the FULL enrolled set, with the absorbed
            // ones mixed in.
            let absorbedCount = data.count ? parseInt(data.count, 10) : NaN;
            if (isNaN(absorbedCount) && data.action) {
                const m = String(data.action).match(/(\d+)\s+unknown/i);
                if (m) absorbedCount = parseInt(m[1], 10);
            }
            facesHeading.textContent = `${personName} — older event, exact pictures not recorded`;
            facesStats.textContent = !isNaN(absorbedCount)
                ? `This event absorbed ${absorbedCount} unknown(s); they're now part of the set below.`
                : "The absorbed pictures are now part of the set below.";
            facesGrid.innerHTML = '<div style="grid-column:1/-1;color:#64748b;font-size:12px;text-align:center;padding:12px;">Loading all enrolled angles…</div>';
            facesWrap.style.display = "block";
            _loadFallbackEnrolledAngles(personName, facesGrid, facesStats);
        } else {
            // Heading makes it explicit these ARE the originally-unknown
            // pictures that got reconciled — the JPEGs are the same bytes
            // that were in unknown_faces before promotion, just relocated
            // to known_faces under the matched person's name.
            facesHeading.textContent = `Unknown captures reconciled to ${personName}`;
            const avg = sims.length ? (sims.reduce((a, b) => a + b, 0) / sims.length) : null;
            facesStats.textContent = avg !== null
                ? `${faceIds.length} unknown(s) absorbed · avg sim ${avg.toFixed(2)}`
                : `${faceIds.length} unknown(s) absorbed`;

            // Render: highest-similarity first, capped at INITIAL so the
            // modal doesn't blow up when a sweep absorbed 30+ angles. A
            // tap on "+N more" reveals the rest, keeping the default view
            // compact — phone screens especially benefit.
            const INITIAL = 12;
            const pairs = faceIds.map((fid, i) => ({ fid, sim: sims[i] }));
            // Stable sort by similarity desc (NaN/undefined at the end)
            pairs.sort((a, b) => (b.sim ?? -1) - (a.sim ?? -1));

            const tile = (p) => {
                const simText = (typeof p.sim === "number") ? p.sim.toFixed(2) : "—";
                const photoUrl = `/api/faces/${p.fid}/photo`;
                const caption = `${personName} · sim ${simText}`.replace(/'/g, "\\'");
                return `
                    <div class="event-face-tile" style="display:flex;flex-direction:column;align-items:stretch;gap:3px;">
                        <img src="${photoUrl}" alt="${caption}"
                            style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px;background:#111;cursor:pointer;border:1px solid #2d3748;"
                            onclick="_openEventPhoto('${photoUrl}', '${caption}')"
                            onerror="this.style.opacity='0.3';this.style.background='#1a2235';">
                        <div style="font-size:10px;color:#94a3b8;text-align:center;font-variant-numeric:tabular-nums;">sim ${simText}</div>
                    </div>`;
            };

            const initial = pairs.slice(0, INITIAL).map(tile).join("");
            const remaining = pairs.slice(INITIAL);
            facesGrid.innerHTML = initial;

            if (remaining.length > 0) {
                // "Show all" sentinel — clicking expands without rebuilding
                const moreBtn = document.createElement("button");
                moreBtn.type = "button";
                moreBtn.textContent = `Show ${remaining.length} more →`;
                moreBtn.style.cssText = "grid-column:1/-1;padding:8px 12px;margin-top:4px;border-radius:6px;border:1px dashed rgba(96,165,250,0.4);background:rgba(96,165,250,0.08);color:#60a5fa;font-size:12px;font-weight:500;cursor:pointer;font-family:inherit;";
                moreBtn.onclick = () => {
                    moreBtn.remove();
                    const extra = document.createElement("div");
                    extra.style.cssText = "display:contents;";
                    extra.innerHTML = remaining.map(tile).join("");
                    facesGrid.appendChild(extra);
                };
                facesGrid.appendChild(moreBtn);
            }

            // Tighter columns on smaller modals so more tiles fit per row
            // without each one becoming a postage stamp. minmax(64px,...)
            // gives ~5 per row on a 350px-wide modal.
            facesGrid.style.gridTemplateColumns = "repeat(auto-fill, minmax(64px, 1fr))";
            facesWrap.style.display = "block";
        }
    }

    // Load snapshot — face_reconciled events have no snapshot so we skip the
    // fetch entirely to avoid a 404 hit on every click.
    snapshot.style.display = "none";
    snapshot.src = "";
    if (!isFaceMatched) {
        const snapshotUrl = `/api/events/${encodeURIComponent(data.eventId)}/snapshot`;
        const testImg = new Image();
        testImg.onload = () => {
            snapshot.src = snapshotUrl;
            snapshot.style.display = "block";
        };
        testImg.onerror = () => { snapshot.style.display = "none"; };
        testImg.src = snapshotUrl;
    }

    // Reset name row + status (only Close button remains in actions)
    if (nameRow) nameRow.style.display = "none";
    if (statusEl) statusEl.style.display = "none";
    const nameInput = document.getElementById("eventDetailNameInput");
    if (nameInput) nameInput.value = "";

    modal.style.display = "flex";

    // Fetch AI scene analysis for this event
    const aiEl = document.getElementById("eventDetailAI");
    if (aiEl) {
        aiEl.style.display = "none";
        aiEl.textContent = "";
        fetch(`/api/events/${encodeURIComponent(data.eventId)}/analysis`)
            .then(r => r.ok ? r.json() : null)
            .then(d => {
                if (d && d.description) {
                    aiEl.textContent = `🤖 ${d.description}`;
                    aiEl.style.display = "block";
                }
            })
            .catch(() => { });
    }
}

function closeEventDetailModal() {
    const modal = document.getElementById("eventDetailModal");
    if (modal) modal.style.display = "none";
    _eventDetailId = null;
}

async function eventDetailVerdict(verdict) {
    if (!_eventDetailId) return;
    const statusEl = document.getElementById("eventDetailStatus");
    statusEl.textContent = `✅ Marked as ${verdict.replace("_", " ")}`;
    statusEl.style.color = "#4ade80";
    statusEl.style.display = "block";
}

function eventDetailNamePrompt() {
    const nameRow = document.getElementById("eventDetailNameRow");
    nameRow.style.display = "block";
    const input = document.getElementById("eventDetailNameInput");
    setTimeout(() => input.focus(), 100);
}

async function eventDetailSubmitName() {
    if (!_eventDetailId) return;
    const input = document.getElementById("eventDetailNameInput");
    const name = input.value.trim();
    if (!name) return;
    const statusEl = document.getElementById("eventDetailStatus");
    statusEl.textContent = `✅ Named as "${name}"`;
    statusEl.style.color = "#4ade80";
    document.getElementById("eventDetailNameRow").style.display = "none";
    statusEl.style.display = "block";
}

// ---------------------------------------------------------------------------
// Photo Lightbox Modal
// ---------------------------------------------------------------------------
let _photoModal = null;

function _openEventPhoto(url, name) {
    if (!_photoModal) {
        _photoModal = document.createElement("div");
        _photoModal.className = "event-photo-modal";
        _photoModal.innerHTML = `
            <div class="event-photo-backdrop" onclick="_closeEventPhoto()"></div>
            <div class="event-photo-content">
                <img id="eventPhotoModalImg" alt="Face">
                <div class="event-photo-caption" id="eventPhotoModalCaption"></div>
            </div>
        `;
        document.body.appendChild(_photoModal);
    }
    document.getElementById("eventPhotoModalImg").src = url;
    document.getElementById("eventPhotoModalCaption").textContent = name;
    _photoModal.style.display = "flex";
}

function _closeEventPhoto() {
    if (_photoModal) _photoModal.style.display = "none";
}

// Pre-populate face cache when events module loads
_refreshFaceIdCache();
