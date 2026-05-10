"""
services/dashboard/helpers/ — small pure-function modules used by the dashboard.

PURPOSE:
    Geometry / math / utility helpers that don't belong in a route module
    or in the WebSocket loop. Keeping these as pure functions makes them
    easy to test (no Redis, no FastAPI, no global state).

CURRENT CONTENTS:
    - geometry.py  — bbox IoU + dead-zone point-in-polygon test
"""
