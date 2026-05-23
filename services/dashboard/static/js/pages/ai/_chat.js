/**
 * ai/_chat.js — chat UI, Ollama readiness, send-message pipeline.
 *
 * Extracted from ai.js during the 2026-05-22 modularity split.
 *
 * Owns: checkOllamaStatus, showChat, loadHistory, renderAllMessages,
 * _appendToolDataToggle, addMessage, appendMessageElement, showTyping,
 * hideTyping, scrollToBottom, sendMessage.
 *
 * Cross-file references (resolved at call time, classic-script semantics):
 *   - _safeHtml (js/lib/safe-html.js)
 *   - renderMarkdown, _escapeHtml, hideSuggestions, showError, hideError (_utils.js)
 *   - aiConfig, chatHistory, isWaiting, ollamaReady (_state.js)
 *
 * innerHTML sinks below are wrapped in _safeHtml(...) (DOMPurify) or
 * built from _escapeHtml-escaped values — safe per existing convention.
 */

// ---------------------------------------------------------------------------
// Ollama readiness polling
// ---------------------------------------------------------------------------
async function checkOllamaStatus() {
    const overlay = document.getElementById('ollamaOverlay');
    const statusEl = document.getElementById('ollamaStatus');
    const sendBtn = document.getElementById('sendBtn');
    const input = document.getElementById('chatInput');

    // Show overlay immediately
    if (overlay) overlay.style.display = 'flex';
    if (sendBtn) sendBtn.disabled = true;
    if (input) input.placeholder = 'Waiting for AI model...';

    const statusMessages = {
        offline: 'Connecting to Ollama...',
        not_found: 'Model not found — downloading may be in progress...',
        loading: 'Loading Qwen 3 14B into GPU memory...',
        ready: 'Ready!',
        disabled: 'AI chat is disabled on this hardware tier.',
    };

    const startTime = Date.now();
    const MAX_WAIT_MS = 120_000; // 2 minutes hard fallback

    const dismissOverlay = () => {
        ollamaReady = true;
        if (sendBtn) sendBtn.disabled = false;
        if (input) input.placeholder = 'Ask me anything...';
        if (overlay) {
            overlay.style.transition = 'opacity 0.5s';
            overlay.style.opacity = '0';
            setTimeout(() => { overlay.style.display = 'none'; }, 500);
        }
        // Re-fetch history to pick up warm-up messages saved after initial load
        loadHistory();
    };

    const showDisabled = () => {
        // Tier sets CHAT_MODEL="" — chat is intentionally off. Keep the overlay
        // visible with a clear message, leave the input disabled.
        if (statusEl) {
            statusEl.textContent = 'AI chat is disabled on this hardware tier. Set CHAT_MODEL in your .env to enable it.';
        }
        if (input) input.placeholder = 'AI chat disabled';
        if (sendBtn) sendBtn.disabled = true;
        // Don't dismiss the overlay — leaving it visible is the signal.
    };

    const poll = async () => {
        // Hard fallback — dismiss after 2 minutes regardless
        if (Date.now() - startTime > MAX_WAIT_MS) {
            console.warn('AI model status check timed out after 2 minutes — dismissing overlay');
            if (statusEl) statusEl.textContent = 'Timed out — try chatting anyway';
            dismissOverlay();
            return;
        }

        try {
            const resp = await fetch('/api/ai/status');
            if (resp.ok) {
                const data = await resp.json();
                if (data.status === 'disabled') {
                    showDisabled();
                    return;  // no polling, no overlay dismissal
                }
                if (statusEl) statusEl.textContent = statusMessages[data.status] || data.status;
                if (data.model_ready) {
                    if (statusEl) statusEl.textContent = 'Ready!';
                    dismissOverlay();
                    return; // Stop polling
                }
            }
        } catch (e) {
            if (statusEl) statusEl.textContent = 'Connecting to Ollama...';
        }
        setTimeout(poll, 3000);
    };
    poll();
}

