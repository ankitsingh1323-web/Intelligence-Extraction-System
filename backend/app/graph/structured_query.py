"""Structured Query -- text-to-SQL branch for chat questions that are
better answered by running an actual query against a job's structured
files (CSV/Excel/SQLite) than by semantic retrieval over embedded text
chunks. See storage/structured_store.py for how the underlying per-job
SQLite database gets populated at ingestion time.

Any failure along the way (no structured tables, LLM call fails, every
attempt at generating safe/valid SQL is exhausted) returns None so the
caller (graphrag.answer_question) falls through to normal GraphRAG
retrieval -- same "a wrong or absent signal can never make the answer
worse than not routing at all" philosophy query_router.py uses for
category inference.

Self-correction: an unsafe/invalid query or a SQLite execution error gets
the error fed back to the model for one corrected retry (MAX_ATTEMPTS)
rather than giving up immediately -- most first-try mistakes are a typo'd
column name or an extra semicolon, both trivially fixable with the actual
error message in hand. The model seeing its own prior attempt is exactly
why this loop, not a second independent guess, fixes more failures.

Multi-table questions ("join orders with customers on customer_id") work
without any special-casing: every table from every structured file in a
job lives in the same per-job SQLite database, so the schema prompt below
already lists all of them and the model can write the JOIN directly.

Answer text: once a query succeeds, a short second LLM call turns the raw
result rows into one grounded sentence (explicitly told to use only the
values shown, not compute anything new) -- skipped for large result sets
where handing full rows back to the model isn't worth the tokens, and
skipped-with-fallback if that call itself fails, in which case a plain
templated line ("N row(s) returned") still gives a correct, if blander,
answer. Either way the full SQL and result table are attached to the
response for verification -- narration failing never means the analyst
loses the ground truth, only the one-sentence gloss on it.
"""
import asyncio
import logging
import re
import sqlite3
import time

from app.llm.client import get_llm_client
from app.models.schemas import ChatResponse, StructuredQueryResult
from app.storage import structured_store

logger = logging.getLogger(__name__)

MAX_RESULT_ROWS = 200
MAX_ATTEMPTS = 2
MAX_ROWS_TO_NARRATE = 25
QUERY_TIMEOUT_S = 10.0
# Past this many tables, listing every one in the schema prompt risks a
# slow, expensive LLM call for a job with dozens of structured files --
# cap it and let a more specific follow-up question narrow things down.
MAX_SCHEMA_TABLES = 40

# Cheap heuristic for "this question probably wants a computed/filtered
# answer over tabular data," not a semantic-similarity search -- aggregate
# verbs, comparison language, and explicit row/table vocabulary. False
# negatives just mean a quantitative question gets a GraphRAG answer
# instead (same as today); false positives cost one extra LLM call that
# self-discards via _is_safe_select/NOT_ANSWERABLE below -- both fail
# safe, so this can stay a blunt regex rather than a classifier.
_QUANT_RE = re.compile(
    r"\b(how many|count|sum|total|average|avg|mean|median|maximum|max|minimum|min|"
    r"top \d+|greater than|less than|more than|fewer than|at least|at most|between|"
    r"group by|sort by|order by|filter|rows?|records?|entries|join|duplicate|distinct|unique)\b",
    re.IGNORECASE,
)

_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|pragma|create|replace|vacuum|reindex|"
    r"begin|commit|rollback)\b",
    re.IGNORECASE,
)

SQL_SYSTEM = """You write a single read-only SQLite SELECT query to answer a question, given a schema.

Rules:
- Output ONLY the SQL query -- no explanation, no markdown fences, no commentary.
- SELECT statements only. Never write anything that modifies data or schema.
- Use exactly the table and column names given in the schema (they are already valid SQLite identifiers).
- One statement only, no semicolons.
- Tables from different files can be joined directly if the question calls for it.
- If the question cannot be answered from this schema, output exactly: SELECT 'NOT_ANSWERABLE' AS error
"""

SUMMARY_SYSTEM = """You are given the exact result of a SQL query that was just run against structured data.

Write ONE short, factual sentence (at most ~30 words) stating the answer, using ONLY the values shown in \
the result -- do not compute anything not directly present, do not round differently than shown, do not \
add caveats or mention SQL. If the result is empty, say so plainly.
"""


def looks_quantitative(question: str) -> bool:
    return bool(_QUANT_RE.search(question))


def _is_not_answerable(sql: str) -> bool:
    return "NOT_ANSWERABLE" in sql.upper()


def _is_safe_select(sql: str) -> bool:
    s = sql.strip().rstrip(";").strip()
    if not s or ";" in s:
        return False
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        return False
    if _FORBIDDEN_RE.search(s):
        return False
    return True


