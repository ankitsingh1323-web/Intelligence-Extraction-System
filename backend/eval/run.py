"""CLI entrypoint for the extraction-agent eval harness.

Usage (from inside the backend container, where the real Kimi/Qwen
backends configured in .env are reachable):

    docker compose exec backend python -m eval.run
    docker compose exec backend python -m eval.run --case long_multi_segment_entity_consistency
    docker compose exec backend python -m eval.run --judge-role extraction

See harness.py's module docstring for what's actually being measured and
why there's no labeled answer key.
"""
import argparse
import asyncio
import logging

from eval.cases import ALL_CASES
from eval.harness import DEFAULT_JUDGE_ROLE, format_report, run_harness

logging.basicConfig(level=logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-as-judge eval harness for the extraction agents.")
    parser.add_argument(
        "--case", action="append", dest="case_ids", default=None,
        help="Case id to run (repeatable). Omit to run all cases. See eval/cases.py for ids.",
    )
    parser.add_argument(
        "--judge-role", default=DEFAULT_JUDGE_ROLE,
        help=f"LLMClient role the judge calls use (default: {DEFAULT_JUDGE_ROLE}).",
    )
    args = parser.parse_args()

    cases = ALL_CASES
    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [c for c in ALL_CASES if c.case_id in wanted]
        missing = wanted - {c.case_id for c in cases}
        if missing:
            available = ", ".join(c.case_id for c in ALL_CASES)
            raise SystemExit(f"Unknown case id(s): {sorted(missing)}. Available: {available}")

    results = asyncio.run(run_harness(cases, judge_role=args.judge_role))
    print(format_report(results))


if __name__ == "__main__":
    main()
