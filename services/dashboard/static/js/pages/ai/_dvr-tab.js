/**
 * ai/_dvr-tab.js — DVR recordings browser tab.
 *
 * Extracted from ai.js during the 2026-05-22 modularity split.
 *
 * Lists per-camera recording dates and 60s segments from /api/recordings,
 * plays them in an inline <video> element with ffmpeg-on-demand remuxing.
 *
 * Public functions on `window` (called from ai.html onclick/onchange or from
 * ai.js handleDeepLink):
 *   window._onRecCameraChange, window._initRecordingsTab,
 *   window._loadRecSegments, window._playRecording, window._closeRecPlayer
 *
 * Helpers kept module-local: _ensureRecCameraList, _refreshDvrRetentionNote.
 *
 * Cross-file references (resolved at call time, classic-script semantics):
 *   - _recCamera, _recLastDate (_state.js)
 *   - _safeHtml (js/lib/safe-html.js)
 *
 * innerHTML sinks below are wrapped in _safeHtml(...) (DOMPurify) or are
 * static string literals — safe per existing convention.
 */

window._onRecCameraChange = function (camId) {
    _recCamera = camId || '';
    _recLastDate = '';  // force segment reload for the new camera
    window._initRecordingsTab();
};

async function _ensureRecCameraList() {
    // Populate the camera selector from /api/recordings/cameras (cameras that
    // actually have recordings on disk). Also load friendly names from the
    // camera registry so we show e.g. "Front Door (cam1)".
    const sel = document.getElementById('recCameraPicker');
    if (!sel) return;
    try {
        const [recsRes, camsRes] = await Promise.all([
            fetch('/api/recordings/cameras'),
            fetch('/api/cameras'),
        ]);
        const recsData = await recsRes.json();
        const camsData = await camsRes.json();

        const nameByid = {};
        for (const c of (camsData.cameras || [])) {
            if (c.id) nameByid[c.id] = c.name || c.id;
        }

        const recCams = recsData.cameras || [];
        const prev = sel.value;
        sel.innerHTML = '';

        if (recCams.length === 0) {
            sel.innerHTML = '<option value="">No cameras have recordings yet</option>';
            return;
        }

        recCams.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            const friendly = nameByid[c.id] || c.id;
            opt.textContent = `${friendly} (${c.day_count} day${c.day_count !== 1 ? 's' : ''})`;
            sel.appendChild(opt);
        });

        // Pick previously selected camera if still valid, else first one
        const valid = recCams.map(c => c.id);
        if (prev && valid.includes(prev)) {
            sel.value = prev;
            _recCamera = prev;
        } else {
            sel.value = recCams[0].id;
            _recCamera = recCams[0].id;
        }
    } catch (e) {
        console.warn('Failed to load DVR camera list:', e);
    }
}

// Populate the DVR retention note. Reads /api/conditions which already
// exposes the three retention settings (recordings, snapshots, clips).
async function _refreshDvrRetentionNote() {
    const note = document.getElementById('dvrRetentionNote');
    if (!note) return;
    try {
        const resp = await fetch('/api/conditions');
        if (!resp.ok) throw new Error(resp.status);
        const data = await resp.json();
        const r = data.retention || {};
        const rec = r.recordings_days, snap = r.snapshots_days, clip = r.clips_days;
        const parts = [];
        if (typeof rec === 'number') parts.push(`recordings kept <strong>${rec} days</strong>`);
        if (typeof snap === 'number') parts.push(`event snapshots <strong>${snap} days</strong>`);
        if (typeof clip === 'number') parts.push(`AI/Telegram clips <strong>${clip} days</strong>`);
        note.innerHTML = _safeHtml(parts.length
            ? `🗑️ Retention: ${parts.join(' · ')}. <span style="opacity:0.7;">Change these in the Settings panel on the home page.</span>`
            : '');
    } catch (e) {
        note.textContent = '';
    }
}

