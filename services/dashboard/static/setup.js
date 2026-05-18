/**
 * services/dashboard/static/setup.js — first-run wizard logic.
 *
 * Flow:
 *   welcome -> hardware -> camera -> finish
 *
 * State (kept in memory; persisted only via /api/setup/complete at the end):
 *   - detected: hardware probe result (gpus list)
 *   - tier:     recommended tier ("small" | "mid" | "full" | "no-gpu")
 *   - cameraAdded: bool
 */

const state = {
    current: 'welcome',
    detected: null,
    tier: null,
    cameraAdded: false,
    cameraSkipped: false,
};

const STEPS = ['welcome', 'hardware', 'camera', 'finish'];

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------
function showStep(step) {
    state.current = step;
    document.querySelectorAll('.wizard-step').forEach(s => {
        s.classList.toggle('active', s.dataset.step === step);
    });
    const stepIdx = STEPS.indexOf(step);
    document.querySelectorAll('#stepIndicator li').forEach(li => {
        const idx = STEPS.indexOf(li.dataset.step);
        li.classList.toggle('active', idx === stepIdx);
        li.classList.toggle('completed', idx < stepIdx);
    });
}

// ---------------------------------------------------------------------------
// Step 2 — Hardware
// ---------------------------------------------------------------------------
async function detectHardware() {
    const statusEl = document.getElementById('hardwareStatus');
    const resultEl = document.getElementById('hardwareResult');
    const nextBtn = document.getElementById('btnNext2');

    statusEl.innerHTML = '<span class="probing">⏳ Asking the orchestrator to spawn an nvidia-smi probe — this can take up to ~30s on first run...</span>';

    try {
        const resp = await fetch('/api/setup/detect-hardware', { method: 'POST' });
        const data = await resp.json();
        renderHardwareResult(data);
        state.detected = data;
        // Allow Continue regardless — wizard works without a detected GPU
        // (small tier with no AI chat still gives detection + DVR + faces)
        nextBtn.disabled = false;
        statusEl.style.display = 'none';
        resultEl.hidden = false;
    } catch (e) {
        statusEl.innerHTML = `<span class="probing" style="color:#ef4444">Probe failed: ${e}. You can still continue — set DETECTOR_GPU / CHAT_GPU manually in .env later.</span>`;
        nextBtn.disabled = false;  // let the user proceed
    }
}

function renderHardwareResult(data) {
    const resultEl = document.getElementById('hardwareResult');
    if (data.error && (!data.gpus || data.gpus.length === 0)) {
        resultEl.innerHTML = `
            <p class="error">No GPUs detected: ${escapeHtml(data.error)}</p>
            <p>You can still run Vision Labs in CPU-only mode, but inference will be 10-100× slower. The dashboard works, the AI chat may not. Recommend installing NVIDIA Container Toolkit and running this wizard again on a machine with an NVIDIA GPU.</p>
        `;
        state.tier = 'no-gpu';
        return;
    }
    // Render GPU table
    const rows = data.gpus.map(g => `
        <tr>
            <td>GPU ${g.index}</td>
            <td>${escapeHtml(g.name)}</td>
            <td>${(g.vram_mb/1024).toFixed(1)} GB</td>
        </tr>
    `).join('');
    const maxVram = Math.max(...data.gpus.map(g => g.vram_mb));
    const tier = recommendTier(maxVram);
    const slots = estimateSlots(maxVram, tier);
    state.tier = tier;

    // Concrete model names per tier so users with weak GPUs know what to set
    // in CHAT_MODEL if they want chat at all (tiers/small.env disables it
    // by default but offers commented options).
    const tierBlurbs = {
        small: 'Small tier (6 GB GPU) — nano YOLO models, AI chat disabled by default. To enable a tiny chat model, set <code>CHAT_MODEL=qwen3:1.7b</code> in .env (~1.5 GB VRAM). Apply with <code>cat tiers/small.env >> .env</code>.',
        mid: 'Mid tier (8-12 GB GPU) — standard "s" YOLO models. Chat options: <code>qwen3:3b</code> (~2 GB) or <code>qwen3:7b</code> (~5 GB). Defaults to qwen3:7b via <code>cat tiers/mid.env >> .env</code>.',
        full: 'Full tier (16+ GB) — Qwen 3 14B chat (~9 GB) + MiniCPM-V vision LLM (~5 GB). Apply with <code>cat tiers/full.env >> .env</code>.',
    };

    resultEl.innerHTML = `
        <table>
            <thead><tr><th>GPU</th><th>Model</th><th>VRAM</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
        <div class="recommended">
            <strong>Recommended tier:</strong> <code>${tier}</code><br>
            <strong>Estimated camera capacity:</strong> ${slots} ${slots === 1 ? 'camera' : 'cameras'} (with default detector mix)<br>
            ${tierBlurbs[tier]}
        </div>
    `;
}

