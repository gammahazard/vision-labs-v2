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
        const response = await fetch("/api/events?count=30");
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
 */
function renderEvent(evt) {
    const item = document.createElement("div");
    const isAppeared = evt.event_type === "person_appeared";
    const isLeft = evt.event_type === "person_left";
    const isActionChanged = evt.event_type === "action_changed";
    const isFaceEvent = evt.event_type === "face_reconciled" || evt.event_type === "face_enrolled" || evt.event_type === "person_identified";
    const isVehicle = evt.event_type === "vehicle_detected";
    const isVehicleIdle = evt.event_type === "vehicle_idle";
    const isUnauthorized = evt.event_type === "unauthorized_access";
    const isAlert = evt.alert_triggered === "True" || evt.alert_triggered === "true";

    item.className = `event-item ${isAppeared ? "appeared" : ""} ${isLeft ? "left" : ""} ${isAlert || isUnauthorized ? "alert" : ""} ${isFaceEvent || isActionChanged ? "appeared" : ""} ${isVehicle ? "appeared" : ""} ${isVehicleIdle ? "alert" : ""}`;

    // Format timestamp
    const ts = parseFloat(evt.timestamp);
    const time = new Date(ts * 1000).toLocaleTimeString("en-US", {
        timeZone: "America/Toronto",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
    });

    let icon, title, meta;
    // Show identity name (e.g. "Alice") when available, fall back to person_id
    const displayName = evt.identity_name || evt.person_id;
    const idSuffix = evt.identity_name ? ` (${evt.person_id})` : "";

    if (isUnauthorized) {
        icon = "🔒";
        const tgUser = evt.telegram_username ? `@${evt.telegram_username}` : `ID:${evt.telegram_user_id || "?"}`;
        const attemptedCmd = evt.action || "unknown";
        title = `Unauthorized Access — ${displayName || tgUser}`;
        meta = `${time} · ${tgUser} tried ${attemptedCmd} · 🚨 Blocked`;
    } else if (isVehicle || isVehicleIdle) {
        // Vehicle event — show vehicle-specific icon and info
        const vClass = evt.vehicle_class || "vehicle";
        const vConf = evt.vehicle_confidence ? parseFloat(evt.vehicle_confidence) : 0;
        const vehicleIcons = { car: "🚗", truck: "🚛", motorcycle: "🏍️", bus: "🚌" };
        icon = isVehicleIdle ? "🚨" : (vehicleIcons[vClass] || "🚗");
        if (isVehicleIdle) {
            title = `Vehicle Idling — ${vClass.charAt(0).toUpperCase() + vClass.slice(1)}`;
            meta = `${time} · ⏱️ ${evt.duration}s · ${(vConf * 100).toFixed(0)}% confidence${evt.zone ? ` · 📍${evt.zone}` : ""} · 🚨 Alert`;
        } else {
            title = `Vehicle Detected — ${vClass.charAt(0).toUpperCase() + vClass.slice(1)}`;
            meta = `${time} · ${(vConf * 100).toFixed(0)}% confidence${evt.zone ? ` · 📍${evt.zone}` : ""}${isAlert ? " · 🚨 Alert" : ""}`;
        }
    } else if (evt.event_type === "person_identified") {
        icon = "👤";
        title = `Person Identified — ${displayName}`;
        meta = `${time} · ${evt.person_id} recognized as ${evt.identity_name}${evt.action && evt.action !== "unknown" ? ` · ${evt.action}` : ""}`;
    } else if (evt.event_type === "face_reconciled") {
        icon = "🔗";
        title = `Face Matched — ${displayName}`;
        meta = `${time} · ${evt.action || "Cleared unknown"}`;
    } else if (evt.event_type === "face_enrolled") {
        icon = "✅";
        title = `Face Enrolled — ${displayName}`;
        meta = `${time} · ${evt.action || "New enrollment"}`;
    } else if (isActionChanged) {
        icon = "🔄";
        title = `Action Changed — ${displayName}`;
        meta = `${time} · ${evt.prev_action || "?"} → ${evt.action}${evt.zone ? ` · 📍${evt.zone}` : ""}`;
    } else {
        icon = isAlert ? "🚨" : (isAppeared ? "🟢" : "🟡");
        title = `${isAppeared ? "Person Appeared" : "Person Left"} — ${displayName}`;
        meta = `${time} · ${evt.duration}s · ${evt.direction}${evt.action && evt.action !== "unknown" ? ` · ${evt.action}` : ""}${evt.zone ? ` · 📍${evt.zone}` : ""}${isAlert ? " · 🚨 Alert" : ""}`;
    }

    // -----------------------------------------------------------------------
    // Build photo thumbnail HTML
    // -----------------------------------------------------------------------
    let photoHtml = "";
    const identityName = evt.identity_name || (evt.event_type === "face_enrolled" ? evt.person_id : null);

    if ((isVehicle || isVehicleIdle) && evt.snapshot_key) {
        // Vehicle snapshot from Redis
        const snapshotUrl = `/api/vehicles/snapshot/${encodeURIComponent(evt.snapshot_key)}`;
        const caption = `Vehicle — ${evt.vehicle_class || "unknown"}`;
        photoHtml = `<img class="event-photo" src="${snapshotUrl}" alt="${caption}" onclick="_openEventPhoto('${snapshotUrl}', '${caption.replace(/'/g, "\\'")}')"
            onerror="this.style.display='none'">`;
    } else if (identityName && _faceIdCache[identityName]) {
        // Known person — use enrolled face photo
        const fid = _faceIdCache[identityName];
        const photoUrl = `/api/faces/${fid}/photo`;
        photoHtml = `<img class="event-photo" src="${photoUrl}" alt="${identityName}" onclick="_openEventPhoto('${photoUrl}', '${identityName.replace(/'/g, "\\'")}')"
            onerror="this.style.display='none'">`;
    } else if (isAppeared || isLeft) {
        // Unknown person or person_left — use camera snapshot
        const snapshotUrl = `/api/events/${encodeURIComponent(evt.id)}/snapshot`;
        const caption = displayName || "Camera snapshot";
        photoHtml = `<img class="event-photo" src="${snapshotUrl}" alt="${caption}" onclick="_openEventPhoto('${snapshotUrl}', '${caption.replace(/'/g, "\\'")}')"
            onerror="this.style.display='none'">`;
    }

    // AI scene analysis (if available)
    let aiHtml = "";
    if (evt.ai_description) {
        const desc = evt.ai_description.replace(/</g, "&lt;").replace(/>/g, "&gt;");
        aiHtml = `<div class="event-ai-desc" style="font-size:11px; color:#a78bfa; margin-top:4px; font-style:italic; line-height:1.4; opacity:0.9;">🤖 ${desc}</div>`;
    }

    item.innerHTML = `
        <span class="event-icon">${icon}</span>
        ${photoHtml}
        <div class="event-content">
            <div class="event-title">${title}</div>
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
