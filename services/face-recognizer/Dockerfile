# services/face-recognizer/Dockerfile
#
# PURPOSE:
#   Face recognition — InsightFace embeddings + SQLite DB.
#
# INHERITS FROM `vision-labs-base:cuda12.8` (cuDNN variant — required by
#   onnxruntime-gpu's CUDA execution provider).
#
# WHY NO PYTORCH HERE:
#   InsightFace uses ONNX Runtime, not PyTorch. Saves ~5 GB vs the
#   pose/vehicle detector images.
#
# PORTS:
#   8081 — Enrollment REST API (used by dashboard). Not exposed to host;
#   dashboard reaches it via the Docker bridge network.

FROM vision-labs-base:cuda12.8

WORKDIR /app

RUN python -m pip install --no-cache-dir \
      "insightface==0.7.3" \
      "onnxruntime-gpu>=1.20.0,<1.23" \
      "fastapi==0.115.0" \
      "uvicorn[standard]==0.32.0" \
      "Cython>=0.29.0"

COPY face_db.py recognizer.py ./

RUN mkdir -p /data

EXPOSE 8081

CMD ["python", "-u", "recognizer.py"]
