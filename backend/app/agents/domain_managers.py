"""L1 Domain Manager — per-file pipeline: parse -> chunk+embed -> functional \
extraction (NER/PII/Financial/Relation) -> DomainResult.

The diagram splits this into five separate L1 managers (Unstructured Doc,
Structured Data, Database, PII/Compliance, Business Intel) that subscribe
to each other's output. In this implementation every file flows through
the same domain pipeline function — file-type routing already happened at
L2 (the parser), and PII/BI extraction subscribes to the resulting text
stream exactly as the diagram specifies (lateral "raw text stream" /
"schema + tabular data" comms), just expressed as sequential awaits
instead of separate message-passing actors.
"""
import asyncio
import logging

from app.agents.chunking import chunk_and_embed
from app.agents.data_quality import assess_quality
from app.agents.extraction import run_financial, run_ner, run_pii, run_relations, run_summary
from app.agents.id_formats import detect_standard_ids
from app.agents.translation import translate_document
from app.llm.client import get_llm_client
from app.models.schemas import DomainResult, FileCategory, Job
from app.parsers.router import classify, parse_file
from app.pipeline.agent_tracker import finish_activity, start_activity
from app.storage import dataset_library, structured_store
from app.storage.file_store import write_tables_parquet

logger = logging.getLogger(__name__)

# Which categories carry real tabular data worth mirroring into the
# structured-query SQLite store (see storage/structured_store.py) -- not
# every category has TableBlocks, and the ones that don't (PDF, email,
# etc) never populate doc.tables anyway, so this is just an early filter
# rather than a hard requirement.
_STRUCTURED_CATEGORIES = {FileCategory.CSV, FileCategory.EXCEL, FileCategory.DATABASE}

# One lock per job serializes structured-store writes for that job only
# -- files within a job parse concurrently (MAX_PARALLEL_FILES), and two
# concurrent writers deciding on the same de-duplicated table name for
# the same job's structured.db before either commits would otherwise
# race. Other files' parsing/chunking/extraction steps are unaffected;
# only this one quick step queues up. Never cleaned up per-job (a Lock
# object is a few dozen bytes -- not worth the bookkeeping to pop it).
_structured_write_locks: dict[str, asyncio.Lock] = {}


def _structured_lock(job_id: str) -> asyncio.Lock:
    lock = _structured_write_locks.get(job_id)
    if lock is None:
        lock = asyncio.Lock()
        _structured_write_locks[job_id] = lock
    return lock


# dataset_library.py's SQLite file is shared by every job in this process,
# not scoped per job like structured.db -- a single process-wide lock
# (rather than the per-job _structured_write_locks above) is what actually
# prevents two different jobs' files from racing on the same "_datasets"
# table at once.
_library_write_lock = asyncio.Lock()

# Functional agents that only make sense on text-bearing categories.
_TEXT_CATEGORIES = {
    FileCategory.PDF, FileCategory.IMAGE, FileCategory.OFFICE, FileCategory.EMAIL,
    FileCategory.CODE, FileCategory.WEB, FileCategory.CSV, FileCategory.EXCEL,
    FileCategory.JSON_, FileCategory.DATABASE,
}

# Display names for the agent status panel, keyed by the category the L1
# router dispatches to -- kept here rather than in router.py since this is
# purely a UI-facing label, not routing logic.
_SPECIALIST_NAMES = {
    FileCategory.PDF: "PDF Specialist",
    FileCategory.IMAGE: "Image/OCR Specialist",
    FileCategory.CSV: "CSV Specialist",
    FileCategory.EXCEL: "Excel Specialist",
    FileCategory.JSON_: "JSON Specialist",
    FileCategory.OFFICE: "Office Specialist",
    FileCategory.EMAIL: "Email Specialist",
    FileCategory.DATABASE: "Database Specialist",
    FileCategory.CODE: "Code & Log Specialist",
    FileCategory.ARCHIVE: "Archive Specialist",
    FileCategory.MEDIA: "Media Specialist",
    FileCategory.WEB: "Web/XML Specialist",
    FileCategory.UNKNOWN: "Format Specialist",
}


