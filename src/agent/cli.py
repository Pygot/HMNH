# src/agent/cli.py
from agent.models import (
    ClarificationAnswer,
    ClarificationQuestion,
    CompanyInput,
    Goal,
    Identity,
    Network,
    PersonReport,
    Priority,
    Requirement,
    RequirementKind,
    RequirementsAssessment,
    ResearchOptions,
    ResearchRequest,
    ScepticismLevel,
    ScepticismMode,
    SkillSearchReport,
    SkillSearchRequest,
    Strictness,
    Tag,
)
from agent.envfile import (
    env_source,
    env_target,
    env_warnings,
    load_settings,
)
from agent.export import (
    delete_outputs,
    export_report,
    ExportedReport,
)
from agent.connections import (
    Connections,
    SECTIONS,
)
from agent.errors import (
    AgentError,
    ComplianceRefusal,
)
from agent.pipeline import (
    apply_answers,
    Pipeline,
)
from agent.runtime import (
    open_runtime,
    ServiceStatus,
)
from collections.abc import (
    Callable,
    Iterator,
)
from typing import (
    Annotated,
    NoReturn,
)
from agent.logs import configure_logging
from agent.text import describe_errors
from contextlib import contextmanager
from pydantic import ValidationError
from agent.compliance import NOTICE
from agent.config import Settings
from agent.forms import Field
from pathlib import Path

import asyncio
import typer

EXIT_ERROR = 2
EXIT_REFUSED = 3
EXIT_NEEDS_INPUT = 4
REQUIREMENT_SEPARATOR = ":"
REQUIREMENT_PARTS = 5
REQUIREMENT_FORMAT = "kind:label[:level[:years[:priority]]]"
ENVIRONMENT_HELP = (
    "Run 'agent init' to be asked for the values and have them saved, or set environment "
    "variables or a private .env file yourself. "
    "LLM (optional): LLM_API_KEY alone is enough (sk-ant-... selects Anthropic, other keys "
    "OpenAI), optionally LLM_MODEL and LLM_BASE_URL; for Ollama set LLM_OLLAMA_SCHEME, "
    "LLM_OLLAMA_HOST and LLM_OLLAMA_PORT instead; with only APIFY_TOKEN the AI models of your "
    "Apify account are used (LLM_PROVIDER forces a choice). "
    "Search: BRAVE_API_KEY or APIFY_TOKEN (SEARCH_PROVIDER forces a choice). "
    "Profiles: APIFY_TOKEN reads LinkedIn, Facebook and Instagram through Apify actors. "
    "Web interface: the first start prints a setup link to create the administrator; "
    "WEB_ACCESS_TOKEN (at least 20 characters) and WEB_API_TOKEN are optional. "
    "Voice: optional ELEVENLABS_API_KEY. Email: optional SMTP_* values. "
    "Any tuning value can be overridden, for example SCORING__THRESHOLD=0.8. "
    "Exit codes: 2 error, 3 refused, 4 clarification needed."
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Research a person's public professional profile for a stated goal.",
    epilog=ENVIRONMENT_HELP,
)
GoalOption = Annotated[Goal, typer.Option("--goal", "-g", help="hiring, sales or due_diligence.")]
ConfirmOption = Annotated[
    bool,
    typer.Option(
        "--confirm-lawful-purpose",
        help="Confirm that you have a lawful purpose for this research.",
    ),
]
OutputOption = Annotated[
    Path | None, typer.Option("--output-dir", help="Directory for exported reports.")
]
InteractiveOption = Annotated[bool, typer.Option("--interactive/--no-interactive")]
SourceOption = Annotated[
    list[Network] | None,
    typer.Option("--source", help="Source to use (repeatable). Default: all sources."),
]
StrictnessOption = Annotated[
    Strictness | None,
    typer.Option("--strictness", help="Profile matching preset: lenient, balanced or strict."),
]
ThresholdOption = Annotated[
    float | None, typer.Option("--threshold", min=0, max=1, help="Minimum match score.")
]
MarginOption = Annotated[
    float | None, typer.Option("--margin", min=0, max=1, help="Required lead over the runner up.")
]
WebPagesOption = Annotated[
    int | None, typer.Option("--max-web-pages", min=0, max=20, help="Open web pages to read.")
]
ScepticismOption = Annotated[
    ScepticismMode, typer.Option("--scepticism", help="standard or strict.")
]
CompanyOption = Annotated[
    str | None,
    typer.Option("--company", help="Your hiring company, so candidates are rated in its context."),
]
CompanyWebsiteOption = Annotated[
    str | None, typer.Option("--company-website", help="Public website of your company.")
]
CompanyAboutOption = Annotated[
    str | None,
    typer.Option("--company-about", help="One sentence about what your company builds."),
]
RequireOption = Annotated[
    list[str] | None,
    typer.Option(
        "--require",
        help=f"Candidate requirement as {REQUIREMENT_FORMAT} (repeatable). Kinds: "
        f"{', '.join(kind.value for kind in RequirementKind)}. Priority: must or nice. "
        "Examples: language:English:C1 or skill:Python::5:nice.",
    ),
]


