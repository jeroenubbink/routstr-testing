"""Shared helpers for the foreign-mint swap tests (Phase 1 + Phase 2).

Not a test module (no test_ prefix) so pytest does not collect it.
"""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from tests.integration import targets

# Docker-network hostnames: the node and the minting container reach the mints
# here; host port mappings (3338/3339/3340) are only for the test process itself.
PRIMARY_MINT_INTERNAL = "http://primary-mint:3338"
FOREIGN_MINT_INTERNAL = "http://foreign-mint:3338"
FAULT_PROXY_INTERNAL = "http://fault-proxy:3340"

# Tokens are minted with the NODE's own cashu lib (version-matched to the code
# under test) by exec-ing the node container's venv python; the node reaches the
# mints over the compose network. Minting is orthogonal to the app under test —
# the container is just a convenient version-matched cashu host.
NODE_CONTAINER = os.environ.get("NODE_CONTAINER", "routstr-testing-node-a-1")
NODE_VENV_PYTHON = "/.venv/bin/python"

NODE = targets.node_api_url(0)
TOKEN_SATS = 32
BEARER_SATS = 64  # comfortably above any input-fee floor for the key-identity token

# Fund a wallet at MINT_URL for AMOUNT sat and print the serialized token.
# include_dleq=True: the node runs verify_proofs_dleq on same-mint receive, so a
# token without DLEQ data is rejected. legacy=True (TokenV3/cashuA): nutshell
# issues 64-hex "v2" keyset ids which the compact TokenV4 (cashuB) form truncates
# to 16 hex, breaking the node's keyset lookup; V3 preserves the full id string.
_MINT_SCRIPT = """
import asyncio, os, tempfile
from cashu.wallet.wallet import Wallet

async def main():
    url = os.environ["MINT_URL"]
    amount = int(os.environ["AMOUNT"])
    with tempfile.TemporaryDirectory() as d:
        w = await Wallet.with_db(url, db=d, unit="sat")
        await w.load_mint()
        quote = await w.request_mint(amount)
        await asyncio.sleep(0.5)  # let FakeWallet settle the invoice
        proofs = await w.mint(amount, quote_id=quote.quote)
        token = await w.serialize_proofs(proofs, include_dleq=True, legacy=True)
        print("TOKEN:" + token)

asyncio.run(main())
"""


def _stack_required() -> bool:
    """True when the orchestrator provisioned the stack (services_required).

    In that case a missing service is a real failure to surface, not a reason
    to skip — see _unavailable().
    """
    return os.environ.get("SERVICES_REQUIRED") == "1"


def _unavailable(reason: str) -> "pytest.fail | pytest.skip":
    """Fail when the stack was provisioned for us; skip when running ad hoc.

    Under the orchestrator (SERVICES_REQUIRED=1) the stack is guaranteed live,
    so a missing node/mint means the test couldn't exercise what it claims —
    that must be a red failure, never a silent green skip. Run directly without
    `make up` and it still skips so a bare `pytest` doesn't error out.
    """
    if _stack_required():
        pytest.fail(f"{reason} (orchestrator provisioned the stack — this is a real failure)")
    pytest.skip(reason)


def mint_token(mint_internal_url: str, amount: int) -> str:
    """Mint `amount` sat at `mint_internal_url` and return the serialized token."""
    try:
        r = subprocess.run(
            [
                "docker", "exec", "-i",
                "-e", f"MINT_URL={mint_internal_url}",
                "-e", f"AMOUNT={amount}",
                NODE_CONTAINER, NODE_VENV_PYTHON, "-",
            ],
            input=_MINT_SCRIPT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        _unavailable(f"cannot mint test token (docker/{NODE_CONTAINER} unavailable): {exc}")
    if r.returncode != 0:
        _unavailable(f"mint helper failed (is {NODE_CONTAINER} up?): {r.stderr[-500:]}")
    for line in r.stdout.splitlines():
        if line.startswith("TOKEN:"):
            return line[len("TOKEN:"):].strip()
    _unavailable(f"no token in mint output: {r.stdout[-300:]}")
    raise AssertionError("unreachable")  # _unavailable always raises


def topup(cashu_token: str, bearer: str) -> httpx.Response:
    return httpx.post(
        f"{NODE}/v1/wallet/topup",
        params={"cashu_token": cashu_token},
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=90,
    )


def require_node() -> None:
    """Ensure the node is reachable.

    Under the orchestrator this is a hard failure (the stack was provisioned);
    run ad hoc without `make up` it skips. See _unavailable().
    """
    try:
        if httpx.get(f"{NODE}/v1/info", timeout=5).status_code >= 500:
            _unavailable(f"node not reachable at {NODE}; run `make up`")
    except httpx.HTTPError:
        _unavailable(f"node not reachable at {NODE}; run `make up`")