// ---------------------------------------------------------------------------
// Chat UI
// ---------------------------------------------------------------------------
function showChat() {
    document.getElementById('wizardContainer').style.display = 'none';
    document.getElementById('chatContainer').style.display = 'flex';

    // Update header with AI name
    document.getElementById('aiNameDisplay').textContent = aiConfig.ai_name || 'Atlas';

    // Focus input
    setTimeout(() => {
        const input = document.getElementById('chatInput');
        if (input) input.focus();
    }, 100);
}

async function loadHistory() {
    try {
        const resp = await fetch('/api/ai/history?limit=50');
        if (resp.ok) {
            const history = await resp.json();
            if (history.length > 0) {
                chatHistory = history.map(m => ({
                    role: m.role,
                    content: m.content,
                }));
                renderAllMessages();
            } else {
                // No history — show welcome
                const name = aiConfig.ai_name || 'Atlas';
                const greeting = aiConfig.user_name
                    ? `Welcome back, ${aiConfig.user_name}! How can I help?`
                    : `Welcome back! How can I help?`;
                addMessage('assistant', greeting);
            }
        }
    } catch (e) {
        console.warn('Failed to load history:', e);
    }
}

function renderAllMessages() {
    const container = document.getElementById('chatMessages');
    container.innerHTML = '';
    chatHistory.forEach(msg => {
        appendMessageElement(msg.role, msg.content, false);
    });
    scrollToBottom();
}

// Build a collapsible <details> with the raw tool-call data, attach it to
// the most recent assistant message bubble. Each tool's result is escaped
// + pretty-printed if it's valid JSON. Click "Show tool data" to expand.
function _appendToolDataToggle(toolCalls) {
    const container = document.getElementById('chatMessages');
    if (!container) return;
    // Find the LAST assistant message (just appended) and append inside
    // its bubble so the toggle is visually associated with the answer.
    const msgs = container.querySelectorAll('.message.assistant');
    const lastMsg = msgs[msgs.length - 1];
    const last = lastMsg ? lastMsg.querySelector('.message-bubble') : null;
    if (!last) return;

    const details = document.createElement('details');
    details.className = 'tool-data-toggle';
    details.style.cssText = 'margin-top:6px;font-size:0.8em;opacity:0.85;';
    const summary = document.createElement('summary');
    summary.textContent = `🔍 Show tool data (${toolCalls.length} call${toolCalls.length > 1 ? 's' : ''})`;
    summary.style.cssText = 'cursor:pointer;color:#94a3b8;user-select:none;';
    details.appendChild(summary);

    for (const call of toolCalls) {
        const block = document.createElement('div');
        block.style.cssText = 'margin:6px 0;padding:6px 8px;background:rgba(0,0,0,0.25);border-left:3px solid #6366f1;border-radius:4px;';
        const hdr = document.createElement('div');
        let argStr = '';
        try { argStr = JSON.stringify(call.args || {}); } catch (_) { argStr = String(call.args || ''); }
        hdr.innerHTML = `<code style="color:#bfdbfe;">${_escapeHtml(call.name)}</code><code style="color:#94a3b8;">(${_escapeHtml(argStr)})</code>`;
        block.appendChild(hdr);

        const body = document.createElement('pre');
        body.style.cssText = 'margin:4px 0 0;padding:6px;font-size:0.85em;color:#cbd5e1;background:rgba(0,0,0,0.4);border-radius:3px;overflow-x:auto;max-height:300px;white-space:pre-wrap;word-break:break-word;';
        // Pretty-print if JSON, raw otherwise
        let pretty = call.result || '';
        try { pretty = JSON.stringify(JSON.parse(call.result), null, 2); } catch (_) { /* keep raw */ }
        body.textContent = pretty;
        block.appendChild(body);
        details.appendChild(block);
    }

    last.appendChild(details);
}

function _escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}


function addMessage(role, content) {
    chatHistory.push({ role, content });
    appendMessageElement(role, content, true);
    scrollToBottom();
}

