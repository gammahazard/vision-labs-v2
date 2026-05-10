/**
 * ai.js — AI assistant frontend logic.
 *
 * Handles onboarding wizard state machine, chat message rendering,
 * and API communication with the backend AI routes.
 */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let aiConfig = { enabled: false, user_name: '', ai_name: 'Atlas' };
let chatHistory = [];  // { role: 'user'|'assistant', content: string }
let isWaiting = false;
let ollamaReady = false;

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
// Wizard
// ---------------------------------------------------------------------------
function showWizard() {
    document.getElementById('wizardContainer').style.display = 'flex';
    document.getElementById('chatContainer').style.display = 'none';
    document.getElementById('wizardStep1').style.display = 'block';
}

function wizardNext(step) {
    // Hide all steps
    for (let i = 1; i <= 3; i++) {
        const el = document.getElementById('wizardStep' + i);
        if (el) el.style.display = 'none';
    }
    // Show target step with animation
    const target = document.getElementById('wizardStep' + step);
    if (target) {
        target.style.display = 'block';
        target.style.animation = 'none';
        // Force reflow
        void target.offsetWidth;
        target.style.animation = 'fadeInUp 0.4s ease-out';
    }
}

function selectAIName(chip, name) {
    // Deselect all
    document.querySelectorAll('.name-chip').forEach(c => c.classList.remove('selected'));
    // Select this one
    chip.classList.add('selected');
    document.getElementById('aiName').value = name;
}

async function finishWizard() {
    const userName = document.getElementById('userName').value.trim();
    const aiName = document.getElementById('aiName').value.trim() || 'Atlas';

    aiConfig = { enabled: true, user_name: userName, ai_name: aiName };

    try {
        const resp = await fetch('/api/ai/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(aiConfig),
        });
        if (resp.ok) {
            aiConfig = await resp.json();
        }
    } catch (e) {
        console.warn('Failed to save AI config:', e);
    }

    showChat();

    // Send introductory message from the AI
    const greeting = userName
        ? `Hey ${userName}! I'm ${aiName}. I'm your local AI assistant — I run entirely on your hardware, so nothing leaves this machine. Ask me about your security cameras, set reminders, or just chat. What can I help you with?`
        : `Hey! I'm ${aiName}. I'm your local AI assistant running right on your hardware. Ask me about security events, set reminders, or just chat. What's on your mind?`;

    addMessage('assistant', greeting);
    checkOllamaStatus();
}

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
        sysDiv.innerHTML = renderMarkdown(content);
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
    bubbleDiv.innerHTML = renderMarkdown(content);

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

// ---------------------------------------------------------------------------
// Error display
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
// Simple Markdown renderer
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
        // Inline images: ![alt](url)
        .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img src="$2" alt="$1" style="max-width:100%;border-radius:8px;margin:4px 0;">')
        // Bold
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        // Italic
        .replace(/\*(.+?)\*/g, '<em>$1</em>');
}

// ---------------------------------------------------------------------------
// Suggestion chips
// ---------------------------------------------------------------------------
function useSuggestion(chip) {
    const text = chip.textContent.replace(/^[\u{1F300}-\u{1FAD6}\u{2600}-\u{27BF}]\s*/u, '').trim();
    const input = document.getElementById('chatInput');
    input.value = text;
    input.focus();
    sendMessage();
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

// ---------------------------------------------------------------------------
// Model Tab Switching
// ---------------------------------------------------------------------------
function switchModelTab(tabName) {
    // Block Chat/Vision tabs if VRAM is in generate mode
    if ((tabName === 'chat' || tabName === 'vision') && window._vramMode === 'generate') {
        // Show a brief warning
        const existing = document.querySelector('.vram-tab-toast');
        if (existing) existing.remove();
        const toast = document.createElement('div');
        toast.className = 'vram-tab-toast';
        toast.textContent = '⚡ VRAM freed for image generation — restore AI Chat first';
        toast.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);background:rgba(245,158,11,0.9);color:#000;padding:10px 20px;border-radius:8px;font-size:0.85em;z-index:9999;animation:fadeOut 3s forwards;';
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 3000);
        return;
    }

    // Update tab buttons
    document.querySelectorAll('.model-tab').forEach(btn => {
        btn.classList.toggle('model-tab--active', btn.dataset.tab === tabName);
    });
    // Update tab panels
    document.getElementById('tabChat').style.display = tabName === 'chat' ? 'flex' : 'none';
    document.getElementById('tabVision').style.display = tabName === 'vision' ? 'flex' : 'none';
    const genTab = document.getElementById('tabGenerate');
    if (genTab) genTab.style.display = tabName === 'generate' ? 'flex' : 'none';
    const recTab = document.getElementById('tabRecordings');
    if (recTab) recTab.style.display = tabName === 'recordings' ? 'flex' : 'none';

    // Focus relevant input
    if (tabName === 'chat') {
        document.getElementById('chatInput')?.focus();
    } else if (tabName === 'vision') {
        checkVisionStatus();
    } else if (tabName === 'generate') {
        if (typeof window.initGenerateTab === 'function') {
            window.initGenerateTab();
        }
    } else if (tabName === 'recordings') {
        window._initRecordingsTab();
    }
}

