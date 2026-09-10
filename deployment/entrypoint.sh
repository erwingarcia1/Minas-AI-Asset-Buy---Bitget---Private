#!/bin/sh
# Apply migrations before serving; production never runs create_all.
set -e

# Check if we should skip migrations
if [ "$SKIP_MIGRATIONS" != "true" ]; then
    echo "Applying database migrations..."
    alembic upgrade head
else
    echo "Skipping database migrations (SKIP_MIGRATIONS=true)"
fi

exec "$@"
