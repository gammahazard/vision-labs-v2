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
import os
from pathlib import Path

logger = logging.getLogger("face-db")

# Default database path (overridden by env var in Docker)
DEFAULT_DB_PATH = "/data/faces.db"

# Maximum unknown faces to keep (oldest pruned when exceeded).
# Env-overridable so we don't have to rebuild to tune.
MAX_UNKNOWN_FACES = int(os.getenv("MAX_UNKNOWN_FACES", "100"))

# Similarity threshold for deduplicating unknown faces.
# Higher = stricter (fewer "this is the same person" matches → more entries kept).
UNKNOWN_DEDUP_THRESHOLD = float(os.getenv("UNKNOWN_DEDUP_THRESHOLD", "0.6"))



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
            self._migrate_genderage_columns(conn)
            conn.commit()
        logger.info(f"Face database initialized at {self.db_path}")

    def _migrate_genderage_columns(self, conn: sqlite3.Connection):
        """Idempotent migration: add sex/age columns when missing.

        InsightFace's buffalo_l includes a genderage head whose output we
        used to discard. Storing it makes the dashboard a bit more useful
        and costs nothing at inference time. Older rows stay NULL.

        Also adds sex_override / age_override on known_faces so the user
        can correct the model when it whiffs (kids and seniors are the
        common cases — the head was trained on adults 20-50).
        """
        for table in ("known_faces", "unknown_faces"):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "sex" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN sex TEXT")
                logger.info(f"Migration: added {table}.sex column")
            if "age" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN age REAL")
                logger.info(f"Migration: added {table}.age column")
        # Overrides only on known_faces — unknowns are transient.
        known_cols = {row[1] for row in conn.execute("PRAGMA table_info(known_faces)")}
        if "sex_override" not in known_cols:
            conn.execute("ALTER TABLE known_faces ADD COLUMN sex_override TEXT")
            logger.info("Migration: added known_faces.sex_override column")
        if "age_override" not in known_cols:
            conn.execute("ALTER TABLE known_faces ADD COLUMN age_override REAL")
            logger.info("Migration: added known_faces.age_override column")

    def _load_cache(self):
        """Rebuild both caches from SQLite and atomically swap them in.

        Safe to call concurrently with readers. Builds new lists locally,
        then assigns `self._cache` / `self._unknown_cache` in a single
        bytecode op each — readers iterating the OLD lists complete safely,
        and the next call sees the NEW lists. This is what lets multiple
        face-recognizer containers sharing the same SQLite stay in sync
        when one of them writes (enroll, label, reconcile) and the others
        need to pick up the change without a restart.
        """
        new_cache: list[dict] = []
        new_unknown_cache: list[dict] = []
        with sqlite3.connect(self.db_path) as conn:
            for row in conn.execute(
                "SELECT id, name, embedding, sex_override, age_override "
                "FROM known_faces"
            ):
                face_id, name, emb_bytes, sex_override, age_override = row
                embedding = np.frombuffer(emb_bytes, dtype=np.float32)
                new_cache.append({
                    "id": face_id, "name": name, "embedding": embedding,
                    "sex_override": sex_override,
                    "age_override": age_override,
                })

            for row in conn.execute(
                "SELECT id, embedding, sighting_count FROM unknown_faces"
            ):
                uid, emb_bytes, count = row
                embedding = np.frombuffer(emb_bytes, dtype=np.float32)
                new_unknown_cache.append({
                    "id": uid, "embedding": embedding, "sighting_count": count,
                })

        prev_known = len(getattr(self, "_cache", []))
        prev_unknown = len(getattr(self, "_unknown_cache", []))
        # Atomic swap (each assignment is a single STORE_ATTR bytecode op)
        self._cache = new_cache
        self._unknown_cache = new_unknown_cache

        if prev_known != len(new_cache) or prev_unknown != len(new_unknown_cache):
            logger.info(
                f"Cache reloaded: {len(new_cache)} known "
                f"(was {prev_known}), {len(new_unknown_cache)} unknown "
                f"(was {prev_unknown})"
            )
        else:
            logger.debug(
                f"Cache reload: no change "
                f"({len(new_cache)} known, {len(new_unknown_cache)} unknown)"
            )

    # ------------------------------------------------------------------
    # Known face operations
    # ------------------------------------------------------------------

    def enroll(self, name: str, embedding: np.ndarray, photo: bytes = None,
               sex: str | None = None, age: float | None = None) -> int:
        """Enroll a new known face. Returns the new face ID."""
        embedding = embedding / np.linalg.norm(embedding)
        emb_bytes = embedding.astype(np.float32).tobytes()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO known_faces (name, embedding, photo, sex, age) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, emb_bytes, photo, sex, age),
            )
            conn.commit()
            face_id = cursor.lastrowid

        # Re-read from SQLite instead of appending in place. If the periodic
        # cache-refresh thread fires between our commit and an in-place
        # .append(), we'd end up with a duplicate cache entry (the refresh
        # already picked up our row, then our append adds it again).
        self._load_cache()
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
                best_match = known

        if best_match and best_sim >= self.match_threshold:
            return {
                "id": best_match["id"],
                "name": best_match["name"],
                "similarity": round(best_sim, 3),
                # Carry through the manual override (if any) so the
                # caller can prefer it over the model's live estimate.
                "sex_override": best_match.get("sex_override"),
                "age_override": best_match.get("age_override"),
            }

        return None

    def list_faces(self) -> list[dict]:
        """Return all enrolled faces (without embeddings)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT id, name, created_at, sex, age, "
                "sex_override, age_override FROM known_faces "
                "ORDER BY created_at DESC"
            )
            return [
                {
                    "id": row[0], "name": row[1], "created_at": row[2],
                    "sex": row[3], "age": row[4],
                    "sex_override": row[5], "age_override": row[6],
                }
                for row in cursor
            ]

    def set_demographics_override(
        self, name: str, sex: str | None, age: float | None
    ) -> int:
        """Pin sex_override/age_override on every angle of `name`.

        Pass None to clear (revert to model output). Returns the number
        of rows updated.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE known_faces SET sex_override = ?, age_override = ? "
                "WHERE name = ?",
                (sex, age, name),
            )
            conn.commit()
            return cursor.rowcount

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
            self._load_cache()
            logger.info(f"Deleted face id={face_id}")

        return deleted

    # ------------------------------------------------------------------
    # Unknown face auto-capture
    # ------------------------------------------------------------------

    def save_unknown(self, embedding: np.ndarray, photo: bytes = None,
                     sex: str | None = None, age: float | None = None) -> int | None:
        """
        Save an unknown face, deduplicating by embedding similarity.

        If a similar unknown was already captured, just bumps sighting_count.
        Otherwise creates a new entry. Keeps at most MAX_UNKNOWN_FACES.

        Returns the unknown face ID if new, or None if deduplicated/skipped.
        """
        embedding = embedding / np.linalg.norm(embedding)

        # Near-miss suppression: if this embedding is plausibly a KNOWN
        # person at an off angle (sim >= match_threshold * 0.6 = 0.30),
        # don't save as unknown. Prevents the gallery from filling up with
        # "first-frame-of-Alice" captures that almost matched her enrolled
        # angles but didn't clear the live recognition threshold (0.50).
        # Same loose-match bar that reconcile uses to delete loose unknowns.
        near_miss_threshold = self.match_threshold * 0.6
        for known in self._cache:
            if float(np.dot(embedding, known["embedding"])) >= near_miss_threshold:
                # Plausibly known — skip to avoid polluting the gallery.
                return None

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
                "INSERT INTO unknown_faces (embedding, photo, sex, age) "
                "VALUES (?, ?, ?, ?)",
                (emb_bytes, photo, sex, age),
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
                "SELECT id, first_seen, last_seen, sighting_count, sex, age "
                "FROM unknown_faces ORDER BY last_seen DESC"
            )
            return [
                {
                    "id": row[0], "first_seen": row[1],
                    "last_seen": row[2], "sighting_count": row[3],
                    "sex": row[4], "age": row[5],
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
                "SELECT embedding, photo, sex, age FROM unknown_faces WHERE id = ?",
                (uid,),
            ).fetchone()
            if not row:
                return None

            emb_bytes, photo, sex, age = row

            cursor = conn.execute(
                "INSERT INTO known_faces (name, embedding, photo, sex, age) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, emb_bytes, photo, sex, age),
            )
            face_id = cursor.lastrowid

            conn.execute("DELETE FROM unknown_faces WHERE id = ?", (uid,))
            conn.commit()

        # Re-read both caches from DB — see enroll() for the reason.
        self._load_cache()
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
            self._load_cache()
            logger.info(f"Deleted unknown face id={uid}")

        return deleted

    def match_and_clear_unknowns(self, name: str, embedding: np.ndarray) -> dict:
        """
        Retroactively promote matching unknowns as additional angles for `name`.

        After enrolling or labeling a face, scan every unknown face in the DB.
        Any unknown with cosine similarity >= match_threshold is moved from
        unknown_faces → known_faces (same name), preserving its embedding +
        photo as an extra angle for recognition.

        Returns {
            "count": int,         # angles absorbed
            "promoted": [         # per-row details so the dashboard can show
                {                 # a "what got absorbed" modal with thumbnails
                    "face_id": int,         # new known_faces row id
                    "similarity": float,    # cosine sim that triggered it
                    "old_unknown_id": int,  # row id in unknown_faces before promotion
                },
                ...
            ]
        }
        """
        embedding = embedding / np.linalg.norm(embedding)
        # Capture (id, similarity) per candidate — we need the score so the
        # UI can render confidence next to each absorbed thumbnail.
        candidates: list[tuple[int, float]] = []
        for cached in self._unknown_cache:
            sim = float(np.dot(embedding, cached["embedding"]))
            if sim >= self.match_threshold:
                candidates.append((cached["id"], sim))

        if not candidates:
            return {"count": 0, "promoted": []}

        promoted: list[dict] = []
        candidate_ids = [uid for uid, _ in candidates]
        with sqlite3.connect(self.db_path) as conn:
            for uid, sim in candidates:
                row = conn.execute(
                    "SELECT embedding, photo, sex, age FROM unknown_faces WHERE id = ?",
                    (uid,),
                ).fetchone()
                if not row:
                    continue
                emb_bytes, photo, sex, age = row
                cursor = conn.execute(
                    "INSERT INTO known_faces (name, embedding, photo, sex, age) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, emb_bytes, photo, sex, age),
                )
                promoted.append({
                    "face_id": cursor.lastrowid,
                    "similarity": round(sim, 3),
                    "old_unknown_id": uid,
                })

            placeholders = ",".join("?" for _ in candidate_ids)
            conn.execute(
                f"DELETE FROM unknown_faces WHERE id IN ({placeholders})",
                candidate_ids,
            )
            conn.commit()

        # Re-read both caches from DB — see enroll() for the reason.
        self._load_cache()
        logger.info(
            f"Retroactive promotion: added {len(promoted)} angle(s) to "
            f"'{name}' from unknowns"
        )
        return {"count": len(promoted), "promoted": promoted}

    def reconcile_unknowns(self) -> dict:
        """
        Sweep all unknown faces against all known faces.

        Two tiers:
        - sim >= match_threshold: PROMOTE the unknown to known_faces under
          the matched name, preserving the embedding + photo as an extra
          recognition angle.
        - match_threshold > sim >= match_threshold * 0.6: DELETE the unknown
          (loose match — probably the same person but too noisy to keep as
          an embedding).
        - sim < match_threshold * 0.6: KEEP the unknown.

        Called on startup AND on demand via the dashboard "Scan unknowns"
        button. Returns:
            {
                "matched_names": {name: count},   # promoted + deleted per name
                "promoted": int,                  # total angles added
                "deleted": int,                   # loose matches removed
                "promoted_by_name": {             # per-name promotion details
                    name: [                       # so the dashboard modal can
                        {                         # render thumbnails + scores
                            "face_id": int,
                            "similarity": float,
                            "old_unknown_id": int,
                        }, ...
                    ]
                }
            }
        """
        if not self._cache or not self._unknown_cache:
            return {"matched_names": {}, "promoted": 0, "deleted": 0,
                    "promoted_by_name": {}}

        delete_threshold = self.match_threshold * 0.6  # relaxed cleanup tier
        logger.info(
            f"Reconciling {len(self._unknown_cache)} unknowns against "
            f"{len(self._cache)} known faces "
            f"(promote>={self.match_threshold:.2f}, delete>={delete_threshold:.2f})"
        )

        # Now (uid, name, similarity) so we can carry scores through to the UI.
        to_promote: list[tuple[int, str, float]] = []
        to_delete: list[int] = []
        matched_names: dict[str, int] = {}
        for unknown in self._unknown_cache:
            emb = unknown["embedding"] / np.linalg.norm(unknown["embedding"])
            best_sim = -1.0
            best_name = None
            for known in self._cache:
                similarity = float(np.dot(emb, known["embedding"]))
                if similarity > best_sim:
                    best_sim = similarity
                    best_name = known["name"]

            if best_sim >= self.match_threshold:
                to_promote.append((unknown["id"], best_name, best_sim))
                matched_names[best_name] = matched_names.get(best_name, 0) + 1
                logger.debug(
                    f"Reconcile: unknown {unknown['id']} → promote to "
                    f"'{best_name}' (sim={best_sim:.3f})"
                )
            elif best_sim >= delete_threshold:
                to_delete.append(unknown["id"])
                matched_names[best_name] = matched_names.get(best_name, 0) + 1
                logger.debug(
                    f"Reconcile: unknown {unknown['id']} → delete "
                    f"(loose match '{best_name}', sim={best_sim:.3f})"
                )
            else:
                logger.debug(
                    f"Reconcile: unknown {unknown['id']} keep (best "
                    f"'{best_name}' sim={best_sim:.3f} below {delete_threshold:.2f})"
                )

        if not to_promote and not to_delete:
            logger.info("Reconcile: no unknowns matched any known face")
            return {"matched_names": {}, "promoted": 0, "deleted": 0,
                    "promoted_by_name": {}}

        promoted_count = 0
        promoted_by_name: dict[str, list[dict]] = {}
        all_to_remove = [uid for uid, _n, _s in to_promote] + to_delete
        with sqlite3.connect(self.db_path) as conn:
            for uid, name, sim in to_promote:
                row = conn.execute(
                    "SELECT embedding, photo, sex, age FROM unknown_faces WHERE id = ?",
                    (uid,),
                ).fetchone()
                if not row:
                    continue
                emb_bytes, photo, sex, age = row
                cursor = conn.execute(
                    "INSERT INTO known_faces (name, embedding, photo, sex, age) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, emb_bytes, photo, sex, age),
                )
                promoted_by_name.setdefault(name, []).append({
                    "face_id": cursor.lastrowid,
                    "similarity": round(sim, 3),
                    "old_unknown_id": uid,
                })
                promoted_count += 1

            placeholders = ",".join("?" for _ in all_to_remove)
            conn.execute(
                f"DELETE FROM unknown_faces WHERE id IN ({placeholders})",
                all_to_remove,
            )
            conn.commit()

        # Re-read the source of truth from SQLite. Fixes the multi-container
        # race: whichever recognizer ran reconcile second sees the first's
        # commits authoritatively from the DB instead of trusting a stale plan.
        self._load_cache()

        logger.info(
            f"Reconcile complete: promoted {promoted_count} angle(s), "
            f"deleted {len(to_delete)} loose-match unknown(s)"
        )
        return {
            "matched_names": matched_names,
            "promoted": promoted_count,
            "deleted": len(to_delete),
            "promoted_by_name": promoted_by_name,
        }

    @property
    def count(self) -> int:
        """Number of enrolled known faces."""
        return len(self._cache)

    @property
    def unknown_count(self) -> int:
        """Number of unknown faces captured."""
        return len(self._unknown_cache)