def _fail(message: str, code: int) -> NoReturn:
    """Print an error message to stderr and exit.

    Args:
        message: The text shown after the 'error:' prefix.
        code: The process exit code.

    Raises:
        Exit: always, with the given code.
    """
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


@contextmanager
def guarded() -> Iterator[None]:
    """Turn project errors inside the block into CLI error exits.

    A compliance refusal exits with code 3. Any other agent error and any validation
    error exit with code 2 after printing a readable message to stderr.
    """
    try:
        yield
    # Listed before AgentError because a refusal is a subclass and has its own exit code.
    except ComplianceRefusal as error:
        _fail(str(error), EXIT_REFUSED)
    except AgentError as error:
        _fail(str(error), EXIT_ERROR)
    except ValidationError as error:
        _fail(describe_errors(error), EXIT_ERROR)


def _progress(stage: str) -> None:
    """Print a progress stage line to stderr.

    Args:
        stage: The name of the stage that has started.
    """
    typer.echo(f"... {stage}", err=True)


def _load_settings() -> Settings:
    """Load the settings and set up logging.

    Prints environment file warnings to stderr first, then configures logging with the
    secret values of the settings.

    Returns:
        The loaded settings.
    """
    for warning in env_warnings(env_source()):
        typer.echo(f"warning: {warning}", err=True)
    settings = load_settings()
    configure_logging(settings.secret_values(), settings.log)
    return settings


def _options(
    sources: list[Network] | None,
    strictness: Strictness | None,
    threshold: float | None,
    margin: float | None,
    max_web_pages: int | None,
    scepticism: ScepticismMode,
) -> ResearchOptions:
    """Build research options from the command line values.

    Args:
        sources: The chosen networks, or None to use all of them.
        strictness: The profile matching preset, if any.
        threshold: The minimum match score, if overridden.
        margin: The required lead over the runner up, if overridden.
        max_web_pages: The number of web pages to read, if overridden.
        scepticism: The scepticism mode.

    Returns:
        The research options.
    """
    return ResearchOptions(
        networks=sources or list(Network),
        strictness=strictness,
        threshold=threshold,
        margin=margin,
        max_web_pages=max_web_pages,
        scepticism=scepticism,
    )


def _company(name: str | None, website: str | None, about: str | None) -> CompanyInput | None:
    """Build the optional company description from the command line values.

    Args:
        name: The company name, or None when no company is given.
        website: The public website of the company.
        about: One sentence about what the company builds.

    Returns:
        The company input, or None when no name is given.

    Raises:
        BadParameter: when a website or description is given without a name.
    """
    if name is None:
        if website or about:
            raise typer.BadParameter("--company-website and --company-about need --company.")
        return None
    return CompanyInput(name=name, website=website, description=about)


