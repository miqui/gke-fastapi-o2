from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.state import state

# Paths are fixed: k8s/deployment.yaml's probes reference them. Handlers stay dependency-free -
# no DB, no cache - so a slow dependency can't fail the probes and restart a healthy pod.
router = APIRouter(prefix="/health", tags=["health"], include_in_schema=False)


@router.get("/liveness")
async def liveness() -> dict[str, str]:
    return {"status": "UP"}


@router.get("/readiness")
async def readiness() -> JSONResponse:
    if state.ready:
        return JSONResponse({"status": "UP"})
    return JSONResponse({"status": "DOWN"}, status_code=503)
