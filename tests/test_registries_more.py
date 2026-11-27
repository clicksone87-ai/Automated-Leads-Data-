"""Finland, Estonia, Czechia, New York and Colorado registry loaders, on rows shaped like
the real bulk files."""

from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from leads.pipeline.normalise import NACE_SOURCES
from leads.sources import SOURCE_FACTORIES
from leads.sources.registries import REGISTRY_SOURCES, prh
from leads.sources.registries.prh import PrhSource
from leads.sources.registries.res import ResSource
from leads.sources.registries.rik import RikSource, city_of
from leads.sources.registries.us_states import ColoradoSource, NewYorkSource

SINCE = date(2018, 1, 1)


def fi_company(**changes: Any) -> dict[str, Any]:
    company: dict[str, Any] = {
        "businessId": {"value": "3498966-2", "registrationDate": "2024-12-27"},
        "names": [
            {"name": "Old Name Oy", "type": "1", "registrationDate": "2024-12-27",
             "endDate": "2025-03-02"},
            {"name": "LeonX Ventures Oy", "type": "1", "registrationDate": "2025-03-03"},
        ],
        "mainBusinessLine": {
            "type": "64210",
            "descriptions": [
                {"languageCode": "3", "description": "Activities of holding companies"},
                {"languageCode": "1", "description": "Holdingyhtiöiden toiminta"},
            ],
        },
        "companyForms": [
            {"type": "16", "descriptions": [{"languageCode": "3", "description": "Limited company"}]}
        ],
        "companySituations": [],
        "addresses": [
            {"type": 2, "street": "PL 5", "postCode": "00101", "postOffices": [
                {"city": "HELSINKI", "languageCode": "1"}]},
            {"type": 1, "street": "Kutojantie", "buildingNumber": "6-8", "postCode": "02630",
             "postOffices": [{"city": "ESBO", "languageCode": "2"},
                             {"city": "ESPOO", "languageCode": "1"}]},
        ],
        "website": {"url": "www.leonx.fi"},
        "tradeRegisterStatus": "1",
        "registrationDate": "2025-03-03",
    }  # fmt: skip
    company.update(changes)
    return company


class TestFinland:
    def test_current_name_visiting_address_and_earliest_date(self) -> None:
        record = prh.to_record(fi_company())
        assert record is not None
        assert record.name == "LeonX Ventures Oy"
        assert (record.address, record.city, record.postcode) == (
            "Kutojantie 6-8", "Espoo", "02630",
        )  # fmt: skip
        assert record.created_date == date(2024, 12, 27)
        assert record.category == "64210 Activities of holding companies"
        assert record.website == "www.leonx.fi"
        assert record.shared_ids == ["prh:3498966-2"]

    @pytest.mark.parametrize(
        "changes",
        [{"tradeRegisterStatus": "2"}, {"companySituations": [{"type": "KONK"}]}],
    )
    def test_closed_or_bankrupt_is_left_out(self, changes: dict[str, Any]) -> None:
        assert prh.to_record(fi_company(**changes)) is None

    def test_reads_the_zipped_json_file(self, tmp_path: Path) -> None:
        folder = tmp_path / "registries" / "prh"
        folder.mkdir(parents=True)
        with zipfile.ZipFile(folder / "all_companies.zip", "w") as zf:
            zf.writestr("all_companies_20261127.json", json.dumps([fi_company()]))
        records = list(PrhSource(tmp_path).fetch(["FI"], SINCE))
        assert [r.name for r in records] == ["LeonX Ventures Oy"]
        assert list(PrhSource(tmp_path).fetch(["SE"], SINCE)) == []


EE_HEADER = (
    "﻿nimi;ariregistri_kood;ettevotja_oiguslik_vorm;ettevotja_oigusliku_vormi_alaliik;"
    "kmkr_nr;ettevotja_staatus;ettevotja_staatus_tekstina;ettevotja_esmakande_kpv;"
    "ettevotja_aadress;asukoht_ettevotja_aadressis;asukoha_ehak_kood;asukoha_ehak_tekstina;"
    "indeks_ettevotja_aadressis;ads_adr_id;ads_ads_oid;ads_normaliseeritud_taisaadress;"
    "teabesysteemi_link\n"
)


