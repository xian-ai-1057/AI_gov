"""Format router with explicit strategy and lazy optional dependencies."""

from __future__ import annotations

from io import BytesIO
from typing import Any, Literal

from ts_parser.parsers.base_parser import BaseParser, UnsupportedFileError
from ts_parser.parsers.melody_parser import MelodyParser
from ts_parser.parsers.native import DocxParser, MsgParser, PdfParser, SpreadsheetParser, TextParser

WordStrategy = Literal["native", "melody", "auto"]


class DefaultParser(BaseParser):
    def __init__(
        self,
        *,
        word_strategy: WordStrategy = "auto",
        ocr_enabled: bool = False,
        vlm_enabled: bool = False,
        model_host: str | None = None,
        timeout: float = 60.0,
        max_pages: int = 200,
    ) -> None:
        self.word_strategy = word_strategy
        self.ocr_enabled = ocr_enabled
        self.vlm_enabled = vlm_enabled
        self.model_host = model_host
        self.timeout = timeout
        self.max_pages = max_pages

    def _melody(self, *, image: bool = False) -> MelodyParser:
        if image and not (self.ocr_enabled or self.vlm_enabled):
            raise UnsupportedFileError("此影像格式需要 OCR/VLM，但功能目前未啟用")
        if not image and not self.vlm_enabled:
            raise UnsupportedFileError("此格式需要 VLM，但 VLM 功能目前未啟用")
        return MelodyParser(
            model_host=self.model_host,
            timeout=self.timeout,
            max_pages=self.max_pages,
        )

    def parse(self, file_bytes: BytesIO, filename: str, **kwargs: Any) -> dict[str, Any]:
        ext = self.extension(filename)
        if ext in {".txt", ".csv", ".tsv", ".md"}:
            parser: BaseParser = TextParser()
        elif ext in {".xls", ".xlsx"}:
            parser = SpreadsheetParser()
        elif ext == ".docx" and self.word_strategy in {"native", "auto"}:
            parser = DocxParser()
        elif ext == ".pdf":
            parser = PdfParser(max_pages=self.max_pages)
        elif ext == ".msg":
            parser = MsgParser()
        elif ext in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}:
            parser = self._melody(image=True)
        elif ext in {".doc", ".docx", ".ppt", ".pptx"}:
            parser = self._melody()
        else:
            raise UnsupportedFileError(f"不支援的檔案類型：{ext or '(none)'}")
        return {"filename": filename, "content": parser.parse(file_bytes, filename, **kwargs)}
