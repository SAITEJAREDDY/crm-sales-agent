"""
run_pipeline_batch.py
─────────────────────
Fetches every deal from Twenty CRM and runs the full 6-agent analysis
pipeline on each one via the running Flask server.

Each pipeline run writes real metrics (latency, tokens, agent timings)
to metrics.db automatically through the existing instrumentation in server.py.

Usage:
    # 1. Start the server first:   python server.py
    # 2. In a second terminal:     python run_pipeline_batch.py

Options (edit at top of file):
    SERVER_URL  — Flask server base URL
    MAX_DEALS   — cap on number of deals to process (None = all)
    DELAY_SEC   — seconds to wait between runs (avoid hammering Ollama)
"""

import requests
import json
import time
import sys

SERVER_URL = "http://localhost:8080"
MAX_DEALS  = 10     # Run 10 deals; set to None to process all
DELAY_SEC  = 2      # Wait between runs so Ollama isn't overloaded

# ── 1. Fetch all deals from the CRM via the Flask API ────────────────────
print("Fetching deals from CRM...")
try:
    resp = requests.get(
        f"{SERVER_URL}/api/deals",
        params={"stage": "NEW,SCREENING,MEETING,PROPOSAL,CUSTOMER,WON,LOST"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
except Exception as e:
    print(f"ERROR: Could not reach server at {SERVER_URL} — is it running?\n{e}")
    sys.exit(1)

deals = data.get("deals", [])
if not deals:
    print("No deals returned from CRM. Check your CRM connection.")
    sys.exit(1)

if MAX_DEALS:
    deals = deals[:MAX_DEALS]

total = len(deals)
print(f"Found {total} deals. Starting pipeline runs...\n")
print("=" * 60)

# ── 2. Run the analysis pipeline on each deal ────────────────────────────
passed = 0
failed = 0

for idx, deal in enumerate(deals, 1):
    deal_id   = deal.get("id", "")
    deal_name = deal.get("name", "Unknown")
    deal_amt  = deal.get("amount", 0)
    stage     = deal.get("stage", "")

    print(f"[{idx:3}/{total}] {deal_name[:45]:<45} ${deal_amt:>10,.0f}  {stage}")

    if not deal_id:
        print("         ↳ SKIP — no deal ID")
        failed += 1
        continue

    try:
        run_start = time.time()
        # Stream the SSE endpoint — blocks until 'complete' or 'error' event
        with requests.get(
            f"{SERVER_URL}/api/analyze/{deal_id}",
            stream=True,
            timeout=300,   # 5-min timeout per deal
        ) as r:
            r.raise_for_status()
            outcome = "unknown"
            for line in r.iter_lines():
                if not line:
                    continue
                raw = line.decode("utf-8") if isinstance(line, bytes) else line
                if raw.startswith("event: complete"):
                    outcome = "ok"
                elif raw.startswith("event: error"):
                    outcome = "error"
                elif raw.startswith("data:") and outcome in ("ok", "error"):
                    break   # got the data payload, done

        elapsed = time.time() - run_start
        if outcome == "ok":
            print(f"         ↳ ✓  {elapsed:.1f}s")
            passed += 1
        else:
            print(f"         ↳ ✗  {elapsed:.1f}s  (pipeline error)")
            failed += 1

    except requests.exceptions.Timeout:
        print("         ↳ ✗  TIMEOUT (>300s)")
        failed += 1
    except Exception as e:
        print(f"         ↳ ✗  {e}")
        failed += 1

    if idx < total:
        time.sleep(DELAY_SEC)

# ── 3. Summary ───────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"Done.  {passed} succeeded  |  {failed} failed  |  {total} total")
print(f"Open http://localhost:8080 → 📊 Metrics to see results.")
