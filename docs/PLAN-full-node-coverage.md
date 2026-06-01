# Full Node Functionality — Test Coverage Status

Status: **in progress** · Updated 2026-06-02 · Target repo: `routstr-testing`

Original goals:
1. Preconfigure a node with different providers; user supplies own upstream creds (webui **or** yaml).
2. Scenarios driving **routstrd** to route to the cheapest node per model.
3. Scenarios testing **routstr-cli** node config (add/update provider, update pricing, …).
4. Scenarios covering the **orchestration** logic itself.
5. (added) Real paid inference + **X-Cashu** pay-per-request, with correct spend telemetry in the UI.

## At a glance

| Area | Status | Evidence |
|---|---|---|
| Phase 0 — vendor sync + capability recon | ✅ done | findings below |
| Cheapest-routing test (routstrd ranks by price) | ✅ done | `test_routstrd_cheapest.py`, run #1 — 18/18 |
| routstr-cli fee update drives routing | ✅ done (live) | `providers update <id> --fee`, ranking flips both ways |
| Real paid inference across models | ✅ done | `test_real_inference.py`, runs #3/#7 — 9 models via openrouter |
| X-Cashu pay-per-request mode | ✅ done | `test_xcashu.py`, runs #6/#8 — 4 models + change-spendable |
| Spend telemetry in UI (`token_consumed_msats`) | ✅ done | `spend.py` + `test_spend_unit.py`, run #7=349 msat, #8=4 sat |
| webui build + single-origin serve | ✅ done | host pnpm build, `make serve` / uvicorn on :8000 |
| Phase 1 — per-node distinct upstreams | ⛔ pending | both nodes currently share openrouter |
| Phase 2 — routstrd actual forwarding / failover | ⚠️ blocked | routeRequests + payment bugs (see Bugs) |
| Phase 3 — CLI config-lifecycle scenarios | ⛔ pending | fee update done live, not yet a scenario |
| Phase 4 — orchestration edge tests | ⛔ pending | — |

---

## Phase 0 — Findings (recon complete)

- **Mint** is `https://mint.chorus.community` (real testnut-style mint, charges swap fees), not `testnut.cashu.space`. Set via `CASHU_MINTS` in `.env`.
- **routstrd discovery is hardcoded to PUBLIC relays** (`nos.lol`, `relay.damus.io`, `relay.primal.net`, `relay.routstr.com`) inside the bundled `@routstr/sdk` — **no env override**. `RELAY_URL` in compose is unused for discovery. Patched in `vendor-dockerfiles/routstrd.Dockerfile` (sed the bundle → `ws://relay:8080`) so only local node-a/node-b are discovered.
- **`--provider <url>`** sets `deps.provider`, used as the **default `forcedProvider`** for every request → an override that bypasses price discovery. Removed from `compose.yml` so discovery runs.
- **Cheapest selection** = `getModelProviders()` (`vendor/routstrd/src/daemon/models.ts`) sorts providers by `sats_pricing.max_cost` ascending; `provider_fee` multiplies that pricing. Exposed at `GET /models/<id>/providers`.
- **CLI fee command**: `routstr --node <url> providers update <id> -t <token> --fee <multiplier>` → `PATCH /admin/api/upstream-providers/<id> {provider_fee}`. Fee is a decimal multiplier (1.01 default; <1 = discount).
- **Admin token**: `POST /admin/api/login {password}` → `{token}` (password from node `ADMIN_PASSWORD`).
- **Node auto-seeds** an openrouter provider at startup when `OPENROUTER_API_KEY` is set (default fee 1.01) — `UPSTREAM_BASE_URL/KEY` are NOT used for seeding.
- **routstrd client mode** defaults to `apikeys`; its own `xcashu` daemon mode is "coming soon" (so X-Cashu is exercised at the node level).
- **CLI runner**: `docker exec routstr-testing-cli-runner-1 bun /app/dist/index.js …`.

---

## Phase 1 — Per-node providers + user creds — ⛔ PENDING

Both nodes currently share one upstream (openrouter) via `OPENROUTER_API_KEY` in `compose.yml` (auto-seed). Still to do: distinct upstream per node (node-a=openai, node-b=anthropic) via `NODE_A_*`/`NODE_B_*` env namespacing + a `node_upstreams` map in the scenario schema + webui per-node cred inputs. See original plan items 1a–1f (still valid).

What IS done: openrouter configured on both nodes from the operator's own key; `POST /api/runs` already accepts write-only `upstream_env`; `RunTokenModal.tsx` renders per-`required_env` inputs.

---

## Phase 2 — routstrd price-based routing — ⚠️ PARTIAL / BLOCKED

