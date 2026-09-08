"""E2E regression: UUID-bearing Apple samples must reconcile legacy rows."""

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
async def test_uuid_sample_reconciles_existing_legacy_heart_rate_row() -> None:
    """A UUID-bearing retry must not collide with the legacy active-row index."""
    device_type = f"e2e legacy uuid {uuid4()}"
    sample_uuid = str(uuid4())
    sample_time = datetime(2026, 9, 1, 12, 34, 56, tzinfo=UTC)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        device_id = await conn.fetchval(
            "INSERT INTO devices (device_type) VALUES ($1) RETURNING id",
            device_type,
        )
        await conn.execute(
            """
            INSERT INTO heart_rate (time, device_id, bpm, owner_id)
            VALUES ($1, $2, $3, $4)
            """,
            sample_time,
            device_id,
            72,
            OWNER,
        )
    finally:
        await conn.close()

    payload = {
        "metric": "heart_rate",
        "batch_index": 0,
        "total_batches": 1,
        "samples": [
            {
                "date": sample_time.isoformat(),
                "qty": 72,
                "source": device_type,
                "uuid": sample_uuid,
            }
        ],
    }

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        response = await client.post("/api/apple/batch", json=payload, headers=_headers())

    assert response.status_code in (200, 201, 202), (
        f"UUID retry collided with existing legacy row: "
        f"{response.status_code} {response.text[:400]}"
    )

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT source_uuid, status
            FROM heart_rate
            WHERE time = $1
              AND device_id = $2
              AND owner_id = $3
              AND status = 'active'
            """,
            sample_time,
            device_id,
            OWNER,
        )
    finally:
        await conn.close()

    assert len(rows) == 1
    assert str(rows[0]["source_uuid"]) == sample_uuid


@pytest.mark.asyncio
async def test_uuid_sample_reconciles_existing_legacy_hrv_row() -> None:
    """A UUID-bearing HRV retry must reconcile the matching legacy row."""
    device_type = f"e2e legacy hrv uuid {uuid4()}"
    sample_uuid = str(uuid4())
    sample_time = datetime(2026, 9, 1, 12, 35, 56, tzinfo=UTC)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        device_id = await conn.fetchval(
            "INSERT INTO devices (device_type) VALUES ($1) RETURNING id",
            device_type,
        )
        await conn.execute(
            """
            INSERT INTO hrv (time, device_id, value_ms, owner_id)
            VALUES ($1, $2, $3, $4)
            """,
            sample_time,
            device_id,
            48.5,
            OWNER,
        )
    finally:
        await conn.close()

    payload = {
        "metric": "heart_rate_variability",
        "batch_index": 0,
        "total_batches": 1,
        "samples": [
            {
                "date": sample_time.isoformat(),
                "qty": 48.5,
                "source": device_type,
                "uuid": sample_uuid,
            }
        ],
    }

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        response = await client.post("/api/apple/batch", json=payload, headers=_headers())

    assert response.status_code in (200, 201, 202), (
        f"UUID HRV retry collided with existing legacy row: "
        f"{response.status_code} {response.text[:400]}"
    )

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT source_uuid, status
            FROM hrv
            WHERE time = $1
              AND device_id = $2
              AND owner_id = $3
              AND status = 'active'
            """,
            sample_time,
            device_id,
            OWNER,
        )
    finally:
        await conn.close()

    assert len(rows) == 1
    assert str(rows[0]["source_uuid"]) == sample_uuid
