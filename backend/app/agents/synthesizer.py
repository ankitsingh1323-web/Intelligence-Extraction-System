"""Synthesiser Agent — merges every DomainResult into the final output bundle.

Runs on the "synthesis" role (Kimi2 by default) since it needs to reason
over the combined findings from every file and has the longest effective
context requirement in the pipeline.
"""
import json
import logging

from app.agents.mapping_agent import build_bi_tables
from app.llm.client import get_llm_client
from app.models.schemas import (
    BIReport,
    BusinessIndex,
    ComplianceReport,
    DataDump,
    DomainResult,
    Entity,
    FileGroup,
    FileStats,
    Job,
    KnowledgeGraphExport,
    Relation,
    SourceTargetMapping,
    SynthesisOutput,
)
from app.pipeline.agent_tracker import finish_activity, start_activity

logger = logging.getLogger(__name__)

SYNTHESIS_SYSTEM = """You are the Synthesiser Agent — the final stage of a multi-agent \
intelligence extraction pipeline. You are given per-file extraction summaries, entity \
counts, PII severity counts, and financial facts gathered across every uploaded \
document. Produce a single cross-document executive briefing.

Return ONLY a JSON object of this exact shape, nothing else:
{
  "executive_summary": "3-6 sentence narrative synthesis across all files",
  "key_entities": ["most important entity names, max 15"],
  "financial_highlights": ["short bullet strings", "..."],
  "risks": ["short bullet strings describing anomalies/red flags", "..."],
  "market_signals": ["short bullet strings, business-relevant observations", "..."],
  "gap_flags": ["short bullet strings: compliance gaps e.g. missing consent basis for PII found", "..."],
  "remediation": ["short bullet strings: concrete remediation actions", "..."]
}

Base every claim strictly on the provided findings. Do not invent facts.
"""

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _dedup_entities(entities: list[Entity]) -> list[Entity]:
    merged: dict[tuple[str, str], Entity] = {}
    for e in entities:
        key = (e.name.strip().lower(), e.type)
        if key in merged:
            merged[key].mentions = list(set(merged[key].mentions + e.mentions))
            merged[key].confidence = max(merged[key].confidence, e.confidence)
        else:
            merged[key] = e.model_copy()
    return list(merged.values())


def _dedup_relations(relations: list[Relation]) -> list[Relation]:
    seen: dict[tuple[str, str, str], Relation] = {}
    for r in relations:
        key = (r.source_entity.lower(), r.relation_type, r.target_entity.lower())
        if key not in seen:
            seen[key] = r
    return list(seen.values())


