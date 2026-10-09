# src/agent/config.py
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
    SecretStr,
)
from agent.errors import (
    ConfigError,
    NotConfigured,
)
from agent.permissions import (
    ALL as ALL_PERMISSIONS,
    is_known,
)
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)
from typing import (
    Any,
    Literal,
)
from string import Formatter
from pathlib import Path

import re

ACTOR_ID = re.compile(r"^[A-Za-z0-9_.-]+[/~][A-Za-z0-9_.-]+$")
HOST = re.compile(r"^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])$")
FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
MAIL_ADDRESS = re.compile(r"^[^@\s<>,;\"']{1,64}@[^@\s<>,;\"']{1,255}\.[^@\s<>,;\"']{2,63}$")
VOICE_ID = re.compile(r"^[A-Za-z0-9]{10,40}$")
TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
BUDDY_PLACEHOLDERS = frozenset({"sources", "findings", "count"})
BUDDY_LINES = (
    "greeting",
    "working",
    "progress",
    "away_back",
    "away_quiet",
    "done",
    "question",
    "failed",
    "idle",
    "focus_start",
    "focus_break",
    "focus_end",
    "focus_stop",
    "detour",
)
CHAT_PLACEHOLDERS = frozenset({"task", "context", "title", "stage", "error", "count"})
CHAT_LINES = (
    "welcome",
    "need_subject",
    "proposal",
    "started",
    "declined",
    "busy",
    "ask",
    "failed",
    "remembered",
    "misunderstood",
    "model_down",
    "not_asking",
    "cv_received",
    "limit",
    "interrupted",
    "tie_hello",
    "not_allowed",
    "not_ready",
)
ENV_FILE_VARIABLE = "AGENT_ENV_FILE"
DEFAULT_ENV_FILE = ".env"
MAX_ENV_FILE_BYTES = 65_536
LLMProvider = Literal["anthropic", "openai", "ollama", "apify", "claude"]
SearchProviderName = Literal["brave", "apify"]


