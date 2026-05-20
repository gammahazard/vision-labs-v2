/**
 * static/nav.js — Shared navbar enhancements across all dashboard pages.
 *
 * PURPOSE:
 *   Currently provides a click-to-open dropdown on the 📺 Detail view nav
 *   link that lists every enabled camera (basement, office, etc.) so the
 *   user can jump straight to a specific camera's detail page instead of
 *   landing on the default and switching.
 *
 * RELATIONSHIPS:
 *   - REST: /api/cameras (camera registry)
 *   - HTML: nav.navbar > .navbar-links > a[href="/single.html"]
 *   - Loaded by: index.html, single.html, cameras.html, ai.html,
 *                telegram.html, monitoring.html
 *
 * NOTES:
 *   - Cameras list is fetched once and cached for 30s.
 *   - Tap-outside / Escape closes the dropdown.
 *   - The original Detail view link's default navigation is suppressed
 *     when there are cameras to choose from (the dropdown is the action);
 *     if /api/cameras fails or returns empty, the link still works.
 */
(function () {
    "use strict";

    let _camerasCache = null;
    let _camerasCacheTs = 0;
    const CACHE_MS = 30_000;
    let _dropdownEl = null;
    let _outsideHandler = null;
    let _escHandler = null;

    async function _fetchCameras() {
        const now = Date.now();
        if (_camerasCache && now - _camerasCacheTs < CACHE_MS) return _camerasCache;
        try {
            const res = await fetch("/api/cameras");
            if (!res.ok) return [];
            const data = await res.json();
            const arr = Array.isArray(data) ? data : (data.cameras || []);
            _camerasCache = arr.filter(c => c && c.id && c.enabled !== false);
            _camerasCacheTs = now;
            return _camerasCache;
        } catch (e) {
            return [];
        }
    }

    function _closeDropdown() {
        if (_dropdownEl && _dropdownEl.parentNode) _dropdownEl.parentNode.removeChild(_dropdownEl);
        _dropdownEl = null;
        if (_outsideHandler) {
            document.removeEventListener("click", _outsideHandler, true);
            _outsideHandler = null;
        }
        if (_escHandler) {
            document.removeEventListener("keydown", _escHandler);
            _escHandler = null;
        }
    }

    function _renderDropdown(anchor, cameras) {
        _closeDropdown();
        const rect = anchor.getBoundingClientRect();

        _dropdownEl = document.createElement("div");
        _dropdownEl.className = "nav-dropdown-menu";
        _dropdownEl.setAttribute("role", "menu");
        // Absolutely-positioned. Anchor below the link, left-aligned with it.
        // On narrow viewports clamp the right edge so it doesn't overflow.
        const left = Math.max(8, Math.min(rect.left, window.innerWidth - 232));
        _dropdownEl.style.cssText = `
            position: fixed;
            top: ${rect.bottom + 4}px;
            left: ${left}px;
            min-width: 200px;
            max-width: 280px;
            background: var(--bg-secondary, #1a2235);
            border: 1px solid var(--border, #2d3748);
            border-radius: 10px;
            box-shadow: 0 8px 28px rgba(0,0,0,0.45);
            padding: 6px;
            z-index: 1000;
            display: flex;
            flex-direction: column;
            gap: 2px;
            font-size: 0.88rem;
        `;

        _dropdownEl.innerHTML = cameras.map(c => {
            const label = c.name || c.id;
            return `
                <a href="/single.html?camera=${encodeURIComponent(c.id)}"
                   role="menuitem" class="nav-dropdown-item"
                   style="display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:6px;
                          text-decoration:none;color:var(--text-secondary,#94a3b8);font-weight:500;
                          -webkit-tap-highlight-color:transparent;">
                    <span style="font-size:1rem;">📺</span>
                    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${label}</span>
                    <span style="font-size:0.7rem;color:var(--text-tertiary,#64748b);">${c.id}</span>
                </a>`;
        }).join("");

        document.body.appendChild(_dropdownEl);

        // Close when tapping outside the dropdown OR pressing Escape.
        _outsideHandler = (e) => {
            if (!_dropdownEl) return;
            if (_dropdownEl.contains(e.target)) return;
            if (anchor.contains(e.target)) return;
            _closeDropdown();
        };
        _escHandler = (e) => { if (e.key === "Escape") _closeDropdown(); };
        // Defer to next tick so the click that opened us doesn't immediately
        // close us via the outside-click handler.
        setTimeout(() => {
            document.addEventListener("click", _outsideHandler, true);
            document.addEventListener("keydown", _escHandler);
        }, 0);

        // Hover affordance for desktop (mobile gets touch defaults).
        for (const a of _dropdownEl.querySelectorAll(".nav-dropdown-item")) {
            a.addEventListener("mouseenter", () => {
                a.style.background = "rgba(96,165,250,0.12)";
                a.style.color = "var(--text-primary, #e2e8f0)";
            });
            a.addEventListener("mouseleave", () => {
                a.style.background = "transparent";
                a.style.color = "var(--text-secondary, #94a3b8)";
            });
        }
    }

    async function _onDetailLinkClick(e, anchor) {
        // Always show the dropdown — never let the browser follow the link
        // before the user has chosen which camera they want.
        e.preventDefault();
        if (_dropdownEl) { _closeDropdown(); return; }
        const cameras = await _fetchCameras();
        if (!cameras.length) {
            // No cameras registered — fall back to following the link.
            window.location.href = anchor.href;
            return;
        }
        _renderDropdown(anchor, cameras);
    }

    function init() {
        // Find the Detail view link in the navbar — there's only one per page.
        const links = document.querySelectorAll('nav.navbar a.nav-link[href="/single.html"], nav.navbar a.nav-link[href^="/single.html"]');
        for (const a of links) {
            a.addEventListener("click", (e) => _onDetailLinkClick(e, a));
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    // -----------------------------------------------------------------
    // Universal modal-backdrop-click-to-close (Phase H.5)
    //
    // Across the dashboard, modals come from different authors and use
    // different class names (.modal-overlay, .gallery-modal-overlay,
    // .lightbox-overlay, .label-modal-overlay). Their CLOSE behavior is
    // inconsistent — some had backdrop close, some only had an explicit
    // ✕ button. Unifying with a single global handler: when the user
    // clicks the OVERLAY (background, not content), close the modal.
    //
    // What counts as "the backdrop": the element with one of the overlay
    // class names whose direct click target IS that element (not a child).
    //
    // Closing strategy: hide via inline style `display: none` (matches
    // existing code's hide approach in galleryModal, lightboxModal, etc.).
    // Modals using the `hidden` attribute (setup.html's onvifCredsModal)
    // get that toggled instead.
    // -----------------------------------------------------------------
    const OVERLAY_CLASSES = [
        "modal-overlay",
        "gallery-modal-overlay",
        "lightbox-overlay",
        "label-modal-overlay",
    ];

    // Map specific overlay IDs to their owning page's close-cleanup function.
    // If the user clicks the backdrop of an overlay listed here, we also
    // invoke the cleanup fn so ongoing state (timers, playback) gets reset.
    const _CLEANUP_BY_ID = {
        "lightboxModal":    "_closeLightbox",
        "galleryModal":     "_genCloseGallery",
        "clipGalleryModal": "_closeClipGallery",
        "labelModal":       "closeLabelModal",
        "onvifCredsModal":  "closeOnvifModal",
    };

    function _closeModal(overlay) {
        if (overlay.hasAttribute("hidden")) {
            overlay.setAttribute("hidden", "");
        } else {
            overlay.style.display = "none";
        }
        // Call the SPECIFIC cleanup fn for this overlay's id, not every one.
        const fnName = _CLEANUP_BY_ID[overlay.id];
        if (fnName && typeof window[fnName] === "function") {
            try { window[fnName](); } catch (_) { /* ignore */ }
        }
    }

    document.addEventListener("click", (e) => {
        const target = e.target;
        if (!target || target.nodeType !== 1) return;
        // Only fire when the user clicks the OVERLAY itself, not a child
        // (i.e. clicking inside the modal content should NOT close it).
        if (target !== e.currentTarget && target.classList) {
            for (const cls of OVERLAY_CLASSES) {
                if (target.classList.contains(cls)) {
                    _closeModal(target);
                    return;
                }
            }
        }
    });

    // Escape key — same closing logic for any visible modal overlay
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        // Find any currently-open overlay
        for (const cls of OVERLAY_CLASSES) {
            const els = document.getElementsByClassName(cls);
            for (const el of els) {
                const visible = !el.hasAttribute("hidden")
                    && el.style.display !== "none"
                    && getComputedStyle(el).display !== "none";
                if (visible) {
                    _closeModal(el);
                    return; // close just the topmost
                }
            }
        }
    });
})();
