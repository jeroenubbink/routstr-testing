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

Marked `destructive` (creates a key + credits balance) so it auto-skips under
TARGET_PROFILE=remote.
"""
from __future__ import annotations

import pytest

from tests.integration._swap_helpers import (
    FOREIGN_MINT_INTERNAL,
    PRIMARY_MINT_INTERNAL,
    BEARER_SATS,
    TOKEN_SATS,
    mint_token,
    require_node,
    topup,
)

pytestmark = pytest.mark.destructive


@pytest.fixture(scope="module", autouse=True)
def _require_stack() -> None:
    require_node()


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
    bearer = mint_token(PRIMARY_MINT_INTERNAL, BEARER_SATS)
    foreign = mint_token(FOREIGN_MINT_INTERNAL, TOKEN_SATS)

    r = topup(foreign, bearer)
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
    bearer = mint_token(PRIMARY_MINT_INTERNAL, BEARER_SATS)
    trusted = mint_token(PRIMARY_MINT_INTERNAL, TOKEN_SATS)

    r = topup(trusted, bearer)
    assert r.status_code == 200, f"topup failed: HTTP {r.status_code}: {r.text[:300]}"
    credited = r.json()["msats"]

    assert credited == TOKEN_SATS * 1000, (
        f"trusted same-mint topup should credit the full face value "
        f"{TOKEN_SATS * 1000} msats with no swap fee, got {credited}."
    )
