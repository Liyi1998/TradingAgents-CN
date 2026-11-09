"""Compatibility import; implementation lives in the data sync layer."""
from app.data.sync.quotes_ingestion_service import QuotesIngestionService

__all__ = ["QuotesIngestionService"]
