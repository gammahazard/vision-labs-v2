/**
 * js/lib/checkbox-dependencies.js
 *
 * Generic parent-child checkbox dependency wiring. Any <input type="checkbox">
 * with a `data-requires="parentId"` attribute is automatically disabled +
 * unchecked when the named parent checkbox is unchecked. Re-enabled (but not
 * auto-checked) when the parent gets checked again.
 *
 * USAGE:
 *   <input type="checkbox" id="detectPersons">
 *   <input type="checkbox" id="detectFaces" data-requires="detectPersons">
 *
 * SCOPE:
 *   Currently wires `detect_faces → detect_persons` and (when shipped)
 *   `detect_vehicle_attributes → detect_vehicles`. Before this file, those
 *   dependencies were soft — a "(requires …)" hint in the label text only,
 *   no UI enforcement. Server-side validation in cameras.py catches the
 *   invalid combinations regardless, but doing it client-side is friendlier
 *   (no round-trip + cryptic error).
 *
 * RUNS ON: setup.html (Step 4 first-camera form) + cameras.html (add-camera).
 */

(function () {
    "use strict";

    function syncChild(child, parent) {
        if (!parent.checked) {
            child.checked = false;
            child.disabled = true;
        } else {
            child.disabled = false;
            // Note: we do NOT auto-check the child when the parent is re-checked.
            // The user opted out of the child explicitly (or never opted in).
        }
    }

    function wireDependencies() {
        const children = document.querySelectorAll('input[type="checkbox"][data-requires]');
        children.forEach(function (child) {
            const parentId = child.getAttribute('data-requires');
            const parent = document.getElementById(parentId);
            if (!parent) {
                // Parent not on this page — leave the child alone.
                return;
            }
            parent.addEventListener('change', function () { syncChild(child, parent); });
            // Initial sync in case the page loaded with parent already unchecked.
            syncChild(child, parent);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wireDependencies);
    } else {
        wireDependencies();
    }
})();
