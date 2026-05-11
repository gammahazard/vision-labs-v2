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
// Event Feed — Poll for new events
// ---------------------------------------------------------------------------
let knownEventIds = new Set();

/**
 * Fetch recent events and render new ones in the feed.
 * Only shows events we haven't seen before (tracked by event ID).
 */
async function pollEvents() {
    try {
        if (_isAggregateFeed && Object.keys(_cameraNameCache).length === 0) {
            await _refreshCameraNameCache();
        }
        const camParam = _cameraFilter ? `&camera=${encodeURIComponent(_cameraFilter)}` : "";
        const response = await fetch(`/api/events?count=30${camParam}`);
        const data = await response.json();
        const events = data.events || [];

        // Update count
        eventCount.textContent = `${events.length} events`;

        // Find new events
        let hasNew = false;
        for (const evt of events.reverse()) {  // Oldest first for correct ordering
            if (knownEventIds.has(evt.id)) continue;
            knownEventIds.add(evt.id);
            hasNew = true;
            renderEvent(evt);
        }

        // Remove empty state message if we have events
        if (hasNew) {
            const emptyState = eventList.querySelector(".empty-state");
            if (emptyState) emptyState.remove();
        }
    } catch (err) {
        console.error("Event poll error:", err);
    }
}

/**
 * Render a single event in the event feed.
 *
 * Display fields come from the server (`evt.render`, computed by
 * event_renderer.py). This keeps the frontend and Telegram bot in
 * lockstep — new event types are wired in one Python module, not two.
 */
function renderEvent(evt) {
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
    const ph = r.photo;
    if (ph) {
        let photoUrl = "";
        const caption = (ph.caption || "Snapshot").replace(/'/g, "\\'");

        if (ph.kind === "face" && ph.identity_name && _faceIdCache[ph.identity_name]) {
            // Known person — use enrolled face photo from the cache
            const fid = _faceIdCache[ph.identity_name];
            photoUrl = `/api/faces/${fid}/photo`;
        } else if ((ph.kind === "face" || ph.kind === "event_snapshot") && ph.event_id) {
            // Fall back to the event's saved snapshot on disk
            const camQ = ph.camera_id ? `?camera=${encodeURIComponent(ph.camera_id)}` : "";
            photoUrl = `/api/events/${encodeURIComponent(ph.event_id)}/snapshot${camQ}`;
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
    item.dataset.zone = evt.zone || "";
    item.dataset.action = evt.action || "";
    item.addEventListener("click", function (e) {
        // Don't open modal if clicking the photo thumbnail (it has its own lightbox)
        if (e.target.classList.contains("event-photo")) return;
        _openEventDetail(this.dataset);
    });

    // Insert at top (newest first)
    eventList.insertBefore(item, eventList.firstChild);

    // Keep only last 50 events in DOM
    while (eventList.children.length > 50) {
        eventList.removeChild(eventList.lastChild);
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

    titleEl.textContent = `📋 ${data.eventTitle}`;

    // Build metadata display
    let metaHtml = `<div>${data.eventMeta}</div>`;
    if (data.identityName) metaHtml += `<div>🏷️ Identity: <strong>${data.identityName}</strong></div>`;
    if (data.zone) metaHtml += `<div>📍 Zone: <strong>${data.zone}</strong></div>`;
    if (data.action && data.action !== "unknown") metaHtml += `<div>🎬 Action: <strong>${data.action}</strong></div>`;
    metaEl.innerHTML = metaHtml;

    // Load snapshot
    snapshot.style.display = "none";
    snapshot.src = "";
    const snapshotUrl = `/api/events/${encodeURIComponent(data.eventId)}/snapshot`;
    const testImg = new Image();
    testImg.onload = () => {
        snapshot.src = snapshotUrl;
        snapshot.style.display = "block";
    };
    testImg.onerror = () => { snapshot.style.display = "none"; };
    testImg.src = snapshotUrl;

    // Reset name row and status
    nameRow.style.display = "none";
    statusEl.style.display = "none";
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