def _extract_sql(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(sql)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _schema_prompt(manifest: list[dict]) -> str:
    shown = manifest[:MAX_SCHEMA_TABLES]
    lines = []
    for t in shown:
        cols = ", ".join(f"{c} ({typ})" for c, typ in t["columns"])
        lines.append(f'Table "{t["table_name"]}" (from {t["source_file"]}, {t["row_count"]} rows): {cols}')
    if len(manifest) > MAX_SCHEMA_TABLES:
        lines.append(
            f"... and {len(manifest) - MAX_SCHEMA_TABLES} more table(s) not shown here "
            f"-- ask a more specific question naming the file if it's one of those."
        )
    return "\n".join(lines)


def _execute_readonly(job_id: str, sql: str) -> tuple[list[str], list[list[str]], bool]:
    conn = structured_store.open_readonly(job_id)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        # SQLite has no query-level timeout of its own -- a progress
        # handler firing every N VM instructions is the standard way to
        # bound a runaway generated query (an accidental cross join, a
        # pathological ORDER BY) without touching the whole process. A
        # truthy return aborts the query with sqlite3.OperationalError,
        # which the retry loop above already treats like any other
        # execution failure -- one corrected retry, then fall back.
        deadline = time.monotonic() + QUERY_TIMEOUT_S
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        cur = conn.execute(sql)
        headers = [d[0] for d in cur.description] if cur.description else []
        fetched = cur.fetchmany(MAX_RESULT_ROWS + 1)
        truncated = len(fetched) > MAX_RESULT_ROWS
        rows = [["" if v is None else str(v) for v in r] for r in fetched[:MAX_RESULT_ROWS]]
        return headers, rows, truncated
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


async def _narrate(client, question: str, headers: list[str], rows: list[list[str]]) -> str | None:
    """Grounded one-sentence gloss on an already-computed result -- the
    model paraphrases numbers it's handed, it never computes them, so this
    can't introduce a wrong figure that wasn't already in `rows`."""
    if not rows or len(rows) > MAX_ROWS_TO_NARRATE:
        return None
    table_text = " | ".join(headers) + "\n" + "\n".join(" | ".join(r) for r in rows)
    user_prompt = f"Question: {question}\n\nQuery result:\n{table_text}"
    try:
        resp = await client.complete("chat", SUMMARY_SYSTEM, user_prompt, temperature=0.0, max_tokens=500)
    except Exception:
        logger.exception("Structured-answer narration failed -- falling back to the templated summary.")
        return None
    return resp.text.strip() or None


async def answer_structured_question(job_id: str, message: str) -> ChatResponse | None:
    manifest = structured_store.read_manifest(job_id)
    if not manifest:
        return None

    client = get_llm_client()
    schema = _schema_prompt(manifest)
    base_prompt = f"Schema:\n{schema}\n\nQuestion: {message}"
    error_context = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = await client.complete(
                "chat", SQL_SYSTEM, base_prompt + error_context, temperature=0.0, max_tokens=1200
            )
        except Exception:
            logger.exception("SQL generation failed for job %s (attempt %s)", job_id, attempt)
            return None

        sql = _extract_sql(resp.text)

        if _is_not_answerable(sql):
            return None

        if not _is_safe_select(sql):
            logger.warning("Generated SQL rejected as unsafe/invalid for job %s (attempt %s): %r", job_id, attempt, sql)
            if attempt == MAX_ATTEMPTS:
                return None
            error_context = (
                f"\n\nYour previous attempt was rejected -- it wasn't a single safe read-only SELECT "
                f"statement: {sql!r}\nWrite a corrected query."
            )
            continue

        try:
            headers, rows, truncated = await asyncio.to_thread(_execute_readonly, job_id, sql)
        except sqlite3.Error as exc:
            logger.warning("Structured query execution failed for job %s (attempt %s): %s (sql=%r)",
                            job_id, attempt, exc, sql)
            if attempt == MAX_ATTEMPTS:
                return None
            error_context = (
                f"\n\nYour previous query failed with this SQLite error: {exc}\n"
                f"Previous query: {sql}\nWrite a corrected query that avoids this error."
            )
            continue

        narration = await _narrate(client, message, headers, rows)
        answer = narration or (
            f"Ran a structured query against {len(manifest)} table(s) of extracted data "
            f"— {len(rows)} row(s) returned" + (", showing the first 200." if truncated else ".")
        )
        return ChatResponse(
            answer=answer,
            query_mode="structured",
            sql_used=sql,
            structured_result=StructuredQueryResult(
                headers=headers, rows=rows, row_count=len(rows), truncated=truncated
            ),
        )

    return None
