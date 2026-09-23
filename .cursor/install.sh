#!/usr/bin/env bash
# Idempotent bootstrap for the Kamailio-Intl Manager development environment.
# Installs PostgreSQL + Python deps, creates the local dev database, applies
# the schema, and seeds a local admin login. Safe to run repeatedly.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANAGER_DIR="$REPO_ROOT/manager"
VENV_DIR="$MANAGER_DIR/.venv"

DB_NAME="kamailio"
DB_USER="kamailio"
DB_PASS="kamailio"

echo "==> Installing system packages (postgresql, python venv)"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y
sudo apt-get install -y postgresql postgresql-client python3-venv

echo "==> Ensuring PostgreSQL cluster is running"
sudo pg_ctlcluster "$(pg_lsclusters -h | awk 'NR==1{print $1}')" main start 2>/dev/null || true
# Wait for the socket to accept connections.
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done

echo "==> Creating role and database (idempotent)"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
  ELSE
    ALTER ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASS}';
  END IF;
END \$\$;
SELECT 'CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='${DB_NAME}')\gexec
GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};
SQL

echo "==> Applying schema (idempotent: uses IF NOT EXISTS / ON CONFLICT)"
PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -q -f "$MANAGER_DIR/schema.sql"

echo "==> Creating Python virtualenv and installing dependencies"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -q -r "$MANAGER_DIR/requirements.txt"

echo "==> Seeding local admin account (username: admin / password: admin)"
ADMIN_HASH="$("$VENV_DIR/bin/python" -c "import bcrypt; print(bcrypt.hashpw(b'admin', bcrypt.gensalt()).decode())")"
PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -c \
  "INSERT INTO platform_users (username, password_hash, role, auth_source, enabled)
   VALUES ('admin', '${ADMIN_HASH}', 'admin', 'local', true)
   ON CONFLICT (username) DO UPDATE
     SET password_hash=EXCLUDED.password_hash, role='admin', enabled=true;"

echo "==> Ensuring application log directory exists"
sudo mkdir -p /var/log/sip-platform
sudo chown "$(id -un):$(id -gn)" /var/log/sip-platform

echo "==> Install complete."
