"""Load a collect run into PostgreSQL: normalise, filter, match, resolve age, upsert.

Work is done one country at a time, so memory is bounded by the largest country. Rows of
the newest earlier run of every *other* source join the matching, so collecting sources
separately neither duplicates a business nor loses its age evidence.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import and_, case, delete, exists, func, null, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from leads.db import finish_run, lock_leads, session_scope
from leads.models import Lead, LeadSource, LeadStatus, Outreach, Run, point_ewkt
from leads.pipeline.age import AgeEvidence, AgeResult, keep_lead, resolve_age
from leads.pipeline.match import (
    MatchRecord,
    cluster_records,
    lead_id_for_cluster,
    make_lead_id,
    merge_fields,
    sort_by_priority,
)
from leads.pipeline.normalise import (
    SOCIAL_DOMAINS,
    is_non_business,
    is_social_domain,
    normalise_country,
    normalise_domain,
    normalise_frame,
    normalise_name,
    normalise_phone,
    normalise_postcode,
)
from leads.pipeline.wayback import load_first_captures
from leads.settings import Settings, get_settings, load_categories, load_countries, load_filters
from leads.sources.base import RAW_SCHEMA, raw_dir
from leads.sources.registries import REGISTRY_SOURCES

# Columns refreshed on an existing lead. Website-check results, sanctions flags and
# first_seen belong to other steps and are never overwritten here.
_KEEP_IF_EMPTY = (
    "category", "phone_e164", "phone_type", "email", "website", "domain", "address", "city",
    "region", "postcode", "geom",
)  # fmt: skip
_ALWAYS_UPDATE = (
    "name", "category_group", "country_iso2", "created_date", "age_source", "age_unknown",
    "status", "sources", "last_seen",
)  # fmt: skip


@dataclass(slots=True)
class CountryInput:
    """Normalised rows of one country; ``current`` marks rows collected in this run."""

    rows: list[dict[str, Any]]
    current: list[bool]
    excluded: int = 0


# --- raw zone --------------------------------------------------------------------------


def _scan(directory: Path) -> pl.LazyFrame:
    # PostgreSQL text and jsonb cannot hold NUL characters, which some source records contain.
    return (
        pl.scan_parquet(str(directory / "*.parquet"), hive_partitioning=False)
        .select(list(RAW_SCHEMA))
        .with_columns(
            pl.col(pl.String)
            .str.replace_all("\x00", "", literal=True)
            .str.replace_all("\\u0000", "", literal=True)
        )
    )


def _run_sources(data_dir: Path, run_id: str) -> dict[str, Path]:
    """``{source: directory}`` for every source with parquet parts in this run."""
    found: dict[str, Path] = {}
    for directory in sorted((data_dir / "raw").glob(f"source=*/run={run_id}")):
        if any(directory.glob("*.parquet")):
            found[directory.parent.name.removeprefix("source=")] = directory
    return found


def _earlier_runs(data_dir: Path, current: dict[str, Path]) -> dict[str, list[Path]]:
    """Runs (newest first) of the sources that are not part of the current run."""
    earlier: dict[str, list[Path]] = {}
    for source_dir in sorted((data_dir / "raw").glob("source=*")):
        source = source_dir.name.removeprefix("source=")
        runs = [d for d in source_dir.glob("run=*") if any(d.glob("*.parquet"))]
        if source not in current and runs:
            earlier[source] = sorted(runs, key=lambda d: d.stat().st_mtime, reverse=True)
    return earlier


def _raw_countries(directories: Iterable[Path]) -> dict[str, set[str]]:
    """``{ISO2: raw spellings}`` present in the given raw directories."""
    spellings: dict[str, set[str]] = defaultdict(set)
    for directory in directories:
        for value in _scan(directory).select("country_iso2").unique().collect().to_series():
            code = normalise_country(value)
            if code:
                spellings[code].add(value)
    return spellings


def _read_country(directory: Path, spellings: set[str]) -> pl.DataFrame:
    return _scan(directory).filter(pl.col("country_iso2").is_in(sorted(spellings))).collect()


def _has_country(directory: Path, spellings: set[str]) -> bool:
    wanted = pl.col("country_iso2").is_in(sorted(spellings))
    return bool(_scan(directory).select(wanted.any()).collect().item())


@dataclass(frozen=True, slots=True)
class PlaceKeys:
    """Identifying values of one country's places, to pick the registry rows that matter."""

    name_postcodes: frozenset[tuple[str, str]]
    postcodes: frozenset[str]
    phones: frozenset[str]
    domains: frozenset[str]

    @classmethod
    def of(cls, rows: Iterable[dict[str, Any]]) -> PlaceKeys:
        rows = list(rows)
        pairs = {(r["name_norm"], r["postcode_key"]) for r in rows if r["postcode_key"]}
        return cls(
            name_postcodes=frozenset(pairs),
            postcodes=frozenset(p for _, p in pairs),
            phones=frozenset(r["phone_e164"] for r in rows if r["phone_e164"]),
            domains=frozenset(r["domain"] for r in rows if r["domain"]),
        )


