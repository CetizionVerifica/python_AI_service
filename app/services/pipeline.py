
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from app.services import ocr, llm, validators, fallback, category_matcher
from app.services.category_matcher import fetch_emission_categories_by_site_and_category
from app.core.database import fetch_column_config
from app.schemas.invoice import ExtractionResponse, CategorySuggestion, EmissionReady

logger = logging.getLogger(__name__)

# Create a threadpool for blocking OCR tasks
executor = ThreadPoolExecutor(max_workers=4)


def _resolve_emission_category_from_mapping(
    column_values: dict[str, str] | None,
    column_config: dict | None,
) -> str | None:
    """
    Use column_config's emission_category_mapping to determine the emission
    category from the LLM-extracted dropdown values.

    The mapping keys are in the format "ParentLabel|ChildLabel" (e.g.,
    "Process Organic Waste|Incineration" -> "Process Organic Waste - Incineration").
    """
    if not column_values or not column_config:
        return None

    mapping = column_config.get("emission_category_mapping", {})
    if not mapping:
        return None

    dependencies = column_config.get("column_dependencies", {})

    if not dependencies:
        # No dependencies — try single-value lookup
        for val in column_values.values():
            if val in mapping:
                return mapping[val]
        return None

    # Build the mapping key from parent|child label pairs.
    # Walk the dependency tree to find terminal children and their parents.
    all_children = set(dependencies.keys())
    all_parents = set(dependencies.values())
    terminal_children = [c for c in sorted(all_children) if c not in all_parents]

    if not terminal_children:
        terminal_children = sorted(all_children)

    key_parts: list[str] = []
    for child_col in terminal_children:
        parent_col = dependencies.get(child_col)
        if not parent_col:
            continue
        parent_val = column_values.get(parent_col)
        child_val = column_values.get(child_col)
        if parent_val and child_val:
            key_parts.append(parent_val)
            key_parts.append(child_val)

    if not key_parts:
        return None

    mapping_key = "|".join(key_parts)

    # Try exact match first
    if mapping_key in mapping:
        return mapping[mapping_key]

    # Try case-insensitive match
    mapping_key_lower = mapping_key.lower()
    for key, value in mapping.items():
        if key.lower() == mapping_key_lower:
            return value

    return None


