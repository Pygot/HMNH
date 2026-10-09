# tests/test_drafts.py
from agent.drafts import (
    build_context,
    builtin_templates,
    Draft,
    DraftCategory,
    DraftPurpose,
    EmailTemplate,
    render_draft,
    sample_context,
    TemplateStore,
    validate_template,
)
from tests.fakes import (
    company_profile,
    hiring_report,
    person_report,
    sceptical_report,
    skill_report,
)
from agent.errors import InvalidRequest
from agent.config import EmailTuning
from agent.models import Tag

import pytest

TUNING = EmailTuning()
THREE_DEEP = (
    "{% for a in [1] %}{% for b in [1] %}{% for c in [1] %}x{% endfor %}{% endfor %}{% endfor %}"
)


def template(identifier="my-template", category=DraftCategory.SINGLE, **extra):
    values = {
        "id": identifier,
        "name": "Mine",
        "category": category,
        "purpose": DraftPurpose.FOLLOWUP,
        "subject": "Hello {{ candidate.first_name }}",
        "body": "Hi {{ candidate.first_name }} from {{ sender.company }}",
    }
    values.update(extra)
    return EmailTemplate(**values)


def context(category=DraftCategory.SINGLE, **extra):
    values = {
        "name": "Jan Novak",
        "role_title": "Python engineer",
        "company": company_profile(),
        "company_name": "Acme",
        "sender_name": "Eva Svobodova",
        "tuning": TUNING,
    }
    values.update(extra)
    return build_context(category, **values)


def test_there_is_a_builtin_template_for_every_category_and_purpose_we_promise():
    found = {(item.category, item.purpose) for item in builtin_templates()}
    assert (DraftCategory.SINGLE, DraftPurpose.OUTREACH) in found
    assert (DraftCategory.SINGLE, DraftPurpose.INTERVIEW) in found
    assert (DraftCategory.SINGLE, DraftPurpose.FOLLOWUP) in found
    assert (DraftCategory.SINGLE, DraftPurpose.REJECTION) in found
    assert (DraftCategory.BATCH, DraftPurpose.OUTREACH) in found
    assert (DraftCategory.TEAM, DraftPurpose.REPORT) in found
    identifiers = [item.id for item in builtin_templates()]
    assert len(identifiers) == len(set(identifiers))
    assert all(item.builtin for item in builtin_templates())


@pytest.mark.parametrize("item", builtin_templates(), ids=lambda item: item.id)
def test_every_builtin_template_renders_with_the_sample_data(item):
    draft = render_draft(item, sample_context(item.category, TUNING), TUNING)
    assert draft.subject and draft.body
    assert "{{" not in draft.subject + draft.body and "{%" not in draft.subject + draft.body
    validate_template(item, TUNING)


def test_rendered_templates_use_the_candidate_the_company_and_the_sender():
    item = next(item for item in builtin_templates() if item.id == "outreach-single")
    draft = render_draft(item, context(), TUNING, recipient="Jan Novak")
    assert draft.recipient == "Jan Novak"
    assert draft.subject == "Python engineer at Acme"
    assert draft.body.startswith("Hi Jan,")
    assert "Eva Svobodova at Acme, a Payments software company" in draft.body
    assert "You would work on Acme builds card payment terminals for small shops." in draft.body
    assert "Best regards,\nEva Svobodova" in draft.body


def test_missing_company_details_are_left_out_gracefully():
    item = next(item for item in builtin_templates() if item.id == "outreach-single")
    draft = render_draft(item, context(company=None, company_name=None, sender_name=None), TUNING)
    assert draft.body.split("\n\n")[1] == "I am part of the hiring team."
    assert draft.subject == "Python engineer"
    assert "You would work on" not in draft.body


def test_outreach_drafts_end_with_the_opt_out_footer():
    item = next(item for item in builtin_templates() if item.id == "outreach-single")
    draft = render_draft(item, context(), TUNING)
    assert "\n\n--\n" in draft.body
    assert draft.body.endswith("Reply STOP and we will not write to you again.")
    assert "from Acme because your public professional profile" in draft.body


def test_other_candidate_purposes_have_no_footer():
    for item in builtin_templates():
        if item.category is DraftCategory.TEAM or item.purpose is DraftPurpose.OUTREACH:
            continue
        draft = render_draft(item, sample_context(item.category, TUNING), TUNING)
        assert "\n--\n" not in draft.body and "STOP" not in draft.body


def test_team_drafts_end_with_the_team_footer():
    item = next(item for item in builtin_templates() if item.id == "report-team")
    draft = render_draft(item, sample_context(DraftCategory.TEAM, TUNING), TUNING)
    assert draft.body.endswith("Decision support only.")


