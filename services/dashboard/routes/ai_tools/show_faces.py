"""
routes/ai_tools/show_faces.py — implementation + schema for the `show_faces` tool.

Extracted from the legacy monolithic ai_tools.py (Phase J modularization).
The function and schema live together so adding/changing a tool is a single-
file change. ``__init__.py`` aggregates SCHEMA from every tool module into the
``TOOLS`` list that the chat endpoint passes to Ollama.
"""

import json
import logging
import os
from datetime import datetime, timedelta

import routes as ctx
import routes.ai_state as ai_state

from ._shared import (
    KNOWN_EVENT_TYPES,
    KNOWN_EVENT_TYPES_DOC,
    EVENT_CATEGORIES,
    TZ_LOCAL,
    _category_matches,
    _camera_key,
    _camera_name,
    _get_camera_list,
    _redact_sensitive,
    _resolve_camera,
)

logger = logging.getLogger("dashboard.ai")


SCHEMA = {'type': 'function', 'function': {'name': 'show_faces', 'description': 'Show enrolled face photos. Sends up to 3 photos per person directly in the chat. Use when the user asks to see who is enrolled or wants to see face photos.', 'parameters': {'type': 'object', 'properties': {'name': {'type': 'string', 'description': "Optional: filter to a specific person's name. If omitted, shows all enrolled people."}}, 'required': []}}}


async def _tool_show_faces(args: dict) -> str:
    """Show enrolled face photos — up to 3 per person, sent as images."""
    import base64
    import httpx
    from collections import defaultdict
    filter_name = args.get('name', '').strip().lower()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f'{ctx.FACE_API_URL}/api/faces')
            if resp.status_code != 200:
                return json.dumps({'error': f'Face API returned {resp.status_code}'})
            data = resp.json()
        faces = data.get('faces', [])
        if not faces:
            return json.dumps({'message': 'No faces enrolled yet.'})
        by_name = defaultdict(list)
        for f in faces:
            name = f.get('name', 'unnamed')
            fid = f.get('id', '')
            if fid:
                by_name[name].append(fid)
        if filter_name:
            filtered = {n: ids for n, ids in by_name.items() if filter_name in n.lower()}
            if not filtered:
                return json.dumps({'error': f"No enrolled person matching '{filter_name}'", 'available': list(by_name.keys())})
            by_name = filtered
        images = []
        summary = []
        async with httpx.AsyncClient(timeout=5.0) as client:
            for name, face_ids in by_name.items():
                photos_to_fetch = face_ids[:3]
                fetched = 0
                for fid in photos_to_fetch:
                    try:
                        photo_resp = await client.get(f'{ctx.FACE_API_URL}/api/faces/{fid}/photo')
                        if photo_resp.status_code == 200:
                            b64 = base64.b64encode(photo_resp.content).decode('utf-8')
                            angle_label = f'angle {fetched + 1}' if len(photos_to_fetch) > 1 else ''
                            caption = f'{name} {angle_label}'.strip()
                            images.append({'url': f'data:image/jpeg;base64,{b64}', 'caption': caption})
                            fetched += 1
                    except Exception:
                        continue
                summary.append(f'{name}: {fetched}/{len(face_ids)} photo(s) shown')
        if images:
            ai_state.stash_images(images)
        return json.dumps({'photos_sent': len(images), 'people': list(by_name.keys()), 'summary': summary, 'instruction': 'Face photos have been sent to the chat. Describe who is enrolled and how many photos each person has.'})
    except Exception as e:
        return json.dumps({'error': str(e)})
