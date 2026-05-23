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

// ---------------------------------------------------------------------------
// Phase 4 labeling — UI for the per-track label form.
//
// The label-classes JSON (color/body/make/model dropdown contents) is
// session-cached after the first GET — small payload, never changes
// mid-session in normal use.
// ---------------------------------------------------------------------------

let _labelClassesCache = null;

async function _fetchLabelClasses() {
    if (_labelClassesCache) return _labelClassesCache;
    try {
        const r = await fetch("/api/browse/label-classes");
        if (r.ok) _labelClassesCache = await r.json();
    } catch (_) { /* fall through with cache=null */ }
    if (!_labelClassesCache) {
        _labelClassesCache = {
            colors: [], body_types: [], makes: [], models: [], make_to_models: {},
        };
    }
    return _labelClassesCache;
}

// Escape user-supplied / model-supplied strings before interpolating into
// HTML attribute values. The wrap-with-_safeHtml at innerHTML assignment
// time is a second line of defense; this prevents broken attribute
// boundaries (e.g. a label containing a literal `"` mid-attribute).
function _escapeAttr(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

// HTML snippet shown for the labeling section of a single track row in
// the cropsModal. Three states:
//   1. No user_labels   → "📝 Label this track" button
//   2. user_labels.skipped → "⊘ Skipped (reason)" + "Re-label" button
//   3. Other user_labels  → "✓ Labeled: <values>" + "Edit" button
// The expanded form (state-4) replaces this snippet when the button is
// clicked — see _renderLabelForm.
function _renderLabelSection(t) {
    const ul = t.user_labels || {};
    const has = ul.color || ul.body_type || ul.make || ul.model || ul.skipped;
    const tDate = _escapeAttr(t.date || "");
    const tCam = _escapeAttr(t.camera || "");
    const tDir = _escapeAttr(t.dir_id || "");
    if (!has) {
        return `<div class="track-label-row" data-track-key="${tDate}/${tCam}/${tDir}">
            <button type="button" class="track-label-btn"
                data-action="label-open"
                data-date="${tDate}" data-camera="${tCam}" data-track-dir="${tDir}">
                📝 Label this track
            </button>
        </div>`;
    }
    let summary;
    if (ul.skipped) {
        const reason = _escapeAttr(ul.skip_reason || "no reason given");
        summary = `<span class="track-label-summary track-label-skipped">⊘ Skipped — ${reason}</span>`;
    } else {
        const parts = [ul.color, ul.body_type, ul.make, ul.model]
            .filter(Boolean).map(_escapeAttr).join(" · ");
        summary = `<span class="track-label-summary">✓ Labeled: ${parts}</span>`;
    }
    return `<div class="track-label-row" data-track-key="${tDate}/${tCam}/${tDir}">
        ${summary}
        <button type="button" class="track-label-btn track-label-edit"
            data-action="label-open"
            data-date="${tDate}" data-camera="${tCam}" data-track-dir="${tDir}">
            Edit
        </button>
    </div>`;
}

// HTML for the inline form expanded when the user clicks the Label button.
// Wired by the modal's local click delegate (see _openCropsModal).
// IR tracks: color dropdown is rendered disabled with a hint, because the
// vehicle was captured monochrome and any color the user picks here would
// be a guess. Server-side allows it anyway (the user might recognize the
// vehicle from past daytime appearances).
function _renderLabelForm(t, classes) {
    const ul = t.user_labels || {};
    const ir = !!(t.attributes && t.attributes.ir_track);
    const tDate = _escapeAttr(t.date || "");
    const tCam = _escapeAttr(t.camera || "");
    const tDir = _escapeAttr(t.dir_id || "");

    const colorOpt = (c) => `<option value="${_escapeAttr(c)}" ${ul.color === c ? "selected" : ""}>${_escapeAttr(c)}</option>`;
    const bodyOpt = (b) => `<option value="${_escapeAttr(b)}" ${ul.body_type === b ? "selected" : ""}>${_escapeAttr(b)}</option>`;
    const makeOpt = (m) => `<option value="${_escapeAttr(m)}"></option>`;
    const modelOpt = (m) => `<option value="${_escapeAttr(m)}"></option>`;

    return `<div class="track-label-form" data-track-key="${tDate}/${tCam}/${tDir}">
        <div class="track-label-hint">
            Fill what you can — blank fields stay unset. Use Skip only when the crop is unusable (blur, occluded, not a vehicle).
        </div>
        <div class="track-label-grid">
            <label>Color:</label>
            <select class="track-label-color" ${ir ? "disabled title='IR-mode track — color not visible to camera'" : ""}>
                <option value="">— pick —</option>
                ${(classes.colors || []).map(colorOpt).join("")}
            </select>
            <label>Body:</label>
            <select class="track-label-body">
                <option value="">— pick —</option>
                ${(classes.body_types || []).map(bodyOpt).join("")}
            </select>
            <label>Make:</label>
            <input type="text" class="track-label-make" list="track-label-makes-${tDir}"
                value="${_escapeAttr(ul.make || "")}" placeholder="e.g. Toyota">
            <datalist id="track-label-makes-${tDir}">
                ${(classes.makes || []).map(makeOpt).join("")}
            </datalist>
            <label>Model:</label>
            <input type="text" class="track-label-model" list="track-label-models-${tDir}"
                value="${_escapeAttr(ul.model || "")}" placeholder="e.g. Sienna 2018">
            <datalist id="track-label-models-${tDir}">
                ${(classes.models || []).map(modelOpt).join("")}
            </datalist>
        </div>
        <div class="track-label-actions">
            <button type="button" class="track-label-action-save"
                data-action="label-save"
                data-date="${tDate}" data-camera="${tCam}" data-track-dir="${tDir}">Save</button>
            <button type="button" class="track-label-action-skip"
                data-action="label-skip"
                data-date="${tDate}" data-camera="${tCam}" data-track-dir="${tDir}">Skip — can't tell</button>
            <button type="button" class="track-label-action-cancel"
                data-action="label-cancel"
                data-date="${tDate}" data-camera="${tCam}" data-track-dir="${tDir}">Cancel</button>
        </div>
        <div class="track-label-error" hidden></div>
    </div>`;
}

// POST to the label endpoint; updates the section in place on success.
async function _saveTrackLabel(date, camera, trackDir, payload, section) {
    const url = `/api/browse/label/${encodeURIComponent(date)}/${encodeURIComponent(camera)}/${encodeURIComponent(trackDir)}`;
    let r;
    try {
        r = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
    } catch (e) {
        _showLabelError(section, `Network error: ${e.message || e}`);
        return null;
    }
    if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try {
            const body = await r.json();
            if (body.error) msg = body.error;
        } catch (_) { /* ignore */ }
        _showLabelError(section, msg);
        return null;
    }
    const data = await r.json();
    return data.user_labels || null;
}