function appendMessageElement(role, content, animate) {
    const container = document.getElementById('chatMessages');

    // System messages: centered, muted, no avatar
    if (role === 'system') {
        const sysDiv = document.createElement('div');
        sysDiv.className = 'message-system';
        if (!animate) sysDiv.style.animation = 'none';
        sysDiv.innerHTML = _safeHtml(renderMarkdown(content));
        container.appendChild(sysDiv);
        return;
    }

    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}`;
    if (!animate) msgDiv.style.animation = 'none';

    const avatarDiv = document.createElement('div');
    avatarDiv.className = 'message-avatar';
    avatarDiv.textContent = role === 'assistant' ? '🤖' : '👤';

    const bubbleDiv = document.createElement('div');
    bubbleDiv.className = 'message-bubble';
    bubbleDiv.innerHTML = _safeHtml(renderMarkdown(content));

    msgDiv.appendChild(avatarDiv);
    msgDiv.appendChild(bubbleDiv);
    container.appendChild(msgDiv);
}

function showTyping() {
    const container = document.getElementById('chatMessages');
    const typing = document.createElement('div');
    typing.className = 'typing-indicator';
    typing.id = 'typingIndicator';

    const avatarDiv = document.createElement('div');
    avatarDiv.className = 'message-avatar';
    avatarDiv.style.background = 'linear-gradient(135deg, var(--accent), #06b6d4)';
    avatarDiv.textContent = '🤖';

    const dotsDiv = document.createElement('div');
    dotsDiv.className = 'typing-dots';
    dotsDiv.innerHTML = '<span></span><span></span><span></span>';

    typing.appendChild(avatarDiv);
    typing.appendChild(dotsDiv);
    container.appendChild(typing);
    scrollToBottom();
}

function hideTyping() {
    const el = document.getElementById('typingIndicator');
    if (el) el.remove();
}

function scrollToBottom() {
    const container = document.getElementById('chatMessages');
    setTimeout(() => {
        container.scrollTop = container.scrollHeight;
    }, 50);
}

// ---------------------------------------------------------------------------
// Send Message
// ---------------------------------------------------------------------------
async function sendMessage() {
    if (isWaiting) return;
    if (!ollamaReady) {
        showError('AI model is still loading. Please wait...');
        return;
    }

    const input = document.getElementById('chatInput');
    const message = input.value.trim();
    if (!message) return;

    // Add user message
    addMessage('user', message);
    hideSuggestions();
    input.value = '';
    input.style.height = 'auto';

    // Disable input
    isWaiting = true;
    document.getElementById('sendBtn').disabled = true;
    showTyping();
    hideError();

    try {
        const resp = await fetch('/api/ai/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message,
                history: chatHistory.slice(-20),
            }),
        });

        hideTyping();

        if (resp.ok) {
            const data = await resp.json();
            addMessage('assistant', data.reply || 'I had trouble generating a response. Please try again.');
            // If the AI called any tools, attach a collapsible "show tool data"
            // panel under the last assistant message. Lets the user verify the
            // AI's claims against ground truth — the cure for hallucinations
            // on count/identity questions.
            if (Array.isArray(data.tool_calls) && data.tool_calls.length > 0) {
                _appendToolDataToggle(data.tool_calls);
            }
        } else {
            const err = await resp.json().catch(() => ({}));
            showError(err.error || `Error ${resp.status}`);
            // Still add a message so user knows something went wrong
            if (resp.status === 500) {
                addMessage('assistant', '⚠️ I\'m having trouble connecting to my brain (the Qwen model). It might still be downloading — this takes a few minutes on first startup. Try again shortly!');
            }
        }
    } catch (e) {
        hideTyping();
        showError('Network error — is the server running?');
        console.error('Chat error:', e);
    } finally {
        isWaiting = false;
        document.getElementById('sendBtn').disabled = false;
        input.focus();
    }
}