class Tuning(BaseModel):
    """A frozen group of tuning values that rejects unknown fields.

    Every tuning group below derives from this class, so values cannot be changed after
    loading and a misspelled key fails loudly instead of being ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class Endpoints(Tuning):
    """Base URLs and API version strings of the external services."""

    brave_search: str = "https://api.search.brave.com/res/v1/web/search"
    apify_api: str = "https://api.apify.com/v2"
    anthropic_api: str = "https://api.anthropic.com"
    anthropic_version: str = "2023-06-01"
    openai_api: str = "https://api.openai.com/v1"
    apify_ai_api: str = "https://openrouter.apify.actor/api/v1"
    elevenlabs_api: str = "https://api.elevenlabs.io/v1"


class Defaults(Tuning):
    """Default models, key prefix and Ollama address used when none is configured."""

    anthropic_model: str = "claude-sonnet-5-5"
    openai_model: str = "gpt-5-mini"
    apify_ai_model: str = "openrouter/auto"
    anthropic_key_prefix: str = "sk-ant-"
    ollama_scheme: Literal["http", "https"] = "http"
    ollama_host: str = "localhost"
    ollama_port: int = Field(default=11434, ge=1, le=65535)


class HttpTuning(Tuning):
    """Timeouts, connection limits, request spacing and cache sizes for HTTP calls."""

    timeout_seconds: float = Field(default=20.0, gt=0)
    llm_timeout_seconds: float = Field(default=180.0, gt=0)
    apify_timeout_seconds: float = Field(default=300.0, gt=0)
    apify_wait_seconds: int = Field(default=280, ge=1, le=3600)
    max_connections: int = Field(default=20, ge=1)
    max_keepalive_connections: int = Field(default=5, ge=0)
    brave_interval_seconds: float = Field(default=1.1, ge=0)
    user_agent_name: str = "person-research-agent"
    cache_max_entries: int = Field(default=512, ge=1)
    cache_ttl_seconds: int = Field(default=900, ge=0, le=86_400)


class LlmTuning(Tuning):
    """Output limits, retry count and ping texts for language model calls."""

    max_output_tokens: int = Field(default=4096, ge=256)
    apify_ai_output_tokens: int = Field(default=2048, ge=256, le=2048)
    ollama_context: int = Field(default=8192, ge=2048)
    claude_command: str = "claude"
    claude_system_file: str = "system.txt"
    attempts: int = Field(default=2, ge=1, le=5)
    error_excerpt_chars: int = Field(default=500, ge=50)
    ping_system: str = "Answer with the single word ok."
    ping_user: str = "Are you there?"


class CvTuning(Tuning):
    """Size, page, link and archive limits for uploaded CV files."""

    max_bytes: int = Field(default=1_048_576, ge=1024, le=20_000_000)
    max_pages: int = Field(default=15, ge=1)
    max_chars: int = Field(default=14_000, ge=1000)
    max_zip_entries: int = Field(default=1000, ge=1)
    max_uncompressed_bytes: int = Field(default=25_000_000, ge=1024)
    max_links: int = Field(default=25, ge=1)


class FetchTuning(Tuning):
    """Limits and allow lists for fetching public web pages."""

    page_types: tuple[str, ...] = ("text/html", "application/xhtml+xml", "text/plain")
    max_page_bytes: int = Field(default=1_500_000, ge=1024)
    max_robots_bytes: int = Field(default=500_000, ge=1024)
    max_redirects: int = Field(default=4, ge=0, le=10)
    max_text_chars: int = Field(default=20_000, ge=1000)
    allowed_ports: tuple[int, ...] = (80, 443)
    min_interval_seconds: float = Field(default=1.0, ge=0)
    robots_ttl_seconds: int = Field(default=3600, ge=0)
    total_seconds: float = Field(default=30.0, ge=1)
    charsets: tuple[str, ...] = (
        "utf-8",
        "ascii",
        "utf-16",
        "utf-32",
        "iso8859-1",
        "iso8859-2",
        "iso8859-5",
        "iso8859-7",
        "iso8859-9",
        "iso8859-15",
        "cp1250",
        "cp1251",
        "cp1252",
        "cp1253",
        "cp1254",
        "cp1257",
        "koi8-r",
        "shift_jis",
        "euc_jp",
        "euc_kr",
        "gb2312",
        "gbk",
        "gb18030",
        "big5",
    )


class CollectTuning(Tuning):
    """Limits on the size and nesting depth of collected data."""

    max_chars: int = Field(default=20_000, ge=1000)
    max_depth: int = Field(default=4, ge=1, le=8)


class DiscoveryTuning(Tuning):
    """Query and result limits for finding candidate matches."""

    results_per_query: int = Field(default=10, ge=1, le=100)
    max_names: int = Field(default=3, ge=1)
    max_names_length: int = Field(default=200, ge=20)
    hits_per_web_query: int = Field(default=5, ge=1)
    max_web_pages: int = Field(default=6, ge=0, le=20)
    brave_max_count: int = Field(default=20, ge=1)
    max_query_chars: int = Field(default=400, ge=20)
    brave_max_query_words: int = Field(default=50, ge=4)
    apify_max_query_words: int = Field(default=32, ge=4)
    candidates_per_network: int = Field(default=3, ge=1, le=10)
    test_query: str = "public company news"


class SummaryTuning(Tuning):
    """Limits for turning source text into short claims."""

    max_source_chars: int = Field(default=10_000, ge=1000)
    max_claims_per_source: int = Field(default=12, ge=1)
    max_statement_chars: int = Field(default=300, ge=40)
    min_year: int = Field(default=1950, ge=1900)
    concurrency: int = Field(default=3, ge=1, le=16)


class MatchPreset(Tuning):
    """A named pair of match threshold and margin, each between 0 and 1."""

    threshold: float = Field(ge=0, le=1)
    margin: float = Field(ge=0, le=1)


class ScoringTuning(Tuning):
    """Weights, thresholds and presets for scoring how well a candidate matches."""

    name_weight: float = Field(default=0.45, gt=0)
    employer_weight: float = Field(default=0.25, gt=0)
    location_weight: float = Field(default=0.15, gt=0)
    skills_weight: float = Field(default=0.15, gt=0)
    fuzzy_token_ratio: float = Field(default=0.85, ge=0, le=1)
    max_options: int = Field(default=3, ge=1, le=10)
    threshold: float = Field(default=0.7, ge=0, le=1)
    margin: float = Field(default=0.15, ge=0, le=1)
    presets: dict[str, MatchPreset] = Field(
        default_factory=lambda: {
            "lenient": MatchPreset(threshold=0.6, margin=0.1),
            "balanced": MatchPreset(threshold=0.7, margin=0.15),
            "strict": MatchPreset(threshold=0.8, margin=0.2),
        }
    )


class RatingTuning(Tuning):
    """Per-goal rating weights and the recency scale used to rate a person."""

    weights: dict[str, dict[str, int]] = Field(
        default_factory=lambda: {
            "hiring": {
                "skill_fit": 35,
                "evidence_of_work": 30,
                "recency": 15,
                "consistency": 20,
            },
            "sales": {
                "skill_fit": 30,
                "evidence_of_work": 25,
                "recency": 30,
                "consistency": 15,
            },
            "due_diligence": {
                "skill_fit": 20,
                "evidence_of_work": 30,
                "recency": 15,
                "consistency": 35,
            },
        }
    )
    recency_by_age: dict[int, int] = Field(
        default_factory=lambda: {0: 5, 1: 4, 2: 3, 3: 2, 4: 1, 5: 1}
    )
    neutral_consistency: int = Field(default=2, ge=0, le=5)
    max_justification_chars: int = Field(default=300, ge=40)

    @field_validator("weights")
    @classmethod
    def _weights_sum_to_one_hundred(cls, value: dict[str, dict[str, int]]) -> dict:
        """Check that the weights of every rating goal add up to 100.

        Args:
            value: the weights of each goal, keyed by goal name.

        Returns:
            The unchanged weights.

        Raises:
            ValueError: when the weights of any goal do not sum to 100.
        """
        for goal, weights in value.items():
            if sum(weights.values()) != 100:
                raise ValueError(f"the weights for {goal} must add up to 100")
        return value


class ScepticismProfile(Tuning):
    """A set of thresholds for flagging doubtful claims and capping ratings."""

    too_good_score: float = Field(default=4.0, ge=0, le=5)
    min_corroborated_share: float = Field(default=0.3, ge=0, le=1)
    thin_sources_min_findings: int = Field(default=4, ge=1)
    max_prestige_claims: int = Field(default=4, ge=1)
    min_hype_findings: int = Field(default=2, ge=1)
    max_concurrent_roles: int = Field(default=3, ge=1)
    max_career_years: int = Field(default=45, ge=1)
    max_skill_claims: int = Field(default=30, ge=1)
    cap_low: float | None = Field(default=None, ge=0, le=5)
    cap_medium: float | None = Field(default=3.5, ge=0, le=5)
    cap_high: float | None = Field(default=2.5, ge=0, le=5)


class ScepticismTuning(Tuning):
    """The standard and strict scepticism profiles plus the list of hype words."""

    standard: ScepticismProfile = Field(default_factory=ScepticismProfile)
    strict: ScepticismProfile = Field(
        default_factory=lambda: ScepticismProfile(
            too_good_score=3.5,
            min_corroborated_share=0.5,
            thin_sources_min_findings=3,
            max_prestige_claims=3,
            min_hype_findings=1,
            max_concurrent_roles=2,
            max_career_years=40,
            max_skill_claims=20,
            cap_low=4.0,
            cap_medium=3.0,
            cap_high=2.0,
        )
    )
    hype_words: tuple[str, ...] = (
        "world-class",
        "world class",
        "top 1%",
        "10x",
        "rockstar",
        "ninja",
        "guru",
        "visionary",
        "thought leader",
        "award-winning",
        "best in class",
        "unparalleled",
        "legendary",
    )


class VoiceTuning(Tuning):
    """ElevenLabs voice, speech, recording and audio size settings."""

    voice_id: str = "JBFqnCBsd6RMkjVDRZzb"
    tts_model: str = "eleven_multilingual_v2"
    stt_model: str = "scribe_v1"
    output_format: str = "mp3_44100_64"
    language_code: str | None = None
    stability: float = Field(default=0.5, ge=0, le=1)
    similarity_boost: float = Field(default=0.75, ge=0, le=1)
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_speech_chars: int = Field(default=900, ge=50, le=5000)
    max_audio_bytes: int = Field(default=1_000_000, ge=10_000, le=10_000_000)
    max_response_bytes: int = Field(default=5_000_000, ge=10_000)
    max_transcript_chars: int = Field(default=400, ge=20)
    max_record_seconds: int = Field(default=30, ge=3, le=120)
    spoken_flags: int = Field(default=2, ge=0, le=10)
    cache_entries: int = Field(default=32, ge=1)
    cache_ttl_seconds: int = Field(default=900, ge=0, le=86_400)
    audio_types: dict[str, str] = Field(
        default_factory=lambda: {
            "audio/webm": "webm",
            "audio/ogg": "ogg",
            "audio/mp4": "mp4",
            "audio/mpeg": "mp3",
            "audio/wav": "wav",
            "audio/x-wav": "wav",
        }
    )


class ScaleTuning(Tuning):
    """Dimensions of the rating scale graphic."""

    top: float = Field(default=5.0, gt=0, le=10)
    width: float = Field(default=500.0, gt=100)
    height: float = Field(default=64.0, gt=30)
    margin: float = Field(default=12.0, ge=0)
    track_y: float = Field(default=22.0, gt=0)
    tick_gap: float = Field(default=10.0, ge=0)
    tick_length: float = Field(default=6.0, gt=0)
    label_gap: float = Field(default=14.0, ge=0)
    bar_width: float = Field(default=100.0, gt=10)
    bar_height: float = Field(default=8.0, gt=1)


class EventTuning(Tuning):
    """Limits and timings for the live event log and its stream."""

    max_events: int = Field(default=1500, ge=50)
    text_chars: int = Field(default=240, ge=40)
    poll_seconds: float = Field(default=0.25, gt=0)
    heartbeat_seconds: float = Field(default=15.0, gt=0)
    max_stream_seconds: float = Field(default=900.0, gt=0)
    page_size: int = Field(default=200, ge=1, le=1000)


class CompanyTuning(Tuning):
    """Page, fact and text limits for researching a hiring company."""

    page_paths: tuple[str, ...] = ("/about", "/about-us", "/company", "/products", "/solutions")
    max_pages: int = Field(default=4, ge=1, le=10)
    search_pages: int = Field(default=3, ge=0, le=10)
    max_chars: int = Field(default=8000, ge=1000)
    max_facts: int = Field(default=10, ge=1, le=30)
    max_statement_chars: int = Field(default=240, ge=40)
    context_chars: int = Field(default=400, ge=100)


class RequirementTuning(Tuning):
    """Weights and limits for scoring a role's requirements."""

    must_weight: int = Field(default=2, ge=1, le=10)
    nice_weight: int = Field(default=1, ge=1, le=10)
    partial_credit: float = Field(default=0.5, ge=0, le=1)
    max_justification_chars: int = Field(default=200, ge=40)


