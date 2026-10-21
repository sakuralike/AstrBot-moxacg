import asyncio
import io
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from PIL import Image

from .client import BridgeError, Settings

MAX_IMAGE_BYTES = 2 * 1024 * 1024


def allowed_address(address: str, local: bool) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        return ip.is_global or (local and ip.is_loopback)
    except ValueError:
        return False


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, local: bool):
        self.local = local
        self.resolver = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        result = await self.resolver.resolve(host, port, family)
        if not result or any(not allowed_address(item["host"], self.local) for item in result):
            raise OSError("Blocked image address")
        return result

    async def close(self):
        await self.resolver.close()


def validate_image(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
            raise ValueError("Unsupported image")
        if image.width * image.height * getattr(image, "n_frames", 1) > 4_000_000:
            raise ValueError("Image too large")
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
    return data


async def fetch_avatar(url: str, settings: Settings) -> bytes:
    resolver = PublicResolver(settings.allow_local_http)
    try:
        async with asyncio.timeout(min(settings.timeout, 10)), aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=resolver),
            timeout=aiohttp.ClientTimeout(total=min(settings.timeout, 10), connect=3),
            cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
        ) as session:
            for _ in range(4):
                if not isinstance(url, str) or any(ord(c) < 33 for c in url) or "\\" in url:
                    raise ValueError("Invalid image URL")
                parsed = urlsplit(url)
                host = parsed.hostname
                if host not in settings.avatar_hosts or parsed.username is not None:
                    raise ValueError("Image host is not allowed")
                local = host == "localhost"
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    pass
                else:
                    if not allowed_address(host, settings.allow_local_http):
                        raise ValueError("Blocked image address")
                    local = ipaddress.ip_address(host).is_loopback
                if parsed.scheme != "https" and not (
                    settings.allow_local_http and local and parsed.scheme == "http"
                ):
                    raise ValueError("HTTPS is required")
                if parsed.port not in {None, 443} and not (local and settings.allow_local_http):
                    raise ValueError("Blocked image port")
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    if response.status != 200 or response.content_type not in {
                        "image/png", "image/jpeg", "image/webp", "image/gif",
                    }:
                        raise ValueError("Invalid image response")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        data.extend(chunk)
                        if len(data) > MAX_IMAGE_BYTES:
                            raise ValueError("Image too large")
                    return await asyncio.to_thread(validate_image, bytes(data))
            raise ValueError("Too many redirects")
    except (aiohttp.ClientError, OSError, ValueError, TimeoutError, Image.DecompressionBombError):
        raise BridgeError("avatar_unavailable") from None
    finally:
        await resolver.close()
