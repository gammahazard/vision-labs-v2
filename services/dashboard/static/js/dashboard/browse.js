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
// View: Day — per-track grouped cards (above) + flat thumbnail grid
// ---------------------------------------------------------------------------

// Fetch /api/browse/tracks/{date} and return an HTML string for the grouped-card
// section, or '' if there are no tracks. Sanitization happens at the call site
// (same convention as the flat-grid renderer below).
async function _renderDayTracks(date, camera) {
    const url = `/api/browse/tracks/${encodeURIComponent(date)}`
                + (camera ? `?camera=${encodeURIComponent(camera)}` : '');
    try {
        const res = await fetch(url);
        if (!res.ok) return '';
        const tracks = await res.json();
        if (!Array.isArray(tracks) || tracks.length === 0) return '';

        const cards = tracks.map(t => `
            <div class="track-card" data-track-id="${t.track_id}">
                <div class="track-hero">
                    <img src="${t.hero_url}" alt="${t.vehicle_class} hero" loading="lazy">
                    <span class="track-class-pill">${t.vehicle_class}</span>
                </div>
                <div class="track-meta">
                    <div class="track-time">${t.time}
                        <span class="track-event">${t.event_kind}</span>
                    </div>
                    <div class="track-stats">
                        ${t.voting_samples} angle${t.voting_samples === 1 ? '' : 's'}
                        · ${t.duration_seconds.toFixed(1)}s
                    </div>
                </div>
                <div class="track-angles">
                    ${t.angle_urls.map(u => `
                        <img src="${u}" class="track-angle" loading="lazy" alt="angle">
                    `).join('')}
                </div>
            </div>
        `).join('');

        return `<section class="track-cards-section">
            <h3 style="margin:0.75rem 0 0.5rem;font-size:0.9rem;color:#94a3b8;">
                Per-track view (${tracks.length} track${tracks.length === 1 ? '' : 's'})
            </h3>
            <div class="track-cards-grid">${cards}</div>
        </section>`;
    } catch (_) {
        return '';
    }
}

async function _browseDayClick(date) {
    _browseCurrentView = "day";
    _browseCurrentDate = date;
    const container = document.getElementById("browseContent");
    if (!container) return;

    container.innerHTML = '<div class="browse-loading">Loading…</div>';

    try {
        // Fetch tracks and flat snapshots in parallel
        const [tracksHtml, snapshots] = await Promise.all([
            _renderDayTracks(date, null),
            fetch(`/api/browse/days/${date}`).then(r => r.json()),
        ]);

        let html = `<div class="browse-nav">
            <button class="browse-back-btn" data-action="back">← Back</button>
            <span class="browse-nav-title">${date} — ${snapshots.length} snapshot${snapshots.length !== 1 ? "s" : ""}</span>
        </div>`;

        // Grouped-card section comes first (empty string when no tracks → no section)
        html += tracksHtml;

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
            // Panel just opened — refresh immediately
            if (_browseCurrentView === "home") {
                _loadBrowseHome();
            } else if (_browseCurrentView === "day" && _browseCurrentDate) {
                _browseDayClick(_browseCurrentDate);
            }
        }
    });
    observer.observe(panel, { attributes: true, attributeFilter: ["class"] });
});
