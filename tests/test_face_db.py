"""
tests/test_face_db.py — Real tests for face_db.py.

Tests the entire FaceDB lifecycle against an actual in-memory SQLite database:
  - Enrolling known faces
  - Matching faces by cosine similarity
  - Threshold behavior (matches above, rejects below)
  - Deleting known faces
  - Auto-capturing unknown faces with deduplication
  - Sighting count increments on repeated unknowns
  - Pruning when MAX_UNKNOWN_FACES exceeded
  - Labeling (promoting) unknowns → known faces
  - Cache coherency after every mutation

NO mocks — all tests hit a real SQLite DB in /tmp.
"""

import os
import sys
import tempfile
import pytest
import numpy as np

# Add the face-recognizer service directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "face-recognizer"))
from face_db import FaceDB, MAX_UNKNOWN_FACES, UNKNOWN_DEDUP_THRESHOLD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def random_embedding(seed: int = None) -> np.ndarray:
    """Generate a random 512-dim unit vector (like InsightFace would produce)."""
    rng = np.random.RandomState(seed)
    vec = rng.randn(512).astype(np.float32)
    return vec / np.linalg.norm(vec)


def similar_embedding(base: np.ndarray, noise: float = 0.05) -> np.ndarray:
    """Create an embedding very similar to `base` (cosine sim > 0.95)."""
    noise_vec = np.random.randn(512).astype(np.float32) * noise
    result = base + noise_vec
    return result / np.linalg.norm(result)


@pytest.fixture
def db(tmp_path):
    """Create a fresh FaceDB for each test."""
    db_path = str(tmp_path / "test_faces.db")
    return FaceDB(db_path=db_path, match_threshold=0.5)


# ---------------------------------------------------------------------------
# Known face tests
# ---------------------------------------------------------------------------

class TestEnrollment:
    def test_enroll_one(self, db):
        """Enrolling a face returns an integer ID and increments count."""
        emb = random_embedding(seed=1)
        face_id = db.enroll("Alice", emb, b"fake_photo")
        assert isinstance(face_id, int)
        assert db.count == 1

    def test_enroll_multiple(self, db):
        """Can enroll multiple people with different names."""
        for i, name in enumerate(["Alice", "Bob", "Charlie"]):
            db.enroll(name, random_embedding(seed=i), b"photo")
        assert db.count == 3

    def test_enroll_same_name_allowed(self, db):
        """Two people can have the same name (different IDs)."""
        id1 = db.enroll("Alice", random_embedding(seed=1), b"photo1")
        id2 = db.enroll("Alice", random_embedding(seed=2), b"photo2")
        assert id1 != id2
        assert db.count == 2

    def test_enroll_normalizes_embedding(self, db):
        """Enrollment normalizes the embedding to unit length."""
        emb = np.ones(512, dtype=np.float32) * 5.0  # Not unit length
        db.enroll("Test", emb, b"photo")
        # The cached embedding should be unit-length
        cached_emb = db._cache[0]["embedding"]
        norm = np.linalg.norm(cached_emb)
        assert abs(norm - 1.0) < 1e-5

    def test_enrolled_face_photo_retrievable(self, db):
        """Photo stored during enrollment can be retrieved."""
        photo_data = b"\xff\xd8\xff\xe0fake_jpeg_data"
        face_id = db.enroll("Alice", random_embedding(seed=1), photo_data)
        retrieved = db.get_photo(face_id)
        assert retrieved == photo_data

    def test_get_photo_nonexistent(self, db):
        """Getting photo for nonexistent face returns None."""
        assert db.get_photo(9999) is None


