
from fastapi import FastAPI
from app.api.invoices import router as api_router
from app.api.categories import router as categories_router
from app.core.logging import setup_logging

# Setup Logging
setup_logging()

app = FastAPI(
    title="OCR Invoice Extraction API",
    description="Extract structured data from Invoices and Bills using Hybrid PDF/OCR + OpenRouter LLM.",
    version="0.1.0"
)

app.include_router(api_router, prefix="/v1")
app.include_router(categories_router, prefix="/v1")

@app.get("/health")
def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
