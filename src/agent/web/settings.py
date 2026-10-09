# src/agent/web/settings.py
from pydantic import (
    field_validator,
    model_validator,
    SecretStr,
    ValidationError,
    ValidationInfo,
)
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)
from agent.envfile import env_source
from agent.errors import ConfigError
from typing import Self

DEFAULT_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
    "microphone=(self), payment=(), usb=(), interest-cohort=()"
)


class WebSettings(BaseSettings):
    """Settings of the web server, read from WEB_ environment variables.

    The settings are immutable. Empty variables are ignored and the offending
    input is left out of validation errors so tokens never end up in logs.
    """

    model_config = SettingsConfigDict(
        env_prefix="WEB_",
        extra="ignore",
        frozen=True,
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    min_token_length: int = 20
    min_token_distinct: int = 8
    trusted_proxies: str = ""
    ipv6_prefix: int = 64
    user_login_ceiling: int = 100
    form_read_seconds: int = 60
    max_sessions_per_user: int = 10
    time_format: str = "%Y-%m-%d %H:%M"
    access_token: SecretStr | None = None
    api_token: SecretStr | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    loopback_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
    allowed_hosts: str = "localhost,127.0.0.1"
    public_origin: str | None = None
    cookie_secure: bool = True
    session_idle_seconds: int = 1800
    session_max_seconds: int = 28_800
    max_sessions: int = 500
    job_ttl_seconds: int = 3600
    max_concurrent_jobs: int = 2
    max_jobs_per_session: int = 3
    login_attempts: int = 5
    login_window_seconds: int = 300
    user_login_attempts: int = 8
    second_factor_attempts: int = 6
    second_factor_window_seconds: int = 300
    link_attempts: int = 20
    link_window_seconds: int = 600
    account_actions: int = 30
    account_window_seconds: int = 300
    setup_code_bytes: int = 12
    submissions: int = 10
    submission_window_seconds: int = 600
    anonymous_sessions: int = 30
    anonymous_session_window_seconds: int = 600
    api_failures: int = 10
    api_failure_window_seconds: int = 300
    api_requests: int = 120
    api_window_seconds: int = 60
    body_overhead_bytes: int = 65_536
    max_form_fields: int = 160
    max_field_length: int = 2000
    job_id_bytes: int = 18
    nonce_bytes: int = 18
    session_id_bytes: int = 32
    hsts_max_age_seconds: int = 63_072_000
    robots_tag: str = "noindex, nofollow, noarchive, nosnippet, noimageindex"
    robots_txt: str = "User-agent: *\nDisallow: /\n"
    permissions_policy: str = DEFAULT_PERMISSIONS_POLICY
    prefs_max_age_seconds: int = 31_536_000
    prefs_max_bytes: int = 3600
    status_cache_seconds: int = 30
    static_max_age_seconds: int = 3600
    static_hashed_max_age_seconds: int = 31_536_000
    asset_digest_chars: int = 10
    favicon_color: str = "#808080"
    chat_requests: int = 60
    mail_requests: int = 20
    mail_window_seconds: int = 3600
    chat_window_seconds: int = 300
    voice_requests: int = 30
    voice_window_seconds: int = 300

    @field_validator("access_token", "api_token")
    @classmethod
    def _strong_token(cls, value: SecretStr | None, info: ValidationInfo) -> SecretStr | None:
        """Check that an access or API token is long enough.

        Args:
            value: Token as configured, or None when unset.
            info: Validation info holding the fields validated so far.

        Returns:
            The token unchanged, or None when unset.

        Raises:
            ValueError: when the token is shorter than the minimum length or has
                spaces at either end.
        """
        if value is None:
            return None
        secret = value.get_secret_value()
        # min_token_length is declared before the tokens, so it has already been validated here.
        minimum = info.data.get("min_token_length", 20)
        if len(secret) < minimum or secret != secret.strip():
            raise ValueError(f"must be at least {minimum} characters without edge spaces")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _no_wildcard_host(cls, value: str) -> str:
        # A lone wildcard would accept any Host header, so it is refused outright.
        """Reject a bare wildcard in the allowed host list.

        Args:
            value: Comma separated list of allowed hosts.

        Returns:
            The list unchanged.

        Raises:
            ValueError: when the list contains only a star as an entry.
        """
        # A lone wildcard would accept any Host header, so it is refused outright.
        if "*" in {host.strip() for host in value.split(",")}:
            raise ValueError("a bare wildcard disables host header protection")
        return value

    @field_validator("public_origin")
    @classmethod
    def _origin(cls, value: str | None) -> str | None:
        """Normalize the public origin of the site.

        Args:
            value: Configured origin, or None when unset.

        Returns:
            The origin without a trailing slash, or None when unset.

        Raises:
            ValueError: when it is not an http or https origin or has a path.
        """
        if value is None:
            return None
        origin = value.rstrip("/")
        scheme, _, rest = origin.partition("://")
        if scheme not in {"http", "https"} or not rest or "/" in rest:
            raise ValueError("must look like https://host[:port] without a path")
        return origin

    @model_validator(mode="after")
    def _distinct_tokens(self) -> Self:
        """Make sure the access token and the API token are different.

        Returns:
            The validated settings.

        Raises:
            ValueError: when both tokens are set to the same secret.
        """
        if (
            self.api_token is not None
            and self.access_token is not None
            and self.api_token.get_secret_value() == self.access_token.get_secret_value()
        ):
            raise ValueError("WEB_API_TOKEN must differ from WEB_ACCESS_TOKEN")
        return self

    @property
    def hosts(self) -> list[str]:
        """Return the allowed host names as a clean list."""
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @property
    def proxies(self) -> list[str]:
        """Return the trusted proxy addresses as a clean list."""
        return [item.strip() for item in self.trusted_proxies.split(",") if item.strip()]


def load_web_settings() -> WebSettings:
    """Load and validate the web settings from the environment.

    Beyond the field checks, each configured token must contain a minimum
    number of different characters, which rejects trivial secrets.

    Returns:
        The validated settings.

    Raises:
        ConfigError: when a value is invalid or a token is too repetitive.
    """
    try:
        loaded = WebSettings(_env_file=env_source(), _env_file_encoding="utf-8")
        for label, token in (
            ("WEB_ACCESS_TOKEN", loaded.access_token),
            ("WEB_API_TOKEN", loaded.api_token),
        ):
            if token is not None and len(set(token.get_secret_value())) < loaded.min_token_distinct:
                raise ConfigError(
                    f"{label} must use at least {loaded.min_token_distinct} different characters; "
                    "generate a random one instead."
                )
        return loaded
    except ValidationError as error:
        fields = ", ".join(".".join(str(part) for part in item["loc"]) for item in error.errors())
        raise ConfigError(
            f"Invalid web settings ({fields}). WEB_ACCESS_TOKEN and WEB_API_TOKEN, when set, must "
            "be different secrets of at least 20 characters."
        ) from error
