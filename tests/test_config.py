# tests/test_config.py
from agent.config import (
    Defaults,
    Endpoints,
    RatingTuning,
    ScaleTuning,
    ScepticismTuning,
    Settings,
    VoiceTuning,
)
from agent.errors import ConfigError
from pydantic import ValidationError

import pytest


def settings(**values):
    return Settings(_env_file=None, **values)


def test_single_anthropic_key_is_enough():
    config = settings(llm_api_key="sk-ant-abc").llm_config()
    assert config.provider == "anthropic"
    assert config.model == Defaults().anthropic_model
    assert config.base_url == Endpoints().anthropic_api


def test_single_openai_style_key_is_enough():
    config = settings(llm_api_key="sk-abc").llm_config()
    assert config.provider == "openai"
    assert config.model == Defaults().openai_model
    assert config.base_url == Endpoints().openai_api


def test_openai_compatible_service_requires_model():
    with pytest.raises(ConfigError, match="LLM_MODEL"):
        settings(llm_api_key="k", llm_base_url="https://router.example/v1/").llm_config()
    config = settings(
        llm_api_key="k", llm_base_url="https://router.example/v1/", llm_model="m"
    ).llm_config()
    assert config.base_url == "https://router.example/v1"


def test_defaults_are_configurable():
    config = settings(
        llm_api_key="sk-ant-x", defaults={"anthropic_model": "custom-model"}
    ).llm_config()
    assert config.model == "custom-model"
    custom = settings(llm_api_key="k", endpoints={"openai_api": "https://gw.example/v1"})
    assert custom.llm_config().base_url == "https://gw.example/v1"


def test_ollama_defaults_and_overrides():
    assert settings(llm_ollama_port=11434).llm_config().base_url == "http://localhost:11434"
    config = settings(
        llm_ollama_scheme="https", llm_ollama_host="gpu.lan", llm_ollama_port=9000, llm_model="m"
    ).llm_config()
    assert (config.provider, config.base_url, config.model) == (
        "ollama",
        "https://gpu.lan:9000",
        "m",
    )
    assert config.api_key is None


def test_ambiguous_and_missing_llm_configuration_fail_loudly():
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        settings(llm_api_key="k", llm_ollama_port=1234).llm_config()
    with pytest.raises(ConfigError, match="No LLM configured"):
        settings().llm_config()
    with pytest.raises(ConfigError, match="requires LLM_API_KEY"):
        settings(llm_provider="anthropic").llm_config()


def test_explicit_provider_overrides_inference():
    config = settings(
        llm_provider="ollama", llm_api_key="sk-ant-x", llm_ollama_port=1234
    ).llm_config()
    assert config.provider == "ollama"


@pytest.mark.parametrize(
    "values",
    [
        {"llm_ollama_host": "evil.example/path"},
        {"llm_ollama_port": 70000},
        {"llm_base_url": "ftp://x"},
        {"apify_linkedin_actor": "no-slash"},
        {"apify_linkedin_url_field": "a b"},
        {"apify_max_charge_usd": 0.1},
        {"scoring": {"threshold": 2}},
        {"rating": {"weights": {"hiring": {"skill_fit": 50}}}},
        {"http": {"unknown_field": 1}},
        {"agent_contact": "bad\x00contact"},
    ],
)
def test_invalid_values_are_rejected(values):
    with pytest.raises(ValidationError):
        settings(**values)


def test_search_provider_inference():
    assert settings(brave_api_key="b", apify_token="a").search_config().provider == "brave"
    assert settings(apify_token="a").search_config().provider == "apify"
    config = settings(brave_api_key="b", apify_token="a", search_provider="apify").search_config()
    assert config.secret.get_secret_value() == "a"


def test_search_provider_errors():
    with pytest.raises(ConfigError, match="No search provider"):
        settings().search_config()
    with pytest.raises(ConfigError, match="BRAVE_API_KEY"):
        settings(search_provider="brave", apify_token="a").search_config()
    with pytest.raises(ConfigError, match="APIFY_TOKEN"):
        settings(search_provider="apify", brave_api_key="b").search_config()


