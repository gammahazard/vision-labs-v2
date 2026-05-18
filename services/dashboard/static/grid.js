/**
 * static/grid.js — Multi-camera grid view.
 *
 * For each registered camera:
 *   1. Fetch list from /api/cameras
 *   2. Create a tile, open its own WebSocket at /ws/live?camera=<id>
 *   3. Render incoming frames into the tile's <img>
 *   4. Click tile → modal expands tile to fullscreen with the same WS feed
 *
 * Implementation notes:
 *   - Each tile WS is independent — one camera glitching doesn't affect others.
 *   - Frames are throttled in the tile (render every Nth frame) to keep CPU low
 *     for many-tile layouts; modal renders every frame.
 *   - Mobile responsive via CSS grid (1 col / 2 col / 3 col / 4 col).
 *   - Status dots: green = recent frame, yellow = stale (>3s), red = WS closed.
 */

const $ = (id) => document.getElementById(id);

// One CameraTile per registered camera
const TILES = new Map(); // camera_id -> CameraTile

// Modal state — only one camera in modal at a time
let modalCameraId = null;
let modalWs = null;
let modalFpsFrames = 0;
let modalFpsT0 = 0;

// -----------------------------------------------------------------------------
// CameraTile — encapsulates one tile's DOM + WebSocket
// -----------------------------------------------------------------------------
class CameraTile {
    constructor(camera) {
        this.camera = camera;
        this.id = camera.id;
        this.lastFrameTime = 0;
        this.frameCount = 0;
        this.ws = null;
        this.reconnectDelay = 1000;
        this.staleTimer = null;
        this.numPeople = 0;
        this.element = this.build();
        this.connect();
    }

    build() {
        const el = document.createElement('div');
        el.className = 'tile';
        el.tabIndex = 0;
        el.setAttribute('role', 'button');
        el.setAttribute('aria-label', `Open ${this.camera.name || this.camera.id}`);
        el.innerHTML = `
            <div class="tile-feed">
                <img alt="" />
                <div class="placeholder">connecting...</div>
                <div class="tile-overlay">
                    <span class="tile-status-dot offline"></span>
                    <span class="tile-name">${escapeHtml(this.camera.name || this.camera.id)}</span>
                    <span class="tile-badge">—</span>
                </div>
            </div>
            <div class="tile-footer">
                <span class="meta">${escapeHtml(this.camera.id)}</span>
                <span class="status">offline</span>
            </div>
        `;
        const open = () => openModal(this.id);
        el.addEventListener('click', open);
        el.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });
        this.imgEl = el.querySelector('.tile-feed img');
        this.placeholderEl = el.querySelector('.tile-feed .placeholder');
        this.dotEl = el.querySelector('.tile-status-dot');
        this.badgeEl = el.querySelector('.tile-badge');
        this.statusEl = el.querySelector('.tile-footer .status');
        return el;
    }

    connect() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${window.location.host}/ws/live?camera=${encodeURIComponent(this.id)}`;
        try {
            this.ws = new WebSocket(url);
        } catch (e) {
            this.setStatus('error');
            this.scheduleReconnect();
            return;
        }

        this.ws.onopen = () => {
            this.reconnectDelay = 1000;
            this.setStatus('connecting');
        };

        this.ws.onmessage = (ev) => {
            try {
                const msg = JSON.parse(ev.data);
                if (msg.type !== 'frame' || !msg.frame) return;
                // Throttle tile rendering — every 3rd frame at 10 FPS server side
                // = ~3 FPS on screen, plenty for a thumbnail. Modal still gets all frames.
                this.frameCount++;
                if (modalCameraId !== this.id && (this.frameCount % 3) !== 0) return;
                this.renderFrame(msg);
            } catch (e) { /* ignore parse errors */ }
        };

        this.ws.onerror = () => this.setStatus('error');
        this.ws.onclose = (ev) => {
            this.setStatus('offline');
            // Auth failure (4401) — don't reconnect, send user to login
            if (ev.code === 4401) {
                window.location.href = '/login.html';
                return;
            }
            this.scheduleReconnect();
        };
    }

    renderFrame(msg) {
        if (this.placeholderEl.style.display !== 'none') {
            this.placeholderEl.style.display = 'none';
        }
        this.imgEl.src = `data:image/jpeg;base64,${msg.frame}`;
        this.lastFrameTime = Date.now();
        // num_people from server message
        const n = parseInt(msg.num_people, 10) || 0;
        if (n !== this.numPeople) {
            this.numPeople = n;
            if (n > 0) {
                this.badgeEl.textContent = `${n} 👤`;
                this.badgeEl.classList.add('has-people');
                this.element.classList.add('alert');
                setTimeout(() => this.element.classList.remove('alert'), 1500);
            } else {
                this.badgeEl.textContent = '—';
                this.badgeEl.classList.remove('has-people');
            }
        }
        this.setStatus('online');
        this.resetStaleTimer();
    }

    setStatus(state) {
        this.dotEl.classList.remove('online', 'stale', 'offline');
        this.dotEl.classList.add(state === 'connecting' ? 'stale' : state === 'error' ? 'offline' : state);
        const labels = { online: 'live', stale: 'stale', offline: 'offline', connecting: 'connecting', error: 'error' };
        this.statusEl.textContent = labels[state] || state;
    }

    resetStaleTimer() {
        if (this.staleTimer) clearTimeout(this.staleTimer);
        this.staleTimer = setTimeout(() => this.setStatus('stale'), 3500);
    }

    scheduleReconnect() {
        this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, 10000);
        setTimeout(() => this.connect(), this.reconnectDelay);
    }

    destroy() {
        if (this.ws) try { this.ws.close(); } catch (e) {}
        if (this.staleTimer) clearTimeout(this.staleTimer);
    }
}

function escapeHtml(s) {
    return String(s).replace(/[<>&"']/g, c => ({ '<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;' }[c]));
}

// -----------------------------------------------------------------------------
// Modal — fullscreen view of one camera (its own WS, full FPS)
// -----------------------------------------------------------------------------
function openModal(cameraId) {
    const tile = TILES.get(cameraId);
    if (!tile) return;
    modalCameraId = cameraId;
    $('modalTitle').textContent = tile.camera.name || cameraId;
    $('modalStatus').textContent = 'connecting...';
    $('modalFps').textContent = '— FPS';
    $('modalImg').src = '';
    $('modalSingleLink').href = `/single.html?camera=${encodeURIComponent(cameraId)}`;
    $('modal').classList.add('open');

    // Open a dedicated WS at full FPS for the modal
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${window.location.host}/ws/live?camera=${encodeURIComponent(cameraId)}`;
    modalWs = new WebSocket(url);
    modalFpsFrames = 0;
    modalFpsT0 = Date.now();
    modalWs.onmessage = (ev) => {
        try {
            const msg = JSON.parse(ev.data);
            if (msg.type !== 'frame' || !msg.frame) return;
            $('modalImg').src = `data:image/jpeg;base64,${msg.frame}`;
            $('modalStatus').textContent = `live · ${msg.num_people || 0} people`;
            modalFpsFrames++;
            const dt = Date.now() - modalFpsT0;
            if (dt > 1000) {
                $('modalFps').textContent = `${Math.round(modalFpsFrames * 1000 / dt)} FPS`;
                modalFpsFrames = 0;
                modalFpsT0 = Date.now();
            }
        } catch (e) {}
    };
    modalWs.onerror = () => $('modalStatus').textContent = 'error';
    modalWs.onclose = (ev) => {
        if (ev.code === 4401) window.location.href = '/login.html';
        else $('modalStatus').textContent = 'disconnected';
    };
}