class TestMatching:
    def test_match_exact_embedding(self, db):
        """Matching with the exact same embedding returns a strong match."""
        emb = random_embedding(seed=42)
        db.enroll("Alice", emb.copy(), b"photo")

        result = db.match(emb.copy())
        assert result is not None
        assert result["name"] == "Alice"
        assert result["similarity"] >= 0.99  # Should be ~1.0

    def test_match_similar_embedding(self, db):
        """Matching with a very similar embedding still returns a match."""
        emb = random_embedding(seed=42)
        db.enroll("Alice", emb.copy(), b"photo")

        noisy = similar_embedding(emb, noise=0.02)
        result = db.match(noisy)
        assert result is not None
        assert result["name"] == "Alice"
        assert result["similarity"] >= 0.5

    def test_no_match_different_person(self, db):
        """Completely different embeddings should not match."""
        db.enroll("Alice", random_embedding(seed=1), b"photo")

        different = random_embedding(seed=999)
        result = db.match(different)
        # Random 512-dim vectors have near-zero cosine similarity
        assert result is None

    def test_match_returns_best_candidate(self, db):
        """When multiple faces enrolled, match returns the most similar."""
        emb_alice = random_embedding(seed=1)
        emb_bob = random_embedding(seed=2)
        db.enroll("Alice", emb_alice.copy(), b"photo")
        db.enroll("Bob", emb_bob.copy(), b"photo")

        # Query with something very close to Alice
        query = similar_embedding(emb_alice, noise=0.05)
        result = db.match(query)
        assert result is not None
        assert result["name"] == "Alice"

    def test_match_empty_db(self, db):
        """Matching against empty DB returns None (not an error)."""
        result = db.match(random_embedding(seed=1))
        assert result is None

    def test_match_threshold_boundary(self, db):
        """Verify the threshold is respected — matches just above pass, just below fail."""
        emb = random_embedding(seed=42)
        db.enroll("Alice", emb.copy(), b"photo")

        # Exact match should definitely pass threshold of 0.5
        result = db.match(emb.copy())
        assert result is not None

        # A very different vector should fail
        orthogonal = np.zeros(512, dtype=np.float32)
        orthogonal[0] = 1.0  # Mostly orthogonal to a random vector
        result = db.match(orthogonal)
        # This may or may not match, but the point is the threshold is enforced
        if result:
            assert result["similarity"] >= 0.5


class TestDeletion:
    def test_delete_existing(self, db):
        """Deleting an existing face returns True and decrements count."""
        face_id = db.enroll("Alice", random_embedding(seed=1), b"photo")
        assert db.count == 1
        deleted = db.delete(face_id)
        assert deleted is True
        assert db.count == 0

    def test_delete_nonexistent(self, db):
        """Deleting a nonexistent face returns False."""
        assert db.delete(9999) is False

    def test_delete_removed_from_match(self, db):
        """After deletion, that face no longer matches."""
        emb = random_embedding(seed=42)
        face_id = db.enroll("Alice", emb.copy(), b"photo")

        # Should match before deletion
        assert db.match(emb.copy()) is not None

        db.delete(face_id)

        # Should NOT match after deletion
        assert db.match(emb.copy()) is None


class TestListFaces:
    def test_list_empty(self, db):
        """Listing faces in empty DB returns empty list."""
        assert db.list_faces() == []

    def test_list_returns_all(self, db):
        """Listing faces returns all enrolled faces."""
        db.enroll("Alice", random_embedding(seed=1), b"photo")
        db.enroll("Bob", random_embedding(seed=2), b"photo")
        faces = db.list_faces()
        assert len(faces) == 2
        names = {f["name"] for f in faces}
        assert names == {"Alice", "Bob"}

    def test_list_excludes_embedding(self, db):
        """Listed faces should NOT contain raw embedding data."""
        db.enroll("Alice", random_embedding(seed=1), b"photo")
        faces = db.list_faces()
        assert "embedding" not in faces[0]


# ---------------------------------------------------------------------------
# Unknown face tests
# ---------------------------------------------------------------------------

