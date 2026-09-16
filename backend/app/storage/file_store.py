"""Local filesystem storage for uploads and generated output artifacts.

Air-gapped deployments have no object storage service by default, so
everything lives under UPLOAD_DIR / OUTPUT_DIR (mounted as Docker volumes
in production so data survives container restarts).
"""
import csv
import io
import json
import logging
import os
import shutil
from pathlib import Path

import pandas as pd

from app.config import get_settings
from app.models.schemas import Job, SynthesisOutput, TableBlock

logger = logging.getLogger(__name__)


def _ensure_dirs() -> tuple[Path, Path]:
    settings = get_settings()
    upload_dir = Path(settings.upload_dir)
    output_dir = Path(settings.output_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir, output_dir


async def save_upload(job_id: str, filename: str, content: bytes) -> str:
    upload_dir, _ = _ensure_dirs()
    job_dir = upload_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name  # strip any path components
    dest = job_dir / safe_name
    dest.write_bytes(content)
    return str(dest)


def job_output_dir(job_id: str) -> Path:
    _, output_dir = _ensure_dirs()
    d = output_dir / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_upload_dir(job_id: str) -> Path:
    upload_dir, _ = _ensure_dirs()
    d = upload_dir / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def delete_job_files(job_id: str) -> None:
    """Removes everything a job owns on disk -- its uploaded source files
    and every generated output artifact (reports, CSVs, job_state.json).
    Safe to call even if one or both directories were never created."""
    upload_dir, output_dir = _ensure_dirs()
    shutil.rmtree(upload_dir / job_id, ignore_errors=True)
    shutil.rmtree(output_dir / job_id, ignore_errors=True)


def write_job_state(job: Job) -> None:
    """Snapshots the job's own state (status, per-file progress, agent
    activity, and -- once complete -- the full result) to disk on every
    meaningful change, so job history survives a backend restart instead
    of living only in the in-memory registry.

    Writes to a temp file in the same directory and atomically renames it
    over the real path (os.replace, atomic on POSIX) rather than writing
    the final path directly. Path.write_text() truncates the target
    before writing -- if the process is killed mid-write (a container
    restart racing a job's completion is exactly this: pulling new code
    requires restarting the backend, and this function fires on nearly
    every state change), the OLD, previously-good content is already gone
    and the new content is incomplete, leaving a job_state.json that
    fails to parse. load_all_job_states() already catches that per-file
    and skips it (so one bad file can't take down startup) -- but "skip"
    here means the job silently vanishes from history with nothing louder
    than a startup log line. A rename either lands the complete new file
    or leaves the old one untouched; there's no truncated-partial state
    for a crash to land in.
    """
    path = job_output_dir(job.job_id) / "job_state.json"
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp_path.write_text(job.model_dump_json(), encoding="utf-8")
    os.replace(tmp_path, path)


def load_all_job_states() -> list[Job]:
    _, output_dir = _ensure_dirs()
    jobs: list[Job] = []
    if not output_dir.exists():
        return jobs
    for job_dir in sorted(output_dir.iterdir()):
        state_path = job_dir / "job_state.json"
        if not state_path.exists():
            continue
        try:
            jobs.append(Job.model_validate_json(state_path.read_text(encoding="utf-8")))
        except Exception:
            logger.exception("Failed to load persisted job state from %s", state_path)
    return jobs


def write_json_report(job_id: str, output: SynthesisOutput) -> str:
    d = job_output_dir(job_id)
    path = d / "report.json"
    path.write_text(output.model_dump_json(indent=2), encoding="utf-8")
    return str(path)


def write_markdown_report(job_id: str, output: SynthesisOutput) -> str:
    d = job_output_dir(job_id)
    bi, comp, kg = output.bi_report, output.compliance_report, output.knowledge_graph

    lines = ["# Intelligence Extraction Report", "", "## High Level Analysis", ""]
    lines += ["### What's in the Dump", bi.executive_summary, ""]
    for idx in bi.corpus_overview:
        sources_str = f" ({', '.join(idx.sources)})" if idx.sources else ""
        lines.append(f"- **{idx.name}: {idx.value}** — {idx.basis}{sources_str}")
    lines.append("")
    lines += ["### File-by-File Breakdown", "",
              "| File | Category | Description | Entities | Relations | PII | Financial Facts | Tables | Chunks |",
              "|---|---|---|---|---|---|---|---|---|"]
    for fs in bi.file_breakdown:
        description = (fs.summary or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {fs.source_file} | {fs.category} | {description} | {fs.entities} | {fs.relations} | "
                      f"{fs.pii_findings} | {fs.financial_facts} | {fs.tables} | {fs.chunks} |")
    lines.append("")

    if bi.file_groups:
        lines += ["### Combined Simple Files",
                   "Files that yielded no entities, relations, PII findings, financial facts, or tables on "
                   "their own were combined by category into one summary row each, rather than listing every "
                   "near-identical \"nothing found\" file individually.", ""]
        for g in bi.file_groups:
            lines.append(f"- **{g.category}** ({g.file_count} files combined): "
                          f"{', '.join(g.member_files)}")
        lines.append("")

    lines += ["### Quality Check Stats",
              "Deterministic per-file quality assessment (Validator agent) -- for tabular files, every row of "
              "every table is scanned column-by-column for NULLs, garbage/placeholder values, duplicate rows, "
              "constant columns, and type consistency.", ""]
    for q in bi.data_quality:
        lines.append(f"- **{q.source_file}: {q.score}/100 ({q.completeness})**")
        for issue in q.issues:
            lines.append(f"  - {issue}")
        for t in q.tables:
            lines.append(f"  - Table `{t.table}`: {t.row_count} rows, {t.duplicate_rows} duplicate ({t.duplicate_rate:.0%})")
            for c in t.columns:
                lines.append(
                    f"    - `{c.column}` ({c.inferred_type}): "
                    f"{c.null_rate:.0%} NULL, {c.garbage_rate:.0%} garbage, "
                    f"{c.type_consistency:.0%} type-consistent"
                )
    lines.append("")

    lines += ["## Key Entities", *[f"- {e}" for e in bi.key_entities], ""]
    lines += ["## Financial Highlights", *[f"- {x}" for x in bi.financial_highlights], ""]
    lines += ["## Risks & Red Flags", *[f"- {x}" for x in bi.risks], ""]
    lines += ["## Market Signals", *[f"- {x}" for x in bi.market_signals], ""]
    lines += ["## Business Use Cases", "Proposed BI-layer tables, computed from column standardization and executed joins.", ""]
    for t in bi.business_use_cases:
        lines.append(f"- **{t.name}** — {t.purpose}")
        lines.append(f"  - Grain: {t.grain}")
        if t.join_logic:
            lines.append(f"  - Join: `{t.join_logic}` — {t.join_quality}")
    lines.append("")

    lines += ["## PII / Masking Report", f"Severity counts: {comp.severity_counts}", ""]
    lines += ["### PII Inventory"]
    for f in comp.pii_inventory[:200]:
        lines.append(f"- [{f.severity.upper()}] {f.category}: {f.value_redacted} ({f.source_file})")
    lines += ["", "### Compliance Gaps", *[f"- {x}" for x in comp.gap_flags], ""]
    lines += ["### Remediation", *[f"- {x}" for x in comp.remediation], ""]

    lines += ["## Knowledge Graph", f"Entities: {len(kg.entities)}, Relations: {len(kg.relations)}", ""]
    lines += ["### Top Relations"]
    for r in kg.relations[:100]:
        lines.append(f"- {r.source_entity} --[{r.relation_type}]--> {r.target_entity}")

    lines += ["", "## Data Dump", f"Files processed: {len(output.data_dump.files_processed)}",
              f"Total chunks indexed: {output.data_dump.chunk_count}"]

    lines += ["", "## Source to Target Mapping", "One mapping sheet per proposed BI table (see Business Use Cases).", ""]
    for t in output.source_target_mapping.tables:
        lines += [f"### {t.name}", ""]
        if t.join_logic:
            lines.append(f"**Join:** `{t.join_logic}`  \n**Join quality:** {t.join_quality}")
            lines.append("")
        lines += ["| Source Column | Source File | Target Column | Target File | Rule | Comments | Sample Value | Proposed Target Table Name |",
                  "|---|---|---|---|---|---|---|---|"]
        for c in t.columns:
            sample = c.sample_values[0] if c.sample_values else ""
            lines.append(
                f"| {c.source_column} | {c.source_file} | {c.target_column} | {t.target_file} | "
                f"{c.rule} | {c.comments or ''} | {sample} | {t.name} |"
            )
        lines.append("")

    content = "\n".join(lines)
    path = d / "report.md"
    path.write_text(content, encoding="utf-8")
    return str(path)


def write_pii_csv(job_id: str, output: SynthesisOutput) -> str:
    d = job_output_dir(job_id)
    path = d / "pii_inventory.csv"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["severity", "category", "value_redacted", "source_file", "location"])
    for f in output.compliance_report.pii_inventory:
        writer.writerow([f.severity, f.category, f.value_redacted, f.source_file, f.location or ""])
    path.write_text(buf.getvalue(), encoding="utf-8")
    return str(path)


