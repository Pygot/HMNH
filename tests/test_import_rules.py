# tests/test_import_rules.py
from tests.import_rules import (
    misplaced_globals,
    normalize_imports,
)
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "tests").rglob("*.py")])
SAMPLE = """# sample.py
import os
import asyncio
from a import b
from long.module import c
from m import (
    zeta,
    alpha,
)
import sys

X = 1
"""
EXPECTED = """# sample.py
from m import (
    alpha,
    zeta,
)
from long.module import c
from a import b

import asyncio
import sys
import os

X = 1
"""


def name_of(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_the_normaliser_orders_imports_as_required():
    assert normalize_imports(SAMPLE) == EXPECTED
    assert normalize_imports(EXPECTED) == EXPECTED


def test_files_without_imports_are_left_alone():
    assert normalize_imports("# x.py\nVALUE = 1\n") == "# x.py\nVALUE = 1\n"


def test_misplaced_globals_are_found():
    source = "import os\n\nA = 1\n\n\ndef f():\n    return A\n\n\nB = 2\n"
    assert misplaced_globals(source) == ["line 10: B"]
    assert misplaced_globals("import os\n\nA = 1\nB: int = 2\n\n\ndef f():\n    pass\n") == []


@pytest.mark.parametrize("path", FILES, ids=name_of)
def test_imports_follow_the_house_order(path):
    source = path.read_text(encoding="utf-8")
    assert normalize_imports(source) == source


@pytest.mark.parametrize("path", FILES, ids=name_of)
def test_global_variables_sit_directly_under_the_imports(path):
    assert misplaced_globals(path.read_text(encoding="utf-8")) == []