class EmailTuning(Tuning):
    """Limits and sandbox rules for email templates.

    The forbidden tags, blocked operators, removed globals and allowed filters
    restrict what a template author can do, and the footers are appended to outreach.
    """

    template_dir: Path = Path("email_templates")
    max_batch: int = Field(default=50, ge=1, le=500)
    max_templates: int = Field(default=60, ge=1, le=500)
    max_subject_chars: int = Field(default=200, ge=20)
    max_body_chars: int = Field(default=8000, ge=100)
    max_file_bytes: int = Field(default=32_768, ge=1024)
    max_loop_depth: int = Field(default=2, ge=1, le=5)
    top_findings: int = Field(default=5, ge=1, le=20)
    shortlist_names: int = Field(default=8, ge=1, le=50)
    forbidden_tags: tuple[str, ...] = (
        "include",
        "import",
        "extends",
        "from",
        "macro",
        "call",
        "block",
        "set",
        "with",
        "do",
    )
    blocked_operators: tuple[str, ...] = ("*", "**", "%")
    removed_globals: tuple[str, ...] = (
        "range",
        "lipsum",
        "dict",
        "cycler",
        "joiner",
        "namespace",
    )
    filters: tuple[str, ...] = (
        "capitalize",
        "count",
        "d",
        "default",
        "first",
        "join",
        "last",
        "length",
        "list",
        "lower",
        "replace",
        "reverse",
        "sort",
        "string",
        "title",
        "trim",
        "truncate",
        "unique",
        "upper",
        "wordcount",
    )
    footer: str = (
        "You are receiving this message from {{ sender.company }} because your public "
        "professional profile suggested you may be a fit for {{ role.title }}. We contacted you "
        "only because of that public information. Reply STOP and we will not write to you again."
    )
    team_footer: str = "Sent by HMNH for {{ sender.company }}. Decision support only."


class BuddyTuning(Tuning):
    """Timings and spoken or shown lines of the Tie assistant.

    The validator requires every standard line to exist, allows only known
    placeholders, and forbids placeholders in lines that are spoken aloud.
    """

    name: str = "Tie"
    bubble_seconds: int = Field(default=9, ge=3, le=120)
    nudge_seconds: int = Field(default=30, ge=5)
    idle_seconds: int = Field(default=120, ge=10)
    away_seconds: int = Field(default=8, ge=1)
    focus_minutes: int = Field(default=25, ge=1, le=180)
    break_minutes: int = Field(default=5, ge=1, le=60)
    max_lines_per_minute: int = Field(default=4, ge=1, le=30)
    type_ms: int = Field(default=22, ge=0, le=200)
    mood_seconds: float = Field(default=4.0, gt=0)
    look_range: float = Field(default=2.0, ge=0, le=3)
    look_distance: int = Field(default=240, ge=50)
    read_ms: int = Field(default=40, ge=0, le=400)
    queue_gap_ms: int = Field(default=3200, ge=500)
    wiggle_seconds: int = Field(default=12, ge=3)
    wiggle_spread_seconds: int = Field(default=14, ge=0)
    lines: dict[str, str] = Field(
        default_factory=lambda: {
            "greeting": "Hi, I am Tie. I watch the search so you can keep your focus. "
            "I will only tap you when something needs you.",
            "working": "The search is running. Nothing needs you right now, so stay on your task.",
            "progress": "Still working: {sources} sources read and {findings} findings so far. "
            "Nothing needs you yet.",
            "away_back": "Welcome back. While you were away: {sources} new sources and "
            "{findings} new findings.",
            "away_quiet": "Welcome back. Nothing new happened while you were away.",
            "done": "The report is ready. Look at the rating first, then the evidence.",
            "question": "I need your pick before the search can go on.",
            "failed": "The search stopped. Read the reason, then try again with a clearer name.",
            "idle": "Ready when you are. One person, one question. Who are we looking for?",
            "focus_start": "Focus time. I will stay quiet until the break.",
            "focus_break": "Time for a short break. Stand up, stretch and drink some water.",
            "focus_end": "Break over. Pick one small next step and begin.",
            "focus_stop": "Focus stopped. We can start again whenever you like.",
            "detour": "Back to it. That was detour number {count}.",
        }
    )
    voice_lines: tuple[str, ...] = (
        "greeting",
        "working",
        "away_quiet",
        "done",
        "question",
        "failed",
        "idle",
        "focus_start",
        "focus_break",
        "focus_end",
        "focus_stop",
    )

    @model_validator(mode="after")
    def _lines_are_complete_and_safe(self) -> BuddyTuning:
        """Check that the buddy lines are complete and use only allowed placeholders.

        Returns:
            This instance, unchanged.

        Raises:
            ValueError: when a standard line is missing or blank, a line uses an unknown
                placeholder, a spoken line has a placeholder, or a spoken line is undefined.
        """
        missing = [key for key in BUDDY_LINES if not self.lines.get(key, "").strip()]
        if missing:
            raise ValueError(f"buddy lines are missing: {', '.join(missing)}")
        for key, line in self.lines.items():
            fields = {name for _, name, _, _ in Formatter().parse(line) if name is not None}
            if not fields <= BUDDY_PLACEHOLDERS:
                raise ValueError(f"buddy line {key} uses an unknown placeholder")
            if key in self.voice_lines and fields:
                raise ValueError(f"buddy line {key} is spoken, so it cannot have placeholders")
        unknown = [key for key in self.voice_lines if key not in self.lines]
        if unknown:
            raise ValueError(f"spoken buddy lines do not exist: {', '.join(unknown)}")
        return self


class StoreTuning(Tuning):
    """Location, retention and size limits of the local database."""

    path: Path = Path("data/agent.db")
    retention_days: int = Field(default=90, ge=1, le=3650)
    max_threads: int = Field(default=200, ge=1)
    max_messages: int = Field(default=500, ge=10)
    message_chars: int = Field(default=8000, ge=100)
    memory_chars: int = Field(default=8000, ge=100)
    busy_timeout_ms: int = Field(default=5000, ge=100)


