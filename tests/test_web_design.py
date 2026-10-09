# tests/test_web_design.py
from tests.web_helpers import (
    add_user,
    API_TOKEN,
    Browser,
    build_app,
    clarification,
    FakeVoice,
    open_client,
    ScriptedResearcher,
    start,
    wait_for,
)
from tests.fakes import (
    hiring_report,
    sceptical_report,
    skill_report,
)
from tests.conftest import isolate_environment
from agent.web.icons import TASK_ICONS
from pathlib import Path

import pytest
import re

WEB = Path(__file__).resolve().parent.parent / "src" / "agent" / "web"
CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
CHAT_CSS = (WEB / "static" / "chat.css").read_text(encoding="utf-8")
BUDDY_CSS = (WEB / "static" / "buddy.css").read_text(encoding="utf-8")
PAGES_CSS = (WEB / "static" / "pages.css").read_text(encoding="utf-8")
BACKDROP_CSS = (WEB / "static" / "backdrop.css").read_text(encoding="utf-8")
MESSAGES_CSS = (WEB / "static" / "messages.css").read_text(encoding="utf-8")
STYLES = CSS + CHAT_CSS + BUDDY_CSS + PAGES_CSS + BACKDROP_CSS + MESSAGES_CSS
JS = (WEB / "static" / "app.js").read_text(encoding="utf-8")
SCRIPTS = {path.name: path.read_text(encoding="utf-8") for path in (WEB / "static").glob("*.js")}
NATIVE_WIDGETS = re.compile(
    r"<(?:select|option|progress|meter|datalist|dialog)\b"
    r'|type="(?:range|date|datetime-local|time|month|week|color|number)"'
    r'|<input(?![^>]*class="sr")[^>]*type="file"',
    re.IGNORECASE,
)
DYNAMIC_CLASS = re.compile(r"p\d+|t\d|mix__[a-z-]+|s\d+|d\d|mark--[a-z]+")
FORM_ACTIONS = {"/login", "/settings", "/emails/templates"}
SETTINGS = {
    "theme": "system",
    "density": "comfortable",
    "live_view": "chat",
    "goal": "hiring",
    "source": ["web"],
    "skill_limit": "5",
    "strictness": "balanced",
    "scepticism": "standard",
    "show_api": "yes",
    "company_name": "Acme",
    "sender_name": "Eva Svobodova",
}
GREY = re.compile(r"#([0-9a-fA-F]{2})\1\1\b|#([0-9a-fA-F])\2\2\b")
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def gather(tmp_path):
    app, stub = build_app(
        tmp_path,
        ScriptedResearcher(),
        voice=FakeVoice(),
        api_token=API_TOKEN,
        max_jobs_per_session=9,
        submissions=50,
        chat_requests=500,
    )
    stub.results = [sceptical_report(), clarification(), RuntimeError("boom"), hiring_report()]
    pages = {}
    with open_client(app) as client:
        browser = Browser(client)
        pages["landing"] = client.get("/").text
        pages["missing"] = client.get("/missing").text
        token = browser.logged_in_token()
        browser.post("/settings", SETTINGS, token=token)
        locations = {}
        for name in ("done", "asking", "failed", "rich"):
            locations[name] = start(browser, token)
            wait_for(browser, locations[name])
        gate = stub.hold()
        running = start(browser, token)
        pages["running"] = client.get(running).text
        gate.set()
        stub.skill_result = skill_report()
        locations["skill"] = start(browser, token, "find Rust in Brno")
        wait_for(browser, locations["skill"])
        wait_for(browser, running)
        for name, location in locations.items():
            pages[name] = client.get(location).text
        job_id = locations["rich"].removeprefix("/jobs/")
        pages["emails"] = client.get(f"/emails?job={job_id}").text
        pages["editor"] = client.get(f"/emails?edit=report-team&job={job_id}").text
        for name, path in (("home", "/"), ("settings", "/settings"), ("api", "/api")):
            pages[name] = client.get(path).text
        thread = re.search(r'data-thread-row="([^"]+)"', pages["home"]).group(1)
        pages["chat"] = client.get(f"/c/{thread}").text
        pages["errors"] = browser.post("/settings", {**SETTINGS, "theme": "neon"}, token=token).text
        member = Browser(open_client(app))
        app.state.ctx.policy.update(email_sending_enabled=True, finance_enabled=True)
        app.state.ctx.finance.add_entry("2026-06-01", "revenue", "1200", note="Contract")
        routine = app.state.ctx.routines.create(
            "", "Rust", "Brno", purpose_confirmed=True, requirements=[]
        )
        eva = add_user(app, "eva")
        person = app.state.ctx.candidates.create("Eva Svobodova", employer="Acme")
        app.state.ctx.candidates.hire(person.id, "Analyst", contacts=[("email", "eva@example.com")])
        member.signed_in_token("eva")
        for name, path in (
            ("account", "/account"),
            ("twofa", "/account/2fa"),
            ("admin_users", "/admin/users"),
            ("admin_user", f"/admin/users/{eva.id}"),
            ("admin_roles", "/admin/roles"),
            ("admin_audit", "/admin/audit"),
            ("admin_security", "/admin/security"),
            ("admin_connections", "/admin/connections"),
            ("candidates", "/candidates"),
            ("candidate_new", "/candidates/new"),
            ("candidate", f"/candidates/{person.id}"),
            ("admin_data", "/admin/data"),
            ("admin_import", "/admin/import"),
            ("messages", "/messages"),
            ("announcements", "/announcements"),
            ("dashboard", "/dashboard"),
            ("finance", "/finance"),
            ("routines", "/routines"),
            ("routine", f"/routines/{routine.id}"),
            ("compose", "/emails/compose"),
            ("mail_log", "/emails/log"),
        ):
            pages[name] = member.client.get(path).text
    return pages


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    root = tmp_path_factory.mktemp("design")
    with pytest.MonkeyPatch.context() as patch:
        isolate_environment(patch, root)
        return gather(root)