class TestEstonia:
    CSV = EE_HEADER + (
        "007 Agent & Partners OÜ;16752073;Osaühing;;;R;Registrisse kantud;05.06.2023;;"
        "Regati pst 12;0596;Pirita linnaosa, Tallinn, Harju maakond;11911;1;;x;x\n"
        "Vana Selts MTÜ;80000001;Mittetulundusühing;;;R;Registrisse kantud;01.01.2020;;"
        "Tee 1;0596;Tallinn, Harju maakond;10000;1;;x;x\n"
        "Pankrot OÜ;16000002;Osaühing;;;N;Pankrotis;01.01.2021;;Tee 2;0596;"
        "Tartu linn, Tartu linn, Tartu maakond;51004;1;;x;x\n"
    )

    def test_only_registered_businesses_with_their_first_entry_date(self, tmp_path: Path) -> None:
        folder = tmp_path / "registries" / "rik"
        folder.mkdir(parents=True)
        with zipfile.ZipFile(folder / "lihtandmed.csv.zip", "w") as zf:
            zf.writestr("ettevotja_rekvisiidid__lihtandmed.csv", self.CSV.encode())
        records = list(RikSource(tmp_path).fetch(["EE"], SINCE))
        assert [r.name for r in records] == ["007 Agent & Partners OÜ"]
        record = records[0]
        assert record.created_date == date(2023, 6, 5)
        assert (record.address, record.city, record.postcode) == (
            "Regati pst 12", "Tallinn", "11911",
        )  # fmt: skip

    def test_city_of(self) -> None:
        assert city_of("Tartu linn, Tartu linn, Tartu maakond") == "Tartu linn"
        assert city_of(None) is None


class TestCzechia:
    CSV = (
        "ICO,OKRESLAU,DDATVZN,DDATZAN,ZPZAN,DDATPAKT,FORMA,ROSFORMA,KATPO,NACE,NACE2025,ICZUJ,"
        "FIRMA,CISS2010,KODADM,TEXTADR,PSC,OBEC_TEXT,COBCE_TEXT,ULICE_TEXT,TYPCDOM,CDOM,COR,"
        "DATPLAT,PRIZNAK\n"
        "12345678,CZ0100,2021-04-01,,,2026-01-15,112,112,110,56101,56101,500054,"
        '"Kavárna Slunce s.r.o.",11002,1,"",11000,Praha,Nové Město,Václavské náměstí,1,816,49,'
        "2026-11-15,\n"
        "00000175,CZ0100,1972-01-01,,,2026-01-15,332,332,320,6820,68200,500054,"
        '"Diplomatický servis",13110,1,"",11000,Praha,Nové Město,Václavské náměstí,1,816,49,'
        "2026-11-15,\n"
        "87654321,CZ0100,2019-01-01,2024-05-01,,2026-01-15,112,112,110,47,47,500054,"
        '"Zrušená s.r.o.",11002,1,"",11000,Praha,Nové Město,,1,1,,2026-11-15,\n'
    )

    def test_only_active_business_forms(self, tmp_path: Path) -> None:
        folder = tmp_path / "registries" / "res"
        folder.mkdir(parents=True)
        (folder / "res_data.csv").write_text(self.CSV, encoding="utf-8")
        records = list(ResSource(tmp_path).fetch(["CZ"], SINCE))
        assert [r.name for r in records] == ["Kavárna Slunce s.r.o."]
        record = records[0]
        assert record.created_date == date(2021, 4, 1)
        assert record.address == "Václavské náměstí 816/49"
        assert (record.city, record.postcode, record.category) == ("Praha", "11000", "56101")


