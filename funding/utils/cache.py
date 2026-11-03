"""Contained, atomic writes for public PNG cache files."""
import os
import re
import tempfile


def cache_file_path(directory: str, filename: str) -> str:
    """Accept one ASCII-alphanumeric PNG filename, never a path or symlink."""
    if not isinstance(filename, str) or len(filename) > 255 or \
            re.fullmatch(r"[A-Za-z0-9]+\.png", filename) is None:
        raise ValueError("Invalid cache filename")
    base = os.path.realpath(directory)
    destination = os.path.join(base, filename)
    if os.path.islink(destination) or \
            os.path.dirname(os.path.realpath(destination)) != base:
        raise ValueError("Cache file must remain inside its cache directory")
    return destination


def write_cache_file(directory: str, filename: str, data: bytes) -> str:
    """Replace a cache entry without following an existing destination symlink."""
    destination = cache_file_path(directory, filename)
    base = os.path.dirname(destination)
    os.makedirs(base, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".cache-", suffix=".tmp", dir=base)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            # These are public images; preserve readability by the web server.
            os.fchmod(output.fileno(), 0o644)
        # A leaf symlink introduced after validation is replaced, not followed.
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination
