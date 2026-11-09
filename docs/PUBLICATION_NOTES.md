# Publication source adjustments

The local application is unchanged. The publication copies have the following adjustments:

- Removed automatic metrics and approval demo seeding and the demo-reset endpoint.
- Removed fixed representative names and compensation tables from the server and dashboard.
- Removed a fixed deal's business-case assumptions and mandatory demo-profile check.
- Removed seeded-deal selection defaults, per-account command-line self-tests, and named filter examples.
- Replaced the commercial proposal prompt's populated illustration with field definitions.
- Expanded the enablement contract with field definitions.
- Suppressed the optional dashboard compensation calculation until an operator supplies aggregate sales-cost inputs.

The account-specific runners, seed datasets, embedded-persona generator, comparison fixtures, original tests, and presentation page are intentionally absent. They contain or depend on concrete sample records.

Changed copies:

- `.env.example`
- `commercial_analysis.html`
- `dashboard.html`
- `knowledge_base/knowledge_lookup.py`
- `knowledge_base/schemas/enablement_schema.py`
- `phase2_kb_builder.py`
- `server.py`
- `tools/analyze_pipeline.py`
- `tools/commercial_data.py`
- `tools/commercial_financial_analysis.py`
- `tools/commercial_policy_engine.py`
- `tools/commercial_strategy_agent.py`
- `tools/comparable_deal_intelligence.py`
- `tools/consumption_commercial_intelligence.py`
- `tools/consumption_profiles.py`
- `tools/deal_package_builder.py`
