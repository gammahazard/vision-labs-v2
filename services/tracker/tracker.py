"""services/tracker/tracker.py — Docker entrypoint shim.

The real implementation lives in core/ (split out for navigability). This
file stays at the original path so the Dockerfile's CMD doesn't need to
change, and so `docker compose logs tracker-cam1` still routes to a
predictable script name.

Re-exports the public surface (compute_iou, TrackedPerson, TrackedVehicle,
PersonTracker) so existing tests and external callers using
`from tracker import X` keep working.
"""

from core.iou import compute_iou
from core.state import TrackedPerson, TrackedVehicle
from core.manager import PersonTracker
from core.main import run

__all__ = [
    "run",
    "compute_iou",
    "TrackedPerson",
    "TrackedVehicle",
    "PersonTracker",
]


if __name__ == "__main__":
    run()
