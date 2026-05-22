/**
 * browse.js — Snapshot browser + enrolled faces gallery.
 *
 * PURPOSE:
 *   Browse vehicle detection snapshots organized by day, and view
 *   enrolled face photos in a unified gallery. Loaded as a modular
 *   script alongside events.js, faces.js, etc.
 *
 * PUBLIC API:
 *   initBrowse()  — called by app.js on page load
 *
 * CLICK BINDING:
 *   The HTML this script builds is run through `_safeHtml()` (DOMPurify) and
 *   DOMPurify strips inline event handlers (`onclick`, `onerror`, …) by
 *   design — they're a classic XSS vector. So instead of `onclick="..."`
 *   attributes, every clickable element carries a `data-action` tag and the
 *   delegated listener below dispatches based on `data-action` of the
 *   closest ancestor. Adding a new clickable thing = give it a `data-action`
 *   and one `case` in `_handleBrowseClick`.
 */

/* global _openEventPhoto */

// `_safeHtml` + `_PURIFY_CFG` are defined in js/lib/safe-html.js — see that
// file for the rationale. Same identifiers, single declaration site.

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let _browseCurrentView = "home"; // "home" | "day" | "faces"
let _browseCurrentDate = "";
let _browseListenerBound = false;

// ---------------------------------------------------------------------------
// Init — called from app.js
// ---------------------------------------------------------------------------
function initBrowse() {
    _bindBrowseClickListener();
    _loadBrowseHome();
}

// Returns an HTML snippet showing non-null color/body/make/model with a
// (beta) tag. Returns '' when attrs is null/undefined or all fields blank.
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

// Delegated click handler on #browseContent. Bound once. Looks at the
// closest [data-action] ancestor of the click target and dispatches.
function _bindBrowseClickListener() {
    if (_browseListenerBound) return;
    const container = document.getElementById("browseContent");
    if (!container) return;
    container.addEventListener("click", _handleBrowseClick);
    _browseListenerBound = true;
}

function _handleBrowseClick(ev) {
    const target = ev.target.closest("[data-action]");
    if (!target) return;
    const action = target.getAttribute("data-action");
    switch (action) {
        case "day":
            _browseDayClick(target.getAttribute("data-date"));
            break;
        case "faces":
            _browseFacesClick();
            break;
        case "back":
            _browseBackHome();
            break;
        case "open":
            _openEventPhoto(
                target.getAttribute("data-url"),
                target.getAttribute("data-label") || "",
            );
            break;
        case "open-crops-modal":
            _openCropsModal(target.getAttribute("data-date"));
            break;
        case "close-crops-modal":
            _closeCropsModal();
            break;
        default:
            // Unknown action — ignore.
            break;
    }
}

// ---------------------------------------------------------------------------
// View: Home — show day folders + link to enrolled faces
// ---------------------------------------------------------------------------
async function _loadBrowseHome() {
    _browseCurrentView = "home";
    _browseCurrentDate = "";
    const container = document.getElementById("browseContent");
    if (!container) return;

    container.innerHTML = '<div class="browse-loading">Loading…</div>';

    try {
        const resp = await fetch("/api/browse/days");
        const days = await resp.json();

        let html = "";

        // Enrolled faces link card
        html += `<div class="browse-section-header">📂 Vehicle Snapshots</div>`;

        if (!days.length) {
            html += '<div class="browse-empty">No vehicle snapshots yet — detections will appear here automatically.</div>';
        } else {
            html += '<div class="browse-day-grid">';
            for (const day of days) {
                html += `
                    <div class="browse-day-card" data-action="day" data-date="${day.date}" role="button" tabindex="0">
                        <span class="browse-day-date">${day.date}</span>
                        <span class="browse-day-count">${day.count} snapshot${day.count !== 1 ? "s" : ""}</span>
                    </div>`;
            }
            html += "</div>";
        }

        // Enrolled faces section
        html += `<div class="browse-section-header" style="margin-top:12px;">👤 Enrolled Faces</div>`;
        html += `<div class="browse-faces-link"><button class="btn btn-primary browse-faces-btn" data-action="faces">View Enrolled Faces Gallery</button></div>`;

        container.innerHTML = _safeHtml(html);
    } catch (e) {
        container.innerHTML = '<div class="browse-empty">Failed to load snapshots.</div>';
    }
}

// ---------------------------------------------------------------------------
// View: Day — flat snapshot grid + a single "Vehicle crops taken" button
//
// Phase 1 of vehicle-attributes writes per-track dirs alongside the legacy
// flat snapshots. We surface those crops via a MODAL (triggered by the
// button at the top of the day view) rather than mixing them into the
// main grid — the user wants the day view to feel like the old simple
// "all the snapshots from today" page, with the per-track stuff as a
// dedicated view-when-you-want-it overlay.
// ---------------------------------------------------------------------------

