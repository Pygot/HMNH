# tests/test_urls.py
from agent.urls import (
    github_profile_url,
    instagram_username,
    network_of,
    normalize_web_url,
    profile_slug,
    profile_url,
    registrable_domain,
    source_key,
)
from agent.models import Network

import pytest


@pytest.mark.parametrize(
    ("url", "network"),
    [
        ("https://www.linkedin.com/in/jane", Network.LINKEDIN),
        ("https://cz.linkedin.com/in/jane", Network.LINKEDIN),
        ("https://lnkd.in/abc", Network.LINKEDIN),
        ("https://m.facebook.com/jane", Network.FACEBOOK),
        ("https://fb.me/jane", Network.FACEBOOK),
        ("https://instagram.com/jane", Network.INSTAGRAM),
        ("https://notlinkedin.com/in/jane", Network.WEB),
        ("https://example.com/linkedin.com", Network.WEB),
    ],
)
def test_network_detection(url, network):
    assert network_of(url) is network


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://cz.linkedin.com/in/Jane-Doe-1a2b3c/?trk=x",
            "https://www.linkedin.com/in/jane-doe-1a2b3c",
        ),
        ("https://www.linkedin.com/company/acme", None),
        ("https://www.linkedin.com/in", None),
        ("https://www.facebook.com/jane.doe", "https://www.facebook.com/jane.doe"),
        (
            "https://www.facebook.com/profile.php?id=1000&x=1",
            "https://www.facebook.com/profile.php?id=1000",
        ),
        ("https://www.facebook.com/profile.php?id=abc", None),
        (
            "https://www.facebook.com/people/Jane-Doe/1000/",
            "https://www.facebook.com/people/Jane-Doe/1000",
        ),
        ("https://www.facebook.com/people/Jane-Doe", None),
        ("https://www.facebook.com/groups/123", None),
        ("https://www.facebook.com/", None),
        ("https://www.instagram.com/Jane.Doe/?hl=en", "https://www.instagram.com/jane.doe/"),
        ("https://www.instagram.com/p/ABC123/", None),
        ("https://www.instagram.com/explore/tags/x", None),
        ("https://example.com/in/jane", None),
        ("ftp://www.linkedin.com/in/jane", None),
    ],
)
def test_profile_url(url, expected):
    assert profile_url(url) == expected


def test_instagram_username_and_slug():
    assert instagram_username("https://www.instagram.com/jane.doe/") == "jane.doe"
    assert profile_slug("https://www.linkedin.com/in/jane-doe") == "jane-doe"
    assert profile_slug("https://www.facebook.com/people/Jane-Doe/1000") == "Jane-Doe"
    assert profile_slug("https://www.facebook.com/jane") == "jane"
    assert profile_slug("https://example.com/jane") is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/octocat", "https://github.com/octocat"),
        ("https://github.com/octocat/hello", None),
        ("https://github.com/topics", None),
        ("https://gitlab.com/octocat", None),
        ("ftp://github.com/octocat", None),
    ],
)
def test_github_profile_url(url, expected):
    assert github_profile_url(url) == expected


def test_normalize_web_url_strips_tracking_and_credentials():
    url = "HTTPS://user:pw@Example.com/Path/?utm_source=x&fbclid=1&keep=2#frag"
    assert normalize_web_url(url) == "https://example.com/Path?keep=2"


@pytest.mark.parametrize(
    ("url", "domain"),
    [
        ("https://www.example.com/a", "example.com"),
        ("https://blog.example.co.uk/a", "example.co.uk"),
        ("https://example.com", "example.com"),
    ],
)
def test_registrable_domain(url, domain):
    assert registrable_domain(url) == domain


def test_source_key_distinguishes_networks_and_domains():
    assert source_key("https://www.linkedin.com/in/x") == "linkedin"
    assert source_key("https://github.com/x") == source_key("https://gist.github.com/x")
    assert source_key("https://github.com/x") != source_key("https://jane.dev")
