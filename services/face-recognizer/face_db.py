"""
services/face-recognizer/face_db.py — SQLite wrapper for known + unknown face storage.

PURPOSE:
    Stores face embeddings (512-dim vectors) alongside names and photos.
    Used by the recognizer to match detected faces against known people.
    Also auto-captures unknown faces for retroactive labeling.

RELATIONSHIPS:
    - Used by: recognizer.py (queries for matches, stores new enrollments)
    - Used by: dashboard server.py (lists enrolled faces, deletes entries)
    - Persisted via: Docker volume mount (survives container restarts)

SCHEMA:
    known_faces:
        id, name, embedding, photo, created_at

    unknown_faces:
        id, embedding, photo, first_seen, last_seen, sighting_count
        (auto-captured when an unrecognized face is detected)
"""

import sqlite3
import numpy as np
import logging
from pathlib import Path

logger = logging.getLogger("face-db")

# Default database path (overridden by env var in Docker)
DEFAULT_DB_PATH = "/data/faces.db"

# Maximum unknown faces to keep (oldest pruned when exceeded)
MAX_UNKNOWN_FACES = 100

# Similarity threshold for deduplicating unknown faces
# (if a new unknown is >0.6 similar to an existing unknown, it's the same person)
UNKNOWN_DEDUP_THRESHOLD = 0.6


