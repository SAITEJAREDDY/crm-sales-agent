"""
Deal Desk Agent — Interactive CLI
Run this to browse deals and trigger the agent pipeline
"""

import requests
import json
import subprocess
import sys
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TWENTY_URL   = "http://localhost:3000/graphql"
TWENTY_TOKEN = os.environ.get("TWENTY_API_TOKEN", "")

HEADERS = {
    "Authorization": f"Bearer {TWENTY_TOKEN}",
    "Content-Type": "application/json",
}

STAGE_COLORS = {
    "NEW":         "\033[37m",   # white
    "MEETING":     "\033[36m",   # cyan
    "PROPOSAL":    "\033[33m",   # yellow
    "NEGOTIATION": "\033[35m",   # magenta
    "WON":         "\033[32m",   # green
    "LOST":        "\033[31m",   # red
    "ON_HOLD":     "\033[90m",   # grey
}
RESET = "\033[0m"
BOLD  = "\033[1m"


def fetch_deals(stage_filter=None):
    stage_clause = ""
    if stage_filter:
        stage_clause = f', filter: {{ stage: {{ in: [{stage_filter}] }} }}'

    query = f"""
    query {{
      opportunities(
        first: 50,
        orderBy: {{ amount: DescNullsLast }}
        {stage_clause}
      ) {{
        edges {{
          node {{
            id
            name
            stage
            amount
            closeDate
          }}
        }}
      }}
    }}
    """
    resp = requests.post(TWENTY_URL, headers=HEADERS,
                         json={"query": query}, timeout=10)
    data = resp.json()
    return [e["node"] for e in data["data"]["opportunities"]["edges"]]


def print_banner():
    os.system("clear")
    print(f"{BOLD}{'█'*58}{RESET}")
    print(f"{BOLD}  NEXUS SOLUTIONS — DEAL DESK AGENT{RESET}")
    print(f"  AI-powered deal review committee")
    print(f"{BOLD}{'█'*58}{RESET}\n")


def print_deals(deals):
    print(f"  {'#':<4} {'DEAL NAME':<38} {'STAGE':<14} {'AMOUNT':>12}  {'CLOSE'}")
    print(f"  {'─'*90}")
    for i, d in enumerate(deals):
        color = STAGE_COLORS.get(d["stage"], "")
        amount = f"${d.get('amount', 0):>12,.0f}"
        close  = (d.get("closeDate") or "")[:10]
        name   = d["name"][:37]
        stage  = d["stage"]
        print(f"  {i+1:<4} {name:<38} {color}{stage:<14}{RESET} {amount}  {close}")


def main():
    print_banner()

    # Stage filter menu
    print("  Filter by stage:")
    print("  [1] Active deals (PROPOSAL + NEGOTIATION)  ← recommended")
    print("  [2] All stages")
    print("  [3] Negotiation only")
    print("  [4] Proposal only")
    print()

    choice = input("  Select filter [1]: ").strip() or "1"

    stage_map = {
        "1": "PROPOSAL, NEGOTIATION",
        "2": None,
        "3": "NEGOTIATION",
        "4": "PROPOSAL",
    }
    stage_filter = stage_map.get(choice, "PROPOSAL, NEGOTIATION")

    print("\n  Fetching deals from Twenty CRM...\n")
    try:
        deals = fetch_deals(stage_filter)
    except Exception as e:
        print(f"  ✗ Could not connect to Twenty CRM: {e}")
        print("  Make sure Twenty CRM is running on localhost:3000")
        sys.exit(1)

    if not deals:
        print("  No deals found for this filter.")
        sys.exit(0)

    print_banner()
    print_deals(deals)
    print()

    # Deal selection
    while True:
        sel = input(f"  Select deal number to analyze [1-{len(deals)}] or Q to quit: ").strip()
        if sel.upper() == "Q":
            sys.exit(0)
        try:
            idx = int(sel) - 1
            if 0 <= idx < len(deals):
                break
            print(f"  Please enter a number between 1 and {len(deals)}")
        except ValueError:
            print("  Please enter a valid number")

    selected = deals[idx]
    print(f"\n  {BOLD}Selected:{RESET} {selected['name']}")
    print(f"  Stage  : {selected['stage']}")
    print(f"  Amount : ${selected.get('amount', 0):,.0f}")
    print()

    confirm = input("  Run Deal Desk Agent on this deal? [Y/n]: ").strip().lower()
    if confirm == "n":
        sys.exit(0)

    print(f"\n  Launching 6-agent pipeline...\n")

    # Run the agent — import and call directly
    sys.path.insert(0, os.path.dirname(__file__))

    # Patch deal_desk to use selected deal
    import deal_desk as dd

    # Override the fetch to return our selected deal directly
    original_fetch = dd.fetch_deal_data

    def patched_fetch(filter=None):
        context = original_fetch(filter)
        # Ensure selected deal is front of list
        context["deal"] = selected
        return context

    dd.fetch_deal_data = patched_fetch
    dd.run_deal_desk()


if __name__ == "__main__":
    main()
