/**
 * services/dashboard/static/faces.js — Face enrollment with multi-angle wizard.
 *
 * PURPOSE:
 *   Handles face enrollment via a guided 5-angle wizard (front, left, right,
 *   up, down). Each angle calls /api/faces/enroll with the same name, storing
 *   multiple embeddings for better recognition accuracy.
 *
 * RELATIONSHIPS:
 *   - REST: /api/faces, /api/faces/preview, /api/faces/enroll
 *   - HTML: #facesPanel, #faceGallery, #enrollWizard in index.html
 */

// ---------------------------------------------------------------------------
// DOM Elements (lazy — avoids load-time null errors)
// ---------------------------------------------------------------------------
function _el(id) { return document.getElementById(id); }
const _faceEls = {
    get gallery() { return _el("faceGallery"); },
    get count() { return _el("faceCount"); },
    get nameInput() { return _el("enrollName"); },
    get btn() { return _el("enrollBtn"); },
};

// ---------------------------------------------------------------------------
// Wizard State
// ---------------------------------------------------------------------------
const WIZARD_ANGLES = [
    { label: "Front", prompt: "Look straight at the camera", icon: "👤" },
    { label: "Left", prompt: "Turn your head slightly right", icon: "👈" },
    { label: "Right", prompt: "Turn your head slightly left", icon: "👉" },
    { label: "Up", prompt: "Tilt your head slightly up", icon: "👆" },
    { label: "Down", prompt: "Tilt your head slightly down", icon: "👇" },
];

let _wizardName = "";
let _wizardStep = 0;
let _wizardCaptured = 0;
let _wizardActive = false;

// ---------------------------------------------------------------------------
// Start the Enrollment Wizard
// ---------------------------------------------------------------------------
// Face enrollment is ALWAYS pinned to the primary camera (cam1). The
// backend enrollment proxy already hits face-recognizer:8081 which reads
// from frames:cam1, so the wizard preview opens a dedicated WebSocket
// to that camera regardless of which detail page the user is on.
// This avoids the visual disconnect where a wizard launched from
// /single.html?camera=cam2 would have shown the basement feed.
const ENROLLMENT_CAMERA = "cam1";  // pinned; matches face-recognizer:8081 mount
let _wizardWs = null;

function _wizardOpenPreviewWS() {
    _wizardCloseWS();  // belt-and-suspenders
    try {
        const protocol = window.location.protocol === "https:" ? "wss" : "ws";
        const url = `${protocol}://${window.location.host}/ws/live?camera=${encodeURIComponent(ENROLLMENT_CAMERA)}`;
        _wizardWs = new WebSocket(url);
        const wizFeed = document.getElementById("wizardLiveFeed");
        _wizardWs.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === "frame" && msg.frame && wizFeed) {
                    wizFeed.src = "data:image/jpeg;base64," + msg.frame;
                    wizFeed.style.display = "block";
                }
            } catch (_) { /* ignore */ }
        };
        _wizardWs.onclose = () => { _wizardWs = null; };
        _wizardWs.onerror = (e) => { console.warn("Wizard WS error:", e); };
    } catch (e) {
        console.warn("Failed to open wizard preview WS:", e);
    }
}

function _wizardCloseWS() {
    if (_wizardWs && _wizardWs.readyState !== WebSocket.CLOSED) {
        try { _wizardWs.close(); } catch (_) {}
    }
    _wizardWs = null;
}

function startEnrollWizard() {
    const name = _faceEls.nameInput?.value.trim();
    if (!name) {
        showEnrollStatus("Please enter a name first", "error");
        return;
    }

    _wizardName = name;
    _wizardStep = 0;
    _wizardCaptured = 0;
    _wizardActive = true;

    // Set wizard name display
    document.getElementById("wizardName").textContent = name;
    // Hint which camera is being used (visible in the wizard header area)
    const camHint = document.getElementById("wizardCameraHint");
    if (camHint) camHint.textContent = `Using ${ENROLLMENT_CAMERA} camera`;

    // Reset all step indicators
    document.querySelectorAll(".wizard-step").forEach((el, i) => {
        el.className = "wizard-step" + (i === 0 ? " active" : "");
    });

    // Reset UI
    _wizardUpdateUI();

    // Open the pinned-cam1 preview WebSocket (independent of the page's main feed)
    _wizardOpenPreviewWS();

    // Show wizard modal with animation
    const overlay = document.getElementById("enrollWizard");
    overlay.style.display = "flex";
    requestAnimationFrame(() => overlay.classList.add("visible"));
}

