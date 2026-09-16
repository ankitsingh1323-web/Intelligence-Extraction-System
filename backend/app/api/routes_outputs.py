import io
import logging
import mimetypes
from pathlib import Path

import pypdfium2 as pdfium
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from app.agents.schema_catalog import build_schema_catalog
from app.models.schemas import (
    BIReport,
    ComplianceReport,
    DataDumpTable,
    FileCategory,
    JobStatus,
    KnowledgeGraphExport,
    SourceDocSummary,
    SourceTargetMapping,
    StructuredDataCatalog,
    TableBlock,
)
from app.pipeline.job_manager import get_job_manager
from app.storage import dataset_library
from app.storage.file_store import job_output_dir, job_upload_dir, list_tables_parquet

logger = logging.getLogger(__name__)

router = APIRouter(tags=["outputs"])

_PREVIEW_SCALE = 1.5  # PDF page-1 thumbnail render scale -- readable without being huge


def _completed_job(job_id: str):
    job = get_job_manager().get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    if job.status != JobStatus.COMPLETE or job.result is None:
        raise HTTPException(409, f"Job is not complete yet (status={job.status.value}).")
    return job


@router.get("/outputs/{job_id}/bi-report", response_model=BIReport)
async def get_bi_report(job_id: str) -> BIReport:
    return _completed_job(job_id).result.bi_report


@router.get("/outputs/{job_id}/compliance-report", response_model=ComplianceReport)
async def get_compliance_report(job_id: str) -> ComplianceReport:
    return _completed_job(job_id).result.compliance_report


@router.get("/outputs/{job_id}/knowledge-graph", response_model=KnowledgeGraphExport)
async def get_knowledge_graph(job_id: str) -> KnowledgeGraphExport:
    return _completed_job(job_id).result.knowledge_graph


@router.get("/outputs/{job_id}/source-target-mapping", response_model=SourceTargetMapping)
async def get_source_target_mapping(job_id: str) -> SourceTargetMapping:
    return _completed_job(job_id).result.source_target_mapping


def _category_of(domain: str) -> FileCategory:
    try:
        return FileCategory(domain)
    except ValueError:
        return FileCategory.UNKNOWN


@router.get("/outputs/{job_id}/data-dump/tables", response_model=list[DataDumpTable])
async def get_data_dump_tables(job_id: str) -> list[DataDumpTable]:
    _completed_job(job_id)
    results = get_job_manager().get_domain_results(job_id)
    category_by_file = {r.source_file: _category_of(r.domain) for r in results}
    return [
        DataDumpTable(**t.model_dump(), source_file=r.source_file, category=category_by_file[r.source_file])
        for r in results
        for t in r.tables
    ]


@router.get("/outputs/{job_id}/data-dump/documents", response_model=list[SourceDocSummary])
async def get_data_dump_documents(job_id: str) -> list[SourceDocSummary]:
    """One card per source file for the Data Dump tab -- entity/PII counts
    and a text excerpt, all computed deterministically from this file's
    already-processed DomainResult (no LLM call, no re-parsing)."""
    _completed_job(job_id)
    results = get_job_manager().get_domain_results(job_id)
    out = []
    for r in results:
        category = _category_of(r.domain)
        out.append(SourceDocSummary(
            source_file=r.source_file,
            category=category,
            has_preview=category in (FileCategory.PDF, FileCategory.IMAGE),
            entities=sorted({e.name for e in r.entities})[:12],
            pii_types=sorted({p.category for p in r.pii_findings}),
            text_excerpt=r.chunks[0].text[:280] if r.chunks else None,
        ))
    return out