// Update Chat/Vision tab buttons greyed state based on VRAM mode
function updateTabStatesForVram(mode) {
    window._vramMode = mode;
    document.querySelectorAll('.model-tab').forEach(btn => {
        if (btn.dataset.tab === 'chat' || btn.dataset.tab === 'vision') {
            if (mode === 'generate') {
                btn.style.opacity = '0.4';
                btn.style.cursor = 'not-allowed';
                btn.title = 'VRAM freed for generation — restore AI Chat first';
            } else {
                btn.style.opacity = '1';
                btn.style.cursor = 'pointer';
                btn.title = '';
            }
        }
    });
}
window.updateTabStatesForVram = updateTabStatesForVram;

// ---------------------------------------------------------------------------
// Vision Tab — State
// ---------------------------------------------------------------------------
let visionImageBase64 = null;
let visionVideoFrames = null;  // array of base64 frames for video
let visionAnalyzing = false;

// ---------------------------------------------------------------------------
// Vision Tab — Status Polling
// ---------------------------------------------------------------------------
async function checkVisionStatus() {
    const dot = document.getElementById('visionStatusDot');
    const indicator = document.getElementById('visionStatusIndicator');
    const text = document.getElementById('visionStatusText');

    try {
        const resp = await fetch('/api/ai/vision/status');
        if (resp.ok) {
            const data = await resp.json();
            if (data.available) {
                const isLoaded = data.status === 'loaded';
                if (dot) {
                    dot.className = 'vision-status-dot ' + (isLoaded ? 'loaded' : 'online');
                    dot.title = isLoaded ? 'Loaded in VRAM' : 'Ready';
                }
                if (indicator) indicator.className = 'vision-status-indicator ready';
                if (text) text.textContent = isLoaded ? 'Loaded in VRAM' : 'Ready';
            } else {
                if (dot) { dot.className = 'vision-status-dot offline'; dot.title = data.status; }
                if (indicator) indicator.className = 'vision-status-indicator error';
                if (text) text.textContent = data.status === 'not_found' ? 'Model not found' : 'Offline';
            }
        }
    } catch (e) {
        if (dot) { dot.className = 'vision-status-dot offline'; dot.title = 'Offline'; }
        if (indicator) indicator.className = 'vision-status-indicator error';
        if (text) text.textContent = 'Offline';
    }
}

// ---------------------------------------------------------------------------
// Vision Tab — Drag & Drop + File Input
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    const dropZone = document.getElementById('visionDropZone');
    const fileInput = document.getElementById('visionFileInput');
    if (!dropZone || !fileInput) return;

    // Click to browse
    dropZone.addEventListener('click', (e) => {
        if (e.target.closest('.vision-clear-btn')) return;
        if (!document.getElementById('visionPreview')?.style.display ||
            document.getElementById('visionPreview')?.style.display === 'none') {
            fileInput.click();
        }
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            const file = e.target.files[0];
            if (file.type.startsWith('video/')) {
                loadVisionVideo(file);
            } else {
                loadVisionImage(file);
            }
        }
    });

    // Drag events
    dropZone.addEventListener('dragenter', (e) => {
        e.preventDefault();
        dropZone.classList.add('drag-over');
    });

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('drag-over');
    });

    dropZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            const file = files[0];
            if (file.type.startsWith('image/')) {
                loadVisionImage(file);
            } else if (file.type.startsWith('video/')) {
                loadVisionVideo(file);
            }
        }
    });

    // Also check vision status on page load
    checkVisionStatus();
});

