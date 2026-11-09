# Schema index

These descriptive contracts define field names, types, units, relationships, and required fields. They contain no populated records. Runtime validators also enforce domain rules that JSON Schema alone does not express.

- [DealInput](../knowledge_base/commercial_strategy/deal_input_schema.json): Canonical account, opportunity, and commercial inputs.
- [CommercialFinancialAnalysis](../knowledge_base/commercial_strategy/commercial_financial_analysis_schema.json): Deterministic commercial scenario evaluation.
- [PolicyEvaluation](../knowledge_base/commercial_strategy/policy_evaluation_schema.json): Deterministic policy and approval evaluation.
- [ValidatedCommercialStrategy](../knowledge_base/commercial_strategy/commercial_strategy_output_schema.json): LLM proposal reconciled with deterministic engines.
- [EnablementRecords](../schemas/enablement.schema.json): Enablement input collection supplied by the operator.
- [ConsumptionProfile](../schemas/consumption_profile.schema.json): Workload requirements used for architecture planning.
- [ConsumptionArchitecture](../schemas/consumption_architecture.schema.json): Architecture proposal without resolved prices.
- [MEDDPICCRecord](../schemas/meddpicc_record.schema.json): Per-opportunity sales qualification assessment.
- [GTMFeedbackHistory](../schemas/gtm_feedback_history.schema.json): Account observation and intervention history supplied by the operator.
- [CustomerHealthCollection](../schemas/customer_health.schema.json): Account-level rollups supplied by the operator.
- [CommercialPolicyRules](../schemas/commercial_policy_rules.schema.json): Reusable rule configuration; this is not a collection of deal records.

See [DATA_DICTIONARY.md](DATA_DICTIONARY.md) for the complete field reference and [RUNTIME_INPUTS.md](RUNTIME_INPUTS.md) for the input locations.
