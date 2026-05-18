# services/vehicle-detector/Dockerfile
#
# PURPOSE:
#   GPU-enabled container for YOLO vehicle detection.
#
# INHERITS FROM `vision-labs-base:cuda12.8` — see pose-detector/Dockerfile
# for the full explanation; this image is structurally identical (both run
# ultralytics + custom .py file), the only difference is the class filter
# in detector.py.

FROM vision-labs-base:cuda12.8

WORKDIR /app

RUN python -m pip install --no-cache-dir \
      torch torchvision \
      --index-url https://download.pytorch.org/whl/cu128

RUN python -m pip install --no-cache-dir "ultralytics>=8.3.0"

COPY detector.py .

CMD ["python", "-u", "detector.py"]
