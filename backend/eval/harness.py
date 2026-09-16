"""LLM-as-judge eval harness for the L2 extraction agents.

Runs the real extraction pipeline (NER -> PII -> Financial -> Relations,
same order and inputs as agents/domain_managers.py uses in production) for
each case in cases.py, then asks a separate LLM call (the "judge", see
judge_prompts.py) to score each agent's output against the source text.

This is deliberately NOT scored against a hand-labeled answer key -- there
isn't one, and building/maintaining one was explicitly traded away for
being able to run this against any new sample document immediately. The
tradeoff: a judge score is an LLM's opinion, not ground truth, and can
vary between runs. Treat scores as a signal to investigate, not a hard
pass/fail gate.

Needs a working Kimi/Qwen backend (same one the app itself uses) --
run via: docker compose exec backend python -m eval.run
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.agents.extraction import run_financial, run_ner, run_pii, run_relations
from app.llm.client import get_llm_client
from app.models.schemas import Entity, FinancialFact, PIIFinding, Relation
from eval.cases import EvalCase
from eval.judge_prompts import (
    CONSISTENCY_JUDGE_SYSTEM, FINANCIAL_JUDGE_SYSTEM, NER_JUDGE_SYSTEM, PII_JUDGE_SYSTEM, RELATION_JUDGE_SYSTEM,
)

logger = logging.getLogger(__name__)

# Judge calls go through the same LLMClient role dispatch as everything
# else (see config.py's role_* settings) -- "synthesis" by default, since
# that role is typically mapped to the stronger/longer-context backend and
# judging benefits from that the same way report synthesis does. Override
# per-run if your deployment maps roles differently.
DEFAULT_JUDGE_ROLE = "synthesis"


@dataclass
class AgentScore:
    agent: str
    output_count: int
    completeness: float | None
    correctness: float | None
    hallucinated: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    notes: str = ""
    judge_error: str | None = None


@dataclass
class ConsistencyScore:
    fragmentation_score: float | None
    duplicate_entity_groups: list[list[str]] = field(default_factory=list)
    notes: str = ""
    judge_error: str | None = None


@dataclass
class CaseResult:
    case_id: str
    description: str
    ner: AgentScore
    pii: AgentScore
    financial: AgentScore
    relation: AgentScore
    consistency: ConsistencyScore | None  # only populated for multi-segment cases


async def _judge(role: str, system: str, source_text: str, output_json: str) -> dict:
    client = get_llm_client()
    user = f"SOURCE TEXT:\n{source_text}\n\nAGENT OUTPUT:\n{output_json}"
    return await client.complete_json(role, system, user)


async def _score_agent(agent: str, system: str, source_text: str, output_items: list, role: str) -> AgentScore:
    import json as _json

    output_json = _json.dumps([item.model_dump() for item in output_items], default=str)
    try:
        data = await _judge(role, system, source_text, output_json)
        return AgentScore(
            agent=agent,
            output_count=len(output_items),
            completeness=data.get("completeness"),
            correctness=data.get("correctness"),
            hallucinated=data.get("hallucinated", []),
            missed=data.get("missed", []),
            notes=data.get("notes", ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Judge call failed for agent=%s", agent)
        return AgentScore(agent=agent, output_count=len(output_items), completeness=None, correctness=None,
                           judge_error=str(exc))


async def _score_consistency(entities: list[Entity], source_text: str, role: str) -> ConsistencyScore:
    import json as _json

    output_json = _json.dumps([e.model_dump() for e in entities], default=str)
    try:
        data = await _judge(role, CONSISTENCY_JUDGE_SYSTEM, source_text, output_json)
        return ConsistencyScore(
            fragmentation_score=data.get("fragmentation_score"),
            duplicate_entity_groups=data.get("duplicate_entity_groups", []),
            notes=data.get("notes", ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Consistency judge call failed")
        return ConsistencyScore(fragmentation_score=None, judge_error=str(exc))


async def run_case(case: EvalCase, judge_role: str = DEFAULT_JUDGE_ROLE) -> CaseResult:
    """Runs the real production pipeline order -- NER first, then PII and
    Relations fed that file's entities, exactly like domain_managers.py --
    so this harness exercises the actual code path, not an idealized
    isolated-agent call."""
    entities: list[Entity] = await run_ner(case.text, case.source_file)
    pii: list[PIIFinding] = await run_pii(case.text, case.source_file, entities)
    facts: list[FinancialFact] = await run_financial(case.text, case.source_file)
    relations: list[Relation] = await run_relations(case.text, entities, case.source_file)

    ner_score, pii_score, fin_score, rel_score = await asyncio.gather(
        _score_agent("ner", NER_JUDGE_SYSTEM, case.text, entities, judge_role),
        _score_agent("pii", PII_JUDGE_SYSTEM, case.text, pii, judge_role),
        _score_agent("financial", FINANCIAL_JUDGE_SYSTEM, case.text, facts, judge_role),
        _score_agent("relation", RELATION_JUDGE_SYSTEM, case.text, relations, judge_role),
    )

    consistency = None
    if len(case.text) > 8000:  # only meaningful once a doc actually spans multiple LLM segments
        consistency = await _score_consistency(entities, case.text, judge_role)

    return CaseResult(
        case_id=case.case_id, description=case.description,
        ner=ner_score, pii=pii_score, financial=fin_score, relation=rel_score,
        consistency=consistency,
    )


async def run_harness(cases: list[EvalCase], judge_role: str = DEFAULT_JUDGE_ROLE) -> list[CaseResult]:
    results = []
    for case in cases:  # sequential, not gathered -- keeps per-case log output readable and avoids
        results.append(await run_case(case, judge_role))  # hammering the LLM backend with N cases at once
    return results


def format_report(results: list[CaseResult]) -> str:
    lines: list[str] = []
    for r in results:
        lines.append(f"\n=== {r.case_id} ===")
        lines.append(r.description)
        for score in (r.ner, r.pii, r.financial, r.relation):
            if score.judge_error:
                lines.append(f"  {score.agent:10s}: {score.output_count:3d} items | JUDGE FAILED: {score.judge_error}")
                continue
            lines.append(
                f"  {score.agent:10s}: {score.output_count:3d} items | "
                f"completeness={score.completeness} correctness={score.correctness}"
            )
            if score.hallucinated:
                lines.append(f"               hallucinated: {score.hallucinated}")
            if score.missed:
                lines.append(f"               missed: {score.missed}")
            if score.notes:
                lines.append(f"               notes: {score.notes}")
        if r.consistency:
            c = r.consistency
            if c.judge_error:
                lines.append(f"  consistency: JUDGE FAILED: {c.judge_error}")
            else:
                lines.append(f"  consistency: fragmentation_score={c.fragmentation_score} (0=good, 10=bad)")
                if c.duplicate_entity_groups:
                    lines.append(f"               unmerged duplicate groups: {c.duplicate_entity_groups}")
                if c.notes:
                    lines.append(f"               notes: {c.notes}")
    return "\n".join(lines)