// ---------------------------------------------------------------------------
// Close the Wizard
// ---------------------------------------------------------------------------
function closeWizard() {
    _wizardActive = false;
    _wizardCloseWS();
    const overlay = document.getElementById("enrollWizard");
    overlay.classList.remove("visible");
    setTimeout(() => { overlay.style.display = "none"; }, 300);

    if (_wizardCaptured > 0) {
        showEnrollStatus(
            `✅ Enrolled ${_wizardName} with ${_wizardCaptured} angle${_wizardCaptured > 1 ? "s" : ""}!`,
            "success"
        );
        if (_faceEls.nameInput) _faceEls.nameInput.value = "";
        loadFaces();
    }
}

// ---------------------------------------------------------------------------
// Capture Current Angle
// ---------------------------------------------------------------------------
async function wizardCapture() {
    if (!_wizardActive) return;

    const captureBtn = document.getElementById("wizardCaptureBtn");
    const statusEl = document.getElementById("wizardStatus");
    const photoEl = document.getElementById("wizardPhoto");
    const feedContainer = document.querySelector(".wizard-feed-container");

    captureBtn.disabled = true;
    statusEl.textContent = "";

    // --- 3-2-1 Countdown ---
    const countdownEl = document.createElement("div");
    countdownEl.className = "wizard-countdown";
    countdownEl.style.cssText = `
        position: absolute; top: 0; left: 0; width: 100%; height: 100%;
        display: flex; align-items: center; justify-content: center;
        font-size: 4rem; font-weight: 700; color: rgba(74,222,128,0.9);
        background: rgba(0,0,0,0.4); border-radius: 12px; z-index: 10;
        text-shadow: 0 2px 12px rgba(0,0,0,0.6);
    `;
    if (feedContainer) {
        feedContainer.style.position = "relative";
        feedContainer.appendChild(countdownEl);
    }

    for (let i = 3; i >= 1; i--) {
        countdownEl.textContent = i;
        await new Promise(r => setTimeout(r, 700));
    }
    countdownEl.textContent = "📸";
    await new Promise(r => setTimeout(r, 300));
    if (countdownEl.parentNode) countdownEl.remove();

    captureBtn.textContent = "⏳ Capturing...";

    try {
        // Enroll directly — one call grabs frame + extracts face + saves
        const enrollRes = await fetch("/api/faces/enroll", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: _wizardName }),
        });
        const enrollData = await enrollRes.json();

        if (enrollRes.ok && enrollData.success) {
            _wizardCaptured++;

            // Hide live feed container, show the enrolled face photo
            if (feedContainer) feedContainer.style.display = "none";
            photoEl.src = `/api/faces/${enrollData.face_id}/photo`;
            photoEl.style.display = "block";

            // Mark this step as done
            const stepEl = document.querySelector(`.wizard-step[data-step="${_wizardStep}"]`);
            if (stepEl) stepEl.classList.add("done");

            statusEl.textContent = `✅ ${WIZARD_ANGLES[_wizardStep].label} captured!`;
            statusEl.className = "wizard-status success";

            // Update progress
            _wizardUpdateProgress();

            // Auto-advance after a brief pause
            if (_wizardStep < WIZARD_ANGLES.length - 1) {
                setTimeout(() => {
                    _wizardStep++;
                    _wizardUpdateUI();
                }, 800);
            } else {
                // All done!
                captureBtn.textContent = "✅ Done!";
                setTimeout(() => closeWizard(), 1200);
                return;
            }
        } else {
            statusEl.textContent = `❌ ${enrollData.error || "No face detected"} — try again`;
            statusEl.className = "wizard-status error";
        }
    } catch (err) {
        statusEl.textContent = "❌ Face recognizer not available";
        statusEl.className = "wizard-status error";
        console.error("Wizard capture error:", err);
    } finally {
        captureBtn.disabled = false;
        captureBtn.textContent = "📸 Capture";
    }
}