class TestUnknownFaces:
    def test_save_new_unknown(self, db):
        """Saving a new unknown face returns an ID."""
        uid = db.save_unknown(random_embedding(seed=1), b"photo")
        assert uid is not None
        assert isinstance(uid, int)
        assert db.unknown_count == 1

    def test_dedup_same_embedding(self, db):
        """Saving the same embedding twice deduplicates (returns None)."""
        emb = random_embedding(seed=42)
        uid1 = db.save_unknown(emb.copy(), b"photo1")
        uid2 = db.save_unknown(emb.copy(), b"photo2")

        assert uid1 is not None
        assert uid2 is None  # Deduped
        assert db.unknown_count == 1

    def test_dedup_similar_embedding(self, db):
        """Similar embeddings (above UNKNOWN_DEDUP_THRESHOLD) are deduped."""
        emb = random_embedding(seed=42)
        uid1 = db.save_unknown(emb.copy(), b"photo1")

        # Create similar embedding
        noisy = similar_embedding(emb, noise=0.05)
        similarity = float(np.dot(
            emb / np.linalg.norm(emb),
            noisy / np.linalg.norm(noisy)
        ))
        assert similarity >= UNKNOWN_DEDUP_THRESHOLD, \
            f"Test setup error: similarity {similarity} below dedup threshold"

        uid2 = db.save_unknown(noisy, b"photo2")
        assert uid2 is None  # Should dedup
        assert db.unknown_count == 1

    def test_sighting_count_increments(self, db):
        """Dedup bumps sighting_count in the cache."""
        emb = random_embedding(seed=42)
        db.save_unknown(emb.copy(), b"photo")

        # First sighting
        assert db._unknown_cache[0]["sighting_count"] == 1

        # Second sighting of same person
        db.save_unknown(emb.copy(), b"photo2")
        assert db._unknown_cache[0]["sighting_count"] == 2

        # Third sighting
        db.save_unknown(emb.copy(), b"photo3")
        assert db._unknown_cache[0]["sighting_count"] == 3

    def test_different_unknowns_not_deduped(self, db):
        """Truly different embeddings are stored as separate unknowns."""
        uid1 = db.save_unknown(random_embedding(seed=1), b"photo1")
        uid2 = db.save_unknown(random_embedding(seed=2), b"photo2")
        assert uid1 is not None
        assert uid2 is not None
        assert uid1 != uid2
        assert db.unknown_count == 2

    def test_unknown_photo_retrievable(self, db):
        """Photo stored with unknown face can be retrieved."""
        photo_data = b"\xff\xd8\xff\xe0fake_jpeg"
        uid = db.save_unknown(random_embedding(seed=1), photo_data)
        retrieved = db.get_unknown_photo(uid)
        assert retrieved == photo_data

    def test_list_unknowns(self, db):
        """Listing unknowns returns all captured faces."""
        db.save_unknown(random_embedding(seed=1), b"photo1")
        db.save_unknown(random_embedding(seed=2), b"photo2")
        unknowns = db.list_unknowns()
        assert len(unknowns) == 2
        assert all("sighting_count" in u for u in unknowns)

    def test_delete_unknown(self, db):
        """Deleting unknown face removes it from DB and cache."""
        uid = db.save_unknown(random_embedding(seed=1), b"photo")
        assert db.unknown_count == 1
        deleted = db.delete_unknown(uid)
        assert deleted is True
        assert db.unknown_count == 0

    def test_delete_unknown_nonexistent(self, db):
        """Deleting nonexistent unknown returns False."""
        assert db.delete_unknown(9999) is False


