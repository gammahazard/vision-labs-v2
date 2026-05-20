"""tracker.core — split package (was tracker.py monolith).

Layout:
    config.py   — env vars + stream keys + logger
    iou.py      — compute_iou geometry helper
    state.py    — TrackedVehicle + TrackedPerson dataclasses
    manager.py  — PersonTracker orchestrator
    main.py     — entrypoint (signal + consumer-group + run loop)

External entry: `from core.main import run` (called by tracker.py shim).
"""

from .main import run

__all__ = ["run"]
