"""Checkout pricing. Contains the planted bug `code_null_tax`."""

from dataclasses import dataclass

from .bugs import is_enabled


@dataclass
class TaxRule:
    region: str
    rate: float


TAX_RULES = {
    "KA": TaxRule("KA", 0.18),
    "MH": TaxRule("MH", 0.18),
    "DL": TaxRule("DL", 0.12),
}


def find_tax_rule(region: str) -> TaxRule | None:
    rule = TAX_RULES.get(region)
    if rule is None and not is_enabled("code_null_tax"):
        # Correct behaviour: unknown regions fall back to a default rule.
        return TaxRule(region, 0.18)
    # Planted bug: with `code_null_tax` enabled, unknown regions return None.
    return rule


def calculate_total(subtotal: float, region: str) -> dict:
    tax = find_tax_rule(region)
    tax_amount = round(subtotal * tax.rate, 2)  # AttributeError when tax is None
    return {"subtotal": subtotal, "tax": tax_amount, "total": round(subtotal + tax_amount, 2)}
