import asyncio
import logging
import sys
import enum
import typing

from dataclasses import dataclass, asdict
from pydantic import BaseModel, Field

import aiocache
import httpx
import uvicorn

import aiomqtt

from contextlib import asynccontextmanager

from twitch_fapi_backend.twitch import Twitch
from twitch_fapi_backend import kodi
from twitch_fapi_backend import tasks

from dynaconf import settings
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from twitch_dota_extension.lib import (API, Playing, SpectatingTournament, Spectating, ProcessedHeroData, TourProcessedHeroData, SpectatingPglTournament,
        Source,
        )

WATCH_CACHE_KEY = 'watch_dotainfo'
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger()
cache = aiocache.SimpleMemoryCache()
t = Twitch(settings.CLIENT_ID, settings.CLIENT_SECRET)

dota_api = API()
heroes = {}
pgl_hero_map = {}
items = {}
mqtt_client: aiomqtt.Client | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mqtt_client
    asyncio.create_task(fill_watched_cache_forever())
    asyncio.create_task(t.get_token_forever())
    asyncio.create_task(tasks.store_progress())
    asyncio.create_task(tasks.fetch_live_ccs_forever())
    global heroes
    global pgl_hero_map
    global items

    items = await dota_api.fetch_items()
    heroes = await dota_api.fetch_heroes()
    try:
        pgl_hero_map = await dota_api.fetch_pgl_hero_mappings()
    except httpx.TimeoutException as e:
        logger.error("Failed to fetch PGL hero map: %s", e)
        pgl_hero_map = {}
    except Exception as e:
        logger.error("Failed HARD to fetch PGL hero map: %s", e)
        pgl_hero_map = {}

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


@app.get("/targets")
async def targets():
    ccs = await cache.get("live_ccs", [])
    logger.info("got ccs %s", ccs)
    return ["Kodi"] + ccs

async def _cast_url_to_target(url: str, target: str) -> bool:
    if target == "Kodi":
        logger.info("Casting %s to %s", url, target)
        await kodi.cast(url)
        return True
    ccs: list[str] = await cache.get("live_ccs", [])
    if target in ccs:
        logger.info("Casting %s to %s", url, target)
        await tasks.cast_to_chromecast(url, target)
        return True
    logger.info("Failed to casting %s to %s", url, target)
    return False

@app.get("/cast_live/{target}/{user}")
async def cast_live(user: str, target: str):
    stream_obj = await t.get_stream(user)
    streamable_url = await t.get_streamable_url(f"https://twitch.tv/{user}")
    if await _cast_url_to_target(streamable_url, target):
        await cache.set(streamable_url, stream_obj)
        to_watch = await cache.get(WATCH_CACHE_KEY, set())
        to_watch.add(user)
        logger.info("Monitoring channels for info: %s", to_watch)
        await cache.set(WATCH_CACHE_KEY, to_watch, 3600)
        return stream_obj
    return {"error": "invalid target"}

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


@dataclass
class DotaSingleResponse:
    type: typing.Literal['single']
    data: ProcessedHeroData

@dataclass
class DotaMultiResponse:
    type: typing.Literal['multiple']
    data: list[TourProcessedHeroData]

@dataclass
class DotaMultiResponseTour:
    type: typing.Literal['multiple']
    data: list[TourProcessedHeroData]

class DotaOkResponse(BaseModel):
    __root__: DotaSingleResponse | DotaMultiResponse | DotaMultiResponseTour = Field(..., discriminator="type")
class DotaErrResponse(typing.TypedDict):
    error: str
    response: dict[str, typing.Any]
    channel:  dict[str, typing.Any]

async def fill_watched_cache_forever():
    while True:
        logger.info("Updating dota info channel")
        to_watch = await cache.get(WATCH_CACHE_KEY, set())
        if len(to_watch) == 0:
            await asyncio.sleep(1)
            continue
        for channel in list(to_watch): # prevent changing-during-iteration errors
            # TODO: maybe parallel?
            try:
                res = await get_dota_info(channel)
            except Exception as e:
                logger.exception(e)
                await asyncio.sleep(10)
                continue

            if res is not None:
                CACHE_KEY = f'dotainfo_{channel}'
                await cache.set(CACHE_KEY, res)
        await asyncio.sleep(10)

async def get_dota_info(channel_name: str):
    channel_name = channel_name.lower()
    try:
        channel = await t.get_user(channel_name)
    except httpx.ReadTimeout:
        return {"error": "API timed out"}
    except asyncio.exceptions.CancelledError:
        return {"error": "API timed out"}

    try:
        channel_id = int(channel['id'])
    except Exception as e:
        logger.error("Bad user ID %s", channel_name)
        return None

    stream_source_key = f'dota_stream_source_{channel_name}'
    str_sour: Source | None = await cache.get(stream_source_key, None)
    game_state = await dota_api.get_stream_status(channel_id, str_sour)
    if isinstance(game_state, Playing):
        await cache.set(stream_source_key, Source.Streamer, 300)
        phd: ProcessedHeroData = game_state.process_data(channel_name, heroes, items)
        ret = DotaSingleResponse("single", phd)
        return ret
    elif isinstance(game_state, Spectating):
        await cache.set(stream_source_key, Source.Streamer, 300)
        phds: list[TourProcessedHeroData] = game_state.process_data(heroes, items)
        ret = DotaMultiResponse("multiple", phds)
        return ret
    elif isinstance(game_state, SpectatingTournament):
        await cache.set(stream_source_key, Source.Tournament, 300)
        phds: list[TourProcessedHeroData] = game_state.process_data(heroes, items)
        ret = DotaMultiResponse("multiple", phds)
        return ret
    elif isinstance(game_state, SpectatingPglTournament):
        await cache.set(stream_source_key, Source.PGL, 3600)
        phds: list[TourProcessedHeroData] = game_state.process_data(heroes, pgl_hero_map, items)
        ret = DotaMultiResponse("multiple", phds)
        return ret

    logger.warning("Bad API response: %s", asdict(game_state))
    return None

@app.get("/dota_info/{channel_name}", responses={200:{"model": DotaOkResponse}, 400:{"model": DotaErrResponse}})
async def dota_info(channel_name: str):
    channel_name = channel_name.lower()
    CACHE_KEY = f'dotainfo_{channel_name}'
    to_watch = await cache.get(WATCH_CACHE_KEY, set())
    to_watch.add(channel_name)
    logger.info("Monitoring channels for info: %s", to_watch)
    await cache.set(WATCH_CACHE_KEY, to_watch, 3600)
    got = await cache.get(CACHE_KEY)
    if got:
        return got

    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=None)

class TwitchChannel(BaseModel):
    user_id: str
    user_login: str
    user_name: str
    game_id: str
    game_name: str
    type: typing.Literal['live']
    title: str
    thumbnail_url: str
    avatar: str

class FileInfo(BaseModel):
    type: typing.Literal['file']
    filename: str

class CurrentlyCasting(BaseModel):
    __root__: TwitchChannel | FileInfo  = Field(..., discriminator="type")

@app.get("/currently_casting", responses={200:{"model": CurrentlyCasting|None}})
async def currently_casting():
    playing: None | str = await kodi.get_playing()
    if playing is None:
        return None
    got = await cache.get(playing)
    return got or {"type": "file", "filename": playing}


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
    assert mqtt_client is not None
    res = await mqtt_client.publish(settings.CEC_TOPIC, comm.value)
    logger.info("Got %s", res)
    return res

@app.get("/end")
async def end():
    await kodi.stop_playing()
    return None

def main():
    uvicorn.run(app, port=7777, host='0.0.0.0')
