/**
 * services/dashboard/static/zones.js — Zone editor logic.
 *
 * PURPOSE:
 *   Manages zone drawing on the canvas overlay (click-to-place polygon),
 *   saving/loading/deleting zones via the API, and rendering the zone list.
 *   Supports drag-to-edit vertices and mobile touch events.
 *
 * RELATIONSHIPS:
 *   - REST: /api/zones (CRUD + PUT for edits)
 *   - HTML: #zoneCanvas, #zoneToolbar, #zoneList in index.html
 *   - Canvas: Draws polygons over the live camera feed
 */

// ---------------------------------------------------------------------------
// DOM Elements
// ---------------------------------------------------------------------------
const zoneCanvas = document.getElementById("zoneCanvas");
const zoneCtx = zoneCanvas.getContext("2d");
const zoneToolbar = document.getElementById("zoneToolbar");
const zoneToggleBtn = document.getElementById("zoneToggleBtn");
const zoneNameInput = document.getElementById("zoneName");
const zoneSaveBtn = document.getElementById("zoneSaveBtn");
const zoneHint = document.getElementById("zoneHint");
const zoneList = document.getElementById("zoneList");
const zoneCountEl = document.getElementById("zoneCount");

let zoneDrawMode = false;
let currentZonePoints = []; // Normalized 0-1 coordinates during drawing
let savedZones = [];        // Loaded from API

// --- Edit mode state ---
let _editingZone = null;      // { id, name, points, alert_level }
let _editDragIdx = -1;        // Index of vertex being dragged
let _editDragging = false;

// Alert level colors for canvas drawing and zone list
const alertColors = {
    always: { fill: "rgba(239, 68, 68, 0.25)", stroke: "#ef4444", label: "🔴 Always" },
    night_only: { fill: "rgba(245, 158, 11, 0.25)", stroke: "#f59e0b", label: "🟠 Night" },
    log_only: { fill: "rgba(59, 130, 246, 0.25)", stroke: "#3b82f6", label: "🔵 Log" },
    dead: { fill: "rgba(127, 29, 29, 0.25)", stroke: "#991b1b", label: "☠️ Dead" },
    dead_zone: { fill: "rgba(127, 29, 29, 0.25)", stroke: "#991b1b", label: "☠️ Dead" },
    ignore: { fill: "rgba(107, 114, 128, 0.25)", stroke: "#6b7280", label: "⚫ Off" },
};

/**
 * Calculate the actual displayed image bounds within the container,
 * accounting for object-fit: contain letterboxing.
 */
function getImageBounds() {
    const img = document.getElementById("liveFrame");
    const container = document.getElementById("videoContainer");
    const containerW = container.clientWidth;
    const containerH = container.clientHeight;
    const naturalW = img.naturalWidth || containerW;
    const naturalH = img.naturalHeight || containerH;

    const containerAspect = containerW / containerH;
    const imageAspect = naturalW / naturalH;

    let imgX, imgY, imgW, imgH;

    if (imageAspect > containerAspect) {
        // Image is wider — letterbox top/bottom
        imgW = containerW;
        imgH = containerW / imageAspect;
        imgX = 0;
        imgY = (containerH - imgH) / 2;
    } else {
        // Image is taller — letterbox left/right
        imgH = containerH;
        imgW = containerH * imageAspect;
        imgX = (containerW - imgW) / 2;
        imgY = 0;
    }

    return { imgX, imgY, imgW, imgH, containerW, containerH };
}

/**
 * Convert page click coordinates to normalized 0-1 image coordinates.
 * Returns null if click is outside the actual image area.
 */
function clickToNormalized(clientX, clientY) {
    const rect = zoneCanvas.getBoundingClientRect();
    const canvasX = clientX - rect.left;
    const canvasY = clientY - rect.top;

    const { imgX, imgY, imgW, imgH } = getImageBounds();

    // Check if click is within the actual image area
    if (canvasX < imgX || canvasX > imgX + imgW ||
        canvasY < imgY || canvasY > imgY + imgH) {
        return null; // Click in letterbox area
    }

    const normX = (canvasX - imgX) / imgW;
    const normY = (canvasY - imgY) / imgH;

    return [
        parseFloat(Math.max(0, Math.min(1, normX)).toFixed(4)),
        parseFloat(Math.max(0, Math.min(1, normY)).toFixed(4)),
    ];
}

/**
 * Convert normalized 0-1 coords to pixel coords on the canvas.
 */
