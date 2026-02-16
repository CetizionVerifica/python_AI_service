
import logging
import sys
from app.core.config import settings

def setup_logging():
    """
    Configures the root logger to output to stdout with a standard format.
    """
    log_level = logging.INFO
    # You could add DEBUG level via env var in settings if needed, 
    # but for now we default to INFO.

    # Format: "2023-10-27 10:00:00 [INFO] app.services.ocr: Message"
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    # Configure Root Logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # Remove existing handlers to prevent duplicates (e.g. from uvicorn)
    # root_logger.handlers = [] 
    # Use careful handler management to play nice with uvicorn.
    
    # Actually, Uvicorn handles its own logging. We want to configure the 'app' logger.
    app_logger = logging.getLogger("app")
    app_logger.setLevel(log_level)
    app_logger.addHandler(handler)
    app_logger.propagate = False # Prevent double logging if root has handlers

    # Also configure third-party loggers if they are too chatty
    logging.getLogger("pypdf").setLevel(logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.WARNING)
    logging.getLogger("google.ai").setLevel(logging.WARNING)

    return app_logger
