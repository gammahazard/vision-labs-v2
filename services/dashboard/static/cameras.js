/**
 * static/cameras.js — Camera registry management UI.
 *
 * Talks to:
 *   GET    /api/cameras              — list
 *   POST   /api/cameras              — register / update
 *   DELETE /api/cameras/{id}         — remove
 *   POST   /api/cameras/test-rtsp    — ffprobe a URL
 */

const $ = (id) => document.getElementById(id);

function showMsg(text, kind = 'ok') {
    const el = $('camMsg');
    el.textContent = text;
    el.className = `cam-msg show ${kind}`;
    if (kind === 'ok') {
        setTimeout(() => el.classList.remove('show'), 4000);
    }
}

async function loadCameras() {
    try {
        const res = await fetch('/api/cameras');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        renderCameras(data.cameras || []);
    } catch (e) {
        $('camList').innerHTML = `<div class="cam-empty">Failed to load: ${e.message}</div>`;
    }
}

function renderCameras(cameras) {
    $('camCount').textContent = `${cameras.length} camera${cameras.length === 1 ? '' : 's'}`;
    const list = $('camList');
    if (!cameras.length) {
        list.innerHTML = '<div class="cam-empty">No cameras registered yet.</div>';
        return;
    }
    list.innerHTML = cameras.map(c => {
        const subUrl = (c.rtsp_sub || '').replace(/:[^@/]+@/, ':***@');
        const loc = (c.location_lat && c.location_lon) ? `${c.location_lat}, ${c.location_lon}` : 'no location';
        const enabled = c.enabled !== false;
        const dets = [];
        if (c.detect_persons !== false) dets.push('persons');
        if (c.detect_vehicles !== false) dets.push('vehicles');
        if (c.detect_faces !== false) dets.push('faces');
        const detStr = dets.length ? dets.join(' · ') : 'no detectors';
        return `
            <div class="cam-item" data-camera-id="${escape(c.id)}">
                <div class="cam-item-info">
                    <div class="cam-item-name">
                        ${escape(c.name || c.id)}
                        <span style="color:#64748b;font-weight:400;font-size:0.75rem;">· ${escape(c.id)}</span>
                        <span class="cam-status" id="status-${escape(c.id)}"
                              style="margin-left:0.5rem;font-size:0.7rem;padding:2px 8px;border-radius:10px;background:#1e293b;color:#94a3b8;vertical-align:middle;">…</span>
                    </div>
                    <div class="cam-item-meta">${escape(subUrl)}</div>
                    <div class="cam-item-meta" style="margin-top:0.15rem;">${escape(loc)} · ${escape(detStr)}</div>
                </div>
                <div class="cam-item-actions" style="flex-direction:column;align-items:flex-end;gap:0.35rem;">
                    <label style="display:flex;align-items:center;gap:0.4rem;font-size:0.78rem;color:#94a3b8;cursor:pointer;">
                        <input type="checkbox" ${enabled ? 'checked' : ''}
                               onchange="handleEnableToggle('${escape(c.id)}', this.checked)"
                               style="width:14px;height:14px;cursor:pointer;">
                        ${enabled ? 'enabled' : 'paused'}
                    </label>
                    <button class="cam-btn cam-btn-danger"
                            onclick="handleDelete('${escape(c.id)}', '${escape(c.name || c.id)}')"
                            style="padding:0.3rem 0.65rem;font-size:0.78rem;">Delete</button>
                </div>
            </div>
        `;
    }).join('');

    // Kick off live status polling for every camera tile we just rendered.
    for (const c of cameras) refreshCameraStatus(c.id);
}

// ---------------------------------------------------------------------------
// Status badge — polls /api/cameras/{id}/status, paints the small pill.
// Slot cameras (cam2..cam5) show real orchestrator state; front_door
// always shows "running" since it's not slot-gated.
// ---------------------------------------------------------------------------
async function refreshCameraStatus(camId) {
    try {
        const res = await fetch(`/api/cameras/${encodeURIComponent(camId)}/status`);
        if (!res.ok) return;
        const data = await res.json();
        paintStatusBadge(camId, data);
    } catch (e) { /* silent */ }
}

function paintStatusBadge(camId, data) {
    const el = document.getElementById(`status-${camId}`);
    if (!el) return;
    // Decide label + color
    let label = 'running';
    let bg = 'rgba(34,197,94,0.15)';
    let fg = '#4ade80';

    if (data.enabled === false) {
        label = 'paused';
        bg = 'rgba(148,163,184,0.18)';
        fg = '#94a3b8';
    } else if (data.slot) {
        // Slot camera — look at the orchestrator action
        const a = data.latest_action;
        if (!a) {
            label = 'pending';
            bg = 'rgba(245,158,11,0.18)';
            fg = '#fbbf24';
        } else if (!a.success) {
            label = `${a.action} failed`;
            bg = 'rgba(239,68,68,0.18)';
            fg = '#f87171';
            el.title = a.detail || 'see orchestrator logs';
        } else if (a.action === 'up') {
            label = 'running';
        } else if (a.action === 'down') {
            label = 'stopped';
            bg = 'rgba(148,163,184,0.18)';
            fg = '#94a3b8';
        }
    }
    el.textContent = label;
    el.style.background = bg;
    el.style.color = fg;
}

