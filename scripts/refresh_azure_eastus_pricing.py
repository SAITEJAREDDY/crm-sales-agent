#!/usr/bin/env python3
"""Phase 8.4.5 CLI: refresh the East US master pricing snapshot and rebuild
the curated enterprise architecture catalog from it.

Usage:
    python scripts/refresh_azure_eastus_pricing.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from tools.azure_pricing_snapshot import refresh_snapshot, SNAPSHOT_PATH  # noqa: E402
from tools.azure_enterprise_catalog import refresh_catalog, ARCHITECTURE_CATALOG_PATH  # noqa: E402


def _line(label: str, value) -> str:
    return f"{label:<30}{value}"


def main() -> int:
    print("Azure East US Pricing Refresh\n")

    result = refresh_snapshot()
    if result.get("refresh_status") == "FAILED":
        print(f"FAILED: {result.get('error')}")
        print(f"Previous snapshot still available: {result.get('previous_snapshot_available')}")
        return 1

    meta = result["snapshot"]
    print(_line("API pages retrieved:", meta["api_pages"]))
    print(_line("Raw records (type=Consumption):", f"{meta['raw_record_count']:,}"))
    print(_line("Excluded (spot/reservation/etc.):", f"{meta['excluded_record_total']:,}"))
    print(_line("Normalized meters:", f"{meta['record_count']:,}"))
    print(_line("  East US-scoped:", f"{meta['eastus_record_count']:,}"))
    print(_line("  Global/non-regional:", f"{meta['global_record_count']:,}"))
    print()
    print("Excluded by reason:")
    for reason, count in sorted(meta.get("excluded_record_counts", {}).items(), key=lambda kv: -kv[1]):
        print(_line(f"  {reason}:", f"{count:,}"))

    try:
        from tools.azure_pricing_snapshot import load_snapshot
        catalog = refresh_catalog(load_snapshot())
        cmeta = catalog["metadata"]
        print()
        print(_line("Architecture catalog entries:", cmeta["architecture_entries"]))
        print(_line("East US priced entries:", cmeta["entries_with_valid_eastus_pricing"]))
        print(_line("Global-priced entries:", cmeta["entries_using_global_pricing"]))
        print(_line("Unsupported entries:", cmeta["unsupported_entries"]))
        if cmeta["unsupported_entries"]:
            print(f"  unsupported: {', '.join(cmeta['unsupported_catalog_ids'])}")
        print()
        print("Entries by domain:")
        for domain, count in sorted(cmeta.get("entries_by_domain", {}).items()):
            print(_line(f"  {domain}:", count))
    except Exception as exc:
        print(f"\nArchitecture catalog rebuild FAILED: {exc}")
        return 1

    print()
    print("Snapshot:")
    print(f"  {SNAPSHOT_PATH}")
    print()
    print("Architecture Catalog:")
    print(f"  {ARCHITECTURE_CATALOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
