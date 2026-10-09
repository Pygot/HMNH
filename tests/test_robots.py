# tests/test_robots.py
from tests.web_helpers import (
    API_TOKEN,
    bearer,
    Browser,
    build_app,
    open_client,
)

import re

TAG = "noindex, nofollow, noarchive, nosnippet, noimageindex"


def test_every_kind_of_response_asks_search_engines_to_stay_away(tmp_path):
    app, _ = build_app(tmp_path, api_token=API_TOKEN)
    with open_client(app) as client:
        browser = Browser(client)
        before = [
            client.get("/"),
            client.get("/robots.txt"),
            client.get("/missing"),
            client.get("/login/verify"),
            client.get("/setup?code=wrong"),
            client.get("/api/v1/status"),
            client.get("/api/v1/status", headers=bearer()),
            client.get("/api/v1/openapi.json", headers=bearer()),
        ]
        asset = re.search(r'href="(/static/app\.[0-9a-f]+\.css)"', client.get("/").text).group(1)
        browser.logged_in_token()
        after = [
            client.get("/"),
            client.get("/settings"),
            client.get("/account"),
            client.get("/admin/users"),
            client.get("/admin/data/export.json?datasets=users"),
            client.get("/candidates"),
            client.get("/messages"),
            client.get("/dashboard"),
            client.get("/jobs/none/events"),
            client.get(asset),
        ]
    for response in [*before, *after]:
        assert response.headers["x-robots-tag"] == TAG, response.url


def test_the_robots_file_disallows_everything_and_the_pages_say_so_too(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        robots = client.get("/robots.txt")
        landing = client.get("/").text
    assert robots.status_code == 200 and robots.headers["content-type"].startswith("text/plain")
    assert robots.text == "User-agent: *\nDisallow: /\n"
    assert '<meta name="robots" content="noindex, nofollow, noarchive">' in landing