def _wanted_registry_row(row: dict[str, Any], keys: PlaceKeys, settings: Settings) -> bool:
    postcode = normalise_postcode(row["postcode"])
    if (
        postcode in keys.postcodes
        and (normalise_name(row["name"]), postcode) in keys.name_postcodes
    ):
        return True
    contactable = bool(row["email"] or row["phone"] or row["website"])
    if contactable or settings.registry.uncontactable_leads:
        created = row["created_date"]
        if created is not None and created >= settings.since:
            return True
    if contactable:
        phone = normalise_phone(row["phone"], normalise_country(row["country_iso2"]))
        if phone and phone.e164 in keys.phones:
            return True
        return normalise_domain(row["website"]) in keys.domains
    return False


def _registry_rows(
    directory: Path, spellings: set[str], keys: PlaceKeys, settings: Settings
) -> Iterator[pl.DataFrame]:
    """Registry rows matching a known place, or able to be leads themselves.

    Registries list millions of companies, so they are read part by part.
    """
    for path in sorted(directory.glob("*.parquet")):
        frame = pl.read_parquet(path, hive_partitioning=False).filter(
            pl.col("country_iso2").is_in(sorted(spellings))
        )
        mask = [_wanted_registry_row(r, keys, settings) for r in frame.iter_rows(named=True)]
        wanted = frame.filter(pl.Series(mask, dtype=pl.Boolean))
        if wanted.height:
            yield wanted


def _load_country(
    country: str,
    spellings: set[str],
    current: dict[str, Path],
    earlier: dict[str, list[Path]],
    run_countries: frozenset[str],
    settings: Settings,
) -> CountryInput:
    categories, filters = load_categories(), load_filters()
    result = CountryInput(rows=[], current=[])

    def add(frame: pl.DataFrame, is_current: bool) -> None:
        # A release can repeat a record; one row per source record keeps upserts stable.
        unique = frame.unique(subset=["source", "source_record_id"], keep="last")
        extras = {
            (r["source"], r["source_record_id"]): r["raw_json"]
            for r in unique.select("source", "source_record_id", "raw_json").iter_rows(named=True)
        }
        for row in normalise_frame(unique, categories, run_countries).iter_rows(named=True):
            raw = extras[(row["source"], row["source_record_id"])]
            details = json.loads(raw) if raw else {}
            path = details.get("category_path") or ()
            if is_non_business(row["source"], row["category"], path, filters):
                result.excluded += is_current
                continue
            result.rows.append({**row, "raw": details})
            result.current.append(is_current)

    inputs: list[tuple[str, Path, bool]] = [(s, d, True) for s, d in current.items()]
    for source, runs in earlier.items():
        newest = next((d for d in runs if _has_country(d, spellings | {country})), None)
        if newest:
            inputs.append((source, newest, False))

    for source, directory, is_current in inputs:
        if source not in REGISTRY_SOURCES:
            add(_read_country(directory, spellings | {country}), is_current)
    keys = PlaceKeys.of(result.rows)
    for source, directory, is_current in inputs:
        if source in REGISTRY_SOURCES:
            for frame in _registry_rows(directory, spellings | {country}, keys, settings):
                add(frame, is_current)
    return result


# --- pure row building -----------------------------------------------------------------


def to_match_record(row: dict[str, Any]) -> MatchRecord:
    domain = row["domain"]
    return MatchRecord(
        source=row["source"],
        source_record_id=row["source_record_id"],
        name_norm=row["name_norm"],
        country_iso2=row["country_iso2"],
        # facebook.com is not the business's own domain, so it must not join businesses.
        domain=None if is_social_domain(domain) else domain,
        phone_e164=row["phone_e164"],
        postcode_key=row["postcode_key"],
        lat=row["lat"],
        lon=row["lon"],
        shared_ids=tuple(row["shared_ids"] or ()),
    )


