"""Application settings.

Precedence (highest first): explicit init kwargs > environment variables > ``.env`` >
``config/settings.yaml``. Nested keys can be overridden from the environment with ``__``,
e.g. ``WEBSITE__TIMEOUT_SECONDS=20``.

The config directory defaults to ``<repo>/config`` and can be moved with ``LEADS_CONFIG_DIR``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    """Directory holding settings.yaml, countries.yaml, categories.yaml and templates/."""
    return Path(os.environ.get("LEADS_CONFIG_DIR", PROJECT_ROOT / "config"))


class ScheduleSettings(BaseModel):
    monthly_collect: str = "0 2 1 * *"
    daily_drafts: str = "0 7 * * *"
    gmail_sync: str = "*/30 * * * *"
    backup_check: str = "30 3 * * *"


class BatchSettings(BaseModel):
    drafts_per_day: int = 100
    website_concurrency: int = 50
    db_chunk_size: int = 5000
    raw_chunk_size: int = 100_000


class WebsiteSettings(BaseModel):
    timeout_seconds: float = 12.0
    max_requests_per_domain: int = 2
    daily_limit: int = 3000
    respect_robots_txt: bool = True
    user_agent: str = "lead-automation/0.1"
    recheck_after_days: int = 30
    min_text_chars: int = 100
    retry_wait_seconds: float = 5.0


class OutreachSettings(BaseModel):
    followup_after_days: int = 6
    auto_drafts: bool = False
    default_language: str = "en"
    delivery_days: int = 7
    price: str = ""


class SenderSettings(BaseModel):
    sender_name: str = ""
    company: str = ""
    company_website: str = ""
    company_phone: str = ""
    company_address: str = ""
    mockup_link: str = ""


class AiSettings(BaseModel):
    model: str = "openai/gpt-oss-120b"


class SanctionsSettings(BaseModel):
    sdn_url: str = (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV"
    )
    add_url: str = (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADD.CSV"
    )
    min_score: int = 92


class MatchSettings(BaseModel):
    name_score_threshold: int = 90
    shared_key_name_score: int = 75
    geo_radius_m: float = 50.0
    max_shared_key_group: int = 8


class RegistrySettings(BaseModel):
    # Also keep new registered companies that list no phone, email or website.
    uncontactable_leads: bool = False


class WaybackSettings(BaseModel):
    enabled: bool = True
    daily_limit: int = 2000
    seconds_per_request: float = 1.5


class CollectSettings(BaseModel):
    # ISO-2 codes the monthly run collects; empty = every country in countries.yaml.
    countries: list[str] = []

    def codes(self, included: frozenset[str]) -> frozenset[str]:
        return frozenset(c.upper() for c in self.countries) & included or (
            included if not self.countries else frozenset()
        )


class RefreshSettings(BaseModel):
    closed_after_missed_runs: int = 2
    max_closed_share: float = 0.2


class Settings(BaseSettings):
    """All runtime configuration. Use :func:`get_settings` instead of instantiating directly."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- from .env / environment -------------------------------------------------------
    database_url: str = "postgresql+psycopg://leads:leads@localhost:5432/leads"
    data_dir: Path = PROJECT_ROOT / "data"
    secrets_dir: Path = PROJECT_ROOT / "secrets"
    backups_dir: Path = PROJECT_ROOT / "backups"
    google_client_secret_file: Path | None = None
    google_token_file: Path | None = None
    gmail_user_id: str = "me"
    gmail_account: str | None = None  # drafts are refused in any other account
    groq_api_key: SecretStr | None = None
    google_sheet_id: str | None = None
    companies_house_api_key: SecretStr | None = None
    hf_token: SecretStr | None = None
    insee_api_key: SecretStr | None = None
    abn_lookup_guid: SecretStr | None = None
    log_level: str = "INFO"
    log_json: bool = False

    # --- from config/settings.yaml -------------------------------------------------------
    since: date = date(2018, 1, 1)
    include_unknown_age: bool = True
    timezone: str = "UTC"
    schedules: ScheduleSettings = ScheduleSettings()
    batch: BatchSettings = BatchSettings()
    website: WebsiteSettings = WebsiteSettings()
    outreach: OutreachSettings = OutreachSettings()
    sender: SenderSettings = SenderSettings()
    ai: AiSettings = AiSettings()
    sanctions: SanctionsSettings = SanctionsSettings()
    match: MatchSettings = MatchSettings()
    registry: RegistrySettings = RegistrySettings()
    wayback: WaybackSettings = WaybackSettings()
    collect: CollectSettings = CollectSettings()
    refresh: RefreshSettings = RefreshSettings()
    country_bbox: dict[str, tuple[float, float, float, float]] = {}
    sources: dict[str, dict[str, Any]] = {}

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_settings = YamlConfigSettingsSource(
            settings_cls, yaml_file=config_dir() / "settings.yaml"
        )
        return (init_settings, env_settings, dotenv_settings, yaml_settings, file_secret_settings)

    @property
    def client_secret_path(self) -> Path:
        return self.google_client_secret_file or self.secrets_dir / "client_secret.json"

    @property
    def token_path(self) -> Path:
        return self.google_token_file or self.secrets_dir / "token.json"

    def source_enabled(self, name: str) -> bool:
        return bool(self.sources.get(name, {}).get("enabled", False))

    def source_option(self, name: str, key: str, default: Any = None) -> Any:
        return self.sources.get(name, {}).get(key, default)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# --- countries.yaml ---------------------------------------------------------------------


