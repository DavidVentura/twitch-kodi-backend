import asyncio
import logging
import sys
import enum

import aiocache
import uvicorn

import aiomqtt

from contextlib import asynccontextmanager

from twitch_fapi_backend.twitch import Twitch
from twitch_fapi_backend import kodi
from twitch_fapi_backend import tasks

from dynaconf import settings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from twitch_dota_extension.lib import API, Playing

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger()
cache = aiocache.SimpleMemoryCache()
t = Twitch(settings.CLIENT_ID, settings.CLIENT_SECRET)

dota_api = API()
heroes = {}
items = {}
mqtt_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mqtt_client
    asyncio.create_task(t.get_token_forever())
    asyncio.create_task(tasks.store_progress())
    asyncio.create_task(tasks.fetch_live_ccs_forever())
    global heroes
    global items

    items = await dota_api.fetch_items()
    heroes = await dota_api.fetch_heroes()
    while not t.ready:
        await asyncio.sleep(0.1)

    async with aiomqtt.Client(hostname=settings.MQTT_HOST) as c:
        mqtt_client = c
        yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class Power(str, enum.Enum):
    ON = "on"
    OFF = "off"


class Inputs(str, enum.Enum):
    KODI = "kodi"
    CHROMECAST = "chromecast"


class CecCommands(str, enum.Enum):
    POWER_ON = "POWER_ON"
    POWER_OFF = "POWER_OFF"
    SOURCE_HDMI_1 = "SOURCE_HDMI_1"
    SOURCE_HDMI_2 = "SOURCE_HDMI_2"
    SOURCE_HDMI_3 = "SOURCE_HDMI_3"
    SOURCE_HDMI_4 = "SOURCE_HDMI_4"


INPUT_MAPPING = {Inputs.KODI: CecCommands.SOURCE_HDMI_4,
                 Inputs.CHROMECAST: CecCommands.SOURCE_HDMI_3,
                 }


@app.get("/")
async def root():
    return {"message": "Hi"}

@app.get("/list")
async def list_streams():
    return await t.get_live_streams()


@app.get("/streamable_url")
async def streamable_url(user: str):
    return {"url": await t.get_streamable_url(f"https://twitch.tv/{user}")}


@app.get("/live_cc")
async def live_cc():
    ccs = await cache.get("live_ccs", [])
    logger.info("got ccs %s", ccs)
    return ccs


@app.get("/cast_live_cc/{target}")
async def cast_target(user: str, target: str):
    stream_obj = await t.get_stream(user)
    streamable_url = await t.get_streamable_url(f"https://twitch.tv/{user}")
    logger.info("url %s", streamable_url)
    await tasks.cast_to_chromecast(streamable_url, target)
    return stream_obj


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


@app.get("/dota_info/{channel_id}")
async def dota_info(channel_id: int):
    game_state = await dota_api.get_stream_status(channel_id)
    if isinstance(game_state, Playing):
        phd = game_state.process_data(heroes, items)
        return phd
    else:
        return {"error": "Bad api response"}

@app.get("/currently_casting")
async def currently_casting():
    playing = await kodi.get_playing()
    got = await cache.get(playing)
    return got or playing


@app.get("/remote/input/{input}")
async def change_input(input: Inputs):
    res = await publish(INPUT_MAPPING[input])
    return {}


@app.get("/remote/tv/{power}")
async def tv_power(power: Power):
    if power is Power.ON:
        res = await publish(CecCommands.POWER_ON)
    elif power is Power.OFF:
        res = await publish(CecCommands.POWER_OFF)
    return {}

async def publish(comm: CecCommands):
    logger.info("Publishing %s to %s", comm.value, settings.CEC_TOPIC)
    res = await mqtt_client.publish(settings.CEC_TOPIC, comm.value)
    logger.info("Got %s", res)
    return res

@app.get("/end")
async def end():
    await kodi.stop_playing()
    return None

def main():
    uvicorn.run(app, port=7777, host='0.0.0.0')
