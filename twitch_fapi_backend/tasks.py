import asyncio

import aiocache

from datetime import datetime, timedelta, timezone

from twitch_fapi_backend import kodi
cache = aiocache.SimpleMemoryCache()


def key_for_stream(stream):
    if stream['type'] == 'archive':
        _id = stream['stream_id']
    elif stream['type'] == 'live':
        _id = stream['id']
    return f"watched_{_id}"

async def store_progress():
    while True:
        await asyncio.sleep(10)
        playing = await kodi.get_playing()
        stream_obj = await cache.get(playing)
        progress = -1
        if not stream_obj:
            continue

        if stream_obj['type'] == 'live':
            now = datetime.now().astimezone()
            naive_tstamp = datetime.strptime(stream_obj["started_at"], '%Y-%m-%dT%H:%M:%SZ')
            aware_tstamp = naive_tstamp.replace(tzinfo=timezone.utc)
            progress = int((now - aware_tstamp).total_seconds())
        else:
            progress = int(await kodi.get_time_played())

        await cache.set(key_for_stream(stream_obj), progress)

async def get_progress(stream):
    key = key_for_stream(stream)
    obj = await cache.get(key)
    if not obj:
        return 0
    return obj
