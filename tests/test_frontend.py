# tests/test_frontend.py
from tests.web_helpers import (
    Browser,
    build_app,
    open_client,
    say,
    ScriptedResearcher,
    start,
    wait_for,
)
from agent.web.icons import (
    FALLBACK,
    task_icon,
    task_icon_set,
    TASK_ICONS,
)
from agent.web.app import (
    STATIC_DIR,
    TEMPLATE_DIR,
)
from agent.web.logo import logo_markup
from agent.events import Task
from markupsafe import Markup

import xml.etree.ElementTree as ET
import pytest
import re

SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"
SCRIPT_FILES = sorted(STATIC_DIR.glob("*.js"))
STYLE_FILES = sorted(STATIC_DIR.glob("*.css"))
MARKUP_FILES = [*sorted(TEMPLATE_DIR.glob("*.html")), STATIC_DIR / "logo.svg"]
EXTERNAL = re.compile(r"""(?:src|href|action|data|poster|srcset)\s*=\s*["']\s*(?:https?:)?//""")


@pytest.mark.parametrize("path", STYLE_FILES, ids=lambda path: path.name)
def test_the_stylesheets_import_nothing(path):
    text = path.read_text(encoding="utf-8")
    assert "@import" not in text
    assert "url(" not in text
    assert "@font-face" not in text
    assert "image-set(" not in text


@pytest.mark.parametrize("path", SCRIPT_FILES, ids=lambda path: path.name)
def test_scripts_import_nothing(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|export)\b", text, re.MULTILINE)
    assert not re.search(r"\bimport\s*\(", text)
    assert not re.search(r"\bimport\.meta\b", text)
    assert "require(" not in text
    assert "importScripts" not in text
    assert "new Worker(" not in text and "new SharedWorker(" not in text
    assert not re.search(r"""\.src\s*=\s*["'`]""", text)


@pytest.mark.parametrize("path", SCRIPT_FILES, ids=lambda path: path.name)
def test_scripts_only_talk_to_their_own_server(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"""(?:fetch|EventSource|open)\(\s*["'`]https?:""", text)
    assert "WebSocket" not in text and "sendBeacon" not in text
    assert "document.write" not in text and "eval(" not in text
    assert "innerHTML" not in text


@pytest.mark.parametrize("path", MARKUP_FILES, ids=lambda path: path.name)
def test_templates_and_the_logo_load_nothing_external(path):
    text = path.read_text(encoding="utf-8")
    assert not EXTERNAL.search(text)
    assert "@import" not in text
    assert not re.search(r"<(iframe|object|embed|img|image|video|audio|source)\b", text)
    assert not re.search(r"\son[a-z]+\s*=", text)
    assert not re.search(r"\sstyle\s*=", text)


@pytest.mark.parametrize("path", MARKUP_FILES, ids=lambda path: path.name)
def test_scripts_and_stylesheets_in_templates_come_from_the_asset_table(path):
    text = path.read_text(encoding="utf-8")
    for tag in re.findall(r"<(?:script|link)\b[^>]*>", text):
        assert "asset(" in tag or "application/json" in tag, tag


def test_pages_reference_only_files_of_this_server(tmp_path):
    app, _ = build_app(tmp_path, ScriptedResearcher())
    with open_client(app) as client:
        anonymous = client.get("/").text
        browser = Browser(client)
        token = browser.logged_in_token()
        thread = say(browser, token, "Jan Novak").json()["thread"]["id"]
        pages = [client.get(path).text for path in ("/", "/emails", "/settings", f"/c/{thread}")]
    for page in [anonymous, *pages]:
        assert not EXTERNAL.search(page)
        for reference in re.findall(r'(?:src|href)="([^"]+)"', page):
            assert reference.startswith(("/", "#")), reference


def test_the_logo_is_one_animated_source_file():
    text = (STATIC_DIR / "logo.svg").read_text(encoding="utf-8")
    assert text.splitlines()[0] == "<!-- src/agent/web/static/logo.svg -->"
    assert text.count("<!--") == 1
    root = ET.fromstring(text)
    assert root.tag == f"{SVG_NAMESPACE}svg" and root.get("viewBox")
    paths = list(root.iter(f"{SVG_NAMESPACE}path"))
    assert len(paths) >= 3
    assert all(path.get("pathLength") == "1" for path in paths)
    assert all(path.get("class") for path in paths)
    assert not list(root.iter(f"{SVG_NAMESPACE}script"))
    assert not list(root.iter(f"{SVG_NAMESPACE}style"))
    assert not list(root.iter(f"{SVG_NAMESPACE}image"))