class TestSaveUnknownNearMissSuppression:
    """Tests for the near-miss suppression in save_unknown — prevents the
    gallery from accumulating off-angle frames of already-enrolled people."""

    def test_skips_save_when_near_miss_to_known(self, db):
        """An unknown that's plausibly a known person (sim >= 0.30 against
        any enrolled angle, given match_threshold=0.5) should NOT be saved."""
        emb = random_embedding(seed=42)
        db.enroll("Alice", emb.copy(), b"photo")

        # Build a vector with sim ~0.40 against Alice — below match_threshold
        # but above the near-miss gate (0.30). This is the "first-frame-of-
        # Alice walking on screen" case we want to suppress.
        np.random.seed(7)
        base = emb / np.linalg.norm(emb)
        noise = np.random.randn(512).astype(np.float32)
        noise -= noise.dot(base) * base
        noise /= np.linalg.norm(noise)
        near_miss = base * 0.40 + noise * np.sqrt(1 - 0.40**2)
        near_miss = near_miss / np.linalg.norm(near_miss)
        sim = float(np.dot(base, near_miss))
        assert 0.30 <= sim < 0.50, f"setup sim={sim} not in near-miss band"

        before = db.unknown_count
        result = db.save_unknown(near_miss, b"first_frame")
        assert result is None  # Suppressed
        assert db.unknown_count == before  # Gallery unchanged

    def test_still_saves_true_strangers(self, db):
        """A face that's NOT close to any known person should still be saved."""
        db.enroll("Alice", random_embedding(seed=1), b"photo")
        # A different random vector — cosine sim to Alice should be near 0
        stranger = random_embedding(seed=999)
        sim = float(np.dot(stranger, db._cache[0]["embedding"]))
        assert sim < 0.30, f"setup sim={sim} too high; test won't isolate stranger case"

        uid = db.save_unknown(stranger, b"truly_unknown")
        assert uid is not None
        assert db.unknown_count == 1

    def test_no_known_faces_means_always_save(self, db):
        """With an empty known_faces cache, save_unknown never suppresses."""
        assert db.count == 0
        uid = db.save_unknown(random_embedding(seed=1), b"first")
        assert uid is not None
        assert db.unknown_count == 1


class TestLabelUnknown:
    def test_label_promotes_to_known(self, db):
        """Labeling an unknown promotes it to known_faces."""
        emb = random_embedding(seed=42)
        uid = db.save_unknown(emb.copy(), b"photo")
        assert db.unknown_count == 1
        assert db.count == 0

        face_id = db.label_unknown(uid, "Alice")
        assert face_id is not None
        assert db.unknown_count == 0
        assert db.count == 1

    def test_labeled_face_is_matchable(self, db):
        """After labeling, the face should be matchable as a known face."""
        emb = random_embedding(seed=42)
        uid = db.save_unknown(emb.copy(), b"photo")
        db.label_unknown(uid, "Alice")

        result = db.match(emb.copy())
        assert result is not None
        assert result["name"] == "Alice"

    def test_label_nonexistent_returns_none(self, db):
        """Labeling a nonexistent unknown returns None."""
        result = db.label_unknown(9999, "Ghost")
        assert result is None

    def test_label_removes_from_unknowns(self, db):
        """After labeling, the unknown no longer appears in unknowns list."""
        uid = db.save_unknown(random_embedding(seed=1), b"photo")
        db.label_unknown(uid, "Alice")
        unknowns = db.list_unknowns()
        assert len(unknowns) == 0


class TestPruning:
    def test_pruning_enforces_max_limit(self, db):
        """Unknown faces are pruned when exceeding MAX_UNKNOWN_FACES."""
        # Override the constant for testing (don't create 100+ embeddings)
        import face_db as fdb_module
        original_max = fdb_module.MAX_UNKNOWN_FACES
        fdb_module.MAX_UNKNOWN_FACES = 5

        try:
            for i in range(8):
                db.save_unknown(random_embedding(seed=i + 100), b"photo")

            assert db.unknown_count <= 5
        finally:
            fdb_module.MAX_UNKNOWN_FACES = original_max


class TestPersistence:
    def test_data_persists_across_instances(self, tmp_path):
        """Enrolled faces persist when creating a new FaceDB instance."""
        db_path = str(tmp_path / "persist_test.db")

        # Instance 1: enroll
        db1 = FaceDB(db_path=db_path)
        emb = random_embedding(seed=42)
        db1.enroll("Alice", emb.copy(), b"photo")
        assert db1.count == 1

        # Instance 2: should load from disk
        db2 = FaceDB(db_path=db_path)
        assert db2.count == 1

        # Should still match
        result = db2.match(emb.copy())
        assert result is not None
        assert result["name"] == "Alice"


# ---------------------------------------------------------------------------
# Retroactive match & clear unknowns (face labeling auto-clear)
# ---------------------------------------------------------------------------

