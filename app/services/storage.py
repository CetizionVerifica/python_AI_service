
import shutil
import uuid
import os
import logging
from pathlib import Path
from fastapi import UploadFile
from app.core.config import settings

logger = logging.getLogger(__name__)

async def save_upload(file: UploadFile) -> Path:
    """
    Saves the uploaded file to the temporary directory with a unique name.
    """
    # Generate unique filename to prevent collisions
    file_id = str(uuid.uuid4())
    extension = os.path.splitext(file.filename)[1] if file.filename else ""
    filename = f"{file_id}{extension}"
    file_path = Path(settings.TEMP_DIR) / filename

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logger.debug(f"File upload saved to {file_path}")
    finally:
        file.file.close()

    return file_path

def cleanup(file_path: Path):
    """
    Removes the file from the filesystem.
    """
    try:
        if file_path.exists():
            os.remove(file_path)
            logger.debug(f"Cleaned up temporary file: {file_path}")
    except Exception as e:
        logger.warning(f"Error cleaning up file {file_path}: {e}")