// ---------------------------------------------------------------------------
// Skip Current Angle
// ---------------------------------------------------------------------------
function wizardSkip() {
    if (_wizardStep < WIZARD_ANGLES.length - 1) {
        // Mark as skipped
        const stepEl = document.querySelector(`.wizard-step[data-step="${_wizardStep}"]`);
        if (stepEl) stepEl.classList.add("skipped");

        _wizardStep++;
        _wizardUpdateUI();
    } else {
        // Last step, close
        closeWizard();
    }
}

// ---------------------------------------------------------------------------
// Update Wizard UI for Current Step
// ---------------------------------------------------------------------------
function _wizardUpdateUI() {
    const angle = WIZARD_ANGLES[_wizardStep];
    const promptEl = document.getElementById("wizardPrompt");
    const photoEl = document.getElementById("wizardPhoto");
    const feedContainer = document.querySelector(".wizard-feed-container");
    const captureBtn = document.getElementById("wizardCaptureBtn");
    const skipBtn = document.getElementById("wizardSkipBtn");
    const statusEl = document.getElementById("wizardStatus");

    // Update prompt
    promptEl.textContent = angle.prompt;

    // Reset — show live feed container, hide captured photo
    photoEl.style.display = "none";
    if (feedContainer) feedContainer.style.display = "block";
    statusEl.textContent = "";
    statusEl.className = "wizard-status";

    captureBtn.disabled = false;
    captureBtn.textContent = "📸 Capture";

    // Show skip button after first capture
    skipBtn.style.display = _wizardCaptured > 0 ? "inline-flex" : "none";

    // Update step indicators
    document.querySelectorAll(".wizard-step").forEach((el, i) => {
        if (i === _wizardStep) {
            el.classList.add("active");
        } else {
            el.classList.remove("active");
        }
    });

    // Animate the prompt change
    promptEl.style.animation = "none";
    requestAnimationFrame(() => {
        promptEl.style.animation = "fadeInUp 0.4s ease forwards";
    });
}

// ---------------------------------------------------------------------------
// Update Progress Bar
// ---------------------------------------------------------------------------
function _wizardUpdateProgress() {
    const fill = document.getElementById("wizardProgressFill");
    const counter = document.getElementById("wizardCounter");
    const pct = (_wizardCaptured / WIZARD_ANGLES.length) * 100;
    fill.style.width = `${pct}%`;
    counter.textContent = `${_wizardCaptured} / ${WIZARD_ANGLES.length} captured`;
}

// ---------------------------------------------------------------------------
// Legacy enrollFace — now redirects to wizard
// ---------------------------------------------------------------------------
function enrollFace() {
    startEnrollWizard();
}

// ---------------------------------------------------------------------------
// Enroll Status (for non-wizard messages)
// ---------------------------------------------------------------------------
function showEnrollStatus(message, type) {
    const existing = document.querySelector(".enroll-status");
    if (existing) existing.remove();
    const preview = document.querySelector(".enroll-preview");
    if (preview) preview.remove();

    const status = document.createElement("div");
    status.className = `enroll-status ${type}`;
    status.textContent = message;

    const form = document.querySelector(".enroll-form");
    form.parentNode.insertBefore(status, form.nextSibling.nextSibling);

    setTimeout(() => status.remove(), 4000);
}

