# tests/test_discovery.py
from agent.discovery import (
    context_terms,
    Discovery,
    fit_query,
    names_clause,
    profile_query,
    skill_queries,
    web_queries,
)
from agent.models import (
    Identity,
    Network,
)
from tests.fakes import (
    FakeSearch,
    hit,
)
from agent.config import DiscoveryTuning

TUNING = DiscoveryTuning()
WORDS = 32
CHARS = 400
JAN = Identity(
    name="Jan Novak",
    aliases=["pygot"],
    location="Brno",
    employers=["Acme", "Globex"],
    skills=["python", "rust", "go"],
)


def test_names_clause_quotes_names_and_strips_quotes():
    assert names_clause(Identity(name='Jan "J" Novak'), TUNING) == '"Jan J Novak"'
    assert names_clause(JAN, TUNING) == '("Jan Novak" OR "pygot")'


def test_names_clause_is_bounded_by_the_configuration():
    identity = Identity(name="a" * 110, aliases=["b" * 110, "c" * 110])
    assert names_clause(identity, TUNING) == f'"{"a" * 110}"'
    short = Identity(name="aa", aliases=["bb", "cc", "dd"])
    assert names_clause(short, TUNING) == '("aa" OR "bb" OR "cc")'
    assert names_clause(short, DiscoveryTuning(max_names=2)) == '("aa" OR "bb")'
    assert names_clause(short, DiscoveryTuning(max_names=1)) == '"aa"'


def test_context_uses_location_and_first_employer():
    assert context_terms(JAN) == "Brno Acme"
    assert context_terms(Identity(name="Jan Novak")) == ""


def test_profile_queries_target_each_network():
    linkedin = profile_query(JAN, Network.LINKEDIN, TUNING, WORDS, CHARS)
    assert linkedin == 'site:linkedin.com/in ("Jan Novak" OR "pygot") Brno Acme'
    assert profile_query(JAN, Network.FACEBOOK, TUNING, WORDS, CHARS).startswith(
        "site:facebook.com "
    )
    plain = profile_query(Identity(name="Jan"), Network.INSTAGRAM, TUNING, WORDS, CHARS)
    assert plain == 'site:instagram.com "Jan"'


def test_web_queries_cover_portfolio_github_publications_talks_and_exclude_socials():
    queries = web_queries(JAN, TUNING, WORDS, CHARS)
    assert len(queries) == 4
    assert "portfolio" in queries[0]
    assert "site:github.com" in queries[1] and '"python" "rust"' in queries[1]
    assert "publications" in queries[2]
    assert "talk" in queries[3]
    for query in (queries[0], queries[2], queries[3]):
        assert "-site:linkedin.com -site:facebook.com -site:instagram.com" in query
    assert all(len(query) <= CHARS and len(query.split()) <= WORDS for query in queries)


def test_queries_are_trimmed_to_the_provider_limits_by_dropping_the_least_important_parts():
    assert fit_query(["a", "b", "c"], 2, 100) == "a b"
    assert fit_query(["a", "bb", "ccc"], 10, 4) == "a bb"
    assert fit_query(["", "a", ""], 5, 100) == "a"
    assert fit_query(["only"], 0, 0) == "only"
    long_identity = Identity(
        name="Maria Josefina Anastasia Konstantina Papadopoulou",
        aliases=["Mary Papadopoulou", "Masha Papadopoulou"],
        location="Thessaloniki",
        employers=["Aristotle University of Thessaloniki"],
    )
    for words in (12, 20, 32):
        queries = web_queries(long_identity, TUNING, words, CHARS)
        assert all(len(query.split()) <= words for query in queries)
    assert len(web_queries(long_identity, TUNING, 12, CHARS)) == 4


def test_skill_queries():
    queries = skill_queries("Rust", "Brno")
    assert queries[Network.LINKEDIN] == 'site:linkedin.com/in "Rust" Brno'
    assert queries[Network.WEB] == 'site:github.com "Rust" Brno'


