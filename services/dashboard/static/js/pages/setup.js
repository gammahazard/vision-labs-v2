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
    gpuMode: null,  // 'single' | 'dual' | null — set on hardware-detect step
    cameraAdded: false,
    cameraSkipped: false,
};

const STEPS = ['welcome', 'hardware', 'location', 'camera', 'verify', 'telegram', 'finish'];

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
        // Mark "visited" steps as clickable — anything we've gotten past
        // or are currently on can be jumped back to. Future steps stay
        // dim and uninteractive so the user can't skip ahead unsafely.
        if (idx <= stepIdx) {
            li.classList.add('clickable');
            li.style.cursor = 'pointer';
        } else {
            li.classList.remove('clickable');
            li.style.cursor = 'default';
        }
    });
}

// Step-indicator click handler: let the user jump back to any step they've
// already visited. Forward jumps are blocked to avoid skipping required
// state (e.g. running the hardware probe before the verify step).
function _onStepIndicatorClick(e) {
    const li = e.target.closest('li[data-step]');
    if (!li || !li.classList.contains('clickable')) return;
    const target = li.dataset.step;
    const targetIdx = STEPS.indexOf(target);
    const currentIdx = STEPS.indexOf(state.current);
    if (targetIdx >= 0 && targetIdx <= currentIdx) {
        // Re-running a step that has its own auto-trigger? Honor those.
        showStep(target);
        if (target === 'telegram') showTelegramSubstep('token');
    }
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
        const resp = await fetch('/api/setup/detect-hardware', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
        });
        if (!resp.ok) {
            // Surface HTTP-level errors (auth, 422, 500, etc.) before
            // touching the body so a non-JSON response can't crash us.
            let detail = `HTTP ${resp.status}`;
            try {
                const errBody = await resp.json();
                detail = errBody.error || errBody.detail || detail;
                if (typeof detail !== 'string') detail = JSON.stringify(detail);
            } catch (_) { /* response wasn't JSON; keep HTTP code */ }
            throw new Error(detail);
        }
        const data = await resp.json();
        // Defensive default — server should always send {gpus: []} on no-GPU.
        if (!data || typeof data !== 'object') {
            throw new Error('Malformed server response');
        }
        if (!Array.isArray(data.gpus)) data.gpus = [];
        renderHardwareResult(data);
        state.detected = data;
        nextBtn.disabled = false;
        statusEl.style.display = 'none';
        resultEl.hidden = false;
        // Show "Apply this configuration" once we have something to apply
        if (data.gpus.length > 0) {
            document.getElementById('btnApplyConfig').hidden = false;
        }
    } catch (e) {
        statusEl.innerHTML = `<span class="probing" style="color:#ef4444">Probe failed: ${escapeHtml(e.message || String(e))}. You can still continue — set DETECTOR_GPU / CHAT_GPU manually in .env later.</span>`;
        nextBtn.disabled = false;  // let the user proceed regardless
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

    // Find the biggest GPU (preferred for chat LLM in dual mode)
    const sorted = [...data.gpus].sort((a, b) => b.vram_mb - a.vram_mb);
    const biggest = sorted[0];
    const second = sorted[1] || null;

    const biggestTier = recommendTier(biggest.vram_mb);
    state.tier = biggestTier;

    const tierBlurbs = {
        small: 'Small tier (6 GB) — nano YOLO models, AI chat disabled by default. Set <code>CHAT_MODEL=qwen3:1.7b</code> (~1.5 GB) to enable a tiny chat model.',
        mid: 'Mid tier (8-12 GB) — standard "s" YOLO models. Chat options: <code>qwen3:3b</code> (~2 GB) or <code>qwen3:7b</code> (~5 GB).',
        full: 'Full tier (16+ GB) — Qwen 3 14B chat (~9 GB) + MiniCPM-V vision LLM (~5 GB).',
    };

    // Build the GPU-mode chooser only when there are 2+ GPUs.
    let modeChooserHtml = '';
    if (data.gpus.length >= 2) {
        // Option A: single-GPU on the biggest card
        const singleSlots = estimateSlots(biggest.vram_mb, biggestTier);
        // Option B: dual — detectors on smaller card, chat on bigger card.
        // Detectors don't carry the chat overhead, so the smaller card hosts
        // however many cameras its VRAM can fit at the chosen detector tier.
        const detectorTier = recommendTier(second.vram_mb);
        const dualSlots = estimateSlots(second.vram_mb, 'small');  // no chat on detector GPU
        // The full tier is achievable on either single (biggest >= 16 GB) or dual.

        modeChooserHtml = `
            <div class="gpu-mode-chooser">
                <strong>You have 2 GPUs. How do you want Vision Labs to use them?</strong>
                <label class="gpu-mode-option">
                    <input type="radio" name="gpuMode" value="single" checked>
                    <div>
                        <div class="opt-title">Use just GPU ${biggest.index} (${escapeHtml(biggest.name)})</div>
                        <div class="opt-meta">
                            Tier: <code>${biggestTier}</code> · ~${singleSlots} ${singleSlots === 1 ? 'camera' : 'cameras'}<br>
                            <code>DETECTOR_GPU=${biggest.index}</code>, <code>CHAT_GPU=${biggest.index}</code>
                        </div>
                    </div>
                </label>
                <label class="gpu-mode-option">
                    <input type="radio" name="gpuMode" value="dual">
                    <div>
                        <div class="opt-title">Split across both GPUs</div>
                        <div class="opt-meta">
                            Detectors on GPU ${second.index} (${escapeHtml(second.name)}) · ~${dualSlots} ${dualSlots === 1 ? 'camera' : 'cameras'}<br>
                            Chat LLM on GPU ${biggest.index} (${escapeHtml(biggest.name)}) — full tier<br>
                            <code>DETECTOR_GPU=${second.index}</code>, <code>CHAT_GPU=${biggest.index}</code>
                        </div>
                    </div>
                </label>
                <p class="wizard-hint">
                    Click <strong>Apply this configuration</strong> below to write these values to <code>.env</code> and recreate the affected services.
                </p>
            </div>
        `;
    }

    const singleSlots = estimateSlots(biggest.vram_mb, biggestTier);
    // When the user opts out of AI chat, VRAM that was reserved for Qwen +
    // MiniCPM-V is freed for detectors. Recompute the slot estimate against
    // that "no chat" budget so the trade-off is visible BEFORE they pick.
    const slotsNoChat = estimateSlots(biggest.vram_mb, 'small');  // 'small' = no chat budget
    const showNoChatToggle = (biggestTier !== 'small') && (slotsNoChat > singleSlots);

    const aiToggleHtml = showNoChatToggle ? `
        <div class="ai-chat-toggle" style="margin-top:14px; padding:12px; background:var(--bg-secondary,#1f2937); border-radius:6px;">
            <label style="display:flex; gap:10px; align-items:flex-start; cursor:pointer;">
                <input type="checkbox" id="prefersMoreCameras" style="margin-top:3px;">
                <div>
                    <strong>I'd rather have more cameras than AI chat.</strong>
                    <div style="font-size:0.9em; color:var(--text-secondary,#9ca3af); margin-top:4px;">
                        Disables the local LLM (Qwen) and vision model (MiniCPM-V) — frees ~14 GB of
                        VRAM. Telegram alerts and detection still work; the AI chat tab will show
                        "disabled on this tier" instead.
                        With chat off, this GPU can run roughly <strong>${slotsNoChat}</strong>
                        ${slotsNoChat === 1 ? 'camera' : 'cameras'} (vs ${singleSlots} with chat on).
                    </div>
                </div>
            </label>
        </div>
    ` : '';

    resultEl.innerHTML = `
        <table>
            <thead><tr><th>GPU</th><th>Model</th><th>VRAM</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
        ${modeChooserHtml}
        <div class="recommended">
            <strong>Recommended tier:</strong> <code id="tierLabel">${biggestTier}</code><br>
            <strong>Estimated camera capacity:</strong>
            <span id="slotsLabel">${singleSlots} ${singleSlots === 1 ? 'camera' : 'cameras'}</span>
            (single-GPU mode, with default detector mix)<br>
            <span id="tierBlurb">${tierBlurbs[biggestTier]}</span>
        </div>
        ${aiToggleHtml}
    `;

    // Wire the radios so state.gpuMode + the .env hint update together
    document.querySelectorAll('input[name="gpuMode"]').forEach(radio => {
        radio.addEventListener('change', (e) => {
            state.gpuMode = e.target.value;
        });
    });
    state.gpuMode = 'single';

    // Wire the "more cameras vs AI" checkbox. Re-render the slot count + tier
    // blurb in place so the user can see the trade-off immediately.
    const noChatBox = document.getElementById('prefersMoreCameras');
    if (noChatBox) {
        noChatBox.addEventListener('change', (e) => {
            state.aiDisabled = e.target.checked;
            const tier = e.target.checked ? 'small' : biggestTier;
            const slots = estimateSlots(biggest.vram_mb, tier);
            document.getElementById('tierLabel').textContent = e.target.checked ? `${biggestTier} (chat off)` : biggestTier;
            document.getElementById('slotsLabel').textContent = `${slots} ${slots === 1 ? 'camera' : 'cameras'}`;
            document.getElementById('tierBlurb').innerHTML = e.target.checked
                ? 'AI chat disabled. The chat tab returns "disabled on this tier"; all other features (live grid, Telegram alerts, DVR, face recognition) still work.'
                : tierBlurbs[biggestTier];
        });
    }
    state.aiDisabled = false;
}