async function _browseDayClick(date) {
    _browseCurrentView = "day";
    _browseCurrentDate = date;
    const container = document.getElementById("browseContent");
    if (!container) return;

    container.innerHTML = '<div class="browse-loading">Loading…</div>';

    try {
        // Fetch flat snapshots + per-track count (used to gate the modal button)
        const [snapshots, tracks] = await Promise.all([
            fetch(`/api/browse/days/${date}`).then(r => r.json()),
            fetch(`/api/browse/tracks/${date}`).then(r => r.ok ? r.json() : []),
        ]);
        const trackCount = Array.isArray(tracks) ? tracks.length : 0;

        let html = `<div class="browse-nav">
            <button class="browse-back-btn" data-action="back">← Back</button>
            <span class="browse-nav-title">${date} — ${snapshots.length} snapshot${snapshots.length !== 1 ? "s" : ""}</span>
        </div>`;

        // Vehicle crops modal trigger — only when there's something to show
        if (trackCount > 0) {
            html += `<div class="crops-modal-trigger-row">
                <button type="button" class="crops-modal-trigger"
                        data-action="open-crops-modal" data-date="${date}">
                    📸 Vehicle crops taken (${trackCount})
                </button>
            </div>`;
        }

        if (!snapshots.length) {
            html += '<div class="browse-empty">No snapshots for this day.</div>';
        } else {
            html += '<div class="browse-thumb-grid">';
            for (const snap of snapshots) {
                const label = `${snap.time} — ${snap.vehicle_class}`;
                html += `
                    <div class="browse-thumb-card" data-action="open" data-url="${snap.url}" data-label="${label}" role="button" tabindex="0">
                        <img class="browse-thumb-img" src="${snap.url}" alt="${label}" loading="lazy">
                        <div class="browse-thumb-label">
                            <span class="browse-thumb-time">${snap.time}</span>
                            <span class="browse-thumb-class">${snap.vehicle_class}</span>
                        </div>
                    </div>`;
            }
            html += "</div>";
        }

        container.innerHTML = _safeHtml(html);
    } catch (e) {
        container.innerHTML = '<div class="browse-empty">Failed to load day snapshots.</div>';
    }
}

// ---------------------------------------------------------------------------
// Vehicle crops modal — grouped-by-track thumbnail overlay
//
// Opened by `data-action="open-crops-modal"`. Re-fetches /api/browse/tracks
// (cheap; same call we already used to get the count) and renders one
// section per track with all that track's angle thumbnails. Each thumbnail
// uses the existing `data-action="open"` plumbing so clicking opens the
// existing fullscreen photo viewer — no new viewer code needed.
//
// Phase 3 (when the classifier is enabled): the per-track section header
// will gain predicted attributes (color/body/make/model) rendered from
// metadata.json.attributes. The data is already in `t.attributes` from
// the API; we just don't render it yet because v0 ships with the
// classifier disabled.
// ---------------------------------------------------------------------------

function _closeCropsModal() {
    const m = document.getElementById("cropsModal");
    if (m) m.remove();
}

async function _openCropsModal(date) {
    _closeCropsModal();  // idempotent — avoid stacking

    const container = document.getElementById("browseContent");
    if (!container) return;

    // Stub modal while we fetch
    const stub = document.createElement("div");
    stub.id = "cropsModal";
    stub.className = "crops-modal-backdrop";
    stub.innerHTML = _safeHtml(`<div class="crops-modal-panel">
        <div class="crops-modal-loading">Loading…</div>
    </div>`);
    // Click outside the panel closes the modal — backdrop catches the click
    stub.addEventListener("click", (ev) => {
        if (ev.target === stub) _closeCropsModal();
    });
    container.appendChild(stub);

    let tracks = [];
    try {
        const res = await fetch(`/api/browse/tracks/${encodeURIComponent(date)}`);
        if (res.ok) tracks = await res.json();
    } catch (_) {
        // fall through with empty tracks
    }

    if (!Array.isArray(tracks) || !tracks.length) {
        stub.innerHTML = _safeHtml(`<div class="crops-modal-panel">
            <div class="crops-modal-header">
                <span>Vehicle crops — ${date}</span>
                <button type="button" class="crops-modal-close" data-action="close-crops-modal">×</button>
            </div>
            <div class="crops-modal-empty">No vehicle crops captured for this day.</div>
        </div>`);
        return;
    }

    let totalCrops = 0;
    const sections = tracks.map(t => {
        const allUrls = [t.hero_url, ...t.angle_urls];
        totalCrops += allUrls.length;
        const thumbs = allUrls.map((url, idx) => {
            const label = `${t.track_id} ${idx === 0 ? "hero" : "angle " + idx}`;
            return `<img class="crops-modal-thumb"
                src="${url}"
                alt="${label}"
                loading="lazy"
                data-action="open"
                data-url="${url}"
                data-label="${label}"
                role="button"
                tabindex="0">`;
        }).join("");

        const attrLine = _formatAttrs(t.attributes);
        return `<section class="crops-modal-track">
            <header class="crops-modal-track-header">
                <span class="crops-modal-track-id">${t.track_id}</span>
                <span class="crops-modal-track-meta">
                    ${t.vehicle_class} · ${t.event_kind} · ${t.time}
                    · ${t.duration_seconds.toFixed(1)}s
                    · ${allUrls.length} crop${allUrls.length === 1 ? "" : "s"}
                </span>
            </header>
            ${attrLine ? `<div class="track-attrs">${attrLine}</div>` : ""}
            <div class="crops-modal-thumb-grid">${thumbs}</div>
        </section>`;
    }).join("");

    stub.innerHTML = _safeHtml(`<div class="crops-modal-panel">
        <div class="crops-modal-header">
            <span>Vehicle crops — ${date} (${tracks.length} track${tracks.length === 1 ? "" : "s"}, ${totalCrops} crop${totalCrops === 1 ? "" : "s"})</span>
            <button type="button" class="crops-modal-close" data-action="close-crops-modal">×</button>
        </div>
        <div class="crops-modal-body">
            ${sections}
        </div>
    </div>`);
}

