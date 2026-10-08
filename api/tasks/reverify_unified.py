"""One-off after the v0.8.0 deploy (run by the operator inside the api container):
    python -m api.tasks.reverify_unified
Re-verifies every key flagged as Unified by v0.7.7 through the v3 API, stores its mode and emails the owner.
Resumes nothing."""
import asyncio

from api.database import AsyncSessionLocal
from api.services import telegram
from api.services.unified_check import reverify_flagged


async def main() -> None:
    async with AsyncSessionLocal() as db:
        out = await reverify_flagged(db)
    await telegram.flush()
    print(out)


if __name__ == "__main__":
    asyncio.run(main())
