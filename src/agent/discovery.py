# src/agent/discovery.py
from agent.urls import (
    github_profile_url,
    hostname,
    normalize_web_url,
    profile_url,
    social_network,
)
from agent.models import (
    Identity,
    Network,
    SearchHit,
)
from agent.compliance import is_data_broker
from agent.config import DiscoveryTuning
from agent.search import SearchProvider
from itertools import zip_longest

import asyncio

PROFILE_SITES = {
    Network.LINKEDIN: "linkedin.com/in",
    Network.FACEBOOK: "facebook.com",
    Network.INSTAGRAM: "instagram.com",
}
EXCLUDED_SITES = "-site:linkedin.com -site:facebook.com -site:instagram.com"
WEB_TOPICS = (
    '(portfolio OR "personal website" OR blog)',
    "(publications OR paper OR article OR thesis)",
    "(talk OR conference OR speaker OR podcast OR webinar)",
)


def _quoted(value: str) -> str:
    # Inner double quotes are removed so the text cannot end the quoted phrase early and change the
    # search.
    """Wrap a value in double quotes for use as an exact search phrase.

    Args:
        value: The text to quote. Any double quotes inside it are removed.

    Returns:
        The quoted phrase.
    """
    # Inner double quotes are removed so the text cannot end the quoted phrase early and change the
    # search.
    return f'"{value.replace(chr(34), "")}"'


def names_clause(identity: Identity, tuning: DiscoveryTuning) -> str:
    """Build the search clause that matches a person's name and aliases.

    The name and aliases are used in order, up to the configured count. Names are
    added until their combined length passes the configured limit, but the first name
    is always kept.

    Args:
        identity: The person to search for.
        tuning: Discovery limits for the number and length of names.

    Returns:
        One quoted name, or several quoted names joined by OR in parentheses.
    """
    chosen: list[str] = []
    length = 0
    for name in (identity.name, *identity.aliases)[: tuning.max_names]:
        length += len(name)
        if chosen and length > tuning.max_names_length:
            break
        chosen.append(_quoted(name))
    return chosen[0] if len(chosen) == 1 else f"({' OR '.join(chosen)})"


def context_terms(identity: Identity) -> str:
    """Return extra search words taken from the person's location and employer.

    Args:
        identity: The person to search for.

    Returns:
        The location and the first employer joined by spaces, empty when unknown.
    """
    terms = [identity.location, *identity.employers[:1]]
    return " ".join(term for term in terms if term)


def fit_query(parts: list[str], max_words: int, max_chars: int) -> str:
    """Join query parts and drop trailing parts until the query fits the limits.

    Empty parts are skipped. The first non-empty part is always kept.

    Args:
        parts: The query parts in order of importance.
        max_words: The most words the search provider accepts.
        max_chars: The most characters the search provider accepts.

    Returns:
        The query with single spaces between words.
    """
    kept = [part for part in parts if part]
    while len(kept) > 1:
        query = " ".join(" ".join(kept).split())
        if len(query) <= max_chars and len(query.split()) <= max_words:
            return query
        kept.pop()
    return " ".join(" ".join(kept).split())


def profile_query(
    identity: Identity, network: Network, tuning: DiscoveryTuning, max_words: int, max_chars: int
) -> str:
    """Build a search query that finds a person's profile on one social network.

    Args:
        identity: The person to search for.
        network: The social network to restrict the search to.
        tuning: Discovery limits for the number and length of names.
        max_words: The most words the search provider accepts.
        max_chars: The most characters the search provider accepts.

    Returns:
        The query text, restricted to the network's profile pages.
    """
    parts = [
        f"site:{PROFILE_SITES[network]}",
        names_clause(identity, tuning),
        context_terms(identity),
    ]
    return fit_query(parts, max_words, max_chars)


def web_queries(
    identity: Identity, tuning: DiscoveryTuning, max_words: int, max_chars: int
) -> list[str]:
    """Build the search queries used to find a person's pages on the open web.

    There is one query per topic (portfolio, publications, talks) that excludes the
    social networks, plus a second query that targets GitHub.

    Args:
        identity: The person to search for.
        tuning: Discovery limits for the number and length of names.
        max_words: The most words the search provider accepts.
        max_chars: The most characters the search provider accepts.

    Returns:
        The queries in the order they are run.
    """
    names = names_clause(identity, tuning)
    context = context_terms(identity)
    skills = " ".join(_quoted(skill) for skill in identity.skills[:2]) or context
    queries = [
        fit_query([names, topic, EXCLUDED_SITES, context], max_words, max_chars)
        for topic in WEB_TOPICS
    ]
    queries.insert(1, fit_query([names, "site:github.com", skills], max_words, max_chars))
    return queries


def skill_queries(skill: str, location: str) -> dict[Network, str]:
    """Build the search queries used to find people with a skill.

    Args:
        skill: The skill to look for.
        location: A location to add to the queries, possibly empty.

    Returns:
        A mapping with a LinkedIn profile query and a GitHub query (under the web key).
    """
    return {
        Network.LINKEDIN: f"site:linkedin.com/in {_quoted(skill)} {location}",
        Network.WEB: f"site:github.com {_quoted(skill)} {location}",
    }