async def test_profile_hits_are_canonical_deduplicated_and_network_specific():
    search = FakeSearch(
        {
            "site:linkedin.com/in": [
                hit("https://cz.linkedin.com/in/Jan-Novak-1?trk=a", "Jan", "s1"),
                hit("https://www.linkedin.com/in/jan-novak-1", "dup"),
                hit("https://www.linkedin.com/company/acme", "company"),
                hit("https://www.instagram.com/jan/", "wrong network"),
                hit("https://example.com/in/jan", "web"),
            ]
        }
    )
    hits = await Discovery(search, TUNING).find_profiles(JAN, Network.LINKEDIN)
    assert [h.url for h in hits] == ["https://www.linkedin.com/in/jan-novak-1"]
    assert hits[0].title == "Jan"


async def test_queries_respect_the_limits_of_the_search_provider():
    search = FakeSearch({})
    search.max_query_words = 6
    await Discovery(search, TUNING).find_profiles(JAN, Network.LINKEDIN)
    assert all(len(query.split()) <= 6 for query in search.queries)


async def test_web_pages_exclude_socials_and_data_brokers_and_deduplicate():
    search = FakeSearch(
        {
            "portfolio": [
                hit("https://jan.dev/?utm_source=x", "Portfolio"),
                hit("https://www.linkedin.com/in/jan", "social"),
                hit("https://www.spokeo.com/Jan-Novak", "broker"),
            ],
            "site:github.com": [
                hit("https://jan.dev", "same page"),
                hit("https://github.com/jan", "gh"),
            ],
            "publications": [hit("https://papers.example/jan", "paper")],
        }
    )
    pages = await Discovery(search, TUNING).find_pages(JAN)
    assert [p.url for p in pages] == [
        "https://jan.dev/?utm_source=x",
        "https://github.com/jan",
        "https://papers.example/jan",
    ]
    assert len(search.queries) == 4


async def test_web_hits_per_query_are_capped_by_the_configuration():
    many = [hit(f"https://site{n}.example/jan", f"t{n}") for n in range(12)]
    pages = await Discovery(FakeSearch({"portfolio": many}), TUNING).find_pages(JAN)
    assert len(pages) == 5
    wider = await Discovery(
        FakeSearch({"portfolio": many}), DiscoveryTuning(hits_per_web_query=8)
    ).find_pages(JAN)
    assert len(wider) == 8


async def test_the_search_count_comes_from_the_configuration():
    counts = []

    class Counting(FakeSearch):
        async def search(self, query, count):
            counts.append(count)
            return []

    await Discovery(Counting({}), DiscoveryTuning(results_per_query=7)).find_pages(JAN)
    assert set(counts) == {7}


async def test_skill_candidates_interleave_linkedin_and_github_profiles():
    search = FakeSearch(
        {
            "site:linkedin.com/in": [
                hit("https://www.linkedin.com/in/a", "A"),
                hit("https://www.linkedin.com/in/b", "B"),
                hit("https://www.linkedin.com/in/c", "C"),
            ],
            "site:github.com": [
                hit("https://github.com/x", "X"),
                hit("https://github.com/x/repo", "repo"),
                hit("https://github.com/y", "Y"),
            ],
        }
    )
    both = [Network.LINKEDIN, Network.WEB]
    candidates = await Discovery(search, TUNING).find_skill_candidates("rust", "Brno", 4, both)
    assert [c.url for c in candidates] == [
        "https://www.linkedin.com/in/a",
        "https://github.com/x",
        "https://www.linkedin.com/in/b",
        "https://github.com/y",
    ]


async def test_skill_candidates_only_search_the_enabled_sources():
    search = FakeSearch(
        {
            "site:linkedin.com/in": [hit("https://www.linkedin.com/in/a", "A")],
            "site:github.com": [hit("https://github.com/x", "X")],
        }
    )
    discovery = Discovery(search, TUNING)
    only_web = await discovery.find_skill_candidates("rust", "Brno", 5, [Network.WEB])
    assert [c.url for c in only_web] == ["https://github.com/x"]
    assert all("linkedin" not in query for query in search.queries)
    only_linkedin = await discovery.find_skill_candidates("rust", "Brno", 5, [Network.LINKEDIN])
    assert [c.url for c in only_linkedin] == ["https://www.linkedin.com/in/a"]
