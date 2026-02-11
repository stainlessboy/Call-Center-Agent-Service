#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List

from sqlalchemy import delete

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.models import Branch  # noqa: E402
from app.db.session import get_session  # noqa: E402


def _sample_branches() -> List[Branch]:
    branches: List[Branch] = []

    # 10 отделений в Ташкенте
    for i in range(10):
        branches.append(
            Branch(
                name=f"Tashkent Branch #{i+1}",
                region="Ташкент",
                district=f"Район {i+1}",
                address=f"Ташкент, Район {i+1}, Улица {10 + i}",
                landmarks="Near metro station",
                metro=f"Metro {i+1}",
                phone=f"+998 71 200-{1000 + i}",
                hours="09:00-18:00",
                weekend="Sat-Sun",
                inn="123456789",
                mfo="00444",
                postal_index="100000",
                uzcard_accounts="40802 0000 0000 0000 001",
                humo_accounts="40803 0000 0000 0000 001",
                latitude=41.3111 + i * 0.01,
                longitude=69.2797 + i * 0.01,
            )
        )

    # Несколько отделений в других регионах (Самарканд, Бухара, Андижан)
    regions = [
        ("Самарканд", "Самаркандский район", 39.6542, 66.9597),
        ("Бухара", "Бухарский район", 39.7740, 64.4286),
        ("Андижан", "Андижанский район", 40.7821, 72.3442),
    ]
    counter = 1
    for region, district, lat, lon in regions:
        for j in range(3):
            branches.append(
                Branch(
                    name=f"{region} Branch #{j+1}",
                    region=region,
                    district=f"{district} {j+1}",
                    address=f"{region}, {district} {j+1}, Sample street {5 + j}",
                    landmarks="Near main square",
                    metro=None,
                    phone=f"+998 90 100-{2000 + counter}",
                    hours="09:00-18:00",
                    weekend="Sat-Sun",
                    inn="987654321",
                    mfo="00555",
                    postal_index="200000",
                    uzcard_accounts="40802 0000 0000 0000 101",
                    humo_accounts="40803 0000 0000 0000 101",
                    latitude=lat + j * 0.005,
                    longitude=lon + j * 0.005,
                )
            )
            counter += 1

    return branches


async def _seed(replace: bool) -> None:
    branches = _sample_branches()
    async with get_session() as session:
        if replace:
            await session.execute(delete(Branch))
        session.add_all(branches)
        await session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed test branches into the database.")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Remove existing branches before inserting test data.",
    )
    args = parser.parse_args()

    asyncio.run(_seed(args.replace))
    print("Seed completed.")


if __name__ == "__main__":
    main()