async function handleEnableToggle(camId, enabled) {
    try {
        const res = await fetch(`/api/cameras/${encodeURIComponent(camId)}/enabled`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        });
        const data = await res.json();
        if (data.ok) {
            showMsg(enabled ? `▶ Enabled — services starting…` : `⏸ Paused — services stopping…`, 'ok');
            // Wait a moment for the orchestrator to react, then refresh the badge
            setTimeout(() => refreshCameraStatus(camId), 1500);
            setTimeout(loadCameras, 5000);
        } else {
            showMsg(`✗ ${data.error || 'toggle failed'}`, 'err');
        }
    } catch (e) {
        showMsg(`✗ ${e.message}`, 'err');
    }
}

function escape(s) {
    return String(s).replace(/[<>&"']/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ---------------------------------------------------------------------------
// ONVIF discovery (Phase D.5) — same backend as the wizard's scan flow
// ---------------------------------------------------------------------------
let _camTabDiscovered = [];

async function handleDiscover() {
    const cidrInput = $('discoverCidr');
    const msgEl = $('discoverMsg');
    const listEl = $('discoverList');

    msgEl.textContent = '⏳ Probing every IP in the subnet (~5-10s for a /24)...';
    msgEl.className = 'cam-msg';
    listEl.innerHTML = '';

    const cidr = cidrInput.value.trim();
    try {
        const resp = await fetch('/api/cameras/discover', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cidr ? { cidr } : {}),
        });
        const data = await resp.json();
        if (!resp.ok) {
            msgEl.textContent = `Scan failed: ${data.error || resp.status}`;
            msgEl.className = 'cam-msg err';
            return;
        }
        if (data.cidr && !cidrInput.value) cidrInput.value = data.cidr;
        _camTabDiscovered = data.cameras || [];
        if (_camTabDiscovered.length === 0) {
            msgEl.textContent = `No ONVIF cameras found on ${data.cidr}. Use the manual form below.`;
            return;
        }
        msgEl.textContent = `Found ${_camTabDiscovered.length} ONVIF camera${_camTabDiscovered.length === 1 ? '' : 's'} on ${data.cidr}. Click one to fill in the form.`;
        msgEl.className = 'cam-msg ok';

        _camTabDiscovered.forEach((cam, idx) => {
            const brand = cam.manufacturer && cam.manufacturer !== 'Streaming' ? cam.manufacturer : '';
            const title = [brand, cam.model || cam.hardware, cam.name].filter(Boolean).join(' · ') || 'ONVIF device';
            const card = document.createElement('div');
            card.style.cssText = 'background:#0f172a;border:1px solid #334155;border-radius:6px;padding:0.6rem 0.9rem;cursor:pointer;';
            card.onmouseover = () => card.style.borderColor = '#4ade80';
            card.onmouseout = () => card.style.borderColor = '#334155';
            card.innerHTML = `
                <div style="color:#e2e8f0;font-weight:600;font-size:0.9rem;">${escape(title)}</div>
                <div style="color:#94a3b8;font-size:0.78rem;margin-top:2px;">${escape(cam.ip)} — ${escape(cam.xaddrs[0] || '')}</div>
            `;
            card.onclick = () => promptOnvifCreds(idx);
            listEl.appendChild(card);
        });
    } catch (e) {
        msgEl.textContent = `Network error: ${e.message}`;
        msgEl.className = 'cam-msg err';
    }
}

async function promptOnvifCreds(idx) {
    const cam = _camTabDiscovered[idx];
    if (!cam) return;
    const username = prompt(`Username for ${cam.manufacturer || ''} ${cam.model || cam.hardware || cam.ip}:`, 'admin');
    if (!username) return;
    const password = prompt(`Password for ${username}@${cam.ip}:`);
    if (password === null) return;

    showMsg('⏳ Calling ONVIF GetStreamUri to fetch the RTSP URL...', 'ok');
    try {
        const resp = await fetch('/api/cameras/onvif-stream-uri', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_url: cam.xaddrs[0], username, password }),
        });
        const data = await resp.json();
        if (!data.ok) {
            showMsg(`✗ ONVIF auth failed: ${data.error}`, 'err');
            return;
        }
        const urls = data.rtsp_urls || [];
        let sub = urls.find(u => /sub|02|low/i.test(u)) || urls[urls.length - 1];
        let main = urls.find(u => /main|01|high/i.test(u)) || urls[0];
        if (sub === main && urls.length > 1) main = urls.find(u => u !== sub);

        // Prefill the manual add-camera form
        const idGuess = (cam.name || cam.ip).toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
        $('camId').value = idGuess;
        $('camName').value = `${cam.manufacturer || ''} ${cam.model || ''}`.trim() || cam.ip;
        $('camRtspSub').value = sub || '';
        $('camRtspMain').value = (main && main !== sub) ? main : '';
        showMsg(`✓ Got RTSP URLs from ${cam.ip}. Review the form below + click Save.`, 'ok');
        $('camName').scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (e) {
        showMsg(`Network error: ${e.message}`, 'err');
    }
}