// ---------------------------------------------------------------------------
// Step 2 — Apply config
// ---------------------------------------------------------------------------
// Builds a {detector_gpu, chat_gpu, chat_model, ...} payload from the
// current wizard state (probe + radio selection + tier defaults) and
// POSTs it to /api/setup/apply-config. That endpoint writes .env and
// asks the orchestrator to recreate the affected services.
async function applyConfig() {
    const btn = document.getElementById('btnApplyConfig');
    const statusEl = document.getElementById('applyStatus');
    if (!state.detected || !state.detected.gpus.length) return;

    // Figure out what to send based on selected GPU mode.
    const sorted = [...state.detected.gpus].sort((a, b) => b.vram_mb - a.vram_mb);
    const biggest = sorted[0];
    const second = sorted[1] || biggest;

    // Default to single-GPU on the biggest card if mode wasn't picked yet
    const mode = state.gpuMode || 'single';
    const detectorGpu = (mode === 'dual') ? second.index : biggest.index;
    const chatGpu = biggest.index;

    // Model recommendations per tier — same logic as the blurb
    const tier = state.tier || recommendTier(biggest.vram_mb);
    const modelsForTier = {
        small: { chat: '',           vision: '', pose: '/models/yolov8n-pose.pt', vehicle: '/models/yolov8n.pt', fps: '5'  },
        mid:   { chat: 'qwen3:7b',   vision: '', pose: '/models/yolov8s-pose.pt', vehicle: '/models/yolov8s.pt', fps: '10' },
        full:  { chat: 'qwen3:14b',  vision: 'minicpm-v', pose: '/models/yolov8s-pose.pt', vehicle: '/models/yolov8s.pt', fps: '15' },
    };
    const m = { ...(modelsForTier[tier] || modelsForTier.mid) };
    // When the user ticked "more cameras instead of AI chat", null out the
    // LLM model env vars so Ollama doesn't pre-load Qwen / MiniCPM-V and
    // detectors get the full VRAM budget. Detector models still come from
    // the chosen tier so accuracy isn't degraded.
    if (state.aiDisabled) {
        m.chat = '';
        m.vision = '';
    }

    const payload = {
        detector_gpu: String(detectorGpu),
        chat_gpu: String(chatGpu),
        chat_model: m.chat,
        vision_model: m.vision,
        pose_model: m.pose,
        vehicle_model: m.vehicle,
        target_fps: m.fps,
    };

    btn.disabled = true;
    btn.textContent = '⏳ Writing .env + restarting services...';
    statusEl.hidden = false;
    statusEl.className = 'rtsp-test-result';
    statusEl.textContent = '⏳ Writing your configuration to .env. The dashboard and detectors will restart automatically — this can take 30-60 seconds.';

    try {
        const resp = await fetch('/api/setup/apply-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
            statusEl.className = 'rtsp-test-result err';
            statusEl.textContent = `✗ ${data.error || ('HTTP ' + resp.status)}`;
            btn.disabled = false;
            btn.textContent = 'Apply this configuration';
            return;
        }
        statusEl.className = 'rtsp-test-result ok';
        const aff = data.affected_services || [];
        statusEl.innerHTML = `
            ✓ Wrote ${data.written.length} value${data.written.length === 1 ? '' : 's'} to .env: <code>${data.written.join(', ')}</code>.<br>
            Orchestrator is restarting: <code>${aff.join(', ') || '(none)'}</code>. New AI models will download automatically on the next dashboard boot if needed.
        `;
        btn.textContent = '✓ Applied';
        // Leave the button disabled to prevent double-apply
    } catch (e) {
        statusEl.className = 'rtsp-test-result err';
        statusEl.textContent = `✗ Network error: ${escapeHtml(e.message || String(e))}`;
        btn.disabled = false;
        btn.textContent = 'Apply this configuration';
    }
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

    // The internal camera ID must be one of the slot names (cam1...cam5) so
    // the orchestrator can find a matching profile to spawn detector services.
    // Fetch the next free slot — fresh installs get cam1, next add gets cam2.
    let id;
    try {
        const sr = await fetch('/api/cameras/next-slot');
        const sd = await sr.json();
        id = sd.slot;
    } catch (_) {}
    if (!id) {
        alert('No free camera slots. The default install ships with 10 slots (cam1-cam10) — add more by duplicating camN blocks in docker-compose.yml.');
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
            showStep('verify');
            runVerifyStep();
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
    // No camera means nothing to verify — jump straight to the Telegram step.
    // Telegram is still useful for system-status messages even without cameras.
    showStep('telegram');
    showTelegramSubstep('token');
}

// ---------------------------------------------------------------------------
// Step 5 — Verify (smoke check that frames are actually flowing)
// ---------------------------------------------------------------------------
// After the user adds their first camera, the orchestrator needs ~10-15s to
// spin up the per-camera detector services and the ingester needs another
// few seconds to make the first RTSP handshake. We poll /api/stats for each
// registered camera and watch frames_in_stream go up. If it does, the
// camera + credentials are good and the pipeline is end-to-end alive.
async function runVerifyStep() {
    const resultsEl = document.getElementById('verifyResults');
    const continueBtn = document.getElementById('btnVerifyContinue');
    resultsEl.innerHTML = '<p class="probing">⏳ Loading registered cameras…</p>';

    let cams = [];
    try {
        const r = await fetch('/api/cameras');
        cams = (await r.json()).cameras || [];
    } catch (e) {
        resultsEl.innerHTML = `<p class="error">Couldn't fetch camera list: ${escapeHtml(String(e))}</p>`;
        continueBtn.disabled = false;
        return;
    }
    cams = cams.filter(c => c.enabled !== false);
    if (cams.length === 0) {
        resultsEl.innerHTML = '<p>No enabled cameras to verify. Continue when ready.</p>';
        continueBtn.disabled = false;
        return;
    }

    // Render one row per camera with a pending indicator
    resultsEl.innerHTML = cams.map(c => `
        <div class="verify-row" data-cam="${escapeHtml(c.id)}" style="display:flex; align-items:center; gap:10px; padding:8px 0; border-bottom:1px solid rgba(255,255,255,0.08);">
            <span class="verify-icon" style="font-size:1.2em;">⏳</span>
            <div>
                <strong>${escapeHtml(c.name || c.id)}</strong>
                <span style="opacity:0.6;">(${escapeHtml(c.id)})</span>
                <div class="verify-detail" style="font-size:0.85em; opacity:0.7;">Waiting for first frame…</div>
            </div>
        </div>
    `).join('');

    // Poll /api/stats?camera=<id> every 2s up to 30s. Mark ✓ once
    // frames_in_stream > 0; mark ✗ on timeout.
    const deadline = Date.now() + 30_000;
    const pending = new Set(cams.map(c => c.id));

    while (pending.size > 0 && Date.now() < deadline) {
        await Promise.all([...pending].map(async (camId) => {
            try {
                const r = await fetch(`/api/stats?camera=${encodeURIComponent(camId)}`);
                if (!r.ok) return;
                const data = await r.json();
                const frames = data.frames_in_stream || 0;
                if (frames > 0) {
                    pending.delete(camId);
                    const row = document.querySelector(`.verify-row[data-cam="${camId}"]`);
                    if (row) {
                        row.querySelector('.verify-icon').textContent = '✓';
                        row.querySelector('.verify-icon').style.color = '#22c55e';
                        row.querySelector('.verify-detail').textContent =
                            `${frames} frame${frames === 1 ? '' : 's'} received — pipeline alive.`;
                    }
                }
            } catch (_) { /* keep retrying */ }
        }));
        if (pending.size === 0) break;
        await new Promise(res => setTimeout(res, 2000));
    }

    // Anything still pending after the deadline failed.
    for (const camId of pending) {
        const row = document.querySelector(`.verify-row[data-cam="${camId}"]`);
        if (row) {
            row.querySelector('.verify-icon').textContent = '✗';
            row.querySelector('.verify-icon').style.color = '#ef4444';
            row.querySelector('.verify-detail').innerHTML =
                `No frames received after 30s. Likely causes: wrong RTSP URL, wrong credentials, or the camera is unreachable from the host network. Fixable in the <strong>Cameras tab</strong> after you finish setup.`;
        }
    }

    continueBtn.disabled = false;
    state.verifyResult = {
        passed: cams.length - pending.size,
        failed: pending.size,
    };
}

function continueFromVerify() {
    showStep('telegram');
    showTelegramSubstep('token');
}

// ---------------------------------------------------------------------------
// Step 6 — Telegram (optional)
// ---------------------------------------------------------------------------
// Three sub-steps: paste token → validate via getMe → user sends a message →
// poll getUpdates to discover chat_id → save to .env. The whole thing can be
// skipped at any time.

function showTelegramSubstep(name) {
    for (const sub of ['token', 'discover', 'done']) {
        document.getElementById('tgStep' + capitalize(sub)).hidden = (sub !== name);
    }
}
function capitalize(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

let _tgToken = '';
let _tgBotUsername = '';

async function tgValidateToken() {
    const token = document.getElementById('tgToken').value.trim();
    const status = document.getElementById('tgTokenStatus');
    const btn = document.getElementById('btnTgValidate');
    if (!token) {
        status.hidden = false;
        status.className = 'rtsp-test-result err';
        status.textContent = 'Paste the token first.';
        return;
    }
    btn.disabled = true;
    status.hidden = false;
    status.className = 'rtsp-test-result';
    status.textContent = '⏳ Checking with Telegram…';

    try {
        const r = await fetch('/api/setup/telegram/validate-token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token }),
        });
        const data = await r.json();
        if (!data.ok) {
            status.className = 'rtsp-test-result err';
            status.textContent = `✗ ${data.error || 'Token rejected.'}`;
            btn.disabled = false;
            return;
        }
        _tgToken = token;
        _tgBotUsername = data.username || '';
        // Move to substep 2
        document.getElementById('tgBotMention').textContent =
            _tgBotUsername ? `@${_tgBotUsername}` : '(your bot)';
        showTelegramSubstep('discover');
        tgDiscoverChatId();
    } catch (e) {
        status.className = 'rtsp-test-result err';
        status.textContent = `✗ Network error: ${e}`;
        btn.disabled = false;
    }
}

async function tgDiscoverChatId() {
    const status = document.getElementById('tgDiscoverStatus');
    const retry = document.getElementById('btnTgDiscoverRetry');
    status.className = 'rtsp-test-result';
    status.textContent = '⏳ Waiting for your message…';
    retry.hidden = true;

    try {
        const r = await fetch('/api/setup/telegram/discover-chat-id', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token: _tgToken }),
        });
        const data = await r.json();
        if (!data.ok) {
            status.className = 'rtsp-test-result err';
            status.textContent = `✗ ${data.error}`;
            retry.hidden = false;
            return;
        }
        // Got the chat id — save + send confirmation
        await tgSave(data);
    } catch (e) {
        status.className = 'rtsp-test-result err';
        status.textContent = `✗ Network error: ${e}`;
        retry.hidden = false;
    }
}

