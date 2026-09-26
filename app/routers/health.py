from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.cache import cache
from app.state import state

# Paths are fixed: k8s/deployment.yaml's probes reference them. Handlers stay dependency-free -
# no DB, no cache call - so a slow dependency can't fail the probes and restart a healthy pod.
router = APIRouter(prefix="/health", tags=["health"], include_in_schema=False)


@router.get("/liveness")
async def liveness() -> JSONResponse:
    # One exception to "always UP": a Hazelcast client that gave up reconnecting (the member was
    # gone longer than cluster_connect_timeout, e.g. rescheduled by a node scale-down) shuts
    # itself down for good, and every cache call then raises - a 500 on every GET/PATCH/DELETE of
    # a message, forever. Restarting the container is the only recovery, so report DOWN and let
    # the kubelet do it. `cache.connected` is a local lifecycle flag, not a network call. Only
    # checked while ready: before startup the client doesn't exist yet, and during shutdown it
    # is closed on purpose.
    if state.ready and not cache.connected:
        return JSONResponse({"status": "DOWN", "reason": "cache client shut down"}, status_code=503)
    return JSONResponse({"status": "UP"})


@router.get("/readiness")
async def readiness() -> JSONResponse:
    if state.ready:
        return JSONResponse({"status": "UP"})
    return JSONResponse({"status": "DOWN"}, status_code=503)