function loadVisionImage(file) {
    const reader = new FileReader();
    reader.onload = (e) => {
        const dataUrl = e.target.result;
        // Show preview
        document.getElementById('visionDropContent').style.display = 'none';
        const preview = document.getElementById('visionPreview');
        preview.style.display = 'flex';
        document.getElementById('visionPreviewImg').src = dataUrl;

        // Store base64 (strip data:image/xxx;base64, prefix)
        visionImageBase64 = dataUrl.split(',')[1];

        // Enable analyze button
        document.getElementById('visionAnalyzeBtn').disabled = false;
    };
    reader.readAsDataURL(file);
}

function clearVisionImage() {
    visionImageBase64 = null;
    visionVideoFrames = null;
    document.getElementById('visionDropContent').style.display = 'block';
    document.getElementById('visionPreview').style.display = 'none';
    document.getElementById('visionPreviewImg').src = '';
    document.getElementById('visionVideoPreview').style.display = 'none';
    const vid = document.getElementById('visionPreviewVideo');
    vid.pause(); vid.removeAttribute('src'); vid.load();
    document.getElementById('visionAnalyzeBtn').disabled = true;
    document.getElementById('visionResults').style.display = 'none';
    document.getElementById('visionFileInput').value = '';
}

// ---------------------------------------------------------------------------
// Vision Tab — Video Frame Extraction
// ---------------------------------------------------------------------------
function loadVisionVideo(file) {
    const url = URL.createObjectURL(file);
    document.getElementById('visionDropContent').style.display = 'none';
    document.getElementById('visionPreview').style.display = 'none';
    const videoPreview = document.getElementById('visionVideoPreview');
    videoPreview.style.display = 'flex';
    const video = document.getElementById('visionPreviewVideo');
    video.src = url;

    // Extract frames once metadata is loaded
    video.onloadedmetadata = () => {
        const badge = document.getElementById('visionFrameBadge');
        badge.textContent = '⏳ Extracting frames...';
        badge.style.display = 'block';
        extractVideoFrames(video, file).then(frames => {
            visionVideoFrames = frames;
            badge.textContent = `🎬 ${frames.length} frames extracted`;
            document.getElementById('visionAnalyzeBtn').disabled = false;
        }).catch(err => {
            console.error('Frame extraction error:', err);
            badge.textContent = '❌ Frame extraction failed';
        });
    };
}

async function extractVideoFrames(videoEl, file) {
    const MAX_FRAMES = 6;
    const duration = videoEl.duration;
    if (!duration || duration <= 0) return [];

    // For short clips (<3s), take fewer frames
    const numFrames = duration < 3 ? Math.min(3, Math.ceil(duration)) : MAX_FRAMES;
    const interval = duration / (numFrames + 1);
    const timestamps = [];
    for (let i = 1; i <= numFrames; i++) {
        timestamps.push(interval * i);
    }

    // Create a fresh video element for seeking (avoids conflicts with preview)
    const seekVideo = document.createElement('video');
    seekVideo.muted = true;
    seekVideo.src = URL.createObjectURL(file);
    await new Promise(r => seekVideo.onloadedmetadata = r);

    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    // Scale down to max 720px width to reduce payload
    const scale = Math.min(1, 720 / seekVideo.videoWidth);
    canvas.width = Math.round(seekVideo.videoWidth * scale);
    canvas.height = Math.round(seekVideo.videoHeight * scale);

    const frames = [];
    for (const ts of timestamps) {
        seekVideo.currentTime = ts;
        await new Promise(r => seekVideo.onseeked = r);
        ctx.drawImage(seekVideo, 0, 0, canvas.width, canvas.height);
        const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
        frames.push(dataUrl.split(',')[1]);
    }

    URL.revokeObjectURL(seekVideo.src);
    return frames;
}

// ---------------------------------------------------------------------------
// Vision Tab — Preset Prompts
// ---------------------------------------------------------------------------
const VISION_PROMPTS = {
    '📋 Describe everything': 'Describe this image in detail. Include all visible objects, people, text, and the overall scene.',
    '👤 Describe people': 'Describe all people visible in this image. Include their apparent gender, clothing, hair color/style, accessories, and what they are doing.',
    '🚗 Describe vehicles': 'Describe all vehicles visible in this image. Include the vehicle type, color, make/model if identifiable, and license plate if readable.',
    '📝 Read all text': 'Read and transcribe all visible text in this image, including signs, labels, license plates, and any other text.',
    '⚠️ Security analysis': 'Analyze this image from a security perspective. Describe any people, vehicles, potential threats, suspicious activity, or items of interest for security monitoring.',
};