// ---------------------------------------------------------------------------
// View: Enrolled Faces Gallery
// ---------------------------------------------------------------------------
async function _browseFacesClick() {
    _browseCurrentView = "faces";
    const container = document.getElementById("browseContent");
    if (!container) return;

    container.innerHTML = '<div class="browse-loading">Loading…</div>';

    try {
        const resp = await fetch("/api/browse/faces");
        const faces = await resp.json();

        let html = `<div class="browse-nav">
            <button class="browse-back-btn" data-action="back">← Back</button>
            <span class="browse-nav-title">Enrolled Faces — ${Array.isArray(faces) ? faces.length : 0} ${Array.isArray(faces) && faces.length === 1 ? "person" : "people"}</span>
        </div>`;

        if (!Array.isArray(faces) || !faces.length) {
            html += '<div class="browse-empty">No faces enrolled yet — use the Known Faces panel to enroll.</div>';
        } else {
            html += '<div class="browse-face-grid">';
            for (const person of faces) {
                const name = person.name || "Unknown";
                const photoUrl = person.photo_url || "";
                const angles = person.angles || [];
                const angleCount = angles.length;
                html += `
                    <div class="browse-face-card">
                        <img class="browse-face-img" src="${photoUrl}" alt="${name}" loading="lazy"
                            data-action="open" data-url="${photoUrl}" data-label="${name}" role="button" tabindex="0">
                        <div class="browse-face-name">${name}</div>
                        <div class="browse-face-sightings">${angleCount} angle${angleCount !== 1 ? "s" : ""} enrolled</div>
                        ${angleCount > 1 ? `<div class="browse-face-angles">${angles.map(a =>
                    `<img class="browse-face-angle-thumb" src="${a.photo_url}" alt="${name}"
                                data-action="open" data-url="${a.photo_url}" data-label="${name} — angle" loading="lazy">`
                ).join("")}</div>` : ""}
                    </div>`;
            }
            html += "</div>";
        }

        container.innerHTML = _safeHtml(html);
    } catch (e) {
        container.innerHTML = '<div class="browse-empty">Failed to load enrolled faces.</div>';
    }
}

// ---------------------------------------------------------------------------
// Navigation helpers
// ---------------------------------------------------------------------------
function _browseBackHome() {
    _loadBrowseHome();
}

// ---------------------------------------------------------------------------
// Auto-refresh — poll every 30s while the browse panel is open
// ---------------------------------------------------------------------------
setInterval(() => {
    const panel = document.getElementById("browsePanel");
    if (!panel || panel.classList.contains("collapsed")) return;

    // Skip refresh while the vehicle-crops modal is open — the modal is
    // mounted inside #browseContent, so a re-render of the day view would
    // nuke the modal element and the user would see the modal close
    // itself every 30 seconds.
    if (document.getElementById("cropsModal")) return;

    // Refresh whichever view is currently active
    if (_browseCurrentView === "home") {
        _loadBrowseHome();
    } else if (_browseCurrentView === "day" && _browseCurrentDate) {
        _browseDayClick(_browseCurrentDate);
    }
    // Don't auto-refresh faces gallery (rarely changes)
}, 30000);

// Also refresh immediately when the panel is opened (uncollapsed)
document.addEventListener("DOMContentLoaded", () => {
    const panel = document.getElementById("browsePanel");
    if (!panel) return;
    const observer = new MutationObserver(() => {
        if (!panel.classList.contains("collapsed")) {
            // Panel just opened — refresh immediately. Skip if the
            // vehicle-crops modal is mounted (same reason as the
            // setInterval guard above).
            if (document.getElementById("cropsModal")) return;
            if (_browseCurrentView === "home") {
                _loadBrowseHome();
            } else if (_browseCurrentView === "day" && _browseCurrentDate) {
                _browseDayClick(_browseCurrentDate);
            }
        }
    });
    observer.observe(panel, { attributes: true, attributeFilter: ["class"] });
});
