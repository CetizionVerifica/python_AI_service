"""S3-12 · Scope 3 AI endpoints (ESG-lite docs/scope3/01-contracts.md §5).

``POST /v1/scope3/eeio-sector`` is called by ESG-lite only (``X-Service-Key``),
from its ``POST /scope3/screening/sector-suggestions``; a signed-in user's
token gets 403. See app/scope3/eeio.py.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import Principal, allow_service
from app.scope3.eeio import EeioSectorRequest, EeioSectorResponse, suggest

router = APIRouter(tags=["Scope 3"])


def service_only(principal: Principal = Depends(allow_service)) -> Principal:
    if principal.kind != "service":
        raise HTTPException(status_code=403, detail="ESG-lite only")
    return principal


@router.post("/scope3/eeio-sector", response_model=EeioSectorResponse, dependencies=[Depends(service_only)])
def eeio_sector(payload: EeioSectorRequest) -> EeioSectorResponse:
    return suggest(payload)
