/**
 * telegram_access.js — Frontend logic for the Telegram Access Manager.
 *
 * Manages:
 *  - Loading and displaying approved users
 *  - Adding / revoking users
 *  - Loading and auto-refreshing the access log
 *  - "Approve" flow from access log entries
 */

// ── Load on page ready ──
document.addEventListener('DOMContentLoaded', () => {
    loadUsers();
    loadAccessLog();
    // Auto-refresh access log every 15s
    setInterval(loadAccessLog, 15000);
    setInterval(loadUsers, 30000);
});


// ─── Users CRUD ───

async function loadUsers() {
    try {
        const resp = await fetch('/api/telegram/users');
        if (!resp.ok) return;
        const data = await resp.json();
        const users = data.users || {};
        const keys = Object.keys(users);

        document.getElementById('userCount').textContent = `${keys.length} user${keys.length !== 1 ? 's' : ''}`;

        const tbody = document.getElementById('usersBody');
        if (keys.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="tg-empty">No approved users yet. Add one below or approve from the access log.</td></tr>';
            return;
        }

        tbody.innerHTML = keys.map(uid => {
            const u = users[uid];
            const name = u.name || '—';
            const username = u.username ? `@${u.username}` : '';
            const chatId = u.chat_id || '—';
            const role = u.role || 'user';
            const roleBadge = role === 'admin'
                ? '<span style="background:rgba(251,191,36,0.15);color:#fbbf24;padding:0.15rem 0.5rem;border-radius:10px;font-size:0.75rem;">admin</span>'
                : '<span style="background:rgba(148,163,184,0.1);color:#94a3b8;padding:0.15rem 0.5rem;border-radius:10px;font-size:0.75rem;">user</span>';
            const approved = u.approved_at || '—';
            return `
                <tr>
                    <td>
                        <strong>${esc(name)}</strong>
                        ${username ? `<br><span style="color:#94a3b8;font-size:0.78rem;">${esc(username)}</span>` : ''}
                    </td>
                    <td class="tg-uid">${esc(uid)}</td>
                    <td class="tg-uid">${esc(chatId)}</td>
                    <td>${roleBadge}</td>
                    <td style="color:#94a3b8;font-size:0.8rem;">${esc(approved)}</td>
                    <td>
                        <button class="tg-btn tg-btn-danger tg-btn-small" onclick="revokeUser('${esc(uid)}')">✕</button>
                    </td>
                </tr>
            `;
        }).join('');
    } catch (e) {
        console.debug('loadUsers error:', e);
    }
}


async function addUser() {
    const userId = document.getElementById('addUserId').value.trim();
    const chatId = document.getElementById('addChatId').value.trim();
    const name = document.getElementById('addUserName').value.trim();
    const username = document.getElementById('addUsername').value.trim().replace(/^@/, '');

    if (!userId) {
        alert('User ID is required');
        return;
    }

    const params = new URLSearchParams({ user_id: userId });
    if (chatId) params.set('chat_id', chatId);
    if (name) params.set('name', name);
    if (username) params.set('username', username);

    try {
        const resp = await fetch(`/api/telegram/users?${params}`, { method: 'POST' });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            alert(err.error || 'Failed to add user');
            return;
        }
        // Clear form
        document.getElementById('addUserId').value = '';
        document.getElementById('addChatId').value = '';
        document.getElementById('addUserName').value = '';
        document.getElementById('addUsername').value = '';
        await loadUsers();
    } catch (e) {
        alert('Network error: ' + e.message);
    }
}


async function revokeUser(userId) {
    if (!confirm(`Revoke user ${userId}? They won't be able to use the bot.`)) return;
    try {
        await fetch(`/api/telegram/users/${userId}`, { method: 'DELETE' });
        await loadUsers();
    } catch (e) {
        console.error('revokeUser error:', e);
    }
}


// ─── Access Log ───

async function loadAccessLog() {
    try {
        const resp = await fetch('/api/telegram/access-log?count=50');
        if (!resp.ok) return;
        const data = await resp.json();
        const log = data.log || [];

        document.getElementById('logCount').textContent = `${log.length} entries`;

        const container = document.getElementById('logScroll');
        if (log.length === 0) {
            container.innerHTML = '<div class="tg-empty">No access attempts recorded yet.</div>';
            return;
        }

        container.innerHTML = log.map(entry => {
            const authorized = entry.authorized === 'true';
            const who = entry.first_name || entry.username || entry.user_id || 'unknown';
            const lastName = entry.last_name || '';
            const fullName = lastName ? `${who} ${lastName}` : who;
            const username = entry.username ? `@${entry.username}` : '';
            const lang = entry.language_code || '';
            const action = entry.action || '—';
            const time = entry.timestamp || '';
            const userId = entry.user_id || '';
            const chatId = entry.chat_id || '';

            const approveBtn = !authorized && userId
                ? `<button class="tg-btn tg-btn-approve tg-btn-small" onclick="approveFromLog('${esc(userId)}','${esc(chatId)}','${esc(fullName)}','${esc(entry.username || '')}')">Approve</button>`
                : '';

            // Build extra info line
            const extraParts = [];
            if (lang) extraParts.push(`🌐 ${esc(lang)}`);
            if (userId) extraParts.push(`ID: ${esc(userId)}`);
            const extraLine = extraParts.length
                ? `<span style="color:#4b5563;margin-left:0.3rem;">${extraParts.join(' · ')}</span>`
                : '';

            return `
                <div class="tg-log-entry">
                    <div class="tg-log-dot ${authorized ? 'approved' : 'denied'}"></div>
                    <div class="tg-log-info">
                        <div class="tg-log-who">
                            ${esc(fullName)} ${username ? `<span style="color:#64748b;font-size:0.78rem;">${esc(username)}</span>` : ''}
                        </div>
                        <div class="tg-log-action">
                            ${authorized ? '✅' : '🔴'} ${esc(action)}
                            ${extraLine}
                        </div>
                    </div>
                    <div class="tg-log-time">${esc(time)}</div>
                    <div class="tg-log-actions">${approveBtn}</div>
                </div>
            `;
        }).join('');
    } catch (e) {
        console.debug('loadAccessLog error:', e);
    }
}


async function approveFromLog(userId, chatId, name, username) {
    // Pre-fill the add form and auto-submit
    const params = new URLSearchParams({
        user_id: userId,
        chat_id: chatId,
        name: name || '',
        username: username || '',
    });

    try {
        const resp = await fetch(`/api/telegram/users?${params}`, { method: 'POST' });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            alert(err.error || 'Failed to approve user');
            return;
        }
        await loadUsers();
        await loadAccessLog();
    } catch (e) {
        alert('Network error: ' + e.message);
    }
}


async function clearLog() {
    if (!confirm('Clear the entire access log?')) return;
    try {
        await fetch('/api/telegram/access-log', { method: 'DELETE' });
        await loadAccessLog();
    } catch (e) {
        console.error('clearLog error:', e);
    }
}


// ─── Helpers ───

function esc(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
