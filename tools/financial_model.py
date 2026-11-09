"""
financial_model.py
Pure Python computation of all financial metrics.
All computed values are returned as a flat dict for use by excel_builder.py.
No LLM calls. No Excel formulas — those live in excel_builder.py.
"""

from __future__ import annotations

from typing import Any


# ── EDP tier table ────────────────────────────────────────────────────────
EDP_TIERS = [
    (0,         100_000,   0.00),
    (100_000,   250_000,   0.05),
    (250_000,   500_000,   0.08),
    (500_000,   1_000_000, 0.12),
    (1_000_000, 2_000_000, 0.15),
    (2_000_000, float("inf"), 0.18),
]


def _edp_discount(amount: float) -> float:
    for low, high, rate in EDP_TIERS:
        if low <= amount < high:
            return rate
    return 0.18


def _next_edp_tier(amount: float) -> tuple[float, float]:
    """Return (next_tier_threshold, next_tier_discount_rate)."""
    for i, (low, high, rate) in enumerate(EDP_TIERS):
        if low <= amount < high:
            if i + 1 < len(EDP_TIERS):
                next_low, _, next_rate = EDP_TIERS[i + 1]
                return next_low, next_rate
    return amount, _edp_discount(amount)


def _npv(rate: float, cash_flows: list[float]) -> float:
    """NPV at given annual discount rate for annual cash flows (period 1, 2, 3...)."""
    return sum(cf / (1 + rate) ** (i + 1) for i, cf in enumerate(cash_flows))


def _confidence_bands(confidence_level: str) -> dict[str, float]:
    bands = {
        "HIGH":   {"conservative": 0.70, "base": 1.0, "optimistic": 1.20},
        "MEDIUM": {"conservative": 0.50, "base": 1.0, "optimistic": 1.30},
        "LOW":    {"conservative": 0.30, "base": 1.0, "optimistic": 1.50},
    }
    return bands.get(confidence_level, bands["MEDIUM"])


