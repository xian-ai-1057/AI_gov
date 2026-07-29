"""Vendored ts_parser source interface.

The package is intentionally dependency-light at import time. Optional VLM and
mail dependencies are loaded only when the selected format needs them.
"""

from ts_parser.factory import ParserFactory
from ts_parser.models import ContentChunk, ParsedFileResponse

__all__ = ["ContentChunk", "ParsedFileResponse", "ParserFactory"]

