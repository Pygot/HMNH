# src/agent/web/assets.py
from agent.web.settings import WebSettings
from dataclasses import dataclass
from pathlib import Path

import hashlib
import gzip
import re

MEDIA_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


@dataclass(frozen=True)
class Asset:
    """A minified static file ready to serve, with a gzip copy and cache headers.

    Instances are immutable.
    """

    media_type: str
    body: bytes
    packed: bytes
    etag: str
    cache_control: str


def minify_css(text: str) -> str:
    """Shrink CSS by removing comments and unneeded whitespace.

    Args:
        text: The CSS source.

    Returns:
        The minified CSS.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([{};,>])\s*", r"\1", text)
    text = re.sub(r":\s+", ":", text)
    return text.replace(";}", "}").strip()


def minify_js(text: str) -> str:
    """Shrink JavaScript by trimming lines and dropping blank ones.

    The first line, the file header comment, is removed too.

    Args:
        text: The JavaScript source.

    Returns:
        The shortened source.
    """
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines[1:] if line)


def minify_markup(text: str, accent: str) -> str:
    """Shrink SVG markup and set its colour.

    Removes comments and whitespace between tags, and replaces currentColor with
    the accent colour.

    Args:
        text: The SVG source.
        accent: The colour that replaces currentColor.

    Returns:
        The minified markup.
    """
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r">\s+<", "><", stripped).strip().replace("currentColor", accent)


def build_assets(directory: Path, web: WebSettings) -> tuple[dict[str, Asset], dict[str, str]]:
    """Minify and fingerprint the static files of a directory.

    Each CSS, JS and SVG file is served under its plain name with a short cache
    lifetime and under a content-hashed name with a long immutable one.

    Args:
        directory: The folder holding the static files.
        web: Web settings for cache lifetimes, digest length and icon colour.

    Returns:
        The assets keyed by served file name, and a map from each original file
        name to its hashed URL under /static/.
    """
    plain = f"public, max-age={web.static_max_age_seconds}"
    hashed_cache = f"public, max-age={web.static_hashed_max_age_seconds}, immutable"
    served: dict[str, Asset] = {}
    urls: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix not in MEDIA_TYPES:
            continue
        raw = path.read_text(encoding="utf-8")
        minifiers = {
            ".css": minify_css,
            ".js": minify_js,
            ".svg": lambda text: minify_markup(text, web.favicon_color),
        }
        body = minifiers[path.suffix](raw).encode()
        digest = hashlib.sha256(body).hexdigest()[: web.asset_digest_chars]
        # A fixed mtime keeps the gzip bytes identical between builds.
        packed = gzip.compress(body, compresslevel=9, mtime=0)
        media = MEDIA_TYPES[path.suffix]
        hashed = f"{path.stem}.{digest}{path.suffix}"
        served[path.name] = Asset(media, body, packed, digest, plain)
        # The hashed name changes with the content, so it can safely be cached as immutable.
        served[hashed] = Asset(media, body, packed, digest, hashed_cache)
        urls[path.name] = f"/static/{hashed}"
    return served, urls
