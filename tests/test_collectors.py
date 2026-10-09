# tests/test_collectors.py
from agent.collectors import (
    build_collectors,
    item_to_text,
    ProfileCollector,
)
from agent.config import (
    CollectTuning,
    Settings,
)
from datetime import (
    datetime,
    UTC,
)
from agent.errors import SourceUnavailable
from agent.models import Network

import pytest

NOW = datetime(2026, 1, 1, tzinfo=UTC)
TUNING = CollectTuning()
LINKEDIN_ITEM = {
    "firstName": "Jane",
    "lastName": "Doe",
    "headline": "Staff Engineer at Acme",
    "about": "Builds data platforms.",
    "location": {"linkedinText": "Brno, Czechia", "countryCode": "CZ", "parsed": {"city": "Brno"}},
    "emails": ["jane@example.com"],
    "connectionsCount": 500,
    "openToWork": True,
    "moreProfiles": [{"firstName": "Someone", "lastName": "Else"}],
    "volunteering": [{"role": "Volunteer", "organization": "Some charity"}],
    "causes": ["Politics"],
    "experience": [
        {
            "position": "Staff Engineer",
            "companyName": "Acme",
            "companyLinkedinUrl": "https://www.linkedin.com/company/acme",
            "companyLogo": "https://img.example/logo.png",
            "startDate": {"year": 2019, "month": "Jan"},
            "endDate": {"text": "Present"},
            "description": "Led a team.",
        }
    ],
    "education": [
        {
            "schoolName": "Brno University",
            "degree": "MSc",
            "fieldOfStudy": "Computer Science",
            "schoolLinkedinUrl": "https://www.linkedin.com/school/x",
            "insights": "private note",
        }
    ],
    "skills": [{"name": "Python"}, {"name": "Rust"}],
}


class FakeRunner:
    def __init__(self, items):
        self.items = items
        self.calls = []

    async def run(self, actor_id, payload):
        self.calls.append((actor_id, payload))
        return self.items


def collector(network, items, builder=None):
    runner = FakeRunner(items)
    built = builder or (lambda url: {"url": url})
    return ProfileCollector(network, runner, "o/a", built, TUNING, now=lambda: NOW), runner


def test_item_to_text_keeps_only_professional_fields():
    item = {
        "fullName": "Jane Doe",
        "headline": "Staff Engineer at Acme",
        "email": "jane@example.com",
        "phone": "+420123456789",
        "address": "Main Street 1",
        "gender": "female",
        "followersCount": 12000,
        "relationshipStatus": "married",
        "profilePicUrl": "https://img.example/x.jpg",
        "latestPosts": [{"caption": "my holiday"}],
        "relatedProfiles": [{"full_name": "Someone Else"}],
        "unknownField": "ignored",
        "experience": [
            {
                "title": "Engineer",
                "companyName": "Acme",
                "startYear": 2019,
                "companyLogo": "https://img.example/logo.png",
                "email": "hr@acme.example",
                "current": True,
            }
        ],
        "skills": ["Python", "Rust"],
    }
    text = item_to_text(item, TUNING)
    assert "fullName: Jane Doe" in text
    assert "headline: Staff Engineer at Acme" in text
    assert "title: Engineer; companyName: Acme; startYear: 2019" in text
    assert "skills: Python | Rust" in text
    for forbidden in (
        "example.com",
        "+420",
        "Main Street",
        "female",
        "12000",
        "married",
        "holiday",
        "Someone",
        "unknownField",
        "logo",
    ):
        assert forbidden not in text


def test_a_realistic_linkedin_item_keeps_work_history_and_drops_everything_else():
    text = item_to_text(LINKEDIN_ITEM, TUNING)
    assert "headline: Staff Engineer at Acme" in text
    assert "about: Builds data platforms." in text
    assert "position: Staff Engineer; companyName: Acme" in text
    assert "schoolName: Brno University; degree: MSc; fieldOfStudy: Computer Science" in text
    assert "skills: name: Python | name: Rust" in text
    assert "linkedinText: Brno, Czechia" in text
    for forbidden in (
        "example.com",
        "500",
        "Someone",
        "charity",
        "Politics",
        "linkedin.com/company",
        "linkedin.com/school",
        "private note",
        "True",
        "emails",
    ):
        assert forbidden not in text