// ---------------------------------------------------------------------------
// Load and display all enrolled faces in the gallery
// ---------------------------------------------------------------------------
async function loadFaces() {
    try {
        const response = await fetch("/api/faces");
        const data = await response.json();
        const faces = data.faces || [];

        // Count unique names (since multi-angle means multiple rows per person)
        const uniqueNames = new Set(faces.map(f => f.name));
        const angleCount = faces.length;
        if (_faceEls.count) _faceEls.count.textContent = `${uniqueNames.size} people (${angleCount} angles)`;

        if (faces.length === 0) {
            if (_faceEls.gallery) _faceEls.gallery.innerHTML = '<div class="empty-state">No faces enrolled yet</div>';
            return;
        }

        // Group faces by name for a cleaner display
        const grouped = {};
        for (const face of faces) {
            if (!grouped[face.name]) {
                grouped[face.name] = { faces: [], latestId: face.id };
            }
            grouped[face.name].faces.push(face);
            if (face.id > grouped[face.name].latestId) {
                grouped[face.name].latestId = face.id;
            }
        }

        if (!_faceEls.gallery) return;
        _faceEls.gallery.innerHTML = "";
        for (const [name, group] of Object.entries(grouped)) {
            const card = document.createElement("div");
            card.className = "face-card";

            const dateStr = group.faces[0].created_at ?
                new Date(group.faces[0].created_at).toLocaleDateString() : "Unknown";

            const angleLabel = group.faces.length > 1
                ? `${group.faces.length} angles`
                : "1 angle";

            // Demographics chip: prefer per-name override, else mode-vote
            // on model output across angles. Same fallback for age (mean).
            const firstOverride = group.faces.find(f =>
                f.sex_override != null || f.age_override != null
            );
            const overrideSex = firstOverride ? firstOverride.sex_override : null;
            const overrideAge = firstOverride ? firstOverride.age_override : null;

            let sexCounts = {};
            let ageSum = 0, ageN = 0;
            for (const f of group.faces) {
                if (f.sex) sexCounts[f.sex] = (sexCounts[f.sex] || 0) + 1;
                if (typeof f.age === "number") { ageSum += f.age; ageN++; }
            }
            let bestSex = null, bestCount = 0;
            for (const [k, v] of Object.entries(sexCounts)) {
                if (v > bestCount) { bestSex = k; bestCount = v; }
            }
            const effectiveSex = overrideSex || bestSex;
            const effectiveAge = (overrideAge != null) ? overrideAge :
                (ageN ? (ageSum / ageN) : null);
            const sexLabel = effectiveSex === "M" ? "M"
                : effectiveSex === "F" ? "F" : "";
            const ageBand = (effectiveAge != null)
                ? `~${Math.round(effectiveAge / 10) * 10}s` : "";
            const demoLabel = [sexLabel, ageBand].filter(Boolean).join(" · ");
            const isOverridden = (overrideSex != null || overrideAge != null);
            const demoTitle = isOverridden
                ? "Manual override (click edit to change)"
                : "Estimated from face — model has ~7yr age MAE and weak on kids/seniors";
            const demoClass = isOverridden ? "face-demo face-demo-pinned" : "face-demo";
            const demoChip = demoLabel
                ? `<div class="${demoClass}" title="${demoTitle}">${demoLabel}${isOverridden ? " 📌" : ""}</div>`
                : "";

            // Stash current override state on the card so the edit modal
            // can preload it without an extra fetch.
            card.dataset.name = name;
            card.dataset.overrideSex = overrideSex || "";
            card.dataset.overrideAge = (overrideAge != null) ? overrideAge : "";

            card.innerHTML = `
                <img src="/api/faces/${group.latestId}/photo"
                     alt="${name}"
                     onerror="this.src='data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0MCIgaGVpZ2h0PSI0MCIgdmlld0JveD0iMCAwIDQwIDQwIj48cmVjdCB3aWR0aD0iNDAiIGhlaWdodD0iNDAiIGZpbGw9IiMyZDM3NDgiLz48dGV4dCB4PSI1MCUiIHk9IjUwJSIgZG9taW5hbnQtYmFzZWxpbmU9Im1pZGRsZSIgdGV4dC1hbmNob3I9Im1pZGRsZSIgZmlsbD0iIzk0YTNiOCIgZm9udC1zaXplPSIxOCI+8J+RpDwvdGV4dD48L3N2Zz4='">
                <div class="face-info">
                    <div class="face-name">${name}</div>
                    <div class="face-date">${angleLabel} · ${dateStr}</div>
                    ${demoChip}
                </div>
                <div class="face-actions">
                    <button class="face-edit" onclick='editDemographics(${JSON.stringify(name)})' title="Edit gender / age">
                        ✏️
                    </button>
                    <button class="face-delete" onclick='deleteAllFaces(${JSON.stringify(name)})' title="Remove all angles">
                        🗑️
                    </button>
                </div>
            `;

            _faceEls.gallery.appendChild(card);
        }
    } catch (err) {
        console.error("Failed to load faces:", err);
        if (_faceEls.gallery) _faceEls.gallery.innerHTML = '<div class="empty-state">Face recognizer offline</div>';
    }
}

