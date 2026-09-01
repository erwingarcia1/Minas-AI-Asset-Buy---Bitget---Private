#!/bin/sh
# Apply migrations before serving; production never runs create_all.
set -e
echo "Applying database migrations..."
alembic upgrade head
exec "$@"