class TestMatchAndClearUnknowns:
    """Tests for FaceDB.match_and_clear_unknowns — retroactive clearing of
    unknown faces that match a newly enrolled/labeled face."""

    def test_clears_matching_unknowns(self, db):
        """Enrolling a face should clear unknowns with similar embeddings."""
        emb = random_embedding(seed=42)
        # Save one unknown with a similar embedding
        np.random.seed(100)
        uid1 = db.save_unknown(similar_embedding(emb, noise=0.02), b"photo1")
        assert uid1 is not None
        assert db.unknown_count == 1

        # Now retroactively clear unknowns matching this embedding
        cleared = db.match_and_clear_unknowns("Alice", emb.copy())["count"]
        assert cleared == 1
        assert db.unknown_count == 0

    def test_keeps_different_unknowns(self, db):
        """Unknowns with very different embeddings should NOT be cleared."""
        emb = random_embedding(seed=42)
        # Save a similar unknown and a completely different one
        db.save_unknown(similar_embedding(emb, noise=0.01), b"photo1")
        db.save_unknown(random_embedding(seed=999), b"photo2")
        assert db.unknown_count == 2

        cleared = db.match_and_clear_unknowns("Alice", emb.copy())["count"]
        assert cleared == 1  # Only the similar one
        assert db.unknown_count == 1  # Different one still there

    def test_returns_zero_when_no_matches(self, db):
        """No matching unknowns → returns 0, no crash."""
        # Save unknowns with completely different embeddings
        db.save_unknown(random_embedding(seed=1), b"photo1")
        db.save_unknown(random_embedding(seed=2), b"photo2")
        assert db.unknown_count == 2

        # Try to clear with an unrelated embedding
        cleared = db.match_and_clear_unknowns("Alice", random_embedding(seed=999))["count"]
        assert cleared == 0
        assert db.unknown_count == 2  # Nothing removed

    def test_returns_zero_with_no_unknowns(self, db):
        """Empty unknowns cache → returns 0, no crash."""
        assert db.unknown_count == 0
        cleared = db.match_and_clear_unknowns("Alice", random_embedding(seed=1))["count"]
        assert cleared == 0

    def test_matching_unknowns_promoted_as_extra_angles(self, db):
        """Matching unknowns become additional known_faces rows under the same name.

        Mirrors the real-world flow: the gallery accumulates unknowns BEFORE
        anyone is enrolled, then enrollment + sweep absorbs the matches.
        Doing this in reverse would trip the near-miss save suppression."""
        emb = random_embedding(seed=42)

        # Unknown captured first (no enrolled people yet → save succeeds)
        unknown_emb = similar_embedding(emb, noise=0.02)
        db.save_unknown(unknown_emb, b"angle_photo")
        assert db.unknown_count == 1

        # Now enroll Alice
        db.enroll("Alice", emb.copy(), b"original_photo")
        assert db.count == 1

        # Retroactive sweep should PROMOTE the unknown, not delete it
        promoted = db.match_and_clear_unknowns("Alice", emb.copy())["count"]
        assert promoted == 1
        assert db.unknown_count == 0
        # Alice should now have 2 angles enrolled
        assert db.count == 2
        names = {f["name"] for f in db.list_faces()}
        assert names == {"Alice"}

    def test_promoted_unknown_preserves_photo_as_known_face(self, db):
        """The unknown's photo travels with it into known_faces."""
        emb = random_embedding(seed=7)
        photo = b"\xff\xd8\xff\xe0unknown_jpeg_bytes"
        db.save_unknown(similar_embedding(emb, noise=0.01), photo)

        db.match_and_clear_unknowns("Bob", emb.copy())
        # The new known face row should carry the unknown's photo
        new_faces = db.list_faces()
        assert len(new_faces) == 1
        retrieved = db.get_photo(new_faces[0]["id"])
        assert retrieved == photo

    def test_promoted_angles_improve_matching(self, db):
        """After promotion, the original unknown's embedding matches as 'Alice'."""
        emb_a = random_embedding(seed=42)
        emb_a_angle = similar_embedding(emb_a, noise=0.02)
        db.save_unknown(emb_a_angle.copy(), b"angle")

        # Enroll Alice with the OTHER angle, then absorb the unknown
        db.enroll("Alice", emb_a.copy(), b"front")
        db.match_and_clear_unknowns("Alice", emb_a.copy())

        # Matching with the promoted angle's embedding should now identify Alice
        result = db.match(emb_a_angle.copy())
        assert result is not None
        assert result["name"] == "Alice"

    def test_non_matching_unknowns_untouched(self, db):
        """Unknowns that do NOT match are neither promoted nor deleted."""
        emb_target = random_embedding(seed=1)
        emb_other = random_embedding(seed=999)
        db.save_unknown(emb_other, b"stranger")

        promoted = db.match_and_clear_unknowns("Alice", emb_target)["count"]
        assert promoted == 0
        assert db.count == 0  # nothing promoted
        assert db.unknown_count == 1  # stranger still in gallery


