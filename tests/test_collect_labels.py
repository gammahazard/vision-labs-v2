"""Tests for scripts/vehicle_attributes/collect_labels.py.

The finetune script itself is hard to unit-test without GPU + real
images, but the data-collection logic (walking per-track dirs +
filtering by user_labels) is straightforward file-system code and is
the load-bearing input to retraining.
"""

import json
import sys
from pathlib import Path

import pytest


# Add scripts/vehicle_attributes to PYTHONPATH so we can import collect_labels.
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts" / "vehicle_attributes"
sys.path.insert(0, str(SCRIPTS_DIR))

from collect_labels import collect, summarize  # noqa: E402


def _make_track(parent: Path, camera: str, date: str, track_id: str,
                user_labels: dict | None,
                attributes: dict | None = None,
                with_hero: bool = True,
                with_angles: int = 0) -> Path:
    """Create a fake per-track dir with metadata.json + optional crops."""
    track_dir = parent / camera / date / track_id
    track_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "track_id": track_id.split("_")[1] if "_" in track_id else track_id,
        "camera_id": camera,
        "first_seen": 1779500000.0,
        "vehicle_class": "car",
        "attributes": attributes or {
            "color": None, "body_type": None,
            "make": None, "model": None, "ir_track": False,
        },
    }
    if user_labels is not None:
        meta["user_labels"] = user_labels
    (track_dir / "metadata.json").write_text(json.dumps(meta))
    if with_hero:
        (track_dir / "hero.jpg").write_bytes(b"fake-jpeg")
    for i in range(with_angles):
        (track_dir / f"angle_{i}.jpg").write_bytes(b"fake-jpeg")
    return track_dir


class TestCollect:
    def test_empty_root_returns_empty(self, tmp_path):
        assert collect(tmp_path) == []

    def test_nonexistent_root_returns_empty(self, tmp_path):
        assert collect(tmp_path / "missing") == []

    def test_track_without_user_labels_skipped(self, tmp_path):
        _make_track(tmp_path, "cam1", "2026-05-23", "vehicle_0001_1779500000",
                    user_labels=None)
        assert collect(tmp_path) == []

    def test_basic_labeled_track_collected(self, tmp_path):
        _make_track(tmp_path, "cam1", "2026-05-23", "vehicle_0001_1779500000",
                    user_labels={
                        "color": "white", "body_type": "sedan",
                        "make": "Toyota", "model": "Sienna 2018",
                        "skipped": False,
                    })
        out = collect(tmp_path)
        assert len(out) == 1
        t = out[0]
        assert t.color == "white"
        assert t.body_type == "sedan"
        assert t.make == "Toyota"
        assert t.model == "Sienna 2018"
        assert t.skipped is False
        assert t.image_path.name == "hero.jpg"
        assert t.camera == "cam1"
        assert t.date == "2026-05-23"

    def test_skipped_track_excluded(self, tmp_path):
        _make_track(tmp_path, "cam1", "2026-05-23", "vehicle_0001_1779500000",
                    user_labels={"skipped": True, "skip_reason": "blurry"})
        assert collect(tmp_path) == []

    def test_partial_labels_collected(self, tmp_path):
        """Only color labeled — body/make/model stay None but track collected."""
        _make_track(tmp_path, "cam1", "2026-05-23", "vehicle_0001_1779500000",
                    user_labels={"color": "white", "skipped": False})
        out = collect(tmp_path)
        assert len(out) == 1
        assert out[0].color == "white"
        assert out[0].body_type is None
        assert out[0].make is None

    def test_fallback_to_angle_when_no_hero(self, tmp_path):
        """If hero.jpg is missing, the first angle_*.jpg should be picked."""
        td = _make_track(tmp_path, "cam1", "2026-05-23",
                         "vehicle_0001_1779500000",
                         user_labels={"color": "white", "skipped": False},
                         with_hero=False, with_angles=2)
        out = collect(tmp_path)
        assert len(out) == 1
        assert out[0].image_path == td / "angle_0.jpg"

    def test_no_crops_skipped(self, tmp_path):
        """A track dir with metadata.json but NO crops gets dropped."""
        _make_track(tmp_path, "cam1", "2026-05-23", "vehicle_0001_1779500000",
                    user_labels={"color": "white", "skipped": False},
                    with_hero=False, with_angles=0)
        assert collect(tmp_path) == []

    def test_multiple_cameras_dates(self, tmp_path):
        _make_track(tmp_path, "cam1", "2026-05-23", "vehicle_0001_1779500000",
                    user_labels={"color": "white", "skipped": False})
        _make_track(tmp_path, "cam2", "2026-05-22", "vehicle_0002_1779400000",
                    user_labels={"color": "red", "skipped": False})
        _make_track(tmp_path, "cam1", "2026-05-22", "vehicle_0003_1779400500",
                    user_labels={"body_type": "suv", "skipped": False})
        out = collect(tmp_path)
        assert len(out) == 3
        cams = {t.camera for t in out}
        assert cams == {"cam1", "cam2"}
        dates = {t.date for t in out}
        assert dates == {"2026-05-23", "2026-05-22"}

    def test_unreadable_metadata_skipped(self, tmp_path):
        """Corrupt JSON in one track doesn't break the collection of others."""
        td = tmp_path / "cam1" / "2026-05-23" / "vehicle_0001_1779500000"
        td.mkdir(parents=True)
        (td / "metadata.json").write_text("not valid json {{{")
        (td / "hero.jpg").write_bytes(b"fake")
        # Plus a valid one
        _make_track(tmp_path, "cam1", "2026-05-23", "vehicle_0002_1779500001",
                    user_labels={"color": "white", "skipped": False})
        out = collect(tmp_path)
        assert len(out) == 1
        assert out[0].color == "white"

    def test_non_date_dir_skipped(self, tmp_path):
        """Random subdirs under a camera that don't look like YYYY-MM-DD
        are not walked."""
        weird = tmp_path / "cam1" / "scratch"
        weird.mkdir(parents=True)
        (weird / "metadata.json").write_text(json.dumps({
            "track_id": "1", "user_labels": {"color": "white"},
        }))
        assert collect(tmp_path) == []


class TestSummarize:
    def test_empty(self):
        s = summarize([])
        assert s["total_labeled_tracks"] == 0
        assert s["color_label_count"] == 0
        assert s["color_distribution"] == {}

    def test_distribution_counts(self, tmp_path):
        for i, color in enumerate(["white", "white", "white", "red", "black"]):
            _make_track(
                tmp_path, "cam1", "2026-05-23",
                # Track names must be globally unique — use the loop index
                # (NOT len(tmp_path.iterdir()) which I had before; that
                # returned 1 after the first iteration since `cam1` is the
                # only top-level entry, causing name collisions).
                f"vehicle_{i:04d}_{1779500000 + i}",
                user_labels={"color": color, "skipped": False},
            )
        tracks = collect(tmp_path)
        s = summarize(tracks)
        assert s["total_labeled_tracks"] == 5
        assert s["color_label_count"] == 5
        assert s["color_distribution"]["white"] == 3
        assert s["color_distribution"]["red"] == 1
        assert s["color_distribution"]["black"] == 1
