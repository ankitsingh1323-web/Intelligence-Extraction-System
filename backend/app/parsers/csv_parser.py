"""CSV / TSV Agent (L2) — pandas-based ingest with schema profiling."""
import asyncio
import logging
import warnings

import pandas as pd

from app.models.schemas import FileCategory, ParsedDocument, TableBlock, TextBlock
from app.parsers.base import BaseParser

logger = logging.getLogger(__name__)

MAX_PREVIEW_ROWS = 500
# NER/PII/Financial/Relation extraction (domain_managers.py) only ever
# runs on doc.full_text() -- the joined text_blocks -- never on
# doc.tables/full_tables directly. Without a text rendering of actual
# rows, those agents only ever saw the column-statistics profile below
# (dtypes, null %, unique counts), which contains no real names, PII, or
# amounts to find -- entities/PII/financial facts came back empty for
# every CSV regardless of what the data actually held. Capped well below
# MAX_PREVIEW_ROWS: this feeds LLM calls (segmented at SEGMENT_CHARS,
# capped at MAX_SEGMENTS in extraction.py), not the structured store,
# which already gets the full/near-full data via doc.tables/full_tables.
MAX_TEXT_PREVIEW_ROWS = 200
# Ceiling for the *full* (uncapped) copy that feeds the structured-query
# SQL store (see storage/structured_store.py) -- much larger than the
# on-screen preview, but still bounded so an accidentally huge upload
# can't blow up memory. Business CSVs this big are rare; when it happens,
# a warning below says so rather than silently dropping the tail.
MAX_STRUCTURED_ROWS = 200_000


class CSVParser(BaseParser):
    category = FileCategory.CSV

    async def parse(self, file_path: str) -> ParsedDocument:
        return await asyncio.to_thread(self._parse_sync, file_path)

    def _parse_sync(self, file_path: str) -> ParsedDocument:
        doc = ParsedDocument(source_file=file_path, category=self.category)
        try:
            sep = "\t" if file_path.lower().endswith(".tsv") else None
            # on_bad_lines="warn" reports malformed rows via the stdlib
            # warnings module rather than raising -- left uncaptured, that
            # warning only ever reaches a server log (if that), so rows get
            # silently dropped from the analyst's point of view. Capture it
            # and surface it on doc.warnings like every other data-loss note.
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                df = pd.read_csv(file_path, sep=sep, engine="python", on_bad_lines="warn")
            for w in caught:
                doc.warnings.append(f"Malformed row(s) skipped while parsing CSV: {w.message}")

            profile_lines = [f"Rows: {len(df)}, Columns: {len(df.columns)}"]
            for col in df.columns:
                null_pct = df[col].isna().mean() * 100
                dtype = str(df[col].dtype)
                profile_lines.append(
                    f"Column '{col}': dtype={dtype}, nulls={null_pct:.1f}%, "
                    f"unique={df[col].nunique(dropna=True)}"
                )
            doc.text_blocks.append(TextBlock(text="\n".join(profile_lines), kind="paragraph"))

            for _, row in df.head(MAX_TEXT_PREVIEW_ROWS).fillna("").astype(str).iterrows():
                line = "; ".join(f"{col}={val}" for col, val in row.items() if val)
                if line:
                    doc.text_blocks.append(TextBlock(text=line, kind="paragraph"))

            preview = df.head(MAX_PREVIEW_ROWS).fillna("").astype(str)
            doc.tables.append(TableBlock(
                headers=list(preview.columns),
                rows=preview.values.tolist(),
                caption="Preview" if len(df) > MAX_PREVIEW_ROWS else "Full data",
            ))
            if len(df) > MAX_PREVIEW_ROWS:
                full = df.head(MAX_STRUCTURED_ROWS).fillna("").astype(str)
                doc.full_tables.append(TableBlock(
                    headers=list(full.columns),
                    rows=full.values.tolist(),
                    caption=f"Full data ({len(full)} of {len(df)} rows)",
                ))
                if len(df) > MAX_STRUCTURED_ROWS:
                    doc.warnings.append(
                        f"Structured-query store only indexed the first {MAX_STRUCTURED_ROWS} of "
                        f"{len(df)} rows -- aggregate SQL answers over this file may be incomplete."
                    )
            doc.metadata = {"row_count": len(df), "column_count": len(df.columns), "parser": "pandas"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("CSV parse failed on %s", file_path)
            doc.warnings.append(f"CSV parse error: {exc}")
        return doc