def _apply_single_subject_fallback(result: DomainResult) -> None:
    """If this file's NER pass found exactly one PERSON entity, any PII
    finding the model didn't already attribute to a subject almost
    certainly belongs to that one person -- the passport / ID card /
    single-employee-form case: every field describes the same subject even
    when the model's per-segment attribution missed one. Deliberately does
    nothing when there are zero or multiple PERSON entities -- guessing
    which of several people a fact belongs to is worse than leaving
    subject_entity unset."""
    people = [e for e in result.entities if e.type == "PERSON"]
    if len(people) != 1:
        return
    subject = people[0].name
    for finding in result.pii_findings:
        if not finding.subject_entity:
            finding.subject_entity = subject


def _assess_quality_safe(doc, result: DomainResult, file_path: str):
    """assess_quality() is pure/deterministic but not infallible (e.g. a
    malformed TableBlock) -- the two early-return branches below call it
    without the surrounding try/except the main path uses, so wrap it here
    once rather than duplicating the pattern."""
    try:
        return assess_quality(doc, result)
    except Exception:
        logger.exception("Data quality assessment failed for %s", file_path)
        result.errors.append("Data quality assessment step failed")
        return None


async def process_file(
    file_path: str, unreachable_backends: set[str] | None = None, job: Job | None = None
) -> DomainResult:
    """unreachable_backends: backends already confirmed down by the job's
    preflight check (see job_manager._run). Extraction is skipped outright
    rather than attempted-and-retried when its backend is already known
    unreachable — this is what keeps a dead LLM endpoint from silently
    stalling a file for several minutes with no visible cause.

    job: optional -- when passed, every agent step reports its start/finish
    onto job.agent_activity for the live agent status panel. None (e.g. in
    isolated tests) simply skips tracking.
    """
    unreachable_backends = unreachable_backends or set()

    specialist_name = _SPECIALIST_NAMES.get(classify(file_path), "Format Specialist")
    activity = start_activity(job, specialist_name, file_path)
    try:
        doc = await parse_file(file_path)
        finish_activity(activity, "completed")
    except Exception:
        finish_activity(activity, "failed")
        raise

    # Best-effort mirror of this file's tables into the job's structured
    # SQLite store, so chat's text-to-SQL branch (graph/structured_query.py)
    # can run real queries against real typed data instead of only the
    # 20-row text preview that reaches the vector index. Prefer
    # doc.full_tables (uncapped, up to each parser's much larger safety
    # ceiling) over doc.tables (capped to 500 rows for the Data Dump
    # preview/export) -- full_tables is empty when a file never exceeded
    # the preview cap to begin with, in which case doc.tables is already
    # complete. job is None in isolated tests/callers that don't have a
    # job_id to key the store by -- skip rather than write to a store
    # nothing will ever read back.
    structured_tables = doc.full_tables or doc.tables
    if job is not None and doc.category in _STRUCTURED_CATEGORIES and structured_tables:
        try:
            async with _structured_lock(job.job_id):
                await asyncio.to_thread(
                    structured_store.write_tables, job.job_id, file_path, structured_tables, doc.category
                )
        except Exception:
            logger.exception("Structured table ingestion failed for %s", file_path)
            doc.warnings.append(
                "Structured query indexing failed for this file -- chat SQL answers may be incomplete."
            )

        # Separately, also save these same tables into the persistent
        # cross-job dataset library (see storage/dataset_library.py) --
        # unlike the structured-query mirror above, this one is meant to
        # outlive the job: it's a permanent, browsable/downloadable record
        # of every extracted structured table, not scratch input for chat.
        # A failure here is independent of the structured-query mirror
        # succeeding or failing above -- each is its own best-effort step.
        try:
            async with _library_write_lock:
                await asyncio.to_thread(
                    dataset_library.save_tables, job.job_id, file_path, structured_tables, doc.category
                )
        except Exception:
            logger.exception("Dataset library save failed for %s", file_path)
            doc.warnings.append(
                "Saving this file's tables to the dataset library failed -- they were still indexed for chat."
            )

        # Third independent best-effort write of the same uncapped table
        # set: a Parquet file per table under this job's output directory
        # (see storage/file_store.write_tables_parquet), so every job's
        # complete structured data is available as a portable columnar
        # file on disk, not just queryable through the app's own stores.
        try:
            await asyncio.to_thread(write_tables_parquet, job.job_id, file_path, structured_tables)
        except Exception:
            logger.exception("Parquet export failed for %s", file_path)
            doc.warnings.append("Saving this file's tables as Parquet failed -- they were still indexed for chat.")

    activity = start_activity(job, "Translator", file_path)
    try:
        doc = await translate_document(doc, unreachable_backends)
        finish_activity(activity, "completed")
    except Exception:
        # Translation is best-effort enrichment, not a hard dependency of
        # the rest of the pipeline -- a failure here (e.g. a malformed
        # backend response) shouldn't take down extraction for a file that
        # parsed fine. Continue with the untranslated doc rather than
        # raising, and record why so it's visible on the file's card
        # instead of leaving this activity stuck at "running" forever.
        logger.exception("Translation failed for %s", file_path)
        doc.warnings.append("Translation step failed — proceeding with untranslated text.")
        finish_activity(activity, "failed")

    result = DomainResult(
        domain=doc.category.value, source_file=file_path, tables=doc.tables,
        detected_language=doc.detected_language, translated=doc.translated,
    )

    if doc.warnings:
        result.errors.extend(doc.warnings)

    text = doc.full_text()
    if not text.strip() or doc.category not in _TEXT_CATEGORIES:
        activity = start_activity(job, "Chunk/Embed Extractor", file_path)
        try:
            result.chunks = await chunk_and_embed(doc)
            finish_activity(activity, "completed")
        except Exception:
            logger.exception("Chunk+embed failed for %s", file_path)
            result.errors.append("Chunk+embed step failed")
            finish_activity(activity, "failed")
        result.quality = _assess_quality_safe(doc, result, file_path)
        return result

    activity = start_activity(job, "Chunk/Embed Extractor", file_path)
    try:
        result.chunks = await chunk_and_embed(doc)
        finish_activity(activity, "completed")
    except Exception:
        logger.exception("Chunk+embed failed for %s", file_path)
        result.errors.append("Chunk+embed step failed")
        finish_activity(activity, "failed")

    extraction_backend = get_llm_client().backend_for_role("extraction")
    if extraction_backend in unreachable_backends:
        msg = (f"NER/PII/Financial/Relation extraction skipped: "
               f"'{extraction_backend}' model endpoint is unreachable")
        result.errors.append(msg)
        for name in ("Entity Extractor", "PII Extractor", "Financial Extractor", "Relation Extractor"):
            finish_activity(start_activity(job, name, file_path), "skipped")
        result.quality = _assess_quality_safe(doc, result, file_path)
        return result

    activity = start_activity(job, "Entity Extractor", file_path)
    try:
        result.entities = await run_ner(text, file_path)
        finish_activity(activity, "completed")
    except Exception:
        logger.exception("NER failed for %s", file_path)
        result.errors.append("NER step failed")
        finish_activity(activity, "failed")

    activity = start_activity(job, "PII Extractor", file_path)
    try:
        result.pii_findings = await run_pii(text, file_path, result.entities)
        # Independent, non-LLM detector for internationally standardized ID
        # formats (passport MRZ, IBAN, card numbers -- checksum-verified;
        # a few national ID shapes -- structural only). See id_formats.py
        # for why this runs on the raw text rather than trying to validate
        # the LLM's already-redacted findings.
        result.pii_findings.extend(detect_standard_ids(text, file_path))
        _apply_single_subject_fallback(result)
        finish_activity(activity, "completed")
    except Exception:
        logger.exception("PII detection failed for %s", file_path)
        result.errors.append("PII step failed")
        finish_activity(activity, "failed")

    activity = start_activity(job, "Financial Extractor", file_path)
    try:
        result.financial_facts = await run_financial(text, file_path)
        finish_activity(activity, "completed")
    except Exception:
        logger.exception("Financial extraction failed for %s", file_path)
        result.errors.append("Financial step failed")
        finish_activity(activity, "failed")

    activity = start_activity(job, "Relation Extractor", file_path)
    try:
        result.relations = await run_relations(text, result.entities, file_path)
        finish_activity(activity, "completed")
    except Exception:
        logger.exception("Relation extraction failed for %s", file_path)
        result.errors.append("Relation step failed")
        finish_activity(activity, "failed")

    try:
        result.summary = await run_summary(text)
    except Exception:
        logger.exception("Summary failed for %s", file_path)

    activity = start_activity(job, "Data Quality Validator", file_path)
    try:
        result.quality = assess_quality(doc, result)
        finish_activity(activity, "completed")
    except Exception:
        logger.exception("Data quality assessment failed for %s", file_path)
        result.errors.append("Data quality assessment step failed")
        finish_activity(activity, "failed")
    return result