def compute(gathered: dict) -> dict:
    """
    Compute all financial metrics from the gathered inputs dict.

    Returns a flat dict of all computed values.  The keys are stable
    and consumed directly by excel_builder.compute_model_values().
    """
    inp     = gathered["inputs"]
    rates   = gathered["rates"]
    res     = gathered["resources"]
    amount  = gathered["deal_amount"]
    conf    = gathered["confidence_level"]
    product = gathered["product"]

    def v(key: str) -> float:
        """Safe float getter from inputs — returns 0 if None."""
        val = inp.get(key, {}).get("value")
        return float(val) if val is not None else 0.0

    # ── Input values ──────────────────────────────────────────────────────
    pain_annual       = v("pain_usd_annual")
    hours_per_week    = v("hours_lost_per_week")
    headcount         = max(1.0, v("headcount_affected"))
    error_rate        = v("error_rate_pct")          # 0–1 fraction
    txn_per_month     = v("transactions_per_month")
    vendor_cost       = v("current_vendor_cost_usd")
    growth_rate       = v("growth_rate_pct")         # 0–1 fraction
    loaded_hourly     = rates.get("loaded_hourly", 85.0)
    sa_day_rate       = float(rates.get("sa_day_rate",      2_500))
    proserv_day_rate  = float(rates.get("proserv_day_rate", 3_000))
    partner_day_rate  = float(rates.get("partner_day_rate", 1_800))
    sa_days           = float(res.get("sa_days",       15))
    proserv_days      = float(res.get("proserv_days",  30))
    partner_days      = float(res.get("partner_days",  15))
    timeline_weeks    = float(res.get("timeline_weeks", 12))

    # ── Year 1 Costs ──────────────────────────────────────────────────────
    sa_cost           = sa_days        * sa_day_rate
    proserv_cost      = proserv_days   * proserv_day_rate
    partner_cost      = partner_days   * partner_day_rate
    internal_cost     = timeline_weeks * 2 * loaded_hourly * 40  # 2 FTE × 40h × rate
    migration_cost    = 15_000.0   # default; overridden if legacy signals found
    training_cost     = headcount  * 500.0
    total_impl        = sa_cost + proserv_cost + partner_cost + internal_cost + migration_cost + training_cost

    y1_consumption    = amount * 0.60   # 60% of deal value is Y1 consumption
    y1_cost           = total_impl + y1_consumption

    # ── Year 2 / Year 3 Costs ─────────────────────────────────────────────
    y2_consumption    = y1_consumption * (1 + growth_rate)
    y3_consumption    = y2_consumption * (1 + growth_rate)

    # ── Year 1 Savings ────────────────────────────────────────────────────
    pain_reduction_y1 = pain_annual * 0.70   # 70% pain eliminated in Y1
    vendor_replace_y1 = vendor_cost           # full vendor replacement savings
    error_reduction_y1 = (error_rate * 100.0 * txn_per_month * 12 * 0.50)  # $0.50 cost per error
    # When pain_annual is a stated aggregate, time savings is already captured inside it —
    # generating a separate line would double-count. Only add time savings when pain is 0.
    _raw_time_y1      = hours_per_week * 52 * loaded_hourly * headcount
    time_savings_y1   = 0.0 if (pain_annual > 0 and _raw_time_y1 > 0) else _raw_time_y1
    y1_savings        = pain_reduction_y1 + vendor_replace_y1 + error_reduction_y1 + time_savings_y1

    # ── Year 2 / Year 3 Savings ───────────────────────────────────────────
    pain_reduction_y2 = pain_annual * 0.95
    vendor_replace_y2 = vendor_cost
    error_reduction_y2 = error_reduction_y1 * (1 + growth_rate)
    time_savings_y2   = time_savings_y1 * (1 + growth_rate)
    y2_savings        = pain_reduction_y2 + vendor_replace_y2 + error_reduction_y2 + time_savings_y2

    # Y3 pain: compound Y2 captured savings by growth rate — don't exceed 100% of pain
    pain_reduction_y3 = pain_reduction_y2 * (1 + growth_rate)
    vendor_replace_y3 = vendor_cost
    error_reduction_y3 = error_reduction_y2 * (1 + growth_rate)
    time_savings_y3   = time_savings_y2 * (1 + growth_rate)
    y3_savings        = pain_reduction_y3 + vendor_replace_y3 + error_reduction_y3 + time_savings_y3

    # ── Net positions ─────────────────────────────────────────────────────
    net_y1            = y1_savings  - y1_cost
    net_y2            = y2_savings  - y2_consumption      # impl is one-time, no repeat in Y2
    net_y3            = y3_savings  - y3_consumption
    cumulative_3yr    = net_y1 + net_y2 + net_y3

    # ── Key metrics ───────────────────────────────────────────────────────
    monthly_net_after_aws = (y1_savings - y1_consumption) / 12
    payback_months = (y1_cost / monthly_net_after_aws) if monthly_net_after_aws > 0 else 999
    payback_months = min(payback_months, 120)  # cap at 10 years

    # ROI denominator = total 3-year cost (implementation + all consumption years)
    total_3yr_cost = y1_cost + y2_consumption + y3_consumption
    roi_3yr = (cumulative_3yr / total_3yr_cost * 100) if total_3yr_cost > 0 else 0
    npv_10pct = _npv(0.10, [net_y1, net_y2, net_y3])

    # ── Confidence bands ──────────────────────────────────────────────────
    bands = _confidence_bands(conf)

    def apply_band(savings: float, costs: float, mult: float) -> float:
        return savings * mult - costs

    scenarios = {
        "year1": {
            "conservative": apply_band(y1_savings, y1_cost,        bands["conservative"]),
            "base":         net_y1,
            "optimistic":   apply_band(y1_savings, y1_cost,        bands["optimistic"]),
        },
        "year2": {
            "conservative": apply_band(y2_savings, y2_consumption, bands["conservative"]),
            "base":         net_y2,
            "optimistic":   apply_band(y2_savings, y2_consumption, bands["optimistic"]),
        },
        "year3": {
            "conservative": apply_band(y3_savings, y3_consumption, bands["conservative"]),
            "base":         net_y3,
            "optimistic":   apply_band(y3_savings, y3_consumption, bands["optimistic"]),
        },
    }

    # ── EDP analysis ──────────────────────────────────────────────────────
    edp_rate     = _edp_discount(amount)
    edp_savings  = amount * edp_rate
    net_price    = amount * (1 - edp_rate)

    next_thresh, next_rate  = _next_edp_tier(amount)
    additional_commitment   = max(0, next_thresh - amount)
    next_tier_savings_total = next_thresh * next_rate
    current_savings_total   = amount * edp_rate
    incremental_benefit     = next_tier_savings_total - current_savings_total
    breakeven_months = (additional_commitment / incremental_benefit * 12) if incremental_benefit > 0 else 0

    # Commitment options (3-year total at list vs 1yr commit vs 3yr commit)
    mtm_3yr_total    = amount * 3
    oneyear_discount = edp_rate
    oneyear_3yr      = amount * (1 - oneyear_discount) * 3
    threeyear_discount = min(edp_rate + 0.03, 0.20)   # slight extra for 3yr commit
    threeyear_3yr    = amount * (1 - threeyear_discount) * 3

    return {
        # Raw inputs for Excel (so excel_builder can write them)
        "pain_annual":        pain_annual,
        "hours_per_week":     hours_per_week,
        "headcount":          headcount,
        "error_rate_pct":     error_rate,
        "txn_per_month":      txn_per_month,
        "vendor_cost":        vendor_cost,
        "growth_rate_pct":    growth_rate,
        "loaded_hourly":      loaded_hourly,
        "sa_day_rate":        sa_day_rate,
        "proserv_day_rate":   proserv_day_rate,
        "partner_day_rate":   partner_day_rate,
        "sa_days":            sa_days,
        "proserv_days":       proserv_days,
        "partner_days":       partner_days,
        "timeline_weeks":     timeline_weeks,
        "migration_cost":     migration_cost,
        "training_cost_per_head": 500.0,
        "y1_consumption":     y1_consumption,
        "y2_growth_rate":     growth_rate,
        "y3_growth_rate":     growth_rate,
        "deal_amount":        amount,
        "edp_rate":           edp_rate,
        "conservative_mult":  bands["conservative"],
        "base_mult":          1.0,
        "optimistic_mult":    bands["optimistic"],
        # Cost breakdown (year 1)
        "sa_cost":            sa_cost,
        "proserv_cost":       proserv_cost,
        "partner_cost":       partner_cost,
        "internal_cost":      internal_cost,
        "migration_cost_total": migration_cost,
        "training_cost_total":  training_cost,
        "total_impl":         total_impl,
        "y2_consumption":     y2_consumption,
        "y3_consumption":     y3_consumption,
        # Savings breakdown
        "pain_reduction_y1":  pain_reduction_y1,
        "pain_reduction_y2":  pain_reduction_y2,
        "pain_reduction_y3":  pain_reduction_y3,
        "vendor_replace_y1":  vendor_replace_y1,
        "vendor_replace_y2":  vendor_replace_y2,
        "vendor_replace_y3":  vendor_replace_y3,
        "error_reduction_y1": error_reduction_y1,
        "error_reduction_y2": error_reduction_y2,
        "error_reduction_y3": error_reduction_y3,
        "time_savings_y1":    time_savings_y1,
        "time_savings_y2":    time_savings_y2,
        "time_savings_y3":    time_savings_y3,
        "y1_savings":         y1_savings,
        "y2_savings":         y2_savings,
        "y3_savings":         y3_savings,
        "y1_cost":            y1_cost,
        "y2_cost":            y2_consumption,
        "y3_cost":            y3_consumption,
        # Net
        "net_y1":             net_y1,
        "net_y2":             net_y2,
        "net_y3":             net_y3,
        "cumulative_3yr":     cumulative_3yr,
        # Key metrics
        "payback_months":     payback_months,
        "roi_3yr_pct":        roi_3yr,
        "npv_10pct":          npv_10pct,
        # Scenarios
        "scenarios":          scenarios,
        "bands":              bands,
        # EDP
        "edp_rate":           edp_rate,
        "edp_savings":        edp_savings,
        "net_price":          net_price,
        "next_edp_threshold": next_thresh,
        "next_edp_rate":      next_rate,
        "additional_commitment": additional_commitment,
        "incremental_benefit":   incremental_benefit,
        "breakeven_months":      breakeven_months,
        # Commitment options
        "mtm_3yr_total":          mtm_3yr_total,
        "oneyear_discount":       oneyear_discount,
        "oneyear_3yr_total":      oneyear_3yr,
        "threeyear_discount":     threeyear_discount,
        "threeyear_3yr_total":    threeyear_3yr,
    }
