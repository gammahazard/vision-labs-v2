/**
 * static/settings.js — post-install Settings panel logic.
 *
 * PURPOSE:
 *   Lets the user change LOCATION_TIMEZONE + retention values after the
 *   first-run setup wizard has already completed, without forcing them
 *   to re-run the wizard.
 *
 * RELATIONSHIPS:
 *   - Calls: /api/setup/timezones (full IANA list grouped by region),
 *            /api/conditions      (current values),
 *            /api/setup/apply-config (write-through to .env via env_writer,
 *            then orchestrator restarts dashboard/recorder as needed).
 *   - Used by: index.html  (Settings panel #settingsPanel)
 */

async function loadSettings() {
    const tzSel = document.getElementById('settingsTimezone');
    const retRec = document.getElementById('settingsRetRecordings');
    const retSnap = document.getElementById('settingsRetSnapshots');
    const retClips = document.getElementById('settingsRetClips');
    if (!tzSel) return;

    // Read current values from /api/conditions (same source the home page
    // uses for the timezone + retention badges).
    let currentTz = 'America/Toronto';
    try {
        const r = await fetch('/api/conditions');
        if (r.ok) {
            const d = await r.json();
            if (d.timezone) currentTz = d.timezone;
            const ret = d.retention || {};
            if (typeof ret.recordings_days === 'number') retRec.value = ret.recordings_days;
            if (typeof ret.snapshots_days === 'number') retSnap.value = ret.snapshots_days;
            if (typeof ret.clips_days === 'number') retClips.value = ret.clips_days;
        }
    } catch (_) { /* keep defaults */ }

    // Populate the timezone dropdown from /api/setup/timezones (~486 IANA
    // zones grouped by region). Cheap to fetch once at panel render time.
    try {
        const r = await fetch('/api/setup/timezones');
        if (!r.ok) throw new Error('failed to load timezones');
        const data = await r.json();
        tzSel.innerHTML = '';
        for (const region of data.regions || []) {
            const group = document.createElement('optgroup');
            group.label = region;
            for (const zone of (data.zones[region] || [])) {
                const opt = document.createElement('option');
                opt.value = zone;
                opt.textContent = zone;
                if (zone === currentTz) opt.selected = true;
                group.appendChild(opt);
            }
            tzSel.appendChild(group);
        }
    } catch (e) {
        tzSel.innerHTML = `<option value="${currentTz}">${currentTz} (only — backend list unavailable)</option>`;
    }
}

async function saveSettings() {
    const btn = document.getElementById('settingsSaveBtn');
    const msg = document.getElementById('settingsMsg');
    const tz = document.getElementById('settingsTimezone').value;
    const rec = parseInt(document.getElementById('settingsRetRecordings').value, 10);
    const snap = parseInt(document.getElementById('settingsRetSnapshots').value, 10);
    const clips = parseInt(document.getElementById('settingsRetClips').value, 10);

    if (!tz || isNaN(rec) || isNaN(snap) || isNaN(clips)) {
        msg.textContent = 'Pick a timezone and valid retention values.';
        msg.style.color = '#f87171';
        return;
    }

    btn.disabled = true;
    btn.textContent = '⏳ Saving...';
    msg.textContent = '';

    try {
        const resp = await fetch('/api/setup/apply-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                LOCATION_TIMEZONE: tz,
                RETENTION_DAYS: rec,
                SNAPSHOT_RETENTION_DAYS: snap,
                CLIP_RETENTION_DAYS: clips,
            }),
        });
        if (!resp.ok) {
            const t = await resp.json().catch(() => ({}));
            throw new Error(t.error || `${resp.status}`);
        }
        const data = await resp.json();
        const restarting = (data.affected_services || []).join(', ');
        msg.style.color = '#4ade80';
        msg.textContent = restarting
            ? `✅ Saved. Restarting: ${restarting} (takes ~10s).`
            : '✅ Saved. Retention picks up on next prune cycle.';
    } catch (e) {
        msg.style.color = '#f87171';
        msg.textContent = `❌ Save failed: ${e.message}`;
    } finally {
        btn.disabled = false;
        btn.textContent = '💾 Save Settings';
    }
}

// Auto-load when the page mounts (panel is collapsed by default so the
// fetch is cheap — only a couple of small JSON responses).
document.addEventListener('DOMContentLoaded', loadSettings);
