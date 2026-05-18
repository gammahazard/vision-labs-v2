# services/orchestrator/Dockerfile
#
# PURPOSE:
#   Sidecar that reconciles docker-compose profiles against the camera
#   registry. Tight scope: only knows how to up/down specific profiles
#   listed in ALLOWED_PROFILES env. No HTTP surface, no auth surface,
#   single-purpose worker.
#
# WHY THIS EXISTS:
#   So adding a camera in the dashboard UI actually starts its detector
#   services without the user having to copy-paste a docker compose
#   command into a terminal. The dashboard publishes a Redis event;
#   this service watches for those events and runs the right command.
#
# WHY A SEPARATE SERVICE FROM DASHBOARD:
#   The decision log in REFACTOR_PLAN.md says: never give the dashboard
#   the Docker socket. Its attack surface is too big (FastAPI app,
#   WebSocket, REST, user-facing). This service is tiny — if it ever
#   got compromised, all an attacker could do is up/down profiles we've
#   already whitelisted.
#
# BASE IMAGE:
#   docker:24-cli is Alpine + the Docker CLI + Compose CLI bundled.
#   We layer Python and redis-py on top. Final image is ~80 MB.

FROM docker:24-cli

RUN apk add --no-cache python3 py3-pip py3-redis

WORKDIR /app
COPY orchestrator.py .

# Run as root inside the container (default) so the Docker socket is
# readable. We don't network-bind anything, and the only filesystem
# this container touches besides its own is the host's Docker socket.
CMD ["python3", "-u", "/app/orchestrator.py"]
