# Rule and calculation definitions

These are reusable implementation rules and illustrative configuration. They contain no account or deal observations. Commercial thresholds and approval assignments are demonstration policy, not Microsoft internal policy or a substitute for an operator's own policy.

## Commercial calculations

- Discounts are percentages on list value; a percentage must be converted to a fraction before multiplication.
- Contract duration is in months. Annual recurring value, annual contract value, and total contract value must be reconciled for the same product scope and duration.
- Commitment coverage is projected eligible consumption divided by the committed amount over a consistent period. Zero or missing commitment needs an explicit applicability decision rather than division by zero.
- Net present value discounts modeled cash flows to a common date. Return and payback depend on the supplied benefit, cost, timing, and discount-rate assumptions.
- The strategy agent may change only discount, term, commitment, billing frequency, and payment terms in its proposed commercial structure. The financial and policy engines re-evaluate those changes.
- Gross margin is not inferred when the required cost inputs are unavailable. Missing inputs and model limitations must remain visible in flags and traces.

Implementation: `tools/commercial_financial_analysis.py`, `tools/commercial_validation.py`, `tools/commercial_economics.py`, and `tools/financial_model.py`.

## Policy and approvals

`commercial_policy_rules.json` defines identifiers, categories, input fields, comparison operators, thresholds, units, severity, and outcome messages. The policy engine evaluates configured rules against deterministic inputs.

Individual policy statuses include `PASS`, `REVIEW`, `EXCEPTION`, `BLOCK`, `NOT_APPLICABLE`, and `NOT_MODELED`. Overall dispositions include `WITHIN_POLICY`, `REQUIRES_REVIEW`, `REQUIRES_APPROVAL`, and `BLOCKED`. An approval requirement is distinct from an approval that has actually been recorded.

`commercial_approval_hierarchy.json` orders approval functions. `synthetic_approval_matrix.json` maps issue categories to review functions. `synthetic_commercial_guardrails.json` describes standard positions, exception conditions, escalation conditions, and mitigations.

## Enablement triggers

| Trigger | Definition |
|---|---|
| `LOW_TRAINED_COVERAGE` | Trained users / licensed users is below 0.25. |
| `STALLED_ADOPTION` | Attendance is below 50 percent and inactivity exceeds 30 days. |
| `RENEWAL_RISK` | Renewal is fewer than 90 days away and coverage is below 40 percent. |
| `HIGH_CONS_LOW_TRAIN` | Annual consumption exceeds 50,000 currency units and the skills-gap risk score exceeds 70. |

Coverage is trained users divided by licensed users, multiplied by 100. The implementation handles a zero licensed-user denominator. Enablement normalization applies tier-based defaults when values are missing and emits warnings; a default is an assumption, not an observed account measurement. See `knowledge_base/schemas/enablement_schema.py` and `tools/enablement_rules.py`.

## Consumption and pricing

- Architecture proposals describe service choice, region, environment, quantity, and usage. They do not define retail prices, ROI, or financial approval results.
- Supported catalog identifiers are validated locally. Unresolved services and missing prices remain explicit rather than being fabricated by an LLM.
- Service quantity and usage must use the same meter unit and billing period as the selected price.
- Forecast growth-rate fractions are distinct from percentage-valued UI fields; annual and monthly values must be converted explicitly.
- The configured secondary-region price multiplier is a synthetic modeling assumption. It must not be represented as observed Azure retail pricing.
- Downloaded Azure prices and operator catalogs are runtime inputs, not included publication data.

Implementation: `tools/consumption_architecture_planner.py`, `tools/azure_pricing_config.py`, `tools/azure_retail_pricing.py`, `tools/consumption_forecast.py`, and `tools/consumption_commercial_intelligence.py`.

## Comparables and GTM interpretation

Comparable selection uses configurable similarity weights, compatibility filters, recency, minimum sample sizes, and confidence thresholds. Missing precedent records cannot establish a market benchmark. See `comparable_deal_config.json` and `tools/comparable_deal_intelligence.py`.

GTM analysis interprets supplied observations and recorded interventions. It must distinguish observation from inference and avoid claiming that a recommendation caused later account changes. No account history or intervention records are included.
