import asyncio
import re

import aiohttp

from funding.utils.cache import cache_file_path, write_cache_file

GRAVATAR_LOCK = asyncio.Lock()
GRAVATAR_MAX_BYTES = 1024 * 1024


def gravatar_filename(hash: str) -> str:
    if not isinstance(hash, str) or re.fullmatch(r"[0-9a-fA-F]{32}", hash) is None:
        raise ValueError("Invalid Gravatar hash")
    return f"{hash.lower()}.png"


async def gravatar_download(hash: str, save_to_dir: str, filename: str) -> str:
    """Download a bounded avatar and atomically cache it under its validated hash."""
    expected_filename = gravatar_filename(hash)
    if filename != expected_filename:
        raise ValueError("Gravatar filename must match its hash")
    cache_file_path(save_to_dir, filename)
    url = f'https://www.gravatar.com/avatar/{hash.lower()}'
    async with GRAVATAR_LOCK:
        timeout = aiohttp.ClientTimeout(connect=3, total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                content_type = resp.headers.get('content-type', '').lower()
                if resp.status != 200 or not content_type.startswith("image/"):
                    raise RuntimeError("Invalid Gravatar image response")
                if resp.content_length is not None and resp.content_length > GRAVATAR_MAX_BYTES:
                    raise RuntimeError("Gravatar image exceeds size limit")

                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if len(data) + len(chunk) > GRAVATAR_MAX_BYTES:
                        raise RuntimeError("Gravatar image exceeds size limit")
                    data.extend(chunk)
                if not data:
                    raise RuntimeError("Empty Gravatar image response")
                return await asyncio.get_running_loop().run_in_executor(
                    None, write_cache_file, save_to_dir, filename, data)
