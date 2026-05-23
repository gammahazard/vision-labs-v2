/**
 * ai/_utils.js — small helpers shared across the AI page.
 *
 * Extracted from ai.js during the 2026-05-22 modularity split.
 *
 * Contains:
 *   _escapeHtml, renderMarkdown, inlineFormat — chat content rendering
 *   showError, hideError                       — error banner
 *   useSuggestion, hideSuggestions             — suggestion chips
 *   resetAssistant                             — wipe history + reopen wizard
 *
 * DOMPurify config + `_safeHtml` live in js/lib/safe-html.js (loaded before
 * this file) and are referenced as globals.
 */

function _escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------------------
// Markdown renderer (light — handles lists, bold/italic, code, links, images,
// and pass-through for tool-emitted <video>/<div>/<figure> blocks). Output is
// later passed through _safeHtml() before injection, so XSS-dangerous tags
// from the LLM or user input are stripped.
// ---------------------------------------------------------------------------
function renderMarkdown(text) {
    if (!text) return '';

    // Process line-by-line for proper list handling
    const lines = text.split('\n');
    let html = '';
    let inUl = false, inOl = false;

    for (const line of lines) {
        const trimmed = line.trim();

        // Unordered list item
        if (/^[-*]\s+/.test(trimmed)) {
            if (inOl) { html += '</ol>'; inOl = false; }
            if (!inUl) { html += '<ul>'; inUl = true; }
            html += `<li>${inlineFormat(trimmed.replace(/^[-*]\s+/, ''))}</li>`;
            continue;
        }
        // Ordered list item
        if (/^\d+\.\s+/.test(trimmed)) {
            if (inUl) { html += '</ul>'; inUl = false; }
            if (!inOl) { html += '<ol>'; inOl = true; }
            html += `<li>${inlineFormat(trimmed.replace(/^\d+\.\s+/, ''))}</li>`;
            continue;
        }

        // Close any open list
        if (inUl) { html += '</ul>'; inUl = false; }
        if (inOl) { html += '</ol>'; inOl = false; }

        // Empty line = paragraph break
        if (!trimmed) {
            html += '<br>';
            continue;
        }

        // Image line: ![alt](url)
        if (/^!\[.*?\]\(.*?\)$/.test(trimmed)) {
            if (inUl) { html += '</ul>'; inUl = false; }
            if (inOl) { html += '</ol>'; inOl = false; }
            const match = trimmed.match(/^!\[(.*)\]\((.+)\)$/);
            if (match) {
                html += `<div class="chat-image"><img src="${match[2]}" alt="${match[1]}" style="max-width:100%;border-radius:8px;margin:8px 0;"><div class="chat-image-caption">${match[1]}</div></div>`;
                continue;
            }
        }

        // HTML tag pass-through (injected by tools: video, div galleries, figures)
        if (/^<(video|div|figure)/.test(trimmed)) {
            html += trimmed;
            continue;
        }

        // Normal paragraph line
        html += `<p>${inlineFormat(trimmed)}</p>`;
    }

    if (inUl) html += '</ul>';
    if (inOl) html += '</ol>';

    // Clean up double breaks and empty paragraphs
    return html.replace(/<p><\/p>/g, '').replace(/(<br>){3,}/g, '<br><br>');
}

function inlineFormat(text) {
    return text
        // Code blocks (triple backtick — shouldn't appear inline but handle gracefully)
        .replace(/```(\w*)\n?([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
        // Inline code
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        // Inline images: ![alt](url) — MUST run before link regex (which would
        // otherwise eat these — image leading `!` differentiates them)
        .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img src="$2" alt="$1" style="max-width:100%;border-radius:8px;margin:4px 0;">')
        // Regular links: [text](url) — clickable. Used by find_dvr_segment and
        // any other tool that hands the user a URL. Stays in same tab so deep
        // links work via the existing handleDeepLink() URL param handler.
        .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" style="color:var(--accent,#6366f1);text-decoration:underline;">$1</a>')
        // Bold
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        // Italic
        .replace(/\*(.+?)\*/g, '<em>$1</em>');
}

// ---------------------------------------------------------------------------
// Error banner
// ---------------------------------------------------------------------------
function showError(msg) {
    const el = document.getElementById('aiError');
    el.textContent = msg;
    el.style.display = 'block';
}

function hideError() {
    document.getElementById('aiError').style.display = 'none';
}

// ---------------------------------------------------------------------------
// Suggestion chips
// ---------------------------------------------------------------------------
function useSuggestion(chip) {
    const text = chip.textContent.replace(/^[\u{1F300}-\u{1FAD6}\u{2600}-\u{27BF}]\s*/u, '').trim();
    const input = document.getElementById('chatInput');
    input.value = text;
    input.focus();
    sendMessage();  // defined in _chat.js (loaded after this file)
}

function hideSuggestions() {
    const el = document.getElementById('suggestionChips');
    if (el) el.style.display = 'none';
}

// ---------------------------------------------------------------------------
// Reset
// ---------------------------------------------------------------------------
async function resetAssistant() {
    if (!confirm('Reset AI assistant? This will clear your chat history and re-open the setup wizard.')) return;
    try {
        await fetch('/api/ai/reset', { method: 'POST' });
    } catch (e) {
        console.warn('Reset failed:', e);
    }
    location.reload();
}
