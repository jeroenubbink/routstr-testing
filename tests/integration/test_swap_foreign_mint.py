"""Foreign-mint swap on wallet topup (routstr-core swap_to_primary_mint).

When a node receives a Cashu token issued by a mint it does NOT trust
(`token.mint not in CASHU_MINTS`), it swaps the value to its primary mint:
mint-quote on primary -> melt the foreign proofs to pay that invoice ->
mint fresh proofs on primary. This exercises the real swap path end to end
through `POST /v1/wallet/topup`, with two local nutshell FakeWallet mints
(`primary-mint` trusted, `foreign-mint` untrusted) so it runs with real Cashu
crypto and zero real sats.

Run config (set by the swap_foreign_mint scenario): the node must trust ONLY
the primary mint, i.e. `CASHU_MINTS=http://primary-mint:3338`, so a
`foreign-mint` token triggers the swap instead of a plain same-mint redeem.

The funded foreign token is minted inside the `foreign-mint` container (it ships
the `cashu` wallet lib and reaches both mints over the compose network); the
topup's `{"msats": ...}` response is the swap result directly (swapped sats x
1000). Marked `destructive` (creates a key + credits balance) so it auto-skips
under TARGET_PROFILE=remote.
"""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from tests.integration import targets

pytestmark = pytest.mark.destructive

# Docker-network hostnames (the node and the minting container reach the mints
# here); host port mappings are 3338/3339 but services talk over the network.
PRIMARY_MINT_INTERNAL = "http://primary-mint:3338"
FOREIGN_MINT_INTERNAL = "http://foreign-mint:3338"

# Tokens are minted using the NODE's own cashu wallet lib (version-matched to
# the code under test) by exec-ing the node container's venv python; the node
# reaches both mints over the compose network. Minting here is orthogonal to the
# app under test — the container is just a convenient version-matched cashu host.
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
# A temp db keeps runs clean.
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


def _mint_token(mint_internal_url: str, amount: int) -> str:
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
        pytest.skip(f"cannot mint test token (docker/{NODE_CONTAINER} unavailable): {exc}")
    if r.returncode != 0:
        pytest.skip(f"mint helper failed (is {NODE_CONTAINER} up?): {r.stderr[-500:]}")
    for line in r.stdout.splitlines():
        if line.startswith("TOKEN:"):
            return line[len("TOKEN:"):].strip()
    pytest.skip(f"no token in mint output: {r.stdout[-300:]}")


@pytest.fixture(scope="module", autouse=True)
def _require_stack() -> None:
    try:
        if httpx.get(f"{NODE}/v1/info", timeout=5).status_code >= 500:
            pytest.skip(f"node not reachable at {NODE}; run `make up`")
    except httpx.HTTPError:
        pytest.skip(f"node not reachable at {NODE}; run `make up`")


def _topup(cashu_token: str, bearer: str) -> httpx.Response:
    return httpx.post(
        f"{NODE}/v1/wallet/topup",
        params={"cashu_token": cashu_token},
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=90,
    )


def test_foreign_mint_token_swaps_on_topup() -> None:
    """A foreign-mint token tops up the node via the swap path.

    The Bearer is a primary-mint token (redeemed same-mint at auth to establish
    the key); the foreign-mint token is then swapped by /topup. Because a real
    cross-mint swap burns a melt fee_reserve + input fees, the credited msats are
    strictly LESS than the token's face value. A credit equal to the face value
    would mean no swap happened (e.g. a trusted same-mint redeem) — so the strict
    inequality is what proves swap_to_primary_mint actually ran.

    The companion negative control
    (test_trusted_mint_token_credits_full_amount_no_swap) credits the FULL face
    value for a trusted-mint token, pinning the difference here to the swap fee.
    """
    bearer = _mint_token(PRIMARY_MINT_INTERNAL, BEARER_SATS)
    foreign = _mint_token(FOREIGN_MINT_INTERNAL, TOKEN_SATS)

    r = _topup(foreign, bearer)
    assert r.status_code == 200, f"topup failed: HTTP {r.status_code}: {r.text[:300]}"
    credited = r.json()["msats"]

    assert 0 < credited < TOKEN_SATS * 1000, (
        f"foreign-mint topup should swap and credit LESS than the face value "
        f"{TOKEN_SATS * 1000} msats (melt + input fees deducted), got {credited}. "
        f"A credit of exactly {TOKEN_SATS * 1000} would mean no swap occurred."
    )


def test_trusted_mint_token_credits_full_amount_no_swap() -> None:
    """Negative control: a token from the node's TRUSTED (primary) mint is
    redeemed same-mint with NO swap, so the full face value is credited and no
    fee is deducted.

    This is the causation guard for the swap test: it proves the shortfall there
    is caused by the swap specifically, not by anything incidental to /topup. If
    this credited less than face value, the swap test's inequality would be
    meaningless (topup itself would be lossy).
    """
    bearer = _mint_token(PRIMARY_MINT_INTERNAL, BEARER_SATS)
    trusted = _mint_token(PRIMARY_MINT_INTERNAL, TOKEN_SATS)

    r = _topup(trusted, bearer)
    assert r.status_code == 200, f"topup failed: HTTP {r.status_code}: {r.text[:300]}"
    credited = r.json()["msats"]

    assert credited == TOKEN_SATS * 1000, (
        f"trusted same-mint topup should credit the full face value "
        f"{TOKEN_SATS * 1000} msats with no swap fee, got {credited}."
    )
