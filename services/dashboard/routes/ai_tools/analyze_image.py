"""
routes/ai_tools/analyze_image.py — implementation + schema for the `analyze_image` tool.

Extracted from the legacy monolithic ai_tools.py (Phase J modularization).
The function and schema live together so adding/changing a tool is a single-
file change. ``__init__.py`` aggregates SCHEMA from every tool module into the
``TOOLS`` list that the chat endpoint passes to Ollama.
"""

import json
import logging

import routes.ai_state as ai_state


logger = logging.getLogger("dashboard.ai")


SCHEMA = {'type': 'function', 'function': {'name': 'analyze_image', 'description': "Analyze the current live camera frame using the MiniCPM-V vision model. Returns a detailed visual description of what the camera sees RIGHT NOW. Use this when the user asks 'what do you see', 'describe the scene', 'look at the camera', or similar requests that need actual visual understanding beyond the tracker metadata.", 'parameters': {'type': 'object', 'properties': {'prompt': {'type': 'string', 'description': "Optional: specific question or instruction for the vision model. E.g. 'describe any people in detail', 'what vehicles are parked', 'is the gate open or closed'. Defaults to a general scene description."}}, 'required': []}}}


async def _tool_analyze_image(args: dict) -> str:
    """Analyze the current camera frame with MiniCPM-V vision model."""
    from routes.notifications import get_latest_frame, describe_scene
    import base64
    try:
        frame = get_latest_frame()
        if not frame:
            return json.dumps({'error': 'No frame available — camera may be offline'})
        b64 = base64.b64encode(frame).decode('utf-8')
        ai_state.stash_snapshot(b64)
        prompt = args.get('prompt', '') or 'Describe this security camera image in detail. Include: time of day (lighting), weather conditions if visible, any people (count, appearance, actions), vehicles, and anything notable or unusual.'
        description = await describe_scene(frame, prompt=prompt, timeout=30.0)
        if not description:
            return json.dumps({'snapshot_captured': True, 'vision_analysis': '(Vision model timed out or returned empty)', 'instruction': 'The snapshot is shown to the user. The vision model could not produce a description. Describe what you can from any available context.'})
        return json.dumps({'snapshot_captured': True, 'vision_analysis': description, 'instruction': "The snapshot is shown to the user. The 'vision_analysis' field contains a detailed description from the MiniCPM-V vision model of what the camera currently sees. Use this to answer the user's question. You may summarize or enhance the description."})
    except Exception as e:
        return json.dumps({'error': str(e)})
