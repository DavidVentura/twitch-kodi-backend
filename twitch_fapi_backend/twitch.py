import asyncio
import logging

import aiocache
import streamlink
import httpx

from dynaconf import settings

logger = logging.getLogger()


class Twitch:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        streamlink_options = {"http-headers": f"Client-ID={client_id}"}
        self.streamlink = streamlink.Streamlink(options=streamlink_options)
        self.headers = {}
        self.ready = False

    async def get_token_forever(self):
        while True:
            if not self.ready:
                logger.info("Getting twitch token for startup")
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "https://id.twitch.tv/oauth2/token",
                    params={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "client_credentials",
                    },
                )
                r.raise_for_status()
            data = r.json()
            expires_in = data["expires_in"]
            self.at = data["access_token"]
            self.headers = {
                "Client-ID": self.client_id,
                "Authorization": f"Bearer {self.at}",
            }
            delta = expires_in - 30
            logger.info("got token, expires in %s, sleeping for %s", expires_in, delta)
            self.ready = True
            await asyncio.sleep(delta)

    @aiocache.cached(ttl=10)
    async def get_live_streams(self, users=None):
        if not users:
            users = settings.HIGHLIGHT
        if not self.headers:
            logger.warning("request came in before authenticated")
            return []

        params = {"language": "en", "user_login": users}
        url = f"https://api.twitch.tv/helix/streams"
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=self.headers, params=params, timeout=5)
            r.raise_for_status()

        data = [item for item in r.json()["data"]]
        for item in data:
            item["avatar"] = await self.get_avatar(item["user_login"])

        return data

    @aiocache.cached(ttl=36000)
    async def get_user(self, user):
        url = "https://api.twitch.tv/helix/users"
        async with httpx.AsyncClient() as client:
            r = await client.get(url, params={"login": user}, headers=self.headers, timeout=5)
        user = r.json()["data"][0]
        return user

    @aiocache.cached(ttl=36000)
    async def get_avatar(self, user):
        user = await self.get_user(user)
        return user["profile_image_url"]

    async def search_categories(self, query):
        url = f"https://api.twitch.tv/helix/search/categories"
        async with httpx.AsyncClient() as client:
            r = await client.get(url, params={"query": query}, headers=self.headers)
        return r.json()

    async def search_channels(self, query):
        url = f"https://api.twitch.tv/helix/search/channels"
        async with httpx.AsyncClient() as client:
            r = await client.get(url, params={"live_only": True, "query": query}, headers=self.headers)
        return r.json()

    async def get_streamable_url(self, query) -> str:
        data = self.streamlink.streams(query)
        return data["best"].url

    async def get_vods_from_favorites(self):
        vods = {}
        for vod_user in settings.VOD_USERS:
            vods[vod_user] = await self.get_vods(vod_user)
        return vods

    @aiocache.cached(ttl=1800)
    async def get_vods(self, username):
        url = f"https://api.twitch.tv/helix/videos"
        async with httpx.AsyncClient() as client:
            user = await self.get_user(username)
            r = await client.get(url, params={"user_id": user["id"], "first": 5}, headers=self.headers)
        return r.json()["data"]

    @aiocache.cached()
    async def get_vod(self, vod_id):
        url = f"https://api.twitch.tv/helix/videos"
        async with httpx.AsyncClient() as client:
            r = await client.get(url, params={"id": vod_id}, headers=self.headers)
        vod = r.json()["data"][0]
        vod["avatar"] = await self.get_avatar(vod["user_login"])
        return vod

    async def get_stream(self, user):
        streams = await self.get_live_streams(users=[user])
        return streams[0]