def test_every_logo_stroke_has_its_animation_in_the_stylesheet():
    root = ET.fromstring((STATIC_DIR / "logo.svg").read_text(encoding="utf-8"))
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    classes = {path.get("class") for path in root.iter(f"{SVG_NAMESPACE}path")}
    assert "stroke-dashoffset" in css
    for name in classes:
        assert f".logo .{name}" in css or f".{name}" in css, name


def test_the_inline_logo_has_no_comment_and_takes_a_class():
    markup = logo_markup()
    assert isinstance(markup, Markup)
    assert markup.startswith('<svg class="logo" aria-hidden="true" ')
    assert "<!--" not in markup and "\n" not in markup
    assert markup.count("<svg") == 1 and markup.endswith("</svg>")
    assert logo_markup("big").startswith('<svg class="big" ')
    ET.fromstring(markup)


def test_the_pages_show_the_logo_and_the_served_icon_is_the_same_drawing(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        page = client.get("/").text
        icon_url = re.search(r'<link rel="icon" type="image/svg\+xml" href="([^"]+)"', page).group(
            1
        )
        icon = client.get(icon_url)
    assert '<svg class="logo logo--hero"' in page and "HMNH" in page
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/svg+xml"
    assert "currentColor" not in icon.text and "#808080" in icon.text
    assert "<!--" not in icon.text
    assert icon.text.count("<path") == logo_markup().count("<path")


def test_every_task_has_its_own_icon():
    assert set(TASK_ICONS) == {task.value for task in Task}
    assert len(set(TASK_ICONS.values())) == len(TASK_ICONS)


@pytest.mark.parametrize("task", list(Task), ids=lambda task: task.value)
def test_each_task_icon_is_valid_svg_and_animated_by_its_own_css(task):
    markup = task_icon(task.value)
    root = ET.fromstring(markup)
    assert root.tag == "svg"
    assert root.get("viewBox") == "0 0 24 24" and root.get("aria-hidden") == "true"
    assert f"t-{task.value}" in root.get("class").split()
    assert "<script" not in markup and "style=" not in markup and " on" not in markup
    css = "".join(path.read_text(encoding="utf-8") for path in STYLE_FILES)
    assert f".t-{task.value}" in css


def test_unknown_tasks_fall_back_to_the_search_icon():
    for name in (None, "", "nonsense"):
        markup = task_icon(name)
        assert f"t-{FALLBACK}" in markup and TASK_ICONS[FALLBACK] in markup


def test_icons_take_extra_classes_and_are_safe_markup():
    markup = task_icon("rate", "task--lg")
    assert isinstance(markup, Markup)
    assert 'class="task t-rate task--lg"' in markup
    assert 'class="task t-rate"' in task_icon("rate")


def test_the_icon_set_template_holds_one_icon_per_task():
    markup = task_icon_set()
    assert markup.startswith("<template data-icons>") and markup.endswith("</template>")
    found = re.findall(r'<svg class="task t-[a-z]+" data-task="([a-z]+)"', markup)
    assert found == [task.value for task in Task]
    assert markup.count("<svg") == markup.count("</svg>") == len(Task)


def test_the_live_page_ships_the_icon_set_for_the_scripts(tmp_path):
    app, _ = build_app(tmp_path, ScriptedResearcher())
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        page = client.get(location).text
    assert page.count("<template data-icons>") == 1
    for task in Task:
        assert f'data-task="{task.value}"' in page


def test_the_globe_turns_with_two_meridians_a_quarter_turn_apart():
    markup = task_icon("web")
    assert markup.count("<ellipse") == 2
    css = (STATIC_DIR / "chat.css").read_text(encoding="utf-8")
    assert ".t-web .a, .t-web .b { animation: turn 3.6s linear infinite; }" in css
    assert ".t-web .b { animation-delay: -0.9s; }" in css