def write_mapping_csv(job_id: str, output: SynthesisOutput) -> str:
    d = job_output_dir(job_id)
    path = d / "source_target_mapping.csv"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "source_column", "source_file", "target_column", "target_file",
        "rule", "comments", "sample_value", "proposed_target_table_name",
    ])
    for t in output.source_target_mapping.tables:
        for c in t.columns:
            writer.writerow([
                c.source_column, c.source_file, c.target_column, t.target_file,
                c.rule, c.comments or "",
                c.sample_values[0] if c.sample_values else "",
                t.name,
            ])
    path.write_text(buf.getvalue(), encoding="utf-8")
    return str(path)


def write_graph_json(job_id: str, output: SynthesisOutput) -> str:
    d = job_output_dir(job_id)
    path = d / "knowledge_graph.json"
    payload = {
        "nodes": [e.model_dump() for e in output.knowledge_graph.entities],
        "edges": [r.model_dump() for r in output.knowledge_graph.relations],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return str(path)


def write_tables_csv(job_id: str, tables: list[TableBlock]) -> list[str]:
    d = job_output_dir(job_id) / "tables"
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for t in tables:
        name = (t.caption or t.table_id).replace("/", "_").replace(" ", "_")[:60]
        path = d / f"{name}_{t.table_id}.csv"
        buf = io.StringIO()
        writer = csv.writer(buf)
        if t.headers:
            writer.writerow(t.headers)
        writer.writerows(t.rows)
        path.write_text(buf.getvalue(), encoding="utf-8")
        paths.append(str(path))
    return paths


def write_tables_parquet(job_id: str, source_file: str, tables: list[TableBlock]) -> list[str]:
    """Writes every structured table extracted from one file as its own
    Parquet file, under a job-scoped folder that accumulates across all of
    a job's files as they're processed.

    Called per-file during extraction (see agents/domain_managers.py) with
    doc.full_tables -- the same uncapped table set already mirrored into
    the structured-query store and the persistent dataset library -- not
    the 500-row-capped `tables` write_tables_csv uses once at the very
    end. So unlike the CSV export, this is the complete structured data
    for the job, not a preview.

    Best-effort per table: one malformed table (e.g. ragged rows pandas
    can't align to its header) is logged and skipped rather than losing
    every other table in the same file.
    """
    d = job_output_dir(job_id) / "tables_parquet"
    d.mkdir(parents=True, exist_ok=True)
    source_stem = Path(source_file).stem.replace("/", "_").replace(" ", "_")[:40]
    paths = []
    for t in tables:
        if not t.rows:
            continue
        name_base = (t.caption or t.table_id).replace("/", "_").replace(" ", "_")[:60]
        path = d / f"{source_stem}__{name_base}_{t.table_id}.parquet"
        try:
            columns = t.headers if t.headers and len(t.headers) == len(t.rows[0]) else None
            df = pd.DataFrame(t.rows, columns=columns)
            df.to_parquet(path, index=False, engine="pyarrow")
            paths.append(str(path))
        except Exception:
            logger.exception("Failed to write Parquet for table %s in %s", t.table_id, source_file)
    return paths


def list_tables_parquet(job_id: str) -> list[str]:
    d = job_output_dir(job_id) / "tables_parquet"
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.suffix == ".parquet")
