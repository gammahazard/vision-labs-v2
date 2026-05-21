"""Unit tests for cameras.py:_validate_camera dependency rules."""
import pytest
from services.dashboard import cameras


def _valid_base():
    return {
        "id": "cam1",
        "name": "Front",
        "rtsp_sub": "rtsp://192.0.2.1/sub",
    }


def test_validate_rejects_attrs_without_vehicles():
    entry = _valid_base()
    entry["detect_vehicles"] = False
    entry["detect_vehicle_attributes"] = True
    err = cameras._validate_camera(entry)
    assert err is not None
    assert "detect_vehicle_attributes" in err
    assert "detect_vehicles" in err


def test_validate_accepts_attrs_with_vehicles():
    entry = _valid_base()
    entry["detect_vehicles"] = True
    entry["detect_vehicle_attributes"] = True
    assert cameras._validate_camera(entry) is None


def test_validate_accepts_attrs_unset():
    """Existing cameras (no detect_vehicle_attributes key) still validate."""
    entry = _valid_base()
    entry["detect_vehicles"] = True
    assert cameras._validate_camera(entry) is None


def test_validate_accepts_attrs_false_with_vehicles_false():
    entry = _valid_base()
    entry["detect_vehicles"] = False
    entry["detect_vehicle_attributes"] = False
    assert cameras._validate_camera(entry) is None
