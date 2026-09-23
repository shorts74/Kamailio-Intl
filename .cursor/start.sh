#!/usr/bin/env bash
# Per-boot reconciliation for the Kamailio-Intl Manager dev environment.
# Ensures PostgreSQL is running and the app log directory exists, then returns.
set -euo pipefail

echo "==> Ensuring PostgreSQL cluster is running"
sudo pg_ctlcluster "$(pg_lsclusters -h | awk 'NR==1{print $1}')" main start 2>/dev/null || true
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done

echo "==> Ensuring application log directory exists"
sudo mkdir -p /var/log/sip-platform
sudo chown "$(id -un):$(id -gn)" /var/log/sip-platform

echo "==> Start reconciliation complete."
