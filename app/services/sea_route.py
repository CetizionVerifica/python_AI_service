from typing import Any, Dict, List

import searoute as sr

from app.schemas.sea_route import SeaRouteRequest, SeaRouteData, SeaRouteGeometry


def _safe_origin_label(lat: float, lng: float, address: str | None) -> str:
    return address or f"{lat},{lng}"


def _extract_coordinates(route: Any) -> List[List[float]]:
    # route is usually a GeoJSON-like feature object/dict
    geometry = None

    if isinstance(route, dict):
        geometry = route.get("geometry")
    else:
        geometry = getattr(route, "geometry", None)

    if not geometry:
        return []

    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(coordinates, list):
        return []

    cleaned: List[List[float]] = []
    for coord in coordinates:
        if (
            isinstance(coord, (list, tuple))
            and len(coord) >= 2
            and isinstance(coord[0], (int, float))
            and isinstance(coord[1], (int, float))
        ):
            cleaned.append([float(coord[0]), float(coord[1])])

    return cleaned


def _extract_length_km(route: Any) -> float | None:
    properties: Dict[str, Any] | None = None

    if isinstance(route, dict):
        properties = route.get("properties")
    else:
        properties = getattr(route, "properties", None)

    if not properties:
        return None

    length = properties.get("length")
    if isinstance(length, (int, float)):
        return float(length)

    return None


def calculate_sea_route(payload: SeaRouteRequest) -> SeaRouteData:
    origin = [payload.origin.lng, payload.origin.lat]
    destination = [payload.destination.lng, payload.destination.lat]

    route = sr.searoute(origin, destination)

    # print("route", route)

    length_km = _extract_length_km(route)
    if length_km is None:
        raise ValueError("No sea route distance returned by searoute")

    coordinates = _extract_coordinates(route)

    sea_geometry = None
    if coordinates:
        sea_geometry = SeaRouteGeometry(
            type="LineString",
            coordinates=coordinates,
        )

    # print("sea route data", SeaRouteData(
    #     mode="sea",
    #     distanceMeters=round(length_km * 1000),
    #     duration=None,
    #     durationText=None,
    #     encodedPolyline=None,
    #     origin=_safe_origin_label(
    #         payload.origin.lat,
    #         payload.origin.lng,
    #         payload.origin.address,
    #     ),
    #     destination=_safe_origin_label(
    #         payload.destination.lat,
    #         payload.destination.lng,
    #         payload.destination.address,
    #     ),
    #     # seaGeometry=sea_geometry,
    # ))

    return SeaRouteData(
        mode="sea",
        distanceMeters=round(length_km * 1000),
        duration=None,
        durationText=None,
        encodedPolyline=None,
        origin=_safe_origin_label(
            payload.origin.lat,
            payload.origin.lng,
            payload.origin.address,
        ),
        destination=_safe_origin_label(
            payload.destination.lat,
            payload.destination.lng,
            payload.destination.address,
        ),
        seaGeometry=sea_geometry,
    )