def test_environment_variables_are_read_including_nested_tuning(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-env")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("APIFY_LINKEDIN_ACTOR", "owner/actor")
    monkeypatch.setenv("SCORING__THRESHOLD", "0.82")
    monkeypatch.setenv("SCEPTICISM__STANDARD__CAP_HIGH", "2.5")
    monkeypatch.setenv("DISCOVERY__MAX_WEB_PAGES", "9")
    loaded = Settings()
    assert loaded.llm_config().model == Defaults().anthropic_model
    assert loaded.collector_actor("linkedin") == "owner/actor"
    assert loaded.collector_actor("web") is None
    assert loaded.scoring.threshold == 0.82
    assert loaded.scepticism.standard.cap_high == 2.5
    assert loaded.discovery.max_web_pages == 9


def test_apify_defaults_match_the_verified_actors():
    loaded = settings()
    assert loaded.apify_google_actor == "apify/google-search-scraper"
    assert loaded.collector_actor("linkedin") == "harvestapi/linkedin-profile-scraper"
    assert loaded.collector_actor("facebook") == "apify/facebook-pages-scraper"
    assert loaded.collector_actor("instagram") == "apify/instagram-profile-scraper"
    assert loaded.apify_linkedin_url_field == "urls"
    assert "no email" in loaded.apify_linkedin_input["profileScraperMode"]
    assert loaded.apify_max_charge_usd == 0.5


def test_tuning_defaults_are_consistent():
    loaded = settings()
    assert set(loaded.scoring.presets) == {"lenient", "balanced", "strict"}
    weights = loaded.scoring
    assert weights.name_weight + weights.employer_weight + weights.location_weight > 0
    assert all(sum(item.values()) == 100 for item in RatingTuning().weights.values())
    assert ScepticismTuning().strict.too_good_score < ScepticismTuning().standard.too_good_score
    assert loaded.discovery.apify_max_query_words == 32


def test_contact_whitespace_is_normalised():
    contact = settings(agent_contact="ops@example.com\r\nX-Evil: 1").agent_contact
    assert contact == "ops@example.com X-Evil: 1"


def test_secrets_are_hidden_and_listed():
    loaded = settings(brave_api_key="brave-secret", llm_api_key="llm-secret")
    assert "brave-secret" not in repr(loaded)
    assert sorted(loaded.secret_values()) == ["brave-secret", "llm-secret"]


def test_voice_is_optional_and_uses_a_default_voice():
    loaded = settings()
    assert loaded.elevenlabs_api_key is None
    assert loaded.voice_id() == VoiceTuning().voice_id
    assert loaded.endpoints.elevenlabs_api == "https://api.elevenlabs.io/v1"


def test_the_voice_can_be_chosen_with_a_validated_id():
    assert settings(elevenlabs_voice_id="AbCdEfGhIjKlMn").voice_id() == "AbCdEfGhIjKlMn"
    for bad in ("short", "has-dashes-in-it-123", "../../etc/passwd", "a" * 41, "with space 12345"):
        with pytest.raises(ValidationError):
            settings(elevenlabs_voice_id=bad)


def test_the_elevenlabs_key_is_a_hidden_secret():
    loaded = settings(elevenlabs_api_key="eleven-secret", llm_api_key="llm-secret")
    assert "eleven-secret" not in repr(loaded)
    assert loaded.secret_values() == ["llm-secret", "eleven-secret"]


def test_voice_and_scale_tuning_can_be_overridden_from_the_environment(monkeypatch):
    monkeypatch.setenv("VOICE__TTS_MODEL", "eleven_flash_v2_5")
    monkeypatch.setenv("VOICE__MAX_RECORD_SECONDS", "20")
    monkeypatch.setenv("SCALE__WIDTH", "400")
    loaded = Settings(_env_file=None)
    assert loaded.voice.tts_model == "eleven_flash_v2_5"
    assert loaded.voice.max_record_seconds == 20
    assert loaded.scale.width == 400


def test_voice_and_scale_tuning_reject_unsafe_values():
    for bad in (
        {"max_record_seconds": 0},
        {"max_audio_bytes": 5},
        {"stability": 2},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            VoiceTuning(**bad)
    for bad in ({"width": 10}, {"top": 0}, {"bar_height": 0}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            ScaleTuning(**bad)


def test_the_supported_recording_types_are_listed_in_the_tuning():
    types = VoiceTuning().audio_types
    assert types["audio/webm"] == "webm" and types["audio/mpeg"] == "mp3"
    assert all(kind.startswith("audio/") for kind in types)