def test_every_page_of_the_application_was_rendered(pages):
    assert set(pages) == {
        "landing",
        "missing",
        "running",
        "done",
        "asking",
        "failed",
        "rich",
        "skill",
        "emails",
        "editor",
        "home",
        "settings",
        "api",
        "chat",
        "errors",
        "account",
        "twofa",
        "admin_users",
        "admin_user",
        "admin_roles",
        "admin_audit",
        "admin_security",
        "admin_connections",
        "candidates",
        "candidate_new",
        "candidate",
        "admin_data",
        "admin_import",
        "messages",
        "announcements",
        "dashboard",
        "finance",
        "routines",
        "routine",
        "compose",
        "mail_log",
    }
    assert "Overall rating" in pages["done"] and "Scepticism review: high" in pages["done"]
    assert "This search needs your pick" in pages["asking"]
    assert "The search stopped" in pages["failed"]
    assert 'data-running="1"' in pages["running"]
    assert "Best match" in pages["skill"]
    assert "Page not found" in pages["missing"] and "Please check this" in pages["errors"]


def test_no_page_uses_a_native_browser_widget_or_tooltip(pages):
    for name, html in pages.items():
        assert not NATIVE_WIDGETS.search(html), name
        assert not re.search(r"\stitle=", html), name
        assert not re.search(r"\srequired[\s>=]", html), name


def test_forms_with_inputs_skip_the_native_validation_bubbles(pages):
    checked = 0
    for html in pages.values():
        for tag in re.findall(r"<form\b[^>]*>", html):
            action = re.search(r'action="([^"]*)"', tag)
            if action and action.group(1) in FORM_ACTIONS:
                assert "novalidate" in tag, tag
                checked += 1
    assert checked >= 4


def test_every_icon_in_use_exists_in_the_sprite(pages):
    for name, html in pages.items():
        symbols = set(re.findall(r'<symbol id="(i-[a-z]+)"', html))
        used = set(re.findall(r'<use href="#(i-[a-z]+)"', html))
        assert symbols and used <= symbols, (name, used - symbols)
    scripted = set(re.findall(r'(?:icon|glyph)\("([a-z]+)"\)', "".join(SCRIPTS.values())))
    symbols = set(re.findall(r'<symbol id="i-([a-z]+)"', pages["home"]))
    assert scripted <= symbols, scripted - symbols


def test_every_class_in_the_markup_has_a_style(pages):
    defined = set(re.findall(r"\.([A-Za-z_][\w-]*)", STYLES))
    used = set()
    for html in pages.values():
        for value in re.findall(r'class="([^"]*)"', html):
            used.update(value.split())
    assert sorted(used - defined) == []


