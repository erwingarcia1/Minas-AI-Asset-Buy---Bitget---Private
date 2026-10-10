"""One-off after the v0.8.0 deploy (run by the operator inside the api container):
    python -m api.tasks.reverify_unified                 # re-verify through v3, store modes, send NO emails
    python -m api.tasks.reverify_unified --send-emails   # same, and queue the agreed customer emails (owner's go)
Resumes nothing."""
import asyncio
import sys

from api.database import AsyncSessionLocal
from api.services import telegram
from api.services.unified_check import reverify_flagged


async def main(send_emails: bool) -> None:
    async with AsyncSessionLocal() as db:
        out = await reverify_flagged(db, send_emails=send_emails)
    await telegram.flush()
    print(out)


if __name__ == "__main__":
    asyncio.run(main("--send-emails" in sys.argv[1:]))