async function tgSave(discovered) {
    const status = document.getElementById('tgDiscoverStatus');
    status.className = 'rtsp-test-result';
    status.textContent = '⏳ Saving and sending a test message to confirm…';

    try {
        const r = await fetch('/api/setup/telegram/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                token: _tgToken,
                chat_id: discovered.chat_id,
                user_id: discovered.user_id,
            }),
        });
        const data = await r.json();
        if (!data.ok) {
            status.className = 'rtsp-test-result err';
            status.textContent = `✗ ${data.error || 'Save failed.'}`;
            return;
        }
        const who = discovered.first_name || discovered.username || `user ${discovered.user_id}`;
        document.getElementById('tgConfirmedName').textContent = who;
        state.telegramConfigured = true;
        showTelegramSubstep('done');
    } catch (e) {
        status.className = 'rtsp-test-result err';
        status.textContent = `✗ Network error: ${e}`;
    }
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
        if (state.verifyResult && state.verifyResult.failed === 0 && state.verifyResult.passed > 0) {
            parts.push(`Your first camera is added and producing frames (${state.verifyResult.passed} ${state.verifyResult.passed === 1 ? 'pipeline' : 'pipelines'} verified end-to-end).`);
        } else if (state.verifyResult && state.verifyResult.failed > 0) {
            parts.push(`Your camera was registered but the verify check didn't see frames after 30s. Open the Cameras tab to recheck the RTSP URL + credentials.`);
        } else {
            parts.push('Your first camera was added. The orchestrator will spawn its detector services within ~10 seconds.');
        }
    } else if (state.cameraSkipped) {
        parts.push('Camera setup skipped — add cameras any time via the Cameras tab.');
    }

    el.innerHTML = parts.join(' ');
}

