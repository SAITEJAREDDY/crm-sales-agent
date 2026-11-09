# Setup

## Dependencies

Use Python 3.12 or later. Install `requirements.txt` in an isolated virtual environment. Optional semantic retrieval and presentation features need `requirements-optional.txt`; publication/schema checks use `requirements-dev.txt`.

## Environment

Copy `.env.example` to `.env` and supply your own credentials. The publication template contains no credential values.

| Variable | Purpose |
|---|---|
| `TWENTY_API_TOKEN` | Authenticate the operator's CRM requests. |
| `TOGETHER_API_KEY` | Authenticate text-generation requests to Together. |
| `TOGETHER_MODEL` | Shared text model; the template selects `Qwen/Qwen3.8-Flash`. |
| `TOGETHER_REASONING_EFFORT` | Shared reasoning effort; the template selects `low`. |
| `LANGCHAIN_TRACING_V2` | Optional tracing switch, disabled in the template. |
| `LANGCHAIN_API_KEY` | Optional tracing credential. |
| `LANGCHAIN_PROJECT` | Optional tracing project name. |
| `LANGCHAIN_ENDPOINT` | Optional tracing service endpoint. |
| `GMAIL_SENDER` | Optional email sender configured by the operator. |
| `GMAIL_APP_PASSWORD` | Optional credential used by the email-delivery action. |
| `COMMERCIAL_PROVIDER_NAME` | Operator-supplied provider name for generated agreement drafts. |

Exported environment variables take precedence over `.env`. Restart the server after changing its model or credentials.

## Integration and inputs

Operate Twenty CRM separately and map the custom fields queried by the source. The current source uses `http://localhost:3000/graphql`; adjust the source connection configuration if the operator uses a different endpoint.

Populate the selected workflow inputs described in [RUNTIME_INPUTS.md](RUNTIME_INPUTS.md). This publication does not initialize account or deal data. Catalog downloads and local derivation are separate operator actions.

Agreement-package generation uses the operator-supplied provider identity. Its optional template reference points to `templates/cloud_service_agreement.docx`; no template document is bundled.

## Start

```bash
python scripts/check_runtime_inputs.py
python server.py
```

The server serves the application at `http://localhost:5001`. Missing local inputs can leave specific endpoints unavailable even when the interfaces load. The publication copy has no automatic approval or metrics demo seeding.

## Verification

```bash
python scripts/verify_publication.py
```

This checks the reviewed file allowlist, file hashes, schema syntax, and absence of obvious credentials and prohibited record files. It performs no live analysis and creates no sample records. Original dataset-dependent tests are excluded from this publication; add operator-owned tests separately.
