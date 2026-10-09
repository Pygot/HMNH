# tests/test_compliance.py
from agent.compliance import (
    check_research_request,
    check_skill_request,
    is_data_broker,
    NOTICE,
    refuse_harmful_requests,
    rejection_reason,
    require_lawful_purpose,
    STORAGE_NOTICE,
)
from agent.models import (
    Goal,
    Identity,
    ResearchRequest,
    SkillSearchRequest,
)
from agent.errors import ComplianceRefusal
from pydantic import ValidationError

import pytest


def research_request(**identity_fields):
    identity = Identity(name="Jane Doe", **identity_fields)
    return ResearchRequest(goal=Goal.HIRING, identity=identity, purpose_confirmed=True)


def test_notice_mentions_informing_the_subject():
    assert "GDPR" in NOTICE
    assert "informed" in NOTICE


def test_the_storage_notice_says_where_things_are_kept_and_how_to_delete_them():
    assert "database on this machine" in STORAGE_NOTICE
    assert "Nothing is uploaded" in STORAGE_NOTICE and "Settings" in STORAGE_NOTICE


def test_lawful_purpose_is_required():
    require_lawful_purpose(True)
    with pytest.raises(ComplianceRefusal, match="lawful purpose"):
        require_lawful_purpose(False)


@pytest.mark.parametrize(
    "text",
    [
        "find her home address",
        "where does Jane live",
        "I want to stalk my ex-girlfriend",
        "harassment campaign",
        "dox this person",
        "track him down",
        "her phone number",
        "kde bydlí Jana Nováková",
        "domácí adresa",
    ],
)
def test_harmful_requests_are_refused(text):
    with pytest.raises(ComplianceRefusal, match="Refused"):
        refuse_harmful_requests([text])


def test_harmless_text_and_blanks_pass():
    refuse_harmful_requests(["Senior Python engineer", None, ""])


def test_research_request_screens_every_free_text_field():
    with pytest.raises(ComplianceRefusal):
        check_research_request(research_request(employers=["stalk inc"]))
    with pytest.raises(ComplianceRefusal):
        check_research_request(research_request(skills=["home address lookup"]))
    check_research_request(research_request(employers=["Acme"], skills=["python"]))


def test_research_request_without_confirmation_is_refused():
    request = ResearchRequest(
        goal=Goal.SALES, identity=Identity(name="Jane Doe"), purpose_confirmed=False
    )
    with pytest.raises(ComplianceRefusal, match="lawful purpose"):
        check_research_request(request)


def test_skill_request_checks():
    ok = SkillSearchRequest(
        goal=Goal.HIRING, skill="python", location="Brno", purpose_confirmed=True
    )
    check_skill_request(ok)
    with pytest.raises(ComplianceRefusal):
        check_skill_request(ok.model_copy(update={"skill": "stalk"}))
    with pytest.raises(ComplianceRefusal):
        check_skill_request(ok.model_copy(update={"purpose_confirmed": False}))


@pytest.mark.parametrize("location", ["Main Street 12", "110 00 Praha", "12345"])
def test_location_must_be_city_level(location):
    with pytest.raises(ValidationError):
        Identity(name="Jane Doe", location=location)


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("Was diagnosed with cancer in 2020", "health"),
        ("Active member of a church community", "religion"),
        ("Voted for a party in the election", "politics"),
        ("Is gay and active in the community", "orientation"),
        ("Belongs to an ethnic minority", "ethnicity"),
        ("Is married with two children and her husband works abroad", "family"),
        ("Has a net worth of two million", "finances"),
        ("Reach her at jane@example.com", "email address"),
        ("Call +420 123 456 789 for details", "phone number"),
        ("Lives at 12 Main Street", "street address"),
        ("Bydlí na Vinohradská 12, 120 00", "street address"),
    ],
)
def test_sensitive_and_contact_statements_are_rejected(statement, reason):
    assert reason in (rejection_reason(statement) or "")


@pytest.mark.parametrize(
    "statement",
    [
        "Senior engineer at Acme from 2019 to 2023",
        "Published a paper on graph databases in 2021",
        "Spoke at PyCon in 2022",
        "Studied at Temple University",
        "Worked 2019-2023 as a financial analyst",
    ],
)
def test_professional_statements_pass(statement):
    assert rejection_reason(statement) is None


def test_data_brokers_are_recognised():
    assert is_data_broker("https://www.spokeo.com/Jane-Doe")
    assert is_data_broker("https://spokeo.com/x")
    assert not is_data_broker("https://notspokeo.com/x")
