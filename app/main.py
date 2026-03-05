
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.invoices import router as api_router
from app.api.categories import router as categories_router
from app.api.emission_factors import router as emission_factors_router
from app.api.category_mapping import router as category_mapping_router
from app.api.column_config import router as column_config_router
from app.core.logging import setup_logging
from app.core.database import ensure_emission_factor_uploads_table

# Setup Logging
setup_logging()

# Ensure tables exist
try:
    ensure_emission_factor_uploads_table()
except Exception:
    pass  # logged inside the function

app = FastAPI(
    title="OCR Invoice Extraction API",
    description="Extract structured data from Invoices and Bills using Hybrid PDF/OCR + OpenRouter LLM.",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/v1")
app.include_router(categories_router, prefix="/v1")
app.include_router(emission_factors_router, prefix="/v1")
app.include_router(category_mapping_router, prefix="/v1")
app.include_router(column_config_router, prefix="/v1")

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/")
def read_root():
    return {
        "project": "OCR Invoice Extraction API",
        "version": "0.1.0",
        "description": "Extract structured data from Invoices and Bills using Hybrid PDF/OCR + OpenRouter LLM.",
        "documentation": "/docs"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
