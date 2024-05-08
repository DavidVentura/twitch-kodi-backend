import asyncio
import logging
import time

import pychromecast
import aiocache

from datetime import datetime, timezone

from twitch_fapi_backend import kodi
cache = aiocache.SimpleMemoryCache()
logger = logging.getLogger()


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


async def fetch_live_ccs_forever():
    zconf = pychromecast.zeroconf.Zeroconf()
    browser = pychromecast.CastBrowser(pychromecast.discovery.SimpleCastListener(), zconf, [])
    browser.start_discovery()
    logger.info("Started thread to poll Chromecasts")
    last_seen = []
    while True:
        ccs = browser.devices.values()
        ret = [cc.friendly_name for cc in ccs]
        if ret != last_seen:
            logger.info("Found Chromecasts %s", ret)
            last_seen = ret
            await cache.set("live_ccs", ret)

        pending_cast = await cache.get("cc_pending_cast")
        pending_url = await cache.get("cc_pending_url")
        if pending_cast and pending_url:
            await cache.delete("cc_pending_url")
            await cache.delete("cc_pending_cast")
            for cc in ccs:
                if cc.friendly_name != pending_cast:
                    continue
                logger.info("Found CC while looking to cast")
                await asyncio.to_thread(blocking_cast_to_chromecast, cc, zconf, pending_url)
        await asyncio.sleep(1)

# 
def blocking_cast_to_chromecast(ci: pychromecast.CastInfo, zconf: pychromecast.zeroconf.Zeroconf, url: str):
    logger.info("Getting CC from CI %s", ci)
    cast = pychromecast.get_chromecast_from_cast_info(ci, zconf)
    logger.info("Connecting to %s", ci.friendly_name)
    cast.wait()  # This takes like 5s
    logger.info("Connected to %s", ci.friendly_name)
    mc = cast.media_controller
    mc.play_media(url, 'video/mp4')
    logger.info("Blocking until active %s", ci.friendly_name)
    mc.block_until_active()
    logger.info("Is active %s", ci.friendly_name)

    mc.pause()
    time.sleep(1)
    mc.play()

async def cast_to_chromecast(url: str, cc_name: str):
    await cache.set("cc_pending_url", url)
    await cache.set("cc_pending_cast", cc_name)
