import asyncio
import logging

import httpx

from datetime import timedelta

from dynaconf import settings

logger = logging.getLogger()


async def cast(url):
    payload = {
        "jsonrpc": "2.0",
        "method": "Player.Open",
        "params": {"item": {"file": url}},
        "id": 1,
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(settings.KODI_JSONRPC, json=payload)
        r.raise_for_status()
        logger.info("sent payload to kodi")


async def get_player():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "Player.GetActivePlayers"}
    async with httpx.AsyncClient() as client:
        req = await client.post(settings.KODI_JSONRPC, json=payload, timeout=1)
    playlists = req.json()
    for res in playlists.get("result", []):
        if res["type"] == "video":
            return res["playerid"]

async def stop_playing():
    player = await get_player()
    if player is None:
        return

    payload = {
        "jsonrpc": "2.0",
        "method": "Player.Stop",
        "params": {"playerid": player},
        "id": 1,
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(settings.KODI_JSONRPC, json=payload)
        r.raise_for_status()
        logger.info("sent payload to kodi")


async def get_time_played():
    player = await get_player()
    if player is None:
        return

    payload = {
        "jsonrpc": "2.0",
        "method": "Player.GetProperties",
        "params": {"properties": ["time"], "playerid": player},
        "id": "VideoGetItem",
    }

    async with httpx.AsyncClient() as client:
        req = await client.post(settings.KODI_JSONRPC, json=payload, timeout=1)
    req.raise_for_status()
    td = timedelta(**req.json()['result']['time'])
    return td.total_seconds()


async def get_playing():
    player = await get_player()
    if player is None:
        return

    payload = {
        "jsonrpc": "2.0",
        "method": "Player.GetItem",
        "params": {"properties": ["file"], "playerid": player},
        "id": "VideoGetItem",
    }

    async with httpx.AsyncClient() as client:
        req = await client.post(settings.KODI_JSONRPC, json=payload, timeout=1)
    req.raise_for_status()
    result = req.json()
    ret = ""
    try:
        ret = result["result"]["item"]["file"]
    except:
        ret = ""
    return ret

async def cast_at_start_time(url, total_seconds):
    await cast(url)
    live = await get_playing()
    while live != url:
        live = await get_playing()
        await asyncio.sleep(0.1)
    await asyncio.sleep(1)

    player = await get_player()
    if player is None:
        return

    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600 ) // 60)
    seconds = int(total_seconds % 60)
    payload = {
        "jsonrpc": "2.0",
        "method": "Player.Seek",
        "params": {"value": {"time": {"hours": hours, "minutes": minutes, "seconds": seconds}}, "playerid": player},
        "id": "VideoGetItem",
    }

    async with httpx.AsyncClient() as client:
        req = await client.post(settings.KODI_JSONRPC, json=payload, timeout=1)
    req.raise_for_status()
    result = req.json()
