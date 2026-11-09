# Operator-supplied runtime inputs

No input files or sample records are supplied. This table describes the contracts and locations expected by the current source. Provide only the inputs needed for the workflows you enable.

| Input | Expected location or integration | Definition |
|---|---|---|
| Live opportunities and contacts | Twenty CRM GraphQL at `http://localhost:3000/graphql` | Map the custom fields queried in the scripts to the operator's CRM. |
| Qualification pipeline | `knowledge_base/meddpicc_pipeline.json` | Collection of `MEDDPICCRecord` objects. |
| Canonical commercial inputs | `knowledge_base/commercial_strategy/canonical_deal_inputs.json` | Collection of `DealInput` objects. |
| Historical comparable deals | `knowledge_base/commercial_strategy/historical_comparable_deals.json` | Operator-owned precedent inputs interpreted by `tools/comparable_deal_intelligence.py`; no historical deal rows are included. |
| Enablement inputs | `knowledge_base/enablement_data.json` | `EnablementRecords` collection. |
| Customer health | `knowledge_base/customer_health_data.json` | `CustomerHealthCollection`. |
| GTM observations and interventions | `knowledge_base/gtm_feedback_history.json` | `GTMFeedbackHistory`. |
| Workload requirements | `knowledge_base/commercial_strategy/consumption_profiles.json` | Collection of `ConsumptionProfile` objects. |
| Workload overrides | `knowledge_base/commercial_strategy/consumption_profile_overrides.json` | Optional deal-ID keyed operator overrides. |
| Product mapping and knowledge | `knowledge_base/kb/` | Operator-owned product mapping, capability, cost-sheet, ICP, and battlecard inputs. |
| Sales notes and profiles | `knowledge_base/notes/`, `knowledge_base/profiles/` | Operator-owned context for qualification and retrieval. |
| Representative assignments | `knowledge_base/personas/` | Operator-supplied representative definitions and deal assignments; the embedded-persona generator is excluded. |
| Pricing snapshot | `knowledge_base/commercial_strategy/azure_eastus_retail_price_snapshot.json` | Retrieved from the Azure Retail Prices API by the refresh script; not bundled. |
| Supported architecture catalog | `knowledge_base/commercial_strategy/azure_enterprise_service_catalog.json` | Operator-reviewed catalog required for catalog-constrained planning and pricing. |
| Service catalog | `knowledge_base/commercial_strategy/azure_service_catalog.json` | Runtime-normalized service catalog; not bundled. |
| Business-case assumptions | `knowledge_base/commercial_strategy/business_case_assumptions.json` | Optional deal-ID keyed costs and benefits supplied by the operator; no fixed deal assumptions are included. |
| Vector index | `knowledge_base/chromadb/` | Build locally from the operator's own notes and knowledge. |

Use the [schema index](SCHEMAS.md) and [field dictionary](DATA_DICTIONARY.md) to construct the documented inputs. `field_mapping.json` describes field provenance and canonical destinations; it contains mapping definitions, not records.

```bash
python scripts/check_runtime_inputs.py --list
python scripts/check_runtime_inputs.py
```

The first command prints requirements only. The second checks presence and validates the documented JSON contracts when input files exist. A missing-input result is expected in a clean publication checkout. Neither command creates demo data or calls CRM or an inference API.

The supplied derivation scripts can produce qualification, enablement, health, history, notes, and resource-estimate outputs from compatible operator inputs. Review the scripts and run them only in the operator's data environment. The canonical-commercial demo seeder and populated fixtures are intentionally excluded; supply canonical commercial records through the operator's own mapping process.
