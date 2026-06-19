"""Foreign-mint swap retry on a melt failure (routstr-core PR #549).

A foreign mint's melt fee_reserve is a non-binding NUT-05 estimate: the mint may
reject the melt at execution even though every quote looked fine (the
mint.cubabitcoin.org / issue #468 incident). The fix retries the
mint-quote/melt-quote/melt cycle with the amount recomputed from the fee the
mint actually demanded, instead of failing the whole topup.

This drives that retry over the real wire. The token is minted through the
`fault-proxy` (so the node treats the proxy as the issuing mint); the proxy
forwards everything to the real foreign mint EXCEPT it fails the first
melt-execute with the verbatim #468 "not enough inputs" error. Because that melt
never reaches the mint, the proofs stay unspent — so a node that retries can
re-melt them and the topup succeeds.

Discriminates the fix: with the retry (PR #549) the topup returns 200 and the
proxy records >= 2 melt attempts; without it (e.g. vendor/routstr-core on main)
the first failure aborts the swap and the topup errors out.

Marked `destructive` (creates a key + credits balance) so it auto-skips under
TARGET_PROFILE=remote.
"""
from __future__ import annotations

import os

import httpx
import pytest

from tests.integration._swap_helpers import (
    FAULT_PROXY_INTERNAL,
    PRIMARY_MINT_INTERNAL,
    BEARER_SATS,
    TOKEN_SATS,
    mint_token,
    topup,
)
from tests.integration.targets import require_node, unavailable

pytestmark = pytest.mark.destructive

# Host-facing control plane of the fault proxy (the test process runs on the host).
PROXY_CTL = os.environ.get("FAULT_PROXY_URL", "http://localhost:3340")


@pytest.fixture(scope="module", autouse=True)
def _require_stack() -> None:
    require_node()
    try:
        if httpx.get(f"{PROXY_CTL}/__proxy__/stats", timeout=5).status_code != 200:
            unavailable(f"fault-proxy not reachable at {PROXY_CTL}; run `make up`")
    except httpx.HTTPError:
        unavailable(f"fault-proxy not reachable at {PROXY_CTL}; run `make up`")


def test_swap_retries_after_melt_failure_and_credits() -> None:
    """The first melt fails; the node retries and the topup still succeeds."""
    # Arm exactly one fault and clear the proxy's counters for this run.
    httpx.post(f"{PROXY_CTL}/__proxy__/reset", params={"faults": 1}, timeout=10)

    bearer = mint_token(PRIMARY_MINT_INTERNAL, BEARER_SATS)
    # Minted THROUGH the proxy, so token.mint == fault-proxy and the node does all
    # foreign-mint operations (incl. the faulted melt) through it.
    foreign = mint_token(FAULT_PROXY_INTERNAL, TOKEN_SATS)

    r = topup(foreign, bearer)
    assert r.status_code == 200, (
        f"topup should recover via swap retry, got HTTP {r.status_code}: {r.text[:300]}"
    )
    credited = r.json()["msats"]
    assert 0 < credited < TOKEN_SATS * 1000, (
        f"swapped credit should be below face value {TOKEN_SATS * 1000} msats, got {credited}"
    )

    # The retry must actually have fired: the proxy saw the failed melt plus at
    # least one more. This is what distinguishes the fix from a lucky one-shot.
    stats = httpx.get(f"{PROXY_CTL}/__proxy__/stats", timeout=10).json()
    assert stats["faulted"] >= 1, f"proxy never injected a fault: {stats}"
    assert stats["melt_attempts"] >= 2, (
        f"expected a retry (>= 2 melt attempts), got {stats} — the node aborted "
        f"instead of retrying (is the fix present?)"
    )
