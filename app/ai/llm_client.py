from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "openrouter": ("https://openrouter.ai/api/v1", "google/gemma-4-31b-it:free"),
    "openai": ("https://api.openai.com/v1", "gpt-4.1-mini"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash"),
    "qwen": ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "ollama": ("http://localhost:11434", "qwen2.5:7b"),
}
LOCAL_PROVIDERS = {"ollama"}  # no API key needed
# Checked when LLM_API_KEY is not set
PROVIDER_KEY_ENV: dict[str, tuple[str, ...]] = {
    "openrouter": ("OPENROUTER_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "qwen": ("DASHSCOPE_API_KEY",),
}
_SECRET_KEY_RE = re.compile(r"(?:^|[_-])(?:api[_-]?key|access[_-]?key|secret|password|token)$", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(r"^(?:sk-|sk_|AIza)[A-Za-z0-9_-]{16,}")  # OpenRouter/OpenAI, Google
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LLMConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LLMProfile:
    name: str
    provider: str = "none"
    model: str | None = None
    base_url: str | None = None
    fallback_models: tuple[str, ...] = ()
    description: str = ""
    temperature: float = 0.0
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    json_mode: bool = True
    timeout: float = 180.0
    max_input_chars: int = 15_000
    num_ctx: int = 16_384


_PROFILE_KEYS = {f.name for f in fields(LLMProfile)} - {"name"}


def _reject_secrets(data: dict[str, Any], file: Path, prefix: str = "") -> None:
    for key, value in data.items():
        if _SECRET_KEY_RE.search(key) or isinstance(value, str) and _SECRET_VALUE_RE.match(value):
            raise LLMConfigError(f"{file}: '{prefix}{key}' looks like a secret. Put API keys in .env "
                                 f"(e.g. OPENROUTER_API_KEY=...), never in the config file")
        if isinstance(value, dict):
            _reject_secrets(value, file, f"{prefix}{key}.")


def load_llm_profiles(path: str | Path) -> tuple[dict[str, LLMProfile], str | None]:
    file = Path(path)
    if not file.is_file():
        return {}, None
    with file.open("rb") as handle:
        data = tomllib.load(handle)
    _reject_secrets(data, file)
    defaults = data.get("defaults") or {}
    profiles: dict[str, LLMProfile] = {}
    for name, values in (data.get("profiles") or {}).items():
        merged = {**defaults, **values}
        unknown = set(merged) - _PROFILE_KEYS
        if unknown:
            raise LLMConfigError(f"{file}: unknown setting(s) {sorted(unknown)} in profile {name!r}")
        merged["provider"] = str(merged.get("provider", "none")).strip().lower()
        merged["fallback_models"] = tuple(merged.get("fallback_models") or ())
        profiles[name] = LLMProfile(name=name, **merged)
    return profiles, data.get("active_profile")


@dataclass
class LLMConfig:
    provider: str
    model: str
    base_url: str
    api_key: str | None = field(repr=False)
    profile: LLMProfile


def _api_key(settings: Settings, provider: str) -> str | None:
    candidates = [settings.llm_api_key]
    for env_name in PROVIDER_KEY_ENV.get(provider, ()):
        candidates.append(getattr(settings, env_name.lower(), None))
        candidates.append(os.environ.get(env_name))
    for value in candidates:
        secret = value.get_secret_value() if hasattr(value, "get_secret_value") else value
        if secret and secret.strip():
            return secret.strip()
    return None


def resolve_llm_config(settings: Settings, profile_name: str | None = None) -> tuple[LLMConfig | None, str]:
    # A per-job profile_name wins over LLM_PROFILE / active_profile; the LLM_PROVIDER / LLM_MODEL /
    # LLM_BASE_URL overrides only apply when no profile was chosen for the job.
    try:
        profiles, active = load_llm_profiles(settings.llm_config_file)
    except (LLMConfigError, tomllib.TOMLDecodeError, TypeError) as exc:
        return None, f"invalid LLM config: {exc}"
    name = profile_name or settings.llm_profile or active
    if name and name not in profiles:
        return None, f"unknown LLM profile {name!r} (available: {', '.join(profiles) or 'none'})"
    profile = profiles[name] if name else LLMProfile(name="default")

    model_override = base_url_override = None
    if profile_name is None:
        provider_override = (settings.llm_provider or "").strip().lower()
        if provider_override and provider_override != profile.provider:
            profile = replace(profile, provider=provider_override, model=None, base_url=None, fallback_models=())
        model_override, base_url_override = settings.llm_model, settings.llm_base_url

    provider = profile.provider
    if provider in ("", "none"):
        return None, f"LLM provider is 'none' (profile {profile.name!r})"
    if provider not in PROVIDER_DEFAULTS:
        return None, f"unknown LLM provider {provider!r} (use {', '.join(PROVIDER_DEFAULTS)} or none)"
    api_key = _api_key(settings, provider)
    base_url = base_url_override or profile.base_url
    if provider not in LOCAL_PROVIDERS and not (api_key or base_url):
        key_name = PROVIDER_KEY_ENV[provider][0]
        return None, f"provider {provider} needs an API key: set {key_name} (or LLM_API_KEY) in .env"
    default_url, default_model = PROVIDER_DEFAULTS[provider]
    base_url = base_url or default_url
    if provider == "ollama":
        base_url = re.sub(r"/v1/?$", "", base_url.rstrip("/"))  # native API lives at the root
    model = model_override or profile.model or default_model
    return LLMConfig(provider, model, base_url, api_key, profile), "configured"


def llm_status(settings: Settings | None = None, profile_name: str | None = None) -> dict[str, Any]:
    config, reason = resolve_llm_config(settings or get_settings(), profile_name)
    return {
        "enabled": config is not None,
        "provider": config.provider if config else None,
        "model": f"{config.provider}:{config.model}" if config else None,
        "profile": config.profile.name if config else profile_name,
        "reason": reason,
    }


def list_llm_profiles(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    try:
        profiles, active = load_llm_profiles(settings.llm_config_file)
    except (LLMConfigError, tomllib.TOMLDecodeError, TypeError) as exc:
        return {"default": None, "profiles": [], "error": str(exc)}
    items = []
    for name, profile in profiles.items():
        status = llm_status(settings, name)
        items.append({
            "name": name, "description": profile.description, "provider": profile.provider,
            "model": profile.model, "enabled": status["enabled"], "reason": status["reason"],
            "temperature": profile.temperature, "max_tokens": profile.max_tokens,
        })
    return {"default": settings.llm_profile or active, "profiles": items}


def parse_json_response(content: str) -> dict[str, Any] | None:
    text = _FENCE_RE.sub("", content.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


class LLMClient:
    """Create one per job (the underlying HTTP pool is bound to the running event loop)."""

    def __init__(self, settings: Settings | None = None, profile: str | None = None):
        settings = settings or get_settings()
        self._client: Any = None
        config, reason = resolve_llm_config(settings, profile)
        self.provider = config.provider if config else None
        self.model = config.model if config else None
        self.profile = config.profile if config else LLMProfile(name=profile or "default")
        self.max_input_chars = self.profile.max_input_chars
        self._json_mode = self.profile.json_mode
        if config is None:
            log.info("LLM disabled (%s); AI stages will use rule-based fallbacks", reason)
            return

        if config.provider == "ollama":
            # Native /api/chat, because Ollama's OpenAI-compatible endpoint cannot set num_ctx and
            # silently truncates prompts to its small default context window.
            self._client = httpx.AsyncClient(base_url=config.base_url, timeout=self.profile.timeout)
            return
        try:
            from openai import AsyncOpenAI
        except ImportError:
            log.warning("openai package not installed; using rule-based fallbacks")
            return
        self._client = AsyncOpenAI(
            api_key=config.api_key or "not-needed",
            base_url=config.base_url,
            timeout=self.profile.timeout,
            max_retries=2,
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def model_name(self) -> str | None:
        return f"{self.provider}:{self.model}" if self.enabled else None

    def _request_options(self) -> dict[str, Any]:
        p = self.profile
        options: dict[str, Any] = {"temperature": p.temperature}
        if p.top_p is not None:
            options["top_p"] = p.top_p
        if p.max_tokens:
            options["max_tokens"] = p.max_tokens
        if p.seed is not None:
            options["seed"] = p.seed
        if self.provider == "openrouter" and p.fallback_models:
            options["extra_body"] = {"models": [self.model, *p.fallback_models]}
        return options

    async def complete_json(self, system: str, user: str) -> dict[str, Any] | None:
        """Run a chat completion in JSON mode; returns None on any failure so callers can fall back."""
        if not self._client:
            return None
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            if self.provider == "ollama":
                content = await self._ollama_chat(messages)
            else:
                content = await self._openai_chat(messages)
        except Exception as exc:  # network, auth, quota, model not pulled, timeout...
            log.warning("LLM request to %s failed: %s", self.model_name, exc or type(exc).__name__)
            return None
        data = parse_json_response(content)
        if data is None:
            log.warning("LLM returned non-JSON output (%d chars)", len(content))
        return data

    async def _openai_chat(self, messages: list[dict[str, str]]) -> str:
        options = self._request_options()
        if self._json_mode:
            try:
                response = await self._client.chat.completions.create(
                    model=self.model, messages=messages, response_format={"type": "json_object"}, **options,
                )
            except Exception as exc:
                if getattr(exc, "status_code", None) not in (400, 404, 422):
                    raise
                # Some models reject response_format; the prompts already ask for JSON only
                log.info("%s rejected JSON mode (%s); retrying without it", self.model_name, exc)
                self._json_mode = False
            else:
                return self._content(response)
        response = await self._client.chat.completions.create(model=self.model, messages=messages, **options)
        return self._content(response)

    def _content(self, response: Any) -> str:
        served_by = getattr(response, "model", None)
        if served_by and served_by != self.model:
            log.info("Request for %s was served by fallback model %s", self.model, served_by)
        return response.choices[0].message.content or ""

    async def _ollama_chat(self, messages: list[dict[str, str]]) -> str:
        p = self.profile
        options: dict[str, Any] = {"temperature": p.temperature, "num_ctx": p.num_ctx}
        if p.top_p is not None:
            options["top_p"] = p.top_p
        if p.max_tokens:
            options["num_predict"] = p.max_tokens
        if p.seed is not None:
            options["seed"] = p.seed
        payload = {"model": self.model, "messages": messages, "stream": False, "options": options}
        if self._json_mode:
            payload["format"] = "json"
        for attempt in (1, 2):
            response = await self._client.post("/api/chat", json=payload)
            if response.status_code == 404:
                raise RuntimeError(f"model {self.model!r} not found in Ollama (run: ollama pull {self.model})")
            if response.status_code >= 500 and attempt == 1:
                # Ollama returns 500 while (re)loading a model, e.g. when another app changed num_ctx
                log.warning("Ollama returned HTTP %d; retrying once", response.status_code)
                await asyncio.sleep(3)
                continue
            response.raise_for_status()
            return response.json().get("message", {}).get("content", "")
        return ""

    async def aclose(self) -> None:
        if self._client is None:
            return
        if isinstance(self._client, httpx.AsyncClient):
            await self._client.aclose()
        else:
            await self._client.close()
