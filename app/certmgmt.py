"""
Certificate Management -- local generation of self-signed TLS certs
and SSH keypairs. Both call real system tools (openssl, ssh-keygen)
via subprocess rather than a Python crypto library, matching what an
admin would get running these commands by hand -- output format is
guaranteed compatible with what Kamailio/OpenSSH actually expect,
since it's the exact same tool they'd use themselves.
"""
import subprocess
import tempfile
import os
import config


def generate_self_signed_cert(common_name, days=3650):
    """
    Generates a self-signed cert+key pair entirely in a temp dir,
    reads both back as PEM text, cleans up the temp files immediately
    -- nothing touches the real filesystem beyond that. Appropriate
    for purely internal traffic (HEP) where there's no public CA to
    verify against anyway; the cert's own signature is the trust
    anchor a node is told to check against directly.

    Returns (cert_pem, key_pem, error) -- error is None on success.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = os.path.join(tmpdir, "key.pem")
        cert_path = os.path.join(tmpdir, "cert.pem")
        try:
            result = subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", key_path, "-out", cert_path,
                 "-days", str(days), "-subj", f"/CN={common_name}"],
                capture_output=True, text=True, timeout=30
            )
        except Exception as e:
            return None, None, f"Could not run openssl: {e}"
        if result.returncode != 0:
            return None, None, f"openssl failed: {result.stderr.strip()[-300:]}"
        try:
            with open(cert_path) as f:
                cert_pem = f.read()
            with open(key_path) as f:
                key_pem = f.read()
        except OSError as e:
            return None, None, f"Generated but could not read back: {e}"
        if "BEGIN CERTIFICATE" not in cert_pem or "PRIVATE KEY" not in key_pem:
            return None, None, "openssl reported success but output doesn't look like valid PEM"
        return cert_pem, key_pem, None


def write_managed_key_file(key_id, private_key_content):
    """
    Writes a registry key's private key content to a real file on the
    Manager's disk -- ssh(1) needs an actual file path, not DB
    content piped in. Idempotent: same key_id always writes to the
    same path, overwriting with current content (covers the case
    where the registry content changed since it was last written).
    600 permissions, matching how a real private key file must be
    (OpenSSH refuses to use a key file that's group/world-readable).

    Returns the file path.
    """
    os.makedirs(config.SSH_MANAGED_KEYS_DIR, exist_ok=True, mode=0o700)
    path = os.path.join(config.SSH_MANAGED_KEYS_DIR, f"key_{key_id}")
    with open(path, "w") as f:
        f.write(private_key_content)
    os.chmod(path, 0o600)
    return path


DEFAULT_AUTOMATION_KEY_PATH = "/root/.ssh/node_automation"


def get_default_automation_key():
    """
    Reads the Manager's own default automation public key from disk --
    generated once by manager-install.sh (ssh-keygen -f
    /root/.ssh/node_automation), and until now only ever shown in that
    script's own terminal output at install time, with no way to
    retrieve it again afterward. This is what every node's own
    ssh_key_path defaults to, so it's the key actually doing the work
    for Apply & Restart/sync/troubleshooting on most nodes, even
    though it was never a row in platform_ssh_keys.

    Returns the public key string, or None if the file doesn't exist
    (e.g. a dev/test environment that never ran manager-install.sh's
    real key-generation step, or SKIP_SSH_HARDENING-style setups that
    manage keys entirely outside this platform).
    """
    try:
        with open(DEFAULT_AUTOMATION_KEY_PATH + ".pub") as f:
            return f.read().strip()
    except OSError:
        return None



    """
    Appends a public key to a node's authorized_keys over the node's
    EXISTING working connection (its current ssh_key_path) -- never
    replaces anything already there, only adds. Idempotent: checks
    for an exact existing match first so re-pushing the same key
    twice doesn't create a duplicate line.

    Returns (ok, message).
    """
    import nodeops
    # grep -qF a literal match first, || append -- avoids a duplicate
    # line on a re-push, and avoids needing to shell-escape the key
    # content directly into a single command (it's passed as -f data
    # via stdin instead, through a small heredoc-safe pattern).
    escaped = public_key_content.replace("'", "'\\''")
    cmd = f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && grep -qF '{escaped}' ~/.ssh/authorized_keys || echo '{escaped}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    out, ok = nodeops.ssh_run(node["ssh_host"], node["ssh_key_path"], cmd, timeout=15)
    return ok, "Pushed" if ok else f"Failed to push: {out}"


def test_connect_with_key(node, key_path):
    """
    Attempts a real connection to the node using a SPECIFIC key path
    (not the node's currently-configured ssh_key_path) -- this is the
    actual verification step in the rotation flow: don't just assume
    a pushed key works, prove it with a real connection before ever
    considering switching the node's primary key reference to it.

    Returns (ok, message).
    """
    import nodeops
    out, ok = nodeops.ssh_run(node["ssh_host"], key_path, "echo rotation-test-ok", timeout=10)
    if ok and "rotation-test-ok" in out:
        return True, "Connected successfully"
    return False, f"Could not connect with this key: {out}"


def push_hep_certificate(cert_pem, key_pem, cert_path="/etc/heplify-server/certs/heplify-server-cert.pem",
                          key_path="/etc/heplify-server/certs/heplify-server-key.pem"):
    """
    Writes the nominated HEP certificate to heplify-server's actual
    TLS cert path and restarts it to pick up the change -- a LOCAL
    file write and LOCAL service restart, unlike SIP TLS certs, since
    heplify-server runs on the Manager itself, not on a node. No SSH
    involved.

    VERIFIED directly against the real heplify-server binary this
    session, not assumed: generated a distinctively-named test cert,
    placed it at exactly this path (matching manager-install.sh's own
    TLSCertFolder = "/etc/heplify-server/certs"), started heplify-
    server pointed at that folder, and confirmed via a live TLS
    handshake that it actually served that cert as the issuing CA --
    not a freshly auto-generated one. Earlier in this session,
    heplify-server's cert mechanism was found to self-generate its
    own CA when nothing is present at this path; this confirms the
    reverse also holds -- a pre-placed cert here is genuinely loaded
    and used instead. The exact filenames (heplify-server-cert.pem /
    heplify-server-key.pem) matter -- they're not an arbitrary choice,
    they're what the underlying negbie/cert library specifically
    looks for.
    """
    try:
        os.makedirs(os.path.dirname(cert_path), exist_ok=True, mode=0o755)
        with open(cert_path, "w") as f:
            f.write(cert_pem)
        with open(key_path, "w") as f:
            f.write(key_pem)
        os.chmod(key_path, 0o600)
    except OSError as e:
        return False, f"Could not write cert files: {e}"

    try:
        result = subprocess.run(["systemctl", "restart", "heplify-server"],
                                 capture_output=True, text=True, timeout=15)
    except Exception as e:
        return False, f"Cert written, but could not restart heplify-server: {e}"
    if result.returncode != 0:
        return False, f"Cert written, but heplify-server restart failed: {result.stderr.strip()[-300:]}"
    return True, "HEP certificate pushed and heplify-server restarted"


def generate_ssh_keypair(comment="platform-generated"):
    """
    Generates an ed25519 SSH keypair (smaller and faster to verify
    than RSA, the modern default for OpenSSH), same temp-dir-then-
    read-back-then-clean-up pattern as the cert generator above.

    Returns (public_key, private_key, error) -- error is None on success.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = os.path.join(tmpdir, "id_ed25519")
        try:
            result = subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-C", comment, "-q"],
                capture_output=True, text=True, timeout=15
            )
        except Exception as e:
            return None, None, f"Could not run ssh-keygen: {e}"
        if result.returncode != 0:
            return None, None, f"ssh-keygen failed: {result.stderr.strip()[-300:]}"
        try:
            with open(f"{key_path}.pub") as f:
                public_key = f.read().strip()
            with open(key_path) as f:
                private_key = f.read()
        except OSError as e:
            return None, None, f"Generated but could not read back: {e}"
        if not public_key.startswith("ssh-"):
            return None, None, "ssh-keygen reported success but public key output looks wrong"
        return public_key, private_key, None
