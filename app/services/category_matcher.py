
import logging
from dataclasses import dataclass
from rapidfuzz import fuzz, process
from app.core.database import get_connection, release_connection

logger = logging.getLogger(__name__)


@dataclass
class CategoryMatch:
    emission_category_name: str
    category_id: int
    category_name: str
    scope: str | None
    denominator_unit: str | None
    confidence: float  # 0-100


# Factors visible to a site: the shared library (no site) plus the factors of
# every site in the same company. Never another company's factors.
_COMPANY_SCOPE_SQL = """
    (ef.site_id IS NULL OR ef.site_id IN (
        SELECT s2.site_id FROM site s1
        JOIN site s2 ON s2.company_id = s1.company_id
        WHERE s1.site_id = %(site_id)s AND s1.company_id IS NOT NULL
    ) OR ef.site_id = %(site_id)s)
"""


def fetch_emission_categories(site_id: int | None = None, all_companies: bool = False) -> list[dict]:
    """Distinct emission category names with their parent category info.

    Scoped to the shared factors plus the factors of ``site_id``'s company
    (only the shared ones when ``site_id`` is None). ``all_companies=True``
    lists every company's factors; only Superadmin callers may ask for that.
    """
    where = "" if all_companies else "WHERE " + _COMPANY_SCOPE_SQL
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT
                    ef.emission_category_name,
                    ef.denominator_unit,
                    c.category_id,
                    c.category_name,
                    c.scope
                FROM emission_factors ef
                JOIN category c ON c.category_id = ef.category_id
                {where}
                ORDER BY c.category_id, ef.emission_category_name
            """, {"site_id": site_id})
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        release_connection(conn)


def fetch_emission_categories_by_site_and_category(site_id: int, category_id: int) -> list[dict]:
    """
    Fetch emission category names scoped to a specific site and emission category.
    Returns only rows where ef.site_id = site_id AND ef.category_id = category_id.
    This is the same scope as the Node.js GET /user/emission-factors/site/{site_id}/category/{category_id}.
    """
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
                WHERE ef.site_id = %s AND ef.category_id = %s
                ORDER BY ef.emission_category_name
            """, (site_id, category_id))
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        release_connection(conn)


def match_category(
    activity_description: str | None,
    site_id: int | None = None,
    category_id: int | None = None,
) -> CategoryMatch | None:
    """
    Fuzzy-match an extracted activity description against known emission categories.
    When site_id and category_id are provided, matching is scoped to that combination
    (same scope as the Node.js emission-factors API). Falls back to the shared
    factors plus the site's own company's factors, never another company's.
    """
    if not activity_description:
        return None

    # Prefer scoped categories when site+category context is available
    categories: list[dict] = []
    if site_id is not None and category_id is not None:
        categories = fetch_emission_categories_by_site_and_category(site_id, category_id)
        if categories:
            logger.info(
                f"Category matching scoped to site_id={site_id}, category_id={category_id} "
                f"({len(categories)} candidates)"
            )
        else:
            logger.warning(
                f"No scoped emission categories found for site_id={site_id}, "
                f"category_id={category_id}. Falling back to the company's factors."
            )

    if not categories:
        categories = fetch_emission_categories(site_id=site_id)

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
