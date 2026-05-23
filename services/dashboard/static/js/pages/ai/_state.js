/**
 * ai/_state.js — Module-level state shared across the AI page's sub-files.
 *
 * Extracted from ai.js during the 2026-05-22 modularity split.
 *
 * Classic-script semantics: `var` at top-level of a non-module script
 * creates a global property, so these names are visible to every sibling
 * file loaded after this one. Order matters — this file MUST load first
 * (see <script> tags in ai.html).
 *
 * If you add new shared state, put it here. State that's read/written
 * within a single tab module (like the chat input element reference)
 * stays local to that file.
 */

// Chat / wizard / ollama-status state
var aiConfig = { enabled: false, user_name: '', ai_name: 'Atlas' };
var chatHistory = [];          // { role: 'user'|'assistant', content: string }
var isWaiting = false;
var ollamaReady = false;

// Vision tab state
var visionImageBase64 = null;
var visionVideoFrames = null;  // array of base64 frames for video
var visionAnalyzing = false;

// DVR recordings tab state
var _recLastDate = '';         // track last loaded date to avoid redundant fetches
var _recCamera = '';           // currently selected camera id