async def process_document(
    file_path: Path,
    filename: str,
    site_id: int | None = None,
    category_id: int | None = None,
    available_units: list[str] | None = None,
) -> ExtractionResponse:
    """
    Orchestrates the extraction pipeline:
    1. Fetch scoped emission categories + column config (if site+category provided)
    2. OCR → 3. LLM (with known categories + dropdown fields injected) → 4. Validate each
    5. Fuzzy match each (scoped) → 6. Build emission-ready payload → 7. Cleanup
    """
    logger.info(f"Starting pipeline for document: {filename}")

    extracted_text = ""
    invoices = []
    all_validations = []
    all_suggestions = []
    all_emission_ready = []
    error_message = None

    # 1. Fetch scoped emission categories when site+category context is available.
    #    These are the same names the Node.js API returns for this combination,
    #    so the LLM and fuzzy-matcher both work from the same constrained list.
    known_category_names: list[str] = []
    known_units: list[str] = []
    column_config: dict | None = None

    if site_id is not None and category_id is not None:
        loop = asyncio.get_event_loop()

        # Fetch emission categories and column_config concurrently
        try:
            scoped_categories, column_config = await asyncio.gather(
                loop.run_in_executor(
                    executor,
                    fetch_emission_categories_by_site_and_category,
                    site_id,
                    category_id,
                ),
                loop.run_in_executor(
                    executor,
                    fetch_column_config,
                    site_id,
                    category_id,
                ),
            )

            known_category_names = [c["emission_category_name"] for c in scoped_categories]
            known_units = list({
                c["denominator_unit"] for c in scoped_categories if c["denominator_unit"]
            })
            logger.info(
                f"Fetched {len(known_category_names)} scoped emission categories for "
                f"site_id={site_id}, category_id={category_id}: {known_category_names}"
            )
            logger.info(f"Known units for LLM prompt: {known_units}")
            if column_config:
                select_cols = [
                    c["column_name"] for c in column_config.get("columns", [])
                    if c["column_type"] == "select"
                ]
                logger.info(
                    f"Column config found for site_id={site_id}, category_id={category_id}: "
                    f"select columns = {select_cols}"
                )
            else:
                logger.info(
                    f"No column config found for site_id={site_id}, category_id={category_id}"
                )
        except Exception as e:
            logger.warning(f"Could not fetch scoped data: {e}. Proceeding without.")

    try:
        # 2. OCR (Run in threadpool to define non-blocking behavior)
        loop = asyncio.get_event_loop()
        extracted_text = await loop.run_in_executor(executor, ocr.extract_text, file_path)
        logger.debug(f"OCR completed for {filename}. Text length: {len(extracted_text)}")

        # 3. LLM Extraction — inject known category names, units, and column config
        #    so the LLM maps emission_category, unit_of_measurement, and dropdown fields.
        try:
            invoices = await loop.run_in_executor(
                executor,
                llm.extract_structured_data,
                extracted_text,
                known_category_names or None,
                available_units,
                column_config,
            )
            logger.info(f"LLM extracted {len(invoices)} invoice(s) from {filename}.")

            # 4. Validate each invoice
            for inv in invoices:
                all_validations.append(validators.validate_totals(inv))

        except Exception as e:
            logger.error(f"LLM Extraction failed for {filename}: {e}")

            error_str = str(e)
            if "API key not valid" in error_str or "API_KEY_INVALID" in error_str:
                error_message = "LLM Service Unavailable: Invalid API Key. Using Regex Fallback."
            else:
                error_message = f"LLM Extraction Failed: {error_str[:200]}"

            # Fallback — returns single invoice
            fallback_data = await loop.run_in_executor(executor, fallback.extract_fallback_data, extracted_text)
            invoices = [fallback_data]
            all_validations = [[]]

        # 5. Fuzzy match each activity + build emission-ready payload
        for inv_idx, inv in enumerate(invoices):
            if not inv or not inv.activities:
                # Invoice with no activities — still add a placeholder emission
                all_suggestions.append(None)
                all_emission_ready.append(EmissionReady(
                    invoice_index=inv_idx,
                    activity_index=0,
                    site_id=site_id,
                    category_id=category_id,
                    activity_data={"Activity Data": "", "emission_category": ""},
                    date_of_reporting=inv.invoice_date if inv else None,
                    vendor_name=inv.vendor_name if inv else None,
                ))
                continue

            for act_idx, activity in enumerate(inv.activities):
                suggestion = None

                # Try to resolve emission_category from column_values + mapping first
                mapped_emission_category = _resolve_emission_category_from_mapping(
                    activity.column_values, column_config,
                )

                if activity.activity_description:
                    match = category_matcher.match_category(
                        activity.activity_description,
                        site_id=site_id,
                        category_id=category_id,
                    )
                    if match:
                        suggestion = CategorySuggestion(
                            emission_category_name=match.emission_category_name,
                            category_id=match.category_id,
                            category_name=match.category_name,
                            scope=match.scope,
                            denominator_unit=match.denominator_unit,
                            confidence=match.confidence,
                        )

                # Validation warnings — append to this invoice's validation list
                if suggestion and not suggestion.denominator_unit:
                    unit_warning = {
                        "check": "activity_unit_defined",
                        "ok": False,
                        "message": (
                            f"No activity unit defined for emission category "
                            f"'{suggestion.emission_category_name}'"
                            + (f" (site_id={site_id}, category_id={category_id})" if site_id and category_id else "")
                            + ". Please configure denominator_unit in emission_factors."
                        ),
                        "emission_category": suggestion.emission_category_name,
                    }
                    if inv_idx < len(all_validations):
                        all_validations[inv_idx].append(unit_warning)
                    else:
                        all_validations.append([unit_warning])
                    logger.warning(unit_warning["message"])
                elif not suggestion and not mapped_emission_category and activity.activity_description:
                    no_match_warning = {
                        "check": "activity_unit_defined",
                        "ok": False,
                        "message": (
                            f"Could not match activity '{activity.activity_description}' to any emission category"
                            + (f" for site_id={site_id}, category_id={category_id}" if site_id and category_id else "")
                            + ". Activity unit unknown."
                        ),
                        "emission_category": None,
                    }
                    if inv_idx < len(all_validations):
                        all_validations[inv_idx].append(no_match_warning)
                    else:
                        all_validations.append([no_match_warning])
                    logger.warning(no_match_warning["message"])

                all_suggestions.append(suggestion)

                # Priority chain for emission category:
                #   1. Mapping from column_values (most precise for categories with dropdowns)
                #   2. LLM-identified value
                #   3. Fuzzy-matcher suggestion
                #   4. Raw activity_description as last resort
                final_emission_category = (
                    mapped_emission_category
                    or activity.emission_category
                    or (suggestion.emission_category_name if suggestion else None)
                    or (activity.activity_description or "")
                )

                final_category_id = category_id if category_id is not None else (suggestion.category_id if suggestion else None)

                # Build activity_data dict with column values included
                activity_data: dict[str, str] = {
                    "Activity Data": str(activity.total_quantity) if activity.total_quantity is not None else "",
                    "emission_category": final_emission_category,
                    "description": activity.activity_description or "",
                }

                # Merge LLM-extracted dropdown field values into activity_data
                # so the frontend can pre-populate the dropdown columns.
                if activity.column_values:
                    for col_name, col_value in activity.column_values.items():
                        if col_value:
                            activity_data[col_name] = col_value
                    if mapped_emission_category:
                        logger.info(
                            f"Mapped column_values {activity.column_values} → "
                            f"emission_category '{mapped_emission_category}'"
                        )

                all_emission_ready.append(EmissionReady(
                    invoice_index=inv_idx,
                    activity_index=act_idx,
                    site_id=site_id,
                    category_id=final_category_id,
                    activity_data=activity_data,
                    activity_data_unit=activity.unit_of_measurement,
                    date_of_reporting=inv.invoice_date,
                    total_emission=0.0,
                    unit="kg CO2e",
                    vendor_name=inv.vendor_name,
                ))

        return ExtractionResponse(
            filename=filename,
            data=invoices,
            error=error_message,
            validations=all_validations,
            suggested_categories=all_suggestions,
            emission=all_emission_ready,
        )

    finally:
        logger.info(f"Pipeline finished for {filename}.")
