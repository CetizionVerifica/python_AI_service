
from pathlib import Path
from typing import List
import io
import logging
import pytesseract
import pypdfium2 as pdfium
from pypdf import PdfReader
from PIL import Image
from app.core.config import settings

logger = logging.getLogger(__name__)

def extract_text_from_pdf_digital(file_path: Path) -> str:
    """
    Extracts text from a digital PDF using pypdf.
    Returns empty string if no text found.
    """
    try:
        reader = PdfReader(str(file_path))
        chunks: List[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                chunks.append(text)
        return "\n\n".join(chunks).strip()
    except Exception as e:
        logger.warning(f"Digital PDF extraction failed for {file_path.name}: {e}")
        return ""

def is_digital_pdf(text: str) -> bool:
    """
    Simple heuristic to check if extracted text is sufficient to consider the PDF digital.
    """
    return len(text.strip()) >= settings.MIN_PDF_TEXT_CHARS

def render_pdf_to_images(file_path: Path) -> List[Image.Image]:
    """
    Renders PDF pages to PIL Images using pypdfium2.
    """
    pdf = pdfium.PdfDocument(str(file_path))
    images = []
    try:
        # Respect MAX_PAGES
        max_pages = min(settings.MAX_PAGES, len(pdf))
        for i in range(max_pages):
            page = pdf[i]
            bitmap = page.render(scale=settings.PDF_RENDER_SCALE)
            images.append(bitmap.to_pil())
    finally:
        pdf.close()
    return images

def extract_text(file_path: Path) -> str:
    """
    Extracts text from an image or PDF file.
    Strategies:
    1. Digital PDF (fastest, cheapest)
    2. OCR via Tesseract (for images & scanned PDFs)
    """
    suffix = file_path.suffix.lower()
    text_content = ""

    try:
        if suffix == ".pdf":
            # Strategy 1: Try Digital Extraction first
            digital_text = extract_text_from_pdf_digital(file_path)
            if is_digital_pdf(digital_text):
                logger.info(f"Using Digital PDF extraction for {file_path.name}")
                return digital_text
            
            # Strategy 2: Fallback to OCR (Render -> Tesseract)
            logger.info(f"PDF {file_path.name} seems scanned/low-text. Falling back to OCR.")
            images = render_pdf_to_images(file_path)
            for i, image in enumerate(images):
                page_text = pytesseract.image_to_string(image)
                text_content += f"--- Page {i+1} ---\n{page_text}\n"

        elif suffix in [".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp"]:
            logger.info(f"Performing OCR on image: {file_path.name}")
            image = Image.open(file_path)
            text_content = pytesseract.image_to_string(image)
        else:
            raise ValueError(f"Unsupported file format: {suffix}")
        
        return text_content
    except Exception as e:
        logger.error(f"OCR Extraction failed for {file_path.name}: {e}")
        raise e