def _requirements(specs: list[str] | None) -> list[Requirement]:
    """Parse requirement specifications into requirements.

    Each spec has the form kind:label[:level[:years[:priority]]]. The priority
    defaults to must.

    Args:
        specs: The raw values of the --require option, or None.

    Returns:
        The parsed requirements in the order given.

    Raises:
        BadParameter: when a spec has no label, an unknown kind or priority, or years
            that are not a valid number.
    """
    parsed: list[Requirement] = []
    for spec in specs or []:
        # The split is limited so that any extra colons stay inside the last part.
        parts = [part.strip() for part in spec.split(REQUIREMENT_SEPARATOR, REQUIREMENT_PARTS - 1)]
        if len(parts) < 2 or not parts[1]:
            raise typer.BadParameter(f"Write requirements as {REQUIREMENT_FORMAT}, not '{spec}'.")
        kind, label, level, years, priority = parts + [""] * (REQUIREMENT_PARTS - len(parts))
        try:
            parsed.append(
                Requirement(
                    kind=RequirementKind(kind.lower()),
                    label=label,
                    level=level or None,
                    minimum_years=int(years) if years else None,
                    priority=Priority(priority.lower() or Priority.MUST.value),
                )
            )
        except ValueError as error:
            raise typer.BadParameter(f"Invalid requirement '{spec}'.") from error
    return parsed


def _echo_requirements(assessment: RequirementsAssessment | None, prefix: str = "") -> None:
    """Print a one-line summary of how well the requirements are covered.

    Nothing is printed without an assessment. Must have requirements that lack
    evidence are listed on a second line.

    Args:
        assessment: The requirements assessment, or None.
        prefix: Text printed at the start of every line.
    """
    if assessment is None:
        return
    typer.echo(
        f"{prefix}Requirements: {assessment.coverage * 100:.0f}% covered (met {assessment.met}, "
        f"partial {assessment.partial}, unmet {assessment.unmet}, unknown {assessment.unknown})"
    )
    if assessment.must_missing:
        typer.echo(f"{prefix}  Must have without evidence: {', '.join(assessment.must_missing)}")


def _require_purpose(flag: bool, interactive: bool) -> None:
    """Print the compliance notice and make sure a lawful purpose is confirmed.

    Args:
        flag: Whether --confirm-lawful-purpose was passed.
        interactive: Whether the user may be asked to confirm.

    Raises:
        ComplianceRefusal: when the purpose is not confirmed by the flag or the prompt.
    """
    typer.echo(f"NOTICE: {NOTICE}\n", err=True)
    if flag:
        return
    if not interactive:
        raise ComplianceRefusal("Pass --confirm-lawful-purpose to confirm a lawful purpose.")
    if not typer.confirm("Do you confirm that you have a lawful purpose for this research?"):
        raise ComplianceRefusal(
            "A lawful purpose must be confirmed before any research is performed."
        )


def _warn_unavailable(pipeline: Pipeline) -> None:
    """Warn about networks whose profiles cannot be read.

    Args:
        pipeline: The pipeline that knows which networks are unavailable.
    """
    missing = [network.value for network in pipeline.unavailable_networks]
    if missing:
        typer.echo(
            f"warning: social profiles will not be read for {', '.join(missing)}; set APIFY_TOKEN "
            "to enable them.",
            err=True,
        )


def _ask(question: ClarificationQuestion) -> ClarificationAnswer:
    """Ask the user to answer one clarification question.

    Lists the options and prompts until the input is valid: a listed number picks
    that profile, 0 rejects all of them and d asks for a city and employer to narrow
    the search.

    Args:
        question: The question with its candidate profiles.

    Returns:
        The answer of the user.
    """
    typer.echo(f"\n{question.text}", err=True)
    for number, option in enumerate(question.options, 1):
        score = f"{option.score:.2f}: {option.evidence}"
        typer.echo(f"  {number}. {option.title} ({score})\n     {option.url}", err=True)
    typer.echo("  0. None of these\n  d. Add a city or employer to narrow the search", err=True)
    while True:
        choice = typer.prompt("Choice").strip().lower()
        if choice == "0":
            return ClarificationAnswer(network=question.network)
        if choice.isdigit() and 1 <= int(choice) <= len(question.options):
            chosen = question.options[int(choice) - 1].url
            return ClarificationAnswer(network=question.network, chosen_url=chosen)
        if choice == "d":
            city = typer.prompt("City", default="", show_default=False).strip() or None
            employer = typer.prompt("Employer", default="", show_default=False).strip() or None
            try:
                answer = ClarificationAnswer(
                    network=question.network, location=city, employer=employer
                )
            except ValidationError as error:
                typer.echo(f"invalid input: {describe_errors(error)}", err=True)
                continue
            if city or employer:
                return answer
        typer.echo("Please enter a listed number, 0 or d.", err=True)