function closeModal(event) {
    // Allow `onclick="closeModal(event)"` on backdrop to close, but not on inner content
    if (event && event.target.id !== 'modal' && event.currentTarget !== event.target) return;
    if (modalWs) {
        try { modalWs.close(); } catch (e) {}
        modalWs = null;
    }
    modalCameraId = null;
    $('modal').classList.remove('open');
}

// ESC closes modal
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
});

// -----------------------------------------------------------------------------
// Initial load + periodic refresh: fetch cameras, build/update tiles.
// The refresh polls every 30s so new cameras added on /cameras.html show up
// here without a manual tab reload. We diff against the existing tile set so
// established tiles + their WebSockets keep running; only added/removed
// cameras trigger DOM changes.
// -----------------------------------------------------------------------------
async function loadCameras() {
    try {
        const res = await fetch('/api/cameras');
        if (res.status === 401) { window.location.href = '/login.html'; return; }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        reconcileTiles(data.cameras || []);
    } catch (e) {
        // Don't blow away the grid on a transient fetch failure — only
        // overwrite if the grid is empty (first-load failure).
        const grid = $('grid');
        if (!grid.children.length || grid.querySelector('.empty')) {
            grid.innerHTML = `<div class="empty">Failed to load cameras: ${escapeHtml(e.message)}</div>`;
        }
    }
}

function reconcileTiles(cameras) {
    const grid = $('grid');
    // Build the desired-set (enabled cameras only, ordered)
    const desired = cameras.filter(c => c.enabled !== false);
    const desiredIds = new Set(desired.map(c => c.id));

    // Empty-state handling: if nothing to show, blow away tiles + display message
    if (desired.length === 0) {
        for (const tile of TILES.values()) tile.destroy();
        TILES.clear();
        grid.innerHTML = `<div class="empty">No cameras registered yet. <a href="/cameras.html">Add one →</a></div>`;
        return;
    }

    // Remove the empty-state if present (first time we have cameras)
    const emptyEl = grid.querySelector('.empty');
    if (emptyEl) emptyEl.remove();

    // Tear down tiles for cameras no longer in the desired set
    for (const [id, tile] of TILES) {
        if (!desiredIds.has(id)) {
            tile.destroy();
            if (tile.element.parentNode) tile.element.parentNode.removeChild(tile.element);
            TILES.delete(id);
        }
    }

    // Add tiles for new cameras (preserving order from the API response)
    for (const cam of desired) {
        if (!TILES.has(cam.id)) {
            const tile = new CameraTile(cam);
            TILES.set(cam.id, tile);
            grid.appendChild(tile.element);
        }
    }

    // Re-apply layout-hint classes based on current count
    grid.classList.remove('cams-3-plus', 'cams-5-plus');
    if (desired.length >= 3) grid.classList.add('cams-3-plus');
    if (desired.length >= 5) grid.classList.add('cams-5-plus');
}

// Clean up tiles when leaving the page
window.addEventListener('beforeunload', () => {
    for (const tile of TILES.values()) tile.destroy();
});

document.addEventListener('DOMContentLoaded', () => {
    loadCameras();
    // Re-poll every 30s so newly-added or paused cameras show up without
    // a manual reload. The reconcile is a diff, so existing tiles + their
    // WebSocket connections are untouched.
    setInterval(loadCameras, 30_000);
});
