"""Optional Melody adapter with explicit endpoint injection and no persistent scan logs."""

from __future__ import annotations

from io import BytesIO
from typing import Any

from ts_parser.parsers.base_parser import BaseParser, ParserUnavailableError


class MelodyParser(BaseParser):
    def __init__(self, *, model_host: str | None, timeout: float, max_pages: int) -> None:
        if not model_host:
            raise ParserUnavailableError("Melody parser 缺少核准的 model_host")
        try:
            from melody import Melody
        except ImportError as exc:
            raise ParserUnavailableError("Melody 套件尚未安裝") from exc
        try:
            self._client = Melody(model_host=model_host, timeout=timeout)
        except TypeError as exc:
            raise ParserUnavailableError("目前 Melody 版本不支援安全的顯式端點注入") from exc
        self._max_pages = max_pages

    def parse(self, file_bytes: BytesIO, filename: str, **kwargs: Any) -> list[dict[str, Any]]:
        ext = self.extension(filename)
        scan = self._client.scanImage if ext in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"} else self._client.scanFile
        result = scan(
            file_bytes,
            input_type="bIO",
            src_name=filename,
            log_to=None,
            max_pages=self._max_pages,
        )
        rows = result.get("results") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Melody 回傳格式不符合 results[] 契約")
        return [
            {"content": str(row.get("content") or ""), "page": str(row.get("page") or index)}
            for index, row in enumerate(rows, start=1)
            if isinstance(row, dict)
        ]

