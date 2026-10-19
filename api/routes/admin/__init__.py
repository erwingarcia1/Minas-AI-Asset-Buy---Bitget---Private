"""Admin surface (/api/admin): users, parked purchases, operations.

`router` aggregates the three modules; `require_admin` and `audit` are
re-exported for callers and tests."""
from fastapi import APIRouter

from api.routes.admin import ops, purchases, users
from api.routes.admin._deps import audit, require_admin  # noqa: F401
from api.routes.admin.purchases import _gateway  # noqa: F401  (tests patch api.routes.admin.purchases._gateway)

router = APIRouter()
router.include_router(users.router)
router.include_router(purchases.router)
router.include_router(ops.router)
