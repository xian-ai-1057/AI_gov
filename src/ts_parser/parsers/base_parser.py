"""Common parser interface derived from arkkb's ts_parser source."""

from __future__ import annotations

from abc import ABC, abstractmethod
from io import BytesIO
from pathlib import Path
from typing import Any


class ParserError(RuntimeError):
    """Base exception for safe parser failures."""


class ParserUnavailableError(ParserError):
    """The requested optional parser cannot run in this environment."""


class UnsupportedFileError(ParserError):
    """The requested format has no enabled parser."""


class BaseParser(ABC):
    @abstractmethod
    def parse(
        self,
        file_bytes: BytesIO,
        filename: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    @staticmethod
    def extension(filename: str) -> str:
        return Path(filename).suffix.casefold()

