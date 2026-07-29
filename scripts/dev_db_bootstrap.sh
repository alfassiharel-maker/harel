#!/usr/bin/env bash
# =============================================================================
# LOCAL DEVELOPMENT AND CI ONLY.
# =============================================================================
# Applies the reviewed migration files to a throwaway local database, and gives
# the roles local passwords so the application can connect.
#
# This script MUST NOT be pointed at staging or production. Real environments
# apply migrations by hand after review — see database/README.md. The two things
# this script does that a real environment must not are:
#   * granting LOGIN and a well-known password to the database roles;
#   * applying every migration unattended.
#
# It refuses to run against anything but a local host, as a guard rail.
# =============================================================================
set -euo pipefail

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
SUPERUSER="${SUPERUSER:-postgres}"
SUPERPASS="${SUPERPASS:-localdev}"
DBNAME="${DBNAME:-aisportscoach}"
LOCAL_ROLE_PASSWORD="${LOCAL_ROLE_PASSWORD:-localdev}"

case "$PGHOST" in
    localhost|127.0.0.1|::1|postgres) ;;
    *)
        echo "REFUSING: PGHOST is '$PGHOST'. This script is local-only." >&2
        exit 1
        ;;
esac

export PGPASSWORD="$SUPERPASS"
psql_super() { psql -h "$PGHOST" -p "$PGPORT" -U "$SUPERUSER" -v ON_ERROR_STOP=1 "$@"; }

echo "==> Recreating database '$DBNAME'"
psql_super -d postgres -c "DROP DATABASE IF EXISTS $DBNAME WITH (FORCE);" >/dev/null
psql_super -d postgres -c "CREATE DATABASE $DBNAME ENCODING 'UTF8';" >/dev/null

echo "==> 0001 extensions, roles, schemas (as superuser)"
psql_super -d "$DBNAME" -q -f database/migrations/0001_extensions_and_roles.sql

echo "==> Granting local logins (LOCAL ONLY — roles ship as NOLOGIN)"
for role in app_migrator app_rw app_ro; do
    psql_super -d "$DBNAME" -q -c \
        "ALTER ROLE $role LOGIN PASSWORD '$LOCAL_ROLE_PASSWORD';"
done
psql_super -d "$DBNAME" -q -c "GRANT CONNECT ON DATABASE $DBNAME TO app_rw, app_ro, app_migrator;"

echo "==> 0002-0010 (as app_migrator, so app_migrator owns every table)"
export PGPASSWORD="$LOCAL_ROLE_PASSWORD"
for file in database/migrations/000[2-9]*.sql database/migrations/001*.sql; do
    [ -e "$file" ] || continue
    echo "    $file"
    psql -h "$PGHOST" -p "$PGPORT" -U app_migrator -d "$DBNAME" \
         -v ON_ERROR_STOP=1 -q -f "$file"
done

echo "==> Verifying the security properties RLS depends on"
export PGPASSWORD="$SUPERPASS"

echo "--- app_rw / app_ro must not bypass RLS ---"
psql_super -d "$DBNAME" -c \
    "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles
     WHERE rolname IN ('app_rw','app_ro');"

echo "--- every table must be owned by app_migrator (owners bypass RLS) ---"
psql_super -d "$DBNAME" -t -c \
    "SELECT count(*) FROM pg_tables
     WHERE schemaname IN ('identity','training','analytics','coaching','billing',
                          'rewards','partners','community','notifications')
       AND tableowner <> 'app_migrator';" \
  | tr -d ' ' | { read -r n; echo "tables not owned by app_migrator: $n"; [ "$n" = "0" ]; }

echo "--- tables with RLS enabled ---"
psql_super -d "$DBNAME" -t -c \
    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname IN ('identity','training','analytics','coaching','billing',
                         'rewards','community','notifications')
       AND c.relkind = 'r' AND c.relrowsecurity;" | tr -d ' '

echo "==> Done. $DBNAME is ready."
