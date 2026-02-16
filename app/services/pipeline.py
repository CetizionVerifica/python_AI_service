
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from app.services import ocr, llm, storage, validators, fallback, category_matcher
from app.schemas.invoice import ExtractionResponse, CategorySuggestion, EmissionReady

logger = logging.getLogger(__name__)

# Create a threadpool for blocking OCR tasks
executor = ThreadPoolExecutor(max_workers=4)

async def process_document(
    file_path: Path, 
    filename: str, 
    site_id: int | None = None,
    category_id: int | None = None
) -> ExtractionResponse:
    """
    Orchestrates the extraction pipeline:
    1. OCR → 2. LLM → 3. Validate each → 4. Fuzzy match each → 5. Cleanup
    """
    logger.info(f"Starting pipeline for document: {filename}")
    
    extracted_text = ""
    invoices = []
    all_validations = []
    all_suggestions = []
    all_emission_ready = []
    error_message = None

    try:
        # 1. OCR (Run in threadpool to define non-blocking behavior)
        loop = asyncio.get_event_loop()
        extracted_text = await loop.run_in_executor(executor, ocr.extract_text, file_path)
        logger.debug(f"OCR completed for {filename}. Text length: {len(extracted_text)}")
        
        # 2. LLM Extraction
        try:
            invoices = await loop.run_in_executor(executor, llm.extract_structured_data, extracted_text)
            logger.info(f"LLM extracted {len(invoices)} invoice(s) from {filename}.")

            # 3. Validate each invoice
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

        # 4. Fuzzy match each invoice + build emission-ready payload
        for inv in invoices:
            suggestion = None
            if inv and inv.activity_description:
                match = category_matcher.match_category(inv.activity_description)
                if match:
                    suggestion = CategorySuggestion(
                        emission_category_name=match.emission_category_name,
                        category_id=match.category_id,
                        category_name=match.category_name,
                        scope=match.scope,
                        denominator_unit=match.denominator_unit,
                        confidence=match.confidence,
                    )

            all_suggestions.append(suggestion)

            # Build emission-ready payload (aligned with Node.js POST /emissions)
            # Use request category_id if provided, else suggestion
            final_category_id = category_id if category_id is not None else (suggestion.category_id if suggestion else None)
            
            all_emission_ready.append(EmissionReady(
                site_id=site_id,
                category_id=final_category_id,
                activity_data={
                    "Activity Data": str(inv.total_quantity) if inv and inv.total_quantity is not None else "",
                    "emission_category": suggestion.emission_category_name if suggestion else (inv.activity_description if inv else ""),
                },
                activity_data_unit=suggestion.denominator_unit if suggestion else (inv.unit_of_measurement if inv else None),
                date_of_reporting=inv.invoice_date if inv else None,
                total_emission=0.0,
                unit="kg CO2e"
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
        storage.cleanup(file_path)
        logger.info(f"Pipeline finished for {filename}.")