// ---------------------------------------------------------------------------
// Location step
// ---------------------------------------------------------------------------
async function populateLocationStep() {
    const tzSelect = document.getElementById('setupTimezone');
    if (!tzSelect) return;

    // Fetch current conditions to learn the active timezone + retention defaults
    let currentTz = 'America/Toronto';
    try {
        const r = await fetch('/api/conditions');
        if (r.ok) {
            const d = await r.json();
            if (d.timezone) currentTz = d.timezone;
            const ret = d.retention || {};
            if (typeof ret.recordings_days === 'number') {
                document.getElementById('setupRetentionRecordings').value = ret.recordings_days;
            }
            if (typeof ret.snapshots_days === 'number') {
                document.getElementById('setupRetentionSnapshots').value = ret.snapshots_days;
            }
            if (typeof ret.clips_days === 'number') {
                document.getElementById('setupRetentionClips').value = ret.clips_days;
            }
        }
    } catch (e) { /* fall back to default */ }

    // Fetch the full IANA timezone list (~600 zones) grouped by region
    tzSelect.innerHTML = '<option value="">Loading…</option>';
    try {
        const r = await fetch('/api/setup/timezones');
        if (!r.ok) throw new Error('failed to load timezones');
        const data = await r.json();
        tzSelect.innerHTML = '';
        for (const region of data.regions || []) {
            const group = document.createElement('optgroup');
            group.label = region;
            for (const zone of data.zones[region] || []) {
                const opt = document.createElement('option');
                opt.value = zone;
                opt.textContent = zone;
                if (zone === currentTz) opt.selected = true;
                group.appendChild(opt);
            }
            tzSelect.appendChild(group);
        }
    } catch (e) {
        tzSelect.innerHTML = `<option value="${currentTz}">${currentTz} (only — backend list unavailable)</option>`;
    }
}