def test_the_footers_come_from_the_configuration():
    item = template(purpose=DraftPurpose.OUTREACH)
    custom = EmailTuning(footer="Unsubscribe via {{ sender.company }}.")
    draft = render_draft(item, context(), custom)
    assert draft.body.endswith("--\nUnsubscribe via Acme.")


def test_subjects_are_one_line_and_bounded():
    item = template(subject="A\n  B {{ candidate.first_name }}")
    assert render_draft(item, context(), TUNING).subject == "A B Jan"
    short = EmailTuning(max_subject_chars=20)
    long = template(subject="x" * 60)
    validate_ok = render_draft(long, context(), short)
    assert len(validate_ok.subject) == 20


def test_bodies_are_bounded_by_the_configuration():
    item = template(body="y" * 500)
    assert len(render_draft(item, context(), EmailTuning(max_body_chars=100)).body) == 100


def test_the_candidate_context_has_names_and_a_public_highlight():
    report = hiring_report()
    data = build_context(
        DraftCategory.SINGLE,
        name="Jan van der Novak",
        role_title=None,
        company=None,
        company_name=None,
        sender_name=None,
        tuning=TUNING,
        report=report,
    )
    candidate = data["candidate"]
    assert candidate["first_name"] == "Jan" and candidate["last_name"] == "Novak"
    assert candidate["location"] == "Brno" and candidate["skills"] == ["python"]
    assert candidate["highlight"] == "Senior Python engineer at Acme since 2021"
    assert data["sender"]["company"] == "our team"
    assert data["role"] == {"title": ""}


def test_uncertain_findings_never_become_the_highlight():
    report = person_report()
    weak = report.findings[0].model_copy(update={"tag": Tag.UNCERTAIN})
    report = report.model_copy(update={"findings": [weak]})
    data = build_context(
        DraftCategory.SINGLE,
        name="Jan Novak",
        role_title=None,
        company=None,
        company_name=None,
        sender_name=None,
        tuning=TUNING,
        report=report,
    )
    assert data["candidate"]["highlight"] == ""


def test_a_one_word_name_has_no_last_name():
    data = context(name="Madonna")
    assert data["candidate"]["first_name"] == "Madonna" and data["candidate"]["last_name"] == ""


def test_the_company_name_given_by_the_user_wins_over_the_profile():
    assert context(company_name="Globex")["sender"]["company"] == "Globex"
    assert context(company_name=None)["sender"]["company"] == "Acme"


def test_only_team_contexts_carry_the_report():
    assert "report" not in context(DraftCategory.SINGLE, report=hiring_report())
    assert "report" not in context(DraftCategory.BATCH, report=hiring_report())
    assert "report" in context(DraftCategory.TEAM, report=hiring_report())


def test_the_team_report_summarises_a_person_report():
    data = context(DraftCategory.TEAM, report=hiring_report())["report"]
    assert data["overall"] == "3.6" and data["capped"] is False
    assert data["findings_count"] == 5 and data["supported_count"] == 2
    assert data["sources_count"] == 3
    assert data["coverage"] == 60
    assert data["requirement_lines"][0] == "English (C1): met"
    assert len(data["top_findings"]) == 5


def test_a_capped_rating_is_explained_in_the_team_draft():
    item = next(item for item in builtin_templates() if item.id == "report-team")
    data = context(DraftCategory.TEAM, report=sceptical_report())
    draft = render_draft(item, data, TUNING)
    assert "Overall rating: 2.5 / 5 (capped from 4.4 by the scepticism check)" in draft.body
    assert "Scepticism: high\n- Roles overlap." in draft.body


def test_the_team_report_summarises_a_skill_search():
    data = context(DraftCategory.TEAM, report=skill_report())["report"]
    assert data["candidates"] == [{"name": "Jan Novak", "rating": "3.4", "level": "none"}]
    assert data["coverage"] is None and data["findings_count"] == 0
    item = next(item for item in builtin_templates() if item.id == "shortlist-team")
    draft = render_draft(item, context(DraftCategory.TEAM, report=skill_report()), TUNING)
    assert "1. Jan Novak - 3.4 / 5, scepticism none" in draft.body
    assert "at Acme" in draft.body


def test_the_team_report_without_any_report_is_empty_but_renders():
    data = context(DraftCategory.TEAM)
    item = next(item for item in builtin_templates() if item.id == "report-team")
    draft = render_draft(item, data, TUNING)
    assert "Findings: 0" in draft.body


def test_sample_contexts_exist_for_every_category():
    for category in DraftCategory:
        sample = sample_context(category, TUNING)
        assert sample["candidate"]["name"] == "Jan Novak"
        assert ("report" in sample) is (category is DraftCategory.TEAM)