def test_item_to_text_respects_the_configured_limits():
    assert len(item_to_text({"summary": "x" * 100_000}, TUNING)) <= TUNING.max_chars
    small = CollectTuning(max_chars=1000)
    assert len(item_to_text({"summary": "x" * 5000}, small)) == 1000
    deep = {"experience": [{"title": {"a": {"b": {"c": {"d": {"e": "buried"}}}}}}]}
    assert "buried" not in item_to_text(deep, CollectTuning(max_depth=2))
    assert "buried" in item_to_text(deep, CollectTuning(max_depth=8))


async def test_title_comes_from_first_and_last_name_for_linkedin_items():
    instance, _ = collector(Network.LINKEDIN, [LINKEDIN_ITEM])
    document = await instance.collect("https://www.linkedin.com/in/jane")
    assert document.title == "Jane Doe"


async def test_title_falls_back_to_the_requested_url():
    instance, _ = collector(Network.FACEBOOK, [{"intro": "Consultant"}])
    document = await instance.collect("https://www.facebook.com/jane")
    assert document.title == "https://www.facebook.com/jane"


async def test_collect_returns_a_source_document():
    instance, runner = collector(Network.INSTAGRAM, [{"fullName": "Jane", "biography": "Engineer"}])
    document = await instance.collect("https://www.instagram.com/jane/")
    assert document.url == "https://www.instagram.com/jane/"
    assert document.network is Network.INSTAGRAM
    assert document.title == "Jane"
    assert "biography: Engineer" in document.text
    assert document.retrieved_at == NOW
    assert runner.calls == [("o/a", {"url": "https://www.instagram.com/jane/"})]


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ([], "returned no data"),
        ([{"private": True, "fullName": "Jane"}], "private"),
        ([{"isPrivate": True}], "private"),
        ([{"errorMessage": "Profile not found"}], "Profile not found"),
        ([{"scrape_error": "blocked", "fullName": "x"}], "blocked"),
        ([{"followersCount": 5}], "no public professional content"),
    ],
)
async def test_collect_reports_unusable_profiles(items, message):
    instance, _ = collector(Network.LINKEDIN, items)
    with pytest.raises(SourceUnavailable, match=message):
        await instance.collect("https://www.linkedin.com/in/jane")


def test_build_collectors_uses_every_default_actor():
    collectors = build_collectors(Settings(_env_file=None), FakeRunner([]))
    assert set(collectors) == {Network.LINKEDIN, Network.FACEBOOK, Network.INSTAGRAM}


async def test_inputs_match_each_actor_contract():
    settings = Settings(_env_file=None)
    runner = FakeRunner([{"fullName": "Jane"}])
    collectors = build_collectors(settings, runner)
    await collectors[Network.LINKEDIN].collect("https://www.linkedin.com/in/jane")
    await collectors[Network.FACEBOOK].collect("https://www.facebook.com/jane")
    await collectors[Network.INSTAGRAM].collect("https://www.instagram.com/jane/")
    assert runner.calls == [
        (
            "harvestapi/linkedin-profile-scraper",
            {
                "profileScraperMode": "Profile details no email ($4 per 1k)",
                "urls": ["https://www.linkedin.com/in/jane"],
            },
        ),
        ("apify/facebook-pages-scraper", {"startUrls": [{"url": "https://www.facebook.com/jane"}]}),
        ("apify/instagram-profile-scraper", {"usernames": ["jane"]}),
    ]


async def test_the_linkedin_actor_and_its_input_are_configurable():
    settings = Settings(
        _env_file=None,
        apify_linkedin_actor="vendor/li",
        apify_linkedin_url_field="profileUrls",
        apify_linkedin_input={"mode": "basic"},
    )
    runner = FakeRunner([{"fullName": "Jane"}])
    await build_collectors(settings, runner)[Network.LINKEDIN].collect(
        "https://www.linkedin.com/in/jane"
    )
    assert runner.calls == [
        ("vendor/li", {"mode": "basic", "profileUrls": ["https://www.linkedin.com/in/jane"]})
    ]


def test_actors_without_a_configured_id_are_left_out():
    settings = Settings(_env_file=None, apify_linkedin_actor=None)
    collectors = build_collectors(settings, FakeRunner([]))
    assert Network.LINKEDIN not in collectors
    assert Network.FACEBOOK in collectors
