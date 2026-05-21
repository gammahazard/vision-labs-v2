"""
routes/ai_tools/query_faces.py — implementation + schema for the `query_faces` tool.

Extracted from the legacy monolithic ai_tools.py (Phase J modularization).
The function and schema live together so adding/changing a tool is a single-
file change. ``__init__.py`` aggregates SCHEMA from every tool module into the
``TOOLS`` list that the chat endpoint passes to Ollama.
"""

import json
import logging

import routes as ctx


logger = logging.getLogger("dashboard.ai")


SCHEMA = {'type': 'function', 'function': {'name': 'query_faces', 'description': 'List all enrolled/known faces in the system.', 'parameters': {'type': 'object', 'properties': {}, 'required': []}}}


async def _tool_query_faces() -> str:
    """Query enrolled faces via the face recognizer API."""
    import httpx
    from collections import Counter
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f'{ctx.FACE_API_URL}/api/faces', timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            face_list = data.get('faces', data) if isinstance(data, dict) else data
            if not isinstance(face_list, list):
                face_list = []
            name_counts = Counter((f.get('name', 'unknown') for f in face_list if isinstance(f, dict)))
            people = [{'name': name, 'photos': count} for name, count in name_counts.most_common()]
            return json.dumps({'enrolled_people': len(people), 'faces': people})
        return json.dumps({'error': f'Face API returned {resp.status_code}'})
    except Exception as e:
        return json.dumps({'error': str(e)})