async function saveLocation() {
    const tz = document.getElementById('setupTimezone').value;
    const recordings = parseInt(document.getElementById('setupRetentionRecordings').value, 10);
    const snapshots = parseInt(document.getElementById('setupRetentionSnapshots').value, 10);
    const clips = parseInt(document.getElementById('setupRetentionClips').value, 10);
    const status = document.getElementById('setupLocationStatus');
    status.style.display = 'block';

    if (!tz || isNaN(recordings) || isNaN(snapshots) || isNaN(clips)) {
        status.style.background = '#3a1f1f';
        status.style.color = '#ff9b9b';
        status.textContent = 'Please pick a timezone and valid retention values.';
        return;
    }

    status.style.background = '#1f2a3a';
    status.style.color = '#9bbbff';
    status.textContent = 'Saving to .env (requires a service restart to fully apply)…';

    try {
        const r = await fetch('/api/setup/apply-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                LOCATION_TIMEZONE: tz,
                RETENTION_DAYS: recordings,
                SNAPSHOT_RETENTION_DAYS: snapshots,
                CLIP_RETENTION_DAYS: clips,
            }),
        });
        if (!r.ok) {
            const t = await r.text();
            throw new Error(t || r.status);
        }
        status.style.background = '#1f3a1f';
        status.style.color = '#9bff9b';
        // Orchestrator auto-restarts dashboard (+ recorder if RETENTION_DAYS
        // changed) so the user doesn't need to touch the terminal. The
        // retention poller cycles hourly and re-reads env each cycle, so
        // snapshot/clip retention values apply within an hour even without
        // a restart.
        status.textContent = `Saved. Dashboard is restarting to pick up the new timezone (takes ~10s). Retention changes apply within the hour.`;
        state.locationSaved = true;
        setTimeout(() => showStep('camera'), 2500);
    } catch (e) {
        status.style.background = '#3a1f1f';
        status.style.color = '#ff9b9b';
        status.textContent = `Save failed: ${e.message}. You can edit .env manually instead.`;
    }
}

