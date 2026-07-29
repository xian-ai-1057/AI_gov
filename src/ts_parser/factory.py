"""Factory entry point compatible with the arkkb ts_parser library shape."""

from __future__ import annotations

from typing import Any

from ts_parser.parsers.default_parser import DefaultParser


class ParserFactory:
    @classmethod
    def get_parser(cls, metadata: dict[str, bool] | None = None, **settings: Any) -> DefaultParser:
        del metadata
        return DefaultParser(**settings)

