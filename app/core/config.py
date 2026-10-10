
import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # OpenRouter
    OPENROUTER_API_KEY: str
    OPENROUTER_MODEL: str = "google/gemini-3-flash-preview"
    OPENROUTER_REFERER: str = "https://esg-lite.app"
    OPENROUTER_TITLE: str = "ESG-Lite OCR"

    # Auth. AUTH_JWT_SECRET must equal ESG-lite's JWT_SECRET: browser calls
    # carry the ESG-lite sign-in token. Required, so the service refuses to
    # start without it. AI_SERVICE_KEY is the shared key ESG-lite sends as
    # X-Service-Key on its server-to-server calls; empty disables those calls.
    AUTH_JWT_SECRET: str
    AI_SERVICE_KEY: str = ""
    
    # Browser origins allowed to call this service (CORS), comma-separated,
    # e.g. "https://app.esglite.com,https://staging.esglite.com".
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    @property
    def cors_allowed_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    # Database
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USERNAME: str = "postgres"
    # No default secret: set DB_PASSWORD in the environment. Empty only works
    # for a passwordless local Postgres; otherwise the first query fails with
    # a message naming DB_PASSWORD (app/core/database.py).
    DB_PASSWORD: str = ""
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
    MAX_PAGES: int = 12
    MIN_PDF_TEXT_CHARS: int = 200
    PDF_RENDER_SCALE: float = 2.0
    LLM_TIMEOUT_S: float = 90.0
    MAX_RETRIES: int = 2
    MAX_CONCURRENT_REQUESTS: int = 5
    MAX_CONCURRENT_LLM: int = 3
    # Whole-request budget for column-name inference, slot wait included. ESG-lite
    # gives up on these calls after AI_SERVICE_TIMEOUT_MS (30 s by default).
    COLUMN_INFER_BUDGET_S: float = 25.0

settings = Settings()

# Ensure directories exist
os.makedirs(settings.TEMP_DIR, exist_ok=True)