async def _research(
    settings: Settings,
    build_request: Callable[[Identity], ResearchRequest],
    source: Identity | Path,
    interactive: bool,
) -> PersonReport:
    """Run the research pipeline and settle any clarification questions.

    When the source is a CV path, its identity is extracted first and printed to
    stderr without links. In interactive mode the user answers the questions and the
    research is repeated with the answers applied.

    Args:
        settings: The application settings.
        build_request: Creates the research request for an identity.
        source: The identity to research, or the path of a CV to read it from.
        interactive: Whether the user may be asked clarification questions.

    Returns:
        The finished person report.

    Raises:
        Exit: with code 4 when questions are open and interactive mode is off.
    """
    async with open_runtime(settings) as runtime:
        pipeline = runtime.pipeline
        _warn_unavailable(pipeline)
        if isinstance(source, Path):
            data = await asyncio.to_thread(source.read_bytes)
            identity = await pipeline.identity_from_cv(data, source.name)
            typer.echo(f"CV identity: {identity.model_dump_json(exclude={'links'})}", err=True)
        else:
            identity = source
        request = build_request(identity)
        while True:
            result = await pipeline.research(request, _progress)
            if isinstance(result, PersonReport):
                return result
            if not interactive:
                for question in result.questions:
                    typer.echo(f"clarification needed ({question.network.value}): {question.text}")
                    for option in question.options:
                        typer.echo(f"  {option.url}  {option.title}")
                raise typer.Exit(EXIT_NEEDS_INPUT)
            request = apply_answers(request, [_ask(question) for question in result.questions])


async def _skill_search(settings: Settings, request: SkillSearchRequest) -> SkillSearchReport:
    """Run a skill search with a freshly opened runtime.

    Args:
        settings: The application settings.
        request: The skill search request.

    Returns:
        The skill search report.
    """
    async with open_runtime(settings) as runtime:
        _warn_unavailable(runtime.pipeline)
        return await runtime.pipeline.skill_search(request, _progress)


async def _status(settings: Settings) -> ServiceStatus:
    """Return the service status from a freshly opened runtime.

    Args:
        settings: The application settings.

    Returns:
        The status of the configured services.
    """
    async with open_runtime(settings) as runtime:
        return await runtime.status()


def _announce(exported: ExportedReport) -> None:
    """Print the locations of the exported report files.

    Args:
        exported: The paths of the exported Markdown and JSON files.
    """
    typer.echo(f"Report:  {exported.markdown}")
    typer.echo(f"Data:    {exported.json}")


def _echo_scepticism(report: PersonReport) -> None:
    """Print how scepticism changed the rating.

    Nothing is printed when the scepticism level is none.

    Args:
        report: The person report with the rating.
    """
    scepticism = report.rating.scepticism
    if scepticism.level is ScepticismLevel.NONE:
        return
    typer.echo(
        f"Scepticism: {scepticism.level.value}, rating {report.rating.raw_overall:.1f} "
        f"reduced to {report.rating.overall:.1f}"
    )
    for flag in scepticism.flags:
        typer.echo(f"  - {flag.message}")