def test_every_class_the_scripts_build_has_a_style():
    defined = set(re.findall(r"\.([A-Za-z_][\w-]*)", STYLES))
    built = set()
    for text in SCRIPTS.values():
        for value in re.findall(r'make\("[a-z0-9]+", `?"?([^",`$]+)', text):
            built.update(
                part for part in value.split() if re.fullmatch(r"[a-z]\w+(?:--?[a-z]+)*", part)
            )
    assert sorted(built - defined) == []


def test_no_style_rule_is_left_without_markup_or_script():
    sources = "".join(SCRIPTS.values())
    for path in [*WEB.rglob("*.html"), *WEB.rglob("*.py"), *WEB.rglob("*.svg")]:
        sources += path.read_text(encoding="utf-8")
    defined = set(re.findall(r"\.([A-Za-z_][\w-]*)", STYLES))

    def is_used(name):
        if (
            name in sources
            or DYNAMIC_CLASS.fullmatch(name)
            or name.removeprefix("t-") in TASK_ICONS
        ):
            return True
        return "--" in name and f"{name.split('--')[0]}--" in sources

    assert sorted(name for name in defined if not is_used(name)) == []


def test_the_landing_page_is_one_screen_with_the_sign_in_and_no_navigation(pages):
    landing = pages["landing"]
    assert 'class="solo landing"' in landing and "<h1" in landing
    assert landing.count("<h1") == 1 and "Know who you are hiring." in landing
    assert 'action="/login"' in landing and 'type="password"' in landing
    assert "landing__demo" in landing and "Example conversation" in landing
    for absent in ("data-shell", "data-find", "New chat", "Emails", 'href="/settings"', "Tie"):
        assert absent not in landing, absent
    assert "<noscript" not in landing and landing.count("<script") == 1


def test_the_chat_is_the_main_page_and_not_a_form(pages):
    home = pages["home"]
    assert "What are we looking into?" in home and 'class="shell"' in home
    assert home.count("<h1") == 1 and 'class="chat__title"' in home
    assert home.count("data-try") == 3 and "data-composer" in home and "data-scroll" in home
    fields = re.findall(r"<(?:input|textarea|select)\b[^>]*>", home)
    assert len(fields) <= 4, fields
    for absent in ('class="seg', "data-select", 'name="lawful"', "Switch theme", "Sign out"):
        assert absent not in home, absent
    assert "<h1>Research a person</h1>" not in home and "Find by skill" not in home
    assert re.search(r'<textarea name="text"[^>]*placeholder="Name someone', home)


def test_the_rail_holds_the_history_the_tools_and_tie(pages):
    home = pages["home"]
    rail = home.split('<aside class="rail"', 1)[1].split("</aside>", 1)[0]
    for expected in (
        'class="newchat"',
        "data-find",
        'placeholder="Search chats"',
        "data-threads",
        'href="/emails"',
        'href="/settings"',
        'href="/api"',
        "data-tie-open",
    ):
        assert expected in rail, expected
    assert rail.count("data-thread-row=") >= 6 and "data-delete-thread=" in rail
    assert rail.index("data-find") < rail.index("data-threads") < rail.index("data-tie-open")


def test_the_report_leads_with_the_verdict_and_explains_a_cap(pages):
    done = pages["done"]
    assert done.index('class="verdict"') < done.index('id="scepticism"')
    assert done.index('data-panel="overview"') < done.index('data-panel="findings"')
    assert done.index('data-panel="findings"') < done.index('data-panel="rating"')
    assert done.index('data-panel="rating"') < done.index('data-panel="drafts"')
    assert done.index('data-panel="drafts"') < done.index('data-panel="activity"')
    assert done.index('data-panel="activity"') < done.index('data-panel="sources"')
    assert 'class="scale"' in done and 'class="scale__cap"' in done and 'class="scale__raw"' in done
    assert "Capped from 4.4 by the scepticism check" in done
    assert "1 warning<" in done and "1 warnings" not in done
    assert 'class="meter"' in done and re.search(r'<i class="p\d+">', done)
    assert ">Markdown<" in done.replace("</svg>", "") or "Markdown</a>" in done
    assert "Delete" in done and 'role="tablist"' in done and done.count('role="tab"') == 6
    skill = pages["skill"]
    assert 'class="verdict verdict--plain"' in skill and 'id="ranking"' in skill
    assert skill.count('role="tab"') == 5
    assert "Best match" in skill and "With scepticism warnings" in skill
    rich = pages["rich"]
    assert (
        'class="mix"' in rich
        and "mix__supported" in rich
        and "2 supported, 2 single source, 1 uncertain" in rich
    )


