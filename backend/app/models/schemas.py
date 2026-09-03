"""Shared data contracts passed between pipeline tiers.

These mirror the message shapes in the architecture diagram:
TaskAssign / DomainResult / Extraction / AtomicResult, etc, collapsed into
concrete Pydantic models used directly as function I/O (no wire
serialization overhead needed since everything runs in-process / same
Python asyncio loop, but they remain JSON-serializable for the API and for
persisting job state).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _utc_now() -> datetime:
    """Timezone-AWARE now, not datetime.utcnow() -- utcnow() returns a
    naive datetime that Pydantic serializes to JSON with no timezone
    offset (e.g. "2026-08-06T04:38:00"), which every other aware
    datetime in this app (anything set via datetime.now(timezone.utc),
    e.g. job_manager.touch()) serializes WITH one ("...+00:00"). The
    frontend's `new Date(...)` parses a date-time string with no offset
    as LOCAL time, not UTC -- so comparing a naive-serialized timestamp
    against an aware-serialized one in JS silently comes out wrong by
    exactly the browser's UTC offset. This was the actual cause of
    CompletionHero's "Time to complete" showing ~241 minutes for jobs
    that took a couple of minutes on a UTC+4 machine (240 = 4h, not a
    coincidence). Use this everywhere instead of datetime.utcnow()."""
    return datetime.now(timezone.utc)


def _ensure_utc(v: datetime | None) -> datetime | None:
    """Normalizes a naive datetime to UTC-aware, leaving an already-aware
    one untouched. Applied as a field_validator on every persisted
    datetime field below (Job.created_at/updated_at/completed_at,
    AgentActivity.started_at/finished_at) so it runs on EVERY path a
    value can enter a model -- both a fresh instance built with _utc_now
    (already aware, a no-op here) and Job.model_validate_json() loading
    an old job_state.json written before this file's naive/aware fix
    (see _utc_now's docstring), which deserializes as naive with no
    offset in the JSON to recover one from.

    Without this, JobManager.list_jobs() mixes naive (pre-fix, loaded
    from disk) and aware (every job created since) datetimes in the same
    in-memory registry, and `sorted(jobs, key=lambda j: j.created_at)`
    raises "TypeError: can't compare offset-naive and offset-aware
    datetimes" the instant both kinds coexist -- which turns GET
    /api/jobs into a 500 for any deployment old enough to have pre-fix
    job state still on disk, silently hiding EVERY job (not just new
    ones) since the frontend's History page has no error handling around
    that fetch and just renders "no analyses run yet" on any failure.
    Assumes UTC for a naive value, matching what this app always
    intended -- correct in every environment this deployment has ever
    run in, since _utc_now() is the only writer of these fields and it's
    always been UTC-aware in intent, even on the runs where the naive
    utcnow() bug meant the offset never made it into the JSON."""
    if v is not None and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


class FileCategory(str, Enum):
    PDF = "pdf"
    IMAGE = "image"
    CSV = "csv"
    JSON_ = "json"
    EXCEL = "excel"
    OFFICE = "office"          # docx/pptx - stub
    EMAIL = "email"            # stub
    DATABASE = "database"      # stub
    CODE = "code"              # stub
    ARCHIVE = "archive"        # stub
    MEDIA = "media"            # stub
    WEB = "web"                # stub
    UNKNOWN = "unknown"


class JobStatus(str, Enum):
    QUEUED = "queued"
    PARSING = "parsing"
    EXTRACTING = "extracting"
    GRAPH_BUILD = "graph_build"
    SYNTHESIZING = "synthesizing"
    COMPLETE = "complete"
    FAILED = "failed"
    # Job-level only: a large job (see importance.py) processes in
    # importance-ranked batches and pauses here after each one but the
    # last, waiting for the user to continue or stop early.
    AWAITING_BATCH_CONFIRM = "awaiting_batch_confirm"
    # FileProgress-level only: the job was stopped early by the user before
    # this file's batch ran, so it was never attempted -- distinct from
    # QUEUED (which also means "not yet attempted") so the file list can
    # show *why* it never ran instead of looking permanently stuck.
    SKIPPED = "skipped"


# ---------------------------------------------------------------- parsing --

class TextBlock(BaseModel):
    block_id: str = Field(default_factory=_id)
    text: str
    page: int | None = None
    kind: str = "paragraph"     # paragraph | heading | table_caption | ocr | cell
    bbox: list[float] | None = None
    confidence: float | None = None


class TableBlock(BaseModel):
    table_id: str = Field(default_factory=_id)
    page: int | None = None
    sheet: str | None = None
    headers: list[str] = []
    rows: list[list[str]] = []
    caption: str | None = None


class DatasetRecord(BaseModel):
    """One saved table in the persistent, cross-job dataset library (see
    storage/dataset_library.py) -- distinct from TableBlock, which is a
    single file's in-memory parse result. `columns` is a list of
    [name, sqlite_type] pairs (e.g. ["amount", "REAL"]), mirroring
    structured_store.py's manifest format."""
    dataset_id: str
    job_id: str
    source_file: str
    table_name: str
    sheet: str | None = None
    category: str
    row_count: int
    columns: list[list[str]]
    saved_at: str


class OracleSchemaColumn(BaseModel):
    """One column of an OracleSchemaGroup, rendered Oracle-DESC-style --
    UPPER_SNAKE_CASE name, Oracle datatype name (VARCHAR2/NUMBER) instead
    of the underlying SQLite storage class (TEXT/INTEGER/REAL)."""
    name: str
    oracle_type: str


class OracleSchemaGroup(BaseModel):
    """One entry in the Data Dump tab's structured-data schema catalog
    (see agents/schema_catalog.py) -- either a single saved table, or
    several tables that share the exact same standardized column set
    UNIONed together into one combined structure. `member_tables` are the
    real table names inside the persistent dataset library (see
    storage/dataset_library.py); `member_files` are the source files they
    came from, so the UI can show what got combined."""
    group_name: str
    columns: list[OracleSchemaColumn]
    member_tables: list[str]
    member_files: list[str]
    row_count: int
    combined: bool
    union_sql: str | None = None


class StructuredDataCatalog(BaseModel):
    """Where a job's structured data actually lives (the persistent
    dataset library's file path) plus its schema catalog -- what the Data
    Dump tab's new "structured data location" section renders."""
    library_path: str
    groups: list[OracleSchemaGroup]


class FileGroup(BaseModel):
    """One row of the High Level Analysis tab's "combined simple files"
    section (see agents/synthesizer.compute_file_groups) -- several files
    of the same category that individually yielded no extraction signal
    (zero entities/relations/PII/financial facts/tables) collapsed into
    one summary row instead of cluttering the file-by-file breakdown with
    N near-identical "nothing found" entries. member_files names exactly
    what was combined."""
    category: str
    member_files: list[str]
    file_count: int
    entities: int
    relations: int
    pii_findings: int
    financial_facts: int
    tables: int
    chunks: int


class OBSCredentialPublic(BaseModel):
    """One saved OBS (object storage) credential profile, as returned to
    the frontend -- NEVER includes the actual secret key. secret_key_hint
    is a display-only masked form (e.g. "a1b2••••••••") built once at save
    time from the plaintext, not decryptable back from what's stored here;
    the real encrypted secret only ever lives in storage/obs_settings.py's
    SQLite file and is decrypted in-process, on demand, by
    storage/obs_client.py -- it never round-trips through the API."""
    id: str
    name: str
    endpoint: str
    region: str | None = None
    bucket: str
    path_prefix: str = ""
    access_key: str
    secret_key_hint: str
    is_active: bool
    created_at: str
    verified_at: str | None = None
    verified_ok: bool | None = None
    verified_detail: str | None = None


class OBSSettings(BaseModel):
    enabled: bool
    credentials: list[OBSCredentialPublic] = []


class ParsedDocument(BaseModel):
    """Unified output of every L2 file-type agent."""
    source_file: str
    category: FileCategory
    text_blocks: list[TextBlock] = []
    tables: list[TableBlock] = []
    # Only populated by the CSV/Excel/Database parsers: the same tables as
    # `tables`, but without the 500-row-per-table cap that keeps `tables`
    # small enough to render in the Data Dump tab / export to CSV. Feeds
    # storage/structured_store.py so chat's structured-query SQL branch
    # (graph/structured_query.py) sees every row of a large file, not just
    # the on-screen preview. Empty means "identical to `tables`" (the file
    # was under the preview cap to begin with) -- callers should use
    # `full_tables or tables`, never assume this list is present.
    full_tables: list[TableBlock] = []
    metadata: dict = {}
    warnings: list[str] = []
    # Set by the Translator agent (L1.5 preprocessing, runs on Qwen) when
    # the detected source language isn't the target language -- text_blocks
    # are translated in place, original language is kept here for
    # provenance. None means "no translation attempted" (already-English or
    # too little text to reliably detect).
    detected_language: str | None = None
    translated: bool = False

    def full_text(self) -> str:
        return "\n".join(b.text for b in self.text_blocks if b.text.strip())


class ColumnQuality(BaseModel):
    """Cell-level quality stats for one column of one table, computed by
    scanning every row -- not sampled, not LLM-guessed."""
    column: str
    null_rate: float            # share of rows blank or a null-token ("", "N/A", "null", "-", ...)
    garbage_rate: float         # share of rows that are non-null but junk (Excel errors, "####", repeated chars)
    distinct_ratio: float       # distinct non-null values / non-null count -- near 0 means constant column
    type_consistency: float     # share of non-null, non-garbage values matching inferred_type
    inferred_type: str          # id | number | date | email | phone | text
    issues: list[str] = []


class TableQuality(BaseModel):
    """Row/column-level quality stats for one table within a file."""
    table: str                  # sheet/caption/table_id label
    row_count: int
    duplicate_rows: int
    duplicate_rate: float
    columns: list[ColumnQuality] = []


class DataQuality(BaseModel):
    """Deterministic per-file quality assessment (L3 Data Quality agent) --
    computed from parser warnings, OCR confidence, extraction yield, and
    (for tabular files) real cell-level scans for NULLs, garbage/placeholder
    values, duplicate rows, constant columns, and type consistency. Not
    LLM-guessed."""
    source_file: str
    score: float               # 0-100 composite
    completeness: str          # short human-readable status
    issues: list[str] = []
    tables: list[TableQuality] = []


# ------------------------------------------------------------- extraction --

class Entity(BaseModel):
    entity_id: str = Field(default_factory=_id)
    name: str
    type: str                 # PERSON | ORG | LOCATION | DATE | MONEY | ID | ...
    source_file: str
    mentions: list[str] = []
    confidence: float = 0.8


class Relation(BaseModel):
    relation_id: str = Field(default_factory=_id)
    source_entity: str        # entity name
    target_entity: str
    relation_type: str        # e.g. OWNS, EMPLOYED_BY, PAID, LOCATED_IN
    source_file: str
    evidence: str | None = None
    confidence: float = 0.7


class PIIFinding(BaseModel):
    finding_id: str = Field(default_factory=_id)
    category: str              # EMAIL, PHONE, IBAN, EMIRATES_ID, CREDIT_CARD, ...
    value_redacted: str
    severity: str               # low | medium | high | critical
    source_file: str
    location: str | None = None
    # Name of the PERSON Entity (from this file's NER pass) this finding
    # describes -- e.g. a passport number's subject_entity is the person
    # named on that passport, so a document's scattered PII findings can be
    # recognized as facts about the same subject rather than disconnected
    # items. Matched by name, same convention Relation.source_entity/
    # target_entity already use. None means no clear single-subject
    # attribution was made (multi-subject document, or genuinely
    # ambiguous) -- never guessed wrong rather than always populated.
    subject_entity: str | None = None
    # "llm" (default): found by run_pii's language-model pass. "rules_checksum":
    # found by agents/id_formats.py matching an internationally standardized
    # format (passport MRZ, IBAN, card number) with its checksum verified --
    # the strongest confidence signal this system produces. "rules_shape":
    # matched a known national-ID format's shape/length but has no verified
    # checksum (no single public spec to check against).
    detection_method: str = "llm"


class FinancialFact(BaseModel):
    fact_id: str = Field(default_factory=_id)
    label: str
    amount: float | None = None
    currency: str | None = None
    period: str | None = None
    source_file: str
    context: str | None = None


class Chunk(BaseModel):
    chunk_id: str = Field(default_factory=_id)
    source_file: str
    text: str
    embedding: list[float] | None = None
    page: int | None = None
    # Source file's category, carried onto both the Neo4j Chunk node and
    # the in-memory fallback copy -- lets chat retrieval narrow its
    # candidate pool to the categories a question's keywords suggest
    # (see graph/query_router.py) instead of always ranking every chunk
    # in the job regardless of format.
    category: FileCategory = FileCategory.UNKNOWN


class DomainResult(BaseModel):
    """What each L1 domain manager reports back to the orchestrator."""
    domain: str
    source_file: str
    entities: list[Entity] = []
    relations: list[Relation] = []
    pii_findings: list[PIIFinding] = []
    financial_facts: list[FinancialFact] = []
    chunks: list[Chunk] = []
    tables: list[TableBlock] = []
    summary: str | None = None
    errors: list[str] = []
    quality: DataQuality | None = None
    detected_language: str | None = None
    translated: bool = False


# -------------------------------------------------------------- synthesis --

class BusinessIndex(BaseModel):
    """A metric computed deterministically by linking extraction results
    across two or more files/data types — not an LLM-guessed number.
    e.g. what share of contract value sits with one counterparty, computed
    by joining financial_facts to entities across every uploaded file.
    Used for the corpus-level overview on the High Level Analysis tab.
    """
    name: str
    value: str                  # formatted result: "42%", "3 of 6 files", "0.78"
    basis: str                  # what was linked/combined to compute it
    sources: list[str] = []     # contributing file names


class FileStats(BaseModel):
    """Per-file breakdown for the High Level Analysis tab -- what came out
    of each uploaded file, at a glance, computed directly from its
    DomainResult (no LLM call).
    """
    source_file: str
    category: str
    entities: int = 0
    relations: int = 0
    pii_findings: int = 0
    financial_facts: int = 0
    tables: int = 0
    chunks: int = 0
    summary: str | None = None


class BITableColumn(BaseModel):
    """One column of a proposed BI table -- where it actually comes from."""
    target_column: str          # standardized name, e.g. "customer_id"
    source_file: str
    source_table: str           # "<file>::<sheet/table/caption>"
    source_column: str
    data_type_guess: str        # id | email | phone | date | number | text
    sample_values: list[str] = []
    # The transformation rule that produces this proposal: the SQL-style
    # join condition for a cross-file table (e.g. "FROM orders.csv LEFT
    # JOIN customers.csv ON orders.csv.customer_id = customers.csv.id"), or
    # a plain-language direct-load rule for a standalone (single-file)
    # table. Always populated, and the same string repeated on every column
    # of a given proposal.
    rule: str = ""
    # Cleaning notes for this exact column, pulled from the Data Quality
    # agent's already-computed per-column issues (NULLs, garbage values,
    # type mismatches, ...) -- None if that column had no flagged issues.
    comments: str | None = None


class BITableProposal(BaseModel):
    """A candidate table for the BI layer -- either a standardized
    single-source table, or (the more valuable case) a cross-file table
    formed by joining two sources on a shared key. Computed deterministically
    by the Mapping Agent from column-name standardization + executed-join
    overlap, never LLM-guessed. This same object is used both as a
    "Business Use Case" (what the table is for) and as its own
    Source -> Target Mapping entry (exactly how to build it) -- one
    proposal, two views of it.
    """
    name: str                      # e.g. "Orders enriched with Customer" or "Customers"
    purpose: str                   # the business use case this table serves
    grain: str                     # what one row represents
    source_files: list[str] = []
    columns: list[BITableColumn] = []
    join_logic: str | None = None      # e.g. "orders.customer_id (FK) -> customers.CustID (PK)"
    join_quality: str | None = None    # e.g. "67% matched (2 of 3); 1 unmatched row in orders"
    target_file: str = ""              # proposed table name, slugified into a filename, e.g. "transaction.csv"


class BIReport(BaseModel):
    executive_summary: str
    key_entities: list[str] = []
    financial_highlights: list[str] = []
    risks: list[str] = []
    market_signals: list[str] = []
    corpus_overview: list[BusinessIndex] = []      # "what's in the dump" -- corpus-level KPIs
    # Files that yielded real extraction signal (at least one entity/
    # relation/PII finding/financial fact/table) -- each still gets its
    # own row. Files with NONE of those are pulled out into file_groups
    # instead of cluttering this list with near-identical "nothing found"
    # entries (see agents/synthesizer.compute_file_groups).
    file_breakdown: list[FileStats] = []           # table/file-wise info for each file
    file_groups: list[FileGroup] = []              # same-category "simple" files combined into one row
    business_use_cases: list[BITableProposal] = []  # proposed BI-layer tables
    data_quality: list[DataQuality] = []           # quality check stats (per-file Validator output)


class ComplianceReport(BaseModel):
    pii_inventory: list[PIIFinding] = []
    severity_counts: dict[str, int] = {}
    gap_flags: list[str] = []
    remediation: list[str] = []


class KnowledgeGraphExport(BaseModel):
    entities: list[Entity] = []
    relations: list[Relation] = []


class DataDump(BaseModel):
    tables: list[TableBlock] = []
    files_processed: list[str] = []
    chunk_count: int = 0


class DataDumpTable(TableBlock):
    """TableBlock plus which file it came from -- the raw TableBlock has no
    source_file (Neo4j/CSV export never needed it), but the Data Dump tab
    groups tables by file type so it needs one."""
    source_file: str
    category: FileCategory


class SourceDocSummary(BaseModel):
    """Per-file card for the Data Dump tab's document previews --
    deterministic, no LLM call, built directly from this file's
    DomainResult (same pattern as FileStats)."""
    source_file: str
    category: FileCategory
    has_preview: bool = False          # image/pdf -- frontend renders <img> from the preview route
    entities: list[str] = []           # distinct entity names, capped
    pii_types: list[str] = []          # distinct PII categories found
    text_excerpt: str | None = None    # short excerpt from the first chunk(s)


class SourceTargetMapping(BaseModel):
    """One entry per proposed BI table (see BITableProposal) -- the exact
    source-column -> target-column mapping and join/FK-PK logic needed to
    actually build it. Same list of tables as BIReport.business_use_cases;
    this tab shows the ETL detail instead of the business framing.
    """
    tables: list[BITableProposal] = []


class SynthesisOutput(BaseModel):
    bi_report: BIReport
    compliance_report: ComplianceReport
    knowledge_graph: KnowledgeGraphExport
    data_dump: DataDump
    source_target_mapping: SourceTargetMapping


# -------------------------------------------------------------------- job --

class FileProgress(BaseModel):
    filename: str
    category: FileCategory = FileCategory.UNKNOWN
    status: JobStatus = JobStatus.QUEUED
    error: str | None = None
    warnings: list[str] = []   # e.g. "NER/PII/Financial/Relation skipped: Qwen unreachable"
    detected_language: str | None = None
    translated: bool = False
    # Populated once this file finishes processing, from its DomainResult --
    # drives the "Extracted: N entities..." / "No info found" line in the
    # live file list so the user sees what each file actually yielded
    # without waiting for the final report.
    entities_found: int = 0
    relations_found: int = 0
    pii_found: int = 0
    financial_facts_found: int = 0
    tables_found: int = 0
    chunks_found: int = 0
    # Set once (at job start) by the importance-ranking pass (see
    # importance.py) when a job is large enough to run in batches --
    # 1-indexed processing order, None on jobs too small to batch.
    batch: int | None = None
    importance_reason: str | None = None


class AgentActivity(BaseModel):
    """One agent's execution span on one file (or "(all files)" for
    job-level stages like synthesis) -- powers the live agent-status panel
    so a slow/stuck agent is visible immediately instead of only inferred
    from the overall progress bar not moving, and so run-to-run durations
    can be compared later when tuning for a given deployment's document mix.
    """
    agent: str
    file: str
    status: str = "running"     # running | completed | failed | skipped
    started_at: datetime = Field(default_factory=_utc_now)
    finished_at: datetime | None = None
    duration_ms: int | None = None

    _normalize_tz = field_validator("started_at", "finished_at", mode="after")(_ensure_utc)


class BatchSummary(BaseModel):
    """Deterministic (no LLM call) checkpoint summary shown to the user
    after each batch, so they can decide whether to continue to the next
    batch or stop early with what's been processed so far."""
    batch_number: int
    file_count: int
    top_files: list[str] = []   # highest-ranked filenames in this batch, with why
    entities_found: int = 0
    relations_found: int = 0
    pii_found: int = 0
    financial_facts_found: int = 0


class Job(BaseModel):
    job_id: str = Field(default_factory=_id)
    name: str | None = None  # user-assigned label; falls back to job_id in the UI when unset
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = Field(default_factory=_utc_now)
    # updated_at is a generic "last modified for any reason" timestamp --
    # bumped by renames (routes_jobs.py) and every processing step alike
    # (job_manager.touch), so it keeps moving long after a job actually
    # finished. completed_at is set exactly once, the moment job.status
    # first becomes COMPLETE, specifically so the UI's "Time to complete"
    # stat (CompletionHero.tsx) reflects real processing duration instead
    # of however long it's been since the job was last touched for any
    # unrelated reason. None for jobs that haven't completed yet, or that
    # completed before this field existed.
    updated_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None
    files: list[FileProgress] = []
    progress_pct: float = 0.0
    result: SynthesisOutput | None = None
    error: str | None = None
    # Non-fatal service connectivity issues detected at job start (e.g.
    # "Cannot reach qwen model endpoint at http://... — affects: extraction").
    # Processing still proceeds with degraded functionality rather than
    # failing outright, but the user sees immediately why extraction/
    # synthesis/chat might be limited instead of watching progress stall
    # with no explanation.
    warnings: list[str] = []
    agent_activity: list[AgentActivity] = []
    # Batching (see importance.py + pipeline/job_manager.py): only used
    # once file_count exceeds settings.batch_threshold_files. total_batches
    # stays 1 for ordinary jobs, which never enter AWAITING_BATCH_CONFIRM.
    total_batches: int = 1
    current_batch: int = 0
    batch_summaries: list[BatchSummary] = []
    # True when the user stopped a batched job early via stop_batches()
    # rather than letting every batch run -- kept distinct from `warnings`
    # (which is specifically about degraded service connectivity) so the
    # frontend can show an accurate, differently-worded notice instead of
    # this appearing under "Service connectivity issues detected."
    stopped_early: bool = False

    _normalize_tz = field_validator("created_at", "updated_at", "completed_at", mode="after")(_ensure_utc)


# -------------------------------------------------------------------- chat --

class ChatMessage(BaseModel):
    role: str            # user | assistant
    content: str


class Citation(BaseModel):
    source_file: str
    chunk_text: str
    page: int | None = None
    score: float | None = None


class ChatRequest(BaseModel):
    job_id: str
    message: str
    history: list[ChatMessage] = []


class StructuredQueryResult(BaseModel):
    """Result of running the LLM-generated SQL from graph/structured_query.py
    against a job's structured-file SQLite store -- see ChatResponse.sql_used."""
    headers: list[str] = []
    rows: list[list[str]] = []
    row_count: int = 0
    truncated: bool = False


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation] = []
    uncertain: bool = False
    # True when this answer came from the local text-similarity fallback
    # (Neo4j unreachable) instead of full GraphRAG -- see graphrag.py.
    degraded: bool = False
    # Set to "qwen"/"kimi" when the chat role's configured backend was
    # unreachable and this answer was generated by the other backend
    # instead -- None means the normal configured chat backend answered.
    # Kept distinct from `degraded` (that's about retrieval quality, this
    # is about which model generated the answer).
    fallback_model: str | None = None
    # "structured" when this answer came from running real SQL against the
    # job's structured-file store (see graph/structured_query.py) instead
    # of GraphRAG vector search -- "graphrag" (the default) means the
    # normal retrieval path answered as usual.
    query_mode: str = "graphrag"
    sql_used: str | None = None
    structured_result: StructuredQueryResult | None = None
