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
        return `
            <div class="cam-item">
                <div class="cam-item-info">
                    <div class="cam-item-name">${escape(c.name || c.id)} <span style="color:#64748b;font-weight:400;font-size:0.75rem;">· ${escape(c.id)}</span></div>
                    <div class="cam-item-meta">${escape(subUrl)} · ${escape(loc)} · ${enabled ? 'enabled' : 'disabled'}</div>
                </div>
                <div class="cam-item-actions">
                    <button class="cam-btn cam-btn-danger" onclick="handleDelete('${escape(c.id)}', '${escape(c.name || c.id)}')">Delete</button>
                </div>
            </div>
        `;
    }).join('');
}

function escape(s) {
    return String(s).replace(/[<>&"']/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c]));
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

        const res = await fetch('/api/cameras', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (data.ok) {
            showMsg(`✓ Saved camera "${name}". Note: detector services for this camera will be spawned in Phase 7b.`, 'ok');
            $('addCameraForm').reset();
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
    if (!confirm(`Delete camera "${name}"? This only removes it from the registry — running services won't be stopped.`)) return;
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

document.addEventListener('DOMContentLoaded', loadCameras);
