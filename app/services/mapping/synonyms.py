"""The code-shipped synonym dictionary (decision 3: shared across clients,
maintained from evals, never learned from client data).

Keys are the parts of a sheet the rules layer looks for; values are header
wordings, normalised the way ``match.normalise`` does, that mean that part.
A header matches a part when its normalised text equals one of the wordings
or, for the longer ones, contains it as whole words.
"""
from __future__ import annotations

from app.services.mapping.match import normalise

SYNONYMS: dict[str, list[str]] = {
    "activity_value": [
        "qty", "quantity", "consumption", "consumed", "units consumed", "usage", "volume", "amount consumed",
        "activity value", "activity data", "value", "total consumption", "monthly consumption", "qty consumed",
        "reading", "kwh", "mwh", "litres", "liters", "ltr", "kg", "tonnes", "km", "distance", "annual quantity",
        "quantity consumed", "fuel consumed", "net consumption",
    ],
    "activity_unit": ["uom", "unit", "units", "unit of measure", "unit of measurement", "measure", "activity data unit", "activity unit"],
    "period": [
        "month", "date", "period", "reporting month", "reporting period", "bill date", "billing month", "billing period",
        "invoice date", "date of reporting", "month year", "mmm yy", "mon", "year month", "reporting date", "transaction date",
        "entry date", "fy month", "for the month of",
    ],
    "activity_name": [
        "activity", "activity name", "fuel", "fuel type", "item", "source", "energy source", "description", "particulars",
        "material", "emission source", "activity type", "type of fuel", "fuel name", "resource", "commodity", "emission category",
        "sub category", "subcategory", "meter", "mode", "vehicle type", "refrigerant", "gas", "waste type", "product",
    ],
    "site": ["site", "plant", "location", "facility", "factory", "works", "branch", "unit name", "site name", "plant name", "office", "premises", "depot"],
    "category": ["category", "scope category", "ghg category", "emission type", "activity category", "source category", "scope"],
}

# Units a category's form may name under other keys: a header "Distance (km)"
# is the value column of a distance form.
FIELD_LABEL_SYNONYMS: dict[str, list[str]] = {
    "distance": ["distance", "km", "kms", "kilometres", "kilometers", "miles", "distance travelled", "distance km"],
    "amount": ["amount", "spend", "cost", "inr", "usd", "value inr", "amount inr", "bill amount"],
    "passengers": ["passengers", "pax", "no of passengers", "travellers", "headcount"],
    "vehicle": ["vehicle", "vehicle no", "vehicle number", "registration", "reg no"],
    "supplier": ["supplier", "vendor", "provider", "utility"],
    "weight": ["weight", "mass", "tonnage", "net weight"],
}

_NORMALISED = {part: [normalise(w) for w in words] for part, words in SYNONYMS.items()}


def part_of_header(header: str) -> list[str]:
    """The parts a header wording may mean, best first (a header can suggest several)."""
    h = normalise(header)
    if not h:
        return []
    exact = [part for part, words in _NORMALISED.items() if h in words]
    if exact:
        return exact
    padded = f" {h} "
    found = [part for part, words in _NORMALISED.items() if any(len(w) >= 3 and f" {w} " in padded for w in words)]
    return found
