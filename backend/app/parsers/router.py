"""File Router — MIME + extension based dispatch to the correct L2 parser.

Mirrors the "MIME detection -> magic byte inspection -> File Router ->
Agent Dispatch" flow in the architecture diagram. Unsupported types are
still classified (for the file-type matrix) but return a stub
ParsedDocument with a warning rather than crashing the pipeline, so a
mixed-format upload always completes.
"""
import logging
import mimetypes
from pathlib import Path

from app.models.schemas import FileCategory, ParsedDocument
from app.parsers.base import BaseParser
from app.parsers.code_parser import CodeParser
from app.parsers.csv_parser import CSVParser
from app.parsers.database_parser import DatabaseParser
from app.parsers.email_parser import EmailParser
from app.parsers.excel_parser import ExcelParser
from app.parsers.image_parser import ImageParser
from app.parsers.json_parser import JSONParser
from app.parsers.media_parser import MediaParser
from app.parsers.office_parser import OfficeParser
from app.parsers.pdf_parser import PDFParser
from app.parsers.web_parser import WebParser

logger = logging.getLogger(__name__)

_EXT_MAP: dict[str, FileCategory] = {
    ".pdf": FileCategory.PDF,
    ".jpg": FileCategory.IMAGE, ".jpeg": FileCategory.IMAGE, ".png": FileCategory.IMAGE,
    ".tif": FileCategory.IMAGE, ".tiff": FileCategory.IMAGE, ".bmp": FileCategory.IMAGE,
    ".webp": FileCategory.IMAGE, ".heic": FileCategory.IMAGE,
    ".csv": FileCategory.CSV, ".tsv": FileCategory.CSV,
    ".json": FileCategory.JSON_, ".jsonl": FileCategory.JSON_,
    ".xlsx": FileCategory.EXCEL, ".xls": FileCategory.EXCEL, ".ods": FileCategory.EXCEL,
    ".docx": FileCategory.OFFICE, ".pptx": FileCategory.OFFICE,
    ".doc": FileCategory.OFFICE, ".ppt": FileCategory.OFFICE,
    ".rtf": FileCategory.OFFICE, ".odt": FileCategory.OFFICE,
    ".pst": FileCategory.EMAIL, ".mbox": FileCategory.EMAIL,
    ".eml": FileCategory.EMAIL, ".msg": FileCategory.EMAIL,
    ".py": FileCategory.CODE, ".js": FileCategory.CODE, ".sql": FileCategory.CODE,
    ".log": FileCategory.CODE, ".md": FileCategory.CODE, ".yaml": FileCategory.CODE,
    ".yml": FileCategory.CODE, ".toml": FileCategory.CODE, ".txt": FileCategory.CODE,
    ".zip": FileCategory.ARCHIVE, ".tar": FileCategory.ARCHIVE, ".gz": FileCategory.ARCHIVE,
    ".tgz": FileCategory.ARCHIVE, ".bz2": FileCategory.ARCHIVE, ".tbz2": FileCategory.ARCHIVE,
    ".xz": FileCategory.ARCHIVE, ".txz": FileCategory.ARCHIVE,
    ".7z": FileCategory.ARCHIVE, ".rar": FileCategory.ARCHIVE,
    ".db": FileCategory.DATABASE, ".sqlite": FileCategory.DATABASE, ".sqlite3": FileCategory.DATABASE,
    ".mdb": FileCategory.DATABASE, ".accdb": FileCategory.DATABASE,
    ".mp3": FileCategory.MEDIA, ".wav": FileCategory.MEDIA, ".mp4": FileCategory.MEDIA,
    ".m4a": FileCategory.MEDIA, ".mov": FileCategory.MEDIA, ".flac": FileCategory.MEDIA,
    ".html": FileCategory.WEB, ".htm": FileCategory.WEB, ".xml": FileCategory.WEB,
    ".geojson": FileCategory.WEB,
}


def classify(file_path: str) -> FileCategory:
    ext = Path(file_path).suffix.lower()
    if ext in _EXT_MAP:
        return _EXT_MAP[ext]
    guessed, _ = mimetypes.guess_type(file_path)
    if guessed:
        if guessed.startswith("image/"):
            return FileCategory.IMAGE
        if guessed == "application/pdf":
            return FileCategory.PDF
        if guessed.startswith("text/"):
            return FileCategory.CODE
    return FileCategory.UNKNOWN


_PARSERS: dict[FileCategory, BaseParser] = {}


def _get_parser(category: FileCategory) -> BaseParser | None:
    if category not in _PARSERS:
        if category == FileCategory.PDF:
            _PARSERS[category] = PDFParser()
        elif category == FileCategory.IMAGE:
            _PARSERS[category] = ImageParser()
        elif category == FileCategory.CSV:
            _PARSERS[category] = CSVParser()
        elif category == FileCategory.EXCEL:
            _PARSERS[category] = ExcelParser()
        elif category == FileCategory.JSON_:
            _PARSERS[category] = JSONParser()
        elif category == FileCategory.OFFICE:
            _PARSERS[category] = OfficeParser()
        elif category == FileCategory.DATABASE:
            _PARSERS[category] = DatabaseParser()
        elif category == FileCategory.EMAIL:
            _PARSERS[category] = EmailParser()
        elif category == FileCategory.CODE:
            _PARSERS[category] = CodeParser()
        elif category == FileCategory.MEDIA:
            _PARSERS[category] = MediaParser()
        elif category == FileCategory.WEB:
            _PARSERS[category] = WebParser()
        else:
            return None
    return _PARSERS[category]


def warm_parser(category: FileCategory) -> BaseParser | None:
    """Eagerly constructs and caches the parser for `category`, so a
    heavyweight first-use cost (e.g. Docling loading its layout model) is
    paid once at startup with a clear log line, not silently on whichever
    request happens to be first. Returns the same cached instance
    `parse_file` will use, or None for a category with no parser."""
    return _get_parser(category)


async def parse_file(file_path: str) -> ParsedDocument:
    category = classify(file_path)
    parser = _get_parser(category)
    if parser is None:
        logger.info("No specialist parser implemented yet for %s (%s) — stubbed.", file_path, category)
        return ParsedDocument(
            source_file=file_path,
            category=category,
            warnings=[f"No parser implemented for file type '{category.value}' in this build. "
                      f"File was classified and routed but not extracted."],
        )
    return await parser.parse(file_path)
