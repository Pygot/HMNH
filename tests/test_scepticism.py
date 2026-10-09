# tests/test_scepticism.py
from agent.models import (
    Category,
    Finding,
    ScepticismLevel,
    ScepticismMode,
    Severity,
    Tag,
)
from agent.config import (
    ScepticismProfile,
    ScepticismTuning,
)
from agent.scepticism import (
    assess,
    capped,
)

STANDARD = ScepticismMode.STANDARD
STRICT = ScepticismMode.STRICT
TUNING = ScepticismTuning()
SITE = "https://jan.dev"
GITHUB = "https://github.com/jan"
LINKEDIN = "https://www.linkedin.com/in/jan"


def finding(
    number,
    category=Category.EMPLOYMENT,
    tag=Tag.SINGLE_SOURCE,
    urls=(SITE,),
    statement=None,
    year=None,
    start_year=None,
    conflict=False,
):
    return Finding(
        id=f"F{number}",
        statement=statement or f"Statement {number}",
        category=category,
        tag=tag,
        source_urls=list(urls),
        independent_sources=len(urls),
        conflict=conflict,
        year=year,
        start_year=start_year,
    )


def codes(result):
    return [flag.code for flag in result.flags]


def run(findings, raw=3.0, mode=STANDARD, tuning=TUNING, corroboration=True):
    return assess(findings, raw, mode, tuning, corroboration)


def test_no_findings_means_no_flags():
    result = run([])
    assert result.level is ScepticismLevel.NONE
    assert result.flags == [] and result.cap is None and result.guidance == []


def test_a_clean_profile_raises_nothing():
    findings = [finding(1, tag=Tag.SUPPORTED, urls=(SITE, GITHUB)), finding(2, urls=(GITHUB,))]
    result = run(findings, raw=3.0)
    assert result.level is ScepticismLevel.NONE
    assert capped(3.0, result) == 3.0


def test_excellence_without_corroboration_is_the_strongest_warning():
    findings = [finding(number, urls=(SITE, GITHUB)) for number in range(1, 5)]
    result = run(findings, raw=4.4)
    assert result.level is ScepticismLevel.HIGH
    assert codes(result)[0] == "uncorroborated_excellence"
    assert result.cap == 2.5
    assert capped(4.4, result) == 2.5
    assert result.flags[0].severity is Severity.HIGH
    assert result.flags[0].finding_ids == ["F1", "F2", "F3", "F4"]


def test_excellence_is_fine_when_enough_findings_are_corroborated():
    findings = [
        finding(1, tag=Tag.SUPPORTED, urls=(SITE, GITHUB)),
        finding(2, tag=Tag.SUPPORTED, urls=(SITE, GITHUB)),
        finding(3, urls=(SITE,)),
    ]
    assert "uncorroborated_excellence" not in codes(run(findings, raw=4.5))
    assert "uncorroborated_excellence" not in codes(run([finding(1, urls=(SITE, GITHUB))], raw=3.9))


def test_many_claims_from_one_source_are_flagged_as_thin():
    findings = [finding(number) for number in range(1, 5)]
    result = run(findings)
    assert codes(result) == ["thin_sources"]
    assert result.level is ScepticismLevel.MEDIUM
    assert result.cap == 3.5
    assert "single independent source" in result.flags[0].message
    assert codes(run(findings[:3])) == []
    assert codes(run([*findings[:3], finding(9, urls=(GITHUB,))])) == []


def test_prestige_claims_without_corroboration_are_flagged():
    awards = [finding(number, category=Category.AWARD) for number in range(1, 5)]
    result = run(awards, corroboration=False)
    assert codes(result) == ["prestige_density"]
    mixed = [
        finding(1, category=Category.CERTIFICATION, tag=Tag.SUPPORTED, urls=(SITE, GITHUB)),
        finding(2, category=Category.AWARD, tag=Tag.SUPPORTED, urls=(SITE, GITHUB)),
        finding(3, category=Category.AWARD),
        finding(4, category=Category.AWARD),
    ]
    assert codes(run(mixed, corroboration=False)) == []


