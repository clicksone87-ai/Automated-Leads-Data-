from __future__ import annotations

import gzip
import io
import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from leads.sources.registries import companies_house
from leads.sources.registries.abn import AbnSource
from leads.sources.registries.abn import bulk_urls as abn_urls
from leads.sources.registries.brreg import BrregSource
from leads.sources.registries.canada import CanadaSource
from leads.sources.registries.companies_house import CompaniesHouseSource, bulk_file_name
from leads.sources.registries.download import download
from leads.sources.registries.sirene import SireneSource, country_of
from leads.sources.registries.sirene import bulk_urls as sirene_urls

SINCE = date(2018, 1, 1)


class TestDownload:
    BODY = b"0123456789"

    def client(self, seen: list[str | None], *, fail_first: bool = False) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            wanted = request.headers.get("Range")
            seen.append(wanted)
            if fail_first and len(seen) == 1:
                return httpx.Response(503)
            if wanted:
                start = int(wanted.removeprefix("bytes=").rstrip("-"))
                return httpx.Response(206, content=self.BODY[start:])
            return httpx.Response(200, content=self.BODY)

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_resumes_partial_file(self, tmp_path: Path) -> None:
        (tmp_path / "f.zip.part").write_bytes(self.BODY[:4])
        seen: list[str | None] = []
        path = download("https://x.test/f", tmp_path / "f.zip", client=self.client(seen))
        assert path.read_bytes() == self.BODY
        assert seen == ["bytes=4-"]
        assert not (tmp_path / "f.zip.part").exists()

    def test_retries_server_errors_and_reuses_fresh_file(self, tmp_path: Path) -> None:
        seen: list[str | None] = []
        client = self.client(seen, fail_first=True)
        for _ in range(2):
            download("https://x.test/f", tmp_path / "f", client=client, pause_seconds=0)
        assert len(seen) == 2

    def test_not_found_is_not_retried(self, tmp_path: Path) -> None:
        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404)))
        with pytest.raises(httpx.HTTPStatusError):
            download("https://x.test/f", tmp_path / "f", client=client, pause_seconds=0)