function normalizedToPixel(normX, normY) {
    const { imgX, imgY, imgW, imgH } = getImageBounds();
    return [imgX + normX * imgW, imgY + normY * imgH];
}

/**
 * Resize the canvas to match the video container dimensions.
 * The canvas covers the full container; getImageBounds() handles
 * mapping to the actual displayed image within it.
 */
function resizeZoneCanvas() {
    const container = document.getElementById("videoContainer");
    const img = document.getElementById("liveFrame");
    if (img.naturalWidth > 0) {
        zoneCanvas.width = container.clientWidth;
        zoneCanvas.height = container.clientHeight;
        if (zoneDrawMode) {
            drawCurrentPolygon();
        }
        if (_editingZone) {
            _drawEditOverlay();
        }
    }
}

/**
 * Toggle zone drawing mode on/off.
 */
function toggleZoneMode() {
    // Exit edit mode if active
    if (_editingZone) { cancelEditMode(); }

    zoneDrawMode = !zoneDrawMode;

    if (zoneDrawMode) {
        zoneToggleBtn.textContent = "⬜ Stop Drawing";
        zoneToggleBtn.classList.add("active");
        zoneToolbar.style.display = "flex";
        zoneCanvas.style.pointerEvents = "auto";
        zoneCanvas.style.cursor = "crosshair";
        currentZonePoints = [];
        resizeZoneCanvas();
        zoneHint.textContent = "Click on the video to place points. Double-click to close polygon.";
        zoneSaveBtn.disabled = true;
    } else {
        exitZoneMode();
    }
}

/**
 * Exit zone drawing mode and clean up.
 */
function exitZoneMode() {
    zoneDrawMode = false;
    zoneToggleBtn.textContent = "🔲 Draw Zone";
    zoneToggleBtn.classList.remove("active");
    zoneToolbar.style.display = "none";
    zoneCanvas.style.pointerEvents = "none";
    zoneCanvas.style.cursor = "default";
    currentZonePoints = [];
    zoneCtx.clearRect(0, 0, zoneCanvas.width, zoneCanvas.height);
    zoneSaveBtn.disabled = true;
}

// ---------------------------------------------------------------------------
// Canvas Event Handlers — support both mouse and touch
// ---------------------------------------------------------------------------

/** Extract clientX/Y from either mouse or touch event */
function _getPointerCoords(e) {
    if (e.touches && e.touches.length > 0) {
        return { clientX: e.touches[0].clientX, clientY: e.touches[0].clientY };
    }
    return { clientX: e.clientX, clientY: e.clientY };
}

/**
 * Handle click/tap on zone canvas — add a point to the polygon, or
 * start dragging an edit vertex.
 */
function _handlePointerDown(e) {
    const { clientX, clientY } = _getPointerCoords(e);

    // --- Edit mode: check if clicking a vertex ---
    if (_editingZone) {
        const rect = zoneCanvas.getBoundingClientRect();
        const cx = clientX - rect.left;
        const cy = clientY - rect.top;
        for (let i = 0; i < _editingZone.points.length; i++) {
            const [px, py] = normalizedToPixel(_editingZone.points[i][0], _editingZone.points[i][1]);
            if (Math.hypot(cx - px, cy - py) < 14) {
                _editDragIdx = i;
                _editDragging = true;
                zoneCanvas.style.cursor = "grabbing";
                e.preventDefault();
                return;
            }
        }
        return;
    }

    // --- Draw mode ---
    if (!zoneDrawMode) return;

    const norm = clickToNormalized(clientX, clientY);
    if (!norm) return;

    currentZonePoints.push(norm);
    drawCurrentPolygon();

    if (currentZonePoints.length >= 3) {
        zoneSaveBtn.disabled = false;
        zoneHint.textContent = `${currentZonePoints.length} points placed. Double-click to close, or click Save.`;
    } else {
        zoneHint.textContent = `${currentZonePoints.length}/3 minimum points. Keep clicking...`;
    }
}

function _handlePointerMove(e) {
    if (!_editDragging || _editDragIdx < 0) return;
    e.preventDefault();
    const { clientX, clientY } = _getPointerCoords(e);
    const norm = clickToNormalized(clientX, clientY);
    if (!norm) return;
    _editingZone.points[_editDragIdx] = norm;
    _drawEditOverlay();
}

function _handlePointerUp(e) {
    if (_editDragging) {
        _editDragging = false;
        _editDragIdx = -1;
        zoneCanvas.style.cursor = "grab";
        // Auto-save after drag
        _saveEditedZone();
    }
}

