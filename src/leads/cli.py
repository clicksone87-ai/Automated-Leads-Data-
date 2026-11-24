"""``leads`` command-line interface. Each command runs one step directly (no Prefect).

Heavy modules are imported inside commands so ``leads --help`` stays fast.
"""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from leads.log import configure_logging
from leads.settings import get_settings

app = typer.Typer(
    name="leads",
    help="Lead automation: collect, filter, check websites, prepare Gmail drafts. Never sends.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    settings = get_settings()
    configure_logging("DEBUG" if verbose else settings.log_level, json=settings.log_json)


def _run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        result = fn(*args, **kwargs)
    except NotImplementedError as exc:
        typer.secho(f"Not implemented yet: {exc}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2) from None
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    if result is not None:
        typer.echo(result)
    return result


@app.command()
def collect(
    source: Annotated[
        list[str] | None, typer.Option("--source", "-s", help="Source name (repeatable)")
    ] = None,
    country: Annotated[
        list[str] | None, typer.Option("--country", "-c", help="Limit to ISO-2 code (repeatable)")
    ] = None,
    run_id: Annotated[str | None, typer.Option(help="Reuse a run id (default: new)")] = None,
) -> None:
    """Fetch sources into the raw zone (data/raw/source=*/run=*)."""
    from leads.settings import load_countries
    from leads.sources import collect as collect_sources

    settings = get_settings()
    names = source or [n for n in settings.sources if settings.source_enabled(n)]
    countries = {c.upper() for c in country} if country else load_countries()
    run = run_id or str(uuid.uuid4())
    typer.echo(f"run_id={run} sources={names} countries={len(countries)}")
    _run(
        collect_sources,
        names,
        settings=settings,
        countries=countries,
        since=settings.since,
        run_id=run,
    )


@app.command("filter")
def filter_(run_id: Annotated[str, typer.Argument(help="Run id printed by `collect`")]) -> None:
    """Normalise, country/age filter, dedupe and load a collect run into PostgreSQL."""
    from leads.pipeline import process_run

    _run(process_run, run_id)


@app.command("check-websites")
def check_websites(
    limit: Annotated[int | None, typer.Option(help="Max leads to check")] = None,
    recheck: Annotated[
        bool, typer.Option(help="Also re-check websites older than 30 days")
    ] = False,
) -> None:
    """Check lead websites and extract public emails / WhatsApp numbers."""
    from leads.enrich.website import check_pending

    _run(check_pending, limit, recheck=recheck)


@app.command()
def drafts(
    batch_size: Annotated[int | None, typer.Option(help="Default: batch.drafts_per_day")] = None,
    dry_run: Annotated[
        bool, typer.Option(help="Render only; no Gmail calls, no DB writes")
    ] = False,
) -> None:
    """Create today's Gmail drafts (you review and send them in Gmail)."""
    from leads.outreach.gmail_drafts import create_daily_drafts

    _run(create_daily_drafts, batch_size, dry_run=dry_run)


@app.command()
def sync() -> None:
    """Sync Gmail: sent / replied / bounced / opted out, follow-up drafts."""
    from leads.outreach.gmail_sync import sync as gmail_sync

    _run(gmail_sync)


@app.command()
def sheet() -> None:
    """Rewrite the Google Sheet lead view."""
    from leads.export.sheets import update_sheet

    _run(update_sheet)


@app.command()
def wayback(
    limit: Annotated[int | None, typer.Option(help="Max domains to look up")] = None,
) -> None:
    """Date unknown-age leads from the first archived copy of their website."""
    from leads.pipeline.wayback import check_wayback

    _run(check_wayback, limit)


@app.command()
def refresh(run_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """Monthly refresh: last_seen, close missing leads, queue website re-checks."""
    from leads.pipeline.refresh import run_refresh

    _run(run_refresh, run_id)


@app.command()
def sanctions() -> None:
    """Download the OFAC SDN list and screen all leads."""
    from leads.pipeline.sanctions import screen_leads

    _run(screen_leads, refresh_list=True)


@app.command("export-csv")
def export_csv(out: Annotated[Path | None, typer.Option(help="Output CSV path")] = None) -> None:
    """Export eligible leads to CSV (with attribution footer)."""
    from leads.export.csv_export import export_leads

    _run(export_leads, out)


@app.command("db-check")
def db_check() -> None:
    """Check the database connection and PostGIS."""
    from leads.db import check_connection

    for key, value in check_connection().items():
        typer.echo(f"{key}: {value}")


@app.command("show-config")
def show_config() -> None:
    """Print the effective settings (secrets masked) and country count."""
    from leads.settings import load_countries

    settings = get_settings()
    typer.echo(settings.model_dump_json(indent=2, exclude={"database_url"}))
    typer.echo(f"countries included: {len(load_countries())}")


if __name__ == "__main__":
    app()
