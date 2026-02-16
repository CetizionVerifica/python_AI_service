from fastapi import APIRouter, HTTPException
from app.services import category_matcher
from app.schemas.category import EmissionCategory
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/emission-categories", response_model=list[EmissionCategory])
async def get_emission_categories():
    """
    Get all unique emission categories available for matching.
    Useful for frontend dropdowns or type-ahead fields.
    """
    try:
        categories = category_matcher.fetch_emission_categories()
        return categories
    except Exception as e:
        logger.error(f"Failed to fetch emission categories: {e}")
        raise HTTPException(status_code=500, detail=str(e))
