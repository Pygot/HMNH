# src/agent/collectors.py
from agent.models import (
    Network,
    SourceDocument,
    utcnow,
)
from agent.config import (
    CollectTuning,
    Settings,
)
from agent.urls import (
    instagram_username,
    SOCIAL_NETWORKS,
)
from agent.errors import SourceUnavailable
from collections.abc import Callable
from agent.apify import ApifyRunner
from datetime import datetime
from typing import Any

import re

NON_ALNUM = re.compile(r"[^a-z0-9]")
ALLOWED_FIELDS = frozenset(
    {
        "about",
        "aboutme",
        "bio",
        "biography",
        "businesscategoryname",
        "categories",
        "category",
        "certifications",
        "city",
        "company",
        "companyname",
        "country",
        "courses",
        "currentcompany",
        "currentposition",
        "description",
        "education",
        "educations",
        "experience",
        "experiences",
        "externalurl",
        "firstname",
        "fullname",
        "headline",
        "honors",
        "honorsandawards",
        "industry",
        "info",
        "intro",
        "jobtitle",
        "lastname",
        "location",
        "name",
        "occupation",
        "pagename",
        "patents",
        "position",
        "positions",
        "projects",
        "publications",
        "skills",
        "summary",
        "title",
        "topskills",
        "website",
        "websites",
        "workexperience",
    }
)
DENIED_FIELDS = frozenset(
    {
        "address",
        "avatar",
        "birthdate",
        "birthday",
        "connections",
        "connectionscount",
        "dateofbirth",
        "email",
        "emails",
        "family",
        "followercount",
        "followers",
        "followerscount",
        "friends",
        "gender",
        "insights",
        "latestposts",
        "likes",
        "messenger",
        "opentowork",
        "phone",
        "phonenumber",
        "phones",
        "politicalviews",
        "posts",
        "relatedprofiles",
        "relationship",
        "relationshipstatus",
        "religion",
        "streetaddress",
        "whatsapp",
    }
)
DENIED_SUFFIXES = (
    "photo",
    "picture",
    "image",
    "images",
    "avatar",
    "logo",
    "pic",
    "pichd",
    "linkedinurl",
    "universalname",
)
NAME_FIELDS = ("fullName", "name", "pageName", "title", "headline")
ERROR_FIELDS = ("errorMessage", "error", "scrape_error")
PRIVATE_FIELDS = ("private", "isPrivate")
InputBuilder = Callable[[str], dict[str, Any]]


def _key(name: str) -> str:
    """Normalise a field name for matching against the allow and deny lists.

    Args:
        name: The field name from the actor output.

    Returns:
        The name in lowercase with all non-alphanumeric characters removed.
    """
    return NON_ALNUM.sub("", name.lower())


def _denied(key: str) -> bool:
    """Check whether a field is on the deny list.

    Args:
        key: A normalised field name.

    Returns:
        True if the name is denied outright or ends with a denied suffix such as photo.
    """
    return key in DENIED_FIELDS or key.endswith(DENIED_SUFFIXES)


def _render(value: Any, depth: int, max_depth: int) -> str:
    """Render a nested value as compact text, leaving out denied fields.

    Booleans and None give no text, lists are joined with " | " and mappings are
    rendered as "name: value" pairs joined with "; ".

    Args:
        value: The value from the actor output.
        depth: The current nesting level.
        max_depth: The deepest level that is still rendered.

    Returns:
        The text, or an empty string when nothing is left to show.
    """
    if depth > max_depth or value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        parts = (_render(item, depth + 1, max_depth) for item in value)
        return " | ".join(part for part in parts if part)
    parts = (
        f"{name}: {rendered}"
        for name, item in value.items()
        if not _denied(_key(str(name)))
        if (rendered := _render(item, depth + 1, max_depth))
    )
    return "; ".join(parts)


def item_to_text(item: dict[str, Any], tuning: CollectTuning) -> str:
    """Convert an actor result item into text for the language model.

    Only allow-listed professional fields are used; contact details, photos, follower
    counts and similar fields are dropped.

    Args:
        item: One dataset item returned by the actor.
        tuning: The collection tuning with the depth and length limits.

    Returns:
        One "name: value" line per kept field, cut to the maximum length.
    """
    lines = []
    for name, value in item.items():
        key = _key(name)
        # Fields are allow-listed, so unexpected actor output never reaches the model.
        if key not in ALLOWED_FIELDS or _denied(key):
            continue
        rendered = _render(value, 0, tuning.max_depth)
        if rendered:
            lines.append(f"{name}: {rendered}")
    return "\n".join(lines)[: tuning.max_chars]


