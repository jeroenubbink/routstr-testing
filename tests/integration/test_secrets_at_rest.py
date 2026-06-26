"""Config-ownership secret storage at rest (routstr-core #553).

Whole-system proof that operator-supplied secrets handed to a real node via
container env (ADMIN_PASSWORD, NSEC) are migrated into the encrypted Secret store
at boot, instead of living in plaintext in the editable settings blob:

  * the admin password still authenticates (POST /admin/api/login) — the env seed
    was hashed into the Secret store, not broken;
  * ``admin_password`` is no longer an editable settings field (GET
    /admin/api/settings omits it — it became a one-way hash, not config);
  * the node exposes a dedicated nsec rotation endpoint (PATCH /admin/api/nsec)
    that derives the npub — the Nostr identity is a rotatable secret, not a blob
    field smuggled through the general settings PATCH;
  * the plaintext admin password and nsec the operator supplied do NOT appear
    anywhere in the node's on-disk SQLite database.

These are whole-system behaviours the in-repo unit/integration tests can't prove:
a real container boot (run_migrations -> bootstrap_secrets -> initialize ordering),
real env-provided secrets, a real SQLite file on disk, and real HTTP through
uvicorn.

Discriminating: RED against a node that keeps secrets in the plaintext settings
blob (pre-#553); GREEN once they move into the Fernet/scrypt-backed Secret store
and ``admin_password`` leaves the settings model. Marked ``destructive`` (it
rotates the node's nsec) so it auto-skips under TARGET_PROFILE=remote.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import httpx
import pytest

from tests.integration.targets import (
    ADMIN_PASSWORD,
    admin_token,
    is_remote,
    node_api_url,
    require_node,
    unavailable,
)

pytestmark = pytest.mark.destructive

NODE = 0

# The nsec the compose stack seeds node-a with (compose.yml default). The node
# persists secrets at rest, so this exact bech32 string must NOT survive in the
# DB once #553 encrypts it. Mirror the compose default; an override flows through
# the same env var.
NODE_NSEC = os.environ.get(
    "NODE_A_NSEC",
    "nsec1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqsmhltgl",
)

# A valid 64-char hex private key — accepted by the node's nsec parser exactly as
# bootstrap accepts one — used to exercise the rotation endpoint.
ROTATE_NSEC_HEX = "1" * 64

# routstr-testing repo root (the docker-compose project dir), for `docker compose cp`.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _require_stack() -> None:
    require_node(NODE)


def _admin_headers() -> dict[str, str]:
    """Authenticate with the node's admin password; obtaining a token proves the
    env-seeded password still logs in after the migration to the hashed store."""
    token = admin_token(NODE)
    if not token:
        unavailable(
            f"could not obtain an admin token for node {NODE} "
            f"(login with the env admin password failed)"
        )
    return {"Authorization": f"Bearer {token}"}


def test_admin_password_is_not_a_settings_field() -> None:
    """admin_password must not be an editable settings field.

    Under #553 the admin password is a one-way scrypt hash in the Secret store,
    not a value you read or write through the settings blob. GET /admin/api/settings
    must therefore not carry an ``admin_password`` key at all (a node that still
    exposes it — redacted or not — is keeping the password as blob config).
    """
    headers = _admin_headers()
    with httpx.Client(base_url=node_api_url(NODE), timeout=15) as client:
        resp = client.get("/admin/api/settings", headers=headers)
    assert resp.status_code == 200, (
        f"GET /admin/api/settings failed: HTTP {resp.status_code}: {resp.text[:300]}"
    )
    data = resp.json()
    assert "admin_password" not in data, (
        "admin_password is still exposed as a settings field — under #553 it must "
        "be a one-way hash in the Secret store, not an editable config value. "
        f"settings keys: {sorted(data)[:40]}"
    )


def test_nsec_rotation_endpoint_derives_npub() -> None:
    """The node exposes a dedicated nsec rotation endpoint that derives the npub.

    The Nostr identity is a rotatable secret with its own write path
    (PATCH /admin/api/nsec), not a field smuggled through the general settings
    PATCH (which strips it). A 404/405 means that write path is missing.
    """
    headers = _admin_headers()
    with httpx.Client(base_url=node_api_url(NODE), timeout=15) as client:
        resp = client.patch(
            "/admin/api/nsec", json={"nsec": ROTATE_NSEC_HEX}, headers=headers
        )
    assert resp.status_code == 200, (
        "PATCH /admin/api/nsec should rotate the node's Nostr identity (200) — a "
        f"404/405 means the dedicated nsec write path is missing. "
        f"Got HTTP {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body.get("ok") is True, f"expected ok=True, got {body!r}"
    assert str(body.get("npub", "")).startswith("npub1"), (
        f"endpoint should derive and return the npub for the new key, got {body!r}"
    )


def test_no_plaintext_secret_in_node_database() -> None:
    """The operator's plaintext password and nsec must not survive on disk.

    The flagship #553 guarantee: secrets supplied via env are hashed/encrypted
    into the Secret store, never persisted in the plaintext settings blob.

    The settings blob is written lazily (a fresh node leaves it empty), so a bare
    "grep the DB" would pass vacuously on a node that *would* persist secrets the
    moment the blob is touched. We therefore first force a settings persist with a
    harmless edit, assert the probe actually landed on disk (so the secret-absence
    check is meaningful, not vacuous), then assert the raw DB bytes contain neither
    the plaintext admin password nor the bech32 nsec. A node that keeps secrets in
    the settings blob (pre-#553) writes them alongside the probe and fails here.
    """
    if shutil.which("docker") is None:
        unavailable("docker CLI not available to inspect the node DB at rest")

    headers = _admin_headers()
    probe = "at-rest-probe-node-name"
    with httpx.Client(base_url=node_api_url(NODE), timeout=15) as client:
        resp = client.patch(
            "/admin/api/settings", json={"name": probe}, headers=headers
        )
    assert resp.status_code == 200, (
        f"settings PATCH (to force a blob persist) failed: HTTP {resp.status_code}: "
        f"{resp.text[:300]}"
    )

    db_bytes = _copy_node_db()

    # Guard against a vacuous pass: the probe must have been persisted, proving a
    # settings blob was actually written to disk for the secret-absence check to mean
    # anything. A pre-#553 node persists admin_password/nsec into that same blob.
    assert probe.encode() in db_bytes, (
        "the settings blob was not persisted to disk after a PATCH, so the "
        "plaintext-secret check below would be vacuous — the node's persistence "
        "path changed; pick a field that actually writes the blob"
    )

    assert ADMIN_PASSWORD.encode() not in db_bytes, (
        "the plaintext admin password is present in the node's on-disk database — "
        "it must be stored only as a one-way scrypt hash in the Secret store, never "
        "in the settings blob"
    )
    assert NODE_NSEC.encode() not in db_bytes, (
        "the plaintext nsec is present in the node's on-disk database — the Nostr "
        "identity must be Fernet-encrypted in the Secret store, not kept in the "
        "settings blob"
    )


def _copy_node_db() -> bytes:
    """Copy node-a's live SQLite state off the container for at-rest inspection.

    Returns the concatenated bytes of the main DB file AND its write-ahead-log
    (``-wal``) sidecar: the node runs SQLite in WAL mode, so a freshly committed
    row lives in ``node-a.db-wal`` until it is checkpointed into ``node-a.db``.
    Copying only the main file would miss recent writes and report a misleadingly
    "clean" database. The ``-wal`` may be absent (already checkpointed) — that's
    fine, we just inspect whatever is present.

    Uses `docker compose cp` from the project root. Under the orchestrator
    (SERVICES_REQUIRED=1) failing to read the main DB is a real failure; ad hoc it
    skips.
    """
    if is_remote():
        unavailable("cannot inspect a remote node's disk for plaintext secrets")

    dest_dir = _REPO_ROOT / "logs"
    dest_dir.mkdir(parents=True, exist_ok=True)

    blobs: list[bytes] = []
    for suffix, required in (("", True), ("-wal", False)):
        src = f"node-a:/data/node-a.db{suffix}"
        dest = dest_dir / f"node-a-at-rest.db{suffix}"
        dest.unlink(missing_ok=True)
        proc = subprocess.run(
            ["docker", "compose", "cp", src, str(dest)],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not dest.exists():
            if required:
                unavailable(
                    "could not copy node-a DB for at-rest inspection: "
                    f"{(proc.stderr or proc.stdout)[:300]}"
                )
            continue
        try:
            blobs.append(dest.read_bytes())
        finally:
            dest.unlink(missing_ok=True)

    return b"".join(blobs)
