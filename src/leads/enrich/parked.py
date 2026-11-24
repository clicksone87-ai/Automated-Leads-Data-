"""Parked-domain detection: parking/marketplace hosts, nameservers and page phrases."""

from __future__ import annotations

from collections.abc import Iterable

from leads.pipeline.normalise import registered_domain

# Registered domains a parked site redirects to / is served from.
PARKED_HOSTS = frozenset(
    {
        "sedo.com",
        "sedoparking.com",
        "parkingcrew.net",
        "bodis.com",
        "above.com",
        "parklogic.com",
        "dan.com",
        "afternic.com",
        "hugedomains.com",
        "buydomains.com",
        "undeveloped.com",
        "domainmarket.com",
        "uniregistry.com",
        "sav.com",
        "atom.com",
        "squadhelp.com",
        "domainnameshop.com",
        "brandbucket.com",
        "parkingpage.namecheap.com",
        "efty.com",
        "smartname.com",
        "voodoo.com",
        "domainlore.uk",
    }
)

# Substrings of nameserver hosts used by parking services.
PARKED_NAMESERVER_HINTS: tuple[str, ...] = (
    "sedoparking.com",
    "parkingcrew.net",
    "bodis.com",
    "above.com",
    "parklogic.com",
    "dan.com",
    "afternic.com",
    "hugedomainsdns.com",
    "uniregistrymarket.link",
    "domainparking",
)

# Lower-cased phrases that indicate a parked or for-sale page.
PARKED_PHRASES: tuple[str, ...] = (
    "domain for sale",
    "this domain is for sale",
    "this domain may be for sale",
    "the domain is for sale",
    "buy this domain",
    "is available for purchase",
    "domain is parked",
    "this domain has been registered",
    "this web page is parked",
    "parked free, courtesy of",
    "parked by",
    "domain parking",
    "related searches",
    "make an offer on this domain",
    "inquire about this domain",
    "this domain name is available",
    "domaine à vendre",
    "diese domain steht zum verkauf",
    "dominio en venta",
    "dominio in vendita",
)


def is_parked_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower()
    return host in PARKED_HOSTS or registered_domain(host) in PARKED_HOSTS


def has_parked_nameservers(nameservers: Iterable[str]) -> bool:
    return any(hint in ns.lower() for ns in nameservers for hint in PARKED_NAMESERVER_HINTS)


def has_parked_phrases(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(phrase in lowered for phrase in PARKED_PHRASES)