def _item_title(item: dict[str, Any], fallback: str) -> str:
    """Choose a display title for a profile.

    Args:
        item: One dataset item returned by the actor.
        fallback: The title to use when the item has no usable name.

    Returns:
        The first and last name, or the first non-empty name field, cut to 200
        characters; otherwise the fallback.
    """
    first, last = item.get("firstName"), item.get("lastName")
    if isinstance(first, str) and isinstance(last, str) and (first.strip() or last.strip()):
        return " ".join(f"{first} {last}".split())[:200]
    for name in NAME_FIELDS:
        value = item.get(name)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:200]
    return fallback


def _raise_for_item_problems(item: dict[str, Any], network: Network) -> None:
    """Raise an error if the actor reported a private profile or a failure.

    Args:
        item: One dataset item returned by the actor.
        network: The network the profile belongs to, used in the message.

    Raises:
        SourceUnavailable: when the item is marked private or carries an error message.
    """
    for name in PRIVATE_FIELDS:
        if item.get(name) is True:
            raise SourceUnavailable(f"The {network.value} profile is private; it was not read.")
    for name in ERROR_FIELDS:
        value = item.get(name)
        if isinstance(value, str) and value.strip():
            raise SourceUnavailable(
                f"The {network.value} actor reported: {' '.join(value.split())[:200]}"
            )


class ProfileCollector:
    """A collector of one public social profile through an Apify actor.

    The actor output is reduced to allow-listed professional fields before it becomes
    a SourceDocument.
    """

    def __init__(
        self,
        network: Network,
        runner: ApifyRunner,
        actor_id: str,
        build_input: InputBuilder,
        tuning: CollectTuning,
        now: Callable[[], datetime] = utcnow,
    ):
        """Initialize the collector.

        Args:
            network: The social network this collector reads.
            runner: The Apify runner that executes actors.
            actor_id: The id of the actor to run.
            build_input: A function that builds the actor input from a profile URL.
            tuning: The collection tuning with the depth and length limits.
            now: A clock returning the current time, replaceable in tests.
        """
        self.network = network
        self._runner = runner
        self._actor_id = actor_id
        self._build_input = build_input
        self._tuning = tuning
        self._now = now

    async def collect(self, url: str) -> SourceDocument:
        """Run the actor for a profile URL and return its public content.

        Only the first dataset item is used.

        Args:
            url: The profile URL to read.

        Returns:
            A SourceDocument with the title and the allow-listed text of the profile.

        Raises:
            SourceUnavailable: when the actor returns nothing, reports an error or a private
                profile, or the profile has no public professional content.
        """
        items = await self._runner.run(self._actor_id, self._build_input(url))
        if not items:
            raise SourceUnavailable(f"The {self.network.value} actor returned no data for {url}.")
        item = items[0]
        _raise_for_item_problems(item, self.network)
        text = item_to_text(item, self._tuning)
        if not text:
            raise SourceUnavailable(
                f"The {self.network.value} profile exposes no public professional content."
            )
        return SourceDocument(
            url=url,
            network=self.network,
            title=_item_title(item, url),
            text=text,
            retrieved_at=self._now(),
        )


def build_collectors(settings: Settings, runner: ApifyRunner) -> dict[Network, ProfileCollector]:
    """Create a collector for every social network that has an actor configured.

    Args:
        settings: The application settings holding actor ids and inputs.
        runner: The Apify runner shared by all collectors.

    Returns:
        A map of network to ProfileCollector; networks without an actor are left out.
    """
    builders: dict[Network, InputBuilder] = {
        Network.LINKEDIN: lambda url: {
            **settings.apify_linkedin_input,
            settings.apify_linkedin_url_field: [url],
        },
        Network.FACEBOOK: lambda url: {"startUrls": [{"url": url}]},
        Network.INSTAGRAM: lambda url: {"usernames": [instagram_username(url)]},
    }
    collectors = {}
    for network in SOCIAL_NETWORKS:
        actor = settings.collector_actor(network.value)
        if actor is not None:
            collectors[network] = ProfileCollector(
                network, runner, actor, builders[network], settings.collect
            )
    return collectors