function recommendTier(vramMb) {
    if (vramMb < 7000) return 'small';
    if (vramMb < 14000) return 'mid';
    return 'full';
}

function estimateSlots(vramMb, tier) {
    const vramGb = vramMb / 1024;
    // ~2.8 GB per camera with 's' models, ~2.0 GB with 'n' models
    const perCam = tier === 'small' ? 2.0 : 2.8;
    // Chat model overhead
    const chatGb = { small: 0, mid: 5, full: 9 }[tier];
    const buffer = 1.0;  // headroom for spikes
    const available = vramGb - chatGb - buffer;
    return Math.max(1, Math.floor(available / perCam));
}

// ---------------------------------------------------------------------------
// Step 3 — First camera: ONVIF discovery
// ---------------------------------------------------------------------------
let _discoveredCameras = [];
let _onvifSelected = null;  // camera the user clicked

async function discoverCameras() {
    const cidrInput = document.getElementById('discoverCidr');
    const statusEl = document.getElementById('discoverStatus');
    const resultsEl = document.getElementById('discoverResults');

    statusEl.hidden = false;
    resultsEl.hidden = true;
    resultsEl.innerHTML = '';
    statusEl.textContent = '⏳ Probing every IP in the subnet (this takes ~5-10s for a /24)...';

    const cidr = cidrInput.value.trim();
    try {
        const resp = await fetch('/api/setup/discover-cameras', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cidr ? { cidr } : {}),
        });
        const data = await resp.json();
        if (!resp.ok) {
            statusEl.textContent = `Scan failed: ${data.error || resp.status}`;
            return;
        }

        // Fill in the auto-detected CIDR so the user sees what we scanned
        if (data.cidr && !cidrInput.value) cidrInput.value = data.cidr;

        _discoveredCameras = data.cameras || [];
        if (_discoveredCameras.length === 0) {
            statusEl.textContent = `No ONVIF cameras found on ${data.cidr}. If you have ONVIF cameras, make sure ONVIF is enabled in their settings. Otherwise expand "enter RTSP URL manually" below.`;
            return;
        }

        statusEl.textContent = `Found ${_discoveredCameras.length} ONVIF camera${_discoveredCameras.length === 1 ? '' : 's'} on ${data.cidr}. Click one to connect.`;
        resultsEl.hidden = false;
        _discoveredCameras.forEach((cam, idx) => {
            const card = document.createElement('div');
            card.className = 'discover-card';
            card.dataset.idx = idx;
            const brand = cam.manufacturer && cam.manufacturer !== 'Streaming' ? cam.manufacturer : '';
            const title = [brand, cam.model || cam.hardware, cam.name].filter(Boolean).join(' · ') || 'ONVIF device';
            card.innerHTML = `
                <div class="discover-card-title">${escapeHtml(title)}</div>
                <div class="discover-card-meta">${escapeHtml(cam.ip)} — ${escapeHtml((cam.xaddrs[0] || '').replace(/\?.*/, ''))}</div>
            `;
            card.addEventListener('click', () => openOnvifModal(cam));
            resultsEl.appendChild(card);
        });
    } catch (e) {
        statusEl.textContent = `Network error: ${e}`;
    }
}

