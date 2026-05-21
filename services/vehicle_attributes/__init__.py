"""Pytest-only import alias for the dashed service dir.

The Dockerfile COPYs `services/vehicle-attributes/*.py` directly; this
underscore-prefixed shim only exists so pytest can `import
services.vehicle_attributes.buffer`. Both files point at the same source.
"""
from pathlib import Path
import sys

_dashed = Path(__file__).resolve().parent.parent / "vehicle-attributes"
if _dashed.is_dir():
    sys.path.insert(0, str(_dashed))