def test_promotional_wording_is_flagged_and_the_word_list_is_configurable():
    hyped = [
        finding(1, statement="A world-class engineer"),
        finding(2, statement="Visionary thought leader"),
    ]
    result = run(hyped, corroboration=False)
    assert codes(result) == ["hype_language"]
    assert result.flags[0].finding_ids == ["F1", "F2"]
    assert codes(run(hyped[:1], corroboration=False)) == []
    custom = ScepticismTuning(hype_words=("plain",))
    assert codes(run(hyped, tuning=custom, corroboration=False)) == []
    plain = [finding(1, statement="Plain wording"), finding(2, statement="More plain words")]
    assert codes(run(plain, tuning=custom, corroboration=False)) == ["hype_language"]


def test_overlapping_full_time_roles_are_flagged():
    roles = [
        finding(1, start_year=2018, year=2024),
        finding(2, start_year=2019, year=2025),
        finding(3, start_year=2020, year=2025),
    ]
    result = run(roles, corroboration=False)
    assert codes(result) == ["overlapping_roles"]
    assert result.level is ScepticismLevel.HIGH
    sequential = [
        finding(1, start_year=2010, year=2014),
        finding(2, start_year=2015, year=2019),
        finding(3, start_year=2020, year=2025),
    ]
    assert codes(run(sequential, corroboration=False)) == []


def test_roles_without_start_years_cannot_overlap():
    undated = [finding(number, year=2024) for number in range(1, 4)]
    assert codes(run(undated, corroboration=False)) == []


def test_an_implausibly_long_career_is_flagged():
    long_career = [finding(1, start_year=1960, year=1990), finding(2, start_year=1995, year=2025)]
    result = run(long_career, corroboration=False)
    assert codes(result) == ["career_span"]
    assert "65 years" in result.flags[0].message
    short = [finding(1, start_year=1990, year=2000), finding(2, start_year=2001, year=2025)]
    assert codes(run(short, corroboration=False)) == []


def test_very_long_skill_lists_are_flagged_as_low_severity():
    skills = [finding(number, category=Category.SKILL) for number in range(1, 31)]
    result = run(skills, corroboration=False)
    assert codes(result) == ["skill_inflation"]
    assert result.level is ScepticismLevel.LOW
    assert result.cap is None
    assert capped(4.2, result) == 4.2


def test_the_worst_flag_decides_the_level_and_guidance_is_deduplicated():
    findings = [
        finding(number, statement="World-class thought leader", urls=(SITE, GITHUB))
        for number in range(1, 6)
    ]
    result = run(findings, raw=4.6)
    assert result.level is ScepticismLevel.HIGH
    order = {"high": 3, "medium": 2, "low": 1}
    ranks = [order[flag.severity.value] for flag in result.flags]
    assert ranks == sorted(ranks, reverse=True)
    assert len(result.guidance) == len(set(result.guidance)) == len(result.flags)


def test_strict_mode_uses_lower_bars_and_harder_caps():
    findings = [finding(number, urls=(SITE, GITHUB)) for number in range(1, 4)]
    assert codes(run(findings, raw=3.6, mode=STANDARD)) == []
    strict = run(findings, raw=3.6, mode=STRICT)
    assert "uncorroborated_excellence" in codes(strict)
    assert strict.mode is STRICT
    assert strict.cap == 2.0


def test_candidates_are_not_asked_to_be_corroborated():
    findings = [finding(number) for number in range(1, 6)]
    assert "thin_sources" in codes(run(findings))
    assert codes(run(findings, raw=4.8, corroboration=False)) == []


def test_profiles_and_caps_are_configurable():
    tuning = ScepticismTuning(
        standard=ScepticismProfile(thin_sources_min_findings=2, cap_medium=1.0)
    )
    result = run([finding(1), finding(2)], tuning=tuning)
    assert codes(result) == ["thin_sources"]
    assert capped(4.0, result) == 1.0
    uncapped = ScepticismTuning(
        standard=ScepticismProfile(thin_sources_min_findings=2, cap_medium=None)
    )
    assert capped(4.0, run([finding(1), finding(2)], tuning=uncapped)) == 4.0


def test_capping_never_raises_a_rating():
    findings = [finding(number) for number in range(1, 5)]
    result = run(findings)
    assert capped(2.0, result) == 2.0
    assert capped(5.0, result) == 3.5
