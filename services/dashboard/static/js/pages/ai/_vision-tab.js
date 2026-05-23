/**
 * ai/_vision-tab.js — Vision tab (Qwen-VL image/video understanding).
 *
 * Extracted from ai.js during the 2026-05-22 modularity split.
 *
 * Owns:
 *   - checkVisionStatus: poll /api/ai/vision/status, paint the indicator dot
 *   - DOMContentLoaded handler that wires the drag-drop zone + file input
 *   - loadVisionImage, clearVisionImage: image preview state
 *   - loadVisionVideo, extractVideoFrames: video → up to 6 frames @ ≤720px
 *   - VISION_PROMPTS + setVisionPrompt: preset prompt picker
 *   - analyzeVisionImage: POST /api/ai/vision with image(s) + prompt
 *   - copyVisionResult: copy assistant response to clipboard
 *
 * Cross-file references (resolved at call time, classic-script semantics):
 *   - visionImageBase64, visionVideoFrames, visionAnalyzing (_state.js)
 */

// ---------------------------------------------------------------------------
// Status Polling
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
// Drag & Drop + File Input
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
// Video Frame Extraction
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
// Preset Prompts
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
// Analyze
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
// Copy Result
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