class ChatTuning(Tuning):
    """Limits, word lists and reply lines of the chat."""

    max_user_chars: int = Field(default=1200, ge=50)
    history_messages: int = Field(default=12, ge=2, le=60)
    reply_chars: int = Field(default=1600, ge=100)
    title_chars: int = Field(default=48, ge=10)
    max_options: int = Field(default=5, ge=1, le=10)
    rail_threads: int = Field(default=40, ge=1, le=200)
    suggestions: tuple[str, ...] = (
        "Jan Novak, backend developer in Brno",
        "Find Rust developers in Brno",
        "We are a fintech startup hiring Python engineers",
    )
    context_chars: int = Field(default=2500, ge=200)
    confirm_words: tuple[str, ...] = (
        "yes",
        "y",
        "yep",
        "yeah",
        "ok",
        "okay",
        "go",
        "start",
        "confirm",
        "sure",
        "go ahead",
        "do it",
    )
    decline_words: tuple[str, ...] = (
        "no",
        "n",
        "nope",
        "cancel",
        "stop",
        "never mind",
        "nevermind",
        "abort",
    )
    none_words: tuple[str, ...] = ("none", "neither", "skip", "nobody", "no one", "none of them")
    lines: dict[str, str] = Field(
        default_factory=lambda: {
            "welcome": "Who would you like me to look into? A name is enough. You can also ask "
            "me to find people with a skill in a place, or just talk something through.",
            "need_subject": "Tell me who to look into. A name is enough, and a city, an "
            "employer or a role helps me find the right person.",
            "proposal": "{task}{context} I only read public professional information. Say yes "
            "to start; by doing so you confirm a lawful purpose and that you will review the "
            "result yourself. Or tell me what to change.",
            "started": "On it. I will post what I find here as I go.",
            "declined": "Okay, I will not start that. Tell me who to look into next.",
            "busy": "I am still working on {title}. {stage}. I will tell you when it is done.",
            "ask": "I found more than one possible match. Pick one by number, or tell me more, "
            "for example a city or an employer. You can also say none.",
            "failed": "That search stopped. {error}",
            "remembered": "Noted. I will use that for every search until you change it.",
            "misunderstood": "I did not follow that. Try a name like Jan Novak in Brno, or ask "
            "me to find Rust developers in Brno.",
            "model_down": "I could not reach the language model. {error}",
            "not_asking": "I am not waiting for an answer right now.",
            "cv_received": "Got your CV. I will read the person from it.",
            "limit": "That is too long for me. Please keep a message under {count} characters.",
            "interrupted": "That search was interrupted by a restart. Ask me again and I will "
            "run it again.",
            "tie_hello": "Hi, I am Tie. Ask me anything, or think out loud with me.",
            "not_allowed": "Your role can talk with me but cannot start searches. Ask an "
            "administrator for the search permission.",
            "not_ready": "Searches are not connected yet. An administrator can connect a "
            "search provider under Connections.",
        }
    )

    @model_validator(mode="after")
    def _lines_are_complete_and_safe(self) -> ChatTuning:
        """Check that the chat lines are complete and use only allowed placeholders.

        Returns:
            This instance, unchanged.

        Raises:
            ValueError: when a standard line is missing or blank, or a line uses an
                unknown placeholder.
        """
        missing = [key for key in CHAT_LINES if not self.lines.get(key, "").strip()]
        if missing:
            raise ValueError(f"chat lines are missing: {', '.join(missing)}")
        for key, line in self.lines.items():
            fields = {name for _, name, _, _ in Formatter().parse(line) if name is not None}
            if not fields <= CHAT_PLACEHOLDERS:
                raise ValueError(f"chat line {key} uses an unknown placeholder")
        return self


