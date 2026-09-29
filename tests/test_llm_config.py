import asyncio

import pytest
from sqlalchemy import create_engine, inspect, text

from app.ai.llm_client import (
    LLMClient,
    LLMConfigError,
    list_llm_profiles,
    llm_status,
    load_llm_profiles,
    parse_json_response,
    resolve_llm_config,
)
from app.config import PROJECT_ROOT, Settings

PROFILES = """
active_profile = "big"

[defaults]
temperature = 0.1
max_tokens = 1000
timeout = 60

[profiles.big]
provider = "openrouter"
model = "vendor/big-model:free"
fallback_models = ["vendor/other:free"]
max_input_chars = 30000

[profiles.small]
provider = "openrouter"
model = "vendor/small-model:free"
temperature = 0.0

[profiles.off]
provider = "none"
"""


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)  # ignore the developer's .env


@pytest.fixture
def profile_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config = tmp_path / "llm.toml"
    config.write_text(PROFILES, encoding="utf-8")

    def build(**overrides) -> Settings:
        return make_settings(llm_config_file=str(config), **overrides)

    return build


def test_llm_disabled_by_default():
    status = llm_status(make_settings(llm_provider="none"))
    assert status["enabled"] is False and "none" in status["reason"]


def test_llm_needs_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config, reason = resolve_llm_config(make_settings(llm_provider="openai", llm_api_key=None))
    assert config is None and "OPENAI_API_KEY" in reason and "LLM_API_KEY" in reason


def test_llm_uses_standard_key_variable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    config, _ = resolve_llm_config(make_settings(llm_provider="gemini", llm_api_key=None))
    assert config.api_key == "test-key" and config.model == "gemini-2.5-flash"


def test_local_qwen_needs_no_key():
    config, _ = resolve_llm_config(make_settings(llm_provider="qwen", llm_api_key=None,
                                            llm_base_url="http://localhost:11434/v1", llm_model="qwen2.5:14b"))
    assert config.base_url == "http://localhost:11434/v1" and config.model == "qwen2.5:14b"


def test_ollama_needs_no_key_and_uses_native_api_root():
    config, _ = resolve_llm_config(make_settings(llm_provider="ollama", llm_api_key=None, llm_model=None,
                                            llm_base_url="http://localhost:11434/v1"))
    assert config.base_url == "http://localhost:11434" and config.model == "qwen2.5:7b"


def test_active_profile_supplies_model_and_parameters(profile_settings):
    config, reason = resolve_llm_config(profile_settings(openrouter_api_key="sk-or-test"))
    assert reason == "configured"
    assert config.provider == "openrouter" and config.model == "vendor/big-model:free"
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.profile.temperature == 0.1 and config.profile.max_tokens == 1000  # from [defaults]
    assert config.profile.max_input_chars == 30000  # profile value wins over defaults
    assert "sk-or-test" not in repr(config)


def test_profile_selection_order(profile_settings):
    settings = profile_settings(openrouter_api_key="sk-or-test", llm_profile="small")
    assert resolve_llm_config(settings)[0].model == "vendor/small-model:free"  # LLM_PROFILE beats active_profile
    # a per-job profile beats LLM_PROFILE and ignores LLM_PROVIDER / LLM_MODEL overrides
    settings = profile_settings(openrouter_api_key="sk-or-test", llm_profile="small", llm_model="x/y")
    assert resolve_llm_config(settings, "big")[0].model == "vendor/big-model:free"
    assert resolve_llm_config(settings)[0].model == "x/y"


def test_profile_needs_openrouter_key(profile_settings):
    config, reason = resolve_llm_config(profile_settings())
    assert config is None and "OPENROUTER_API_KEY" in reason
    assert llm_status(profile_settings(), "off")["enabled"] is False
    assert "unknown LLM profile" in resolve_llm_config(profile_settings(), "missing")[1]


def test_openrouter_request_options(profile_settings):
    client = LLMClient(profile_settings(openrouter_api_key="sk-or-test"))
    options = client._request_options()
    assert options["temperature"] == 0.1 and options["max_tokens"] == 1000
    assert options["extra_body"] == {"models": ["vendor/big-model:free", "vendor/other:free"]}
    assert client.max_input_chars == 30000
    asyncio.run(client.aclose())


def test_list_profiles_reports_availability(profile_settings):
    listing = list_llm_profiles(profile_settings(openrouter_api_key="sk-or-test"))
    assert listing["default"] == "big"
    by_name = {p["name"]: p for p in listing["profiles"]}
    assert by_name["big"]["enabled"] and by_name["small"]["enabled"] and not by_name["off"]["enabled"]
    assert "sk-or-test" not in str(listing)


@pytest.mark.parametrize("line", ['api_key = "abc"', 'openrouter_api_key = "abc"', 'model = "sk-or-v1-0123456789abcdef0123"'])
def test_secrets_in_config_file_are_rejected(tmp_path, line):
    config = tmp_path / "llm.toml"
    config.write_text(f'[profiles.p]\nprovider = "openrouter"\n{line}\n', encoding="utf-8")
    with pytest.raises(LLMConfigError, match="secret"):
        load_llm_profiles(config)


def test_shipped_config_is_valid():
    profiles, active = load_llm_profiles(PROJECT_ROOT / "config" / "llm.toml")
    assert active in profiles
    assert all(p.provider in ("openrouter", "ollama", "none") for p in profiles.values())
    assert all(p.model.endswith(":free") for p in profiles.values() if p.provider == "openrouter")


def test_parse_json_response_handles_fences():
    assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_response('Here you go: {"a": 2} thanks') == {"a": 2}
    assert parse_json_response("no json") is None


def test_missing_columns_are_added(monkeypatch, tmp_path):
    import app.database.database as database

    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as connection:  # an extraction_results table from before page_report existed
        connection.execute(text("CREATE TABLE extraction_results (id INTEGER PRIMARY KEY, job_id VARCHAR(32))"))
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    columns = {c["name"] for c in inspect(engine).get_columns("extraction_results")}
    assert {"page_report", "understanding", "content_hash"} <= columns
