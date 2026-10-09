# src/agent/urls.py
from urllib.parse import (
    parse_qsl,
    urlencode,
    urlsplit,
    urlunsplit,
)
from agent.models import Network

SOCIAL_NETWORKS = (Network.LINKEDIN, Network.FACEBOOK, Network.INSTAGRAM)

SOCIAL_DOMAINS: dict[str, Network] = {
    "linkedin.com": Network.LINKEDIN,
    "lnkd.in": Network.LINKEDIN,
    "facebook.com": Network.FACEBOOK,
    "fb.com": Network.FACEBOOK,
    "fb.me": Network.FACEBOOK,
    "fb.watch": Network.FACEBOOK,
    "instagram.com": Network.INSTAGRAM,
    "instagr.am": Network.INSTAGRAM,
}

_FACEBOOK_RESERVED = frozenset(
    {
        "ads",
        "business",
        "dialog",
        "events",
        "gaming",
        "groups",
        "hashtag",
        "help",
        "legal",
        "login",
        "marketplace",
        "permalink.php",
        "photo",
        "photo.php",
        "plugins",
        "policies",
        "posts",
        "privacy",
        "public",
        "reel",
        "reels",
        "share",
        "sharer",
        "sharer.php",
        "stories",
        "story.php",
        "videos",
        "watch",
    }
)

_INSTAGRAM_RESERVED = frozenset(
    {
        "about",
        "accounts",
        "challenge",
        "developer",
        "direct",
        "directory",
        "explore",
        "legal",
        "locations",
        "oauth",
        "p",
        "reel",
        "reels",
        "stories",
        "tags",
        "tv",
        "web",
    }
)

_GITHUB_RESERVED = frozenset(
    {
        "about",
        "apps",
        "collections",
        "enterprise",
        "events",
        "explore",
        "features",
        "issues",
        "join",
        "login",
        "marketplace",
        "notifications",
        "orgs",
        "pricing",
        "pulls",
        "search",
        "settings",
        "sponsors",
        "topics",
        "trending",
    }
)

_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src"})

_SECOND_LEVEL_LABELS = frozenset({"ac", "co", "com", "edu", "gov", "net", "org"})


def hostname(url: str) -> str:
    """Return the lower-case host name of a URL without a trailing dot.

    Args:
        url: the URL to read.

    Returns:
        The host name, or an empty string when the URL has none.
    """
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def social_network(host: str) -> Network | None:
    """Return the social network that owns a host name.

    The domain itself and any of its subdomains match.

    Args:
        host: lower-case host name.

    Returns:
        The matching network, or None when the host is not a known social site.
    """
    for domain, network in SOCIAL_DOMAINS.items():
        if host == domain or host.endswith(f".{domain}"):
            return network
    return None


def network_of(url: str) -> Network:
    """Return the network a URL belongs to.

    Args:
        url: the URL to classify.

    Returns:
        The social network of the host, or the general web network otherwise.
    """
    return social_network(hostname(url)) or Network.WEB


def registrable_domain(url: str) -> str:
    """Return the registrable domain of a URL's host.

    This is a heuristic without a public suffix list: it keeps the last two labels, or
    three for a two-letter country code under a common second level such as co.uk.

    Args:
        url: the URL to read.

    Returns:
        The domain, or the whole host when it has two labels or fewer.
    """
    labels = hostname(url).split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    # Handle country domains such as co.uk without needing a public suffix list.
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL_LABELS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def source_key(url: str) -> str:
    """Return a key that identifies the source of a URL.

    Args:
        url: the URL to classify.

    Returns:
        The registrable domain for ordinary web pages, or the network name for a
        social network.
    """
    network = network_of(url)
    if network is Network.WEB:
        return registrable_domain(url)
    return network.value