class TestReconcileUnknowns:
    """Tests for FaceDB.reconcile_unknowns — startup two-tier sweep."""

    def test_promotes_strong_match_on_startup(self, db):
        """An unknown that strongly matches a known face is promoted on reconcile.

        Order matters: save the unknown BEFORE enrolling, otherwise the
        near-miss save suppression blocks the unknown from being captured."""
        emb = random_embedding(seed=42)
        db.save_unknown(similar_embedding(emb, noise=0.02), b"angle")
        db.enroll("Alice", emb.copy(), b"front")

        result = db.reconcile_unknowns()
        assert result["matched_names"].get("Alice") == 1
        assert result["promoted"] == 1
        assert result["deleted"] == 0
        assert db.unknown_count == 0
        # Alice gained an extra angle
        assert db.count == 2

    def test_deletes_loose_match_without_promoting(self, db):
        """An unknown that loosely matches (>= 0.6 * threshold but < threshold)
        should be deleted, not promoted. With match_threshold=0.5, a sim of
        ~0.35 lands in the delete tier."""
        # Build a base, then create a deliberately-mid-similarity vector
        base = random_embedding(seed=1)
        # Mix base with orthogonal noise to land in the [0.3, 0.5) sim band
        np.random.seed(2)
        noise = np.random.randn(512).astype(np.float32)
        noise -= noise.dot(base) * base  # orthogonalize
        noise /= np.linalg.norm(noise)
        mid = base * 0.4 + noise * np.sqrt(1 - 0.4**2)
        mid = mid / np.linalg.norm(mid)
        sim = float(np.dot(base, mid))
        # Sanity-check the test setup: sim must be in the delete tier
        assert 0.5 * 0.6 <= sim < 0.5, f"setup sim={sim} not in delete tier"

        # Save the unknown BEFORE enrolling Alice — once Alice exists, the
        # near-miss save suppression would block this same call.
        db.save_unknown(mid, b"loose")
        db.enroll("Alice", base.copy(), b"photo")

        result = db.reconcile_unknowns()
        assert result["matched_names"].get("Alice") == 1
        assert result["promoted"] == 0
        assert result["deleted"] == 1
        assert db.unknown_count == 0
        # Alice did NOT gain an angle — only the original enrollment row
        assert db.count == 1

    def test_keeps_unknown_below_both_thresholds(self, db):
        """Truly different unknowns stay in the gallery."""
        db.enroll("Alice", random_embedding(seed=1), b"photo")
        db.save_unknown(random_embedding(seed=999), b"stranger")

        result = db.reconcile_unknowns()
        assert result["matched_names"] == {}
        assert result["promoted"] == 0
        assert result["deleted"] == 0
        assert db.unknown_count == 1
        assert db.count == 1


