# tests/test_assets.py
from agent.web.assets import (
    build_assets,
    minify_css,
    minify_js,
)
from tests.web_helpers import (
    build_app,
    open_client,
    TOKEN,
)
from agent.web.settings import WebSettings
from agent.web.app import STATIC_DIR

import pytest
import gzip
import re

WEB = WebSettings(_env_file=None, access_token=TOKEN)


def test_css_is_collapsed_without_changing_its_meaning():
    source = (
        "/* path */\na , b {\n  color: red;\n  margin: 0 auto;\n}\n"
        "@media (max-width: 9px) {\n  a > b { top: calc(1px + 2px); }\n}\n"
    )
    assert minify_css(source) == (
        "a,b{color:red;margin:0 auto}@media (max-width:9px){a>b{top:calc(1px + 2px)}}"
    )


def test_javascript_keeps_its_line_breaks_but_loses_indentation_and_the_path_line():
    source = "// path\nconst a = 1;\n\n  if (a) {\n    run();\n  }\n"
    assert minify_js(source) == "const a = 1;\nif (a) {\nrun();\n}"


def test_every_shipped_asset_is_smaller_after_minification():
    served, urls = build_assets(STATIC_DIR, WEB)
    assert set(urls) == {
        "app.css",
        "chat.css",
        "buddy.css",
        "backdrop.css",
        "pages.css",
        "messages.css",
        "messages.js",
        "e2ee.js",
        "app.js",
        "chat.js",
        "live.js",
        "emails.js",
        "buddy.js",
        "logo.svg",
        "tie.svg",
    }
    for name in urls:
        raw = (STATIC_DIR / name).read_bytes()
        asset = served[name]
        assert len(asset.body) < len(raw)
        assert len(asset.packed) < len(asset.body)
        assert gzip.decompress(asset.packed) == asset.body
        assert b"\n\n" not in asset.body and b"/*" not in asset.body


def test_assets_are_served_by_name_and_by_content_hash():
    served, urls = build_assets(STATIC_DIR, WEB)
    hashed = urls["app.css"].removeprefix("/static/")
    assert re.fullmatch(r"app\.[0-9a-f]{10}\.css", hashed)
    assert served[hashed].body == served["app.css"].body
    assert served["app.css"].cache_control == "public, max-age=3600"
    assert served[hashed].cache_control == "public, max-age=31536000, immutable"
    assert served[hashed].etag == hashed.split(".")[1]
    assert served["app.css"].media_type.startswith("text/css")
    assert served["app.js"].media_type.startswith("text/javascript")


def test_cache_lifetimes_and_digest_length_are_configurable():
    web = WebSettings(
        _env_file=None,
        access_token=TOKEN,
        static_max_age_seconds=60,
        static_hashed_max_age_seconds=120,
        asset_digest_chars=6,
    )
    served, urls = build_assets(STATIC_DIR, web)
    assert re.fullmatch(r"/static/app\.[0-9a-f]{6}\.js", urls["app.js"])
    assert served["app.js"].cache_control == "public, max-age=60"
    assert served[urls["app.js"].removeprefix("/static/")].cache_control.endswith("120, immutable")


def test_other_files_in_the_folder_are_ignored(tmp_path):
    (tmp_path / "app.css").write_text("/* x */ a { color: red; }", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "data.json").write_text("{}", encoding="utf-8")
    served, urls = build_assets(tmp_path, WEB)
    assert set(urls) == {"app.css"}
    assert all(not name.startswith(("notes", "data")) for name in served)


def test_the_page_links_the_hashed_files_and_nothing_else(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        page = client.get("/").text
    assert re.search(r'<link rel="stylesheet" href="/static/app\.[0-9a-f]{10}\.css">', page)
    assert re.search(
        r'<script src="/static/app\.[0-9a-f]{10}\.js" nonce="[^"]+" defer></script>', page
    )
    assert re.search(r'<link rel="stylesheet" href="/static/backdrop\.[0-9a-f]{10}\.css">', page)
    assert page.count("<script") == 1 and page.count("stylesheet") == 2


def test_the_hashed_asset_is_immutable_and_compressed_on_request(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        url = re.search(r'href="(/static/app\.[0-9a-f]+\.css)"', client.get("/").text).group(1)
        plain = client.get(url, headers={"accept-encoding": "identity"})
        packed = client.get(url, headers={"accept-encoding": "gzip"})
    assert plain.status_code == 200 and "content-encoding" not in plain.headers
    assert plain.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert plain.headers["vary"] == "Accept-Encoding"
    assert plain.headers["x-content-type-options"] == "nosniff"
    assert packed.headers["content-encoding"] == "gzip"
    assert packed.text == plain.text
    assert len(plain.content) < 40_000


def test_etags_allow_a_cheap_revalidation(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        first = client.get("/static/app.js")
        again = client.get("/static/app.js", headers={"if-none-match": first.headers["etag"]})
        stale = client.get("/static/app.js", headers={"if-none-match": '"other"'})
    assert first.headers["etag"].startswith('"') and first.status_code == 200
    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == first.headers["etag"]
    assert stale.status_code == 200


@pytest.mark.parametrize("name", ["app.exe", "app.0000000000.css", "../app.css", "app.js.map"])
def test_unknown_assets_are_not_found(tmp_path, name):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        assert client.get(f"/static/{name}").status_code == 404
