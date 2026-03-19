from pydantic import BaseModel, Field
from typing import List, Optional, Literal


class SeaRoutePoint(BaseModel):
    lat: float
    lng: float
    address: Optional[str] = None


class SeaRouteRequest(BaseModel):
    origin: SeaRoutePoint
    destination: SeaRoutePoint


class SeaRouteGeometry(BaseModel):
    type: Literal["LineString"]
    coordinates: List[List[float]] = Field(default_factory=list)  # [lng, lat]


class SeaRouteData(BaseModel):
    mode: Literal["sea"] = "sea"
    distanceMeters: int
    duration: Optional[str] = None
    durationText: Optional[str] = None
    encodedPolyline: Optional[str] = None
    origin: str
    destination: str
    seaGeometry: Optional[SeaRouteGeometry] = None


class SeaRouteResponse(BaseModel):
    success: bool = True
    data: SeaRouteData