async function finishWizard() {
    const steps = [];
    if (state.detected) steps.push('hardware_detected');
    if (state.cameraAdded) steps.push('camera_added');
    else if (state.cameraSkipped) steps.push('camera_skipped');
    if (state.verifyResult) {
        steps.push(`verify_${state.verifyResult.passed}of${state.verifyResult.passed + state.verifyResult.failed}`);
    }
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
    // Step indicator: click to jump back to previously-visited steps.
    document.getElementById('stepIndicator').addEventListener('click', _onStepIndicatorClick);

    document.getElementById('btnNext1').addEventListener('click', () => showStep('hardware'));
    document.getElementById('btnBack2').addEventListener('click', () => showStep('welcome'));
    document.getElementById('btnNext2').addEventListener('click', () => {
        populateLocationStep();
        showStep('location');
    });
    document.getElementById('btnBackLocation').addEventListener('click', () => showStep('hardware'));
    document.getElementById('btnSkipLocation').addEventListener('click', () => showStep('camera'));
    document.getElementById('btnSaveLocation').addEventListener('click', saveLocation);
    document.getElementById('btnBack3').addEventListener('click', () => showStep('location'));

    document.getElementById('btnDetect').addEventListener('click', detectHardware);
    document.getElementById('btnApplyConfig').addEventListener('click', applyConfig);
    document.getElementById('btnTestRtsp').addEventListener('click', testRtsp);
    document.getElementById('btnAddCamera').addEventListener('click', addCamera);
    document.getElementById('btnSkipCamera').addEventListener('click', skipCamera);
    document.getElementById('btnFinish').addEventListener('click', finishWizard);
    document.getElementById('btnVerifySkip').addEventListener('click', () => {
        showStep('telegram');
        showTelegramSubstep('token');
    });
    document.getElementById('btnVerifyContinue').addEventListener('click', continueFromVerify);

    // Telegram sub-step wiring
    document.getElementById('btnTgBack').addEventListener('click', () => showStep('verify'));
    document.getElementById('btnTgSkip').addEventListener('click', () => {
        state.telegramSkipped = true;
        showStep('finish');
        renderFinishSummary();
    });
    document.getElementById('btnTgValidate').addEventListener('click', tgValidateToken);
    document.getElementById('btnTgDiscoverCancel').addEventListener('click', () => {
        state.telegramSkipped = true;
        showStep('finish');
        renderFinishSummary();
    });
    document.getElementById('btnTgDiscoverRetry').addEventListener('click', tgDiscoverChatId);
    document.getElementById('btnTgFinish').addEventListener('click', () => {
        showStep('finish');
        renderFinishSummary();
    });

    // ONVIF discovery
    document.getElementById('btnDiscover').addEventListener('click', discoverCameras);
    document.getElementById('btnOnvifCancel').addEventListener('click', closeOnvifModal);
    document.getElementById('btnOnvifConnect').addEventListener('click', connectOnvif);
    document.getElementById('onvifCredsModal').addEventListener('click', (e) => {
        if (e.target.id === 'onvifCredsModal') closeOnvifModal();
    });
});
