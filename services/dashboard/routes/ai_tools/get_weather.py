"""
routes/ai_tools/get_weather.py — implementation + schema for the `get_weather` tool.

Extracted from the legacy monolithic ai_tools.py (Phase J modularization).
The function and schema live together so adding/changing a tool is a single-
file change. ``__init__.py`` aggregates SCHEMA from every tool module into the
``TOOLS`` list that the chat endpoint passes to Ollama.
"""

import json
import logging
import os



logger = logging.getLogger("dashboard.ai")


SCHEMA = {'type': 'function', 'function': {'name': 'get_weather', 'description': 'Get the current weather conditions at the camera location. Useful for correlating activity with weather.', 'parameters': {'type': 'object', 'properties': {}, 'required': []}}}


async def _tool_get_weather() -> str:
    """Get current weather from OpenWeatherMap."""
    import httpx
    api_key = os.getenv('OPENWEATHER_API_KEY', '')
    lat = os.getenv('LOCATION_LAT', '')
    lon = os.getenv('LOCATION_LON', '')
    if not api_key:
        return json.dumps({'error': 'OPENWEATHER_API_KEY not configured'})
    if not lat or not lon:
        return json.dumps({'error': 'LOCATION_LAT/LON not configured'})
    try:
        url = f'https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric'
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            weather = {'condition': data.get('weather', [{}])[0].get('description', 'unknown'), 'temperature_c': data.get('main', {}).get('temp'), 'feels_like_c': data.get('main', {}).get('feels_like'), 'humidity_pct': data.get('main', {}).get('humidity'), 'wind_speed_ms': data.get('wind', {}).get('speed'), 'visibility_m': data.get('visibility'), 'sunrise': data.get('sys', {}).get('sunrise'), 'sunset': data.get('sys', {}).get('sunset')}
            return json.dumps(weather)
        return json.dumps({'error': f'Weather API returned {resp.status_code}'})
    except Exception as e:
        return json.dumps({'error': str(e)})