def test_state_is_told_by_shape_and_word_never_by_colour_alone(pages):
    rich = pages["rich"]
    for shape in ("mark--full", "mark--half", "mark--none", "mark--dash"):
        assert shape in rich, shape
    assert re.search(r'<span class="tag"><i class="mark mark--full"[^>]*></i>Met</span>', rich)
    assert re.search(r'<span class="tag"><i class="mark mark--half"[^>]*></i>Partial</span>', rich)
    assert re.search(r'<span class="tag"><i class="mark mark--dash"[^>]*></i>Unknown</span>', rich)
    assert re.search(
        r'<span class="tag"><i class="mark mark--none"[^>]*></i>Uncertain</span>', rich
    )
    assert ".mark--cross" in STYLES and ".mark--half::after" in STYLES


def test_no_decorative_charts_stat_cards_or_filler_remain(pages):
    banned = (
        "kpi",
        "ring__",
        "donut",
        'class="line"',
        "data-count",
        "Hi there",
        "research desk",
        "Make the desk yours",
        "<h1>Welcome back</h1>",
        "Doubt",
    )
    for name, html in pages.items():
        for word in banned:
            assert word not in html, (name, word)


def test_the_pages_without_a_verdict_have_no_stat_strip(pages):
    for name in ("home", "settings", "emails", "api", "chat"):
        assert "verdict" not in pages[name], name
        assert 'class="subnav"' not in pages[name], name


def test_settings_are_sections_and_the_theme_lives_only_there(pages):
    settings = pages["settings"]
    for name in (
        "appearance",
        "research",
        "context",
        "matching",
        "tie",
        "data",
        "developer",
        "account",
    ):
        assert f'data-tab="{name}"' in settings and f'data-panel="{name}"' in settings, name
    assert 'name="theme"' in settings and 'name="show_api"' in settings
    for name in ("home", "chat", "emails", "api", "landing"):
        assert 'name="theme"' not in pages[name], name
    assert pages["home"].count('href="/settings"') == 1


def test_the_stylesheet_is_responsive_animated_and_themeable():
    for breakpoint in ("max-width: 1100px", "max-width: 860px"):
        assert f"@media ({breakpoint})" in STYLES
    assert "prefers-reduced-motion: reduce" in CSS and "@media print" in CSS
    assert "light-dark(" in CSS and ':root[data-theme="dark"] { color-scheme: dark; }' in CSS
    assert ':root[data-theme="system"] { color-scheme: light dark; }' in CSS
    assert STYLES.count("@keyframes") >= 15 and "animation:" in CSS
    assert "scrollbar-width" in CSS and "accent-color" not in STYLES and "!important" in CSS
    assert "100dvh" in CSS and "overflow: hidden" in CSS


def test_the_stylesheets_have_no_external_resources_or_inline_data():
    for sheet in (CSS, CHAT_CSS, BUDDY_CSS, PAGES_CSS, BACKDROP_CSS, MESSAGES_CSS):
        assert "url(" not in sheet and "@import" not in sheet and "http" not in sheet


def test_the_scripts_never_build_markup_from_strings():
    for name, text in SCRIPTS.items():
        for forbidden in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "eval(",
            "new Function",
            "document.write",
        ):
            assert forbidden not in text, (name, forbidden)
        assert text.splitlines()[1] == '"use strict";', name
    assert "textContent" in JS


def test_the_scripts_cover_voice_and_the_custom_controls():
    for feature in ("MediaRecorder", "/voice/transcribe", "X-CSRF-Token", "dataset.speak"):
        assert feature in JS
    for hook in ("[data-select]", "[data-tabs]", "[data-copy]", "[data-find]", "[data-speak]"):
        assert hook in JS


def test_every_job_state_shows_the_live_card_with_the_stream_address(pages):
    for name in ("running", "done", "asking", "failed", "rich", "skill"):
        assert 'data-live="/jobs/' in pages[name], name
    assert 'data-running="1"' in pages["running"] and 'data-running=""' in pages["done"]


def test_the_rich_report_shows_the_company_requirements_and_email_drafts(pages):
    rich = pages["rich"]
    assert 'id="company"' in rich and "Payments software" in rich
    assert 'id="requirements"' in rich and 'id="drafts"' in rich
    assert "data-draft=" in rich and "Copy all" in rich
    assert "Text only. Nothing is sent" in rich