// ---------------------------------------------------------------------------
// Delete all faces for a person (all angles)
// ---------------------------------------------------------------------------
async function deleteAllFaces(name) {
    if (!confirm(`Remove all enrolled angles for ${name}?`)) return;

    try {
        const response = await fetch("/api/faces");
        const data = await response.json();
        const toDelete = (data.faces || []).filter(f => f.name === name);

        for (const face of toDelete) {
            await fetch(`/api/faces/${face.id}`, { method: "DELETE" });
        }

        loadFaces();
    } catch (err) {
        console.error("Face deletion error:", err);
    }
}

// Legacy single-face delete (still works)
async function deleteFace(faceId, name) {
    if (!confirm(`Remove ${name} from known faces?`)) return;

    try {
        const response = await fetch(`/api/faces/${faceId}`, { method: "DELETE" });
        if (response.ok) loadFaces();
    } catch (err) {
        console.error("Face deletion error:", err);
    }
}

// Allow Enter key to trigger enrollment
const _enrollInput = _faceEls.nameInput;
if (_enrollInput) {
    _enrollInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") startEnrollWizard();
    });
}

// ---------------------------------------------------------------------------
// Manual gender/age override modal
// ---------------------------------------------------------------------------
// Built lazily on first use, reused thereafter. Lives on the document so it
// survives gallery re-renders.
function _ensureDemoModal() {
    let overlay = document.getElementById("demoEditOverlay");
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.id = "demoEditOverlay";
    overlay.className = "modal-overlay";
    overlay.hidden = true;
    overlay.innerHTML = `
        <div class="modal-content" style="max-width: 340px;">
            <h3>Edit demographics</h3>
            <p id="demoEditTarget" class="hint"></p>
            <label>Gender
                <select id="demoEditSex">
                    <option value="">Auto (use model)</option>
                    <option value="M">Male</option>
                    <option value="F">Female</option>
                </select>
            </label>
            <label>Age
                <input type="number" id="demoEditAge" min="0" max="120" step="1"
                       placeholder="Auto (use model)">
            </label>
            <p class="wizard-hint" style="font-size: 11px;">
                Set explicitly when the auto-estimate is wrong (kids / seniors).
                Leave blank for "use the model's value."
            </p>
            <div class="modal-actions">
                <button id="demoEditCancel" class="btn-tertiary">Cancel</button>
                <button id="demoEditSave" class="btn-primary">Save</button>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);
    overlay.querySelector("#demoEditCancel").addEventListener("click", () => {
        overlay.hidden = true;
    });
    overlay.querySelector("#demoEditSave").addEventListener("click", async () => {
        const name = overlay.dataset.name;
        const sexEl = overlay.querySelector("#demoEditSex");
        const ageEl = overlay.querySelector("#demoEditAge");
        const sex = sexEl.value || null;
        const ageRaw = ageEl.value.trim();
        const age = ageRaw === "" ? null : Number(ageRaw);
        if (age !== null && (Number.isNaN(age) || age < 0 || age > 120)) {
            alert("Age must be a number between 0 and 120.");
            return;
        }
        try {
            const resp = await fetch(
                `/api/faces/by_name/${encodeURIComponent(name)}/demographics`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ sex, age }),
                },
            );
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                alert(`Save failed: ${err.error || resp.status}`);
                return;
            }
            overlay.hidden = true;
            loadFaces();
        } catch (e) {
            console.error("Demographics save error:", e);
            alert("Save failed — see console.");
        }
    });
    return overlay;
}

function editDemographics(name) {
    const overlay = _ensureDemoModal();
    overlay.dataset.name = name;
    overlay.querySelector("#demoEditTarget").textContent = `For: ${name}`;
    // Preload current override from the matching card
    const card = document.querySelector(`.face-card[data-name="${CSS.escape(name)}"]`);
    const sex = card ? card.dataset.overrideSex : "";
    const age = card ? card.dataset.overrideAge : "";
    overlay.querySelector("#demoEditSex").value = sex || "";
    overlay.querySelector("#demoEditAge").value = age || "";
    overlay.hidden = false;
}

// Expose so the inline onclick handler can call it.
window.editDemographics = editDemographics;