@pytest.mark.parametrize(
    "source",
    [
        "{{ ''.__class__.__mro__ }}",
        "{{ cycler }}",
        "{{ range(5) }}",
        "{{ lipsum() }}",
        "{{ dict(a=1) }}",
        "{{ namespace() }}",
        "{{ candidate.__class__ }}",
        "{{ self.__init__.__globals__ }}",
        "{{ config }}",
        "{{ undefined_name }}",
        "{{ candidate.nothing }}",
    ],
)
def test_unsafe_or_unknown_expressions_cannot_be_rendered(source):
    with pytest.raises(InvalidRequest, match="could not be rendered"):
        render_draft(template(body=source), context(), TUNING)


@pytest.mark.parametrize(
    "source",
    [
        "{% include 'x' %}",
        "{%+ include 'x' %}",
        "{% set x = 1 %}",
        "{% with x = 1 %}{{ x }}{% endwith %}",
        "{% do candidate %}",
        "{%- import 'x' as y %}",
        "{% extends 'base' %}",
        "{% from 'x' import y %}",
        "{% macro m() %}x{% endmacro %}",
        "{% call m() %}x{% endcall %}",
        "{% block b %}x{% endblock %}",
    ],
)
def test_template_structure_tags_are_forbidden(source):
    with pytest.raises(InvalidRequest, match="may not use these tags"):
        validate_template(template(body=source), TUNING)
    with pytest.raises(InvalidRequest, match="may not use these tags"):
        validate_template(template(subject=source), TUNING)


def test_mutating_the_context_is_impossible():
    with pytest.raises(InvalidRequest):
        render_draft(template(body="{{ candidate.skills.append(1) }}"), context(), TUNING)
    with pytest.raises(InvalidRequest):
        render_draft(template(body="{{ candidate.update({}) }}"), context(), TUNING)


def test_loops_run_over_the_data_we_provide():
    body = "{% for item in candidate.skills %}{{ item }},{% endfor %}"
    data = context(report=hiring_report())
    assert render_draft(template(body=body), data, TUNING).body == "python,"


def test_loops_may_follow_each_other_but_nest_only_to_the_configured_depth():
    flat = (
        "{% for a in candidate.skills %}a{% endfor %}{% for b in candidate.skills %}b{% endfor %}"
    )
    two = "{% for a in candidate.skills %}{% for b in candidate.skills %}x{% endfor %}{% endfor %}"
    three = THREE_DEEP
    data = context(report=hiring_report())
    assert render_draft(template(body=flat), data, TUNING).body == "ab"
    assert render_draft(template(body=two), data, TUNING).body == "x"
    with pytest.raises(InvalidRequest, match="nested at most 2 deep"):
        render_draft(template(body=three), data, TUNING)
    deeper = EmailTuning(max_loop_depth=3)
    assert render_draft(template(body=three), data, deeper).body == "x"


def test_loop_depth_is_checked_in_the_subject_and_the_footer_too():
    nested = THREE_DEEP
    with pytest.raises(InvalidRequest, match="nested"):
        render_draft(template(subject=nested), context(), TUNING)
    with pytest.raises(InvalidRequest, match="nested"):
        render_draft(template(purpose=DraftPurpose.OUTREACH), context(), EmailTuning(footer=nested))


@pytest.mark.parametrize(
    "source",
    [
        "{{ 'x' * 100000000 }}",
        "{{ 2 ** 100000 }}",
        "{{ '%1000000000s' % 'x' }}",
        "{{ 3 % 2 }}",
        "{{ 'x'|center(1000000000) }}",
        "{{ 'x'|indent(1000000000) }}",
        "{{ 'x'|format() }}",
        "{{ candidate|attr('__class__') }}",
        "{{ candidate|tojson }}",
    ],
)
def test_expressions_that_could_exhaust_memory_are_not_available(source):
    with pytest.raises(InvalidRequest, match="could not be rendered"):
        render_draft(template(body=source), context(), TUNING)


def test_the_allowed_filters_and_operators_come_from_the_configuration():
    source = "{{ candidate.first_name|upper }}{{ 'x' * 3 }}"
    permissive = EmailTuning(blocked_operators=())
    assert render_draft(template(body=source), context(), permissive).body == "JANxxx"
    no_filters = EmailTuning(filters=())
    with pytest.raises(InvalidRequest):
        render_draft(template(body="{{ candidate.first_name|upper }}"), context(), no_filters)
    assert (
        render_draft(
            template(body="{{ candidate.first_name|upper }}{{ 1 + 2 }}"), context(), TUNING
        ).body
        == "JAN3"
    )