class FaceDB:
    """
    SQLite-backed face embedding database.

    Stores known faces as 512-dimensional float32 vectors. On query,
    computes cosine similarity against all stored embeddings and returns
    the best match if above the threshold.

    Also stores unknown faces for later labeling.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH, match_threshold: float = 0.5):
        self.db_path = db_path
        self.match_threshold = match_threshold

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

        # In-memory caches for fast matching
        self._cache: list[dict] = []
        self._unknown_cache: list[dict] = []
        self._load_cache()

    def _init_db(self):
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS known_faces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    photo BLOB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS unknown_faces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    embedding BLOB NOT NULL,
                    photo BLOB,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    sighting_count INTEGER DEFAULT 1
                )
            """)
            conn.commit()
        logger.info(f"Face database initialized at {self.db_path}")

    def _load_cache(self):
        """Load all embeddings into memory for fast cosine similarity."""
        self._cache = []
        self._unknown_cache = []
        with sqlite3.connect(self.db_path) as conn:
            for row in conn.execute("SELECT id, name, embedding FROM known_faces"):
                face_id, name, emb_bytes = row
                embedding = np.frombuffer(emb_bytes, dtype=np.float32)
                self._cache.append({
                    "id": face_id, "name": name, "embedding": embedding,
                })

            for row in conn.execute(
                "SELECT id, embedding, sighting_count FROM unknown_faces"
            ):
                uid, emb_bytes, count = row
                embedding = np.frombuffer(emb_bytes, dtype=np.float32)
                self._unknown_cache.append({
                    "id": uid, "embedding": embedding, "sighting_count": count,
                })

        logger.info(
            f"Loaded {len(self._cache)} known + "
            f"{len(self._unknown_cache)} unknown faces into cache"
        )

    # ------------------------------------------------------------------
    # Known face operations
    # ------------------------------------------------------------------

    def enroll(self, name: str, embedding: np.ndarray, photo: bytes = None) -> int:
        """Enroll a new known face. Returns the new face ID."""
        embedding = embedding / np.linalg.norm(embedding)
        emb_bytes = embedding.astype(np.float32).tobytes()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO known_faces (name, embedding, photo) VALUES (?, ?, ?)",
                (name, emb_bytes, photo),
            )
            conn.commit()
            face_id = cursor.lastrowid

        self._cache.append({"id": face_id, "name": name, "embedding": embedding})
        logger.info(f"Enrolled face: {name} (id={face_id})")
        return face_id

    def match(self, embedding: np.ndarray) -> dict | None:
        """
        Find the best matching known face using max similarity per person.

        With multi-angle enrollment, each person may have multiple embeddings.
        We compute similarity against ALL embeddings per person and use the
        BEST single-angle match as the score. This ensures that one good angle
        is sufficient for recognition — averaging would penalize side views
        when frontal embeddings drag the score down.

        Returns {id, name, similarity} or None if no match exceeds threshold.
        """
        if not self._cache:
            return None

        embedding = embedding / np.linalg.norm(embedding)

        # Find the single best match across all enrolled embeddings
        best_match = None
        best_sim = -1

        for known in self._cache:
            similarity = float(np.dot(embedding, known["embedding"]))
            if similarity > best_sim:
                best_sim = similarity
                best_match = {"id": known["id"], "name": known["name"]}

        if best_match and best_sim >= self.match_threshold:
            return {
                "id": best_match["id"],
                "name": best_match["name"],
                "similarity": round(best_sim, 3),
            }

        return None

    def list_faces(self) -> list[dict]:
        """Return all enrolled faces (without embeddings)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT id, name, created_at FROM known_faces ORDER BY created_at DESC"
            )
            return [
                {"id": row[0], "name": row[1], "created_at": row[2]}
                for row in cursor
            ]

    def get_photo(self, face_id: int) -> bytes | None:
        """Get the JPEG thumbnail for a specific known face."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT photo FROM known_faces WHERE id = ?", (face_id,)
            ).fetchone()
            return row[0] if row else None

    def delete(self, face_id: int) -> bool:
        """Remove an enrolled known face by ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM known_faces WHERE id = ?", (face_id,)
            )
            conn.commit()
            deleted = cursor.rowcount > 0

        if deleted:
            self._cache = [f for f in self._cache if f["id"] != face_id]
            logger.info(f"Deleted face id={face_id}")

        return deleted

    # ------------------------------------------------------------------
    # Unknown face auto-capture
    # ------------------------------------------------------------------

    def save_unknown(self, embedding: np.ndarray, photo: bytes = None) -> int | None:
        """
        Save an unknown face, deduplicating by embedding similarity.

        If a similar unknown was already captured, just bumps sighting_count.
        Otherwise creates a new entry. Keeps at most MAX_UNKNOWN_FACES.

        Returns the unknown face ID if new, or None if deduplicated.
        """
        embedding = embedding / np.linalg.norm(embedding)

        # Dedup: check if we've already captured this unknown person
        for cached in self._unknown_cache:
            similarity = float(np.dot(embedding, cached["embedding"]))
            if similarity >= UNKNOWN_DEDUP_THRESHOLD:
                # Same person — increment sighting count
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "UPDATE unknown_faces SET sighting_count = sighting_count + 1, "
                        "last_seen = CURRENT_TIMESTAMP WHERE id = ?",
                        (cached["id"],),
                    )
                    conn.commit()
                cached["sighting_count"] += 1
                return None  # Not a new entry

        # New unknown person — save
        emb_bytes = embedding.astype(np.float32).tobytes()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO unknown_faces (embedding, photo) VALUES (?, ?)",
                (emb_bytes, photo),
            )
            conn.commit()
            uid = cursor.lastrowid

        self._unknown_cache.append({
            "id": uid, "embedding": embedding, "sighting_count": 1,
        })

        logger.info(f"New unknown face captured (id={uid})")
        self._prune_unknowns()
        return uid

    def _prune_unknowns(self):
        """Remove oldest unknowns if count exceeds MAX_UNKNOWN_FACES."""
        if len(self._unknown_cache) <= MAX_UNKNOWN_FACES:
            return

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM unknown_faces WHERE id NOT IN "
                "(SELECT id FROM unknown_faces ORDER BY last_seen DESC LIMIT ?)",
                (MAX_UNKNOWN_FACES,),
            )
            conn.commit()

        # Reload cache
        self._unknown_cache = []
        with sqlite3.connect(self.db_path) as conn:
            for row in conn.execute(
                "SELECT id, embedding, sighting_count FROM unknown_faces"
            ):
                uid, emb_bytes, count = row
                emb = np.frombuffer(emb_bytes, dtype=np.float32)
                self._unknown_cache.append({
                    "id": uid, "embedding": emb, "sighting_count": count,
                })
        logger.info(f"Pruned unknown faces to {len(self._unknown_cache)}")

    def list_unknowns(self) -> list[dict]:
        """Return all unknown faces (for the dashboard gallery)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT id, first_seen, last_seen, sighting_count "
                "FROM unknown_faces ORDER BY last_seen DESC"
            )
            return [
                {
                    "id": row[0], "first_seen": row[1],
                    "last_seen": row[2], "sighting_count": row[3],
                }
                for row in cursor
            ]

    def get_unknown_photo(self, uid: int) -> bytes | None:
        """Get the JPEG thumbnail for an unknown face."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT photo FROM unknown_faces WHERE id = ?", (uid,)
            ).fetchone()
            return row[0] if row else None

    def label_unknown(self, uid: int, name: str) -> int | None:
        """
        Promote an unknown face to a known face by assigning a name.

        Moves embedding + photo from unknown_faces → known_faces.
        Returns the new known face ID, or None if unknown not found.
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT embedding, photo FROM unknown_faces WHERE id = ?", (uid,)
            ).fetchone()
            if not row:
                return None

            emb_bytes, photo = row

            cursor = conn.execute(
                "INSERT INTO known_faces (name, embedding, photo) VALUES (?, ?, ?)",
                (name, emb_bytes, photo),
            )
            face_id = cursor.lastrowid

            conn.execute("DELETE FROM unknown_faces WHERE id = ?", (uid,))
            conn.commit()

        # Update caches
        embedding = np.frombuffer(emb_bytes, dtype=np.float32)
        self._cache.append({"id": face_id, "name": name, "embedding": embedding})
        self._unknown_cache = [u for u in self._unknown_cache if u["id"] != uid]

        logger.info(f"Labeled unknown {uid} as '{name}' (known id={face_id})")
        return face_id

    def delete_unknown(self, uid: int) -> bool:
        """Remove an unknown face entry."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM unknown_faces WHERE id = ?", (uid,)
            )
            conn.commit()
            deleted = cursor.rowcount > 0

        if deleted:
            self._unknown_cache = [u for u in self._unknown_cache if u["id"] != uid]
            logger.info(f"Deleted unknown face id={uid}")

        return deleted

    def match_and_clear_unknowns(self, name: str, embedding: np.ndarray) -> int:
        """
        Retroactively match all unknown faces against a newly enrolled embedding.

        After enrolling or labeling a face, scan every unknown face in the DB.
        If cosine similarity >= match_threshold, delete that unknown (it's the
        same person who just got enrolled). Returns the count of cleared unknowns.
        """
        embedding = embedding / np.linalg.norm(embedding)
        to_delete = []

        for cached in self._unknown_cache:
            similarity = float(np.dot(embedding, cached["embedding"]))
            if similarity >= self.match_threshold:
                to_delete.append(cached["id"])

        if not to_delete:
            return 0

        # Batch delete from DB
        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join("?" for _ in to_delete)
            conn.execute(
                f"DELETE FROM unknown_faces WHERE id IN ({placeholders})",
                to_delete,
            )
            conn.commit()

        # Update cache
        delete_set = set(to_delete)
        self._unknown_cache = [
            u for u in self._unknown_cache if u["id"] not in delete_set
        ]

        logger.info(
            f"Retroactive match: cleared {len(to_delete)} unknowns matching '{name}'"
        )
        return len(to_delete)

    def reconcile_unknowns(self) -> dict:
        """
        Sweep all unknown faces against all known faces on startup.

        Any unknown whose embedding matches a known face is deleted.
        Uses a relaxed threshold (70% of match_threshold) since we're
        cleaning up, not making live identification decisions.
        Returns a dict of {name: count} for cleared unknowns.
        """
        if not self._cache or not self._unknown_cache:
            return {}

        reconcile_threshold = self.match_threshold * 0.6  # Relaxed for cleanup
        logger.info(
            f"Reconciling {len(self._unknown_cache)} unknowns against "
            f"{len(self._cache)} known faces (threshold={reconcile_threshold:.2f})"
        )

        to_delete = []
        matched_names = {}  # {name: count}
        for unknown in self._unknown_cache:
            emb = unknown["embedding"] / np.linalg.norm(unknown["embedding"])
            best_sim = -1
            best_name = None
            for known in self._cache:
                similarity = float(np.dot(emb, known["embedding"]))
                if similarity > best_sim:
                    best_sim = similarity
                    best_name = known["name"]

            if best_sim >= reconcile_threshold:
                to_delete.append(unknown["id"])
                matched_names[best_name] = matched_names.get(best_name, 0) + 1
                logger.info(
                    f"Reconcile: unknown {unknown['id']} matches "
                    f"'{best_name}' (sim={best_sim:.3f})"
                )
            else:
                logger.info(
                    f"Reconcile: unknown {unknown['id']} best match "
                    f"'{best_name}' (sim={best_sim:.3f}) — below threshold"
                )

        if not to_delete:
            logger.info("Reconcile: no unknowns matched any known face")
            return {}

        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join("?" for _ in to_delete)
            conn.execute(
                f"DELETE FROM unknown_faces WHERE id IN ({placeholders})",
                to_delete,
            )
            conn.commit()

        delete_set = set(to_delete)
        self._unknown_cache = [
            u for u in self._unknown_cache if u["id"] not in delete_set
        ]

        logger.info(f"Reconcile complete: cleared {len(to_delete)} unknowns")
        return matched_names

    @property
    def count(self) -> int:
        """Number of enrolled known faces."""
        return len(self._cache)

    @property
    def unknown_count(self) -> int:
        """Number of unknown faces captured."""
        return len(self._unknown_cache)
