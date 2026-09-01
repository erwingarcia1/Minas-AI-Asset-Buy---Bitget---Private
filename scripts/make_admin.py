"""Promote a user to admin: python scripts/make_admin.py user@example.com"""
import os
import sys

from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.config import settings  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/make_admin.py <email>")
    email = sys.argv[1].lower()
    engine = create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"))
    with engine.begin() as conn:
        result = conn.execute(
            text("UPDATE users SET role='admin' WHERE email=:email"), {"email": email})
        if result.rowcount == 0:
            sys.exit(f"no user with email {email}")
    print(f"{email} is now an admin")


if __name__ == "__main__":
    main()