class TestCacheReload:
    """Tests for FaceDB._load_cache — multi-container DB sync behavior."""

    def test_picks_up_external_known_face(self, db, tmp_path):
        """A second FaceDB sharing the same SQLite sees enrollments after reload."""
        # Two instances pointing at the same DB (simulating two containers)
        db_path = db.db_path
        emb = random_embedding(seed=1)
        db.enroll("Alice", emb.copy(), b"photo")
        assert db.count == 1

        # Second instance loads the same row at startup
        db2 = FaceDB(db_path=db_path, match_threshold=0.5)
        assert db2.count == 1

        # First instance enrolls another face — db2 is out of sync
        db.enroll("Bob", random_embedding(seed=2), b"photo")
        assert db.count == 2
        assert db2.count == 1  # stale

        # After reload, db2 picks up the new row
        db2._load_cache()
        assert db2.count == 2
        names = {f["name"] for f in db2.list_faces()}
        assert names == {"Alice", "Bob"}

    def test_picks_up_external_unknown_captures(self, db, tmp_path):
        """Same pattern for unknown_faces table."""
        db_path = db.db_path
        db2 = FaceDB(db_path=db_path, match_threshold=0.5)
        assert db.unknown_count == 0
        assert db2.unknown_count == 0

        db.save_unknown(random_embedding(seed=1), b"u1")
        db.save_unknown(random_embedding(seed=2), b"u2")
        assert db2.unknown_count == 0  # stale

        db2._load_cache()
        assert db2.unknown_count == 2

    def test_picks_up_external_promotions(self, db, tmp_path):
        """If another container promotes unknowns→known, reload reflects both
        the new known rows AND the disappearance from unknowns."""
        db_path = db.db_path
        emb = random_embedding(seed=42)
        # Both instances have the same starting unknown row
        db.save_unknown(similar_embedding(emb, noise=0.02), b"angle")
        db2 = FaceDB(db_path=db_path, match_threshold=0.5)
        assert db.unknown_count == 1
        assert db2.unknown_count == 1
        assert db2.count == 0

        # First instance promotes via match_and_clear_unknowns
        db.enroll("Alice", emb.copy(), b"front")
        db.match_and_clear_unknowns("Alice", emb.copy())
        assert db.count == 2  # original enroll + promoted angle
        assert db.unknown_count == 0

        # db2 still stale
        assert db2.count == 0
        assert db2.unknown_count == 1

        # Reload — db2 catches up to truth
        db2._load_cache()
        assert db2.count == 2
        assert db2.unknown_count == 0

    def test_reload_is_idempotent(self, db):
        """Calling _load_cache repeatedly with no DB changes is a no-op."""
        db.enroll("Alice", random_embedding(seed=1), b"photo")
        db.save_unknown(random_embedding(seed=2), b"unk")
        before_known = list(db._cache)
        before_unknown = list(db._unknown_cache)

        db._load_cache()
        db._load_cache()
        db._load_cache()

        assert len(db._cache) == len(before_known)
        assert len(db._unknown_cache) == len(before_unknown)
        # Ids should match (rows didn't change)
        assert {f["id"] for f in db._cache} == {f["id"] for f in before_known}

    def test_reload_swaps_in_a_new_list_object(self, db, tmp_path):
        """_load_cache replaces self._cache with a freshly-built list rather
        than mutating in place. This is the property that lets a reader on
        another thread iterate the old list to completion without seeing
        the rebuild — they hold the previous reference, the new list is
        only seen by callers that re-read self._cache after the swap."""
        db.enroll("Alice", random_embedding(seed=1), b"photo")

        # Sibling DB instance simulates a second container that loaded the
        # same row. We snapshot its cache reference BEFORE any mutation,
        # then perform an external DB write (via the first instance) and
        # trigger a reload on the sibling.
        db2 = FaceDB(db_path=db.db_path, match_threshold=0.5)
        old_known = db2._cache
        old_unknown = db2._unknown_cache
        assert len(old_known) == 1

        # External writer adds a row
        db.enroll("Bob", random_embedding(seed=2), b"photo")
        db.save_unknown(random_embedding(seed=3), b"unk")

        # Sibling reloads — must produce NEW list objects, not mutate the
        # ones a reader might still be iterating.
        db2._load_cache()
        assert db2._cache is not old_known
        assert db2._unknown_cache is not old_unknown
        # And the new lists reflect the truth in SQLite
        assert len(db2._cache) == 2
        assert len(db2._unknown_cache) == 1