@app.command()
def research(
    goal: GoalOption,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Name or nickname.")] = None,
    alias: Annotated[list[str] | None, typer.Option("--alias", help="Other names.")] = None,
    city: Annotated[str | None, typer.Option("--city", help="City or region only.")] = None,
    employer: Annotated[list[str] | None, typer.Option("--employer")] = None,
    role: Annotated[list[str] | None, typer.Option("--role")] = None,
    skill: Annotated[list[str] | None, typer.Option("--skill")] = None,
    cv: Annotated[
        Path | None,
        typer.Option("--cv", exists=True, dir_okay=False, readable=True, help="PDF or DOCX CV."),
    ] = None,
    focus: Annotated[str | None, typer.Option("--focus", help="Role or need to rate.")] = None,
    company: CompanyOption = None,
    company_website: CompanyWebsiteOption = None,
    company_about: CompanyAboutOption = None,
    require: RequireOption = None,
    source: SourceOption = None,
    strictness: StrictnessOption = None,
    threshold: ThresholdOption = None,
    margin: MarginOption = None,
    max_web_pages: WebPagesOption = None,
    scepticism: ScepticismOption = ScepticismMode.STANDARD,
    output_dir: OutputOption = None,
    confirm_lawful_purpose: ConfirmOption = False,
    interactive: InteractiveOption = True,
) -> None:
    """Research one person's public profile and export a report."""
    if name is not None and cv is not None:
        raise typer.BadParameter("Provide either --name or --cv, not both.")
    if cv is not None and any([alias, city, employer, role, skill]):
        raise typer.BadParameter("--cv cannot be combined with identity options.")
    with guarded():
        subject: Identity | Path
        if name is not None:
            subject = Identity(
                name=name,
                aliases=alias or [],
                location=city,
                employers=employer or [],
                roles=role or [],
                skills=skill or [],
            )
        elif cv is not None:
            subject = cv
        else:
            raise typer.BadParameter("Provide --name or --cv.")
        settings = _load_settings()
        _require_purpose(confirm_lawful_purpose, interactive)
        options = _options(source, strictness, threshold, margin, max_web_pages, scepticism)
        hiring_company = _company(company, company_website, company_about)
        requirements = _requirements(require)

        def build_request(identity: Identity) -> ResearchRequest:
            """Create the research request for an identity.

            The request carries the confirmed purpose and the options chosen on the command
            line.

            Args:
                identity: The identity to research.

            Returns:
                The research request.
            """
            return ResearchRequest(
                goal=goal,
                identity=identity,
                focus=focus,
                purpose_confirmed=True,
                company=hiring_company,
                requirements=requirements,
                options=options,
            )

        report = asyncio.run(_research(settings, build_request, subject, interactive))
        exported = export_report(report, output_dir or settings.output_dir)
        counts = {tag: sum(1 for f in report.findings if f.tag is tag) for tag in Tag}
        typer.echo(f"Overall rating: {report.rating.overall:.1f} / 5")
        typer.echo(
            f"Findings: {len(report.findings)} (supported {counts[Tag.SUPPORTED]}, "
            f"single-source {counts[Tag.SINGLE_SOURCE]}, uncertain {counts[Tag.UNCERTAIN]})"
        )
        _echo_requirements(report.requirements)
        _echo_scepticism(report)
        typer.echo(f"Limitations listed: {len(report.limitations)}")
        _announce(exported)


@app.command("skill-search")
def skill_search(
    goal: GoalOption,
    skill: Annotated[str, typer.Option("--skill", help="Skill to search for.")],
    location: Annotated[str, typer.Option("--location", help="City or region.")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=10)] = 5,
    company: CompanyOption = None,
    company_website: CompanyWebsiteOption = None,
    company_about: CompanyAboutOption = None,
    require: RequireOption = None,
    source: SourceOption = None,
    scepticism: ScepticismOption = ScepticismMode.STANDARD,
    output_dir: OutputOption = None,
    confirm_lawful_purpose: ConfirmOption = False,
    interactive: InteractiveOption = True,
) -> None:
    """Find and rank people who have a skill in a location."""
    with guarded():
        settings = _load_settings()
        _require_purpose(confirm_lawful_purpose, interactive)
        options = _options(source, None, None, None, None, scepticism)
        request = SkillSearchRequest(
            goal=goal,
            skill=skill,
            location=location,
            limit=limit,
            purpose_confirmed=True,
            company=_company(company, company_website, company_about),
            requirements=_requirements(require),
            options=options,
        )
        report = asyncio.run(_skill_search(settings, request))
        exported = export_report(report, output_dir or settings.output_dir)
        for candidate in report.candidates:
            marker = "  <- best" if candidate.best else ""
            caution = (
                f"  scepticism {candidate.rating.scepticism.level.value}"
                if candidate.rating.scepticism.level is not ScepticismLevel.NONE
                else ""
            )
            typer.echo(
                f"{candidate.rank}. {candidate.name}  {candidate.rating.overall:.1f} / 5  "
                f"{candidate.profile_url}{marker}{caution}"
            )
            _echo_requirements(candidate.requirements, "   ")
        _announce(exported)