window._initRecordingsTab = async function () {
    _refreshDvrRetentionNote();  // fire-and-forget, doesn't block tab init
    await _ensureRecCameraList();

    // Always re-fetch dates so new recordings appear
    try {
        const camParam = _recCamera ? `?camera=${encodeURIComponent(_recCamera)}` : '';
        const resp = await fetch('/api/recordings/dates' + camParam);
        const data = await resp.json();
        const sel = document.getElementById('recDatePicker');
        const prevValue = sel.value;  // remember current selection
        sel.innerHTML = '';

        if (!data.dates || data.dates.length === 0) {
            sel.innerHTML = '<option value="">No recordings found</option>';
            // Also clear segment grid since camera might be empty
            const grid = document.getElementById('recSegmentGrid');
            if (grid) grid.innerHTML = '<div style="color:var(--text-secondary,#888); grid-column:1/-1; text-align:center; padding:20px;">No recordings for this camera</div>';
            return;
        }

        data.dates.forEach((d) => {
            const opt = document.createElement('option');
            opt.value = d;
            try {
                const dt = new Date(d + 'T12:00:00');
                opt.textContent = dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
            } catch (_) {
                opt.textContent = d;
            }
            sel.appendChild(opt);
        });

        // Restore previous selection if still valid, otherwise load most recent
        const dateToLoad = data.dates.includes(prevValue) ? prevValue : data.dates[0];
        sel.value = dateToLoad;
        if (dateToLoad !== _recLastDate) {
            window._loadRecSegments(dateToLoad);
        }
    } catch (e) {
        console.warn('Failed to load recording dates:', e);
    }
};

window._loadRecSegments = async function (date) {
    if (!date) return;
    _recLastDate = date;

    // Close any playing video when switching dates
    window._closeRecPlayer();

    const grid = document.getElementById('recSegmentGrid');
    grid.innerHTML = '<div style="color:var(--text-secondary,#888); grid-column:1/-1; text-align:center; padding:20px;">Loading...</div>';

    try {
        const camParam = _recCamera ? `&camera=${encodeURIComponent(_recCamera)}` : '';
        const resp = await fetch(`/api/recordings/segments?date=${date}${camParam}`);
        const data = await resp.json();

        if (!data.segments || data.segments.length === 0) {
            grid.innerHTML = '<div style="color:var(--text-secondary,#888); grid-column:1/-1; text-align:center; padding:20px;">No recordings for this date</div>';
            return;
        }

        grid.innerHTML = '';
        data.segments.forEach(seg => {
            const card = document.createElement('button');
            card.style.cssText = 'background:var(--bg-elevated,#1e1e2e); border:1px solid var(--border,#333); border-radius:8px; padding:12px; cursor:pointer; text-align:center; transition:all 0.2s;';
            card.innerHTML = _safeHtml(`
                <div style="font-size:1.1em; font-weight:600; color:var(--text-primary,#e0e0e0);">${seg.time}</div>
                <div style="font-size:0.8em; color:var(--text-secondary,#888); margin-top:4px;">${seg.size_mb} MB</div>
            `);
            card.onmouseenter = () => card.style.borderColor = 'var(--accent,#6366f1)';
            card.onmouseleave = () => card.style.borderColor = 'var(--border,#333)';
            card.onclick = () => window._playRecording(date, seg.filename, seg.time);
            grid.appendChild(card);
        });
    } catch (e) {
        grid.innerHTML = '<div style="color:#ef4444; grid-column:1/-1; text-align:center; padding:20px;">Failed to load segments</div>';
    }
};

window._playRecording = function (date, filename, timeLabel) {
    const player = document.getElementById('recPlayer');
    const wrap = document.getElementById('recPlayerWrap');
    const label = document.getElementById('recPlayerLabel');

    // Stop any current playback
    player.pause();

    // Show loading state while ffmpeg remuxes on first access
    label.textContent = `⏳ Converting ${timeLabel}...`;
    wrap.style.display = 'block';
    player.style.opacity = '0.4';

    // Set source (include camera so the backend picks the right per-camera dir)
    const camParam = _recCamera ? `?camera=${encodeURIComponent(_recCamera)}` : '';
    const url = `/api/recordings/stream/${date}/${filename}${camParam}`;
    player.src = url;

    // When video is ready to play, update label and show player
    player.oncanplay = function () {
        label.textContent = `📹 ${timeLabel} — ${date}`;
        player.style.opacity = '1';
        player.oncanplay = null;  // fire once
    };

    // Error handler
    player.onerror = function () {
        const err = player.error;
        const msg = err ? `Code ${err.code}: ${err.message || 'unknown'}` : 'unknown error';
        console.error('DVR playback error:', msg, 'URL:', url);
        label.textContent = `❌ Playback error — ${msg}`;
        player.style.opacity = '1';
    };

    // Load the video
    player.load();
};

window._closeRecPlayer = function () {
    const player = document.getElementById('recPlayer');
    player.pause();
    player.removeAttribute('src');
    player.style.opacity = '1';
    player.oncanplay = null;
    player.load();
    document.getElementById('recPlayerWrap').style.display = 'none';
};