def test_the_emails_pages_offer_categories_placeholders_and_copy_buttons(pages):
    emails = pages["emails"]
    for category in ("single", "batch", "team"):
        assert category in emails
    assert "data-select" in emails and "novalidate" in emails and 'class="trio"' in emails
    editor = pages["editor"]
    assert "report.overall" in editor and "candidate.name" in editor
    assert "Built-in templates are read only" in editor
    assert "data-copy-all" in editor or "data-preview" in editor


def test_the_palette_is_only_black_white_and_grey():
    for sheet in (CSS, CHAT_CSS, BUDDY_CSS, PAGES_CSS, BACKDROP_CSS, MESSAGES_CSS):
        for colour in HEX.findall(sheet):
            assert GREY.fullmatch(colour), colour
        assert not re.search(r"hsl\(|hwb\(|oklch\(|lab\(|lch\(|color\(", sheet)
        for value in re.findall(r"rgba?\(([^)]*)\)", sheet):
            parts = [part.strip() for part in value.split(",")]
            assert parts[0] == parts[1] == parts[2], value
    assert "--g:" not in CSS and "--r:" in CSS


def test_the_theme_is_soft_not_black_and_not_razor_edged():
    dark = re.search(r"--paper: light-dark\(#([0-9a-f]{6}), #([0-9a-f]{6})\)", CSS)
    assert dark and int(dark.group(2)[:2], 16) >= 0x20 and int(dark.group(1)[:2], 16) >= 0xF0
    rail = re.search(r"--rail: light-dark\(#([0-9a-f]{6}), #([0-9a-f]{6})\)", CSS)
    assert rail and int(rail.group(1)[:2], 16) >= 0xD0
    radii = {int(value) for value in re.findall(r"--r\d?: (\d+)px", CSS)}
    assert radii and min(radii) >= 4 and max(radii) <= 20
    assert not re.search(r"box-shadow: \d+px \d+px 0", STYLES)
    assert STYLES.count("border-radius: 0;") <= 3


def test_the_visual_language_is_restrained():
    for sheet in (CSS, CHAT_CSS, BUDDY_CSS, PAGES_CSS):
        assert "gradient" not in sheet
        assert "text-transform: uppercase" not in sheet
        assert "letter-spacing: 0.0" not in sheet
    assert "Manrope" not in CSS and "Plus Jakarta" not in CSS and "Inter" not in CSS
    assert "--display:" in CSS and "--mono:" in CSS and "Bahnschrift" in CSS
    assert "purple" not in CSS and "#8b5cf6" not in CSS and "#a98bff" not in CSS
    radii = set(re.findall(r"border-radius: (\d+px)", STYLES))
    assert len(radii) <= 8, radii
    assert STYLES.count("box-shadow") <= 40
    assert ".page > *" not in CSS and ".chart" not in CSS
    assert not re.search(r":hover[^}]*translate\(-?\d+px, -?\d+px\)", STYLES)


def test_the_animated_background_is_cheap_quiet_and_switchable(pages):
    assert 'class="backdrop"' in pages["landing"] and "backdrop." in pages["landing"]
    assert pages["landing"].count("<i></i>") == 3
    keyframes = re.findall(r"@keyframes drift-[abc] \{([^}]*\{[^}]*\})", BACKDROP_CSS)
    assert len(keyframes) == 3
    for body in keyframes:
        assert set(re.findall(r"([a-z-]+):", body)) == {"transform"}
    for needle in (
        "will-change: transform",
        "contain: strict",
        "pointer-events: none",
        "prefers-reduced-motion: reduce",
        "animation-play-state: paused",
        "@media print",
    ):
        assert needle in BACKDROP_CSS, needle
    assert "filter:" not in BACKDROP_CSS and "box-shadow" not in BACKDROP_CSS
    assert BACKDROP_CSS.count("<i>") == 0 and "rgba(0, 0, 0," in BACKDROP_CSS
    assert 'name="backdrop"' in pages["settings"] and "Animated background" in pages["settings"]


def test_turning_the_background_off_removes_it_from_every_page(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        assert 'class="backdrop"' in client.get("/").text
        token = browser.logged_in_token()
        saved = browser.post("/settings", {**SETTINGS, "backdrop": ""}, token=token)
        assert saved.status_code == 303
        for path in ("/", "/settings", "/account"):
            page = client.get(path).text
            assert 'class="backdrop"' not in page and "backdrop." not in page, path
        again = browser.post("/settings", {**SETTINGS, "backdrop": "yes"}, token=token)
        assert again.status_code == 303 and 'class="backdrop"' in client.get("/settings").text