class Discovery:
    """Finds web pages and social profiles for people, companies, and skills.

    All searching goes through the configured search provider. Results from data
    brokers and, for general pages, from social networks are left out.
    """

    def __init__(self, provider: SearchProvider, tuning: DiscoveryTuning):
        """Initialize the discovery service.

        Args:
            provider: The search provider used for every query.
            tuning: Discovery limits such as results per query.
        """
        self._provider = provider
        self._tuning = tuning

    async def find_profiles(self, identity: Identity, network: Network) -> list[SearchHit]:
        """Find a person's profile pages on one social network.

        Args:
            identity: The person to search for.
            network: The social network to search.

        Returns:
            Profile hits with canonical profile URLs, without duplicates.
        """
        query = profile_query(
            identity,
            network,
            self._tuning,
            self._provider.max_query_words,
            self._provider.max_query_chars,
        )
        hits = await self._provider.search(query, self._tuning.results_per_query)
        return _profile_hits(hits, network)

    async def find_pages(self, identity: Identity) -> list[SearchHit]:
        """Find pages about a person on the open web.

        The web queries run at the same time. Each query contributes at most the
        configured number of hits.

        Args:
            identity: The person to search for.

        Returns:
            The usable hits without duplicate URLs, in query order.
        """
        queries = web_queries(
            identity,
            self._tuning,
            self._provider.max_query_words,
            self._provider.max_query_chars,
        )
        batches = await asyncio.gather(
            *(self._provider.search(query, self._tuning.results_per_query) for query in queries)
        )
        seen: set[str] = set()
        pages: list[SearchHit] = []
        for batch in batches:
            for hit in _usable_pages(batch)[: self._tuning.hits_per_web_query]:
                key = normalize_web_url(hit.url)
                if key not in seen:
                    seen.add(key)
                    pages.append(hit)
        return pages

    async def find_company_pages(self, name: str, limit: int) -> list[SearchHit]:
        """Find web pages about a company.

        Args:
            name: The company name.
            limit: The most hits to return.

        Returns:
            Usable hits that are not social profiles or data broker pages.
        """
        query = fit_query(
            [_quoted(name), "(company OR products OR about)", EXCLUDED_SITES],
            self._provider.max_query_words,
            self._provider.max_query_chars,
        )
        hits = await self._provider.search(query, self._tuning.results_per_query)
        return _usable_pages(hits)[:limit]

    async def find_skill_candidates(
        self, skill: str, location: str, limit: int, networks: list[Network]
    ) -> list[SearchHit]:
        """Find people who list a skill on LinkedIn or GitHub.

        Args:
            skill: The skill to look for.
            location: A location to add to the queries, possibly empty.
            limit: The most hits to return.
            networks: The networks to search. LinkedIn uses LinkedIn, web uses GitHub.

        Returns:
            Profile hits with LinkedIn and GitHub results interleaved, without duplicates.
        """
        queries = skill_queries(skill, location)
        count = self._tuning.results_per_query
        linkedin: list[SearchHit] = []
        github: list[SearchHit] = []
        if Network.LINKEDIN in networks:
            linkedin = await self._provider.search(queries[Network.LINKEDIN], count)
        if Network.WEB in networks:
            github = await self._provider.search(queries[Network.WEB], count)
        profiles = [
            SearchHit(url=url, title=hit.title, snippet=hit.snippet)
            for hit in github
            if (url := github_profile_url(hit.url))
        ]
        merged: list[SearchHit] = []
        seen: set[str] = set()
        interleaved = zip_longest(_profile_hits(linkedin, Network.LINKEDIN), profiles)
        for hit in (hit for pair in interleaved for hit in pair if hit is not None):
            if hit.url not in seen:
                seen.add(hit.url)
                merged.append(hit)
        return merged[:limit]


def _profile_hits(hits: list[SearchHit], network: Network) -> list[SearchHit]:
    """Keep only the hits that are profile pages of the given network.

    Args:
        hits: Raw search hits.
        network: The network the profiles must belong to.

    Returns:
        The hits with canonical profile URLs, without duplicates.
    """
    seen: set[str] = set()
    profiles: list[SearchHit] = []
    for hit in hits:
        canonical = profile_url(hit.url)
        # The site: operator is not guaranteed by the search engine, so each hit is checked to be a
        # real profile of this network.
        if canonical is None or social_network(hostname(canonical)) is not network:
            continue
        if canonical not in seen:
            seen.add(canonical)
            profiles.append(SearchHit(url=canonical, title=hit.title, snippet=hit.snippet))
    return profiles


def _usable_pages(hits: list[SearchHit]) -> list[SearchHit]:
    """Remove social network pages and data broker pages from search hits.

    Args:
        hits: Raw search hits.

    Returns:
        The hits that may be used as general web pages.
    """
    return [
        hit
        for hit in hits
        # Data broker pages are always excluded for compliance, and social pages are left to the
        # profile search.
        if social_network(hostname(hit.url)) is None and not is_data_broker(hit.url)
    ]