def profile_url(url: str) -> str | None:
    """Return the canonical personal profile URL for a social network page.

    Only http and https URLs are accepted. LinkedIn needs a /in/ path, Facebook paths
    that are not reserved site pages are accepted, and Instagram needs a first path
    segment that is not a reserved page. Other query strings and fragments are dropped.

    Args:
        url: URL of a page that may be a profile.

    Returns:
        The canonical profile URL, or None if the URL is not a person's profile.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"}:
        return None
    network = social_network((parts.hostname or "").lower())
    segments = [segment for segment in parts.path.split("/") if segment]
    if network is Network.LINKEDIN:
        if len(segments) >= 2 and segments[0].lower() == "in":
            return f"https://www.linkedin.com/in/{segments[1].lower()}"
        return None
    if network is Network.FACEBOOK:
        return _facebook_profile(segments, parts.query)
    if network is Network.INSTAGRAM:
        if segments and segments[0].lower() not in _INSTAGRAM_RESERVED:
            return f"https://www.instagram.com/{segments[0].lower()}/"
        return None
    return None


def _facebook_profile(segments: list[str], query: str) -> str | None:
    """Return the canonical Facebook profile URL for a URL path.

    Supports profile.php with a numeric id, people and pages paths with a name and an
    id, and plain usernames. Reserved site paths are rejected.

    Args:
        segments: non-empty path segments of the URL.
        query: query string of the URL.

    Returns:
        The canonical profile URL, or None when the path is not a profile.
    """
    if not segments:
        return None
    first = segments[0].lower()
    if first == "profile.php":
        ids = [value for key, value in parse_qsl(query) if key == "id" and value.isdigit()]
        return f"https://www.facebook.com/profile.php?id={ids[0]}" if ids else None
    if first in {"people", "pages"}:
        if len(segments) >= 3:
            return f"https://www.facebook.com/{first}/{segments[1]}/{segments[2]}"
        return None
    if first in _FACEBOOK_RESERVED:
        return None
    return f"https://www.facebook.com/{first}"


def github_profile_url(url: str) -> str | None:
    """Return the canonical GitHub profile URL for a user or organisation page.

    Only http and https URLs on github.com with a single, non-reserved path segment
    are accepted, so repository and site pages are rejected.

    Args:
        url: URL of a page that may be a profile.

    Returns:
        The canonical profile URL, or None if the URL is not a profile.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"}:
        return None
    if hostname(url) not in {"github.com", "www.github.com"}:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 1 or segments[0].lower() in _GITHUB_RESERVED:
        return None
    return f"https://github.com/{segments[0]}"


def instagram_username(url: str) -> str:
    """Return the first path segment of an Instagram URL.

    The URL must have a non-empty path, otherwise an IndexError is raised.

    Args:
        url: Instagram URL.

    Returns:
        The username as it appears in the URL.
    """
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    return segments[0]


def profile_slug(url: str) -> str | None:
    """Return the profile identifier found in the path of a social network URL.

    The case of the slug is kept as it appears in the URL.

    Args:
        url: URL of a profile page.

    Returns:
        The slug for LinkedIn, Facebook and Instagram URLs, or None for other URLs or
        when the path has no usable segment.
    """
    network = network_of(url)
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    if network is Network.LINKEDIN and len(segments) >= 2:
        return segments[1]
    if network is Network.FACEBOOK and segments:
        if segments[0] in {"people", "pages"} and len(segments) > 1:
            return segments[1]
        return segments[0]
    if network is Network.INSTAGRAM and segments:
        return segments[0]
    return None


def normalize_web_url(url: str) -> str:
    """Return a canonical form of a web URL, useful for comparing links.

    The scheme and host are lower-cased, utm_ and other tracking query parameters are
    removed, a trailing slash is stripped from the path and the fragment is dropped.

    Args:
        url: the URL to normalise.

    Returns:
        The normalised URL.
    """
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    host = (parts.hostname or "").lower()
    # Rebuilding the network location from host and port also drops any user:password@ part.
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))
