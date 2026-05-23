/**
 * ai.js — AI assistant page entry point.
 *
 * The original 1193-line ai.js was split into ai/_*.js sibling files on
 * 2026-05-22. This file is now just the entry: it runs the init handler
 * (load config, decide wizard vs chat, wire chat input, handle deep link)
 * and owns the small tab-switching + deep-link routines.
 *
 * Load order (see ai.html <script> tags): _state.js → _utils.js →
 * _wizard.js → _chat.js → _vision-tab.js → _dvr-tab.js → ai.js.
 *
 * Classic scripts share the global scope, so cross-file function /
 * `var` references resolve at call time. No imports needed.
 */

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
    try {
        const resp = await fetch('/api/ai/config');
        if (resp.ok) {
            aiConfig = await resp.json();
        }
    } catch (e) {
        console.warn('Failed to load AI config:', e);
    }

    if (aiConfig.enabled) {
        showChat();
        loadHistory();
        checkOllamaStatus();
    } else {
        showWizard();
    }

    // Deep-link support — used by the AI's find_dvr_segment tool to direct
    // the user to a specific recording. Format:
    //   /ai.html?tab=recordings&camera=cam1&date=2026-05-18&segment=13-00.ts
    handleDeepLink();

    // Chat input — Enter to send, Shift+Enter for newline
    const input = document.getElementById('chatInput');
    if (input) {
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
        // Auto-resize textarea
        input.addEventListener('input', () => {
            input.style.height = 'auto';
            input.style.height = Math.min(input.scrollHeight, 120) + 'px';
        });
    }
});

// ---------------------------------------------------------------------------
// Model Tab Switching
// ---------------------------------------------------------------------------
function switchModelTab(tabName) {
    document.querySelectorAll('.model-tab').forEach(btn => {
        btn.classList.toggle('model-tab--active', btn.dataset.tab === tabName);
    });
    document.getElementById('tabChat').style.display = tabName === 'chat' ? 'flex' : 'none';
    document.getElementById('tabVision').style.display = tabName === 'vision' ? 'flex' : 'none';
    const recTab = document.getElementById('tabRecordings');
    if (recTab) recTab.style.display = tabName === 'recordings' ? 'flex' : 'none';

    if (tabName === 'chat') {
        document.getElementById('chatInput')?.focus();
    } else if (tabName === 'vision') {
        checkVisionStatus();
    } else if (tabName === 'recordings') {
        window._initRecordingsTab();
    }
}

// Handle ?tab=recordings&camera=&date=&segment= deep links from the AI's
// find_dvr_segment tool. Drives the DVR tab directly INSTEAD of going through
// _initRecordingsTab — that function auto-selects "latest date" and kicks off
// a non-awaited _loadRecSegments, which races against our override and wins
// when the user wanted an older date.
async function handleDeepLink() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('tab') !== 'recordings') return;

    const camera = params.get('camera') || '';
    const date = params.get('date') || '';
    const segment = params.get('segment') || '';

    // Show the DVR tab visually — UI flip only, no init side effects.
    document.querySelectorAll('.model-tab').forEach(btn => {
        btn.classList.toggle('model-tab--active', btn.dataset.tab === 'recordings');
    });
    const chatTab = document.getElementById('tabChat');
    const visionTab = document.getElementById('tabVision');
    const recTab = document.getElementById('tabRecordings');
    if (chatTab) chatTab.style.display = 'none';
    if (visionTab) visionTab.style.display = 'none';
    if (recTab) recTab.style.display = 'flex';

    // Populate camera dropdown
    await _ensureRecCameraList();
    const camSel = document.getElementById('recCameraPicker');
    let effectiveCam = camera;
    if (camSel && camera && Array.from(camSel.options).some(o => o.value === camera)) {
        camSel.value = camera;
    } else if (camSel && camSel.options.length > 0) {
        effectiveCam = camSel.value;
    }
    _recCamera = effectiveCam;

    // Load date list for the chosen camera (fetched directly so we don't race
    // _initRecordingsTab's auto-pick-latest-date logic).
    try {
        const camParam = effectiveCam ? `?camera=${encodeURIComponent(effectiveCam)}` : '';
        const resp = await fetch('/api/recordings/dates' + camParam);
        const data = await resp.json();
        const dateSel = document.getElementById('recDatePicker');
        if (dateSel) {
            dateSel.innerHTML = '';
            (data.dates || []).forEach((d) => {
                const opt = document.createElement('option');
                opt.value = d;
                try {
                    const dt = new Date(d + 'T12:00:00');
                    opt.textContent = dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
                } catch (_) {
                    opt.textContent = d;
                }
                dateSel.appendChild(opt);
            });
            // Pick requested date if present; otherwise fall back to most recent
            const dateMatch = date && Array.from(dateSel.options).some(o => o.value === date);
            const targetDate = dateMatch ? date : (dateSel.options[0] ? dateSel.options[0].value : '');
            if (targetDate) {
                dateSel.value = targetDate;
                _recLastDate = targetDate;
                await window._loadRecSegments(targetDate);

                // Auto-play the requested segment
                if (segment) {
                    try {
                        const segCamParam = effectiveCam ? `&camera=${encodeURIComponent(effectiveCam)}` : '';
                        const segResp = await fetch(`/api/recordings/segments?date=${targetDate}${segCamParam}`);
                        const segData = await segResp.json();
                        const match = (segData.segments || []).find(s => s.filename === segment);
                        const label = match ? match.time : segment;
                        window._playRecording(targetDate, segment, label);
                    } catch (_) {
                        window._playRecording(targetDate, segment, segment);
                    }
                }
            }
        }
    } catch (e) {
        console.warn('Deep-link DVR load failed:', e);
    }

    // Strip deep-link params from the URL so a manual refresh doesn't replay
    if (window.history && window.history.replaceState) {
        const cleanUrl = window.location.pathname + window.location.hash;
        window.history.replaceState({}, '', cleanUrl);
    }
}