def age_evidence(
    rows: Sequence[dict[str, Any]], first_captures: Mapping[str, date] | None = None
) -> AgeEvidence:
    """Earliest date each kind of source gives for one business."""

    def earliest(sources: Iterable[str]) -> date | None:
        wanted = set(sources)
        dates = [r["created_date"] for r in rows if r["source"] in wanted and r["created_date"]]
        return min(dates, default=None)

    known = first_captures or {}
    captures = [known[r["domain"]] for r in rows if r["domain"] in known]
    return AgeEvidence(
        wayback_first_capture=min(captures, default=None),
        registry=earliest(REGISTRY_SOURCES),
        fsq_date_created=earliest(("foursquare",)),
        osm_first_version=earliest(("osm",)),
    )


def socials_from_rows(rows: Sequence[dict[str, Any]]) -> dict[str, str]:
    """``{"facebook": url, ...}`` from the social URLs the sources list."""
    found: dict[str, str] = {}
    for row in rows:
        if row["listing_url"]:
            found.setdefault("listing", row["listing_url"])
        for url in row["raw"].get("socials") or ():
            domain = normalise_domain(url)
            if domain in SOCIAL_DOMAINS:
                found.setdefault(SOCIAL_DOMAINS[domain], url)
    return found


def build_lead(
    lead_id: str,
    rows: Sequence[dict[str, Any]],
    *,
    age: AgeResult,
    seen_at: datetime,
) -> dict[str, Any]:
    """One ``leads`` row from the normalised rows of a cluster."""
    lead = merge_fields(rows)
    lat, lon = lead.pop("lat"), lead.pop("lon")
    return {
        **lead,
        "lead_id": lead_id,
        "geom": point_ewkt(lon, lat) if lat is not None and lon is not None else None,
        "socials": socials_from_rows(sort_by_priority(rows)),
        "created_date": age.created_date,
        "age_source": age.age_source,
        "age_unknown": age.age_unknown,
        "status": LeadStatus.ACTIVE.value,
        "first_seen": seen_at,
        "last_seen": seen_at,
    }


# --- database --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExistingLead:
    lead_id: str
    first_seen: datetime | None
    created_date: date | None
    age_source: str | None
    age_unknown: bool
    contacted: bool


