# Publication scope

## Included

- Reusable Python application, analysis, validation, generation, and reporting scripts.
- The five web interfaces used by the application.
- Structural schemas, field descriptions, types, units, allowed values, and required-field definitions.
- CRM-to-canonical field mapping definitions.
- Illustrative commercial policies, approval ordering, guardrails, comparable-selection rules, and enablement trigger logic.
- Dependency declarations, an environment template with empty credentials, and offline publication checks.

## Excluded

- All real and synthetic account, opportunity, contact, representative, learner, and historical-deal records.
- CSV exports, populated JSON inputs, golden records, fixture datasets, personas, company profiles, and sales notes.
- Product and service catalog datasets, retail-price snapshots, embeddings, vector stores, and pricing caches.
- Analysis outputs, workbooks, deal packages, memos, presentations, and database contents.
- Credentials, machine configuration, logs, development backups, and account-specific analysis runners.
- Dataset seeding scripts whose source embeds populated account, persona, or deal examples.

Schema enum values, validation bounds, calculation constants, and reusable policy thresholds are definitions and rules. They are included; populated customer records are not. The commercial configuration files explicitly identify their policies as synthetic demonstration logic rather than an organization's internal policy.

## Implementation boundaries

The source implements the workflows, but users must supply compatible inputs and configure the integrations. This publication does not provide a hosted service or a complete ingestion adapter for every CRM.

The schemas describe the main input and output contracts. Open objects deliberately allow extra fields used by tenant mappings and legacy calculations. Runtime validators also check reconciliation, allowed proposal fields, catalog compatibility, pricing resolution, and other cross-field rules.

LLM text is a proposal or interpretation. Deterministic financial and policy engines supply authoritative calculated values and configured approval results. Historical GTM observations and intervention timing do not establish causal impact.

## Next integration work

- Map the operator's CRM fields into the documented contracts.
- Supply the account, product, workload, pricing, and historical inputs needed by selected workflows.
- Calibrate illustrative rule thresholds, cost assumptions, and approval routing for the operator's use case.
- Add operator-owned tests and fixtures in a separate private environment.
- Evaluate recommendation quality against the operator's requirements before using the generated output operationally.