function _showLabelError(section, msg) {
    const errEl = section.querySelector(".track-label-error");
    if (errEl) {
        errEl.textContent = msg;
        errEl.hidden = false;
    }
}

// Replace the section's label-row / label-form with the rendered summary
// reflecting the saved user_labels. Track object's user_labels is mutated
// in-place so subsequent Edit clicks see the latest state.
function _replaceLabelArea(section, track, userLabels) {
    track.user_labels = userLabels || {};
    const row = section.querySelector(".track-label-row, .track-label-form");
    if (!row) return;
    const replacement = document.createElement("div");
    replacement.innerHTML = _safeHtml(_renderLabelSection(track));
    const newRow = replacement.firstElementChild;
    if (newRow) row.replaceWith(newRow);
}

// Click-dispatch for label-{open,save,skip,cancel} actions. Called by
// _openCropsModal's stub-local listener. `target` is the [data-action]
// element clicked; `tracks` is the closure-scoped array of tracks for
// this modal session (mutated in-place when a label is saved).
async function _handleLabelAction(action, target, tracks) {
    const date = target.getAttribute("data-date") || "";
    const camera = target.getAttribute("data-camera") || "";
    const trackDir = target.getAttribute("data-track-dir") || "";
    const trackKey = `${date}/${camera}/${trackDir}`;
    const section = target.closest(".crops-modal-track");
    if (!section) return;
    // tracks is closure-captured by reference; find the matching entry so
    // we can mutate user_labels in place + re-render from it.
    const track = (tracks || []).find(
        (t) => `${t.date}/${t.camera}/${t.dir_id}` === trackKey,
    );
    if (!track) return;

    if (action === "label-open") {
        const classes = await _fetchLabelClasses();
        const row = section.querySelector(".track-label-row, .track-label-form");
        if (!row) return;
        const replacement = document.createElement("div");
        replacement.innerHTML = _safeHtml(_renderLabelForm(track, classes));
        const newForm = replacement.firstElementChild;
        if (newForm) row.replaceWith(newForm);
        return;
    }
    if (action === "label-cancel") {
        // Re-render as summary using the latest user_labels (no save).
        _replaceLabelArea(section, track, track.user_labels || {});
        return;
    }
    if (action === "label-skip") {
        const form = section.querySelector(".track-label-form");
        const reasonEl = form ? form.querySelector(".track-label-skip-reason") : null;
        const ul = await _saveTrackLabel(date, camera, trackDir,
            { skipped: true, skip_reason: reasonEl ? reasonEl.value : "" },
            section,
        );
        if (ul) _replaceLabelArea(section, track, ul);
        return;
    }
    if (action === "label-save") {
        const form = section.querySelector(".track-label-form");
        if (!form) return;
        const colorEl = form.querySelector(".track-label-color");
        const bodyEl = form.querySelector(".track-label-body");
        const makeEl = form.querySelector(".track-label-make");
        const modelEl = form.querySelector(".track-label-model");
        const payload = {
            color: (colorEl && !colorEl.disabled) ? colorEl.value : "",
            body_type: bodyEl ? bodyEl.value : "",
            make: makeEl ? makeEl.value.trim() : "",
            model: modelEl ? modelEl.value.trim() : "",
            skipped: false,
        };
        // Reject empty client-side too — the server returns 400 but a
        // fast feedback loop is nicer than a roundtrip for an obvious miss.
        if (!payload.color && !payload.body_type && !payload.make && !payload.model) {
            _showLabelError(section, "Set at least one field, or click Skip.");
            return;
        }
        const ul = await _saveTrackLabel(date, camera, trackDir, payload, section);
        if (ul) _replaceLabelArea(section, track, ul);
        return;
    }
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

// Body-scroll lock helpers. iOS Safari ignores `overflow: hidden` on body,
// so we also pin it with `position: fixed; top: -<scrollY>` and restore the
// scroll offset on close. Without this, the page underneath scrolls when the
// user drags inside the modal's backdrop region (and on iOS the modal's
// `position: fixed` panel can visually drift during that page scroll).
function _lockBodyForCropsModal() {
    if (document.body.dataset.cropsModalLockedY !== undefined) return;
    const y = window.scrollY;
    document.body.dataset.cropsModalLockedY = String(y);
    document.body.style.position = 'fixed';
    document.body.style.top = `-${y}px`;
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.width = '100%';
}

function _unlockBodyForCropsModal() {
    if (document.body.dataset.cropsModalLockedY === undefined) return;
    const y = parseInt(document.body.dataset.cropsModalLockedY || '0', 10);
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.width = '';
    delete document.body.dataset.cropsModalLockedY;
    window.scrollTo(0, y);
}

function _closeCropsModal() {
    const m = document.getElementById("cropsModal");
    if (m) m.remove();
    _unlockBodyForCropsModal();
}

async function _openCropsModal(date) {
    _closeCropsModal();  // idempotent — avoid stacking

    // Mount the modal under <body> rather than the (scrollable) panel-body's
    // #browseContent. The backdrop is `position: fixed`, which is supposed
    // to anchor to the viewport regardless of ancestors, but iOS Safari is
    // unreliable about that when the modal sits inside an `overflow: auto`
    // container that's been scrolled — the panel can visually drift during
    // momentum scrolls, and the "top cut off when opening" symptom comes
    // from the modal rendering relative to the scrolled #browseContent
    // instead of the viewport. Body-mounting eliminates that whole class.
    // Click events on the modal are now handled by a stub-local listener
    // because the delegated #browseContent handler can't see body children.
    _lockBodyForCropsModal();

    // Stub modal while we fetch
    const stub = document.createElement("div");
    stub.id = "cropsModal";
    stub.className = "crops-modal-backdrop";
    stub.innerHTML = _safeHtml(`<div class="crops-modal-panel">
        <div class="crops-modal-loading">Loading…</div>
    </div>`);
    let tracks = [];
    // Click outside the panel closes the modal — backdrop catches the click
    stub.addEventListener("click", async (ev) => {
        if (ev.target === stub) {
            _closeCropsModal();
            return;
        }
        // The X button + thumbnails use data-action attrs that the
        // delegated #browseContent handler would have caught — since the
        // modal is now in body, dispatch them locally. Phase 4 added
        // label-* actions for the per-track labeling form.
        const target = ev.target.closest("[data-action]");
        if (!target) return;
        const action = target.getAttribute("data-action");
        if (action === "close-crops-modal") {
            _closeCropsModal();
            return;
        }
        if (action === "open") {
            _openEventPhoto(
                target.getAttribute("data-url"),
                target.getAttribute("data-label") || "",
            );
            return;
        }
        if (action === "label-open" || action === "label-save"
            || action === "label-skip" || action === "label-cancel") {
            await _handleLabelAction(action, target, tracks);
        }
    });
    document.body.appendChild(stub);
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
        const labelSection = _renderLabelSection(t);
        return `<section class="crops-modal-track" data-track-key="${_escapeAttr((t.date || '') + '/' + (t.camera || '') + '/' + (t.dir_id || ''))}">
            <header class="crops-modal-track-header">
                <span class="crops-modal-track-id">${t.track_id}</span>
                <span class="crops-modal-track-meta">
                    ${t.vehicle_class} · ${t.event_kind} · ${t.time}
                    · ${t.duration_seconds.toFixed(1)}s
                    · ${allUrls.length} crop${allUrls.length === 1 ? "" : "s"}
                </span>
            </header>
            ${attrLine ? `<div class="track-attrs">${attrLine}</div>` : ""}
            ${labelSection}
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
