
import logging
import cloudinary
import cloudinary.uploader
from pathlib import Path
from app.core.config import settings

logger = logging.getLogger(__name__)

# Configure Cloudinary
cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
)


def is_configured() -> bool:
    """
    True when credentials are present. Callers use this to fall back to local
    storage (dev machines, or a deploy with the vars unset) instead of failing
    the request outright.
    """
    return bool(
        settings.CLOUDINARY_CLOUD_NAME
        and settings.CLOUDINARY_API_KEY
        and settings.CLOUDINARY_API_SECRET
    )


def upload_file(file_path: Path, folder: str = "invoices") -> dict:
    """
    Upload a file to Cloudinary and return the result.
    Returns dict with: public_id, secure_url, url, resource_type, bytes, format
    """
    try:
        result = cloudinary.uploader.upload_large(
            str(file_path),
            folder=folder,
            resource_type="raw",
            type="upload",
            chunk_size=6_000_000,
        )
        logger.info(f"Uploaded to Cloudinary: {result.get('public_id')}")
        return {
            "public_id": result["public_id"],
            "secure_url": result["secure_url"],
            "url": result["url"],
            "bytes": result.get("bytes"),
            "format": result.get("format"),
        }
    except Exception as e:
        logger.error(f"Cloudinary upload failed: {e}")
        raise


def delete_file(public_id: str) -> bool:
    """
    Delete a file from Cloudinary by its public_id.
    Returns True if successful.
    """
    try:
        result = cloudinary.uploader.destroy(public_id, resource_type="raw")
        if result.get("result") != "ok":
            # Try with image resource_type as fallback
            result = cloudinary.uploader.destroy(public_id, resource_type="image")
        logger.info(f"Deleted from Cloudinary: {public_id} -> {result}")
        return result.get("result") == "ok"
    except Exception as e:
        logger.error(f"Cloudinary delete failed for {public_id}: {e}")
        return False