def _chunks[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _existing_ids(
    session: Session, rows: Sequence[dict[str, Any]], chunk: int
) -> dict[tuple[str, str], str]:
    """``{(source, source_record_id): lead_id}`` for records loaded before."""
    by_source: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        by_source[row["source"]].append(row["source_record_id"])
    found: dict[tuple[str, str], str] = {}
    for source, ids in by_source.items():
        for part in _chunks(ids, chunk):
            stmt = select(LeadSource.source_record_id, LeadSource.lead_id).where(
                LeadSource.source == source, LeadSource.source_record_id.in_(part)
            )
            found.update({(source, rid): lead_id for rid, lead_id in session.execute(stmt)})
    return found


def _existing_leads(session: Session, ids: Sequence[str], chunk: int) -> dict[str, ExistingLead]:
    contacted = exists().where(Outreach.lead_id == Lead.lead_id)
    found: dict[str, ExistingLead] = {}
    for part in _chunks(ids, chunk):
        stmt = select(
            Lead.lead_id,
            Lead.first_seen,
            Lead.created_date,
            Lead.age_source,
            Lead.age_unknown,
            contacted,
        ).where(Lead.lead_id.in_(part))
        for row in session.execute(stmt):
            found[row[0]] = ExistingLead(*row)
    return found


def choose_survivor(leads: Sequence[ExistingLead]) -> ExistingLead:
    """Lead id kept when one business had several: contacted first, then the oldest."""
    latest = datetime.max.replace(tzinfo=UTC)
    return min(leads, key=lambda x: (not x.contacted, x.first_seen or latest, x.lead_id))


def combine_age(
    fresh: AgeResult, existing: Sequence[ExistingLead], first_seen: datetime
) -> AgeResult:
    """Earliest known date from this run and from what was already stored."""
    known = [(e.created_date, e.age_source) for e in existing if not e.age_unknown]
    if not fresh.age_unknown:
        known.append((fresh.created_date, fresh.age_source))
    dated = [(d, s or "") for d, s in known if d is not None]
    if dated:
        created, source = min(dated)
        return AgeResult(created_date=created, age_source=source, age_unknown=False)
    return AgeResult(created_date=first_seen.date(), age_source="first_seen", age_unknown=True)


def _upsert_leads(session: Session, leads: Sequence[dict[str, Any]], chunk: int) -> None:
    if not leads:
        return
    stmt = insert(Lead)
    new = stmt.excluded
    changes: dict[str, Any] = {c: new[c] for c in _ALWAYS_UPDATE}
    changes |= {c: func.coalesce(new[c], getattr(Lead, c)) for c in _KEEP_IF_EMPTY}
    changes["socials"] = new["socials"].op("||")(Lead.socials)
    # A different website has not been checked yet.
    moved = and_(new["website"].is_not(None), new["website"].is_distinct_from(Lead.website))
    for column in (Lead.website_status, Lead.website_reason, Lead.website_checked_at):
        changes[column.key] = case((moved, null()), else_=column)
    changes["updated_at"] = func.now()
    upsert = stmt.on_conflict_do_update(index_elements=["lead_id"], set_=changes)
    for part in _chunks(leads, chunk):
        session.execute(upsert, list(part))


def _upsert_sources(session: Session, records: Sequence[dict[str, Any]], chunk: int) -> None:
    if not records:
        return
    stmt = insert(LeadSource)
    new = stmt.excluded
    upsert = stmt.on_conflict_do_update(
        constraint="uq_lead_sources_source_record",
        set_={
            "lead_id": new["lead_id"],
            "raw": new["raw"],
            "fetched_at": new["fetched_at"],
            # Carried-over rows have no run id of their own; keep the one already stored.
            "run_id": func.coalesce(new["run_id"], LeadSource.run_id),
        },
    )
    for part in _chunks(records, chunk):
        session.execute(upsert, list(part))


def _merge_away(session: Session, pairs: Sequence[tuple[str, str]]) -> None:
    """Move everything from each duplicate lead to its survivor, then delete it."""
    for loser, survivor in pairs:
        session.execute(
            update(LeadSource).where(LeadSource.lead_id == loser).values(lead_id=survivor)
        )
        taken = aliased(Outreach)
        session.execute(
            update(Outreach)
            .where(
                Outreach.lead_id == loser,
                ~exists().where(
                    taken.lead_id == survivor,
                    taken.channel == Outreach.channel,
                    taken.is_followup == Outreach.is_followup,
                ),
            )
            .values(lead_id=survivor)
            .execution_options(synchronize_session=False)
        )
        session.execute(delete(Lead).where(Lead.lead_id == loser))


def _retire(session: Session, retired: Sequence[tuple[str, AgeResult]]) -> None:
    """Remove leads now known to be too old; contacted ones are closed to keep history."""
    contacted = exists().where(Outreach.lead_id == Lead.lead_id)
    for lead_id, age in retired:
        session.execute(
            update(Lead)
            .where(Lead.lead_id == lead_id, contacted)
            .values(
                status=LeadStatus.CLOSED.value,
                created_date=age.created_date,
                age_source=age.age_source,
                age_unknown=False,
            )
            .execution_options(synchronize_session=False)
        )
        session.execute(
            delete(Lead)
            .where(Lead.lead_id == lead_id, ~contacted)
            .execution_options(synchronize_session=False)
        )


def _drop_sources(session: Session, keys: Sequence[tuple[str, str]], chunk: int) -> None:
    """Forget too-old records, except those documenting a lead kept for its history."""
    contacted = exists().where(Outreach.lead_id == LeadSource.lead_id)
    by_source: dict[str, list[str]] = defaultdict(list)
    for source, record_id in keys:
        by_source[source].append(record_id)
    for source, ids in by_source.items():
        for part in _chunks(ids, chunk):
            session.execute(
                delete(LeadSource)
                .where(
                    LeadSource.source == source,
                    LeadSource.source_record_id.in_(part),
                    ~contacted,
                )
                .execution_options(synchronize_session=False)
            )


def _keep(age: AgeResult, settings: Settings) -> bool:
    return keep_lead(LeadStatus.ACTIVE, age.created_date, age.age_unknown, settings.since)


def load_country(
    session: Session,
    data: CountryInput,
    *,
    run_id: uuid.UUID,
    seen_at: datetime,
    settings: Settings,
) -> Counter[str]:
    """Match one country's rows and write the resulting leads."""
    rows, chunk = data.rows, settings.batch.db_chunk_size
    matches = [to_match_record(r) for r in rows]
    clusters = cluster_records(
        matches,
        name_threshold=settings.match.name_score_threshold,
        shared_key_threshold=settings.match.shared_key_name_score,
        radius_m=settings.match.geo_radius_m,
        max_shared_key_group=settings.match.max_shared_key_group,
    )
    known_ids = _existing_ids(session, rows, chunk)
    stored = _existing_leads(session, sorted(set(known_ids.values())), chunk)

    captures = load_first_captures(session, {r["domain"] for r in rows if r["domain"]}, chunk)
    counts: Counter[str] = Counter(
        rows=sum(data.current), clusters=len(clusters), not_business=data.excluded
    )
    leads: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    merged: list[tuple[str, str]] = []
    retired: list[tuple[str, AgeResult]] = []
    claimed: set[str] = set()
    dropped: list[tuple[str, str]] = []

    candidates = []
    for members in clusters:
        if any(data.current[i] for i in members):
            group = [rows[i] for i in members]
            candidates.append((members, group, resolve_age(age_evidence(group, captures))))
    # Too-old clusters go last, so they never take a lead that a kept cluster still needs.
    candidates.sort(key=lambda c: not _keep(c[2], settings))

    for members, group, fresh in candidates:
        ids = {known_ids.get((r["source"], r["source_record_id"])) for r in group}
        # A lead whose records now fall into two clusters stays with the first of them.
        existing = [stored[i] for i in sorted(x for x in ids if x) if i not in claimed]
        first_seen = min((e.first_seen for e in existing if e.first_seen), default=seen_at)
        age = combine_age(fresh, existing, first_seen)

        if not _keep(age, settings):
            counts["too_old"] += 1
            dropped.extend((r["source"], r["source_record_id"]) for r in group)
            retired.extend((e.lead_id, age) for e in existing)
            claimed.update(e.lead_id for e in existing)
            continue

        if existing:
            lead_id = choose_survivor(existing).lead_id
            merged.extend((e.lead_id, lead_id) for e in existing if e.lead_id != lead_id)
            counts["updated"] += 1
        else:
            lead_id = lead_id_for_cluster([matches[i] for i in members])
            if lead_id in claimed or lead_id in stored:
                # Split-off cluster whose natural id is taken: derive one from a free record.
                lead_id = _free_id([matches[i] for i in members], claimed | stored.keys())
            counts["new"] += 1
        claimed.update([lead_id, *(e.lead_id for e in existing)])
        counts["age_unknown"] += age.age_unknown

        leads.append(build_lead(lead_id, group, age=age, seen_at=seen_at))
        records.extend(
            {
                "lead_id": lead_id,
                "source": rows[i]["source"],
                "source_record_id": rows[i]["source_record_id"],
                "raw": rows[i]["raw"],
                "fetched_at": seen_at,
                "run_id": run_id if data.current[i] else None,
            }
            for i in members
        )

    _upsert_leads(session, leads, chunk)
    _upsert_sources(session, records, chunk)
    _merge_away(session, merged)
    _drop_sources(session, dropped, chunk)
    _retire(session, retired)
    counts["merged"] = len(merged)
    counts["retired"] = len(retired)
    return counts


def _free_id(records: Sequence[MatchRecord], taken: Iterable[str]) -> str:
    used = set(taken)
    for rec in records:
        candidate = make_lead_id(rec.source, rec.source_record_id)
        if candidate not in used:
            return candidate
    first, n = records[0], 1
    while (candidate := make_lead_id(first.source, f"{first.source_record_id}#{n}")) in used:
        n += 1
    return candidate


def _ensure_run(run_id: uuid.UUID) -> bool:
    """Create the ``runs`` row for a CLI collect. True if this call created it."""
    with session_scope() as session:
        result = session.execute(
            insert(Run)
            .values(run_id=run_id, flow="cli-filter", status="running")
            .on_conflict_do_nothing()
            .returning(Run.run_id)
        )
        return result.first() is not None


def process_run(run_id: uuid.UUID | str) -> dict[str, Any]:
    """Process one collect run. Safe to repeat: the same run never duplicates leads."""
    run_uuid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(run_id)
    settings = get_settings()
    current = _run_sources(settings.data_dir, str(run_uuid))
    if not current:
        missing = raw_dir(settings.data_dir, "*", str(run_uuid))
        raise FileNotFoundError(f"No raw data for run {run_uuid} ({missing})")

    earlier = _earlier_runs(settings.data_dir, current)
    included = load_countries()
    spellings = _raw_countries(current.values())
    created = _ensure_run(run_uuid)
    seen_at = datetime.now(UTC)
    totals: Counter[str] = Counter()
    totals["countries_skipped"] = sum(1 for c in spellings if c not in included)
    try:
        for country in sorted(c for c in spellings if c in included):
            data = _load_country(country, spellings[country], current, earlier, included, settings)
            with session_scope() as session:
                lock_leads(session)
                totals += load_country(
                    session, data, run_id=run_uuid, seen_at=seen_at, settings=settings
                )
    except Exception:
        if created:
            finish_run(run_uuid, "failed", dict(totals))
        raise
    if created:
        finish_run(run_uuid, "success", dict(totals))
    return dict(totals)
