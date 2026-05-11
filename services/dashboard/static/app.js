/**
 * services/dashboard/static/app.js — Dashboard core logic.
 *
 * PURPOSE:
 *   Core application: WebSocket connection for live camera frames,
 *   settings slider handlers, and initialization orchestration.
 *
 * RELATIONSHIPS:
 *   - WebSocket: ws://localhost:8080/ws/live (live frames + detections)
 *   - REST: /api/config (read/write settings)
 *
 * MODULES (loaded via separate <script> tags):
 *   - conditions.js — Time period + weather panel
 *   - events.js     — Event feed polling + rendering
 *   - faces.js      — Face enrollment + gallery
 *   - unknowns.js   — Unknown faces + labeling modal
 *   - zones.js      — Zone drawing + canvas + CRUD
 */

// ---------------------------------------------------------------------------
// Camera selection (multi-camera)
// ---------------------------------------------------------------------------
// `single.html?camera=<id>` scopes the whole page (WebSocket, config, zones,
// events, state) to that camera. If absent, defaults to the dashboard's
// primary camera (server-side env CAMERA_ID); the backend handles that.
const CAMERA_ID = (() => {
    try {
        const fromUrl = new URLSearchParams(window.location.search).get("camera");
        if (fromUrl) return fromUrl;
    } catch (e) { /* no-op */ }
    return "";
})();
// Convenience for appending the param to API calls
const _cameraQ = CAMERA_ID ? `camera=${encodeURIComponent(CAMERA_ID)}` : "";
function withCamera(url) {
    if (!_cameraQ) return url;
    return url + (url.includes("?") ? "&" : "?") + _cameraQ;
}
// Expose so events.js (and other modules) can read the same id
window.EVENT_FEED_CAMERA = CAMERA_ID;

/** Fetch camera friendly-name from registry and update the page title. */
async function _updateLiveViewTitle() {
    const titleEl = document.getElementById("liveViewTitle");
    if (!titleEl) return;
    try {
        const res = await fetch("/api/cameras");
        const data = await res.json();
        const cams = data.cameras || [];
        // Resolve which camera we're showing: URL param > server primary
        const id = CAMERA_ID || (cams.find(c => c.is_primary) || cams[0] || {}).id;
        const match = cams.find(c => c.id === id);
        const label = match ? (match.name || match.id) : (id || "Primary");
        titleEl.textContent = `📹 Live View — ${label}`;
        document.title = `${label} · Vision Labs`;
    } catch (e) {
        titleEl.textContent = CAMERA_ID
            ? `📹 Live View — ${CAMERA_ID}`
            : "📹 Live View";
    }
}

// ---------------------------------------------------------------------------
// DOM Elements
// ---------------------------------------------------------------------------
const liveFrame = document.getElementById("liveFrame");
const noSignal = document.getElementById("noSignal");
const connectionStatus = document.getElementById("connectionStatus");
const liveBadge = document.getElementById("liveBadge");

// Stats display
const fpsValue = document.querySelector("#fpsDisplay .stat-value");
const inferenceValue = document.querySelector("#inferenceDisplay .stat-value");
const peopleValue = document.querySelector("#peopleDisplay .stat-value");

// Settings sliders — Person
const confidenceSlider = document.getElementById("confidenceSlider");
const confidenceValue = document.getElementById("confidenceValue");
const iouSlider = document.getElementById("iouSlider");
const iouValue = document.getElementById("iouValue");
const lostTimeoutSlider = document.getElementById("lostTimeoutSlider");
const lostTimeoutValue = document.getElementById("lostTimeoutValue");

// Settings sliders — Vehicle
const vehicleConfSlider = document.getElementById("vehicleConfSlider");
const vehicleConfValue = document.getElementById("vehicleConfValue");
const vehicleIdleSlider = document.getElementById("vehicleIdleSlider");
const vehicleIdleValue = document.getElementById("vehicleIdleValue");



// ---------------------------------------------------------------------------
// Collapsible Panels
// ---------------------------------------------------------------------------
/**
 * Toggle a sidebar panel open/closed.
 * Called from onclick on .panel-header elements in index.html.
 */
function togglePanel(headerEl) {
    const panel = headerEl.closest(".panel");
    if (panel) {
        panel.classList.toggle("collapsed");
    }
}


// ---------------------------------------------------------------------------
// WebSocket — Live Frame Streaming
// ---------------------------------------------------------------------------
let ws = null;
let frameCount = 0;
let lastFpsTime = Date.now();
let currentFps = 0;
let currentStreamMode = "sd"; // "sd" or "hd"

