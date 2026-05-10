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
 */

/* global _openEventPhoto */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let _browseCurrentView = "home"; // "home" | "day" | "faces"
let _browseCurrentDate = "";

// ---------------------------------------------------------------------------
// Init — called from app.js
// ---------------------------------------------------------------------------
function initBrowse() {
    _loadBrowseHome();
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
                    <div class="browse-day-card" onclick="_browseDayClick('${day.date}')">
                        <span class="browse-day-date">${day.date}</span>
                        <span class="browse-day-count">${day.count} snapshot${day.count !== 1 ? "s" : ""}</span>
                    </div>`;
            }
            html += "</div>";
        }

        // Enrolled faces section
        html += `<div class="browse-section-header" style="margin-top:12px;">👤 Enrolled Faces</div>`;
        html += `<div class="browse-faces-link"><button class="btn btn-primary browse-faces-btn" onclick="_browseFacesClick()">View Enrolled Faces Gallery</button></div>`;

        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<div class="browse-empty">Failed to load snapshots.</div>';
    }
}

// ---------------------------------------------------------------------------
// View: Day — thumbnail grid for a specific date
// ---------------------------------------------------------------------------
async function _browseDayClick(date) {
    _browseCurrentView = "day";
    _browseCurrentDate = date;
    const container = document.getElementById("browseContent");
    if (!container) return;

    container.innerHTML = '<div class="browse-loading">Loading…</div>';

    try {
        const resp = await fetch(`/api/browse/days/${date}`);
        const snapshots = await resp.json();

        let html = `<div class="browse-nav">
            <button class="browse-back-btn" onclick="_browseBackHome()">← Back</button>
            <span class="browse-nav-title">${date} — ${snapshots.length} snapshot${snapshots.length !== 1 ? "s" : ""}</span>
        </div>`;

        if (!snapshots.length) {
            html += '<div class="browse-empty">No snapshots for this day.</div>';
        } else {
            html += '<div class="browse-thumb-grid">';
            for (const snap of snapshots) {
                const label = `${snap.time} — ${snap.vehicle_class}`;
                html += `
                    <div class="browse-thumb-card" onclick="_openEventPhoto('${snap.url}', '${label.replace(/'/g, "\\'")}')">
                        <img class="browse-thumb-img" src="${snap.url}" alt="${label}" loading="lazy"
                            onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 1 1%22><rect fill=%22%23333%22 width=%221%22 height=%221%22/></svg>'">
                        <div class="browse-thumb-label">
                            <span class="browse-thumb-time">${snap.time}</span>
                            <span class="browse-thumb-class">${snap.vehicle_class}</span>
                        </div>
                    </div>`;
            }
            html += "</div>";
        }

        container.innerHTML = html;
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
            <button class="browse-back-btn" onclick="_browseBackHome()">← Back</button>
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
                            onclick="_openEventPhoto('${photoUrl}', '${name.replace(/'/g, "\\'")}')"
                            onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 1 1%22><rect fill=%22%23333%22 width=%221%22 height=%221%22/></svg>'">
                        <div class="browse-face-name">${name}</div>
                        <div class="browse-face-sightings">${angleCount} angle${angleCount !== 1 ? "s" : ""} enrolled</div>
                        ${angleCount > 1 ? `<div class="browse-face-angles">${angles.map(a =>
                    `<img class="browse-face-angle-thumb" src="${a.photo_url}" alt="${name}"
                                onclick="_openEventPhoto('${a.photo_url}', '${name.replace(/'/g, "\\'")} — angle')"
                                onerror="this.style.display='none'" loading="lazy">`
                ).join("")}</div>` : ""}
                    </div>`;
            }
            html += "</div>";
        }

        container.innerHTML = html;
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
