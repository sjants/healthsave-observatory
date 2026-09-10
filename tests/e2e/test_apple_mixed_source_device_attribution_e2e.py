"""E2E regression: mixed-source Apple batches preserve device attribution."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

BASE_URL = os.getenv("E2E_BASE_URL")
DATABASE_URL = os.getenv("E2E_DATABASE_URL")
API_KEY = os.getenv("E2E_API_KEY", "")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not BASE_URL or not DATABASE_URL,
        reason="set E2E_BASE_URL + E2E_DATABASE_URL to run e2e",
    ),
]

OWNER = UUID("00000000-0000-0000-0000-000000000001")


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY} if API_KEY else {}


@pytest.mark.asyncio
async def test_mixed_source_batch_projects_each_sample_under_its_own_device() -> None:
    """A later source in the batch must not inherit the first source's device id."""
    first_device_type = f"e2e mixed first {uuid4()}"
    second_device_type = f"e2e mixed second {uuid4()}"
    first_sample_uuid = str(uuid4())
    second_sample_uuid = str(uuid4())
    first_sample_time = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    second_sample_time = datetime(2026, 9, 9, 12, 1, tzinfo=UTC)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        first_device_id = await conn.fetchval(
            "INSERT INTO devices (device_type) VALUES ($1) RETURNING id",
            first_device_type,
        )
        second_device_id = await conn.fetchval(
            "INSERT INTO devices (device_type) VALUES ($1) RETURNING id",
            second_device_type,
        )
    finally:
        await conn.close()

    payload = {
        "metric": "heart_rate",
        "batch_index": 0,
        "total_batches": 1,
        "samples": [
            {
                "date": first_sample_time.isoformat(),
                "qty": 120,
                "source": first_device_type,
                "uuid": first_sample_uuid,
            },
            {
                "date": second_sample_time.isoformat(),
                "qty": 72,
                "source": second_device_type,
                "uuid": second_sample_uuid,
            },
        ],
    }

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        response = await client.post("/api/apple/batch", json=payload, headers=_headers())

    assert response.status_code in (200, 201, 202), (
        f"mixed-source batch failed: {response.status_code} {response.text[:400]}"
    )

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT source_uuid, device_id
            FROM heart_rate
            WHERE source_uuid = ANY($1::uuid[])
              AND owner_id = $2
              AND status = 'active'
            """,
            [UUID(first_sample_uuid), UUID(second_sample_uuid)],
            OWNER,
        )
    finally:
        await conn.close()

    device_by_uuid = {str(row["source_uuid"]): row["device_id"] for row in rows}

    assert device_by_uuid == {
        first_sample_uuid: first_device_id,
        second_sample_uuid: second_device_id,
    }
