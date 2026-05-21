"""
routes/ai_tools/query_unknowns.py — implementation + schema for the `query_unknowns` tool.

Extracted from the legacy monolithic ai_tools.py (Phase J modularization).
The function and schema live together so adding/changing a tool is a single-
file change. ``__init__.py`` aggregates SCHEMA from every tool module into the
``TOOLS`` list that the chat endpoint passes to Ollama.
"""

import json
import logging

import routes as ctx


logger = logging.getLogger("dashboard.ai")


SCHEMA = {'type': 'function', 'function': {'name': 'query_unknowns', 'description': 'List unknown/unidentified faces that have been auto-captured by the system. Shows how many strangers have been seen.', 'parameters': {'type': 'object', 'properties': {}, 'required': []}}}


async def _tool_query_unknowns() -> str:
    """Query unknown/auto-captured faces from the face recognizer."""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            unknowns_resp = await client.get(f'{ctx.FACE_API_URL}/api/unknowns', timeout=5)
            faces_resp = await client.get(f'{ctx.FACE_API_URL}/api/faces', timeout=5)
        unknowns = []
        if unknowns_resp.status_code == 200:
            data = unknowns_resp.json()
            unknowns = data.get('unknowns', data) if isinstance(data, dict) else data
            if not isinstance(unknowns, list):
                unknowns = []
        enrolled_names = []
        if faces_resp.status_code == 200:
            data = faces_resp.json()
            face_list = data.get('faces', data) if isinstance(data, dict) else data
            if isinstance(face_list, list):
                enrolled_names = [f.get('name', '?') for f in face_list if isinstance(f, dict)]
        unique_enrolled = sorted(set(enrolled_names))
        SHOW_LIMIT = 20
        shown = unknowns[:SHOW_LIMIT]
        return json.dumps({'enrolled_people_count': len(unique_enrolled), 'enrolled_names': unique_enrolled, 'enrolled_photo_count': len(enrolled_names), 'unknown_count': len(unknowns), 'unknowns_shown': len(shown), 'truncated': len(unknowns) > SHOW_LIMIT, 'unknowns': [{'id': f.get('id', '?'), 'first_seen': f.get('first_seen', '?')} for f in shown], 'note': f'Showing latest {len(shown)} of {len(unknowns)} unknown faces.' if len(unknowns) > SHOW_LIMIT else f'All {len(unknowns)} unknown faces shown.'})
    except Exception as e:
        return json.dumps({'error': str(e)})
