
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from app.services import ocr, llm, validators, fallback, category_matcher
from app.services.category_matcher import fetch_emission_categories_by_site_and_category
from app.schemas.invoice import ExtractionResponse, CategorySuggestion, EmissionReady

logger = logging.getLogger(__name__)

# Create a threadpool for blocking OCR tasks
executor = ThreadPoolExecutor(max_workers=4)

async def process_document(
    file_path: Path,
    filename: str,
    site_id: int | None = None,
    category_id: int | None = None,
    available_units: list[str] | None = None,
) -> ExtractionResponse:
    """
    Orchestrates the extraction pipeline:
    1. Fetch scoped emission categories (if site+category provided)
    2. OCR → 3. LLM (with known categories injected) → 4. Validate each
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
    if site_id is not None and category_id is not None:
        try:
            loop = asyncio.get_event_loop()
            scoped_categories = await loop.run_in_executor(
                executor,
                fetch_emission_categories_by_site_and_category,
                site_id,
                category_id,
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
        except Exception as e:
            logger.warning(f"Could not fetch scoped emission categories: {e}. Proceeding without.")

    try:
        # 2. OCR (Run in threadpool to define non-blocking behavior)
        loop = asyncio.get_event_loop()
        extracted_text = await loop.run_in_executor(executor, ocr.extract_text, file_path)
        logger.debug(f"OCR completed for {filename}. Text length: {len(extracted_text)}")

        # 3. LLM Extraction — inject known category names and units so the LLM
        #    maps both emission_category and unit_of_measurement to the configured values.
        try:
            invoices = await loop.run_in_executor(
                executor,
                llm.extract_structured_data,
                extracted_text,
                known_category_names or None,
                available_units,
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
                elif not suggestion and activity.activity_description:
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
                #   1. LLM-identified value
                #   2. Fuzzy-matcher suggestion
                #   3. Raw activity_description as last resort
                final_emission_category = (
                    activity.emission_category
                    or (suggestion.emission_category_name if suggestion else None)
                    or (activity.activity_description or "")
                )

                final_category_id = category_id if category_id is not None else (suggestion.category_id if suggestion else None)

                all_emission_ready.append(EmissionReady(
                    invoice_index=inv_idx,
                    activity_index=act_idx,
                    site_id=site_id,
                    category_id=final_category_id,
                    activity_data={
                        "Activity Data": str(activity.total_quantity) if activity.total_quantity is not None else "",
                        "emission_category": final_emission_category,
                    },
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
