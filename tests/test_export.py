# tests/test_export.py
from agent.models import (
    Category,
    Network,
    PersonReport,
    ResearchOptions,
    Scepticism,
    ScepticismFlag,
    ScepticismLevel,
    ScepticismMode,
    Severity,
    SkillSearchReport,
)
from agent.export import (
    delete_exported,
    delete_outputs,
    export_report,
    md_text,
    md_url,
    OUTPUT_NAME,
    render_json,
    render_markdown,
)
from tests.fakes import (
    person_report,
    skill_report,
)

import pytest
import json
import stat
import os
import re


def test_person_markdown_has_every_required_section_and_a_url_per_claim():
    text = render_markdown(person_report())
    for heading in (
        "# Research report: Jan Novak",
        "## Rating",
        "## Findings",
        "### Employment",
        "## Source summaries",
        "## Sources",
        "## Limitations",
    ):
        assert heading in text
    assert "Goal: hiring" in text
    assert "Overall: **3.4 / 5**" in text
    assert "| skill fit | 3 / 5 |" in text
    assert (
        "- **single-source** Senior engineer at Acme (2025) - 1 independent source(s): <https://jan.dev/about>"
        in text
    )
    assert "- No Instagram profile was found in the search results." in text
    assert "GDPR" in text
    assert "Lawful purpose confirmed" in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_every_finding_and_claim_line_carries_a_source_url():
    text = render_markdown(person_report())
    claim_lines = [
        line for line in text.splitlines() if line.startswith("- **") or "(employment," in line
    ]
    assert claim_lines
    assert all("<https://" in line for line in claim_lines)


def test_empty_report_sections_are_explicit():
    report = person_report().model_copy(
        update={"findings": [], "sources": [], "source_summaries": []}
    )
    text = render_markdown(report)
    assert "No findings could be confirmed." in text
    assert "No sources were read." in text


def test_markdown_is_escaped_against_injection():
    report = person_report()
    hostile = "[click](https://evil.example) <script>alert(1)</script> | `x` *y* _z_"
    finding = report.findings[0].model_copy(update={"statement": hostile})
    text = render_markdown(report.model_copy(update={"findings": [finding]}))
    line = next(line for line in text.splitlines() if "click" in line)
    assert "[click]" not in line.replace("\\[click\\]", "")
    assert "<script>" not in line
    assert "\\|" in line and "\\`x\\`" in line and "\\*y\\*" in line


def test_md_helpers():
    assert md_text("a|b_c*d[e]<f>&g~h\\") == "a\\|b\\_c\\*d\\[e\\]\\<f\\>\\&g\\~h\\\\"
    assert md_url("https://x.example/a>b<c") == "<https://x.example/a%3Eb%3Cc>"


def test_json_round_trips_and_contains_sources_and_limitations():
    report = person_report()
    data = json.loads(render_json(report))
    assert data["mode"] == "person"
    assert data["limitations"] == report.limitations
    assert data["sources"][0]["url"] == "https://jan.dev/about"
    assert data["findings"][0]["tag"] == "single-source"
    assert PersonReport.model_validate_json(render_json(report)) == report


def test_json_does_not_contain_raw_source_text():
    assert '"text"' not in render_json(person_report())


def test_skill_search_markdown_marks_the_best_candidate():
    text = render_markdown(skill_report())
    assert "# Candidate ranking: Rust in Brno" in text
    assert "| 1 | Jan Novak (best) | 3.4 / 5 | none | <https://github.com/jan> |" in text
    assert "### 1. Jan Novak (best match)" in text
    assert "<https://github.com/jan>" in text
    assert SkillSearchReport.model_validate_json(render_json(skill_report())) == skill_report()


def test_skill_search_with_no_candidates_says_so():
    report = skill_report().model_copy(update={"candidates": []})
    assert "No candidates could be assessed" in render_markdown(report)


def test_all_finding_categories_render_under_their_own_heading():
    report = person_report()
    findings = [
        report.findings[0].model_copy(update={"category": category, "id": f"F{n}"})
        for n, category in enumerate(Category, 1)
    ]
    text = render_markdown(report.model_copy(update={"findings": findings}))
    for category in Category:
        assert f"### {category.value.capitalize()}" in text


def test_export_writes_both_files_with_anonymous_names(tmp_path):
    exported = export_report(person_report(), tmp_path / "out")
    assert exported.markdown.read_text(encoding="utf-8") == render_markdown(person_report())
    assert json.loads(exported.json.read_text(encoding="utf-8"))["goal"] == "hiring"
    for path in (exported.markdown, exported.json):
        assert OUTPUT_NAME.fullmatch(path.name)
        assert "novak" not in path.name.lower()
    assert exported.markdown.stem == exported.json.stem
    assert re.fullmatch(r"report-20261008T120000Z-[0-9a-f]{8}", exported.markdown.stem)


