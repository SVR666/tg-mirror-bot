"""Resolve strict release-ID deep links through a trusted, configured API."""

from asyncio import timeout
from re import fullmatch, search
from urllib.parse import parse_qs, urlsplit

from httpx import AsyncClient, HTTPError


class MirrorLookupError(Exception):
    """A release could not be resolved safely into a magnet."""


def mirror_deep_link_release_id(payload: str) -> str:
    """Accept only ``mirror_<positive decimal release ID>`` start payloads."""
    match = fullmatch(r"mirror_([1-9][0-9]{0,19})", payload)
    if match is None:
        raise ValueError("Invalid mirror deep-link payload")
    return match[1]


def release_lookup_url(api_url: str, release_id: str) -> str:
    """Append only the release ID to the configured URL ending in ``id=``."""
    if fullmatch(r"[1-9][0-9]{0,19}", release_id) is None:
        raise ValueError("Invalid release ID")
    prefix = api_url.strip()
    if not prefix:
        raise MirrorLookupError(
            "Mirror download links are disabled: AT_API_URL is not configured."
        )
    try:
        parts = urlsplit(prefix)
        valid = (
            parts.scheme in {"https", "http"}
            and parts.hostname
            and not parts.username
            and not parts.password
            and not parts.fragment
            and search(r"(?:^|&)id=$", parts.query)
            and not search(r"\s", prefix)
        )
    except ValueError:
        valid = False
    if not valid:
        raise MirrorLookupError("AT_API_URL must be an HTTP(S) URL ending in 'id='.")
    return f"{prefix}{release_id}"


def _valid_magnet(magnet: object) -> bool:
    if not isinstance(magnet, str) or not magnet.startswith("magnet:?"):
        return False
    # The mirror command parser splits on whitespace. Never accept injected flags.
    if search(r"\s", magnet):
        return False
    try:
        parts = urlsplit(magnet)
        topics = parse_qs(parts.query).get("xt", [])
    except ValueError:
        return False
    return not parts.fragment and any(
        fullmatch(r"urn:btih:(?:[0-9a-fA-F]{40}|[A-Za-z2-7]{32})", topic)
        for topic in topics
    )


async def fetch_release_magnet(api_url: str, release_id: str) -> str:
    """Fetch the complete magnet, including trackers; never follow redirects."""
    url = release_lookup_url(api_url, release_id)
    try:
        async with timeout(20), AsyncClient(
            timeout=20, follow_redirects=False
        ) as session:
            response = await session.get(url)
            if response.status_code != 200:
                raise MirrorLookupError(
                    "Could not fetch this release from the configured API. Please try again later."
                )
            data = response.json()
    except (HTTPError, TimeoutError, ValueError) as error:
        raise MirrorLookupError(
            "Could not fetch this release from the configured API. Please try again later."
        ) from error
    magnet = data.get("magnet_uri") if isinstance(data, dict) else None
    if not _valid_magnet(magnet):
        raise MirrorLookupError(
            "The configured API did not return a valid magnet for this release."
        )
    return magnet
