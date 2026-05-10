/**
 * monitoring.js — Client-side logic for the System Monitor page.
 *
 * PURPOSE:
 *   Fetches health summary data from /api/monitoring/health,
 *   updates the summary cards, manages the Grafana iframe time range,
 *   and handles fullscreen toggle.
 *
 * RELATIONSHIPS:
 *   - Loaded by monitoring.html
 *   - Calls /api/monitoring/health (served by routes/metrics.py)
 *   - Controls the Grafana iframe URL
 */

(function () {
    "use strict";

    // ─── Config ───
    const HEALTH_POLL_INTERVAL = 30_000; // 30 seconds
    const HEALTH_API = "/api/monitoring/health";
    // Use the same hostname the user is browsing from (supports LAN IPs)
    const GRAFANA_BASE = `http://${window.location.hostname}:3000`;
    const DASHBOARD_PATH = "/d/vision-labs-main/vision-labs-system-monitor";

    let _healthTimer = null;
    let _isFullscreen = false;

    // ─── Health Card Updates ───
    async function fetchHealth() {
        try {
            const resp = await fetch(HEALTH_API);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            updateCards(data);
        } catch (err) {
            console.warn("Health fetch failed:", err.message);
        }
    }

    function updateCards(data) {
        // Active People
        const personsEl = document.getElementById("cardPersonsValue");
        if (personsEl) {
            personsEl.textContent = data.active_persons ?? "--";
            setCardStatus("cardPersons", data.active_persons > 0 ? "warn" : "ok");
        }

        // Inference Time
        const inferenceEl = document.getElementById("cardInferenceValue");
        if (inferenceEl) {
            const ms = data.inference_ms ?? 0;
            inferenceEl.textContent = ms > 0 ? `${ms}ms` : "--";
            setCardStatus("cardInference", ms > 50 ? "warn" : ms > 0 ? "ok" : null);
        }

        // GPU Status
        const gpuEl = document.getElementById("cardGpuValue");
        if (gpuEl) {
            gpuEl.textContent = data.gpu_paused ? "Generating" : "Detecting";
            setCardStatus("cardGpu", data.gpu_paused ? "warn" : "ok");
        }

        // Redis Memory
        const redisEl = document.getElementById("cardRedisValue");
        if (redisEl) {
            const mb = data.redis_memory_mb ?? 0;
            redisEl.textContent = mb > 0 ? `${mb} MB` : "--";
            setCardStatus("cardRedis", mb > 1500 ? "danger" : mb > 800 ? "warn" : "ok");
        }

        // Total Events
        const eventsEl = document.getElementById("cardEventsValue");
        if (eventsEl) {
            const total = data.total_events ?? 0;
            eventsEl.textContent = total > 0 ? total.toLocaleString() : "--";
        }

        // Alert Accuracy (no longer tracked — feedback system removed)
        const accEl = document.getElementById("cardAccuracyValue");
        if (accEl) {
            accEl.textContent = "N/A";
        }
    }

    function setCardStatus(cardId, status) {
        const el = document.getElementById(cardId);
        if (!el) return;
        el.classList.remove("mon-card--ok", "mon-card--warn", "mon-card--danger");
        if (status) el.classList.add(`mon-card--${status}`);
    }

    // ─── Grafana Iframe Management ───
    function updateGrafanaUrl(from) {
        const iframe = document.getElementById("grafanaFrame");
        if (!iframe) return;
        const url = `${GRAFANA_BASE}${DASHBOARD_PATH}?orgId=1&from=${from}&to=now&refresh=5s&theme=dark`;
        iframe.src = url;
    }

    window._monUpdateTimeRange = function (from) {
        updateGrafanaUrl(from);
    };

    // ─── Fullscreen Toggle ───
    window._monToggleFullscreen = function () {
        const wrapper = document.getElementById("monIframeWrapper");
        const btn = document.getElementById("monExpandBtn");
        if (!wrapper) return;

        _isFullscreen = !_isFullscreen;
        if (_isFullscreen) {
            wrapper.classList.add("mon-fullscreen");
            if (btn) btn.textContent = "✕";
            document.addEventListener("keydown", _escHandler);
        } else {
            wrapper.classList.remove("mon-fullscreen");
            if (btn) btn.textContent = "⛶";
            document.removeEventListener("keydown", _escHandler);
        }
    };

    function _escHandler(e) {
        if (e.key === "Escape") {
            window._monToggleFullscreen();
        }
    }

    // ─── Refresh Button ───
    window._monRefreshHealth = function () {
        const btn = document.getElementById("healthRefreshBtn");
        if (btn) {
            btn.textContent = "↻ ...";
            btn.disabled = true;
        }
        fetchHealth().finally(() => {
            if (btn) {
                btn.textContent = "↻ Refresh";
                btn.disabled = false;
            }
        });
    };

    // ─── Iframe Load Detection ───
    function setupIframeLoader() {
        const iframe = document.getElementById("grafanaFrame");
        const loading = document.getElementById("monIframeLoading");
        if (!iframe || !loading) return;

        iframe.addEventListener("load", () => {
            loading.classList.add("mon-hidden");
        });

        // Fallback: hide loading after 8 seconds regardless
        setTimeout(() => {
            loading.classList.add("mon-hidden");
        }, 8000);
    }

    // ─── Init ───
    function init() {
        fetchHealth();
        _healthTimer = setInterval(fetchHealth, HEALTH_POLL_INTERVAL);
        setupIframeLoader();

        // Set iframe src dynamically (uses correct hostname)
        updateGrafanaUrl("now-1h");

        // Update the "Open Grafana" direct link to use correct hostname
        const directLink = document.getElementById("grafanaDirectLink");
        if (directLink) directLink.href = GRAFANA_BASE;
    }

    // Start when DOM is ready
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
