"""Office Document Agent (L2, "Document" specialist alongside PDF) --
DOCX/PPTX via python-docx/python-pptx (pure-Python, no heavy deps), plus
legacy binary DOC/PPT (pre-2007 OLE2 format) via a headless LibreOffice
conversion to the modern XML format first. There is no reliable
pure-Python reader for the old binary format, and a text-only extractor
(antiword/catdoc) would silently drop every table -- converting first
lets the legacy path reuse the exact same _parse_docx/_parse_pptx logic
below, tables and all.
"""
import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import docx
from pptx import Presentation

from app.models.schemas import FileCategory, ParsedDocument, TableBlock, TextBlock
from app.parsers.base import BaseParser

logger = logging.getLogger(__name__)

LIBREOFFICE_TIMEOUT_S = 120


class OfficeParser(BaseParser):
    category = FileCategory.OFFICE

    async def parse(self, file_path: str) -> ParsedDocument:
        return await asyncio.to_thread(self._parse_sync, file_path)

    def _parse_sync(self, file_path: str) -> ParsedDocument:
        doc = ParsedDocument(source_file=file_path, category=self.category)
        lower = file_path.lower()
        tmp_dir: Path | None = None
        try:
            if lower.endswith(".ppt"):
                converted, tmp_dir = self._convert_legacy(file_path, "pptx")
                self._parse_pptx(converted, doc)
            elif lower.endswith(".doc"):
                converted, tmp_dir = self._convert_legacy(file_path, "docx")
                self._parse_docx(converted, doc)
            elif lower.endswith(".pptx"):
                self._parse_pptx(file_path, doc)
            else:
                self._parse_docx(file_path, doc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Office parse failed on %s", file_path)
            doc.warnings.append(f"Office document parse error: {exc}")
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        return doc

    def _convert_legacy(self, file_path: str, target_ext: str) -> tuple[str, Path]:
        """Converts a legacy .doc/.ppt file to .docx/.pptx via headless
        LibreOffice. Returns the converted file's path and the temp output
        directory it lives in (caller's responsibility to clean up).

        A unique -env:UserInstallation profile dir is required per call --
        soffice's default profile is a single shared, lock-file-guarded
        directory, so two conversions running concurrently (this pipeline
        processes up to MAX_PARALLEL_FILES files at once) would otherwise
        serialize on that lock or fail outright with "soffice already
        running; exiting."."""
        outdir = Path(tempfile.mkdtemp(prefix="office_convert_out_"))
        profile_dir = Path(tempfile.mkdtemp(prefix="office_convert_profile_"))
        try:
            result = subprocess.run(
                [
                    "soffice", "--headless", "--norestore",
                    f"-env:UserInstallation=file://{profile_dir}",
                    "--convert-to", target_ext, "--outdir", str(outdir), file_path,
                ],
                capture_output=True, text=True, timeout=LIBREOFFICE_TIMEOUT_S,
            )
            converted = outdir / f"{Path(file_path).stem}.{target_ext}"
            if result.returncode != 0 or not converted.exists():
                raise RuntimeError(
                    "LibreOffice conversion to ." + target_ext + " failed: "
                    + (result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}")
                )
            return str(converted), outdir
        except Exception:
            shutil.rmtree(outdir, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)

    def _parse_docx(self, file_path: str, doc: ParsedDocument) -> None:
        d = docx.Document(file_path)
        for para in d.paragraphs:
            if not para.text.strip():
                continue
            kind = "heading" if para.style and para.style.name and para.style.name.startswith("Heading") else "paragraph"
            doc.text_blocks.append(TextBlock(text=para.text, kind=kind))

        for table in d.tables:
            rows = [[cell.text for cell in row.cells] for row in table.rows]
            headers = rows[0] if rows else []
            doc.tables.append(TableBlock(headers=headers, rows=rows[1:]))

        doc.metadata = {
            "paragraph_count": len(d.paragraphs),
            "table_count": len(d.tables),
            "parser": "python-docx",
        }
        if not doc.text_blocks and not doc.tables:
            doc.warnings.append("No extractable text or tables found in document.")

    def _parse_pptx(self, file_path: str, doc: ParsedDocument) -> None:
        prs = Presentation(file_path)
        for i, slide in enumerate(prs.slides, start=1):
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    doc.text_blocks.append(
                        TextBlock(text=shape.text_frame.text, page=i, kind="paragraph")
                    )
                if shape.has_table:
                    rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
                    headers = rows[0] if rows else []
                    doc.tables.append(TableBlock(page=i, headers=headers, rows=rows[1:]))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                doc.text_blocks.append(
                    TextBlock(text=slide.notes_slide.notes_text_frame.text, page=i, kind="notes")
                )

        doc.metadata = {"slide_count": len(prs.slides), "parser": "python-pptx"}
        if not doc.text_blocks and not doc.tables:
            doc.warnings.append("No extractable text or tables found in presentation.")
