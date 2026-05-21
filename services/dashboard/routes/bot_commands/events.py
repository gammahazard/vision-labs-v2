"""
routes/bot_commands/events.py — Telegram command handler(s).

Extracted from the legacy monolithic bot_commands.py (Phase J modularization).
The function and any per-command helpers live together so adding/changing a
command is a single-file change. ``__init__.py`` wires this into the dispatcher.
"""

import os



from ._shared import (
    send_text, send_photo, _telegram_get_cameras, _camera_friendly_name, _resolve_camera_token, make_redis_client, REDIS_HOST, REDIS_PORT,
)


async def _cmd_events(chat_id: str = "", text: str = "", **kwargs):
    """Show recent detection events with snapshot images.

    Examples:
      /events            — last 5 from all cameras (merged)
      /events 10         — last 10 from all cameras
      /events basement   — last 5 from basement only
      /events 10 all     — last 10 across every camera
    """
    from contracts.streams import EVENT_STREAM as _EVT_TMPL, stream_key as _stream_key

    cam_ids, remaining_text = _resolve_camera_token(text)
    # /events defaults to aggregating across all cameras when no token given.
    token_present = any(
        tok.lower() in (
            {c["id"].lower() for c in _telegram_get_cameras()} |
            {(c.get("name") or "").lower() for c in _telegram_get_cameras()} |
            {"all"}
        )
        for tok in text.split()
    )
    if not token_present:
        cam_ids = [c["id"] for c in _telegram_get_cameras()] or cam_ids

    # Parse count from the remaining text (camera already stripped)
    count = 5
    for p in remaining_text.split()[1:]:
        try:
            count = max(1, min(20, int(p)))
            break
        except ValueError:
            continue

    try:
        r_ev = make_redis_client(decode_responses=True, host=REDIS_HOST, port=REDIS_PORT)
        # Pull last N from each camera, merge by stream id (ms timestamp), trim to N
        merged = []
        for cid in cam_ids:
            evt_stream = _stream_key(_EVT_TMPL, camera_id=cid)
            entries = r_ev.xrevrange(evt_stream, count=count)
            for msg_id, data in entries:
                merged.append((msg_id, dict(data), cid))
        # Sort newest-first using the millisecond timestamp encoded in stream id
        def _ms(mid):
            try:
                return int(str(mid).split("-")[0])
            except Exception:
                return 0
        merged.sort(key=lambda x: _ms(x[0]), reverse=True)
        merged = merged[:count]

        if not merged:
            cams_label = ", ".join(_camera_friendly_name(c) for c in cam_ids) or "any camera"
            await send_text(f"📋 No events recorded yet on {cams_label}.", chat_id=chat_id)
            return

        cams_label = ", ".join(_camera_friendly_name(c) for c in cam_ids)
        await send_text(
            f"📋 <b>Recent Events</b> from {cams_label} (showing {len(merged)})",
            chat_id=chat_id,
        )

        # Build captions + photos using the shared renderer so this matches
        # whatever the web event feed shows. New event types are added in
        # event_renderer.py once and both consumers pick them up.
        from event_renderer import render_event
        from routes.events import resolve_event_snapshot_path

        for msg_id, data, src_cid in merged:
            mid = msg_id if isinstance(msg_id, str) else msg_id.decode()
            # Build the same dict shape the API returns, then render it.
            evt = {**data, "id": mid, "camera_id": data.get("camera_id", src_cid)}
            r = render_event(evt)

            # Prefix with camera name only when reporting across multiple cameras
            cam_prefix = (f"📷 {_camera_friendly_name(src_cid)} · "
                          if len(cam_ids) > 1 else "")
            caption_parts = [f"{cam_prefix}{r['icon']} <b>{r['title']}</b>"]
            if r["subtitle"]:
                caption_parts.append(r["subtitle"])
            caption = "\n".join(caption_parts)

            # Try to send an event snapshot photo if the renderer asked for one
            sent_photo = False
            photo = r.get("photo")
            if photo and photo.get("kind") in ("face", "event_snapshot"):
                snap_path = resolve_event_snapshot_path(mid, camera_id=src_cid)
                if snap_path and os.path.isfile(snap_path):
                    try:
                        with open(snap_path, "rb") as f:
                            snap_bytes = f.read()
                        if snap_bytes:
                            await send_photo(snap_bytes, caption, chat_id=chat_id)
                            sent_photo = True
                    except Exception:
                        pass

            if not sent_photo:
                await send_text(caption, chat_id=chat_id)

    except Exception as e:
        await send_text(f"⚠️ Failed to fetch events: {e}", chat_id=chat_id)
