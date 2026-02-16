
import logging
from dataclasses import dataclass
from rapidfuzz import fuzz, process
from app.core.database import get_connection

logger = logging.getLogger(__name__)


@dataclass
class CategoryMatch:
    emission_category_name: str
    category_id: int
    category_name: str
    scope: str | None
    denominator_unit: str | None
    confidence: float  # 0-100


def fetch_emission_categories() -> list[dict]:
    """Fetch all distinct emission category names with their parent category info from DB."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT
                    ef.emission_category_name,
                    ef.denominator_unit,
                    c.category_id,
                    c.category_name,
                    c.scope
                FROM emission_factors ef
                JOIN category c ON c.category_id = ef.category_id
                ORDER BY c.category_id, ef.emission_category_name
            """)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def match_category(activity_description: str | None) -> CategoryMatch | None:
    """
    Fuzzy-match an extracted activity description against known emission categories.
    Queries the DB fresh every time — always up-to-date, no cache to manage.
    """
    if not activity_description:
        return None

    categories = fetch_emission_categories()
    if not categories:
        logger.warning("No emission categories found in DB for matching")
        return None

    # Build the list of names to match against
    choices = [cat["emission_category_name"] for cat in categories]

    # Fuzzy match using token_set_ratio (handles word order + partial matches well)
    result = process.extractOne(
        activity_description,
        choices,
        scorer=fuzz.token_set_ratio,
        score_cutoff=40,  # Minimum threshold to even consider
    )

    if not result:
        logger.info(f"No fuzzy match found for: '{activity_description}'")
        return None

    matched_name, score, idx = result
    cat_info = categories[idx]

    logger.info(
        f"Matched '{activity_description}' → '{matched_name}' "
        f"(confidence: {score}%, category: {cat_info['category_name']})"
    )

    return CategoryMatch(
        emission_category_name=matched_name,
        category_id=cat_info["category_id"],
        category_name=cat_info["category_name"],
        scope=cat_info["scope"],
        denominator_unit=cat_info["denominator_unit"],
        confidence=round(score, 1),
    )