@app.command()
def status() -> None:
    """Show the configured providers and the Apify usage."""
    with guarded():
        report = asyncio.run(_status(_load_settings()))
        typer.echo(f"LLM:     {report.llm_provider} ({report.llm_model or 'auto'})")
        typer.echo(f"Search:  {report.search_provider}")
        missing = ", ".join(report.unavailable_networks) or "none"
        typer.echo(f"Social profiles not readable: {missing}")
        typer.echo(f"Voice:   {'enabled' if report.voice_enabled else 'disabled'}")
        if report.apify_error:
            typer.echo(f"Apify:   {report.apify_error}")
        elif report.apify_remaining_usd is not None:
            typer.echo(
                f"Apify:   {report.apify_monthly_usage_usd:.2f} USD used this month, "
                f"{report.apify_remaining_usd:.2f} USD remaining"
            )


@app.command("delete-outputs")
def delete_outputs_command(
    output_dir: OutputOption = None,
    yes: Annotated[bool, typer.Option("--yes", help="Delete without asking.")] = False,
) -> None:
    """Delete all exported reports from the output directory."""
    with guarded():
        directory = output_dir or load_settings().output_dir
        if not yes and not typer.confirm(f"Delete all exported reports in {directory}?"):
            raise typer.Exit(1)
        removed = delete_outputs(directory)
        typer.echo(f"Deleted {len(removed)} file(s) from {directory}.")


def _ask_field(field: Field, current: str) -> str | None:
    """Prompt for the value of one connection setting.

    Secret values are read without echo. The choices of a field are shown with an
    empty choice labelled automatic.

    Args:
        field: The setting to ask for.
        current: The value shown as the default, or a marker for a saved secret.

    Returns:
        The entered value, an empty string when it was cleared, or None when a secret
        was left empty so the saved one is kept.
    """
    if field.secret:
        value = typer.prompt(
            f"  {field.label} (empty keeps the saved one)" if current else f"  {field.label}",
            default="",
            hide_input=True,
            show_default=False,
        )
        return value or None
    options = " / ".join(value or "automatic" for value, _ in field.choices)
    label = f"  {field.label} ({options})" if field.choices else f"  {field.label}"
    value = typer.prompt(label, default=current, show_default=bool(current))
    return value.strip() or ""


@app.command()
def init() -> None:
    """Ask for the connections and save them to the private settings file."""
    with guarded():
        settings = load_settings()
        manager = Connections()
        present = manager.present(settings)
        changes: dict[str, str | None] = {}
        typer.echo("Everything is optional. Press Enter to skip or to keep the current value.")
        for section in SECTIONS:
            configured = any(field.key in present for field in section.fields)
            typer.echo(f"\n{section.title}: {section.intro}")
            if not typer.confirm(
                f"Set up {section.title.lower()}?", default=not configured and section.name == "llm"
            ):
                continue
            for field in section.fields:
                shown = "" if field.secret else present.get(field.key, "")
                answer = _ask_field(
                    field, "saved" if field.secret and field.key in present else shown
                )
                if answer is None:
                    continue
                if field.secret or answer != shown:
                    changes[field.key] = answer or None
        if not changes:
            typer.echo("\nNothing to save.")
            return
        _, shadowed = manager.apply(changes)
        typer.echo(f"\nSaved {len(changes)} settings to {env_target()}.")
        for key in shadowed:
            typer.echo(
                f"warning: {key} is also set in the environment and that value wins.", err=True
            )
        typer.echo("Start the web interface with: agent serve")


@app.command()
def serve(
    host: Annotated[str | None, typer.Option("--host")] = None,
    port: Annotated[int | None, typer.Option("--port", min=1, max=65535)] = None,
) -> None:
    """Start the web interface."""
    with guarded():
        from agent.web.server import run_server

        settings = _load_settings()
        run_server(settings, host, port)
