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

// DOMPurify config — consistent with other dashboard JS files; strips dangerous
// attributes from server-supplied container/health data rendered into innerHTML.
// `_safeHtml` + `_PURIFY_CFG` are defined in js/lib/safe-html.js — single
// declaration site so two dashboard JS modules loading on the same page don't
// collide on the const.

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

    // ─── Tab switching (Grafana / Containers) ───
    let _containersTimer = null;
    const CONTAINERS_POLL_INTERVAL = 5000;

    function showTab(name) {
        const tabs = {
            grafana: document.getElementById("tabGrafana"),
            containers: document.getElementById("tabContainers"),
        };
        const panels = {
            grafana: document.getElementById("panelGrafana"),
            containers: document.getElementById("panelContainers"),
        };
        for (const k of Object.keys(tabs)) {
            const active = (k === name);
            if (tabs[k]) {
                tabs[k].classList.toggle("mon-tab--active", active);
                tabs[k].setAttribute("aria-selected", active ? "true" : "false");
            }
            if (panels[k]) panels[k].hidden = !active;
        }
        if (name === "containers") {
            fetchContainers();
            if (!_containersTimer) {
                _containersTimer = setInterval(fetchContainers, CONTAINERS_POLL_INTERVAL);
            }
        } else if (_containersTimer) {
            clearInterval(_containersTimer);
            _containersTimer = null;
        }
    }
    window._monShowTab = showTab;
    window._monRefreshContainers = function () { fetchContainers(); };

    function _escHtml(s) {
        return String(s)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    async function fetchContainers() {
        const wrapper = document.getElementById("monContainersWrapper");
        const ageEl = document.getElementById("containersAge");
        if (!wrapper) return;
        try {
            const resp = await fetch("/api/containers");
            const data = await resp.json();
            if (!data.ok) {
                wrapper.innerHTML = _safeHtml(`<div class="mon-iframe-loading"><span>${_escHtml(data.error || "No data")}</span><p class="mon-iframe-hint">Containers panel needs the orchestrator running.</p></div>`);
                if (ageEl) ageEl.textContent = "";
                return;
            }
            if (ageEl) {
                ageEl.textContent = data.stale
                    ? `(${data.age_seconds.toFixed(0)}s old — stale)`
                    : `(${data.age_seconds.toFixed(0)}s ago)`;
            }
            renderContainers(wrapper, data.containers || []);
        } catch (e) {
            wrapper.innerHTML = _safeHtml(`<div class="mon-iframe-loading"><span>Network error</span><p class="mon-iframe-hint">${_escHtml(e.message || String(e))}</p></div>`);
        }
    }

    function renderContainers(wrapper, containers) {
        if (!containers.length) {
            wrapper.innerHTML = `<div class="mon-iframe-loading"><span>No containers reported.</span></div>`;
            return;
        }
        const stateClass = (s) => {
            const v = (s || "").toLowerCase();
            if (v === "running") return "ok";
            if (v === "exited") return "err";
            if (v === "restarting" || v === "created") return "warn";
            return "muted";
        };
        const rows = containers.map(c => {
            const sc = stateClass(c.state);
            const health = c.health ? ` · ${_escHtml(c.health)}` : "";
            return `<tr>
                <td><code>${_escHtml(c.name)}</code></td>
                <td>${_escHtml(c.service)}</td>
                <td><span class="container-state container-state--${sc}">${_escHtml(c.state || "?")}</span></td>
                <td class="container-status">${_escHtml(c.status || "")}${health}</td>
            </tr>`;
        }).join("");
        wrapper.innerHTML = _safeHtml(`
            <table class="mon-containers-table">
                <thead><tr><th>Name</th><th>Service</th><th>State</th><th>Status</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
            <p class="mon-iframe-hint" style="padding: 8px 14px;">
                Read-only view from the orchestrator. For start/stop/exec/logs, use the
                <a href="https://localhost:9443" target="_blank" rel="noopener" style="color:#60a5fa;">Portainer</a>
                button above.
            </p>`);
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