// Mouse events
zoneCanvas.addEventListener("mousedown", _handlePointerDown);
zoneCanvas.addEventListener("mousemove", _handlePointerMove);
zoneCanvas.addEventListener("mouseup", _handlePointerUp);

// Touch events (#13)
zoneCanvas.addEventListener("touchstart", (e) => {
    _handlePointerDown(e);
}, { passive: false });
zoneCanvas.addEventListener("touchmove", (e) => {
    _handlePointerMove(e);
}, { passive: false });
zoneCanvas.addEventListener("touchend", _handlePointerUp);

/**
 * Handle double-click — close the polygon automatically.
 */
zoneCanvas.addEventListener("dblclick", (e) => {
    if (!zoneDrawMode || currentZonePoints.length < 3) return;

    // Remove the duplicate point from the double-click (two click events fire)
    if (currentZonePoints.length > 3) {
        currentZonePoints.pop();
    }

    drawCurrentPolygon(true);
    zoneHint.textContent = "Polygon closed! Name it and click Save.";
    zoneSaveBtn.disabled = false;
});

/**
 * Draw the current in-progress polygon on the canvas.
 * Now converts from normalized coords to pixel coords for rendering.
 */
function drawCurrentPolygon(closed = false) {
    zoneCtx.clearRect(0, 0, zoneCanvas.width, zoneCanvas.height);

    if (currentZonePoints.length === 0) return;

    const alertLevel = document.getElementById("zoneAlertLevel").value;
    const colors = alertColors[alertLevel] || alertColors.log_only;

    // Convert normalized points to pixel coords
    const pixelPoints = currentZonePoints.map(([nx, ny]) => normalizedToPixel(nx, ny));

    zoneCtx.beginPath();
    zoneCtx.moveTo(pixelPoints[0][0], pixelPoints[0][1]);

    for (let i = 1; i < pixelPoints.length; i++) {
        zoneCtx.lineTo(pixelPoints[i][0], pixelPoints[i][1]);
    }

    if (closed || pixelPoints.length >= 3) {
        zoneCtx.closePath();
        zoneCtx.fillStyle = colors.fill;
        zoneCtx.fill();
    }

    zoneCtx.strokeStyle = colors.stroke;
    zoneCtx.lineWidth = 2;
    zoneCtx.stroke();

    // Draw vertex dots
    for (const pt of pixelPoints) {
        zoneCtx.beginPath();
        zoneCtx.arc(pt[0], pt[1], 5, 0, Math.PI * 2);
        zoneCtx.fillStyle = colors.stroke;
        zoneCtx.fill();
        zoneCtx.strokeStyle = "#fff";
        zoneCtx.lineWidth = 1;
        zoneCtx.stroke();
    }
}

/**
 * Save the current polygon as a zone via the API.
 * Points are already in normalized 0-1 coords.
 */
async function saveZone() {
    if (currentZonePoints.length < 3) return;

    const name = zoneNameInput.value.trim() || "Zone";
    const alertLevel = document.getElementById("zoneAlertLevel").value;

    try {
        const resp = await fetch("/api/zones", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: name,
                points: currentZonePoints,
                alert_level: alertLevel,
            }),
        });

        if (!resp.ok) {
            const err = await resp.json();
            console.error("Save zone error:", err);
            return;
        }

        const result = await resp.json();
        console.log("Zone saved:", result);

        // Reset and exit draw mode
        zoneNameInput.value = "";
        exitZoneMode();
        loadZones();
    } catch (err) {
        console.error("Save zone error:", err);
    }
}

/**
 * Cancel the current zone drawing.
 */
function cancelZoneDraw() {
    zoneNameInput.value = "";
    exitZoneMode();
}

// ---------------------------------------------------------------------------
// Edit Mode — drag vertices of saved zones (#7)
// ---------------------------------------------------------------------------

/**
 * Enter edit mode for a specific zone.
 */
function editZone(zoneId) {
    const zone = savedZones.find(z => z.id === zoneId);
    if (!zone) return;

    // Exit draw mode if active
    if (zoneDrawMode) exitZoneMode();

    _editingZone = JSON.parse(JSON.stringify(zone)); // Deep copy
    zoneCanvas.style.pointerEvents = "auto";
    zoneCanvas.style.cursor = "grab";
    resizeZoneCanvas();
    _drawEditOverlay();

    zoneHint.textContent = `Editing "${zone.name}" — drag vertices to reposition.`;
    zoneToolbar.style.display = "flex";
    zoneSaveBtn.disabled = true; // Auto-saves on drop
    zoneToggleBtn.textContent = "✕ Stop Editing";
    zoneToggleBtn.classList.add("active");
}

