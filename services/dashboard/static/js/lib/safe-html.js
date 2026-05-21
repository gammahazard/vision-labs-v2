/**
 * js/lib/safe-html.js — Shared DOMPurify config + helper for the dashboard.
 *
 * PURPOSE:
 *   A single top-level `const _PURIFY_CFG` for the whole page. Before this
 *   file existed, each dashboard JS module (events.js, browse.js, ai.js,
 *   monitoring.js) had its own `const _PURIFY_CFG = {...}` declaration —
 *   which works fine when only one of them loads, but the moment two
 *   are loaded on the same page (e.g. events.js + browse.js on the main
 *   dashboard) the second declaration throws:
 *
 *     Uncaught SyntaxError: Identifier '_PURIFY_CFG' has already been declared
 *
 *   That kills the entire second script. browse.js never defined
 *   `initBrowse()` so the Browse panel sat on its initial "Loading
 *   snapshots…" placeholder forever.
 *
 * WIRING:
 *   Include this script BEFORE any per-page JS that calls _safeHtml().
 *   Order must be:
 *     1. js/lib/dompurify.min.js
 *     2. js/lib/safe-html.js      ← this file
 *     3. js/dashboard/*.js, js/pages/*.js
 *
 * KEEP IN SYNC WITH CLAUDE.md §13:
 *   "Every dashboard JS file that writes to innerHTML uses a _safeHtml(html)
 *    helper that wraps DOMPurify.sanitize(html, {ADD_TAGS: [...], ADD_ATTR: [...]})."
 *   The ADD_TAGS / ADD_ATTR list here is the canonical one; updating it here
 *   updates every page that uses _safeHtml().
 */

const _PURIFY_CFG = {
    ADD_TAGS: ['video', 'figure', 'source'],
    ADD_ATTR: ['controls', 'autoplay', 'loop', 'muted', 'playsinline', 'preload']
};

function _safeHtml(html) {
    return DOMPurify.sanitize(html, _PURIFY_CFG);
}