class TestCompaniesHouse:
    CSV = (
        "CompanyName, CompanyNumber,RegAddress.AddressLine1,RegAddress.PostTown,"
        "RegAddress.County,RegAddress.PostCode,CompanyStatus,IncorporationDate,SICCode.SicText_1\n"
        '"BLUE ""CAFE"" LTD","01","1 HIGH ST","LEEDS","","LS1 4AP","Active","31/01/2019",'
        '"56102 - Unlicensed restaurants and cafes"\n'
        '"OLD INN LTD","02","","LEEDS","","LS1 4AP","Active - Proposal to Strike off",'
        '"01/02/1990",""\n'
        '"GONE LTD","03","","","","","Liquidation","01/02/2020",""\n'
    )

    def test_bulk_file_name_takes_newest(self) -> None:
        page = "BasicCompanyDataAsOneFile-2026-10-01.zip BasicCompanyDataAsOneFile-2026-11-01.zip"
        assert bulk_file_name(page) == "BasicCompanyDataAsOneFile-2026-11-01.zip"
        with pytest.raises(RuntimeError):
            bulk_file_name("")

    def test_fetch_keeps_active_of_any_age(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        name = "BasicCompanyDataAsOneFile-2026-11-01.zip"
        folder = tmp_path / "registries" / "companies_house"
        folder.mkdir(parents=True)
        with zipfile.ZipFile(folder / name, "w") as zf:
            zf.writestr("data.csv", self.CSV)
        monkeypatch.setattr(companies_house, "get_text", lambda _: name)
        records = list(CompaniesHouseSource(tmp_path).fetch({"GB"}, SINCE))
        assert [(r.source_record_id, r.created_date) for r in records] == [
            ("01", date(2019, 1, 31)),
            ("02", date(1990, 2, 1)),
        ]
        assert records[0].name == 'BLUE "CAFE" LTD'
        assert records[0].postcode == "LS1 4AP"
        assert list(CompaniesHouseSource(tmp_path).fetch({"IE"}, SINCE)) == []

    def test_missing_column_is_named(self, tmp_path: Path) -> None:
        path = tmp_path / "c.csv"
        path.write_text(self.CSV.replace("IncorporationDate", "Started"))
        with pytest.raises(ValueError, match="IncorporationDate"):
            list(CompaniesHouseSource(tmp_path).rows(path))


def _brreg_files(tmp_path: Path) -> BrregSource:
    address = ["adresse", "poststed", "kommune", "postnummer", "landkode"]
    common = ["organisasjonsnummer", "navn", "organisasjonsform.kode", "naeringskode1.kode",
              "naeringskode1.beskrivelse", "hjemmeside", "epostadresse", "telefon",
              "mobil"]  # fmt: skip
    units = [*common, *(f"forretningsadresse.{a}" for a in address), "konkurs", "underAvvikling",
             "underTvangsavviklingEllerTvangsopplosning", "stiftelsesdato",
             "registreringsdatoenhetsregisteret"]  # fmt: skip
    sites = [*common, *(f"beliggenhetsadresse.{a}" for a in address), "overordnetEnhet",
             "oppstartsdato", "nedleggelsesdato"]  # fmt: skip

    def unit(number: str, **kw: str) -> dict[str, str]:
        return {"organisasjonsnummer": number, "navn": f"U{number}", "organisasjonsform.kode": "AS",
                "konkurs": "false", "underAvvikling": "false", "stiftelsesdato": "2019-01-01",
                "underTvangsavviklingEllerTvangsopplosning": "false",
                "forretningsadresse.landkode": "NO", **kw}  # fmt: skip

    unit_rows = [
        unit("1", stiftelsesdato="1977-06-15", epostadresse="post@one.no"),
        unit("2", konkurs="true"),
        unit("3", **{"organisasjonsform.kode": "FLI"}),
        unit("4", **{"organisasjonsform.kode": "ENK", "epostadresse": "me@home.no"}),
    ]
    site_rows = [
        {"organisasjonsnummer": "11", "navn": "Shop", "overordnetEnhet": "1",
         "oppstartsdato": "2021-01-01", "naeringskode1.kode": "56.110",
         "naeringskode1.beskrivelse": "Restauranter"},
        {"organisasjonsnummer": "12", "navn": "Closed", "overordnetEnhet": "1",
         "nedleggelsesdato": "2024-01-01"},
    ]  # fmt: skip

    def write(kind: str, columns: list[str], rows: list[dict[str, str]]) -> Path:
        lines = [",".join(f'"{c}"' for c in columns)]
        lines += [",".join(f'"{r.get(c, "")}"' for c in columns) for r in rows]
        path = tmp_path / f"{kind}.csv.gz"
        path.write_bytes(gzip.compress("\n".join(lines).encode()))
        return path

    files = (write("enheter", units, unit_rows), write("underenheter", sites, site_rows))
    source = BrregSource(tmp_path)
    source.files = lambda: files  # type: ignore[method-assign]
    return source


def test_brreg_sites_inherit_company_date_and_contacts(tmp_path: Path) -> None:
    records = {r.source_record_id: r for r in _brreg_files(tmp_path).fetch({"NO"}, SINCE)}
    assert set(records) == {"11", "4"}
    shop = records["11"]
    assert shop.created_date == date(1977, 6, 15)
    assert shop.email == "post@one.no"
    assert shop.category == "56.110 Restauranter"


def test_brreg_sole_trader_email_is_not_kept(tmp_path: Path) -> None:
    records = {r.source_record_id: r for r in _brreg_files(tmp_path).fetch({"NO"}, SINCE)}
    assert records["4"].email is None


def test_canada_both_name_forms(tmp_path: Path) -> None:
    path = tmp_path / "c.csv"
    path.write_text(
        "﻿Corporation number,Corporate name - form 1,Corporate name - form 2,Status,"
        "Anniversary date,Street,City/town,Province/territory,Country,Postal code\n"
        "1,BLUE CAFE INC.,CAFÉ BLEU INC.,Active,2019-12-10,1 ST,TORONTO,ON,CA,M8Z 5X2\n"
        "2,ABROAD INC.,,Active,2019-12-10,1 ST,NYC,NY,US,10001\n",
        encoding="utf-8",
    )
    source = CanadaSource(tmp_path)
    records = [r for row in source.rows(path) for r in source.to_records(row)]
    assert [(r.source_record_id, r.name) for r in records] == [
        ("1", "BLUE CAFE INC."),
        ("1:2", "CAFÉ BLEU INC."),
    ]
    assert records[0].created_date == date(2019, 12, 10)


ABN_XML = b"""<?xml version="1.0"?><Transfer><TransferInfo/>
<ABR><ABN status="ACT" ABNStatusFromDate="20190501">11</ABN><MainEntity>
<NonIndividualName type="MN"><NonIndividualNameText>BLUE PTY LTD</NonIndividualNameText>
</NonIndividualName><BusinessAddress><AddressDetails><State>NSW</State><Postcode>2000</Postcode>
</AddressDetails></BusinessAddress></MainEntity><OtherEntity><NonIndividualName type="BN">
<NonIndividualNameText>BLUE CAFE</NonIndividualNameText></NonIndividualName></OtherEntity></ABR>
<ABR><ABN status="CAN" ABNStatusFromDate="20190501">12</ABN><MainEntity>
<NonIndividualName type="MN"><NonIndividualNameText>GONE</NonIndividualNameText>
</NonIndividualName></MainEntity></ABR>
<ABR><ABN status="ACT" ABNStatusFromDate="20200101">13</ABN><LegalEntity>
<IndividualName type="LGL"><FamilyName>PERSON</FamilyName></IndividualName></LegalEntity></ABR>
</Transfer>"""


def test_abn_names_status_and_individuals(tmp_path: Path) -> None:
    records = list(AbnSource(tmp_path).parse(io.BytesIO(ABN_XML)))
    assert [(r.source_record_id, r.name) for r in records] == [
        ("11", "BLUE PTY LTD"),
        ("11:1", "BLUE CAFE"),
    ]
    assert records[1].postcode == "2000"
    assert records[1].created_date == date(2019, 5, 1)


def test_abn_urls() -> None:
    urls = [{"url": "https://x/b.zip"}, {"url": "https://x/a.ZIP"}, {"url": "https://x/r.pdf"}]
    assert abn_urls(json.dumps({"result": {"resources": urls}})) == [
        "https://x/a.ZIP",
        "https://x/b.zip",
    ]


def test_sirene_urls_and_country() -> None:
    base = "https://static/20261103/stock-"
    names = ["stocketablissement-parquet.parquet", "stockunitelegale-parquet.parquet",
             "stocketablissementhistorique-parquet.parquet"]  # fmt: skip
    data = json.dumps({"resources": [{"url": base + n} for n in names]})
    assert sirene_urls(data) == (base + names[0], base + names[1])
    assert (country_of("97411"), country_of("75056"), country_of(None)) == ("RE", "FR", "FR")


def test_sirene_query(tmp_path: Path) -> None:
    site: dict[str, Any] = {
        "siret": "'1'", "siren": "'s'", "enseigne1Etablissement": "'LE CAFE'",
        "denominationUsuelleEtablissement": "NULL::VARCHAR", "codeCommuneEtablissement": "'75056'",
        "activitePrincipaleEtablissement": "'56.10A'", "libelleCommuneEtablissement": "'PARIS'",
        "codePostalEtablissement": "'75001'", "dateCreationEtablissement": "DATE '2022-01-01'",
        "etatAdministratifEtablissement": "'A'", "statutDiffusionEtablissement": "'O'",
    }  # fmt: skip
    unit = {
        "siren": "'s'", "denominationUsuelle1UniteLegale": "NULL::VARCHAR",
        "denominationUniteLegale": "'SARL X'", "dateCreationUniteLegale": "DATE '2015-01-01'",
        "statutDiffusionUniteLegale": "'O'",
    }  # fmt: skip

    def select(values: dict[str, Any], **changed: str) -> str:
        return "SELECT " + ", ".join(f"{v} AS {k}" for k, v in {**values, **changed}.items())

    sites, units = tmp_path / "s.parquet", tmp_path / "u.parquet"
    rows = [
        select(site),
        select(site, siret="'2'", etatAdministratifEtablissement="'F'"),
        select(site, siret="'3'", statutDiffusionEtablissement="'P'"),
    ]
    duckdb.sql(f"COPY ({' UNION ALL '.join(rows)}) TO '{sites}' (FORMAT parquet)")
    duckdb.sql(f"COPY ({select(unit)}) TO '{units}' (FORMAT parquet)")
    source = SireneSource(tmp_path)
    cursor = duckdb.connect().execute(source.query(str(sites), str(units)))
    columns = [d[0] for d in cursor.description or []]
    (values,) = cursor.fetchall()
    record = source.to_record(dict(zip(columns, values, strict=True)))
    assert (record.source_record_id, record.name) == ("1", "LE CAFE")
    assert record.created_date == date(2015, 1, 1)