def _read_yaml(path: Path, *, strings_only: bool = False) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        # BaseLoader keeps every scalar a string, so `NO` (Norway) is not parsed as false.
        data = yaml.load(fh, Loader=yaml.BaseLoader) if strings_only else yaml.safe_load(fh)
    data = data or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return data


@lru_cache(maxsize=4)
def load_country_groups(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """Return ``{group: (ISO2, ...)}`` from countries.yaml."""
    data = _read_yaml(path or config_dir() / "countries.yaml", strings_only=True)
    groups: dict[str, tuple[str, ...]] = {}
    for group, codes in (data.get("include") or {}).items():
        cleaned = tuple(str(c).strip().upper() for c in codes)
        bad = [c for c in cleaned if len(c) != 2 or not c.isalpha()]
        if bad:
            raise ValueError(f"countries.yaml group {group!r} has invalid codes: {bad}")
        groups[group] = cleaned
    return groups


def load_countries(path: Path | None = None) -> frozenset[str]:
    """All included ISO-2 country codes."""
    return frozenset(c for codes in load_country_groups(path).values() for c in codes)


# --- categories.yaml --------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryConfig:
    default_group: str
    groups: dict[str, str]  # key -> display name
    exact: dict[str, dict[str, str]]  # source -> {lower-cased category: group}
    keywords: dict[str, tuple[str, ...]]  # group -> keywords
    nace: dict[str, str]  # two-digit activity code -> group

    def display_name(self, group: str | None) -> str:
        key = group or self.default_group
        return self.groups.get(key, key)


@lru_cache(maxsize=4)
def load_categories(path: Path | None = None) -> CategoryConfig:
    data = _read_yaml(path or config_dir() / "categories.yaml")
    exact = {
        source: {str(k).lower(): str(v) for k, v in (mapping or {}).items()}
        for source, mapping in (data.get("sources") or {}).items()
    }
    keywords = {
        group: tuple(str(w).lower() for w in words)
        for group, words in (data.get("keywords") or {}).items()
    }
    return CategoryConfig(
        default_group=str(data.get("default_group", "other")),
        groups={str(k): str(v) for k, v in (data.get("groups") or {}).items()},
        exact=exact,
        keywords=keywords,
        nace={str(k): str(v) for k, v in (data.get("nace") or {}).items()},
    )


# --- filters.yaml -----------------------------------------------------------------------


@dataclass(frozen=True)
class FilterConfig:
    exclude_categories: dict[str, tuple[str, ...]]  # source -> lower-cased entries
    platform_domains: frozenset[str]
    platform_brands: frozenset[str]


@lru_cache(maxsize=4)
def load_filters(path: Path | None = None) -> FilterConfig:
    data = _read_yaml(path or config_dir() / "filters.yaml")
    return FilterConfig(
        exclude_categories={
            source: tuple(str(x).lower() for x in entries or ())
            for source, entries in (data.get("exclude_categories") or {}).items()
        },
        platform_domains=frozenset(str(x).lower() for x in data.get("platform_domains") or ()),
        platform_brands=frozenset(str(x).lower() for x in data.get("platform_brands") or ()),
    )
