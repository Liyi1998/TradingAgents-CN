"""金策智算 DuckDB HTTP 数据源。"""

from .adapter import JinceDuckDBCNAdapter
from .provider import JinceDuckDBCNProvider

__all__ = ["JinceDuckDBCNAdapter", "JinceDuckDBCNProvider"]
