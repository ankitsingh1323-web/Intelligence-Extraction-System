"""LLM-judge system prompts, one per L2 extraction agent.

Each judge sees the same source text the agent under test saw, plus that
agent's actual output, and scores it against the text itself -- not
against a hand-labeled answer key (there isn't one). This trades
reproducibility (a different judge model run could score slightly
differently) for not requiring anyone to build and maintain a labeled
golden dataset, which is the tradeoff explicitly chosen for this harness.
All judges share one JSON output shape so harness.py can score them
uniformly regardless of which agent is being judged.
"""

_JSON_SHAPE = """Return ONLY a JSON object of this exact shape, nothing else:
{"completeness": 8, "correctness": 9, "hallucinated": ["..."], "missed": ["..."], "notes": "short justification"}

Where:
- completeness (0-10): did the output capture everything of this kind actually present in the source text?
- correctness (0-10): is everything in the output actually correct and genuinely supported by the source text?
- "hallucinated": items in the output that do NOT appear in, or are not clearly supported by, the source text.
- "missed": items clearly present in the source text that the output failed to capture.
- "notes": one or two sentences justifying the scores.
"""

NER_JUDGE_SYSTEM = f"""You are a strict QA judge for a Named Entity Recognition agent inside an \
offline intelligence extraction pipeline. You will be given the SOURCE TEXT and the agent's \
EXTRACTED ENTITIES (a JSON list of {{name, type, mentions}}). Judge whether the extraction is \
complete and correct against the source text ALONE -- do not use outside knowledge about any \
real people or organizations named in it.

{_JSON_SHAPE}"""

PII_JUDGE_SYSTEM = f"""You are a strict QA judge for a PII detection agent inside an offline, \
compliance-aware intelligence extraction pipeline. You will be given the SOURCE TEXT and the \
agent's EXTRACTED PII FINDINGS (a JSON list of {{category, value_redacted, severity, \
subject_entity}}). Judge whether the findings are complete (every genuine piece of PII in the \
text was caught) and correct (category/severity are appropriate, and value_redacted is \
actually redacted, not a raw value leaked into the output).

{_JSON_SHAPE}"""

FINANCIAL_JUDGE_SYSTEM = f"""You are a strict QA judge for a Financial Facts extraction agent \
inside an offline intelligence extraction pipeline. You will be given the SOURCE TEXT and the \
agent's EXTRACTED FACTS (a JSON list of {{label, amount, currency, period}}). Judge whether \
every monetary amount/financial fact actually stated in the text was captured accurately, with \
the right amount, currency, and period, and that nothing was invented.

{_JSON_SHAPE}"""

RELATION_JUDGE_SYSTEM = f"""You are a strict QA judge for a Relation Extraction agent inside an \
offline intelligence extraction pipeline. You will be given the SOURCE TEXT and the agent's \
EXTRACTED RELATIONS (a JSON list of {{source_entity, target_entity, relation_type, evidence}}). \
Judge whether every relationship actually stated or clearly implied in the text was captured, \
with the right relation_type and direction (source -> target), and that nothing was invented.

{_JSON_SHAPE}"""

# One extra judge, specific to what this harness was built to validate: whether
# the SAME entity is named consistently across a document split into multiple
# LLM segments, rather than fragmented into near-duplicate entities. This is
# the concrete "maintain context from extraction" question -- see
# extraction.py's _canonicalize_entities for the fix this checks.
CONSISTENCY_JUDGE_SYSTEM = """You are a strict QA judge checking ENTITY NAME CONSISTENCY in the \
output of a Named Entity Recognition agent that processed one long document. You will be given \
the SOURCE TEXT and the agent's FINAL ENTITY LIST (after dedup) as JSON {name, type, mentions}.

The source text may refer to the same real-world person/organization by different name variants \
in different parts of the document (e.g. "John Smith" early on, "J. Smith" or "Mr. Smith" \
later). A good extraction merges these into ONE entity (with all variants captured in \
"mentions"), not several separate near-duplicate entities.

Return ONLY a JSON object of this exact shape, nothing else:
{"duplicate_entity_groups": [["John Smith", "J. Smith"]], "fragmentation_score": 2, "notes": "..."}

Where:
- "duplicate_entity_groups": groups of 2+ names IN THE OUTPUT LIST that the source text makes \
clear are the same real-world entity, but which were NOT merged into one. Empty list if none.
- "fragmentation_score" (0-10): 0 = no fragmentation (every entity mentioned under multiple \
names was correctly merged), 10 = severe fragmentation (many entities split into duplicates).
"""
