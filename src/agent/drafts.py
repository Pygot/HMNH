# src/agent/drafts.py
from agent.models import (
    Category,
    CompanyProfile,
    FactKind,
    PersonReport,
    ShortText,
    SkillSearchReport,
    Tag,
)
from jinja2 import (
    nodes,
    StrictUndefined,
    TemplateError,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
)
from agent.config import (
    EmailTuning,
    TEMPLATE_ID,
)
from jinja2.sandbox import ImmutableSandboxedEnvironment
from jinja2.exceptions import SecurityError
from agent.errors import InvalidRequest
from functools import cache
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

import json
import re

HIGHLIGHT_CATEGORIES = (Category.EMPLOYMENT, Category.PROJECT, Category.PUBLICATION, Category.TALK)


class DraftCategory(StrEnum):
    """The audience of an email template: one candidate, a group or the team."""

    SINGLE = "single"
    BATCH = "batch"
    TEAM = "team"


class DraftPurpose(StrEnum):
    """The reason an email template is written, such as outreach or rejection."""

    OUTREACH = "outreach"
    INTERVIEW = "interview"
    FOLLOWUP = "followup"
    REJECTION = "rejection"
    REPORT = "report"


class EmailTemplate(BaseModel):
    """A named email template with a subject and a body in Jinja syntax.

    The subject and body are rendered in a sandbox. Built-in templates are marked with
    builtin and cannot be changed or deleted.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: ShortText
    category: DraftCategory
    purpose: DraftPurpose
    subject: str
    body: str
    builtin: bool = False


class Draft(BaseModel):
    """A rendered email with an optional recipient, a subject and a body."""

    recipient: str | None = None
    subject: str
    body: str


@cache
def builtin_templates() -> tuple[EmailTemplate, ...]:
    """Return the email templates that ship with the application.

    The result is cached, so every call returns the same tuple.

    Returns:
        The built-in templates for single, batch and team messages.
    """
    return (
        EmailTemplate(
            id="outreach-single",
            name="Reach out to one candidate",
            category=DraftCategory.SINGLE,
            purpose=DraftPurpose.OUTREACH,
            subject=(
                "{{ role.title or 'An opportunity' }}"
                "{% if company.name %} at {{ company.name }}{% endif %}"
            ),
            body=(
                "Hi {{ candidate.first_name }},\n\n"
                "I am {{ sender.name or 'part of the hiring team' }}"
                "{% if company.name %} at {{ company.name }}"
                "{% if company.category %}, a {{ company.category }} company{% endif %}"
                "{% endif %}."
                "{% if candidate.highlight %} I came across your public work: "
                "{{ candidate.highlight }}{% endif %}\n\n"
                "We have an open position{% if role.title %}: {{ role.title }}{% endif %}"
                "{% if company.products %}. You would work on "
                "{{ company.products[0] }}{% endif %}. "
                "Would you be open to a short conversation this week?\n\n"
                "Best regards,\n{{ sender.name or sender.company }}"
            ),
            builtin=True,
        ),
        EmailTemplate(
            id="interview-single",
            name="Invite one candidate to interview",
            category=DraftCategory.SINGLE,
            purpose=DraftPurpose.INTERVIEW,
            subject=(
                "Interview for {{ role.title or 'the role' }}"
                "{% if company.name %} at {{ company.name }}{% endif %}"
            ),
            body=(
                "Hi {{ candidate.first_name }},\n\n"
                "Thank you for your interest in {{ sender.company }}. We would like to invite you "
                "to an interview for {{ role.title or 'the role' }}. It lasts about 45 minutes and "
                "we will talk about your experience and about how we work.\n\n"
                "Please reply with two or three times that suit you.\n\n"
                "Best regards,\n{{ sender.name or sender.company }}"
            ),
            builtin=True,
        ),
        EmailTemplate(
            id="followup-single",
            name="Follow up with one candidate",
            category=DraftCategory.SINGLE,
            purpose=DraftPurpose.FOLLOWUP,
            subject="Following up: {{ role.title or 'our conversation' }}",
            body=(
                "Hi {{ candidate.first_name }},\n\n"
                "I wanted to follow up on my earlier message about {{ role.title or 'the role' }}"
                "{% if company.name %} at {{ company.name }}{% endif %}. "
                "If the timing is not right, no problem at all.\n\n"
                "Best regards,\n{{ sender.name or sender.company }}"
            ),
            builtin=True,
        ),
        EmailTemplate(
            id="rejection-single",
            name="Decline one candidate kindly",
            category=DraftCategory.SINGLE,
            purpose=DraftPurpose.REJECTION,
            subject="Your application for {{ role.title or 'the role' }}",
            body=(
                "Hi {{ candidate.first_name }},\n\n"
                "Thank you for the time you invested in {{ sender.company }}. After careful "
                "consideration we decided to continue with other candidates for "
                "{{ role.title or 'this role' }}. We appreciate your interest and wish you every "
                "success.\n\nBest regards,\n{{ sender.name or sender.company }}"
            ),
            builtin=True,
        ),
        EmailTemplate(
            id="outreach-batch",
            name="Reach out to a group of candidates",
            category=DraftCategory.BATCH,
            purpose=DraftPurpose.OUTREACH,
            subject=(
                "{{ role.title or 'An opportunity' }}"
                "{% if company.name %} at {{ company.name }}{% endif %}"
            ),
            body=(
                "Hi {{ candidate.first_name }},\n\n"
                "I am {{ sender.name or 'part of the hiring team' }}"
                "{% if company.name %} at {{ company.name }}"
                "{% if company.category %}, a {{ company.category }} company{% endif %}"
                "{% endif %}. "
                "We are growing and have an open position"
                "{% if role.title %}: {{ role.title }}{% endif %}."
                "{% if candidate.highlight %} Your public work caught our eye: "
                "{{ candidate.highlight }}{% endif %}\n\n"
                "If you are curious, reply and we will set up a short call.\n\n"
                "Best regards,\n{{ sender.name or sender.company }}"
            ),
            builtin=True,
        ),
        EmailTemplate(
            id="update-batch",
            name="Status update for a group",
            category=DraftCategory.BATCH,
            purpose=DraftPurpose.FOLLOWUP,
            subject="An update on {{ role.title or 'our hiring' }}",
            body=(
                "Hi {{ candidate.first_name }},\n\n"
                "A short update from {{ sender.company }}: we are still reviewing applications for "
                "{{ role.title or 'the role' }} and will come back to everyone soon. Thank you for "
                "your patience.\n\nBest regards,\n{{ sender.name or sender.company }}"
            ),
            builtin=True,
        ),
        EmailTemplate(
            id="report-team",
            name="Share a candidate report with the team",
            category=DraftCategory.TEAM,
            purpose=DraftPurpose.REPORT,
            subject=(
                "Research summary: {{ candidate.name }}"
                "{% if role.title %} for {{ role.title }}{% endif %}"
            ),
            body=(
                "Hi team,\n\nHere is the research summary for {{ candidate.name }}"
                "{% if role.title %} for the {{ role.title }} role{% endif %}.\n\n"
                "Overall rating: {{ report.overall }} / 5"
                "{% if report.capped %} (capped from {{ report.raw_overall }} by the scepticism "
                "check){% endif %}.\n"
                "Scepticism: {{ report.level }}{% for flag in report.flags %}\n- {{ flag }}"
                "{% endfor %}\n"
                "Findings: {{ report.findings_count }} ({{ report.supported_count }} supported by "
                "two or more sources) from {{ report.sources_count }} sources.\n"
                "{% if report.coverage is not none %}Requirements covered: {{ report.coverage }}%."
                "{% for line in report.requirement_lines %}\n- {{ line }}{% endfor %}\n{% endif %}"
                "\nTop findings:{% for finding in report.top_findings %}\n- {{ finding }}"
                "{% endfor %}\n\n"
                "This is automated decision support; please review the sources before acting."
            ),
            builtin=True,
        ),
        EmailTemplate(
            id="shortlist-team",
            name="Share a candidate shortlist with the team",
            category=DraftCategory.TEAM,
            purpose=DraftPurpose.REPORT,
            subject="Shortlist: {{ role.title or 'candidates' }}",
            body=(
                "Hi team,\n\nHere is the shortlist"
                "{% if role.title %} for {{ role.title }}{% endif %}"
                "{% if company.name %} at {{ company.name }}{% endif %}:\n"
                "{% for item in report.candidates %}\n{{ loop.index }}. {{ item.name }} - "
                "{{ item.rating }} / 5, scepticism {{ item.level }}{% endfor %}\n\n"
                "Each rating is automated decision support; please review the sources "
                "before acting."
            ),
            builtin=True,
        ),
    )


class DraftEnvironment(ImmutableSandboxedEnvironment):
    """A sandboxed Jinja environment that rejects selected operators."""

    def call_binop(self, _context: Any, operator: str, _left: Any, _right: Any) -> Any:
        """Reject a binary operator that the sandbox intercepts.

        Jinja calls this only for the operators listed in intercepted_binops, which the
        configuration fills in.

        Args:
            _context: The evaluation context, not used.
            operator: The operator symbol.
            _left: The left operand, not used.
            _right: The right operand, not used.

        Raises:
            SecurityError: always, naming the operator.
        """
        raise SecurityError(f"the operator {operator} is not available in templates")


@cache
def _environment(tuning: EmailTuning) -> DraftEnvironment:
    """Build the cached sandbox used to render email templates.

    Undefined variables raise an error, autoescape is off because the output is plain
    text, the configured operators and globals are removed, and only the allowed filters
    are kept.

    Args:
        tuning: The email settings that list the blocked operators, globals and filters.

    Returns:
        The configured environment.
    """
    environment = DraftEnvironment(
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=False,
        keep_trailing_newline=False,
    )
    # Blocking operators such as * and ** stops templates from building huge strings or numbers.
    environment.intercepted_binops = frozenset(tuning.blocked_operators)
    for name in tuning.removed_globals:
        environment.globals.pop(name, None)
    for name in list(environment.filters):
        if name not in tuning.filters:
            del environment.filters[name]
    return environment


@cache
def _forbidden(tags: tuple[str, ...]) -> re.Pattern[str]:
    """Compile a pattern that finds forbidden Jinja block tags.

    The pattern matches a block opening with any of the tags, including the whitespace
    control forms such as {%- and {%+.

    Args:
        tags: The tag names that templates may not use.

    Returns:
        The compiled pattern.
    """
    names = "|".join(re.escape(tag) for tag in tags)
    return re.compile(r"\{%[-+]?\s*(" + names + r")\b")


def _loop_depth(node: nodes.Node) -> int:
    """Return how deeply for loops are nested in a parsed template.

    Args:
        node: The parsed template node to measure.

    Returns:
        The deepest nesting of for loops below the node, 0 when there is none.
    """
    deepest = max((_loop_depth(child) for child in node.iter_child_nodes()), default=0)
    return deepest + 1 if isinstance(node, nodes.For) else deepest


def _first_name(name: str) -> str:
    """Return the first word of a name.

    Args:
        name: The full name.

    Returns:
        The first word, or the name unchanged when it has no words.
    """
    parts = name.split()
    return parts[0] if parts else name


def _candidate(name: str, report: PersonReport | None) -> dict[str, Any]:
    """Build the candidate variables that templates can use.

    The highlight is the first finding about employment, a project, a publication or a
    talk that is not tagged uncertain.

    Args:
        name: The candidate's full name.
        report: The research report of the candidate, if one exists.

    Returns:
        The name, first and last name, location, skills and highlight.
    """
    location = report.identity.location or "" if report else ""
    skills = list(report.identity.skills) if report else []
    highlight = ""
    if report is not None:
        strong = [
            finding.statement
            for finding in report.findings
            if finding.category in HIGHLIGHT_CATEGORIES and finding.tag is not Tag.UNCERTAIN
        ]
        highlight = strong[0] if strong else ""
    parts = name.split()
    return {
        "name": name,
        "first_name": _first_name(name),
        "last_name": parts[-1] if len(parts) > 1 else "",
        "location": location,
        "skills": skills,
        "highlight": highlight,
    }


def _company(profile: CompanyProfile | None, fallback: str) -> dict[str, Any]:
    """Build the company variables that templates can use.

    Args:
        profile: The researched company profile, or None.
        fallback: The company name to use when there is no profile.

    Returns:
        The name, website, category, summary, products and markets. Without a profile
        only the name is filled in.
    """
    if profile is None:
        return {
            "name": fallback,
            "website": "",
            "category": "",
            "summary": "",
            "products": [],
            "markets": [],
        }
    mission = profile.statements(FactKind.MISSION) or profile.statements(FactKind.PROVIDED)
    return {
        "name": profile.name,
        "website": profile.website or "",
        "category": profile.category or "",
        "summary": mission[0] if mission else "",
        "products": profile.products,
        "markets": profile.statements(FactKind.MARKET),
    }


def _team_report(
    report: PersonReport | SkillSearchReport | None, tuning: EmailTuning
) -> dict[str, Any]:
    """Build the report variables used by team templates.

    A person report gives the rating, scepticism, finding counts, requirement coverage
    and top findings. Any other report gives a shortlist of candidates.

    Args:
        report: A person report, a skill search report, or None.
        tuning: The email settings that limit the findings and shortlist names.

    Returns:
        The values for the report variable of a template.
    """
    if isinstance(report, PersonReport):
        rating = report.rating
        assessment = report.requirements
        return {
            "overall": f"{rating.overall:.1f}",
            "raw_overall": f"{rating.raw_overall:.1f}",
            "capped": rating.raw_overall != rating.overall,
            "level": rating.scepticism.level.value,
            "flags": [flag.message for flag in rating.scepticism.flags],
            "findings_count": len(report.findings),
            "supported_count": sum(1 for f in report.findings if f.tag is Tag.SUPPORTED),
            "sources_count": len(report.sources),
            "coverage": round(assessment.coverage * 100) if assessment else None,
            "requirement_lines": [
                f"{item.requirement.description}: {item.status.value}"
                for item in (assessment.results if assessment else [])
            ],
            "top_findings": [
                finding.statement for finding in report.findings[: tuning.top_findings]
            ],
            "candidates": [],
        }
    candidates = report.candidates[: tuning.shortlist_names] if report else []
    return {
        "overall": "",
        "raw_overall": "",
        "capped": False,
        "level": "",
        "flags": [],
        "findings_count": 0,
        "supported_count": 0,
        "sources_count": len(report.sources) if report else 0,
        "coverage": None,
        "requirement_lines": [],
        "top_findings": [],
        "candidates": [
            {
                "name": item.name,
                "rating": f"{item.rating.overall:.1f}",
                "level": item.rating.scepticism.level.value,
            }
            for item in candidates
        ],
    }


def build_context(
    category: DraftCategory,
    *,
    name: str,
    role_title: str | None,
    company: CompanyProfile | None,
    company_name: str | None,
    sender_name: str | None,
    tuning: EmailTuning,
    report: PersonReport | SkillSearchReport | None = None,
    index: int = 1,
    total: int = 1,
) -> dict[str, Any]:
    """Build the variables that an email template can use.

    Args:
        category: The template category; team templates also get a report variable.
        name: The candidate's full name.
        role_title: The job title the email is about, if any.
        company: The researched company profile, if any.
        company_name: The company name given by the user, if any.
        sender_name: The name of the person who sends the email, if any.
        tuning: The email settings.
        report: The research report to summarise for team templates.
        index: The position of this recipient in a batch, starting at 1.
        total: The number of recipients in the batch.

    Returns:
        The template variables: candidate, company, role, sender, today, index and total,
        plus report for team templates.
    """
    person = report if isinstance(report, PersonReport) else None
    employer = company_name or (company.name if company else "") or "our team"
    context: dict[str, Any] = {
        "candidate": _candidate(name, person),
        "company": _company(company, company_name or ""),
        "role": {"title": role_title or ""},
        "sender": {"name": sender_name or "", "company": employer},
        "today": date.today().isoformat(),
        "index": index,
        "total": total,
    }
    if category is DraftCategory.TEAM:
        context["report"] = _team_report(report, tuning)
    return context


def sample_context(category: DraftCategory, tuning: EmailTuning) -> dict[str, Any]:
    """Build made-up variables for testing a template.

    Args:
        category: The template category.
        tuning: The email settings.

    Returns:
        Template variables with a fictional candidate, company and sender.
    """
    profile = CompanyProfile(name="Acme", website="https://acme.example")
    context = build_context(
        category,
        name="Jan Novak",
        role_title="Senior Python engineer",
        company=profile,
        company_name="Acme",
        sender_name="Eva Svobodova",
        tuning=tuning,
    )
    if category is DraftCategory.TEAM:
        context["report"] = {
            **_team_report(None, tuning),
            "overall": "3.6",
            "raw_overall": "3.6",
            "level": "none",
            "findings_count": 5,
            "supported_count": 2,
            "sources_count": 3,
            "top_findings": ["Senior engineer at Acme"],
            "candidates": [{"name": "Jan Novak", "rating": "3.6", "level": "none"}],
        }
    return context


def _footer(template: EmailTemplate, tuning: EmailTuning) -> str:
    """Return the footer source that applies to a template.

    Args:
        template: The template that is rendered.
        tuning: The email settings that hold the footers.

    Returns:
        The team footer for team templates, the outreach footer for outreach, and an
        empty string for everything else.
    """
    if template.category is DraftCategory.TEAM:
        return tuning.team_footer
    return tuning.footer if template.purpose is DraftPurpose.OUTREACH else ""


def _render(source: str, context: dict[str, Any], tuning: EmailTuning) -> str:
    """Render one template source in the sandbox.

    Args:
        source: The Jinja source of a subject, body or footer.
        context: The variables available to the template.
        tuning: The email settings.

    Returns:
        The rendered text.

    Raises:
        InvalidRequest: when loops are nested too deeply or the template fails to parse
            or render.
    """
    environment = _environment(tuning)
    try:
        tree = environment.parse(source)
        # The depth is checked on the parsed tree before rendering, so deeply nested loops never
        # run.
        if _loop_depth(tree) > tuning.max_loop_depth:
            raise InvalidRequest(f"Loops may be nested at most {tuning.max_loop_depth} deep.")
        return environment.from_string(tree).render(**context)
    except TemplateError as error:
        raise InvalidRequest(f"The template could not be rendered: {error}") from error


def render_draft(
    template: EmailTemplate,
    context: dict[str, Any],
    tuning: EmailTuning,
    recipient: str | None = None,
) -> Draft:
    """Render a template into a draft email.

    The subject becomes one line and the body is trimmed. A footer is added after a
    "--" line when the template needs one. Both parts are cut to the configured limits.

    Args:
        template: The template to render.
        context: The variables available to the template.
        tuning: The email settings.
        recipient: The address to put on the draft, if known.

    Returns:
        The rendered draft.

    Raises:
        InvalidRequest: when the template cannot be rendered.
    """
    subject = " ".join(_render(template.subject, context, tuning).split())
    body = _render(template.body, context, tuning).replace("\r\n", "\n").strip()
    footer = _footer(template, tuning)
    if footer:
        body = f"{body}\n\n--\n{_render(footer, context, tuning).strip()}"
    return Draft(
        recipient=recipient,
        subject=subject[: tuning.max_subject_chars],
        body=body[: tuning.max_body_chars],
    )


def validate_template(template: EmailTemplate, tuning: EmailTuning) -> None:
    """Check that a template is allowed and renders with sample data.

    Args:
        template: The template to check.
        tuning: The email settings that hold the limits and forbidden tags.

    Raises:
        InvalidRequest: when the id is malformed, the subject or body is empty or too
            long, the subject has several lines, a forbidden tag is used, or rendering
            fails.
    """
    if not TEMPLATE_ID.fullmatch(template.id):
        raise InvalidRequest("The template id must be 3 to 40 lowercase letters, digits or dashes.")
    if not template.subject.strip() or not template.body.strip():
        raise InvalidRequest("A template needs a subject and a body.")
    if len(template.subject) > tuning.max_subject_chars or "\n" in template.subject:
        raise InvalidRequest(
            f"The subject must be one line of at most {tuning.max_subject_chars} characters."
        )
    if len(template.body) > tuning.max_body_chars:
        raise InvalidRequest(f"The body may be at most {tuning.max_body_chars} characters.")
    forbidden = _forbidden(tuning.forbidden_tags)
    if forbidden.search(template.subject) or forbidden.search(template.body):
        raise InvalidRequest(
            f"Templates may not use these tags: {', '.join(tuning.forbidden_tags)}."
        )
    render_draft(template, sample_context(template.category, tuning), tuning)


class TemplateStore:
    """Hold the built-in templates and the custom ones saved as JSON files.

    Each custom template is a file named after its id in the template directory. Files
    that are too large, unreadable, invalid, named differently from their id, or that
    clash with a built-in id are skipped.
    """

    def __init__(self, tuning: EmailTuning):
        """Initialize the store with the email settings.

        Args:
            tuning: The email settings that give the template directory and the limits.
        """
        self._tuning = tuning
        self._directory = tuning.template_dir
        self._builtin = {template.id: template for template in builtin_templates()}

    def _path(self, identifier: str) -> Path:
        """Return the file path of a custom template.

        Args:
            identifier: The template id.

        Returns:
            The path of the JSON file inside the template directory.
        """
        return self._directory / f"{identifier}.json"

    def custom(self) -> list[EmailTemplate]:
        """Return the valid custom templates from disk.

        Only up to the configured number of files are read.

        Returns:
            The custom templates, sorted by file name.
        """
        if not self._directory.is_dir():
            return []
        found: list[EmailTemplate] = []
        for path in sorted(self._directory.glob("*.json"))[: self._tuning.max_templates]:
            try:
                if not path.is_file() or path.stat().st_size > self._tuning.max_file_bytes:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                template = EmailTemplate.model_validate({**data, "builtin": False})
            except OSError, ValueError, ValidationError:
                continue
            if template.id == path.stem and template.id not in self._builtin:
                found.append(template)
        return found

    def all(self) -> list[EmailTemplate]:
        """Return every template.

        Returns:
            The built-in templates followed by the custom ones.
        """
        return [*self._builtin.values(), *self.custom()]

    def builtin_ids(self) -> frozenset[str]:
        """Return the ids of the built-in templates.

        Returns:
            The set of built-in ids.
        """
        return frozenset(self._builtin)

    def get(self, identifier: str) -> EmailTemplate | None:
        """Find a template by id.

        Args:
            identifier: The template id.

        Returns:
            The built-in or custom template, or None when none has that id.
        """
        if identifier in self._builtin:
            return self._builtin[identifier]
        return next((item for item in self.custom() if item.id == identifier), None)

    def check(self, template: EmailTemplate, pending: int = 0) -> EmailTemplate:
        """Validate a template for saving without writing it.

        Args:
            template: The template to check.
            pending: How many other new templates are being saved in the same operation.

        Returns:
            A copy of the template that is marked as not built in.

        Raises:
            InvalidRequest: when the template is invalid, uses a built-in id, or would go over
                the template limit.
        """
        saved = template.model_copy(update={"builtin": False})
        validate_template(saved, self._tuning)
        if saved.id in self._builtin:
            raise InvalidRequest("Built-in templates cannot be changed; save a copy instead.")
        existing = {item.id for item in self.custom()}
        if saved.id not in existing and len(existing) + pending >= self._tuning.max_templates:
            raise InvalidRequest("The template limit was reached; delete one first.")
        return saved

    def save(self, template: EmailTemplate) -> EmailTemplate:
        """Validate a custom template and write it to disk.

        The template directory is created when it is missing, and a template with the same
        id is overwritten.

        Args:
            template: The template to save.

        Returns:
            The saved template, marked as not built in.

        Raises:
            InvalidRequest: when the template does not pass the checks.
        """
        saved = self.check(template)
        self._directory.mkdir(parents=True, exist_ok=True)
        payload = saved.model_dump(mode="json", exclude={"builtin"})
        self._path(saved.id).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return saved

    def delete(self, identifier: str) -> bool:
        # The id pattern check keeps a crafted id from pointing outside the template directory.
        """Delete a custom template.

        Args:
            identifier: The id of the template.

        Returns:
            True when a file was removed, and False for a built-in id, a malformed id or a
            missing template.
        """
        # The id pattern check keeps a crafted id from pointing outside the template directory.
        if identifier in self._builtin or not TEMPLATE_ID.fullmatch(identifier):
            return False
        path = self._path(identifier)
        existed = path.is_file()
        path.unlink(missing_ok=True)
        return existed