def test_syntax_errors_are_reported_as_invalid_requests():
    with pytest.raises(InvalidRequest, match="could not be rendered"):
        validate_template(template(body="{% if %}"), TUNING)
    with pytest.raises(InvalidRequest, match="could not be rendered"):
        validate_template(template(body="{{ unclosed"), TUNING)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"id": "ab"}, "template id"),
        ({"id": "Has Spaces"}, "template id"),
        ({"id": "-leading-dash"}, "template id"),
        ({"subject": "  "}, "subject and a body"),
        ({"body": ""}, "subject and a body"),
        ({"subject": "two\nlines"}, "one line"),
        ({"subject": "s" * 300}, "one line"),
        ({"body": "b" * 9000}, "at most 8000"),
    ],
)
def test_templates_are_validated(update, message):
    with pytest.raises(InvalidRequest, match=message):
        validate_template(template(**update), TUNING)


def test_the_template_model_rejects_unknown_fields_and_values():
    with pytest.raises(ValueError):
        EmailTemplate(
            id="abc",
            name="x",
            category="everyone",
            purpose="outreach",
            subject="s",
            body="b",
        )
    with pytest.raises(ValueError):
        template(extra_field=1)


def store(tmp_path, **settings):
    return TemplateStore(EmailTuning(template_dir=tmp_path / "templates", **settings))


def test_a_fresh_store_lists_only_the_builtin_templates(tmp_path):
    items = store(tmp_path).all()
    assert [item.id for item in items] == [item.id for item in builtin_templates()]
    assert store(tmp_path).custom() == []


def test_saved_templates_are_listed_and_can_be_read_back(tmp_path):
    items = store(tmp_path)
    saved = items.save(template("sales-note"))
    assert saved.builtin is False
    assert [item.id for item in items.custom()] == ["sales-note"]
    assert items.get("sales-note") == saved
    assert items.get("outreach-single").builtin is True
    assert items.get("nothing-here") is None
    assert (tmp_path / "templates" / "sales-note.json").is_file()


def test_saving_never_marks_a_template_builtin_and_does_not_store_that_flag(tmp_path):
    items = store(tmp_path)
    saved = items.save(template("sales-note", builtin=True))
    assert saved.builtin is False
    assert '"builtin"' not in (tmp_path / "templates" / "sales-note.json").read_text()


def test_saving_again_replaces_the_template(tmp_path):
    items = store(tmp_path)
    items.save(template("sales-note"))
    items.save(template("sales-note", name="Renamed"))
    assert [item.name for item in items.custom()] == ["Renamed"]


def test_builtin_templates_cannot_be_overwritten(tmp_path):
    with pytest.raises(InvalidRequest, match="Built-in"):
        store(tmp_path).save(template("outreach-single"))


def test_invalid_templates_are_not_written(tmp_path):
    items = store(tmp_path)
    with pytest.raises(InvalidRequest):
        items.save(template("bad-one", body="{% include 'x' %}"))
    assert items.custom() == []
    assert not (tmp_path / "templates").exists()


def test_the_template_limit_is_enforced_but_replacing_is_still_allowed(tmp_path):
    items = store(tmp_path, max_templates=2)
    items.save(template("one-note"))
    items.save(template("two-note"))
    with pytest.raises(InvalidRequest, match="limit"):
        items.save(template("three-note"))
    items.save(template("two-note", name="Again"))
    assert len(items.custom()) == 2


def test_deleting_removes_custom_templates_only(tmp_path):
    items = store(tmp_path)
    items.save(template("sales-note"))
    assert items.delete("sales-note") is True
    assert items.delete("sales-note") is False
    assert items.delete("outreach-single") is False
    assert items.delete("../../etc/passwd") is False
    assert items.custom() == []


def test_corrupt_oversized_and_misnamed_files_are_ignored(tmp_path):
    items = store(tmp_path, max_file_bytes=1024)
    items.save(template("good-one"))
    folder = tmp_path / "templates"
    (folder / "broken.json").write_text("{not json", encoding="utf-8")
    (folder / "wrong-shape.json").write_text('{"id": "wrong-shape"}', encoding="utf-8")
    (folder / "other-name.json").write_text(
        (folder / "good-one.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (folder / "huge.json").write_text(" " * 2000, encoding="utf-8")
    (folder / "folder.json").mkdir()
    (folder / "outreach-single.json").write_text(
        (folder / "good-one.json")
        .read_text(encoding="utf-8")
        .replace("good-one", "outreach-single"),
        encoding="utf-8",
    )
    assert [item.id for item in items.custom()] == ["good-one"]


def test_stored_templates_cannot_claim_to_be_builtin(tmp_path):
    items = store(tmp_path)
    items.save(template("sales-note"))
    path = tmp_path / "templates" / "sales-note.json"
    path.write_text(path.read_text(encoding="utf-8").replace("{", '{"builtin": true,', 1))
    assert items.get("sales-note").builtin is False


def test_a_draft_model_holds_the_recipient_subject_and_body():
    draft = Draft(subject="S", body="B")
    assert draft.recipient is None
    assert Draft(recipient="Jan", subject="S", body="B").recipient == "Jan"
