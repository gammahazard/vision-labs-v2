/* ==========================================================================
   generate.js — Image generation frontend (ComfyUI integration)
   Features: batch generation, LoRA support, gallery, bulk download
   ========================================================================== */

(function () {
    'use strict';

    // --- State ---
    let currentPromptId = null;
    let isGenerating = false;
    let pollTimer = null;

    // --- Prompt revision tracking ---
    let _initialSnapshot = null;
    let _revisions = [];
    let _changeWatcher = null;
    let _lastSnapshot = null;

    function _snapshotParams() {
        return {
            prompt: ($('genPrompt') || {}).value || '',
            negative_prompt: ($('genNegative') || {}).value || '',
            model: ($('genModel') || {}).value || '',
            steps: parseInt(($('genSteps') || {}).value) || 20,
            cfg: parseFloat(($('genCfg') || {}).value) || 7,
            lora: ($('genLora') || {}).value || '',
            lora_strength: parseFloat(($('genLoraStrength') || {}).value) || 0.8,
            seed: parseInt(($('genSeed') || {}).value) || -1,
            width: selectedWidth,
            height: selectedHeight,
        };
    }

    function _diffParams(a, b) {
        var changes = {};
        var keys = ['prompt', 'negative_prompt', 'model', 'steps', 'cfg', 'lora', 'lora_strength', 'seed', 'width', 'height'];
        for (var k = 0; k < keys.length; k++) {
            var key = keys[k];
            if (String(a[key]) !== String(b[key])) {
                changes[key] = { from: a[key], to: b[key] };
            }
        }
        return Object.keys(changes).length > 0 ? changes : null;
    }

    function _startChangeWatcher() {
        _stopChangeWatcher();
        _lastSnapshot = JSON.parse(JSON.stringify(_initialSnapshot));
        _revisions = [];
        _changeWatcher = setInterval(function () {
            var current = _snapshotParams();
            var diff = _diffParams(_lastSnapshot, current);
            if (diff) {
                _revisions.push({
                    timestamp: new Date().toISOString(),
                    changes: diff,
                    snapshot: JSON.parse(JSON.stringify(current)),
                });
                _lastSnapshot = JSON.parse(JSON.stringify(current));
            }
        }, 2000);
    }

    function _stopChangeWatcher() {
        if (_changeWatcher) {
            clearInterval(_changeWatcher);
            _changeWatcher = null;
        }
    }
    let generatedImages = [];       // current batch results [{src, filename}]
    let selectedWidth = 1024;
    let selectedHeight = 1024;
    let _initDone = false;
    let bulkCancelled = false;      // cancel flag for bulk generation
    let img2imgFile = null;          // uploaded source image for img2img
    let _modelsLoaded = false;       // track if models have loaded
    let _lorasLoaded = false;        // track if LoRAs have loaded

    const $ = (id) => document.getElementById(id);

    // Enable buttons only after models + LoRAs have loaded
    function _checkReadyState() {
        if (_modelsLoaded && _lorasLoaded) {
            var genBtn = $('generateBtn');
            var sweepBtn = $('sweepToggleBtn');
            var sweepRunBtn = $('sweepRunBtn');
            if (genBtn && !isGenerating) genBtn.disabled = false;
            if (sweepBtn) sweepBtn.disabled = false;
            if (sweepRunBtn) sweepRunBtn.disabled = false;
        }
    }

    // --- Init ---
    let _comfyuiPollTimer = null;
    function initGenerateTab() {
        if (_initDone) {
            checkComfyUIStatus();
            return;
        }
        _initDone = true;

        checkComfyUIStatus();
        loadModels();
        loadLoras();

        // Poll ComfyUI status every 10s so the dot updates when it comes online
        if (!_comfyuiPollTimer) {
            _comfyuiPollTimer = setInterval(checkComfyUIStatus, 10000);
        }

        // Steps slider
        const stepsSlider = $('genSteps');
        if (stepsSlider) stepsSlider.addEventListener('input', () => {
            $('genStepsValue').textContent = stepsSlider.value;
        });

        // CFG slider
        const cfgSlider = $('genCfg');
        if (cfgSlider) cfgSlider.addEventListener('input', () => {
            $('genCfgValue').textContent = parseFloat(cfgSlider.value).toFixed(1);
        });

        // LoRA strength slider
        const loraSlider = $('genLoraStrength');
        if (loraSlider) loraSlider.addEventListener('input', () => {
            $('genLoraStrengthValue').textContent = parseFloat(loraSlider.value).toFixed(2);
        });

        // Dimension presets
        document.querySelectorAll('.dimension-preset').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.dimension-preset').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                const [w, h] = btn.dataset.dim.split('x').map(Number);
                selectedWidth = w;
                selectedHeight = h;
            });
        });

        // Seed random
        const seedBtn = $('genSeedRandom');
        if (seedBtn) seedBtn.addEventListener('click', () => { $('genSeed').value = -1; });

        // Denoise slider (img2img)
        const denoiseSlider = $('genDenoise');
        if (denoiseSlider) denoiseSlider.addEventListener('input', () => {
            $('genDenoiseValue').textContent = parseFloat(denoiseSlider.value).toFixed(2);
        });

        // img2img drag-and-drop
        const dropzone = $('img2imgDropzone');
        if (dropzone) {
            ['dragenter', 'dragover'].forEach(ev => {
                dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.add('dragover'); });
            });
            ['dragleave', 'drop'].forEach(ev => {
                dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.remove('dragover'); });
            });
            dropzone.addEventListener('drop', e => {
                const file = e.dataTransfer?.files?.[0];
                if (file && file.type.startsWith('image/')) setImg2imgFile(file);
            });
        }
    }

    // --- ComfyUI Status ---
    async function checkComfyUIStatus() {
        const dot = $('genStatusDot');
        const text = $('genStatusText');
        try {
            const resp = await fetch('/api/generate/status');
            const data = await resp.json();
            if (data.online) {
                dot.classList.add('online');
                text.textContent = 'Online';
                $('generateBtn').disabled = false;
            } else {
                dot.classList.remove('online');
                text.textContent = data.error || 'Offline';
                $('generateBtn').disabled = true;
            }
        } catch {
            dot.classList.remove('online');
            text.textContent = 'Offline';
            $('generateBtn').disabled = true;
        }
        checkVramMode();
    }

    // --- VRAM Management ---
    async function checkVramMode() {
        const btn = $('vramToggleBtn');
        if (!btn) return;
        try {
            const resp = await fetch('/api/generate/vram/mode');
            const data = await resp.json();
            updateVramButton(data.mode);
        } catch { /* ignore */ }
    }

    function updateVramButton(mode) {
        const btn = $('vramToggleBtn');
        const warn = $('vramWarning');
        if (!btn) return;
        if (mode === 'generate') {
            btn.textContent = '📋 Restore AI Chat';
            btn.classList.add('vram-free');
            if (warn) warn.style.display = 'block';
        } else {
            btn.textContent = '⚡ Free VRAM';
            btn.classList.remove('vram-free');
            if (warn) warn.style.display = 'none';
        }
        // Sync with main tab controller
        if (typeof window.updateTabStatesForVram === 'function') {
            window.updateTabStatesForVram(mode);
        }
    }

    async function toggleVram() {
        const btn = $('vramToggleBtn');
        if (!btn) return;
        const currentMode = btn.classList.contains('vram-free') ? 'generate' : 'chat';

        btn.disabled = true;
        btn.textContent = currentMode === 'generate' ? '⏳ Reloading...' : '⏳ Freeing...';

        try {
            const endpoint = currentMode === 'generate'
                ? '/api/generate/vram/restore'
                : '/api/generate/vram/free';
            const resp = await fetch(endpoint, { method: 'POST' });
            const data = await resp.json();
            updateVramButton(data.mode);
        } catch (e) {
            console.error('VRAM toggle error:', e);
        }
        btn.disabled = false;
    }

    // --- Load Models ---
    async function loadModels() {
        const select = $('genModel');
        if (!select) return;
        try {
            const resp = await fetch('/api/generate/models');
            const data = await resp.json();
            select.innerHTML = '';
            if (data.models && data.models.length > 0) {
                data.models.forEach(m => {
                    const opt = document.createElement('option');
                    opt.value = m;
                    opt.textContent = m.replace('.safetensors', '').replace(/_/g, ' ');
                    select.appendChild(opt);
                });
            } else {
                select.innerHTML = '<option value="">No models — add .safetensors to checkpoints/</option>';
            }
        } catch {
            select.innerHTML = '<option value="">ComfyUI offline</option>';
        }
        _modelsLoaded = true;
        _checkReadyState();
    }

    // --- Load LoRAs ---
    async function loadLoras() {
        const select = $('genLora');
        const sweepSelect = $('sweepLoraSelect');
        if (!select) return;
        try {
            const resp = await fetch('/api/generate/loras');
            const data = await resp.json();
            select.innerHTML = '<option value="">None</option>';
            if (sweepSelect) sweepSelect.innerHTML = '<option value="">(use main selection)</option>';
            if (data.loras && data.loras.length > 0) {
                data.loras.forEach(l => {
                    const opt = document.createElement('option');
                    opt.value = l;
                    opt.textContent = l.replace('.safetensors', '').replace(/_/g, ' ');
                    select.appendChild(opt);
                    // Also add to sweep dropdown
                    if (sweepSelect) {
                        const sopt = opt.cloneNode(true);
                        sweepSelect.appendChild(sopt);
                    }
                });
            }
        } catch {
            select.innerHTML = '<option value="">No LoRAs found</option>';
        }
        _lorasLoaded = true;
        _checkReadyState();
    }

    // --- Generate (handles both single and bulk) ---
    async function generateImage() {
        if (isGenerating) return;

        const prompt = $('genPrompt').value.trim();

        // img2img mode: image required, prompt optional
        if (img2imgFile) {
            return generateImg2Img();
        }

        if (!prompt) { $('genPrompt').focus(); return; }

        const totalCount = parseInt($('genBatch').value) || 1;

        // If more than 4 images, use bulk queue system
        if (totalCount > 4) {
            return bulkGenerate(totalCount);
        }

        // Standard single-batch generation
        const params = getGenParams();
        params.batch_size = totalCount;

        isGenerating = true;
        _initialSnapshot = _snapshotParams();
        _startChangeWatcher();
        $('generateBtn').disabled = true;
        $('generateBtn').classList.add('generating');
        $('generateBtn').innerHTML = '<span>⏳</span> Generating...';
        $('genCancelBtn').style.display = 'inline-flex';

        $('genOutput').innerHTML = `
            <div class="generate-loading">
                <div class="generate-loading-spinner"></div>
                <span class="generate-loading-text">Generating ${totalCount > 1 ? totalCount + ' images' : 'image'}...</span>
                <span class="generate-loading-progress" id="genProgress">Queuing prompt with ComfyUI</span>
            </div>
        `;

        try {
            const resp = await fetch('/api/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(params),
            });
            const data = await resp.json();
            if (data.error) { showError(data.error); resetButton(); return; }

            currentPromptId = data.prompt_id;
            pollForResult();
        } catch (e) {
            showError('Failed to connect to server: ' + e.message);
            resetButton();
        }
    }

    // --- Get generation parameters ---
    function getGenParams() {
        return {
            prompt: $('genPrompt').value.trim(),
            negative_prompt: $('genNegative').value.trim(),
            model: $('genModel').value,
            width: selectedWidth,
            height: selectedHeight,
            steps: parseInt($('genSteps').value),
            cfg: parseFloat($('genCfg').value),
            seed: parseInt($('genSeed').value),
            batch_size: 4,
            lora: $('genLora').value,
            lora_strength: parseFloat($('genLoraStrength').value),
        };
    }

    // --- Bulk generation queue ---
    async function bulkGenerate(totalCount) {
        isGenerating = true;
        bulkCancelled = false;
        $('generateBtn').disabled = true;
        $('generateBtn').classList.add('generating');
        $('generateBtn').innerHTML = '<span>⏳</span> Generating...';

        const batchSize = 4;  // max per ComfyUI batch
        const numBatches = Math.ceil(totalCount / batchSize);
        let allImages = [];
        let completed = 0;

        $('genOutput').innerHTML = `
            <div class="generate-loading">
                <div class="generate-loading-spinner"></div>
                <span class="generate-loading-text">Bulk generating ${totalCount} images...</span>
                <div class="bulk-progress">
                    <div class="bulk-progress-bar" id="bulkProgressBar" style="width: 0%;"></div>
                </div>
                <span class="generate-loading-progress" id="genProgress">Starting batch 1/${numBatches}...</span>
                <button class="bulk-cancel-btn" onclick="window._genCancelBulk()">✕ Cancel</button>
            </div>
        `;

        for (let batch = 0; batch < numBatches; batch++) {
            if (bulkCancelled) break;

            const remaining = totalCount - completed;
            const thisBatchSize = Math.min(batchSize, remaining);
            const params = getGenParams();
            params.batch_size = thisBatchSize;
            // Use random seed for each batch so images are different
            params.seed = -1;

            const prog = $('genProgress');
            if (prog) prog.textContent = `Batch ${batch + 1}/${numBatches} — queuing ${thisBatchSize} images...`;

            try {
                const images = await queueAndWaitForBatch(params, batch + 1, numBatches);
                if (bulkCancelled) break;

                allImages = allImages.concat(images);
                completed += images.length;

                // Update progress bar
                const pct = Math.round((completed / totalCount) * 100);
                const bar = $('bulkProgressBar');
                if (bar) bar.style.width = pct + '%';
                if (prog) prog.textContent = `${completed}/${totalCount} images complete`;
            } catch (e) {
                if (prog) prog.textContent = `Batch ${batch + 1} failed: ${e.message}. Continuing...`;
                await new Promise(r => setTimeout(r, 2000));
            }
        }

        if (allImages.length > 0) {
            showResults(allImages);
        } else {
            showError(bulkCancelled ? 'Bulk generation cancelled' : 'No images generated');
        }
        resetButton();
    }

    // --- Queue a single batch and wait for completion ---
    function queueAndWaitForBatch(params, batchNum, totalBatches, label) {
        var progressLabel = label || 'Batch';
        return new Promise(async (resolve, reject) => {
            try {
                const resp = await fetch('/api/generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(params),
                });
                const data = await resp.json();
                if (data.error) return reject(new Error(data.error));

                const promptId = data.prompt_id;
                let attempts = 0;
                let batchPolling = false;  // reentrance guard

                const timer = setInterval(async () => {
                    if (bulkCancelled) {
                        clearInterval(timer);
                        return resolve([]);
                    }
                    if (batchPolling) return;  // skip if previous poll still in-flight
                    batchPolling = true;

                    attempts++;
                    try {
                        const r = await fetch(`/api/generate/history/${promptId}`);
                        const d = await r.json();

                        if (d.status === 'complete' && (d.images || d.image)) {
                            clearInterval(timer);
                            const imgs = d.images || [{ image: d.image, filename: d.filename }];
                            resolve(imgs);
                        } else if (d.status === 'error') {
                            clearInterval(timer);
                            reject(new Error(d.error || 'Generation failed'));
                        } else {
                            const prog = $('genProgress');
                            if (prog) prog.textContent = `${progressLabel} ${batchNum}/${totalBatches} \u2014 processing (${attempts}s)...`;
                        }
                    } catch { /* retry */ } finally {
                        batchPolling = false;
                    }

                    if (attempts > 1800) {
                        clearInterval(timer);
                        reject(new Error(progressLabel + ' timed out after 30 minutes'));
                    }
                }, 1000);
            } catch (e) {
                reject(e);
            }
        });
    }

    // --- Poll for result (single batch) ---
    let isPolling = false;
    function pollForResult() {
        if (!currentPromptId) return;
        let attempts = 0;

        pollTimer = setInterval(async () => {
            if (isPolling) return;  // skip if previous poll still in-flight
            isPolling = true;
            attempts++;
            const prog = $('genProgress');
            try {
                const resp = await fetch(`/api/generate/history/${currentPromptId}`);
                const data = await resp.json();
                console.log(`[gen poll #${attempts}]`, data.status, data.error || '');

                if (data.status === 'complete' && (data.images || data.image)) {
                    clearInterval(pollTimer);
                    const imgs = data.images || [{ image: data.image, filename: data.filename }];
                    showResults(imgs);
                    _savePromptHistory();
                    resetButton();
                } else if (data.status === 'error') {
                    clearInterval(pollTimer);
                    showError(data.error || 'Generation failed');
                    resetButton();
                } else if (data.error) {
                    clearInterval(pollTimer);
                    showError(data.error);
                    resetButton();
                } else if (prog) {
                    prog.textContent = `Processing... (${attempts}s)`;
                }
            } catch (pollErr) {
                console.warn('[gen poll error]', pollErr);
            } finally {
                isPolling = false;
            }

            if (attempts > 1800) {
                clearInterval(pollTimer);
                showError('Generation timed out after 30 minutes');
                resetButton();
            }
        }, 1000);
    }

    // --- Show results (supports batch) ---
    function showResults(images) {
        generatedImages = images.map((img, i) => ({
            src: `data:image/png;base64,${img.image}`,
            filename: img.filename || `visionlabs_${Date.now()}_${i}.png`,
        }));

        const isBatch = generatedImages.length > 1;
        const gridClass = isBatch ? 'generate-results-grid' : '';

        let html = `<div class="${gridClass}">`;
        generatedImages.forEach((img, i) => {
            html += `
                <div class="generate-result-item" onclick="window._genPreview(${i})">
                    <img src="${img.src}" alt="Generated ${i + 1}" loading="lazy">
                </div>
            `;
        });
        html += '</div>';

        html += `
            <div class="generate-image-actions">
                ${generatedImages.length > 1 ? `<button onclick="window._genDownloadAll()">📦 Download All (${generatedImages.length})</button>` : ''}
                <button onclick="window._genDownload()">💾 Download${isBatch ? ' Selected' : ''}</button>
                <button onclick="window._genCopyPrompt()">📋 Copy Prompt</button>
                <button onclick="window._genRegenerate()">🔄 Generate Again</button>
            </div>
        `;

        $('genOutput').innerHTML = html;

        // Auto-select first image for preview
        if (generatedImages.length === 1) {
            _genPreviewIndex = 0;
        }
    }

    let _genPreviewIndex = 0;

    // --- Error ---
    function showError(msg) {
        $('genOutput').innerHTML = `
            <div class="generate-error">âš ï¸ ${msg}</div>
            <div class="generate-output-placeholder">
                <span class="placeholder-icon">🎨</span>
                <span>Try again or check ComfyUI status</span>
            </div>
        `;
    }

    // --- Reset button ---
    function resetButton() {
        isGenerating = false;
        _stopChangeWatcher();
        $('generateBtn').disabled = false;
        $('generateBtn').classList.remove('generating');
        $('generateBtn').innerHTML = '<span>🎨</span> Generate';
        $('genCancelBtn').style.display = 'none';
        checkComfyUIStatus();
    }

    // --- Gallery (Modal) ---
    let galleryOpen = false;
    const GALLERY_LIMIT = 50;

    async function loadGallery(offset = 0) {
        const body = $('galleryModalBody');
        const footer = $('galleryModalFooter');
        const count = $('galleryCount');

        body.innerHTML = `
            <div class="generate-loading" style="padding:60px 0;">
                <div class="generate-loading-spinner"></div>
                <span class="generate-loading-text">Loading gallery...</span>
            </div>
        `;
        footer.style.display = 'none';

        try {
            const resp = await fetch(`/api/generate/gallery?limit=${GALLERY_LIMIT}&offset=${offset}`);
            const data = await resp.json();

            if (!data.images || data.images.length === 0) {
                body.innerHTML = `
                    <div class="generate-output-placeholder" style="padding:60px 0;">
                        <span class="placeholder-icon">📂</span>
                        <span>No generated images found yet</span>
                    </div>
                `;
                if (count) count.textContent = '0 images';
                return;
            }

            if (count) count.textContent = `${data.total} images`;

            let html = '<div class="gallery-grid">';
            _lightboxImages = data.images.map(img => img.filename);
            data.images.forEach((img, idx) => {
                const sourceBadge = img.source === 'qnap' ? '💾' : 'ðŸ–¼ï¸';
                html += `
                    <div class="gallery-item" onclick="window._genGalleryPreview('${img.filename}', ${idx})">
                        <img src="/api/generate/gallery/image/${encodeURIComponent(img.filename)}" alt="${img.filename}" loading="lazy">
                        <div class="gallery-item-info">
                            <span class="gallery-item-source">${sourceBadge}</span>
                            <span class="gallery-item-size">${img.size_kb} KB</span>
                        </div>
                    </div>
                `;
            });
            html += '</div>';
            body.innerHTML = html;

            // Pagination
            if (data.total > GALLERY_LIMIT) {
                const totalPages = Math.ceil(data.total / GALLERY_LIMIT);
                const currentPage = Math.floor(offset / GALLERY_LIMIT) + 1;

                let pHtml = '';
                if (offset > 0) {
                    pHtml += `<button onclick="window._genGalleryPage(${Math.max(0, offset - GALLERY_LIMIT)})">← Newer</button>`;
                }

                // Page jump dropdown
                pHtml += `<select onchange="window._genGalleryPage((this.value - 1) * ${GALLERY_LIMIT})" style="
                    background: var(--surface-2, #2a2a3e); color: var(--text-primary, #e0e0e0);
                    border: 1px solid var(--border, #3a3a4e); border-radius: 6px;
                    padding: 4px 8px; font-size: 0.85rem; cursor: pointer;
                ">`;
                for (let p = 1; p <= totalPages; p++) {
                    pHtml += `<option value="${p}" ${p === currentPage ? 'selected' : ''}>Page ${p} of ${totalPages}</option>`;
                }
                pHtml += `</select>`;

                pHtml += `<span style="font-size:0.8rem;opacity:0.7">${offset + 1}–${Math.min(offset + GALLERY_LIMIT, data.total)} of ${data.total}</span>`;

                if (offset + GALLERY_LIMIT < data.total) {
                    pHtml += `<button onclick="window._genGalleryPage(${offset + GALLERY_LIMIT})">Older →</button>`;
                }
                footer.innerHTML = pHtml;
                footer.style.display = 'flex';
            }
        } catch (e) {
            body.innerHTML = `<div class="generate-error" style="padding:40px;">âš ï¸ Failed to load gallery: ${e.message}</div>`;
        }
    }

    function toggleGallery() {
        const modal = $('galleryModal');
        if (!modal) return;
        galleryOpen = !galleryOpen;
        if (galleryOpen) {
            modal.style.display = 'flex';
            loadGallery(0);
        } else {
            modal.style.display = 'none';
        }
    }

    // --- Global functions ---
    window._genPreview = function (index) {
        _genPreviewIndex = index;
        // Highlight selected
        document.querySelectorAll('.generate-result-item').forEach((el, i) => {
            el.classList.toggle('selected', i === index);
        });
    };

    window._genDownload = function () {
        const img = generatedImages[_genPreviewIndex];
        if (!img) return;
        const a = document.createElement('a');
        a.href = img.src;
        a.download = img.filename;
        a.click();
    };

    window._genDownloadAll = function () {
        generatedImages.forEach((img, i) => {
            setTimeout(() => {
                const a = document.createElement('a');
                a.href = img.src;
                a.download = img.filename;
                a.click();
            }, i * 300); // stagger downloads
        });
    };

    window._genCopyPrompt = function () {
        navigator.clipboard.writeText($('genPrompt').value).catch(() => { });
    };

    window._genRegenerate = function () {
        generateImage();
    };

    window._genCancelBulk = function () {
        bulkCancelled = true;
        const prog = $('genProgress');
        if (prog) prog.textContent = 'Cancelling...';
    };

    // --- Cancel current generation (interrupts ComfyUI) ---
    window._genCancel = async function () {
        try {
            await fetch('/api/generate/cancel', { method: 'POST' });
        } catch (e) {
            console.warn('Cancel error:', e);
        }
        if (pollTimer) clearInterval(pollTimer);
        $('genOutput').innerHTML = '<div class="generate-loading"><span class="generate-loading-text">Generation cancelled</span></div>';
        resetButton();
    };

    // --- Parameter Sweep ---
    let sweepCancelled = false;

    window._genToggleSweep = function () {
        var panel = $('sweepPanel');
        if (!panel) return;
        var visible = panel.style.display !== 'none';
        panel.style.display = visible ? 'none' : 'block';
        if (!visible) _updateSweepComboCount();
    };

    function _parseSweepValues(inputId) {
        var raw = $(inputId).value.trim();
        if (!raw) return [];
        return raw.split(',').map(function (v) { return parseFloat(v.trim()); }).filter(function (v) { return !isNaN(v); });
    }

    function _getSweepCombos() {
        var base = getGenParams();
        var axes = [];

        if ($('sweepStepsEnabled').checked) {
            var vals = _parseSweepValues('sweepStepsValues');
            if (vals.length > 0) axes.push({ key: 'steps', values: vals });
        }
        if ($('sweepCfgEnabled').checked) {
            var vals = _parseSweepValues('sweepCfgValues');
            if (vals.length > 0) axes.push({ key: 'cfg', values: vals });
        }
        if ($('sweepLoraEnabled').checked) {
            var vals = _parseSweepValues('sweepLoraValues');
            if (vals.length > 0) axes.push({ key: 'lora_strength', values: vals });
        }

        // Determine which LoRA to use for sweep
        var sweepLoraSelect = $('sweepLoraSelect');
        var sweepLora = (sweepLoraSelect && sweepLoraSelect.value) ? sweepLoraSelect.value : null;

        if (axes.length === 0) return [base];

        // Cartesian product
        var combos = [{}];
        for (var a = 0; a < axes.length; a++) {
            var axis = axes[a];
            var newCombos = [];
            for (var c = 0; c < combos.length; c++) {
                for (var v = 0; v < axis.values.length; v++) {
                    var copy = JSON.parse(JSON.stringify(combos[c]));
                    copy[axis.key] = axis.values[v];
                    newCombos.push(copy);
                }
            }
            combos = newCombos;
        }

        // Fix seed across all combos for fair comparison (unless random seed is checked)
        var fixedSeed = base.seed;
        if (fixedSeed < 0) fixedSeed = Math.floor(Math.random() * 4294967295);
        var useRandomSeed = ($('sweepRandomSeed') || {}).checked || false;

        // Per-combo batch count
        var sweepBatch = parseInt(($('sweepBatchCount') || {}).value) || 1;
        sweepBatch = Math.max(1, Math.min(sweepBatch, 4));  // backend clamps to 4 max

        return combos.map(function (overrides, idx) {
            var params = JSON.parse(JSON.stringify(base));
            params.batch_size = sweepBatch;
            // Apply sweep overrides FIRST (steps, cfg, lora_strength)
            Object.keys(overrides).forEach(function (k) {
                params[k] = overrides[k];
            });
            // Then set seed AFTER overrides (so sweep can't accidentally overwrite it)
            params.seed = useRandomSeed ? Math.floor(Math.random() * 4294967295) : fixedSeed;
            console.log('[Sweep] Combo ' + (idx + 1) + ' seed=' + params.seed + ' useRandom=' + useRandomSeed);
            // LoRA strength 0 = disable LoRA
            if (params.lora_strength === 0) {
                params.lora = '';
            }
            // Override LoRA with sweep-specific selection if set
            if (sweepLora && params.lora_strength > 0) {
                params.lora = sweepLora;
            }
            return params;
        });
    }

    function _updateSweepComboCount() {
        var combos = _getSweepCombos();
        var count = combos.length;
        var batchPer = parseInt(($('sweepBatchCount') || {}).value) || 1;
        var totalImages = count * batchPer;
        var el = $('sweepComboCount');
        if (el) {
            var est = Math.ceil(totalImages * 0.5);
            var label = count + ' combo' + (count !== 1 ? 's' : '');
            if (batchPer > 1) label += ' × ' + batchPer + ' = ' + totalImages + ' images';
            label += ' — ~' + est + ' min';
            el.textContent = label;
        }
    }

    // Attach change listeners for live combo count
    ['sweepStepsEnabled', 'sweepCfgEnabled', 'sweepLoraEnabled'].forEach(function (id) {
        var el = document.getElementById(id);
        if (el) el.addEventListener('change', _updateSweepComboCount);
    });
    ['sweepStepsValues', 'sweepCfgValues', 'sweepLoraValues'].forEach(function (id) {
        var el = document.getElementById(id);
        if (el) el.addEventListener('input', _updateSweepComboCount);
    });
    // Listen for batch count changes
    if (document.getElementById('sweepBatchCount')) {
        document.getElementById('sweepBatchCount').addEventListener('input', _updateSweepComboCount);
    }
    // Also listen for sweep LoRA dropdown changes
    if (document.getElementById('sweepLoraSelect')) {
        document.getElementById('sweepLoraSelect').addEventListener('change', _updateSweepComboCount);
    }

    window._genRunSweep = async function () {
        if (isGenerating) return;

        var prompt = $('genPrompt').value.trim();
        if (!prompt) { $('genPrompt').focus(); return; }

        var combos = _getSweepCombos();
        if (combos.length === 0) return;
        if (combos.length > 100) {
            if (!confirm('This will generate ' + combos.length + ' images. Continue?')) return;
        }

        isGenerating = true;
        sweepCancelled = false;
        bulkCancelled = false;
        $('generateBtn').disabled = true;
        $('generateBtn').classList.add('generating');
        $('generateBtn').innerHTML = '<span>🧪</span> Sweeping...';
        $('sweepPanel').style.display = 'none';

        var results = [];  // {params, images}
        var baseParams = getGenParams();  // original UI params for label comparison

        // Build initial sweep UI with progress + grid that fills incrementally
        $('genOutput').innerHTML = '<div class="sweep-results">' +
            '<div class="sweep-results-header" id="sweepHeader">🧪 Sweep: 0/' + combos.length + ' combos</div>' +
            '<div class="bulk-progress"><div class="bulk-progress-bar" id="sweepProgressBar" style="width:0%"></div></div>' +
            '<span class="generate-loading-progress" id="genProgress">Starting combo 1/' + combos.length + '...</span>' +
            '<button class="bulk-cancel-btn" onclick="window._genCancelSweep()">✕ Cancel Sweep</button>' +
            '<div class="sweep-results-grid" id="sweepGrid"></div>' +
            '</div>';

        for (var i = 0; i < combos.length; i++) {
            if (sweepCancelled) break;

            var params = combos[i];
            var prog = $('genProgress');
            if (prog) {
                var label = _sweepParamLabel(params, baseParams);
                prog.textContent = 'Combo ' + (i + 1) + '/' + combos.length + ': ' + label;
            }

            try {
                var images = await queueAndWaitForBatch(params, i + 1, combos.length, 'Combo');
                // Push results BEFORE checking cancel so partial results are kept
                if (images && images.length > 0) {
                    results.push({ params: params, images: images });
                    // Append this combo's results to the grid immediately
                    _appendSweepCard(params, images, baseParams);
                }
                if (sweepCancelled) break;

                var pct = Math.round(((i + 1) / combos.length) * 100);
                var bar = $('sweepProgressBar');
                if (bar) bar.style.width = pct + '%';
                var header = $('sweepHeader');
                if (header) header.textContent = '🧪 Sweep: ' + (i + 1) + '/' + combos.length + ' combos';
            } catch (e) {
                if (prog) prog.textContent = 'Combo ' + (i + 1) + ' failed: ' + e.message + '. Continuing...';
                results.push({ params: params, images: [], error: e.message });
                if (sweepCancelled) break;
                await new Promise(function (r) { setTimeout(r, 2000); });
            }
        }

        // Finalize header and remove progress elements
        var totalImages = results.reduce(function (s, r) { return s + (r.images ? r.images.length : 0); }, 0);
        var cancelNote = sweepCancelled ? ' (cancelled after ' + results.length + '/' + combos.length + ' combos)' : '';
        var header = $('sweepHeader');
        if (header) header.textContent = '🧪 Sweep Results — ' + results.length + ' combos, ' + totalImages + ' images' + cancelNote;
        var progEl = $('genProgress');
        if (progEl) progEl.remove();
        var cancelBtn = $('genOutput') ? $('genOutput').querySelector('.bulk-cancel-btn') : null;
        if (cancelBtn) cancelBtn.remove();
        var progBar = $('genOutput') ? $('genOutput').querySelector('.bulk-progress') : null;
        if (progBar) progBar.remove();

        if (results.length > 0) {
            _saveSweepHistory(results);
        } else {
            $('genOutput').innerHTML = '<div class="generate-error">⚠️ Sweep ' + (sweepCancelled ? 'cancelled' : 'completed') + ' — no results</div>';
        }
        resetButton();
    };

    window._genCancelSweep = async function () {
        sweepCancelled = true;
        bulkCancelled = true;  // also stops queueAndWaitForBatch
        try {
            await fetch('/api/generate/cancel', { method: 'POST' });
        } catch (e) { /* ignore */ }
    };

    function _appendSweepCard(params, images, baseParams) {
        var grid = $('sweepGrid');
        if (!grid) return;

        var card = document.createElement('div');
        card.className = 'sweep-result-card';

        var imgsHtml = '<div class="sweep-result-images">';
        for (var j = 0; j < images.length; j++) {
            var img = images[j];
            var src = img.image ? 'data:image/png;base64,' + img.image : '/api/generate/gallery/image/' + encodeURIComponent(img.filename);
            imgsHtml += '<img class="sweep-result-img" src="' + src + '" alt="Sweep result" onclick="window._genGalleryPreview(\'' + (img.filename || '') + '\')">';
        }
        imgsHtml += '</div>';

        var labelsHtml = '<div class="sweep-result-labels">';
        labelsHtml += '<span class="sweep-label">Steps: ' + params.steps + '</span>';
        labelsHtml += '<span class="sweep-label">CFG: ' + params.cfg + '</span>';
        if (params.lora) {
            labelsHtml += '<span class="sweep-label sweep-label-lora">LoRA: ' + parseFloat(params.lora_strength).toFixed(2) + '</span>';
        } else {
            labelsHtml += '<span class="sweep-label sweep-label-nolora">No LoRA</span>';
        }
        labelsHtml += '</div>';

        card.innerHTML = imgsHtml + labelsHtml;
        grid.appendChild(card);
    }

    function _sweepParamLabel(params, baseParams) {
        var parts = [];
        if (params.steps !== baseParams.steps) parts.push('Steps:' + params.steps);
        if (params.cfg !== baseParams.cfg) parts.push('CFG:' + params.cfg);
        if (params.lora) {
            parts.push('LoRA:' + parseFloat(params.lora_strength).toFixed(2));
        } else if (baseParams.lora) {
            parts.push('No LoRA');
        }
        // If nothing differs (single-axis sweep shows at least the swept param)
        if (parts.length === 0) parts.push('Steps:' + params.steps + ' | CFG:' + params.cfg);
        return parts.join(' | ');
    }

    function _showSweepResults(results, cancelNote) {
        cancelNote = cancelNote || '';
        var totalImages = results.reduce(function (s, r) { return s + (r.images ? r.images.length : 0); }, 0);
        var html = '<div class="sweep-results">';
        html += '<div class="sweep-results-header">🧪 Sweep Results — ' + results.length + ' combos, ' + totalImages + ' images' + cancelNote + '</div>';
        html += '<div class="sweep-results-grid">';

        for (var i = 0; i < results.length; i++) {
            var r = results[i];
            var p = r.params;
            html += '<div class="sweep-result-card">';

            if (r.images && r.images.length > 0) {
                html += '<div class="sweep-result-images">';
                for (var j = 0; j < r.images.length; j++) {
                    var img = r.images[j];
                    var src = img.image ? 'data:image/png;base64,' + img.image : '/api/generate/gallery/image/' + encodeURIComponent(img.filename);
                    html += '<img class="sweep-result-img" src="' + src + '" alt="Sweep result" onclick="window._genGalleryPreview(\'' + (img.filename || '') + '\')">';
                }
                html += '</div>';
            } else {
                html += '<div class="sweep-result-error">' + (r.error || 'Failed') + '</div>';
            }

            html += '<div class="sweep-result-labels">';
            html += '<span class="sweep-label">Steps: ' + p.steps + '</span>';
            html += '<span class="sweep-label">CFG: ' + p.cfg + '</span>';
            if (p.lora) {
                html += '<span class="sweep-label sweep-label-lora">LoRA: ' + parseFloat(p.lora_strength).toFixed(2) + '</span>';
            } else {
                html += '<span class="sweep-label sweep-label-nolora">No LoRA</span>';
            }
            html += '</div>';
            html += '</div>';
        }

        html += '</div></div>';
        $('genOutput').innerHTML = html;
    }

    function _saveSweepHistory(results) {
        var params = results[0] ? results[0].params : {};
        var entry = {
            prompt: params.prompt || '',
            negative_prompt: params.negative_prompt || '',
            model: params.model || '',
            steps: 'sweep',
            cfg: 'sweep',
            lora: params.lora || '',
            lora_strength: 'sweep',
            width: params.width,
            height: params.height,
            seed: params.seed,
            timestamp: new Date().toISOString(),
            sweep: true,
            sweep_count: results.length,
            sweep_results: results.map(function (r) {
                var img = (r.images && r.images.length > 0) ? r.images[0] : null;
                return {
                    steps: r.params.steps,
                    cfg: r.params.cfg,
                    lora: r.params.lora || '',
                    lora_strength: r.params.lora_strength,
                    filename: img ? (img.filename || '') : '',
                    error: r.error || null,
                };
            }),
        };
        fetch('/api/generate/prompt-history', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(entry),
        }).catch(function (e) { console.warn('Failed to save sweep history:', e); });
    }

    // --- Lightbox state ---
    let _lightboxImages = [];   // Array of filenames from current gallery page
    let _lightboxIndex = 0;     // Current image index
    let _lightboxMeta = null;   // Last fetched metadata for import

    window._genGalleryPreview = function (filename, index) {
        const modal = document.getElementById('lightboxModal');
        const img = document.getElementById('lightboxImg');
        const info = document.getElementById('lightboxInfo');
        const counter = document.getElementById('lightboxCounter');
        if (!modal || !img) return;

        if (typeof index === 'number') _lightboxIndex = index;
        img.src = `/api/generate/gallery/image/${encodeURIComponent(filename)}`;
        if (info) info.innerHTML = `<span class="lightbox-filename">${filename}</span><div class="lightbox-meta" id="lightboxMeta">Loading metadata...</div>`;
        if (counter && _lightboxImages.length > 1) {
            counter.textContent = `${_lightboxIndex + 1} / ${_lightboxImages.length}`;
        } else if (counter) {
            counter.textContent = '';
        }

        // Show/hide arrows
        const prev = document.getElementById('lightboxPrev');
        const next = document.getElementById('lightboxNext');
        if (prev) prev.style.display = _lightboxImages.length > 1 ? 'flex' : 'none';
        if (next) next.style.display = _lightboxImages.length > 1 ? 'flex' : 'none';

        modal.style.display = 'flex';

        // Fetch metadata asynchronously
        _lightboxMeta = null;
        fetch(`/api/generate/gallery/metadata/${encodeURIComponent(filename)}`)
            .then(function (r) { return r.json(); })
            .then(function (meta) {
                _lightboxMeta = meta;
                var el = document.getElementById('lightboxMeta');
                if (!el) return;
                if (meta.error && !meta.prompt_text) {
                    el.textContent = meta.size || '';
                    return;
                }
                var html = '';
                if (meta.prompt_text) {
                    html += '<div class="lightbox-meta-prompt">' + meta.prompt_text + '</div>';
                }
                if (meta.negative_prompt) {
                    html += '<div class="lightbox-meta-neg">Neg: ' + meta.negative_prompt.substring(0, 120) + '</div>';
                }
                var badges = [];
                if (meta.model) badges.push('Model: ' + meta.model.replace('.safetensors', ''));
                if (meta.steps) badges.push('Steps: ' + meta.steps);
                if (meta.cfg) badges.push('CFG: ' + meta.cfg);
                if (meta.seed) badges.push('Seed: ' + meta.seed);
                if (meta.sampler) badges.push(meta.sampler);
                if (meta.lora) badges.push('LoRA: ' + meta.lora.replace('.safetensors', '') + ' @ ' + (meta.lora_strength || '?'));
                if (meta.size) badges.push(meta.size);
                if (badges.length > 0) {
                    html += '<div class="lightbox-meta-badges">' + badges.map(function (b) { return '<span class="lightbox-meta-badge">' + b + '</span>'; }).join('') + '</div>';
                }
                // Import button — only show if we have meaningful metadata
                if (meta.prompt_text || meta.model || meta.steps) {
                    html += '<button class="lightbox-import-btn" onclick="event.stopPropagation(); window._importLightboxSettings();">\u2B06 Use These Settings</button>';
                }
                el.innerHTML = html || '';
            })
            .catch(function () {
                _lightboxMeta = null;
                var el = document.getElementById('lightboxMeta');
                if (el) el.textContent = '';
            });
    };

    window._lightboxNav = function (direction) {
        if (_lightboxImages.length <= 1) return;
        _lightboxIndex = (_lightboxIndex + direction + _lightboxImages.length) % _lightboxImages.length;
        window._genGalleryPreview(_lightboxImages[_lightboxIndex], _lightboxIndex);
    };

    window._closeLightbox = function (event) {
        // Only close when clicking the overlay background itself, not child elements
        if (event && event.target !== event.currentTarget) return;
        const modal = document.getElementById('lightboxModal');
        if (modal) modal.style.display = 'none';
    };

    // --- Import lightbox metadata into generate form ---
    window._importLightboxSettings = function () {
        var meta = _lightboxMeta;
        if (!meta) return;

        // Populate prompt
        var promptEl = $('genPrompt');
        if (promptEl && meta.prompt_text) promptEl.value = meta.prompt_text;

        // Populate negative prompt
        var negEl = $('genNegative');
        if (negEl && meta.negative_prompt) negEl.value = meta.negative_prompt;

        // Populate steps
        var stepsEl = $('genSteps');
        if (stepsEl && meta.steps) stepsEl.value = meta.steps;

        // Populate CFG
        var cfgEl = $('genCfg');
        if (cfgEl && meta.cfg) cfgEl.value = meta.cfg;

        // Populate seed
        var seedEl = $('genSeed');
        if (seedEl && meta.seed) seedEl.value = meta.seed;

        // Populate model dropdown (try exact match, then partial)
        var modelEl = $('genModel');
        if (modelEl && meta.model) {
            var modelName = meta.model;
            var found = false;
            for (var i = 0; i < modelEl.options.length; i++) {
                if (modelEl.options[i].value === modelName || modelEl.options[i].value.indexOf(modelName) !== -1) {
                    modelEl.value = modelEl.options[i].value;
                    found = true;
                    break;
                }
            }
        }

        // Populate LoRA dropdown
        var loraEl = $('genLora');
        if (loraEl && meta.lora) {
            var loraName = meta.lora;
            for (var j = 0; j < loraEl.options.length; j++) {
                if (loraEl.options[j].value === loraName || loraEl.options[j].value.indexOf(loraName) !== -1) {
                    loraEl.value = loraEl.options[j].value;
                    break;
                }
            }
        }

        // Populate LoRA strength
        var loraStrEl = $('genLoraStrength');
        if (loraStrEl && meta.lora_strength) loraStrEl.value = meta.lora_strength;

        // Close lightbox
        var modal = document.getElementById('lightboxModal');
        if (modal) modal.style.display = 'none';

        // Scroll to generate form and switch to Generate sub-tab if needed
        var genSection = $('genPrompt');
        if (genSection) genSection.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };

    // Keyboard navigation: Escape=close, Left/Right=prev/next
    document.addEventListener('keydown', function (e) {
        const lb = document.getElementById('lightboxModal');
        if (!lb || lb.style.display === 'none') return;
        if (e.key === 'Escape') lb.style.display = 'none';
        else if (e.key === 'ArrowLeft') window._lightboxNav(-1);
        else if (e.key === 'ArrowRight') window._lightboxNav(1);
    });

    // --- Prompt History (with revision tracking) — server-side storage ---
    var _historyCache = [];  // cached from last server fetch

    function _savePromptHistory() {
        _stopChangeWatcher();
        try {
            var finalSnap = _snapshotParams();
            var initial = _initialSnapshot || finalSnap;
            var entry = {
                prompt: initial.prompt || '',
                negative_prompt: initial.negative_prompt || '',
                model: initial.model || '',
                steps: initial.steps,
                cfg: initial.cfg,
                lora: initial.lora || '',
                lora_strength: initial.lora_strength,
                width: initial.width,
                height: initial.height,
                seed: initial.seed,
                timestamp: new Date().toISOString(),
                revisions: _revisions.length > 0 ? _revisions : undefined,
                final_snapshot: _revisions.length > 0 ? finalSnap : undefined,
            };
            fetch('/api/generate/prompt-history', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(entry),
            }).catch(function (e) { console.warn('Failed to save prompt history:', e); });
        } catch (e) {
            console.warn('Failed to save prompt history:', e);
        }
        _initialSnapshot = null;
        _revisions = [];
    }

    function _renderParamBadges(h) {
        var modelName = (h.model || 'default').replace('.safetensors', '').replace(/_/g, ' ');
        return '<div class="prompt-history-meta">' +
            '<span>' + modelName + '</span>' +
            '<span>' + h.width + '\u00D7' + h.height + '</span>' +
            '<span>Steps: ' + h.steps + '</span>' +
            '<span>CFG: ' + h.cfg + '</span>' +
            (h.lora ? '<span>LoRA: ' + h.lora.replace('.safetensors', '') + '</span>' : '') +
            '</div>';
    }

    function _renderChangeBadges(changes) {
        var html = '<div class="prompt-history-changes">';
        var keys = Object.keys(changes);
        for (var c = 0; c < keys.length; c++) {
            var key = keys[c];
            var ch = changes[key];
            var label = key.replace(/_/g, ' ');
            var fromVal = String(ch.from).substring(0, 30);
            var toVal = String(ch.to).substring(0, 30);
            html += '<span class="prompt-history-change-badge" title="' + label + ': ' + fromVal + ' \u2192 ' + toVal + '">' + label + ': ' + toVal + '</span>';
        }
        html += '</div>';
        return html;
    }

    window._genShowHistory = function () {
        var modal = $('promptHistoryModal');
        if (!modal) return;
        var list = $('promptHistoryList');
        list.innerHTML = '<div class="prompt-history-empty">Loading...</div>';
        modal.style.display = 'flex';

        fetch('/api/generate/prompt-history').then(function (r) { return r.json(); }).then(function (data) {
            var history = data.history || [];
            _historyCache = history;

            if (history.length === 0) {
                list.innerHTML = '<div class="prompt-history-empty">No prompt history yet. Generate some images first!</div>';
            } else {
                list.innerHTML = history.map(function (h, i) {
                    var dt = new Date(h.timestamp);
                    var timeStr = dt.toLocaleDateString() + ' ' + dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                    var hasRevisions = h.revisions && h.revisions.length > 0;

                    var html = '<div class="prompt-history-card">';

                    // Sweep entry — render mini comparison grid
                    if (h.sweep && h.sweep_results) {
                        html += '<div class="prompt-history-card-main">';
                        html += '<div class="prompt-history-time">' + timeStr + ' <span class="prompt-history-rev-badge">🧪 Sweep — ' + h.sweep_count + ' combos</span></div>';
                        html += '<div class="prompt-history-prompt">' + (h.prompt || '(empty)').substring(0, 120) + '</div>';
                        html += '<div class="sweep-history-grid">';
                        for (var s = 0; s < h.sweep_results.length; s++) {
                            var sr = h.sweep_results[s];
                            html += '<div class="sweep-history-item">';
                            if (sr.filename) {
                                html += '<img class="sweep-history-thumb" src="/api/generate/gallery/image/' + encodeURIComponent(sr.filename) + '" alt="Sweep" onclick="window._genGalleryPreview(\'' + sr.filename + '\')">';
                            } else {
                                html += '<div class="sweep-history-nothumb">' + (sr.error || '—') + '</div>';
                            }
                            html += '<div class="sweep-result-labels">';
                            html += '<span class="sweep-label">S:' + sr.steps + '</span>';
                            html += '<span class="sweep-label">C:' + sr.cfg + '</span>';
                            if (sr.lora) {
                                html += '<span class="sweep-label sweep-label-lora">L:' + parseFloat(sr.lora_strength).toFixed(1) + '</span>';
                            } else {
                                html += '<span class="sweep-label sweep-label-nolora">No L</span>';
                            }
                            html += '</div></div>';
                        }
                        html += '</div></div>';
                        html += '</div>';
                        return html;
                    }

                    html += '<div class="prompt-history-card-main" onclick="window._genLoadHistory(' + i + ', -1)">';
                    html += '<div class="prompt-history-time">' + timeStr;
                    if (hasRevisions) {
                        html += ' <span class="prompt-history-rev-badge">' + h.revisions.length + ' revision' + (h.revisions.length > 1 ? 's' : '') + '</span>';
                    }
                    html += '</div>';
                    html += '<div class="prompt-history-label">\u25B6 Initial prompt (sent to ComfyUI)</div>';
                    html += '<div class="prompt-history-prompt">' + (h.prompt || '(empty)').substring(0, 120) + (h.prompt && h.prompt.length > 120 ? '...' : '') + '</div>';
                    html += _renderParamBadges(h);
                    if (h.negative_prompt) html += '<div class="prompt-history-neg">Neg: ' + h.negative_prompt.substring(0, 80) + '</div>';
                    html += '</div>';

                    // Revisions
                    if (hasRevisions) {
                        html += '<div class="prompt-history-revisions">';
                        html += '<div class="prompt-history-rev-header" onclick="window._genToggleRevisions(' + i + ')">';
                        html += '\u23F3 ' + h.revisions.length + ' change' + (h.revisions.length > 1 ? 's' : '') + ' during generation <span class="prompt-history-rev-arrow" id="phRevArrow' + i + '">\u25B6</span>';
                        html += '</div>';
                        html += '<div class="prompt-history-rev-list" id="phRevList' + i + '" style="display:none;">';

                        for (var r = 0; r < h.revisions.length; r++) {
                            var rev = h.revisions[r];
                            var revTime = new Date(rev.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                            html += '<div class="prompt-history-rev-item" onclick="window._genLoadHistory(' + i + ', ' + r + ')">';
                            html += '<div class="prompt-history-rev-time">\u270E ' + revTime + '</div>';
                            html += _renderChangeBadges(rev.changes);
                            html += '</div>';
                        }

                        // Final state
                        if (h.final_snapshot) {
                            html += '<div class="prompt-history-rev-item prompt-history-rev-final" onclick="window._genLoadHistory(' + i + ', -2)">';
                            html += '<div class="prompt-history-rev-time">\u2705 Final state at completion</div>';
                            html += _renderParamBadges(h.final_snapshot);
                            html += '</div>';
                        }

                        html += '</div></div>';
                    }

                    html += '</div>';
                    return html;
                }).join('');
            }

            modal.style.display = 'flex';
        }).catch(function (e) {
            console.warn('Failed to load prompt history:', e);
            list.innerHTML = '<div class="prompt-history-empty">Failed to load history</div>';
        });
    };

    window._genToggleRevisions = function (index) {
        var list = document.getElementById('phRevList' + index);
        var arrow = document.getElementById('phRevArrow' + index);
        if (!list) return;
        var visible = list.style.display !== 'none';
        list.style.display = visible ? 'none' : 'block';
        if (arrow) arrow.textContent = visible ? '\u25B6' : '\u25BC';
    };

    window._genCloseHistory = function (event) {
        if (event && event.target !== event.currentTarget) return;
        $('promptHistoryModal').style.display = 'none';
    };

    function _applySnapshot(snap) {
        if (!snap) return;
        if ($('genPrompt')) $('genPrompt').value = snap.prompt || '';
        if ($('genNegative')) $('genNegative').value = snap.negative_prompt || '';
        if ($('genSteps')) $('genSteps').value = snap.steps || 20;
        if ($('genCfg')) $('genCfg').value = snap.cfg || 7;
        if ($('genSeed')) $('genSeed').value = snap.seed || -1;
        if ($('genModel') && snap.model) {
            var sel = $('genModel');
            for (var j = 0; j < sel.options.length; j++) {
                if (sel.options[j].value === snap.model) { sel.selectedIndex = j; break; }
            }
        }
        if ($('genLora') && snap.lora) {
            var loSel = $('genLora');
            for (var k = 0; k < loSel.options.length; k++) {
                if (loSel.options[k].value === snap.lora) { loSel.selectedIndex = k; break; }
            }
        }
        if ($('genLoraStrength') && snap.lora_strength) $('genLoraStrength').value = snap.lora_strength;
    }

    window._genLoadHistory = function (index, revIndex) {
        var h = _historyCache[index];
        if (!h) return;

        if (revIndex === -2 && h.final_snapshot) {
            _applySnapshot(h.final_snapshot);
        } else if (revIndex >= 0 && h.revisions && h.revisions[revIndex]) {
            _applySnapshot(h.revisions[revIndex].snapshot);
        } else {
            _applySnapshot(h);
        }

        $('promptHistoryModal').style.display = 'none';
    };

    window._genClearHistory = function () {
        if (!confirm('Clear all prompt history?')) return;
        fetch('/api/generate/prompt-history', { method: 'DELETE' })
            .then(function () { window._genShowHistory(); })
            .catch(function (e) { console.warn('Failed to clear history:', e); });
    };

    window._genCloseGallery = function () {
        galleryOpen = false;
        const modal = $('galleryModal');
        if (modal) modal.style.display = 'none';
    };

    window._genGalleryPage = function (offset) {
        loadGallery(offset);
    };

    // --- img2img helpers ---
    function setImg2imgFile(file) {
        img2imgFile = file;
        const placeholder = $('img2imgPlaceholder');
        const preview = $('img2imgPreview');
        const denoiseGroup = $('img2imgDenoiseGroup');

        if (placeholder) placeholder.style.display = 'none';
        if (preview) {
            preview.style.display = 'flex';
            const thumb = $('img2imgThumb');
            if (thumb) thumb.src = URL.createObjectURL(file);
            const name = $('img2imgName');
            if (name) name.textContent = file.name;
        }
        if (denoiseGroup) denoiseGroup.style.display = 'block';

        // Change button text
        const btn = $('generateBtn');
        if (btn && !isGenerating) btn.innerHTML = '<span>🔄</span> Generate Variation';
    }

    window._genImg2imgSelected = function (input) {
        const file = input.files?.[0];
        if (file && file.type.startsWith('image/')) setImg2imgFile(file);
    };

    window._genImg2imgClear = function () {
        img2imgFile = null;
        const placeholder = $('img2imgPlaceholder');
        const preview = $('img2imgPreview');
        const denoiseGroup = $('img2imgDenoiseGroup');
        const fileInput = $('img2imgFile');

        if (placeholder) placeholder.style.display = 'flex';
        if (preview) preview.style.display = 'none';
        if (denoiseGroup) denoiseGroup.style.display = 'none';
        if (fileInput) fileInput.value = '';

        const btn = $('generateBtn');
        if (btn && !isGenerating) btn.innerHTML = '<span>🎨</span> Generate';
    };

    async function generateImg2Img() {
        isGenerating = true;
        _initialSnapshot = _snapshotParams();
        _startChangeWatcher();
        $('generateBtn').disabled = true;
        $('generateBtn').classList.add('generating');
        $('generateBtn').innerHTML = '<span>⏳</span> Generating variation...';
        $('genCancelBtn').style.display = 'inline-flex';

        $('genOutput').innerHTML = `
            <div class="generate-loading">
                <div class="generate-loading-spinner"></div>
                <span class="generate-loading-text">Generating image variation...</span>
                <span class="generate-loading-progress" id="genProgress">Uploading source image</span>
            </div>
        `;

        try {
            const formData = new FormData();
            formData.append('image', img2imgFile);
            formData.append('prompt', $('genPrompt').value.trim());
            formData.append('negative_prompt', $('genNegative').value.trim());
            formData.append('model', $('genModel').value);
            formData.append('steps', $('genSteps').value);
            formData.append('cfg', $('genCfg').value);
            formData.append('seed', $('genSeed').value);
            formData.append('denoise', $('genDenoise').value);
            formData.append('lora', $('genLora').value);
            formData.append('lora_strength', $('genLoraStrength').value);
            formData.append('batch_size', Math.min(parseInt($('genBatch').value) || 1, 4));

            const resp = await fetch('/api/generate/img2img', {
                method: 'POST',
                body: formData,
            });
            const data = await resp.json();
            if (data.error) { showError(data.error); resetButton(); return; }

            currentPromptId = data.prompt_id;
            const prog = $('genProgress');
            if (prog) prog.textContent = 'Processing with ComfyUI...';
            pollForResult();
        } catch (e) {
            showError('Failed to connect: ' + e.message);
            resetButton();
        }
    }


    // Expose for tab switch
    window.initGenerateTab = initGenerateTab;
    window.generateImage = generateImage;
    window.toggleVram = toggleVram;
    window.toggleGallery = toggleGallery;

    // Auto-init if visible
    if ($('tabGenerate') && $('tabGenerate').style.display !== 'none') {
        initGenerateTab();
    }
})();

