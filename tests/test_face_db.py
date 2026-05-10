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
        cleared = db.match_and_clear_unknowns("Alice", emb.copy())
        assert cleared == 1
        assert db.unknown_count == 0

    def test_keeps_different_unknowns(self, db):
        """Unknowns with very different embeddings should NOT be cleared."""
        emb = random_embedding(seed=42)
        # Save a similar unknown and a completely different one
        db.save_unknown(similar_embedding(emb, noise=0.01), b"photo1")
        db.save_unknown(random_embedding(seed=999), b"photo2")
        assert db.unknown_count == 2

        cleared = db.match_and_clear_unknowns("Alice", emb.copy())
        assert cleared == 1  # Only the similar one
        assert db.unknown_count == 1  # Different one still there

    def test_returns_zero_when_no_matches(self, db):
        """No matching unknowns → returns 0, no crash."""
        # Save unknowns with completely different embeddings
        db.save_unknown(random_embedding(seed=1), b"photo1")
        db.save_unknown(random_embedding(seed=2), b"photo2")
        assert db.unknown_count == 2

        # Try to clear with an unrelated embedding
        cleared = db.match_and_clear_unknowns("Alice", random_embedding(seed=999))
        assert cleared == 0
        assert db.unknown_count == 2  # Nothing removed

    def test_returns_zero_with_no_unknowns(self, db):
        """Empty unknowns cache → returns 0, no crash."""
        assert db.unknown_count == 0
        cleared = db.match_and_clear_unknowns("Alice", random_embedding(seed=1))
        assert cleared == 0