function setVisionPrompt(btn) {
    const label = btn.textContent.trim();
    const prompt = VISION_PROMPTS[label] || label;
    document.getElementById('visionPrompt').value = prompt;
}

// ---------------------------------------------------------------------------
// Vision Tab — Analyze
// ---------------------------------------------------------------------------
async function analyzeVisionImage() {
    if (visionAnalyzing) return;

    const isVideo = !!visionVideoFrames;
    if (!visionImageBase64 && !visionVideoFrames) return;

    const prompt = document.getElementById('visionPrompt').value.trim()
        || (isVideo ? 'Describe what happens in this video.' : 'Describe this image in detail.');

    visionAnalyzing = true;
    document.getElementById('visionAnalyzeBtn').disabled = true;
    document.getElementById('visionResults').style.display = 'none';
    const loadingEl = document.getElementById('visionLoading');
    loadingEl.style.display = 'flex';
    loadingEl.querySelector('span').textContent = isVideo
        ? `Analyzing ${visionVideoFrames.length} frames...` : 'Analyzing image...';

    try {
        const resp = await fetch('/api/ai/vision', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(
                isVideo ? { images: visionVideoFrames, prompt } : { image: visionImageBase64, prompt }
            ),
        });

        document.getElementById('visionLoading').style.display = 'none';

        if (resp.ok) {
            const data = await resp.json();
            document.getElementById('visionResultsText').textContent = data.description;
            document.getElementById('visionResults').style.display = 'block';
        } else {
            const err = await resp.json().catch(() => ({}));
            document.getElementById('visionResultsText').textContent =
                '❌ ' + (err.error || `Error ${resp.status}`);
            document.getElementById('visionResults').style.display = 'block';
        }
    } catch (e) {
        document.getElementById('visionLoading').style.display = 'none';
        document.getElementById('visionResultsText').textContent =
            '❌ Network error — is the server running?';
        document.getElementById('visionResults').style.display = 'block';
    } finally {
        visionAnalyzing = false;
        if (visionImageBase64) {
            document.getElementById('visionAnalyzeBtn').disabled = false;
        }
    }
}

// ---------------------------------------------------------------------------
// Vision Tab — Copy Result
// ---------------------------------------------------------------------------
function copyVisionResult() {
    const text = document.getElementById('visionResultsText').textContent;
    navigator.clipboard.writeText(text).then(() => {
        const btn = document.querySelector('.vision-copy-btn');
        const orig = btn.textContent;
        btn.textContent = '✅';
        setTimeout(() => { btn.textContent = orig; }, 1500);
    }).catch(() => {
        console.warn('Failed to copy to clipboard');
    });
}

// ---------------------------------------------------------------------------
// DVR Recordings Tab
// ---------------------------------------------------------------------------
let _recLastDate = '';  // track last loaded date to avoid redundant fetches

window._initRecordingsTab = async function () {
    // Always re-fetch dates so new recordings appear
    try {
        const resp = await fetch('/api/recordings/dates');
        const data = await resp.json();
        const sel = document.getElementById('recDatePicker');
        const prevValue = sel.value;  // remember current selection
        sel.innerHTML = '';

        if (!data.dates || data.dates.length === 0) {
            sel.innerHTML = '<option value="">No recordings found</option>';
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
        const resp = await fetch(`/api/recordings/segments?date=${date}`);
        const data = await resp.json();

        if (!data.segments || data.segments.length === 0) {
            grid.innerHTML = '<div style="color:var(--text-secondary,#888); grid-column:1/-1; text-align:center; padding:20px;">No recordings for this date</div>';
            return;
        }

        grid.innerHTML = '';
        data.segments.forEach(seg => {
            const card = document.createElement('button');
            card.style.cssText = 'background:var(--bg-elevated,#1e1e2e); border:1px solid var(--border,#333); border-radius:8px; padding:12px; cursor:pointer; text-align:center; transition:all 0.2s;';
            card.innerHTML = `
                <div style="font-size:1.1em; font-weight:600; color:var(--text-primary,#e0e0e0);">${seg.time}</div>
                <div style="font-size:0.8em; color:var(--text-secondary,#888); margin-top:4px;">${seg.size_mb} MB</div>
            `;
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

    // Set source
    const url = `/api/recordings/stream/${date}/${filename}`;
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
