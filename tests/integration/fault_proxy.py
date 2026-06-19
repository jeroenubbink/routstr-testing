"""Fault-injecting reverse proxy in front of a Cashu mint (Phase 2 swap retry).

Transparently forwards every request to PROXY_TARGET (the real foreign mint) so
all crypto/protocol flows untouched, EXCEPT the NUT-05 melt-execute endpoint
(`POST /v1/melt/bolt11`): the first N executes after a reset are short-circuited
with the verbatim issue-#468 "not enough inputs" NUT-00 error instead of being
forwarded. The mint therefore never sees those melts (proofs stay unspent), so a
node that correctly retries can re-melt the same proofs and succeed.

This drives routstr-core's swap_to_primary_mint retry loop over the real wire:
real error string -> real cashu wallet lib -> the node's melt-failure classifier
-> retry. The melt-quote path (`POST /v1/melt/quote/bolt11`) is never faulted.

Control plane (not forwarded):
  POST /__proxy__/reset?faults=1  -> arm N faults, clear stats
  GET  /__proxy__/stats           -> {melt_attempts, faulted, fault_remaining}

Runs on the nutshell image (already ships fastapi/uvicorn/httpx) — no new build.
"""
import json
import os
from collections import Counter

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

TARGET = os.environ.get("PROXY_TARGET", "http://foreign-mint:3338").rstrip("/")
PORT = int(os.environ.get("PROXY_PORT", "3340"))

# NUT-05 melt EXECUTE paths (path param has no leading slash). The melt QUOTE
# path (v1/melt/quote/bolt11) is deliberately excluded — only the execute fails.
MELT_EXECUTE_PATHS = {"v1/melt/bolt11", "v1/melt"}

# Raw NUT-00 error body a mint returns when inputs don't cover amount + fee.
# The node's classifier keys on the code (11000 = nutshell TransactionError) and
# the "not enough inputs" text; "Provided/needed" gives the retry shrink step.
_FAULT_BODY = json.dumps(
    {"detail": "not enough inputs provided for melt. Provided: 1, needed: 2", "code": 11000}
)

app = FastAPI()
_state = {"fault_remaining": 0}
_stats: Counter = Counter()


@app.post("/__proxy__/reset")
async def reset(faults: int = 1) -> dict:
    _state["fault_remaining"] = faults
    _stats.clear()
    return {"fault_remaining": faults}


@app.get("/__proxy__/stats")
async def stats() -> dict:
    return {
        "melt_attempts": _stats["melt_attempts"],
        "faulted": _stats["faulted"],
        "fault_remaining": _state["fault_remaining"],
    }


@app.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
)
async def proxy(path: str, request: Request) -> Response:
    body = await request.body()

    if request.method == "POST" and path in MELT_EXECUTE_PATHS:
        _stats["melt_attempts"] += 1
        print(f"[fault-proxy] melt execute #{_stats['melt_attempts']} "
              f"(fault_remaining={_state['fault_remaining']})", flush=True)
        if _state["fault_remaining"] > 0:
            _state["fault_remaining"] -= 1
            _stats["faulted"] += 1
            return Response(
                content=_FAULT_BODY, status_code=400, media_type="application/json"
            )

    async with httpx.AsyncClient(timeout=90) as client:
        upstream = await client.request(
            request.method,
            f"{TARGET}/{path}",
            params=dict(request.query_params),
            content=body,
            headers={
                k: v
                for k, v in request.headers.items()
                if k.lower() not in ("host", "content-length")
            },
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
