/**
 * services/dashboard/static/unknowns.js — Unknown faces gallery + labeling.
 *
 * PURPOSE:
 *   Manages auto-captured unknown faces: loading, labeling via modal,
 *   deleting individuals, and clearing all unknowns.
 *
 * RELATIONSHIPS:
 *   - REST: /api/unknowns, /api/unknowns/:id/label, /api/unknowns/:id (DELETE)
 *   - HTML: #unknownsPanel, #unknownGallery, #labelModal in index.html
 *   - Depends on: faces.js (showEnrollStatus, loadFaces)
 */

// ---------------------------------------------------------------------------
// DOM Elements
// ---------------------------------------------------------------------------
const unknownGallery = document.getElementById("unknownGallery");
const unknownCount = document.getElementById("unknownCount");

/**
 * Load and display auto-captured unknown faces.
 */
async function loadUnknowns() {
    try {
        const response = await fetch("/api/unknowns");
        const data = await response.json();
        const unknowns = data.unknowns || [];

        unknownCount.textContent = `${unknowns.length} captured`;

        if (unknowns.length === 0) {
            unknownGallery.innerHTML = '<div class="empty-state">No unknown faces yet</div>';
            return;
        }

        unknownGallery.innerHTML = "";
        for (const u of unknowns) {
            const card = document.createElement("div");
            card.className = "face-card unknown-card";

            const lastSeen = u.last_seen ?
                new Date(u.last_seen + "Z").toLocaleString() : "Unknown";

            card.innerHTML = `
                <img src="/api/unknowns/${u.id}/photo"
                     alt="Unknown person"
                     onerror="this.src='data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0MCIgaGVpZ2h0PSI0MCIgdmlld0JveD0iMCAwIDQwIDQwIj48cmVjdCB3aWR0aD0iNDAiIGhlaWdodD0iNDAiIGZpbGw9IiMyZDM3NDgiLz48dGV4dCB4PSI1MCUiIHk9IjUwJSIgZG9taW5hbnQtYmFzZWxpbmU9Im1pZGRsZSIgdGV4dC1hbmNob3I9Im1pZGRsZSIgZmlsbD0iIzk0YTNiOCIgZm9udC1zaXplPSIxOCI+4p2TPC90ZXh0Pjwvc3ZnPg=='">
                <div class="face-info">
                    <div class="face-name unknown-label">Unknown #${u.id}</div>
                    <div class="face-date">Seen ${u.sighting_count}× · Last ${lastSeen}</div>
                </div>
                <div class="unknown-actions">
                    <button class="label-btn" onclick="labelUnknown(${u.id})" title="Assign name">
                        🏷️
                    </button>
                    <button class="face-delete" onclick="deleteUnknown(${u.id})" title="Dismiss">
                        🗑️
                    </button>
                </div>
            `;

            unknownGallery.appendChild(card);
        }
    } catch (err) {
        console.error("Failed to load unknowns:", err);
        unknownGallery.innerHTML = '<div class="empty-state">Face recognizer offline</div>';
    }
}

/**
 * Track which unknown we're labeling.
 */
let _labelingUid = null;

/**
 * Open the label modal for an unknown face.
 * Populates the dropdown with all enrolled face names.
 */
async function labelUnknown(uid) {
    _labelingUid = uid;
    const modal = document.getElementById("labelModal");
    const photo = document.getElementById("labelModalPhoto");
    const select = document.getElementById("labelModalSelect");
    const input = document.getElementById("labelModalInput");

    // Show the unknown's photo
    photo.src = `/api/unknowns/${uid}/photo`;
    input.value = "";

    // Populate dropdown with enrolled face names
    select.innerHTML = '<option value="">— Choose enrolled face —</option>';
    try {
        const resp = await fetch("/api/faces");
        const data = await resp.json();
        const names = [...new Set((data.faces || []).map(f => f.name))];
        for (const name of names) {
            const opt = document.createElement("option");
            opt.value = name;
            opt.textContent = name;
            select.appendChild(opt);
        }
    } catch (err) {
        console.error("Failed to load faces for dropdown:", err);
    }

    // Clear text input when dropdown is selected and vice versa
    select.onchange = () => { if (select.value) input.value = ""; };
    input.oninput = () => { if (input.value) select.value = ""; };

    modal.style.display = "flex";
}

/**
 * Close the label modal.
 */
function closeLabelModal() {
    document.getElementById("labelModal").style.display = "none";
    _labelingUid = null;
}

/**
 * Submit the label from the modal.
 */
async function submitLabel() {
    if (!_labelingUid) return;

    const select = document.getElementById("labelModalSelect");
    const input = document.getElementById("labelModalInput");
    const name = select.value || input.value.trim();

    if (!name) {
        input.focus();
        return;
    }

    try {
        const response = await fetch(`/api/unknowns/${_labelingUid}/label`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
        });

        const data = await response.json();
        if (response.ok && data.success) {
            showEnrollStatus(`✅ Labeled as ${name} — will be recognized from now on!`, "success");
            loadUnknowns();
            loadFaces();
        } else {
            showEnrollStatus(`❌ ${data.error || "Labeling failed"}`, "error");
        }
    } catch (err) {
        console.error("Label unknown error:", err);
    }

    closeLabelModal();
}

/**
 * Delete an auto-captured unknown face.
 */
async function deleteUnknown(uid) {
    if (!confirm("Dismiss this unknown face?")) return;

    try {
        const response = await fetch(`/api/unknowns/${uid}`, { method: "DELETE" });
        if (response.ok) {
            loadUnknowns();
        }
    } catch (err) {
        console.error("Delete unknown error:", err);
    }
}

/**
 * Clear all unknown faces at once.
 */
async function clearAllUnknowns() {
    if (!confirm("Clear ALL unknown faces? This cannot be undone.")) return;

    try {
        const response = await fetch("/api/unknowns/clear", { method: "DELETE" });
        const data = await response.json();
        if (response.ok && data.success) {
            showEnrollStatus(`🗑️ Cleared ${data.cleared} unknown faces`, "success");
            loadUnknowns();
        } else {
            showEnrollStatus(`❌ ${data.error || "Failed to clear unknowns"}`, "error");
        }
    } catch (err) {
        console.error("Clear unknowns error:", err);
    }
}