async function handleTestRtsp() {
    const url = $('camRtspSub').value.trim();
    if (!url) {
        showMsg('Enter the sub-stream URL before testing', 'err');
        return;
    }
    showMsg('Testing connection — give it 5-10 seconds...', 'ok');
    try {
        const res = await fetch('/api/cameras/test-rtsp', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });
        const data = await res.json();
        if (data.ok) {
            showMsg(`✓ Connected — ${data.codec || 'unknown codec'} ${data.width || '?'}x${data.height || '?'} @ ${data.fps || '?'} FPS`, 'ok');
        } else {
            showMsg(`✗ Test failed: ${data.error || 'unknown error'}`, 'err');
        }
    } catch (e) {
        showMsg(`✗ Test failed: ${e.message}`, 'err');
    }
}

async function handleAddCamera(event) {
    event.preventDefault();
    const id = $('camId').value.trim();
    const name = $('camName').value.trim();
    const rtsp_sub = $('camRtspSub').value.trim();
    const rtsp_main = $('camRtspMain').value.trim();
    const lat = parseFloat($('camLat').value) || 0;
    const lon = parseFloat($('camLon').value) || 0;

    if (!id || !name || !rtsp_sub) {
        showMsg('ID, Name, and RTSP sub-stream URL are required', 'err');
        return;
    }

    const btn = $('addBtn');
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        const body = { id, name, rtsp_sub };
        if (rtsp_main) body.rtsp_main = rtsp_main;
        if (lat) body.location_lat = lat;
        if (lon) body.location_lon = lon;
        body.detect_persons = $('camDetectPersons').checked;
        body.detect_vehicles = $('camDetectVehicles').checked;
        body.detect_faces = $('camDetectFaces').checked;

        const res = await fetch('/api/cameras', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (data.ok) {
            if (data.activation_cmd) {
                // Phase 7b: orchestrator service automatically brings up the
                // matching profile when we publish to cameras:events (which
                // happens inside the registry upsert). Show provisioning
                // status and let the badge in the list reflect live state.
                showMsg(`✓ Saved "${name}" as slot ${id}. Provisioning services — watch the status badge in the list (can take ~30-60s for the first start).`, 'ok');
                // Poll the status badge aggressively while the orchestrator
                // brings services up. Container startup can take 30-60s on
                // first run (image pulls, GPU warmup). Poll every 3 seconds
                // for the first 90 seconds, then stop — the periodic load
                // takes over after that.
                let polls = 0;
                const poller = setInterval(() => {
                    refreshCameraStatus(id);
                    polls++;
                    if (polls >= 30) clearInterval(poller); // 30 × 3s = 90s
                }, 3000);
            } else {
                showMsg(`✓ Saved "${name}". Custom ID — services must be added to docker-compose.yml manually.`, 'ok');
            }
            $('addCameraForm').reset();
            loadNextSlot();
            loadCameras();
        } else {
            showMsg(`✗ Save failed: ${data.error || 'unknown error'}`, 'err');
        }
    } catch (e) {
        showMsg(`✗ Save failed: ${e.message}`, 'err');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save Camera';
    }
}

async function handleDelete(id, name) {
    if (!confirm(`Delete camera "${name}"?\n\nThe orchestrator will stop its detection services within seconds. The face DB is shared across cameras and is NOT affected by this delete.`)) return;
    try {
        const res = await fetch(`/api/cameras/${encodeURIComponent(id)}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.ok) {
            showMsg(`✓ Deleted "${name}"`, 'ok');
            loadCameras();
        } else {
            showMsg(`✗ Delete failed: ${data.error || 'unknown error'}`, 'err');
        }
    } catch (e) {
        showMsg(`✗ Delete failed: ${e.message}`, 'err');
    }
}

async function loadNextSlot() {
    // Pre-fill the Camera ID field with the next available pre-defined slot
    // (cam2/cam3/...). User can override if they want a custom slot name and
    // manually add their own services to compose.
    try {
        const res = await fetch('/api/cameras/next-slot');
        const data = await res.json();
        if (data.slot) {
            $('camId').value = data.slot;
            $('camId').placeholder = data.slot;
        } else {
            $('camId').placeholder = 'all slots used';
        }
    } catch (e) { /* best-effort */ }
}

document.addEventListener('DOMContentLoaded', () => {
    loadCameras();
    loadNextSlot();
});
