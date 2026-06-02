"""HTTP layer for the broker daemon.

Bind-and-shift on EADDRINUSE per V2_PLAN.md locked decision #6. Three verbs
plus one read endpoint:

    GET  /status                 -> pool snapshot (no lease tokens leaked)
    POST /acquire {holder, slot?, dev_port?}  -> LeaseRecord incl. token
    POST /heartbeat {lease_token}             -> LeaseRecord (refreshed expiry)
    POST /release {lease_token}               -> LeaseRecord (now free)
    GET  /reaped                              -> last-50 reap log (debugging)

Returns 409 for pool_exhausted / slot_leased, 404 for unknown_slot /
unknown_lease, 400 for malformed payloads. The MCP shim translates these
into model-readable errors; the LLM never sees raw HTTP status codes.
"""

from __future__ import annotations

import errno
import socket
from dataclasses import asdict

from aiohttp import web

from escarp.broker.lease import (
    Broker,
    PoolExhausted,
    SlotLeased,
    UnknownLease,
    UnknownSlot,
)
from escarp.broker.pool import (
    NoBrowserOnPort,
    PoolController,
    PoolError,
    SlotAlreadyInPool,
)

DEFAULT_PORT = 7878


def build_app(broker: Broker, *, pool: PoolController | None = None) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/status")
    async def status(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "pool_size": broker.pool_size(),
                "lease_ttl_s": broker.lease_ttl_s,
                "reaper_interval_s": broker.reaper_interval_s,
                "slots": broker.snapshot(),
            }
        )

    @routes.post("/acquire")
    async def acquire(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_json"}, status=400)
        holder = body.get("holder")
        if not holder or not isinstance(holder, str):
            return web.json_response({"error": "missing_holder"}, status=400)
        slot = body.get("slot")
        dev_port = body.get("dev_port")
        try:
            rec = await broker.acquire(holder=holder, slot=slot, dev_port=dev_port)
        except PoolExhausted:
            return web.json_response(
                {"error": "pool_exhausted", "snapshot": broker.snapshot()},
                status=409,
            )
        except SlotLeased as exc:
            return web.json_response({"error": "slot_leased", "message": str(exc)}, status=409)
        except UnknownSlot as exc:
            return web.json_response({"error": "unknown_slot", "message": str(exc)}, status=404)
        # The acquire response is the ONLY place we return the lease token.
        return web.json_response(asdict(rec))

    @routes.post("/heartbeat")
    async def heartbeat(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_json"}, status=400)
        token = body.get("lease_token")
        if not token:
            return web.json_response({"error": "missing_lease_token"}, status=400)
        try:
            rec = await broker.heartbeat(lease_token=token)
        except UnknownLease as exc:
            return web.json_response({"error": "unknown_lease", "message": str(exc)}, status=404)
        return web.json_response(asdict(rec))

    @routes.post("/release")
    async def release(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_json"}, status=400)
        token = body.get("lease_token")
        if not token:
            return web.json_response({"error": "missing_lease_token"}, status=400)
        try:
            rec = await broker.release(lease_token=token)
        except UnknownLease as exc:
            return web.json_response({"error": "unknown_lease", "message": str(exc)}, status=404)
        return web.json_response(
            rec.to_public(
                lease_ttl_s=broker.lease_ttl_s,
                reaper_interval_s=broker.reaper_interval_s,
            )
        )

    @routes.get("/reaped")
    async def reaped(_request: web.Request) -> web.Response:
        return web.json_response({"reaped": broker.reaped_log()})

    @routes.post("/pool/add")
    async def pool_add(request: web.Request) -> web.Response:
        if pool is None:
            return web.json_response({"error": "hot_pool_unavailable"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_json"}, status=400)
        slot = body.get("slot")
        if not isinstance(slot, int):
            return web.json_response({"error": "missing_slot"}, status=400)
        try:
            rec = await pool.add_slot(slot)
        except SlotAlreadyInPool as exc:
            return web.json_response({"error": "slot_already_in_pool", "message": str(exc)}, status=409)
        except NoBrowserOnPort as exc:
            return web.json_response({"error": "no_browser_on_port", "message": str(exc)}, status=409)
        except PoolError as exc:
            return web.json_response({"error": "pool_error", "message": str(exc)}, status=409)
        return web.json_response(
            rec.to_public(lease_ttl_s=broker.lease_ttl_s, reaper_interval_s=broker.reaper_interval_s)
        )

    @routes.post("/pool/remove")
    async def pool_remove(request: web.Request) -> web.Response:
        if pool is None:
            return web.json_response({"error": "hot_pool_unavailable"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_json"}, status=400)
        slot = body.get("slot")
        if not isinstance(slot, int):
            return web.json_response({"error": "missing_slot"}, status=400)
        force = bool(body.get("force", False))
        try:
            await pool.remove_slot(slot, force=force)
        except UnknownSlot as exc:
            return web.json_response({"error": "unknown_slot", "message": str(exc)}, status=404)
        except SlotLeased as exc:
            return web.json_response({"error": "slot_leased", "message": str(exc)}, status=409)
        return web.json_response({"removed": True, "slot": slot})

    app = web.Application()
    app.add_routes(routes)
    return app


def bind_with_shift(host: str, preferred_port: int, *, max_attempts: int = 10) -> tuple[socket.socket, int]:
    """Bind a TCP socket atomically; on EADDRINUSE shift by +10 and retry.

    Per V2_PLAN.md locked decision #6: bind() is the atomic test, no
    pre-check race. Returns (bound socket, actual port).
    """
    last_error: OSError | None = None
    for i in range(max_attempts):
        port = preferred_port + i * 10
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(128)
            return sock, port
        except OSError as exc:
            sock.close()
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            last_error = exc
    raise OSError(
        f"could not bind {host}:{preferred_port} after {max_attempts} attempts: {last_error}"
    )
