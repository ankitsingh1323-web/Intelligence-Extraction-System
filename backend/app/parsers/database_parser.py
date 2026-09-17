"""Database Agent (L2) -- uploaded SQLite and Microsoft Access files.

Scope note: this pipeline is file-upload based, so "Database" here covers
files you can upload directly: .db/.sqlite/.sqlite3 via Python's stdlib
sqlite3, and .mdb/.accdb via the mdbtools CLI. mdbtools reads both the
legacy Jet engine format (.mdb, Access 97-2003) and the modern ACE format
(.accdb, Access 2007+) from the same binaries, so both old and new Access
files are covered without separate code paths. Live connections to a
running Postgres/MySQL/MongoDB/Access server are a different integration
pattern (connection string + credentials, not a file) and are out of
scope for this agent -- see README if you need that.
"""
import asyncio
import csv
import io
import logging
import sqlite3
import subprocess

from app.models.schemas import FileCategory, ParsedDocument, TableBlock, TextBlock
from app.parsers.base import BaseParser

logger = logging.getLogger(__name__)

MAX_ROWS_PER_TABLE = 500
# See csv_parser.MAX_STRUCTURED_ROWS -- same rationale, per table here.
MAX_STRUCTURED_ROWS = 200_000
MDB_TIMEOUT_S = 60
# See csv_parser.MAX_TEXT_PREVIEW_ROWS -- same reasoning: NER/PII/
# Financial/Relation extraction only ever runs on doc.full_text(), which
# used to contain nothing but "Table 'X': N rows, columns: A, B, C" --
# schema description, no actual cell values -- so those agents never had
# any real names/PII/amounts to find in a database file, regardless of
# what the data actually held.
MAX_TEXT_PREVIEW_ROWS = 200


