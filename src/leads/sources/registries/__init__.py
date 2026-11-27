"""Official company registries. Registry incorporation dates are the strongest age evidence.

Each registry module exposes a Source class whose ``fetch`` yields RawRecords with
``created_date`` = incorporation / registration date and ``shared_ids`` =
["<registry>:<company number>"]. Registries rarely include phone/website, so these
records mostly enrich age and legal name via matching (name + postcode).
"""

REGISTRY_SOURCES = frozenset(
    {"companies_house", "sirene", "brreg", "abn", "canada", "prh", "rik", "res", "us_ny", "us_co"}
)