**Done:** cheapest-RANKING is proven. `tests/integration/test_routstrd_cheapest.py` + `scenarios/routstrd_cheapest.yaml` (run #1, **18/18**): for 9 models × 2 fee regimes, routstrd ranks the lower-`provider_fee` node first, and the ranking **flips** when fees flip (node-a 0.3→#1, then node-b 0.35→#1). Fees driven by `routstr-cli`. Discovery isolated to local nodes via the relay patch.

**Blocked:** the actual request-forwarding path (`POST /v1/chat/completions` through routstrd) does not complete — see Bugs #5 and #6. Failover (`test_failover_to_next_cheapest`) and model-filter scenarios are not built yet (depend on the forwarding path working).

Pending compose nicety: make node pricing env-overridable (`NODE_A_FIXED_COST_PER_REQUEST`) for cleaner reprice scenarios.

---

## Phase 3 — routstr-cli config-lifecycle — ⛔ PENDING (partly exercised live)

`providers update <id> --fee` is used live and verified (Phase 2). Still to package as `tests/cli/test_config_lifecycle.py` + scenario: add-provider→route, disable→404, pricing update→balance delta, model enable/disable, config get/set round-trip. Base happy-path specs already in `tests/cli/test_schema_driven.py`.

---

## Phase 4 — Orchestration self-tests — ⛔ PENDING

Not yet built. See original list (cost-ceiling reject, missing-env abort, remote-no-urls, timeout, malformed junit, param injection, etc.). All unit-level, `services_required: false`, CI-safe.

---

## Delivered beyond the original plan

### Real paid inference
`tests/integration/test_real_inference.py` + `scenarios/real_inference.yaml` (runs #3/#7). 9 models (gpt-4o-mini, gpt-4o, o3-mini-high, claude-3.5-haiku, llama-3.3-70b, aion-llama-3.1-8b, mistral-medium-3-5, deepseek-v3.1, qwen2.5-vl-72b) → real openrouter completions, paid from a funded ecash balance via the node's Bearer api-key path. o3-mini-high handled as a reasoning model (empty content + `finish_reason=length` accepted).

### X-Cashu pay-per-request
`tests/integration/test_xcashu.py` + `scenarios/xcashu.yaml` (runs #6/#8). `X-Cashu: <token>` header (no auth) → node redeems, charges exact cost, returns change in the `X-Cashu` response header. 4 models single-shot (own token each) + change-token-is-spendable.

### Spend telemetry (fixes "Token spent always 0")
Node billing is sub-sat (millisats); the integer `token_consumed_sats` rounded real spends to 0, and the balance probe was gated to `services_required` and probed routstrd (not the node that was actually paid). Fix:
- `runner/models.py` — new `token_consumed_msats` column (idempotent ALTER).
- `tests/integration/spend.py` — dependency-free Cashu TokenV4 decoder + `record_msats()`/`record_sats()` writing to `$SPEND_REPORT_PATH`.
- `runner/orchestrate.py` — always set `SPEND_REPORT_PATH`; sum it into `token_consumed_msats`; fall back to it for `token_consumed_sats`.
- paid tests report their spend; `server/schemas.py`+`server/runs.py` expose it; `webui` `RunsPage.tsx` `formatSpend()` renders msat/sat.
- `tests/integration/test_spend_unit.py` — 4 green unit tests (no stack/funds).
Result: run #7 shows **349 msat**, run #8 shows **4 sats** (was "0 sats").

### Vendor build / harness fixes (in `vendor-dockerfiles/routstrd.Dockerfile` + `compose.yml`, survive `make sync`)
- `webui` Docker build broke `make up` (corepack/pnpm on node20) → bring up services explicitly; build webui on host (node22/pnpm10).
- node healthcheck used absent `wget` → switched to `python urllib`.
- routstrd: `better-sqlite3` optional native dep not built → install + `--external` for bundling; missing `cocod` wallet daemon → `bun install -g @routstr/cocod`; public-relay isolation (above).
- `make serve` / uvicorn serves webui + API single-origin on :8000.

---

## Bugs found (vendor / routstr stack)

1. **node refund/X-Cashu change retention (fund leak).** `POST /v1/wallet/refund` and X-Cashu change return a token to the caller but node-a keeps the underlying proofs in its own nutshell wallet; re-receiving the token elsewhere fails `proofs already spent`. Leaks funds every refund/change cycle. Recoverable only while the node still holds unspent proofs (via `cashu_transactions` / `/app/.wallet`, mint-checked with NUT-07). **Main money drain — do not run more real-money rounds until fixed.**
2. **routstrd → node payment minting fails.** When routstrd proxies a paid request it cannot mint a payment token the node accepts (`credit_balance: token redemption failed`, "expected 2, got 0"). Client→node direct payment works; routstrd→node does not.
3. **routeRequests provider resolution empty.** routstrd's `routeRequests` (the actual forwarding path) returns `No providers found for model` even though `/models/<id>/providers` lists both — the `providerManager` needs Nostr model-price events the local nodes don't emit in the SDK-expected form.
4. **routstrd discovery has no relay override** (hardcoded public relays) — patched here, should be an env/config upstream.
5. **routstrd build** needs `better-sqlite3` + `cocod` that the upstream Dockerfile doesn't provide for a direct `bun run` of the bundle.

## Out of scope (per decision)
- No local cashu mint — use external `mint.chorus.community`. Cheapest-routing/CLI scenarios are payment-free (rank via `/models/<id>/providers`); paid scenarios use real ecash directly against the node.

## Recommended next order
1. Fix Bug #1 (refund/change retention) — stops fund leakage; prerequisite for any further real-money testing.
2. Phase 4 orchestration edge tests (free, de-risks the harness).
3. Phase 1 per-node upstreams (unlocks richer Phase 2/3).
4. Phase 2 failover/model-filter — after Bugs #2/#3 unblock routstrd forwarding.
5. Phase 3 CLI config-lifecycle scenario.
