/**
 * ai/_wizard.js — first-run setup wizard.
 *
 * Extracted from ai.js during the 2026-05-22 modularity split.
 *
 * Three steps: collect user name → pick AI name → confirm. On finish, posts
 * to /api/ai/config and switches to the chat container. Reads/writes the
 * shared `aiConfig` state from _state.js.
 *
 * Cross-file references resolved at call time:
 *   - addMessage, showChat, checkOllamaStatus (in _chat.js)
 */

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
