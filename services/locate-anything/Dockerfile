# locate-anything — open-vocabulary visual grounding via nvidia/LocateAnything-3B.
#
# OPT-IN. The model is NON-COMMERCIAL (NVIDIA License) and is NOT baked into
# this image — it lazy-downloads from HF Hub at runtime into the locate-models
# volume, so the END USER fetches it and accepts NVIDIA's license. This image
# ships only our plumbing (code + public deps), so it's safe to publish to
# GHCR — publishing the wrapper does not redistribute the licensed weights.
#
# Mirrors the vehicle-attributes pattern: shared CUDA base + explicit torch.
FROM vision-labs-base:cuda12.8

# python3.11 in the base; bare `pip` maps to a different interpreter, so always
# use `python -m pip` (same gotcha as the other CUDA services).
RUN python -m pip install --no-cache-dir \
        torch torchvision --index-url https://download.pytorch.org/whl/cu128 \
 && python -m pip install --no-cache-dir \
        transformers==4.57.1 \
        accelerate \
        huggingface_hub \
        peft \
        Pillow \
        decord==0.6.0 \
        lmdb==1.7.5 \
        fastapi \
        "uvicorn[standard]" \
        python-multipart

WORKDIR /app
COPY app.py /app/app.py

# Model cache persists in the mounted volume across restarts.
ENV HF_HOME=/models
EXPOSE 8082
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8082"]