def _build_context_digest(results: list[DomainResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"\n### File: {r.source_file} (domain: {r.domain})")
        if r.summary:
            lines.append(f"Summary: {r.summary}")
        if r.entities:
            top_entities = sorted(r.entities, key=lambda e: -e.confidence)[:10]
            lines.append("Top entities: " + ", ".join(f"{e.name} ({e.type})" for e in top_entities))
        if r.relations:
            lines.append(f"Relations found: {len(r.relations)}")
        if r.pii_findings:
            counts: dict[str, int] = {}
            for f in r.pii_findings:
                counts[f.severity] = counts.get(f.severity, 0) + 1
            lines.append(f"PII findings by severity: {counts}")
        if r.financial_facts:
            facts_preview = [f"{f.label}={f.amount}{f.currency or ''}" for f in r.financial_facts[:8]]
            lines.append("Financial facts: " + "; ".join(facts_preview))
        if r.errors:
            lines.append(f"Processing warnings: {r.errors}")
    return "\n".join(lines)


def _cross_document_entity_index(results: list[DomainResult]) -> BusinessIndex:
    """% of entities that appear in more than one file -- joins NER output
    across every document rather than reading any single file in isolation.
    """
    name_to_files: dict[str, set[str]] = {}
    for r in results:
        for e in r.entities:
            name_to_files.setdefault(e.name.strip().lower(), set()).add(r.source_file)
    total = len(name_to_files)
    linked_files = sorted({f for files in name_to_files.values() if len(files) > 1 for f in files})
    linked = sum(1 for files in name_to_files.values() if len(files) > 1)
    pct = round(100 * linked / total) if total else 0
    return BusinessIndex(
        name="Cross-Document Entity Linkage",
        value=f"{pct}% ({linked} of {total})",
        basis="Share of entities whose name appears in more than one uploaded file, joining NER "
              "output across every document to find where records actually overlap.",
        sources=linked_files[:10],
    )


def _counterparty_concentration_index(results: list[DomainResult]) -> BusinessIndex | None:
    """Attributes each file's financial amount evenly across the ORG entities
    mentioned in that same file, then finds which single counterparty holds
    the largest share of the combined total -- links financial_facts to
    entities via co-occurrence since no direct FK exists between them.
    """
    entity_totals: dict[str, float] = {}
    entity_files: dict[str, set[str]] = {}
    grand_total = 0.0
    for r in results:
        file_amount = sum(f.amount for f in r.financial_facts if f.amount and not f.label.upper().startswith("ANOMALY"))
        if file_amount <= 0:
            continue
        org_names = {e.name for e in r.entities if e.type == "ORG"}
        if not org_names:
            continue
        grand_total += file_amount
        share = file_amount / len(org_names)
        for name in org_names:
            entity_totals[name] = entity_totals.get(name, 0.0) + share
            entity_files.setdefault(name, set()).add(r.source_file)
    if not entity_totals or grand_total <= 0:
        return None
    top_name, top_amount = max(entity_totals.items(), key=lambda kv: kv[1])
    pct = round(100 * top_amount / grand_total)
    return BusinessIndex(
        name="Counterparty Concentration",
        value=f"{pct}% — {top_name}",
        basis="Share of total extracted financial amount attributable to the single most "
              "financially-linked organization, joining financial facts to co-occurring entities per file.",
        sources=sorted(entity_files.get(top_name, [])),
    )


def _anomaly_index(results: list[DomainResult]) -> BusinessIndex:
    anomalies = [(r.source_file, f) for r in results for f in r.financial_facts
                 if f.label.upper().startswith("ANOMALY")]
    return BusinessIndex(
        name="Financial Anomalies Detected",
        value=str(len(anomalies)),
        basis="Anomaly-flagged financial facts (e.g. duplicate transactions, round-tripping) found "
              "by linking amounts and labels across every file's financial extraction.",
        sources=sorted({f for f, _ in anomalies}),
    )


def _relation_density_index(all_entities: list[Entity], all_relations: list[Relation]) -> BusinessIndex:
    n = len(all_entities)
    density = round(len(all_relations) / n, 2) if n else 0.0
    return BusinessIndex(
        name="Entity Graph Connectivity",
        value=f"{density} relations / entity",
        basis="Ratio of extracted relationships to unique entities across the combined knowledge "
              "graph — higher values mean the document set is more cross-referenced, not just a pile "
              "of unrelated records.",
        sources=[],
    )


def _pii_exposure_index(results: list[DomainResult]) -> BusinessIndex:
    weights = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    per_file: dict[str, int] = {}
    total = 0
    for r in results:
        for f in r.pii_findings:
            w = weights.get(f.severity, 1)
            total += w
            per_file[r.source_file] = per_file.get(r.source_file, 0) + w
    top_files = sorted(per_file, key=lambda k: -per_file[k])[:5]
    return BusinessIndex(
        name="PII Exposure Score",
        value=str(total),
        basis="Severity-weighted PII count (critical=4, high=3, medium=2, low=1), summed across "
              "every file into one comparable exposure score.",
        sources=top_files,
    )


def _compliance_coverage_index(results: list[DomainResult]) -> BusinessIndex:
    files_with_risk = {r.source_file for r in results
                        if any(f.severity in ("critical", "high") for f in r.pii_findings)}
    total_files = len({r.source_file for r in results})
    pct = round(100 * len(files_with_risk) / total_files) if total_files else 0
    return BusinessIndex(
        name="High-Risk File Coverage",
        value=f"{pct}% ({len(files_with_risk)} of {total_files})",
        basis="Share of uploaded files containing at least one critical- or high-severity PII "
              "finding, computed by linking PII detections back to their source files.",
        sources=sorted(files_with_risk)[:10],
    )


def compute_corpus_overview(
    results: list[DomainResult], all_entities: list[Entity], all_relations: list[Relation]
) -> list[BusinessIndex]:
    """Corpus-level KPIs for the High Level Analysis tab's "what's in the
    dump" section. Every index here is computed deterministically -- not
    asked of the LLM -- specifically because each one requires joining two
    or more data types/files together (financial facts <-> entities, PII
    <-> source files, entities <-> entities via relations). Unaffected by
    the synthesis model being down, since none of them depend on an LLM call.
    """
    indices = [
        _cross_document_entity_index(results),
        _anomaly_index(results),
        _relation_density_index(all_entities, all_relations),
        _pii_exposure_index(results),
        _compliance_coverage_index(results),
    ]
    concentration = _counterparty_concentration_index(results)
    if concentration:
        indices.append(concentration)
    return indices


def compute_file_breakdown(results: list[DomainResult]) -> list[FileStats]:
    """Table/file-wise info for the High Level Analysis tab -- what came
    out of each individual file, computed directly from its DomainResult.
    """
    return [
        FileStats(
            source_file=r.source_file,
            category=r.domain,
            entities=len(r.entities),
            relations=len(r.relations),
            pii_findings=len(r.pii_findings),
            financial_facts=len(r.financial_facts),
            tables=len(r.tables),
            chunks=len(r.chunks),
            summary=r.summary,
        )
        for r in results
    ]


def _is_simple(stats: FileStats) -> bool:
    """No entities, relations, PII findings, financial facts, or tables --
    literally nothing for the file-by-file breakdown to show about this
    file beyond its name. (chunks is deliberately excluded: even a
    "simple" file still gets indexed for chat, that's not a sign it had
    distinguishing content.)"""
    return (stats.entities == 0 and stats.relations == 0 and stats.pii_findings == 0
            and stats.financial_facts == 0 and stats.tables == 0)


def compute_file_groups(file_breakdown: list[FileStats]) -> tuple[list[FileStats], list[FileGroup]]:
    """Splits a full file_breakdown into (files still listed individually,
    combined groups) for the High Level Analysis tab. A file with zero
    extraction signal (see _is_simple) carries no distinguishing
    information worth its own row -- once 2+ such files share a category,
    they're collapsed into one FileGroup that names exactly which files
    were combined, rather than the table filling up with near-identical
    "nothing found" rows for e.g. a dozen empty log files. A single simple
    file has nothing to combine WITH, so it's left as an ordinary row --
    "combined" implies 2 or more.
    """
    simple_by_category: dict[str, list[FileStats]] = {}
    individual: list[FileStats] = []
    for stats in file_breakdown:
        if _is_simple(stats):
            simple_by_category.setdefault(stats.category, []).append(stats)
        else:
            individual.append(stats)

    groups: list[FileGroup] = []
    for category, members in simple_by_category.items():
        if len(members) < 2:
            individual.extend(members)
            continue
        groups.append(FileGroup(
            category=category,
            member_files=[m.source_file for m in members],
            file_count=len(members),
            entities=sum(m.entities for m in members),
            relations=sum(m.relations for m in members),
            pii_findings=sum(m.pii_findings for m in members),
            financial_facts=sum(m.financial_facts for m in members),
            tables=sum(m.tables for m in members),
            chunks=sum(m.chunks for m in members),
        ))
    groups.sort(key=lambda g: (-g.file_count, g.category))
    return individual, groups


async def synthesize(results: list[DomainResult], skip_llm: bool = False, job: Job | None = None) -> SynthesisOutput:
    """skip_llm: set when the job's preflight check already found the
    synthesis backend unreachable -- skips straight to the fallback report
    instead of burning through several minutes of doomed retries first.

    job: optional -- when passed, the Mapping Agent (BI table proposals),
    Insight Agent (deterministic cross-document indices), and the LLM
    synthesis call each report their own start/finish onto
    job.agent_activity, so they show up as distinct steps in the live
    agent status panel instead of being invisible inside one opaque "BI
    Synthesizer" span. None (e.g. in isolated tests) simply skips
    tracking, same convention as domain_managers.process_file.
    """
    client = get_llm_client()

    all_entities = _dedup_entities([e for r in results for e in r.entities])
    all_relations = _dedup_relations([rel for r in results for rel in r.relations])
    all_pii = [f for r in results for f in r.pii_findings]
    all_tables_files = [r.source_file for r in results]
    chunk_count = sum(len(r.chunks) for r in results)

    digest = _build_context_digest(results)
    llm_activity = start_activity(job, "BI Synthesizer", "(all files)")
    if skip_llm:
        raw = {}
        finish_activity(llm_activity, "skipped")
    else:
        try:
            # A large nested JSON object (BIReport-shaped), and the most
            # complex single output this app asks any model for -- given a
            # generous starting budget rather than relying purely on
            # complete()'s automatic doubling (see
            # LLMReasoningBudgetExceededError) to get there after a wasted
            # round trip.
            raw = await client.complete_json("synthesis", SYNTHESIS_SYSTEM, digest, max_tokens=8192)
            finish_activity(llm_activity, "completed")
        except Exception:
            logger.exception("Synthesis LLM call failed")
            raw = {}
            finish_activity(llm_activity, "failed")

    fallback_summary = (
        f"Synthesis model unreachable — showing per-file findings only. "
        f"{len(all_entities)} entities and {len(all_pii)} PII findings were still extracted "
        f"where the extraction model was reachable."
        if skip_llm else
        "Synthesis could not be generated (LLM unavailable). See per-file summaries below."
    )

    mapping_activity = start_activity(job, "Mapping Agent", "(all files)")
    try:
        bi_tables = build_bi_tables(results)
        finish_activity(mapping_activity, "completed")
    except Exception:
        logger.exception("Mapping Agent failed to build BI table proposals")
        bi_tables = []
        finish_activity(mapping_activity, "failed")
    source_target_mapping = SourceTargetMapping(tables=bi_tables)

    insight_activity = start_activity(job, "Insight Agent", "(all files)")
    try:
        corpus_overview = compute_corpus_overview(results, all_entities, all_relations)
        finish_activity(insight_activity, "completed")
    except Exception:
        logger.exception("Insight Agent failed to compute cross-document indices")
        corpus_overview = []
        finish_activity(insight_activity, "failed")

    individual_files, file_groups = compute_file_groups(compute_file_breakdown(results))

    bi_report = BIReport(
        executive_summary=raw.get("executive_summary") or fallback_summary,
        key_entities=raw.get("key_entities") or [e.name for e in all_entities[:15]],
        financial_highlights=raw.get("financial_highlights") or [],
        risks=raw.get("risks") or [],
        market_signals=raw.get("market_signals") or [],
        corpus_overview=corpus_overview,
        file_breakdown=individual_files,
        file_groups=file_groups,
        business_use_cases=bi_tables,
        data_quality=[r.quality for r in results if r.quality is not None],
    )

    severity_counts: dict[str, int] = {}
    for f in all_pii:
        severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1

    compliance_report = ComplianceReport(
        pii_inventory=sorted(all_pii, key=lambda f: SEVERITY_ORDER.get(f.severity, 4)),
        severity_counts=severity_counts,
        gap_flags=raw.get("gap_flags") or [],
        remediation=raw.get("remediation") or [],
    )

    knowledge_graph = KnowledgeGraphExport(entities=all_entities, relations=all_relations)

    data_dump = DataDump(
        tables=[],  # full tables are available per-job via the outputs API, not duplicated here
        files_processed=all_tables_files,
        chunk_count=chunk_count,
    )

    return SynthesisOutput(
        bi_report=bi_report,
        compliance_report=compliance_report,
        knowledge_graph=knowledge_graph,
        data_dump=data_dump,
        source_target_mapping=source_target_mapping,
    )
