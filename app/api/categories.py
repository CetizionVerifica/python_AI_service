from typing import Optional

from fastapi import APIRouter, HTTPException
from app.services import category_matcher
from app.schemas.category import EmissionCategory
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/emission-categories", response_model=list[EmissionCategory])
async def get_emission_categories(site_id: Optional[int] = None):
    """
    Get the unique emission categories available for matching.
    Useful for frontend dropdowns or type-ahead fields.

    With ``site_id``: the shared factors plus that site's company's factors
    only. Without it: every company's (the router is Superadmin-only).
    """
    try:
        categories = category_matcher.fetch_emission_categories(
            site_id=site_id, all_companies=site_id is None
        )
        return categories
    except Exception as e:
        logger.error(f"Failed to fetch emission categories: {e}")
        raise HTTPException(status_code=500, detail=str(e))
