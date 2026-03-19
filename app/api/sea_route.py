from fastapi import APIRouter, HTTPException

from app.schemas.sea_route import SeaRouteRequest, SeaRouteResponse
from app.services.sea_route import calculate_sea_route

router = APIRouter(tags=["Sea Route"])


@router.post("/sea-route", response_model=SeaRouteResponse)
def sea_route(payload: SeaRouteRequest):
    try:
        data = calculate_sea_route(payload)
        return SeaRouteResponse(success=True, data=data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to calculate sea route: {str(e)}")