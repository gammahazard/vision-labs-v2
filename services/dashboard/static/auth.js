/**
 * static/auth.js — Authentication UI for the Vision Labs dashboard.
 *
 * PURPOSE:
 *   Provides logout and change-password functionality in the dashboard settings.
 *   Loaded by index.html alongside other JS modules.
 *
 * RELATIONSHIPS:
 *   - Calls: /api/auth/logout, /api/auth/change-password, /api/auth/status
 *   - Used by: index.html (Settings panel)
 */

// ---------------------------------------------------------------------------
// Auth Status — Load current user on page load
// ---------------------------------------------------------------------------
async function loadAuthStatus() {
    try {
        const res = await fetch('/api/auth/status');
        const data = await res.json();
        const el = document.getElementById('authUsername');
        if (el && data.username) {
            el.textContent = data.username;
        }
    } catch (e) {
        console.error('Failed to load auth status:', e);
    }
}

// ---------------------------------------------------------------------------
// Logout
// ---------------------------------------------------------------------------
async function logout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
    } catch (e) {
        // Best-effort
    }
    window.location.href = '/login.html';
}

// ---------------------------------------------------------------------------
// Change Password
// ---------------------------------------------------------------------------
async function changePassword() {
    const currentPw = document.getElementById('currentPassword').value;
    const newPw = document.getElementById('newPassword').value;
    const confirmPw = document.getElementById('confirmPassword').value;
    const newUsername = document.getElementById('newUsername').value.trim();
    const msgEl = document.getElementById('changePwMsg');

    if (!currentPw || !newPw) {
        msgEl.textContent = 'Current and new password are required';
        msgEl.className = 'auth-msg error';
        return;
    }

    if (newPw.length < 4) {
        msgEl.textContent = 'Password must be at least 4 characters';
        msgEl.className = 'auth-msg error';
        return;
    }

    if (newPw !== confirmPw) {
        msgEl.textContent = 'New passwords do not match';
        msgEl.className = 'auth-msg error';
        return;
    }

    try {
        const body = { current_password: currentPw, new_password: newPw };
        if (newUsername) body.new_username = newUsername;

        const res = await fetch('/api/auth/change-password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        const data = await res.json();

        if (res.ok) {
            msgEl.textContent = 'Credentials updated successfully';
            msgEl.className = 'auth-msg success';
            document.getElementById('currentPassword').value = '';
            document.getElementById('newPassword').value = '';
            document.getElementById('confirmPassword').value = '';
            document.getElementById('newUsername').value = '';
            if (data.username) {
                const el = document.getElementById('authUsername');
                if (el) el.textContent = data.username;
            }
        } else {
            msgEl.textContent = data.error || 'Update failed';
            msgEl.className = 'auth-msg error';
        }
    } catch (e) {
        msgEl.textContent = 'Connection error';
        msgEl.className = 'auth-msg error';
    }
}

// Load auth status when page loads
document.addEventListener('DOMContentLoaded', loadAuthStatus);
