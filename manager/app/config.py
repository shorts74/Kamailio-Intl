import os

PG_HOST = os.environ.get("PLATFORM_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("PLATFORM_PG_PORT", "5432"))
PG_DB   = os.environ.get("PLATFORM_PG_DB", "kamailio")
PG_USER = os.environ.get("PLATFORM_PG_USER", "kamailio")
PG_PASS = os.environ.get("PLATFORM_PG_PASS", "CHANGE_ME")

HOMER_PG_DB = os.environ.get("PLATFORM_HOMER_DB", "homer_config")

DEFAULT_SSH_KEY = os.environ.get("PLATFORM_SSH_KEY", "/root/.ssh/node_automation")
APP_PORT = int(os.environ.get("PLATFORM_APP_PORT", "5001"))
APP_SECRET = os.environ.get("PLATFORM_SECRET", "change-this-secret-key")
# Set to True once HTTPS is configured (nginx TLS termination) --
# Secure cookies are never sent over plain HTTP, so enabling this
# before TLS exists would break login entirely.
FORCE_SECURE_COOKIES = os.environ.get("PLATFORM_FORCE_SECURE_COOKIES", "false").lower() == "true"
SSH_TIMEOUT = 10
PAGE_SIZE = 25

# Troubleshoot Toolkit -- PCAP capture. Ceilings are server-side and
# always enforced regardless of what a request asks for -- an admin
# can ask for less, never more.
PCAP_STORAGE_DIR = os.environ.get("PLATFORM_PCAP_DIR", "/var/lib/platform-manager/pcap-captures")
PCAP_MAX_DURATION_SEC = 4 * 60 * 60      # 4 hours
PCAP_MAX_SIZE_MB = 1024                  # 1 GB hard ceiling
PCAP_DEFAULT_SIZE_MB = 500
PCAP_RETENTION_HOURS = 48                # completed captures auto-expire after this
PCAP_DISK_USED_PCT_LIMIT = 90            # refuse to start a new capture above this

# Authoritative copy of the node-side bundle (kamailio.cfg.template +
# scripts) the Manager pushes on every apply_and_restart() -- this is
# what actually makes a restart apply the LATEST code, not just the
# latest data. Updated via the Node Bundle admin page whenever a new
# platform-v3-node package is released.
NODE_BUNDLE_DIR = os.environ.get("PLATFORM_NODE_BUNDLE_DIR", "/var/lib/platform-manager/node-bundle")


# Certificate Management -- SSH key rotation. Private key content for
# a registry key gets written here (not just kept in the DB) whenever
# it's actually used to connect -- ssh(1) needs a real file, not DB
# content piped in.
SSH_MANAGED_KEYS_DIR = os.environ.get("PLATFORM_SSH_MANAGED_KEYS_DIR", "/root/.ssh/managed_keys")