/**
 * Toggle between SD (with detection overlays) and HD (raw main stream).
 * Sends a WebSocket message to the server to switch modes.
 */
function toggleStreamMode() {
    const newMode = currentStreamMode === "sd" ? "hd" : "sd";
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: "switch_stream", stream: newMode }));
        currentStreamMode = newMode;
        _updateStreamToggleBtn();
    }
}

function _updateStreamToggleBtn() {
    const btn = document.getElementById("streamToggleBtn");
    if (!btn) return;
    if (currentStreamMode === "hd") {
        btn.textContent = "HD";
        btn.classList.add("hd-active");
        btn.title = "Currently viewing HD main stream (no overlays) — click for SD";
    } else {
        btn.textContent = "SD";
        btn.classList.remove("hd-active");
        btn.title = "Currently viewing SD sub stream (with overlays) — click for HD";
    }
}

/**
 * Connect to the live frame WebSocket.
 * Automatically reconnects on disconnect with a 2-second delay.
 */
function connectWebSocket() {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    let wsUrl = `${protocol}://${window.location.host}/ws/live`;
    if (CAMERA_ID) wsUrl += `?camera=${encodeURIComponent(CAMERA_ID)}`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log("WebSocket connected");
        connectionStatus.textContent = "Connected";
        connectionStatus.className = "status-badge connected";
        liveBadge.classList.add("live");
        noSignal.style.display = "none";
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);

            if (msg.type === "frame" && msg.frame) {
                liveFrame.src = "data:image/jpeg;base64," + msg.frame;
                liveFrame.style.display = "block";
                noSignal.style.display = "none";

                // Mirror frame to wizard live feed when enrollment wizard is open
                if (typeof _wizardActive !== "undefined" && _wizardActive) {
                    const wizFeed = document.getElementById("wizardLiveFeed");
                    if (wizFeed) {
                        wizFeed.src = liveFrame.src;
                        wizFeed.style.display = "block";
                    }
                }

                // Update FPS counter
                frameCount++;
                const now = Date.now();
                const elapsed = now - lastFpsTime;
                if (elapsed >= 1000) {
                    currentFps = Math.round((frameCount * 1000) / elapsed);
                    fpsValue.textContent = currentFps;
                    frameCount = 0;
                    lastFpsTime = now;
                }

                // Update stats
                inferenceValue.textContent = `${msg.inference_ms || "--"}ms`;
                peopleValue.textContent = msg.num_people || "0";
            }
        } catch (err) {
            console.error("WebSocket message error:", err);
        }
    };

    ws.onclose = () => {
        connectionStatus.textContent = "Disconnected";
        connectionStatus.className = "status-badge disconnected";
        liveBadge.classList.remove("live");
        setTimeout(connectWebSocket, 2000);
    };

    ws.onerror = (err) => {
        console.error("WebSocket error:", err);
        ws.close();
    };
}


// ---------------------------------------------------------------------------
// Settings — Slider handlers
// ---------------------------------------------------------------------------

/**
 * Send updated config to the backend.
 * The backend writes it to Redis, and the detector/tracker pick it up
 * on their next config poll cycle (every few seconds).
 */
async function updateConfig(key, value) {
    try {
        const response = await fetch(withCamera("/api/config"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ [key]: value }),
        });

        if (!response.ok) {
            console.error("Config update failed:", response.status);
        }
    } catch (err) {
        console.error("Config update error:", err);
    }
}

// Debounce timer for slider changes (don't spam the API)
let configDebounce = null;

function handleSliderChange(slider, valueDisplay, configKey) {
    const value = parseFloat(slider.value);
    valueDisplay.textContent = value.toFixed(2);

    // Debounce: wait 300ms after last change before sending
    if (configDebounce) clearTimeout(configDebounce);
    configDebounce = setTimeout(() => {
        updateConfig(configKey, value);
    }, 300);
}

// Wire up sliders
confidenceSlider.addEventListener("input", () =>
    handleSliderChange(confidenceSlider, confidenceValue, "confidence_thresh")
);
iouSlider.addEventListener("input", () =>
    handleSliderChange(iouSlider, iouValue, "iou_threshold")
);
lostTimeoutSlider.addEventListener("input", () =>
    handleSliderChange(lostTimeoutSlider, lostTimeoutValue, "lost_timeout")
);
vehicleConfSlider.addEventListener("input", () =>
    handleSliderChange(vehicleConfSlider, vehicleConfValue, "vehicle_confidence_thresh")
);
vehicleIdleSlider.addEventListener("input", () => {
    const value = parseFloat(vehicleIdleSlider.value);
    vehicleIdleValue.textContent = value.toFixed(0);
    if (configDebounce) clearTimeout(configDebounce);
    configDebounce = setTimeout(() => updateConfig("vehicle_idle_timeout", value), 300);
});


