"""Smoke test: validate O1 (atomic set -> PG) + O2 (unified config_provider GET)."""
import asyncio
import os
import sys

sys.path.insert(0, "/app")

import asyncpg
import redis.asyncio as aioredis
from shared.config_provider import ConfigProviderV3


async def main():
    db_url = os.environ["DB_URL"]
    redis_url = os.environ["REDIS_URL"]
    key = "smoke.test.key"
    val = "smoke_value_789"

    pg = await asyncpg.create_pool(dsn=db_url)
    r = aioredis.from_url(redis_url)
    cp = ConfigProviderV3(pg, r)
    await cp.initialize()

    # O1: atomic set must write to PG and return True
    ok = await cp.set(key, val)
    print("SET_OK:", ok)

    # O2: get should read consistently (L1/L2/L3 all resolve to same value)
    g1 = await cp.get(key)
    print("GET_AFTER_SET:", g1)

    # O2: get_keys_by_prefix must query PG (Source of Truth)
    keys = await cp.get_keys_by_prefix("smoke.test.")
    print("PREFIX_RESULT:", keys)

    # Direct PG proof
    pg_direct = await pg.fetchval(
        "SELECT current_value FROM hcm_config.metadata WHERE config_key=$1", key
    )
    print("PG_DIRECT:", pg_direct)

    # Cleanup so we leave no residue
    await pg.execute("DELETE FROM hcm_config.metadata WHERE config_key=$1", key)

    await pg.close()
    await r.aclose()

    assert ok is True, "SET did not return True"
    assert g1 == val, f"GET mismatch: {g1!r} != {val!r}"
    assert keys.get(key) == val, f"PREFIX mismatch: {keys!r}"
    assert str(pg_direct) == val, f"PG mismatch: {pg_direct!r}"
    print("ALL_ASSERTS_PASSED")


if __name__ == "__main__":
    asyncio.run(main())