class DatabaseParser(BaseParser):
    category = FileCategory.DATABASE

    async def parse(self, file_path: str) -> ParsedDocument:
        return await asyncio.to_thread(self._parse_sync, file_path)

    def _parse_sync(self, file_path: str) -> ParsedDocument:
        if file_path.lower().endswith((".mdb", ".accdb")):
            return self._parse_access(file_path)
        return self._parse_sqlite(file_path)

    @staticmethod
    def _row_text_blocks(table: str, headers: list[str], rows: list[list[str]]) -> list[TextBlock]:
        blocks = []
        for row in rows[:MAX_TEXT_PREVIEW_ROWS]:
            line = "; ".join(f"{h}={v}" for h, v in zip(headers, row) if v)
            if line:
                blocks.append(TextBlock(text=f"[{table}] {line}", kind="paragraph"))
        return blocks

    def _parse_sqlite(self, file_path: str) -> ParsedDocument:
        doc = ParsedDocument(source_file=file_path, category=self.category)
        try:
            # Open read-only via URI so a corrupt/non-SQLite file fails fast
            # instead of sqlite3 silently creating a new empty database.
            conn = sqlite3.connect(f"file:{file_path}?mode=ro", uri=True)
            try:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
                table_names = [row[0] for row in cur.fetchall()]

                if not table_names:
                    doc.warnings.append("No user tables found — not a valid SQLite database?")
                    return doc

                doc.text_blocks.append(TextBlock(text=f"Database contains {len(table_names)} table(s): "
                                                       f"{', '.join(table_names)}", kind="paragraph"))

                for table in table_names:
                    # One table's failure (locked, corrupted page, an
                    # unreadable column type) shouldn't abort every table
                    # after it in table_names -- catch per-table so the
                    # loop always reaches every table, not just the ones
                    # before the first failure.
                    try:
                        cur.execute(f'PRAGMA table_info("{table}")')
                        columns = [row[1] for row in cur.fetchall()]
                        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
                        row_count = cur.fetchone()[0]

                        doc.text_blocks.append(TextBlock(
                            text=f"Table '{table}': {row_count} rows, columns: {', '.join(columns)}",
                            kind="paragraph",
                        ))

                        cur.execute(f'SELECT * FROM "{table}" LIMIT {MAX_ROWS_PER_TABLE}')
                        rows = [[str(v) if v is not None else "" for v in row] for row in cur.fetchall()]
                        doc.tables.append(TableBlock(
                            sheet=table, headers=columns, rows=rows,
                            caption=f"{table} (showing up to {MAX_ROWS_PER_TABLE} of {row_count} rows)",
                        ))
                        doc.text_blocks.extend(self._row_text_blocks(table, columns, rows))

                        # Populated for every table unconditionally, same
                        # reasoning as excel_parser.py -- the structured
                        # store consumes doc.full_tables for the whole
                        # database file at once, so a partial list would
                        # silently drop every other table from chat's SQL
                        # store entirely.
                        cur.execute(f'SELECT * FROM "{table}" LIMIT {MAX_STRUCTURED_ROWS}')
                        full_rows = [[str(v) if v is not None else "" for v in row] for row in cur.fetchall()]
                        doc.full_tables.append(TableBlock(
                            sheet=table, headers=columns, rows=full_rows,
                            caption=f"{table} ({len(full_rows)} of {row_count} rows)",
                        ))
                        if row_count > MAX_STRUCTURED_ROWS:
                            doc.warnings.append(
                                f"Structured-query store only indexed the first {MAX_STRUCTURED_ROWS} of "
                                f"{row_count} rows in table '{table}' -- aggregate SQL answers over this "
                                f"table may be incomplete."
                            )
                    except sqlite3.DatabaseError as exc:
                        logger.warning("Failed to read table '%s' in %s: %s", table, file_path, exc)
                        doc.warnings.append(f"Table '{table}' could not be read and was skipped: {exc}")

                doc.metadata = {"table_count": len(table_names), "parser": "sqlite3"}
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            doc.warnings.append(f"Not a readable SQLite database: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Database parse failed on %s", file_path)
            doc.warnings.append(f"Database parse error: {exc}")
        return doc

    def _parse_access(self, file_path: str) -> ParsedDocument:
        doc = ParsedDocument(source_file=file_path, category=self.category)
        try:
            table_names = self._mdb_tables(file_path)
        except FileNotFoundError:
            doc.warnings.append(
                "mdbtools is not installed in this image -- Access (.mdb/.accdb) files cannot be read."
            )
            return doc
        except subprocess.TimeoutExpired:
            doc.warnings.append("Timed out listing tables in Access database.")
            return doc
        except subprocess.CalledProcessError as exc:
            doc.warnings.append(f"Not a readable Access database: {(exc.stderr or '').strip() or exc}")
            return doc

        if not table_names:
            doc.warnings.append("No user tables found — not a valid Access database?")
            return doc

        doc.text_blocks.append(TextBlock(
            text=f"Database contains {len(table_names)} table(s): {', '.join(table_names)}",
            kind="paragraph",
        ))

        for table in table_names:
            # Same per-table isolation as the SQLite path -- one bad table
            # shouldn't abort every table after it.
            try:
                headers, rows = self._mdb_export(file_path, table)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                logger.warning("Failed to read table '%s' in %s: %s", table, file_path, exc)
                doc.warnings.append(f"Table '{table}' could not be read and was skipped: {exc}")
                continue

            row_count = len(rows)
            doc.text_blocks.append(TextBlock(
                text=f"Table '{table}': {row_count} rows, columns: {', '.join(headers)}",
                kind="paragraph",
            ))
            doc.tables.append(TableBlock(
                sheet=table, headers=headers, rows=rows[:MAX_ROWS_PER_TABLE],
                caption=f"{table} (showing up to {MAX_ROWS_PER_TABLE} of {row_count} rows)",
            ))
            doc.text_blocks.extend(self._row_text_blocks(table, headers, rows))
            # mdb-export has no server-side LIMIT the way a SQL SELECT does
            # -- it always dumps the whole table -- so unlike the SQLite
            # path above, this slices an already-fully-read list rather
            # than bounding the read itself.
            doc.full_tables.append(TableBlock(
                sheet=table, headers=headers, rows=rows[:MAX_STRUCTURED_ROWS],
                caption=f"{table} ({min(row_count, MAX_STRUCTURED_ROWS)} of {row_count} rows)",
            ))
            if row_count > MAX_STRUCTURED_ROWS:
                doc.warnings.append(
                    f"Structured-query store only indexed the first {MAX_STRUCTURED_ROWS} of "
                    f"{row_count} rows in table '{table}' -- aggregate SQL answers over this "
                    f"table may be incomplete."
                )

        doc.metadata = {"table_count": len(table_names), "parser": "mdbtools"}
        return doc

    @staticmethod
    def _mdb_tables(file_path: str) -> list[str]:
        # -1: one table name per line, user tables only (system MSys* tables
        # are excluded by default).
        result = subprocess.run(
            ["mdb-tables", "-1", file_path],
            capture_output=True, text=True, timeout=MDB_TIMEOUT_S, check=True,
        )
        return [t for t in result.stdout.splitlines() if t.strip()]

    @staticmethod
    def _mdb_export(file_path: str, table: str) -> tuple[list[str], list[list[str]]]:
        result = subprocess.run(
            ["mdb-export", file_path, table],
            capture_output=True, text=True, timeout=MDB_TIMEOUT_S, check=True,
        )
        rows = list(csv.reader(io.StringIO(result.stdout)))
        headers = rows[0] if rows else []
        return headers, rows[1:]
