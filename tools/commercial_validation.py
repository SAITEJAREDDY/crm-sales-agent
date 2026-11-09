"""Numeric reconciliation gates for canonical commercial DealInput records.

Single source of truth for the cross-field identities every DealInput must
satisfy. Used at build time by generate_commercial_negotiation_data.py (which
refuses to write data that fails a gate) and at runtime by
tools.commercial_data.validate_deal_input_object.

Canonical semantics enforced here:
  - commercial.list_value is the full-term (contract) list value
  - commercial.proposed_net_value == list_value x (1 - proposed_discount_pct)
  - commercial.tcv == proposed_net_value (net contract value over the term)
  - commercial.arr == commercial.acv == tcv / term_years
  - products list_value entries sum to commercial.list_value
  - consumption_commitment year targets sum to commitment_amount and ramp up
  - commitment_amount stays within a sane band of tcv
"""

from __future__ import annotations

RELATIVE_TOLERANCE = 0.02
COMMITMENT_TO_TCV_BAND = (0.5, 1.5)
EXPECTED_COVERAGE_BAND = (0.4, 1.4)


def _rel_diff(actual: float, expected: float) -> float:
    return abs(actual - expected) / max(abs(expected), 1.0)


def _issue(gate: str, field: str, message: str) -> dict:
    return {"gate": gate, "field": field, "message": message}


def numeric_reconciliation_issues(deal_input: dict) -> list[dict]:
    """Return every numeric-identity violation in one DealInput (empty = clean)."""
    issues = []
    commercial = deal_input.get("commercial", {})
    commitment = deal_input.get("consumption_commitment", {})
    products = deal_input.get("products", [])

    list_value = float(commercial.get("list_value") or 0)
    discount = float(commercial.get("proposed_discount_pct") or 0) / 100.0
    net_value = float(commercial.get("proposed_net_value") or 0)
    tcv = float(commercial.get("tcv") or 0)
    arr = float(commercial.get("arr") or 0)
    acv = float(commercial.get("acv") or 0)
    term_months = float(commercial.get("contract_term_months") or 0)

    product_sum = sum(float(p.get("list_value") or 0) for p in products)
    if list_value and _rel_diff(product_sum, list_value) > RELATIVE_TOLERANCE:
        issues.append(_issue(
            "G1_PRODUCT_SUM", "products.list_value",
            f"Product lines sum to ${product_sum:,.0f} but commercial.list_value is ${list_value:,.0f}.",
        ))

    expected_net = list_value * (1 - discount)
    if list_value and _rel_diff(net_value, expected_net) > RELATIVE_TOLERANCE:
        issues.append(_issue(
            "G2_NET_VALUE", "commercial.proposed_net_value",
            f"Net value ${net_value:,.0f} does not equal list ${list_value:,.0f} x (1 - {discount:.0%}) = ${expected_net:,.0f}.",
        ))

    if net_value and _rel_diff(tcv, net_value) > RELATIVE_TOLERANCE:
        issues.append(_issue(
            "G3_TCV", "commercial.tcv",
            f"TCV ${tcv:,.0f} does not equal the net contract value ${net_value:,.0f}.",
        ))

    if term_months > 0 and tcv:
        expected_arr = tcv / (term_months / 12.0)
        if _rel_diff(arr, expected_arr) > RELATIVE_TOLERANCE:
            issues.append(_issue(
                "G4_ARR", "commercial.arr",
                f"ARR ${arr:,.0f} does not equal TCV ${tcv:,.0f} / {term_months / 12.0:.1f} years = ${expected_arr:,.0f}.",
            ))
        if _rel_diff(acv, expected_arr) > RELATIVE_TOLERANCE:
            issues.append(_issue(
                "G4_ACV", "commercial.acv",
                f"ACV ${acv:,.0f} does not equal TCV ${tcv:,.0f} / {term_months / 12.0:.1f} years = ${expected_arr:,.0f}.",
            ))

    total = float(commitment.get("commitment_amount") or 0)
    targets = [float(commitment.get(f"year_{i}_target") or 0) for i in (1, 2, 3)]
    if total:
        if _rel_diff(sum(targets), total) > RELATIVE_TOLERANCE:
            issues.append(_issue(
                "G5_TARGET_SUM", "consumption_commitment",
                f"Year targets sum to ${sum(targets):,.0f} but commitment_amount is ${total:,.0f}.",
            ))
        if targets[1] and targets[0] > targets[1] * (1 + RELATIVE_TOLERANCE):
            issues.append(_issue(
                "G6_RAMP", "consumption_commitment",
                f"Ramp is front-loaded ({targets[0]:,.0f} / {targets[1]:,.0f} / {targets[2]:,.0f}); consumption targets must not fall after year 1.",
            ))
        if tcv:
            ratio = total / tcv
            low, high = COMMITMENT_TO_TCV_BAND
            if not low <= ratio <= high:
                issues.append(_issue(
                    "G7_COMMITMENT_BAND", "consumption_commitment.commitment_amount",
                    f"Commitment ${total:,.0f} is {ratio:.2f}x TCV ${tcv:,.0f}; expected between {low}x and {high}x.",
                ))
        current_expected = float(commitment.get("current_expected_consumption") or 0)
        if current_expected:
            # Approximate the engine's 3-year curve at ~10% growth; catches
            # account-level spend leaking into a deal-level field.
            low, high = EXPECTED_COVERAGE_BAND
            projected = current_expected * (1 + 1.10 + 1.21)
            coverage = projected / total
            if not low <= coverage <= high:
                issues.append(_issue(
                    "G8_EXPECTED_COVERAGE", "consumption_commitment.current_expected_consumption",
                    f"Expected 3-year consumption ~${projected:,.0f} is {coverage:.2f}x the ${total:,.0f} commitment; expected between {low}x and {high}x.",
                ))

    return issues