function openOnvifModal(cam) {
    _onvifSelected = cam;
    document.getElementById('onvifCredsTitle').textContent =
        `${cam.manufacturer || ''} ${cam.model || cam.hardware || 'camera'} at ${cam.ip}`.trim();
    document.getElementById('onvifPassword').value = '';
    document.getElementById('onvifCredsResult').hidden = true;
    document.getElementById('onvifCredsModal').hidden = false;
    setTimeout(() => document.getElementById('onvifPassword').focus(), 100);
}

function closeOnvifModal() {
    document.getElementById('onvifCredsModal').hidden = true;
    _onvifSelected = null;
}

async function connectOnvif() {
    if (!_onvifSelected) return;
    const username = document.getElementById('onvifUsername').value.trim();
    const password = document.getElementById('onvifPassword').value;
    const resultEl = document.getElementById('onvifCredsResult');

    if (!password) {
        resultEl.hidden = false;
        resultEl.className = 'rtsp-test-result err';
        resultEl.textContent = 'Password is required.';
        return;
    }

    resultEl.hidden = false;
    resultEl.className = 'rtsp-test-result';
    resultEl.textContent = '⏳ Calling ONVIF GetStreamUri...';

    try {
        const resp = await fetch('/api/cameras/onvif-stream-uri', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                device_url: _onvifSelected.xaddrs[0],
                username,
                password,
            }),
        });
        const data = await resp.json();
        if (!data.ok) {
            resultEl.className = 'rtsp-test-result err';
            resultEl.textContent = `✗ ${data.error || 'Failed to fetch stream URL.'}`;
            return;
        }
        // Prefill the manual form with the discovered URLs and open it.
        const urls = data.rtsp_urls || [];
        // Most cameras return [main, sub] or [sub, main] — heuristic: shorter
        // URL or one containing "sub"/"02" is usually the sub-stream.
        let sub = urls.find(u => /sub|02|low/i.test(u)) || urls[urls.length - 1];
        let main = urls.find(u => /main|01|high/i.test(u)) || urls[0];
        if (sub === main && urls.length > 1) main = urls.find(u => u !== sub);

        document.getElementById('camName').value =
            `${_onvifSelected.manufacturer || ''} ${_onvifSelected.model || ''}`.trim() || _onvifSelected.ip;
        document.getElementById('camRtspSub').value = sub || '';
        document.getElementById('camRtspMain').value = (main && main !== sub) ? main : '';

        // Open the manual form so the user can review + click Add
        document.querySelector('.manual-fallback').open = true;
        closeOnvifModal();
        document.getElementById('camName').focus();
    } catch (e) {
        resultEl.className = 'rtsp-test-result err';
        resultEl.textContent = `✗ Network error: ${e}`;
    }
}

async function testRtsp() {
    const url = document.getElementById('camRtspSub').value.trim();
    const resultEl = document.getElementById('rtspTestResult');
    if (!url) {
        resultEl.hidden = false;
        resultEl.className = 'rtsp-test-result err';
        resultEl.textContent = 'Enter an RTSP URL first.';
        return;
    }
    resultEl.hidden = false;
    resultEl.className = 'rtsp-test-result';
    resultEl.textContent = '⏳ Probing — this calls ffprobe and can take 5-10s...';

    try {
        const resp = await fetch('/api/cameras/test-rtsp', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rtsp_url: url }),
        });
        const data = await resp.json();
        if (data.ok) {
            resultEl.className = 'rtsp-test-result ok';
            resultEl.textContent = `✓ Connected. Resolution: ${data.width}×${data.height} @ ${data.fps} fps, codec ${data.codec_name}.`;
        } else {
            resultEl.className = 'rtsp-test-result err';
            resultEl.textContent = `✗ ${data.error || 'Connection failed.'}`;
        }
    } catch (e) {
        resultEl.className = 'rtsp-test-result err';
        resultEl.textContent = `✗ Network error: ${e}`;
    }
}