class RoleSpec(Tuning):
    """A role with a description and the permissions it grants."""

    description: str
    permissions: tuple[str, ...]

    @field_validator("permissions")
    @classmethod
    def _known_permissions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Check that every permission is the all-permissions marker or a known name.

        Args:
            value: the permission names of the role.

        Returns:
            The unchanged permission names.

        Raises:
            ValueError: when any name is not recognised.
        """
        unknown = [name for name in value if name != ALL_PERMISSIONS and not is_known(name)]
        if unknown:
            raise ValueError(f"unknown permissions: {', '.join(unknown)}")
        return value


class PolicyTuning(Tuning):
    """Switches for login methods and for the optional workspace features."""

    allow_password_login: bool = True
    allow_magic_links: bool = False
    require_totp: bool = False
    email_code_login: bool = False
    allow_api_keys: bool = True
    candidates_enabled: bool = True
    candidates_auto_record: bool = True
    finance_enabled: bool = False
    routines_enabled: bool = True
    email_sending_enabled: bool = False
    messaging_enabled: bool = True
    messaging_e2ee: bool = True


class AuthTuning(Tuning):
    """Password, token, TOTP, API key, audit and role settings for accounts.

    Passwords are hashed with scrypt using the configured cost. Validation makes
    sure the default, API and first admin roles all exist.
    """

    password_min_length: int = Field(default=10, ge=6, le=64)
    password_max_length: int = Field(default=256, ge=32, le=1024)
    password_blocklist: tuple[str, ...] = (
        "password",
        "passw0rd",
        "qwerty",
        "letmein",
        "welcome",
        "iloveyou",
        "admin",
        "changeme",
        "123456",
        "111111",
        "abc123",
    )
    scrypt_n: int = Field(default=2**15, ge=2**10, le=2**20)
    scrypt_r: int = Field(default=8, ge=1, le=32)
    scrypt_p: int = Field(default=1, ge=1, le=8)
    scrypt_maxmem: int = Field(default=2**27, ge=2**24)
    salt_bytes: int = Field(default=16, ge=8, le=64)
    hash_bytes: int = Field(default=32, ge=16, le=128)
    max_username_chars: int = Field(default=64, ge=8, le=200)
    max_code_chars: int = Field(default=40, ge=8, le=200)
    username_pattern: str = r"^[A-Za-z0-9][A-Za-z0-9._@+-]{1,63}$"
    invite_hours: int = Field(default=72, ge=1, le=720)
    reset_hours: int = Field(default=2, ge=1, le=72)
    login_link_minutes: int = Field(default=15, ge=1, le=120)
    token_bytes: int = Field(default=32, ge=16, le=64)
    email_code_digits: int = Field(default=6, ge=4, le=10)
    email_code_minutes: int = Field(default=10, ge=1, le=60)
    code_attempts: int = Field(default=5, ge=1, le=20)
    totp_digits: int = Field(default=6, ge=6, le=8)
    totp_period_seconds: int = Field(default=30, ge=15, le=120)
    totp_window: int = Field(default=1, ge=0, le=3)
    totp_secret_bytes: int = Field(default=20, ge=16, le=64)
    totp_issuer: str = "HMNH"
    recovery_codes: int = Field(default=8, ge=0, le=20)
    recovery_code_bytes: int = Field(default=10, ge=4, le=16)
    link_cooldown_seconds: int = Field(default=120, ge=0, le=3600)
    api_key_prefix: str = "hra"
    api_key_bytes: int = Field(default=32, ge=24, le=64)
    api_key_days: int = Field(default=365, ge=0, le=3650)
    api_key_day_choices: tuple[int, ...] = (30, 90, 365)
    qr_scale: int = Field(default=5, ge=2, le=12)
    qr_border: int = Field(default=2, ge=0, le=8)
    max_api_keys_per_user: int = Field(default=20, ge=1, le=200)
    audit_retention_days: int = Field(default=365, ge=1, le=3650)
    audit_page_size: int = Field(default=100, ge=10, le=500)
    audit_detail_chars: int = Field(default=500, ge=50, le=5000)
    audit_export_rows: int = Field(default=5000, ge=100, le=100_000)
    max_users: int = Field(default=500, ge=1, le=100_000)
    max_roles: int = Field(default=50, ge=1, le=1000)
    key_file: Path = Path("data/secret.key")
    policy: PolicyTuning = Field(default_factory=PolicyTuning)
    roles: dict[str, RoleSpec] = Field(
        default_factory=lambda: {
            "admin": RoleSpec(
                description="Everything, including accounts, roles and connections.",
                permissions=(ALL_PERMISSIONS,),
            ),
            "manager": RoleSpec(
                description="Runs the hiring work and sees the numbers, but not the settings.",
                permissions=(
                    "chat.use",
                    "research.run",
                    "reports.view",
                    "reports.export",
                    "reports.delete",
                    "candidates.view",
                    "candidates.edit",
                    "contacts.view",
                    "contacts.edit",
                    "hires.manage",
                    "emails.view",
                    "emails.edit",
                    "emails.send",
                    "emails.log",
                    "routines.view",
                    "routines.manage",
                    "messages.use",
                    "messages.groups",
                    "messages.announce",
                    "stats.view",
                    "finance.view",
                    "data.export",
                    "tie.use",
                    "voice.use",
                    "context.edit",
                    "users.view",
                    "api.use",
                    "apikeys.own",
                ),
            ),
            "recruiter": RoleSpec(
                description="Searches, candidates and emails.",
                permissions=(
                    "chat.use",
                    "research.run",
                    "reports.view",
                    "reports.export",
                    "candidates.view",
                    "candidates.edit",
                    "contacts.view",
                    "contacts.edit",
                    "hires.manage",
                    "emails.view",
                    "emails.edit",
                    "emails.send",
                    "emails.log",
                    "routines.view",
                    "routines.manage",
                    "messages.use",
                    "messages.groups",
                    "stats.view",
                    "tie.use",
                    "voice.use",
                ),
            ),
            "viewer": RoleSpec(
                description="Reads reports, candidates and the dashboard.",
                permissions=(
                    "reports.view",
                    "candidates.view",
                    "routines.view",
                    "messages.use",
                    "stats.view",
                    "tie.use",
                ),
            ),
            "finance": RoleSpec(
                description="Revenue and finance data only.",
                permissions=(
                    "messages.use",
                    "stats.view",
                    "finance.view",
                    "finance.edit",
                    "data.export",
                ),
            ),
            "service": RoleSpec(
                description="Default role of API keys made for other programs.",
                permissions=(
                    "api.use",
                    "research.run",
                    "reports.view",
                    "reports.export",
                    "reports.delete",
                    "candidates.view",
                    "emails.view",
                    "voice.use",
                    "stats.view",
                ),
            ),
        }
    )
    default_role: str = "recruiter"
    api_role: str = "service"
    first_admin_role: str = "admin"

    @field_validator("username_pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        """Check that the username pattern is a valid regular expression.

        Args:
            value: the pattern text.

        Returns:
            The unchanged pattern.

        Raises:
            re.error: when the pattern does not compile.
        """
        re.compile(value)
        return value

    @model_validator(mode="after")
    def _roles_exist(self) -> AuthTuning:
        """Check that the default, API and first admin roles are defined.

        Returns:
            This instance, unchanged.

        Raises:
            ValueError: when one of the three roles is missing from the role table.
        """
        for name in (self.default_role, self.api_role, self.first_admin_role):
            if name not in self.roles:
                raise ValueError(f"the role {name} is not defined")
        return self


class CandidateTuning(Tuning):
    """Statuses, field limits and paging for saved candidates."""

    statuses: tuple[str, ...] = (
        "sourced",
        "contacted",
        "interviewing",
        "offer",
        "hired",
        "rejected",
        "withdrawn",
        "alumni",
    )
    first_status: str = "sourced"
    hired_status: str = "hired"
    kept_statuses: tuple[str, ...] = ("hired", "alumni")
    hire_outcomes: tuple[str, ...] = ("active", "completed", "left", "dismissed")
    contact_kinds: tuple[str, ...] = ("email", "phone", "website", "address", "other")
    phone_pattern: str = r"^\+?[0-9][0-9 ()./-]{4,24}$"
    page_size: int = Field(default=25, ge=5, le=200)
    max_candidates: int = Field(default=50_000, ge=1)
    max_name_chars: int = Field(default=120, ge=20, le=300)
    max_text_chars: int = Field(default=200, ge=40, le=1000)
    max_notes_chars: int = Field(default=4000, ge=200, le=50_000)
    max_tags: int = Field(default=12, ge=1, le=50)
    tag_chars: int = Field(default=30, ge=5, le=80)
    max_contacts: int = Field(default=20, ge=1, le=100)
    max_hires: int = Field(default=30, ge=1, le=200)
    max_links: int = Field(default=25, ge=1, le=100)
    max_skills: int = Field(default=30, ge=1, le=100)
    events_shown: int = Field(default=60, ge=10, le=500)
    history_ratings: int = Field(default=12, ge=3, le=100)
    rating_low: int = Field(default=1, ge=0, le=5)
    rating_high: int = Field(default=5, ge=1, le=10)
    retention_days: int = Field(default=365, ge=0, le=3650)


class RoutineTuning(Tuning):
    """Scheduling, run limits and defaults for recurring routines."""

    poll_seconds: float = Field(default=30.0, ge=1, le=3600)
    default_interval_hours: int = Field(default=24, ge=1, le=720)
    min_interval_hours: int = Field(default=1, ge=1, le=720)
    max_interval_hours: int = Field(default=720, ge=1, le=8760)
    default_max_runs: int = Field(default=14, ge=1, le=1000)
    max_runs_limit: int = Field(default=100, ge=1, le=10_000)
    default_min_rating: float = Field(default=4.0, ge=0, le=5)
    default_size: int = Field(default=5, ge=1, le=10)
    max_active: int = Field(default=10, ge=1, le=1000)
    max_routines: int = Field(default=100, ge=1, le=10_000)
    max_runs_per_day: int = Field(default=20, ge=1, le=10_000)
    max_failures: int = Field(default=3, ge=1, le=20)
    max_name_chars: int = Field(default=80, ge=10, le=200)
    max_recipients: int = Field(default=5, ge=0, le=50)
    runs_kept: int = Field(default=50, ge=5, le=1000)
    interval_choices: tuple[int, ...] = (1, 6, 12, 24, 72, 168)
    max_parallel: int = Field(default=1, ge=1, le=10)
    summary_chars: int = Field(default=300, ge=50, le=2000)
    matches_listed: int = Field(default=5, ge=1, le=20)


class FinanceTuning(Tuning):
    """Currency, entry kinds and size limits for finance data."""

    currency: str = "USD"
    decimals: int = Field(default=2, ge=0, le=4)
    kinds: tuple[str, ...] = ("revenue", "expense")
    max_entries: int = Field(default=200_000, ge=1)
    max_categories: int = Field(default=100, ge=1, le=1000)
    max_amount: int = Field(default=10**13, ge=1)
    page_size: int = Field(default=30, ge=5, le=200)
    months_shown: int = Field(default=12, ge=3, le=60)
    top_categories: int = Field(default=8, ge=2, le=30)
    note_chars: int = Field(default=200, ge=20, le=1000)
    name_chars: int = Field(default=40, ge=5, le=100)
    first_year: int = Field(default=2000, ge=1900, le=2100)
    future_days: int = Field(default=366, ge=0, le=3650)
    currency_pattern: str = r"^[A-Z]{3}$"


class ChartTuning(Tuning):
    """Size, spacing and colors of generated charts."""

    width: int = Field(default=720, ge=240, le=2000)
    height: int = Field(default=280, ge=120, le=1200)
    left: int = Field(default=64, ge=20, le=200)
    right: int = Field(default=16, ge=0, le=100)
    top: int = Field(default=18, ge=0, le=100)
    bottom: int = Field(default=40, ge=10, le=200)
    ticks: int = Field(default=4, ge=2, le=10)
    bar_gap: float = Field(default=0.3, ge=0, le=0.9)
    series_gap: float = Field(default=0.08, ge=0, le=0.5)
    label_chars: int = Field(default=14, ge=4, le=40)
    row_height: int = Field(default=28, ge=14, le=80)
    row_label_width: int = Field(default=150, ge=40, le=400)
    max_labels: int = Field(default=8, ge=2, le=40)
    compact_width: int = Field(default=420, ge=200, le=1200)
    compact_height: int = Field(default=230, ge=100, le=800)
    ink: str = "#1f1f1f"
    faint: str = "#9c9c9c"
    tint: str = "#e1e1e1"
    grid: str = "#d9d9d9"
    text: str = "#666666"
    paper: str = "#ffffff"

    def compact(self) -> ChartTuning:
        """Return a copy of this tuning sized for compact charts.

        Returns:
            A new tuning whose width and height are the compact width and height.
        """
        return self.model_copy(update={"width": self.compact_width, "height": self.compact_height})


class StatsTuning(Tuning):
    """Time ranges and week or month counts for statistics views."""

    default_days: int = Field(default=30, ge=7, le=365)
    max_days: int = Field(default=365, ge=7, le=3650)
    default_months: int = Field(default=12, ge=3, le=60)
    max_months: int = Field(default=60, ge=3, le=240)
    weeks_shown: int = Field(default=12, ge=4, le=52)
    day_choices: tuple[int, ...] = (7, 30, 90, 365)


class MessageTuning(Tuning):
    """Size, rate, retention and key derivation settings for messaging."""

    max_chars: int = Field(default=4000, ge=100, le=50_000)
    title_chars: int = Field(default=60, ge=10, le=200)
    max_members: int = Field(default=200, ge=2, le=5000)
    max_groups_per_user: int = Field(default=50, ge=1, le=1000)
    feed_size: int = Field(default=50, ge=10, le=500)
    poll_seconds: float = Field(default=4.0, ge=1, le=60)
    send_per_minute: int = Field(default=30, ge=1, le=600)
    retention_days: int = Field(default=0, ge=0, le=3650)
    announcement_chars: int = Field(default=4000, ge=100, le=50_000)
    announcement_title_chars: int = Field(default=100, ge=10, le=300)
    announcement_max_active: int = Field(default=100, ge=1, le=10_000)
    announcement_max_per_author: int = Field(default=20, ge=1, le=10_000)
    announcement_shown: int = Field(default=30, ge=5, le=200)
    banner_shown: int = Field(default=2, ge=0, le=10)
    expiry_choices: tuple[int, ...] = (0, 1, 7, 30)
    min_iterations: int = Field(default=600_000, ge=10_000, le=10_000_000)
    kdf_iterations: int = Field(default=600_000, ge=10_000, le=10_000_000)
    max_key_chars: int = Field(default=2000, ge=200, le=20_000)
    max_blob_chars: int = Field(default=8000, ge=200, le=100_000)
    max_cipher_chars: int = Field(default=12_000, ge=200, le=200_000)
    directory_size: int = Field(default=500, ge=10, le=10_000)
    unlock_minutes: int = Field(default=30, ge=1, le=1440)


class ApiTuning(Tuning):
    """Paging limits and the shared identity of the public API."""

    default_page_size: int = Field(default=25, ge=1, le=500)
    max_page_size: int = Field(default=100, ge=1, le=1000)
    shared_user: str = "api-shared"
    shared_name: str = "Shared API access"


class PortabilityTuning(Tuning):
    """Size and row limits for data import and export."""

    max_bytes: int = Field(default=5_000_000, ge=10_000, le=200_000_000)
    max_rows: int = Field(default=20_000, ge=10, le=1_000_000)
    errors_shown: int = Field(default=20, ge=1, le=200)
    stash_minutes: int = Field(default=15, ge=1, le=240)
    stash_max: int = Field(default=20, ge=1, le=500)
    stash_per_owner: int = Field(default=3, ge=1, le=100)
    export_events: int = Field(default=200, ge=0, le=10_000)
    export_reports: int = Field(default=200, ge=0, le=10_000)
    export_log_rows: int = Field(default=5000, ge=100, le=100_000)
    strategies: tuple[str, ...] = ("skip", "update")


class MailTuning(Tuning):
    """SMTP timeouts, send limits, log paging and message texts."""

    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    send_per_hour: int = Field(default=60, ge=1, le=10_000)
    system_per_recipient: int = Field(default=4, ge=1, le=1000)
    log_retention_days: int = Field(default=365, ge=1, le=3650)
    log_page_size: int = Field(default=50, ge=10, le=500)
    max_address_chars: int = Field(default=254, ge=20, le=320)
    max_subject_chars: int = Field(default=200, ge=20, le=998)
    max_body_chars: int = Field(default=10_000, ge=100, le=100_000)
    test_body: str = "This is a test message. If you can read it, email is connected."
    outreach_footer: str = (
        "--\nYou received this message because your public professional profile matched a "
        "role. If you would rather not hear from us, reply and say so and we will not write "
        "again."
    )
    starttls_port: int = Field(default=587, ge=1, le=65535)
    ssl_port: int = Field(default=465, ge=1, le=65535)
    plain_port: int = Field(default=25, ge=1, le=65535)
    system_subjects: dict[str, str] = Field(
        default_factory=lambda: {
            "login_code": "Your sign in code",
            "invite": "You were invited to the hiring workspace",
            "reset": "Reset your password",
            "login_link": "Your sign in link",
            "verify": "Confirm your email address",
            "test": "Test message from the hiring workspace",
            "routine": "A routine found a match",
        }
    )


class LogTuning(Tuning):
    """Log level, format and the loggers to quieten."""

    level: int = Field(default=20, ge=0, le=50)
    format: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    min_secret_length: int = Field(default=4, ge=1)
    noisy_loggers: tuple[str, ...] = ("httpx", "httpcore", "uvicorn.access")


class ValidationLimits(Tuning):
    """Maximum lengths and counts applied when validating input."""

    max_text: int = Field(default=120, ge=10)
    max_url: int = Field(default=600, ge=50)
    max_list: int = Field(default=20, ge=1)
    max_names: int = Field(default=100, ge=1)
    max_city: int = Field(default=80, ge=10)
    max_sentence: int = Field(default=300, ge=40)


class LLMConfig(BaseModel):
    """The resolved connection settings of the language model.

    The API key is a secret string, so it is hidden when the object is printed.
    """

    model_config = ConfigDict(frozen=True)

    provider: LLMProvider
    model: str | None
    base_url: str
    api_key: SecretStr | None


class SearchConfig(BaseModel):
    """The resolved search provider together with its secret."""

    model_config = ConfigDict(frozen=True)

    provider: SearchProviderName
    secret: SecretStr


class Settings(BaseSettings):
    """Application settings read from environment variables.

    The object is immutable. Secrets are secret strings, empty variables are ignored,
    input values are hidden in validation errors, and nested tuning groups are set
    with a double underscore delimiter.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        frozen=True,
        env_ignore_empty=True,
        env_nested_delimiter="__",
        hide_input_in_errors=True,
    )

    brave_api_key: SecretStr | None = None
    apify_token: SecretStr | None = None
    search_provider: SearchProviderName | None = None
    apify_google_actor: str = "apify/google-search-scraper"
    apify_linkedin_actor: str | None = "harvestapi/linkedin-profile-scraper"
    apify_linkedin_url_field: str = "urls"
    apify_linkedin_input: dict[str, Any] = Field(
        default_factory=lambda: {"profileScraperMode": "Profile details no email ($4 per 1k)"}
    )
    apify_facebook_actor: str = "apify/facebook-pages-scraper"
    apify_instagram_actor: str = "apify/instagram-profile-scraper"
    # The lower bound of 0.5 USD is the smallest cost cap that Apify accepts.
    apify_max_charge_usd: float = Field(default=0.5, ge=0.5, le=50)
    apify_max_pages_per_query: int = Field(default=1, ge=1, le=5)

    llm_provider: LLMProvider | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_ollama_scheme: Literal["http", "https"] | None = None
    llm_ollama_host: str | None = None
    llm_ollama_port: int | None = Field(default=None, ge=1, le=65535)

    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_from_name: str | None = None

    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_voice_id: str | None = None

    agent_contact: str | None = None
    output_dir: Path = Path("outputs")

    endpoints: Endpoints = Field(default_factory=Endpoints)
    defaults: Defaults = Field(default_factory=Defaults)
    http: HttpTuning = Field(default_factory=HttpTuning)
    llm_tuning: LlmTuning = Field(default_factory=LlmTuning)
    cv: CvTuning = Field(default_factory=CvTuning)
    fetch: FetchTuning = Field(default_factory=FetchTuning)
    collect: CollectTuning = Field(default_factory=CollectTuning)
    discovery: DiscoveryTuning = Field(default_factory=DiscoveryTuning)
    summary: SummaryTuning = Field(default_factory=SummaryTuning)
    scoring: ScoringTuning = Field(default_factory=ScoringTuning)
    rating: RatingTuning = Field(default_factory=RatingTuning)
    scepticism: ScepticismTuning = Field(default_factory=ScepticismTuning)
    log: LogTuning = Field(default_factory=LogTuning)
    voice: VoiceTuning = Field(default_factory=VoiceTuning)
    scale: ScaleTuning = Field(default_factory=ScaleTuning)
    events: EventTuning = Field(default_factory=EventTuning)
    company: CompanyTuning = Field(default_factory=CompanyTuning)
    requirements: RequirementTuning = Field(default_factory=RequirementTuning)
    email: EmailTuning = Field(default_factory=EmailTuning)
    buddy: BuddyTuning = Field(default_factory=BuddyTuning)
    store: StoreTuning = Field(default_factory=StoreTuning)
    chat: ChatTuning = Field(default_factory=ChatTuning)
    auth: AuthTuning = Field(default_factory=AuthTuning)
    mail: MailTuning = Field(default_factory=MailTuning)
    candidates: CandidateTuning = Field(default_factory=CandidateTuning)
    routines: RoutineTuning = Field(default_factory=RoutineTuning)
    finance: FinanceTuning = Field(default_factory=FinanceTuning)
    charts: ChartTuning = Field(default_factory=ChartTuning)
    stats: StatsTuning = Field(default_factory=StatsTuning)
    messages: MessageTuning = Field(default_factory=MessageTuning)
    api: ApiTuning = Field(default_factory=ApiTuning)
    portability: PortabilityTuning = Field(default_factory=PortabilityTuning)

    @field_validator("elevenlabs_voice_id")
    @classmethod
    def _voice_id(cls, value: str | None) -> str | None:
        """Check that the ElevenLabs voice id is alphanumeric.

        Args:
            value: the voice id, or None.

        Returns:
            The unchanged value.

        Raises:
            ValueError: when the voice id has an unexpected shape.
        """
        if value is not None and not VOICE_ID.fullmatch(value):
            raise ValueError("must be an alphanumeric ElevenLabs voice id")
        return value

    @field_validator(
        "apify_google_actor",
        "apify_linkedin_actor",
        "apify_facebook_actor",
        "apify_instagram_actor",
    )
    @classmethod
    def _actor_id(cls, value: str | None) -> str | None:
        """Check that an Apify actor id looks like username/actor-name.

        Args:
            value: the actor id, or None.

        Returns:
            The unchanged value.

        Raises:
            ValueError: when the actor id does not have the expected shape.
        """
        if value is not None and not ACTOR_ID.fullmatch(value):
            raise ValueError("must look like 'username/actor-name'")
        return value

    @field_validator("apify_linkedin_url_field")
    @classmethod
    def _field_name(cls, value: str) -> str:
        """Check that a value is a plain field name.

        Args:
            value: the field name.

        Returns:
            The unchanged value.

        Raises:
            ValueError: when the name is not a short identifier.
        """
        if not FIELD_NAME.fullmatch(value):
            raise ValueError("must be a plain field name")
        return value

    @field_validator("smtp_host")
    @classmethod
    def _smtp_host(cls, value: str | None) -> str | None:
        """Check that the SMTP host is a bare host name or IP address.

        Args:
            value: the host, or None.

        Returns:
            The unchanged value.

        Raises:
            ValueError: when the host contains anything but a name or an address.
        """
        if value is not None and not HOST.fullmatch(value):
            raise ValueError("must be a bare host name or IP address")
        return value

    @field_validator("smtp_from")
    @classmethod
    def _smtp_from(cls, value: str | None) -> str | None:
        """Check that the sender address is a plausible email address.

        Args:
            value: the address, or None.

        Returns:
            The unchanged value.

        Raises:
            ValueError: when the value is not shaped like an email address.
        """
        if value is not None and not MAIL_ADDRESS.fullmatch(value):
            raise ValueError("must be an email address")
        return value

    @field_validator("smtp_from_name", "smtp_username")
    @classmethod
    def _smtp_text(cls, value: str | None) -> str | None:
        """Normalize and check the SMTP sender name or username.

        Args:
            value: the text, or None.

        Returns:
            The text with whitespace collapsed, or None when no value was given.

        Raises:
            ValueError: when the text is longer than 120 characters or not printable.
        """
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if len(cleaned) > 120 or not cleaned.isprintable():
            raise ValueError("must be printable text of at most 120 characters")
        return cleaned

    def smtp_configured(self) -> bool:
        """Return whether an SMTP host and a sender address are both set.

        Returns:
            True when mail can be sent, False otherwise.
        """
        return self.smtp_host is not None and self.smtp_from is not None

    @field_validator("llm_ollama_host")
    @classmethod
    def _ollama_host(cls, value: str | None) -> str | None:
        """Check that the Ollama host is a bare host name or IP address.

        Args:
            value: the host, or None.

        Returns:
            The unchanged value.

        Raises:
            ValueError: when the host contains anything but a name or an address.
        """
        if value is not None and not HOST.fullmatch(value):
            raise ValueError("must be a bare host name or IP address")
        return value

    @field_validator("llm_base_url")
    @classmethod
    def _base_url(cls, value: str | None) -> str | None:
        """Check the LLM base URL and strip any trailing slashes.

        Args:
            value: the URL, or None.

        Returns:
            The URL without trailing slashes, or None when no value was given.

        Raises:
            ValueError: when the URL does not start with http:// or https://.
        """
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("agent_contact")
    @classmethod
    def _contact(cls, value: str | None) -> str | None:
        """Normalize and check the contact text sent in the User-Agent header.

        Args:
            value: the contact text, or None.

        Returns:
            The text with whitespace collapsed, or None when no value was given.

        Raises:
            ValueError: when the text is empty, longer than 200 characters or not printable.
        """
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned or len(cleaned) > 200 or not cleaned.isprintable():
            raise ValueError("must be printable text of at most 200 characters")
        return cleaned

    def llm_config(self) -> LLMConfig:
        """Resolve the language model connection from the settings.

        When no provider is named, one is chosen from the credentials: an Anthropic key
        prefix selects Anthropic, any other key selects OpenAI, Ollama settings select
        Ollama, and a bare Apify token selects the Apify models.

        Returns:
            The provider, model, base URL and API key to use.

        Raises:
            NotConfigured: when no LLM credentials are set at all.
            ConfigError: when both a key and Ollama settings are set without a provider,
                when the chosen provider lacks its key or token, or when a custom base URL
                is used without a model.
        """
        ollama_configured = any(
            value is not None
            for value in (self.llm_ollama_scheme, self.llm_ollama_host, self.llm_ollama_port)
        )
        provider = self.llm_provider
        if provider is None:
            if self.llm_api_key is not None and ollama_configured:
                raise ConfigError(
                    "Both LLM_API_KEY and LLM_OLLAMA_* are set; set LLM_PROVIDER to choose one."
                )
            if self.llm_api_key is not None:
                prefix = self.defaults.anthropic_key_prefix
                # The key prefix is the only hint available, so any other key is treated as OpenAI
                # compatible.
                is_anthropic = self.llm_api_key.get_secret_value().startswith(prefix)
                provider = "anthropic" if is_anthropic else "openai"
            elif ollama_configured:
                provider = "ollama"
            elif self.apify_token is not None:
                provider = "apify"
            else:
                raise NotConfigured(
                    "No LLM configured. Set LLM_API_KEY (Anthropic, OpenAI or an OpenAI-compatible "
                    "service), LLM_OLLAMA_PORT / LLM_OLLAMA_HOST / LLM_OLLAMA_SCHEME for Ollama, "
                    "or APIFY_TOKEN to use the AI models of your Apify account."
                )
        if provider == "ollama":
            return LLMConfig(
                provider=provider,
                model=self.llm_model,
                base_url=self._ollama_url(),
                api_key=None,
            )
        if provider == "claude":
            return LLMConfig(provider=provider, model=self.llm_model, base_url="", api_key=None)
        if provider == "apify":
            if self.apify_token is None:
                raise ConfigError("LLM_PROVIDER=apify requires APIFY_TOKEN.")
            return LLMConfig(
                provider=provider,
                model=self.llm_model or self.defaults.apify_ai_model,
                base_url=self.endpoints.apify_ai_api,
                api_key=self.apify_token,
            )
        if self.llm_api_key is None:
            raise ConfigError(f"LLM_PROVIDER={provider} requires LLM_API_KEY.")
        if provider == "anthropic":
            return LLMConfig(
                provider=provider,
                model=self.llm_model or self.defaults.anthropic_model,
                base_url=self.endpoints.anthropic_api,
                api_key=self.llm_api_key,
            )
        base_url = self.llm_base_url or self.endpoints.openai_api
        if self.llm_model is None and base_url != self.endpoints.openai_api:
            raise ConfigError("LLM_MODEL is required when LLM_BASE_URL points to another service.")
        return LLMConfig(
            provider=provider,
            model=self.llm_model or self.defaults.openai_model,
            base_url=base_url,
            api_key=self.llm_api_key,
        )

    def llm_config_or_none(self) -> LLMConfig | None:
        """Return the LLM configuration, or None when none is configured.

        Returns:
            The resolved configuration, or None when no LLM credentials are set.

        Raises:
            ConfigError: when the settings are inconsistent.
        """
        try:
            return self.llm_config()
        except NotConfigured:
            return None

    def search_config_or_none(self) -> SearchConfig | None:
        """Return the search configuration, or None when none is configured.

        Returns:
            The resolved configuration, or None when no search credentials are set.

        Raises:
            ConfigError: when the settings are inconsistent.
        """
        try:
            return self.search_config()
        except NotConfigured:
            return None

    def _ollama_url(self) -> str:
        """Build the Ollama base URL from the configured or default parts.

        Returns:
            The URL as scheme://host:port.
        """
        scheme = self.llm_ollama_scheme or self.defaults.ollama_scheme
        host = self.llm_ollama_host or self.defaults.ollama_host
        port = self.llm_ollama_port or self.defaults.ollama_port
        return f"{scheme}://{host}:{port}"

    def search_config(self) -> SearchConfig:
        """Resolve the search provider and its secret from the settings.

        When no provider is named, Brave is used if its key is set, otherwise Apify.

        Returns:
            The provider name and the secret that belongs to it.

        Raises:
            NotConfigured: when neither a Brave key nor an Apify token is set.
            ConfigError: when the named provider lacks its key or token.
        """
        provider = self.search_provider
        if provider is None:
            if self.brave_api_key is not None:
                provider = "brave"
            elif self.apify_token is not None:
                provider = "apify"
            else:
                raise NotConfigured(
                    "No search provider configured. Set BRAVE_API_KEY or APIFY_TOKEN."
                )
        if provider == "brave":
            if self.brave_api_key is None:
                raise ConfigError("SEARCH_PROVIDER=brave requires BRAVE_API_KEY.")
            return SearchConfig(provider=provider, secret=self.brave_api_key)
        if self.apify_token is None:
            raise ConfigError("SEARCH_PROVIDER=apify requires APIFY_TOKEN.")
        return SearchConfig(provider=provider, secret=self.apify_token)

    def collector_actor(self, network: str) -> str | None:
        """Return the Apify actor that collects data from a social network.

        Args:
            network: the network name, such as linkedin, facebook or instagram.

        Returns:
            The actor id, or None when the network has no actor configured.
        """
        actors = {
            "linkedin": self.apify_linkedin_actor,
            "facebook": self.apify_facebook_actor,
            "instagram": self.apify_instagram_actor,
        }
        return actors.get(network)

    def voice_id(self) -> str:
        """Return the ElevenLabs voice id to speak with.

        Returns:
            The configured voice id, or the default one from the voice tuning.
        """
        return self.elevenlabs_voice_id or self.voice.voice_id

    def secret_values(self) -> list[str]:
        """Return the plain text of every configured secret.

        The values are not masked, so handle the result with care.

        Returns:
            The Brave, Apify, LLM, ElevenLabs and SMTP secrets that are set.
        """
        secrets = (
            self.brave_api_key,
            self.apify_token,
            self.llm_api_key,
            self.elevenlabs_api_key,
            self.smtp_password,
        )
        return [secret.get_secret_value() for secret in secrets if secret is not None]