/**
 * Draw the editing overlay with draggable vertex handles.
 */
function _drawEditOverlay() {
    zoneCtx.clearRect(0, 0, zoneCanvas.width, zoneCanvas.height);
    if (!_editingZone) return;

    const colors = alertColors[_editingZone.alert_level] || alertColors.log_only;
    const pixelPoints = _editingZone.points.map(([nx, ny]) => normalizedToPixel(nx, ny));

    // Fill polygon
    zoneCtx.beginPath();
    zoneCtx.moveTo(pixelPoints[0][0], pixelPoints[0][1]);
    for (let i = 1; i < pixelPoints.length; i++) {
        zoneCtx.lineTo(pixelPoints[i][0], pixelPoints[i][1]);
    }
    zoneCtx.closePath();
    zoneCtx.fillStyle = colors.fill;
    zoneCtx.fill();
    zoneCtx.strokeStyle = colors.stroke;
    zoneCtx.lineWidth = 2;
    zoneCtx.stroke();

    // Draw large draggable vertex handles
    for (const pt of pixelPoints) {
        zoneCtx.beginPath();
        zoneCtx.arc(pt[0], pt[1], 8, 0, Math.PI * 2);
        zoneCtx.fillStyle = colors.stroke;
        zoneCtx.fill();
        zoneCtx.strokeStyle = "#fff";
        zoneCtx.lineWidth = 2;
        zoneCtx.stroke();
    }
}

/**
 * Save the edited zone via PUT endpoint.
 */
async function _saveEditedZone() {
    if (!_editingZone) return;
    try {
        await fetch(`/api/zones/${_editingZone.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ points: _editingZone.points }),
        });
        loadZones(); // Refresh list
    } catch (err) {
        console.error("Edit zone save error:", err);
    }
}

/**
 * Cancel edit mode.
 */
function cancelEditMode() {
    _editingZone = null;
    _editDragIdx = -1;
    _editDragging = false;
    zoneCanvas.style.pointerEvents = "none";
    zoneCanvas.style.cursor = "default";
    zoneCtx.clearRect(0, 0, zoneCanvas.width, zoneCanvas.height);
    zoneToolbar.style.display = "none";
    zoneToggleBtn.textContent = "🔲 Draw Zone";
    zoneToggleBtn.classList.remove("active");
}

// ---------------------------------------------------------------------------
// Zone List CRUD
// ---------------------------------------------------------------------------

/**
 * Load all zones from the API and render them in the zone list panel.
 */
async function loadZones() {
    try {
        const resp = await fetch("/api/zones");
        const data = await resp.json();
        savedZones = data.zones || [];

        zoneCountEl.textContent = `${savedZones.length} zone${savedZones.length !== 1 ? "s" : ""}`;

        if (savedZones.length === 0) {
            zoneList.innerHTML = '<div class="empty-state">No zones defined — use "Draw Zone" above</div>';
            return;
        }

        zoneList.innerHTML = savedZones.map(zone => {
            const colors = alertColors[zone.alert_level] || alertColors.log_only;
            return `
                <div class="zone-card">
                    <div class="zone-card-info">
                        <span class="zone-card-dot" style="background: ${colors.stroke}"></span>
                        <span class="zone-card-name">${zone.name}</span>
                        <span class="zone-card-level">${colors.label}</span>
                    </div>
                    <div class="zone-card-actions">
                        <button class="zone-card-edit" onclick="editZone('${zone.id}')" title="Edit vertices">✏️</button>
                        <button class="zone-card-delete" onclick="deleteZone('${zone.id}', '${zone.name}')" title="Delete zone">🗑️</button>
                    </div>
                </div>
            `;
        }).join("");
    } catch (err) {
        console.error("Load zones error:", err);
    }
}

/**
 * Delete a zone by ID.
 */
async function deleteZone(zoneId, name) {
    if (!confirm(`Delete zone "${name}"?`)) return;

    try {
        await fetch(`/api/zones/${zoneId}`, { method: "DELETE" });
        loadZones();
    } catch (err) {
        console.error("Delete zone error:", err);
    }
}

// Auto-resize canvas when frame updates
const liveFrameImg = document.getElementById("liveFrame");
liveFrameImg.addEventListener("load", resizeZoneCanvas);
window.addEventListener("resize", resizeZoneCanvas);

// Update polygon preview when alert level changes
document.getElementById("zoneAlertLevel").addEventListener("change", () => {
    if (currentZonePoints.length > 0) drawCurrentPolygon();
});

