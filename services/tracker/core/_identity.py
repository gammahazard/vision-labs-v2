"""tracker/core/_identity.py — IdentityMixin.

Extracted from manager.py during the 2026-05-22 mixin split.

Mixed into PersonTracker. Owns the per-poll read of `face_identity:*`
from Redis and the IoU-match that assigns identity names to currently-
tracked person bboxes, including the N-cycle confirmation gate that
prevents single-frame flips from corrupting an established identity.
"""

import json
import time

from .config import (
    logger,
    IDENTITY_KEY,
    IDENTITY_POLL_INTERVAL,
)
from .iou import compute_iou
from .state import TrackedPerson


class IdentityMixin:
    """Face-recognizer → person-track identity sync."""

    def _update_identities(self):
        """Read face identity state from Redis and map names to tracked persons."""
        now = time.time()
        if now - self._identity_load_time < IDENTITY_POLL_INTERVAL:
            return
        self._identity_load_time = now

        try:
            id_state = self.r.hgetall(IDENTITY_KEY)
            if not id_state:
                return
            id_json = id_state.get(b"identities", id_state.get("identities", b"[]"))
            if isinstance(id_json, bytes):
                id_json = id_json.decode()
            identities = json.loads(id_json)
        except Exception:
            return

        for ident in identities:
            id_name = ident.get("name", "Unknown")
            if id_name == "Unknown":
                continue
            id_bbox = ident.get("bbox", [])
            if len(id_bbox) != 4:
                continue
            # Skip identities whose face bbox sits inside a dead zone —
            # don't let an identity match in a "don't care" area assign
            # a name to a legitimate person whose bbox happens to overlap.
            if self._check_in_dead_zone(id_bbox):
                continue
            # Match identity bbox to a tracked person via IoU
            best_iou = 0.0
            best_person = None
            for person in self.tracked.values():
                iou = compute_iou(id_bbox, person.bbox)
                if iou > best_iou:
                    best_iou = iou
                    best_person = person
            if best_iou > 0.2 and best_person:
                # Identity-expiry guard reads this. Refreshed on every
                # face-recognizer match — first identification, same-
                # name re-confirmation, and identity flip — so a track
                # that's continuously confirmed is never demoted.
                best_person.last_identity_confirmation_ts = now
                if not best_person.identity_name:
                    # First identification — emit event
                    best_person.identity_name = id_name
                    best_person._pending_identity = id_name
                    best_person._pending_identity_count = 1
                    self._emit_event(
                        "person_identified", best_person, now,
                        extra={"identity_name": id_name}
                    )
                elif id_name == best_person.identity_name:
                    # Same name — clear any pending flip candidate.
                    best_person._pending_identity = id_name
                    best_person._pending_identity_count = 0
                else:
                    # Different name proposed for an already-identified
                    # person. Require N consecutive cycles agreeing on
                    # the new name before overwriting; one bad face
                    # frame shouldn't corrupt the track. Always log so
                    # unexpected flips show up in operator review.
                    if best_person._pending_identity == id_name:
                        best_person._pending_identity_count += 1
                    else:
                        best_person._pending_identity = id_name
                        best_person._pending_identity_count = 1
                    logger.info(
                        f"Identity flip candidate: {best_person.person_id} "
                        f"'{best_person.identity_name}' → '{id_name}' "
                        f"({best_person._pending_identity_count}"
                        f"/{TrackedPerson._IDENTITY_FLIP_CONFIRM_CYCLES})"
                    )
                    if (best_person._pending_identity_count
                            >= TrackedPerson._IDENTITY_FLIP_CONFIRM_CYCLES):
                        previous = best_person.identity_name
                        logger.warning(
                            f"Identity flip CONFIRMED: {best_person.person_id} "
                            f"'{previous}' → '{id_name}'"
                        )
                        best_person.identity_name = id_name
                        best_person._pending_identity_count = 0
                        self._emit_event(
                            "person_identified", best_person, now,
                            extra={
                                "identity_name": id_name,
                                "previous_identity": previous,
                            },
                        )