async function addCamera() {
    const name = document.getElementById('camName').value.trim();
    const rtspSub = document.getElementById('camRtspSub').value.trim();
    const rtspMain = document.getElementById('camRtspMain').value.trim();

    if (!name || !rtspSub) {
        alert('Camera name and RTSP sub-stream URL are required.');
        return;
    }

    // Generate a simple ID from the name
    const id = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    if (!id) {
        alert('Name must contain alphanumeric characters.');
        return;
    }

    const body = {
        id,
        name,
        rtsp_sub: rtspSub,
        rtsp_main: rtspMain || null,
        enabled: true,
        detect_persons: document.getElementById('detectPersons').checked,
        detect_vehicles: document.getElementById('detectVehicles').checked,
        detect_faces: document.getElementById('detectFaces').checked,
        gpu_id: 0,
    };

    try {
        const resp = await fetch('/api/cameras', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (resp.ok) {
            state.cameraAdded = true;
            showStep('finish');
            renderFinishSummary();
        } else {
            const data = await resp.json();
            alert(`Couldn't add camera: ${data.error || resp.status}`);
        }
    } catch (e) {
        alert(`Network error: ${e}`);
    }
}

function skipCamera() {
    state.cameraSkipped = true;
    showStep('finish');
    renderFinishSummary();
}

// ---------------------------------------------------------------------------
// Step 4 — Finish
// ---------------------------------------------------------------------------
function renderFinishSummary() {
    const el = document.getElementById('finishSummary');
    const parts = [];

    if (state.tier === 'no-gpu') {
        parts.push('Hardware detection found no NVIDIA GPU — Vision Labs will run in CPU-only mode (slow).');
    } else if (state.detected && state.detected.gpus.length > 0) {
        const names = state.detected.gpus.map(g => g.name).join(', ');
        parts.push(`Detected: <strong>${escapeHtml(names)}</strong>. Recommended tier: <code>${state.tier}</code>.`);
    }

    if (state.cameraAdded) {
        parts.push('Your first camera was added. The orchestrator will spawn its detector services within ~10 seconds.');
    } else if (state.cameraSkipped) {
        parts.push('Camera setup skipped — add cameras any time via the Cameras tab.');
    }

    el.innerHTML = parts.join(' ');
}

async function finishWizard() {
    const steps = [];
    if (state.detected) steps.push('hardware_detected');
    if (state.cameraAdded) steps.push('camera_added');
    else if (state.cameraSkipped) steps.push('camera_skipped');
    steps.push('finished');

    try {
        await fetch('/api/setup/complete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                steps,
                hardware: state.detected || {},
            }),
        });
    } catch (e) {
        console.warn('Could not call /api/setup/complete:', e);
    }
    window.location.href = '/';
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// ---------------------------------------------------------------------------
// Wire up
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('btnNext1').addEventListener('click', () => showStep('hardware'));
    document.getElementById('btnBack2').addEventListener('click', () => showStep('welcome'));
    document.getElementById('btnNext2').addEventListener('click', () => showStep('camera'));
    document.getElementById('btnBack3').addEventListener('click', () => showStep('hardware'));

    document.getElementById('btnDetect').addEventListener('click', detectHardware);
    document.getElementById('btnTestRtsp').addEventListener('click', testRtsp);
    document.getElementById('btnAddCamera').addEventListener('click', addCamera);
    document.getElementById('btnSkipCamera').addEventListener('click', skipCamera);
    document.getElementById('btnFinish').addEventListener('click', finishWizard);

    // ONVIF discovery
    document.getElementById('btnDiscover').addEventListener('click', discoverCameras);
    document.getElementById('btnOnvifCancel').addEventListener('click', closeOnvifModal);
    document.getElementById('btnOnvifConnect').addEventListener('click', connectOnvif);
    document.getElementById('onvifCredsModal').addEventListener('click', (e) => {
        if (e.target.id === 'onvifCredsModal') closeOnvifModal();
    });
});