@router.get("/outputs/{job_id}/data-dump/schema", response_model=StructuredDataCatalog)
async def get_data_dump_schema(job_id: str) -> StructuredDataCatalog:
    """Where this job's structured (CSV/Excel/Database) tables actually
    live on disk, plus an Oracle-DESC-style schema catalog with
    same-shape tables UNIONed into one combined structure (see
    agents/schema_catalog.py). Reads storage/dataset_library.py's
    persistent library scoped to just this job's saved tables -- not
    structured_store.py's per-job scratch store, which is chat's SQL
    input and gets deleted with the job rather than being the durable
    "where is this saved" record."""
    _completed_job(job_id)
    datasets = dataset_library.list_datasets(job_id=job_id)
    return StructuredDataCatalog(
        library_path=str(dataset_library.library_db_path()),
        groups=build_schema_catalog(datasets),
    )


@router.get("/outputs/{job_id}/files/preview/{filename}")
async def preview_source_file(job_id: str, filename: str) -> Response:
    """Serves a viewable image for a source file: the raw bytes for an
    image upload, or a page-1 render for a PDF (same pypdfium2 render path
    the PDF parser's OCR fallback uses, just for display here rather than
    OCR input). Lets the Data Dump tab show "what did the OCR/PDF agent
    actually see" next to what it extracted.
    """
    _completed_job(job_id)
    path = job_upload_dir(job_id) / Path(filename).name
    if not path.exists():
        raise HTTPException(404, "Source file not found on disk.")

    content_type, _ = mimetypes.guess_type(path.name)
    if content_type and content_type.startswith("image/"):
        return FileResponse(path, media_type=content_type)

    if path.suffix.lower() == ".pdf":
        try:
            pdf = pdfium.PdfDocument(str(path))
            try:
                page = pdf[0]
                try:
                    bitmap = page.render(scale=_PREVIEW_SCALE)
                    try:
                        image = bitmap.to_pil().convert("RGB")
                        buf = io.BytesIO()
                        image.save(buf, format="PNG")
                        return Response(content=buf.getvalue(), media_type="image/png")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                pdf.close()
        except Exception:
            logger.exception("PDF preview render failed for %s", path)
            raise HTTPException(500, "Could not render a preview for this PDF.")

    raise HTTPException(404, "No preview available for this file type.")


@router.get("/outputs/{job_id}/files/{artifact}")
async def download_artifact(job_id: str, artifact: str) -> FileResponse:
    _completed_job(job_id)
    allowed = {
        "report.json": "application/json",
        "report.md": "text/markdown",
        "pii_inventory.csv": "text/csv",
        "knowledge_graph.json": "application/json",
        "source_target_mapping.csv": "text/csv",
    }
    if artifact not in allowed:
        raise HTTPException(404, "Unknown artifact.")
    path = job_output_dir(job_id) / artifact
    if not path.exists():
        raise HTTPException(404, "Artifact not found on disk.")
    return FileResponse(path, media_type=allowed[artifact], filename=artifact)


@router.get("/outputs/{job_id}/files/tables/{table_filename}")
async def download_table(job_id: str, table_filename: str) -> FileResponse:
    _completed_job(job_id)
    path = job_output_dir(job_id) / "tables" / Path(table_filename).name
    if not path.exists():
        raise HTTPException(404, "Table file not found.")
    return FileResponse(path, media_type="text/csv", filename=table_filename)


@router.get("/outputs/{job_id}/data-dump/parquet-tables", response_model=list[str])
async def list_parquet_tables(job_id: str) -> list[str]:
    """Filenames of this job's complete structured data, one Parquet file
    per table (see storage/file_store.write_tables_parquet) -- written
    per-file during extraction from the uncapped table set, so this list
    can be non-empty even before the job finishes; unlike the CSV table
    export it isn't capped to a 500-row preview."""
    _completed_job(job_id)
    return list_tables_parquet(job_id)


@router.get("/outputs/{job_id}/files/tables-parquet/{table_filename}")
async def download_table_parquet(job_id: str, table_filename: str) -> FileResponse:
    _completed_job(job_id)
    path = job_output_dir(job_id) / "tables_parquet" / Path(table_filename).name
    if not path.exists():
        raise HTTPException(404, "Parquet table file not found.")
    return FileResponse(path, media_type="application/octet-stream", filename=table_filename)