def test_exports_are_never_overwritten(tmp_path):
    first = export_report(person_report(), tmp_path)
    second = export_report(person_report(), tmp_path)
    assert first.markdown != second.markdown


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_exported_files_are_readable_by_the_owner_only(tmp_path):
    exported = export_report(person_report(), tmp_path)
    for path in (exported.markdown, exported.json):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_delete_outputs_removes_only_exported_reports(tmp_path):
    export_report(person_report(), tmp_path)
    export_report(skill_report(), tmp_path)
    keep = tmp_path / "notes.md"
    keep.write_text("mine", encoding="utf-8")
    lookalike = tmp_path / "report-x.md"
    lookalike.write_text("mine too", encoding="utf-8")
    removed = delete_outputs(tmp_path)
    assert len(removed) == 4
    assert keep.exists() and lookalike.exists()
    assert tmp_path.exists()


def test_delete_outputs_keeps_the_directory_and_tolerates_missing_ones(tmp_path):
    target = tmp_path / "out"
    export_report(person_report(), target)
    assert len(delete_outputs(target)) == 2
    assert target.is_dir() and list(target.iterdir()) == []
    assert delete_outputs(target) == []
    assert delete_outputs(tmp_path / "never-created") == []


def test_delete_exported_removes_a_single_report(tmp_path):
    first = export_report(person_report(), tmp_path)
    second = export_report(person_report(), tmp_path)
    delete_exported(first)
    delete_exported(first)
    assert not first.markdown.exists() and not first.json.exists()
    assert second.markdown.exists() and second.json.exists()


def sceptical_report():
    report = person_report()
    scepticism = Scepticism(
        mode=ScepticismMode.STRICT,
        level=ScepticismLevel.HIGH,
        flags=[
            ScepticismFlag(
                code="uncorroborated_excellence",
                severity=Severity.HIGH,
                message="The overall rating of 4.4 is very high but little is corroborated.",
                finding_ids=["F1"],
            )
        ],
        cap=2.5,
        guidance=["Verify the strongest claims with references."],
    )
    rating = report.rating.model_copy(
        update={"raw_overall": 4.4, "overall": 2.5, "scepticism": scepticism}
    )
    options = ResearchOptions(
        networks=[Network.LINKEDIN, Network.WEB], scepticism=ScepticismMode.STRICT
    )
    return report.model_copy(update={"rating": rating, "options": options})


def test_markdown_explains_a_scepticism_cap_and_what_to_do_next():
    text = render_markdown(sceptical_report())
    assert "Overall: **2.5 / 5**" in text
    assert "Before the scepticism cap: 4.4 / 5" in text
    assert "## Scepticism review" in text
    assert "Level: **high** (mode: strict, rating capped at 2.5)" in text
    expected = (
        "- **high** The overall rating of 4.4 is very high"
        " but little is corroborated. Findings: F1."
    )
    assert expected in text
    assert "What to do next:" in text
    assert "- Verify the strongest claims with references." in text


def test_markdown_says_so_when_no_scepticism_flags_were_raised():
    text = render_markdown(person_report())
    assert "No scepticism flags were raised (mode: standard)." in text
    assert "Before the scepticism cap" not in text


def test_markdown_header_lists_the_enabled_sources_and_mode():
    text = render_markdown(sceptical_report())
    assert "- Sources enabled: linkedin, web" in text
    assert "- Scepticism mode: strict" in text
    default = render_markdown(person_report())
    assert "- Sources enabled: linkedin, facebook, instagram, web" in default


def test_json_carries_the_scepticism_review_and_options():
    data = json.loads(render_json(sceptical_report()))
    assert data["rating"]["raw_overall"] == 4.4
    assert data["rating"]["overall"] == 2.5
    assert data["rating"]["scepticism"]["level"] == "high"
    assert data["rating"]["scepticism"]["flags"][0]["code"] == "uncorroborated_excellence"
    assert data["options"]["networks"] == ["linkedin", "web"]
    assert PersonReport.model_validate_json(render_json(sceptical_report())) == sceptical_report()


def test_skill_markdown_shows_each_candidates_scepticism_level():
    report = skill_report()
    candidate = report.candidates[0]
    flagged = candidate.rating.model_copy(
        update={
            "scepticism": Scepticism(
                mode=ScepticismMode.STANDARD,
                level=ScepticismLevel.MEDIUM,
                flags=[
                    ScepticismFlag(code="hype_language", severity=Severity.MEDIUM, message="Hype.")
                ],
                cap=3.5,
                guidance=["Ask for concrete results."],
            )
        }
    )
    text = render_markdown(
        report.model_copy(update={"candidates": [candidate.model_copy(update={"rating": flagged})]})
    )
    assert "| 1 | Jan Novak (best) | 3.4 / 5 | medium |" in text
    assert "Level: **medium**" in text