NY_CSV = (
    "DOS ID,Current Entity Name,Initial DOS Filing Date,County,Jurisdiction,Entity Type,"
    "DOS Process Name,DOS Process Address 1,DOS Process Address 2,DOS Process City,"
    "DOS Process State,DOS Process Zip,CEO Name,CEO Address 1,CEO Address 2,CEO City,CEO State,"
    "CEO Zip,Registered Agent Name,Registered Agent Address 1,Registered Agent Address 2,"
    "Registered Agent City,Registered Agent State,Registered Agent Zip,Location Name,"
    "Location Address 1,Location Address 2,Location City,Location State,Location Zip\n"
    '4424185,"BUTCHY\'S WINE & SPIRITS, INC.",06/27/2013,Westchester,New York,'
    'DOMESTIC BUSINESS CORPORATION,"BUTCHY\'S",23 BUSHNEIL PLACE,,MOUNT VERNON,NY,10550,'
    ",,,,,,,,,,,,,,,,,\n"
    "8132387,BROOKLYN BAGELS LLC,02/03/2024,Kings,New York,DOMESTIC LIMITED LIABILITY COMPANY,"
    ",1 LAW ST,,ALBANY,NY,12207,,,,,,,,,,,,,,9 MAIN ST,,BROOKLYN,NY,11238\n"
    "8132388,KIND HEARTS INC.,02/03/2024,Kings,New York,DOMESTIC NOT-FOR-PROFIT CORPORATION,"
    ",1 LAW ST,,ALBANY,NY,12207,,,,,,,,,,,,,,,,,,\n"
)

CO_HEADER = (
    "entityid,entityname,principaladdress1,principaladdress2,principalcity,principalstate,"
    "principalzipcode,principalcountry,mailingaddress1,mailingaddress2,mailingcity,"
    "mailingstate,mailingzipcode,mailingcountry,entitystatus,jurisdictonofformation,"
    "entitytype,agentfirstname,agentmiddlename,agentlastname,agentsuffix,"
    "agentorganizationname,agentprincipaladdress1,agentprincipaladdress2,agentprincipalcity,"
    "agentprincipalstate,agentprincipalzipcode,agentprincipalcountry,agentmailingaddress1,"
    "agentmailingaddress2,agentmailingcity,agentmailingstate,agentmailingzipcode,"
    "agentmailingcountry,entityformdate\n"
)
CO_TAIL = ",,,,,,,,,,,,,,,,,"


def co_row(number: str, name: str, status: str, kind: str, formed: str) -> str:
    return (
        f'{number},"{name}",1500 N GRANT ST,STE N,DENVER,CO,80203,US,,,,,,,{status},CO,{kind}'
        f"{CO_TAIL},{formed}\n"
    )


class TestUsStates:
    def test_new_york_dates_are_month_first_and_location_wins(self, tmp_path: Path) -> None:
        folder = tmp_path / "registries" / "us_ny"
        folder.mkdir(parents=True)
        (folder / "entities.csv").write_text(NY_CSV, encoding="utf-8")
        records = {r.name: r for r in NewYorkSource(tmp_path).fetch(["US"], SINCE)}
        assert set(records) == {"BUTCHY'S WINE & SPIRITS, INC.", "BROOKLYN BAGELS LLC"}
        assert records["BUTCHY'S WINE & SPIRITS, INC."].created_date == date(2013, 6, 27)
        bagels = records["BROOKLYN BAGELS LLC"]
        assert bagels.created_date == date(2024, 2, 3)
        assert (bagels.address, bagels.city, bagels.postcode) == (
            "9 MAIN ST", "Brooklyn", "11238",
        )  # fmt: skip
        assert bagels.shared_ids == ["us_ny:8132387"]

    def test_colorado_keeps_active_businesses_only(self, tmp_path: Path) -> None:
        folder = tmp_path / "registries" / "us_co"
        folder.mkdir(parents=True)
        csv = CO_HEADER + "".join(
            [
                co_row("1", "Mile High Tacos LLC", "Good Standing", "DLLC", "01/02/2024"),
                co_row("2", "Old Mine Co", "Voluntarily Dissolved", "DPC", "11/13/1869"),
                co_row("3", "Kind Hearts", "Good Standing", "DNC", "01/02/2024"),
            ]
        )
        (folder / "entities.csv").write_text(csv, encoding="utf-8")
        records = list(ColoradoSource(tmp_path).fetch(["US"], SINCE))
        assert [r.name for r in records] == ["Mile High Tacos LLC"]
        record = records[0]
        assert record.created_date == date(2024, 1, 2)
        assert (record.address, record.city, record.region) == (
            "1500 N GRANT ST STE N", "Denver", "CO",
        )  # fmt: skip
        assert list(ColoradoSource(tmp_path).fetch(["CA"], SINCE)) == []


def test_every_registry_is_wired_in() -> None:
    for name in REGISTRY_SOURCES:
        assert name in SOURCE_FACTORIES, name
    assert {"prh", "res"} <= NACE_SOURCES
