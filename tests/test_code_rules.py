# tests/test_code_rules.py
from pathlib import Path

import unicodedata
import tokenize
import pytest
import io
import re

ROOT = Path(__file__).resolve().parent.parent
SUFFIXES = {".py", ".html", ".css", ".js", ".toml", ".md", ".svg"}
NAMES = {".gitignore", ".env.example"}
EXCLUDED = {".venv", ".idea", ".git", ".pytest_cache", ".ruff_cache", "__pycache__", "outputs"}
FORBIDDEN_CATEGORIES = {"Pd", "Pi", "Pf", "Sm", "So", "Sc", "Sk", "Zs", "Cf", "Po"}
EXTRA_ALLOWED = {" "}
ALLOWED_CONTROLS = {"\n", "\t"}


def project_files() -> list[Path]:
    found = []
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED for part in relative.parts) or not path.is_file():
            continue
        if path.suffix in SUFFIXES or path.name in NAMES:
            found.append(path)
    return found


def relative_name(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_the_project_files_were_found():
    names = {relative_name(path) for path in project_files()}
    assert "src/agent/pipeline.py" in names
    assert "src/agent/web/templates/job.html" in names
    assert "src/agent/web/static/app.css" in names
    assert "src/agent/web/static/app.js" in names
    assert "src/agent/web/static/logo.svg" in names
    assert ".env.example" in names
    assert "pyproject.toml" in names
    assert "README.md" in names


@pytest.mark.parametrize("path", project_files(), ids=relative_name)
def test_first_line_is_the_path_comment_and_nothing_else_is_commented(path):
    name = relative_name(path)
    text = path.read_text(encoding="utf-8")
    first_line = text.splitlines()[0] if text else ""
    if path.suffix == ".py":
        assert first_line == f"# {name}"
        comments = [
            token
            for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type == tokenize.COMMENT
        ]
        found = [(token.start[0], token.string) for token in comments]
        assert found[0] == (1, f"# {name}")
        if not name.startswith("src/"):
            assert found == [(1, f"# {name}")]
    elif path.suffix == ".html":
        assert first_line == f"{{# {name} #}}"
        assert text.count("{#") == 1
        assert "<!--" not in text
        for script in re.findall(r"<script\b[^>]*>(.*?)</script>", text, re.DOTALL):
            assert not re.search(r"(?<![:\"'])//|/\*", script)
    elif path.suffix == ".css":
        assert first_line == f"/* {name} */"
        assert text.count("/*") == 1
    elif path.suffix == ".js":
        assert first_line == f"// {name}"
        body = "\n".join(text.splitlines()[1:])
        assert "/*" not in body
        assert not re.search(r"(?<![:\"'`\\])//", body)
    elif path.suffix in {".md", ".svg"}:
        assert first_line == f"<!-- {name} -->"
        assert text.count("<!--") == 1
    else:
        assert first_line == f"# {name}"
        assert [line for line in text.splitlines() if line.lstrip().startswith("#")] == [first_line]


@pytest.mark.parametrize("path", project_files(), ids=relative_name)
def test_text_avoids_dashes_and_special_symbols(path):
    offenders = sorted(
        {
            f"U+{ord(ch):04X}"
            for ch in path.read_text(encoding="utf-8")
            if ord(ch) > 127
            and ch not in EXTRA_ALLOWED
            and unicodedata.category(ch) in FORBIDDEN_CATEGORIES
        }
    )
    assert offenders == []


@pytest.mark.parametrize("path", project_files(), ids=relative_name)
def test_files_have_no_stray_control_characters(path):
    text = path.read_text(encoding="utf-8")
    offenders = sorted(
        {
            f"U+{ord(ch):04X}"
            for ch in text
            if unicodedata.category(ch) == "Cc" and ch not in ALLOWED_CONTROLS
        }
    )
    assert offenders == []
