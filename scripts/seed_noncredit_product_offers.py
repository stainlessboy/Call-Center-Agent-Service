#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.seed_deposit_product_offers import _seed as _seed_deposits  # noqa: E402
from scripts.seed_card_product_offers import _seed as _seed_cards  # noqa: E402


async def _seed_all(manifest: Path, replace: bool) -> None:
    dep_inserted, dep_skipped = await _seed_deposits(manifest, replace)
    print(f"Deposits — inserted: {dep_inserted}, skipped: {dep_skipped}")

    card_inserted, card_skipped = await _seed_cards(manifest, replace)
    print(f"Cards    — inserted: {card_inserted}, skipped: {card_skipped}")


def main() -> None:
    p = argparse.ArgumentParser(description="Seed normalized non-credit offers (deposits + cards).")
    p.add_argument("--manifest", type=Path, default=Path("app/data/ai_chat_info.json"))
    p.add_argument("--replace", action="store_true")
    args = p.parse_args()
    if not args.replace:
        raise SystemExit("Use --replace for deterministic reseed.")
    manifest = args.manifest.resolve()
    if not manifest.exists():
        raise SystemExit(f"Manifest not found: {manifest}")
    asyncio.run(_seed_all(manifest, args.replace))
    print("Done.")


if __name__ == "__main__":
    main()
