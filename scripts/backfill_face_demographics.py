"""
scripts/backfill_face_demographics.py — one-shot fill in sex/age for old rows.

Runs InsightFace on each stored face thumbnail (already 200×200 JPEG with
padding around the face) and writes sex + age back into the row. Idempotent:
only touches rows where sex IS NULL or age IS NULL.

Run inside the face-recognizer container (it has InsightFace + CUDA wired up).
For convenience, pipe this file via `docker exec -i`:

    docker exec -i vision-labs-face-recognizer-cam1-1 python3 - \
        < scripts/backfill_face_demographics.py
"""
import sqlite3
import sys

import cv2
import numpy as np
from insightface.app import FaceAnalysis


def main() -> int:
    db_path = "/data/faces.db"
    print(f"Opening {db_path}")
    conn = sqlite3.connect(db_path)

    print("Loading InsightFace (buffalo_l)…")
    app = FaceAnalysis(
        name="buffalo_l",
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    app.prepare(ctx_id=0, det_size=(320, 320))

    for table in ("known_faces", "unknown_faces"):
        rows = conn.execute(
            f"SELECT id, photo FROM {table} WHERE sex IS NULL OR age IS NULL"
        ).fetchall()
        print(f"\n[{table}] rows to backfill: {len(rows)}")
        if not rows:
            continue

        updated = 0
        no_photo = 0
        no_face = 0
        bad_image = 0
        for row_id, photo in rows:
            if not photo:
                no_photo += 1
                continue
            arr = np.frombuffer(photo, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                bad_image += 1
                continue
            faces = app.get(img)
            if not faces:
                no_face += 1
                continue
            face = max(
                faces,
                key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
            )
            sex = getattr(face, "sex", None)
            age = getattr(face, "age", None)
            sex = str(sex) if sex is not None else None
            age = float(age) if age is not None else None
            conn.execute(
                f"UPDATE {table} SET sex = ?, age = ? WHERE id = ?",
                (sex, age, row_id),
            )
            updated += 1
            if updated % 25 == 0:
                print(f"  {updated}/{len(rows)} updated…")
        conn.commit()
        print(
            f"  done: updated={updated} no_photo={no_photo} "
            f"no_face_detected={no_face} bad_image={bad_image}"
        )

    print("\nSample rows after backfill:")
    for table in ("known_faces", "unknown_faces"):
        print(f"  {table}:")
        for row in conn.execute(
            f"SELECT id, sex, age FROM {table} "
            f"WHERE sex IS NOT NULL OR age IS NOT NULL "
            f"ORDER BY id DESC LIMIT 5"
        ):
            print(f"    id={row[0]:3d}  sex={row[1]}  age={row[2]:.1f}" if row[2] is not None
                  else f"    id={row[0]:3d}  sex={row[1]}  age=None")
    return 0


if __name__ == "__main__":
    sys.exit(main())