// ---------------------------------------------------------------------------
// Load existing config from backend (populate sliders on page load)
// ---------------------------------------------------------------------------
async function loadConfig() {
    try {
        const response = await fetch(withCamera("/api/config"));
        const data = await response.json();
        const config = data.config || {};

        if (config.confidence_thresh) {
            confidenceSlider.value = config.confidence_thresh;
            confidenceValue.textContent = parseFloat(config.confidence_thresh).toFixed(2);
        }
        if (config.iou_threshold) {
            iouSlider.value = config.iou_threshold;
            iouValue.textContent = parseFloat(config.iou_threshold).toFixed(2);
        }
        if (config.lost_timeout) {
            lostTimeoutSlider.value = config.lost_timeout;
            lostTimeoutValue.textContent = parseFloat(config.lost_timeout).toFixed(1);
        }
        if (config.vehicle_confidence_thresh) {
            vehicleConfSlider.value = config.vehicle_confidence_thresh;
            vehicleConfValue.textContent = parseFloat(config.vehicle_confidence_thresh).toFixed(2);
        }
        if (config.vehicle_idle_timeout) {
            vehicleIdleSlider.value = config.vehicle_idle_timeout;
            vehicleIdleValue.textContent = parseFloat(config.vehicle_idle_timeout).toFixed(0);
        }
    } catch (err) {
        console.error("Failed to load config:", err);
    }
}


// ---------------------------------------------------------------------------
// Notifications — Telegram test + status
// ---------------------------------------------------------------------------
async function checkNotificationStatus() {
    try {
        const resp = await fetch("/api/notifications/status");
        const data = await resp.json();
        const statusEl = document.getElementById("notifStatus");
        const hintEl = document.getElementById("notifConfigHint");
        const btnEl = document.getElementById("testNotificationBtn");

        if (data.configured) {
            statusEl.textContent = "✅ Active";
            statusEl.style.color = "#4ade80";
            hintEl.textContent = `Telegram connected. Rate limit: ${data.rate_limit_seconds}s between person alerts.`;
            btnEl.disabled = false;
        } else {
            statusEl.textContent = "⚠️ Not Set";
            statusEl.style.color = "#f59e0b";
            const missing = [];
            if (!data.has_token) missing.push("TELEGRAM_BOT_TOKEN");
            if (!data.has_chat_id) missing.push("TELEGRAM_CHAT_ID");
            hintEl.textContent = `Missing in .env: ${missing.join(", ")}`;
            btnEl.disabled = true;
        }
    } catch (e) {
        console.warn("Failed to check notification status:", e);
    }
}

async function testNotification() {
    const btn = document.getElementById("testNotificationBtn");
    const resultEl = document.getElementById("notifResult");

    btn.disabled = true;
    btn.textContent = "📤 Sending...";
    resultEl.style.display = "none";

    try {
        const resp = await fetch("/api/notifications/test", { method: "POST" });
        const data = await resp.json();

        resultEl.style.display = "block";
        if (resp.ok) {
            resultEl.textContent = "✅ " + data.message;
            resultEl.style.color = "#4ade80";
        } else {
            resultEl.textContent = "❌ " + (data.error || "Failed to send");
            resultEl.style.color = "#f87171";
        }
    } catch (e) {
        resultEl.style.display = "block";
        resultEl.textContent = "❌ Network error: " + e.message;
        resultEl.style.color = "#f87171";
    } finally {
        btn.disabled = false;
        btn.textContent = "📤 Send Test Notification";
    }
}


// ---------------------------------------------------------------------------
// Initialize — orchestrate all modules
// ---------------------------------------------------------------------------
function init() {
    // Update the title bar to show which camera we're scoped to
    _updateLiveViewTitle();

    // Connect to live stream
    connectWebSocket();

    // Load current settings
    loadConfig();

    // Load zones (zones.js)
    loadZones();

    // Load enrolled + unknown faces (faces.js, unknowns.js)
    loadFaces();
    loadUnknowns();

    // Start polling events every 2 seconds (events.js)
    pollEvents();
    setInterval(pollEvents, 2000);

    // Refresh face lists every 30 seconds
    setInterval(loadFaces, 30000);
    setInterval(loadUnknowns, 10000);

    // Refresh zone list every 15 seconds
    setInterval(loadZones, 15000);

    // Check notification status
    checkNotificationStatus();

    // Load browse panel (browse.js)
    initBrowse();
}

// Start when DOM is ready
document.addEventListener("DOMContentLoaded", init);
