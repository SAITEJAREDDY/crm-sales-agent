# CRM Deal Desk Agent

Application source, data contracts, field definitions, and reusable rule logic for an AI-assisted CRM deal desk.

This publication contains **no datasets or populated records**, including synthetic examples. Account and deal records, personas, learner rosters, knowledge-base content, pricing snapshots, generated reports, logs, and databases must be supplied or created in the operator's own environment.

## Workflows

| Workflow | Purpose | Main entry points |
|---|---|---|
| Deal Desk | Retrieve CRM context, assess qualification and risk, compare perspectives, and draft a deal memo | `deal_desk.py`, `server.py`, `index.html` |
| GTM Intelligence | Interpret account adoption, enablement, consumption, and intervention history | `server.py`, `gtm_intelligence.html` |
| Commercial Analysis | Calculate scenarios, evaluate configured policies, compare precedents, and validate negotiation proposals | `tools/commercial_*.py`, `commercial_analysis.html` |
| Consumption Analysis | Turn workload requirements into architecture proposals, resolve pricing, forecast consumption, and evaluate commitment economics | `tools/consumption_*.py`, `consumption_analysis.html` |
| Pipeline and Enablement | Summarize pipeline health and derive enablement recommendations from operator-supplied inputs | `tools/analyze_pipeline.py`, `enablement_agent.py`, `dashboard.html` |

The text-generation workflows share `TOGETHER_MODEL`, configured as `Qwen/Qwen3.8-Flash` in `.env.example`. The shared client supports Qwen's streaming endpoint and buffers responses for JSON consumers. Financial calculations and policy validation remain deterministic. Semantic embeddings and image generation use their own specialized integrations.

## Read the design

- [Scope and limitations](docs/SCOPE.md)
- [Architecture and data flow](docs/ARCHITECTURE.md)
- [Schema index](docs/SCHEMAS.md)
- [Complete field dictionary](docs/DATA_DICTIONARY.md)
- [Business rules and calculation definitions](docs/RULES.md)
- [Required runtime inputs](docs/RUNTIME_INPUTS.md)
- [Installation and configuration](docs/SETUP.md)
- [Publication source adjustments](docs/PUBLICATION_NOTES.md)

## Run with your own inputs

Use Python 3.12 or later. This export is checked with Python 3.13.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
```

Fill in your own Together API key and Twenty CRM token. Supply the compatible inputs described in [RUNTIME_INPUTS.md](docs/RUNTIME_INPUTS.md), then run:

```bash
python scripts/check_runtime_inputs.py
python server.py
```

The interface is served at `http://localhost:5001`. The source currently connects to a separately operated Twenty CRM GraphQL service at `http://localhost:3000/graphql`. A generic CRM instance requires mapping the custom fields used by these scripts.

Connecting CRM alone does not populate the local Commercial, GTM, and Consumption inputs. Without the relevant inputs and catalogs, those workflows are not ready for analysis. This repository is a source-and-contract release, not a pre-populated demo.

## Verify publication contents

```bash
pip install -r requirements-dev.txt
python scripts/verify_publication.py
python scripts/check_runtime_inputs.py --list
```

The verifier checks file hashes, schema validity, and the publication allowlist without calling CRM or an inference provider. The input-list command shows file requirements without creating records. The original application's dataset-dependent tests and fixtures are intentionally excluded.

`PUBLICATION_MANIFEST.json` identifies every reviewed file. `.gitignore` denies files outside that allowlist, including `.env`, datasets, databases, caches, and generated outputs. Change the manifest and review new files explicitly before adding them to a publication.
