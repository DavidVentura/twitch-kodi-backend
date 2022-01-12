import asyncio
import logging
import sys

import aiocache
import uvicorn

from typing import Optional

from twitch_fapi_backend.twitch import Twitch
from twitch_fapi_backend import kodi
from twitch_fapi_backend import tasks

from dynaconf import settings
from fastapi import FastAPI, Path
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger()
cache = aiocache.SimpleMemoryCache()
t = Twitch(settings.CLIENT_ID, settings.CLIENT_SECRET)
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
async def root():
    return {"message": "Hi"}


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(t.get_token_forever())
    asyncio.create_task(tasks.store_progress())
    while not t.ready:
        await asyncio.sleep(0.1)

@app.get("/list")
async def list_streams():
    return await t.get_live_streams()


@app.get("/streamable_url")
async def streamable_url(user: str):
    return await t.get_streamable_url(f"https://twitch.tv/{user}")


@app.get("/cast_live")
async def cast(user: str):
    stream_obj = await t.get_stream(user)
    streamable_url = await t.get_streamable_url(f"https://twitch.tv/{user}")
    await cache.set(streamable_url, stream_obj)
    await kodi.cast(streamable_url)
    return stream_obj


@app.get("/cast_vod")
async def cast_vod(vod_id: str):
    vod = await t.get_vod(vod_id)
    streamable_url = await t.get_streamable_url(f"https://twitch.tv/videos/{vod_id}")
    await cache.set(streamable_url, vod)
    last_watched = await tasks.get_progress(vod)
    await kodi.cast_at_start_time(streamable_url, last_watched)
    return vod


@app.get("/vods")
async def all_vods():
    return await t.get_vods_from_favorites()


@app.get("/vods/{user}")
async def vods(user: str):
    return await t.get_vods(user)


@app.get("/currently_casting")
async def currently_casting():
    playing = await kodi.get_playing()
    got = await cache.get(playing)
    return got or playing


@app.get("/end")
async def end():
    await kodi.stop_playing()
    return None


def main():
    uvicorn.run(app, port=7777, host='0.0.0.0')
