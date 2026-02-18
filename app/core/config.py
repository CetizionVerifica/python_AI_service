
import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # OpenRouter
    OPENROUTER_API_KEY: str
    OPENROUTER_MODEL: str = "google/gemini-3-flash-preview"
    OPENROUTER_REFERER: str = "https://esg-lite.app"
    OPENROUTER_TITLE: str = "ESG-Lite OCR"
    
    # Database
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USERNAME: str = "postgres"
    DB_PASSWORD: str = "postgres123"
    DB_NAME: str = "emissions_db"

    # Cloudinary
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # Project Paths
    BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    TEMP_DIR: str = os.path.join(BASE_DIR, "temp")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore"
    )

    # App Settings
    MAX_FILE_MB: int = 20
    MAX_PAGES: int = 10
    MIN_PDF_TEXT_CHARS: int = 200
    PDF_RENDER_SCALE: float = 2.0
    LLM_TIMEOUT_S: float = 90.0
    MAX_RETRIES: int = 2
    MAX_CONCURRENT_REQUESTS: int = 4
    MAX_CONCURRENT_LLM: int = 2

settings = Settings()

# Ensure temp directory exists
os.makedirs(settings.TEMP_DIR, exist_ok=True)
