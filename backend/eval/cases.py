"""Sample documents the harness runs the extraction agents against.

No golden/labeled answer key -- see judge_prompts.py's module docstring
for why. Each case is picked to exercise something specific: a normal
single-segment document as a baseline, a document that specifically
triggers PII_SYSTEM's subject_entity attribution and RELATION_SYSTEM's
kinship rules, and one long multi-segment document built to test whether
the SAME person stays one entity across LLM segment boundaries (see
extraction.py's _canonicalize_entities).
"""
from dataclasses import dataclass


@dataclass
class EvalCase:
    case_id: str
    source_file: str
    text: str
    description: str


VENDOR_CONTRACT = EvalCase(
    case_id="vendor_contract",
    source_file="vendor_agreement.txt",
    description="Short single-segment business contract -- baseline quality check for all four agents.",
    text="""Vendor Services Agreement — Meridian Steel Supply

This Vendor Services Agreement ("Agreement") is entered into as of January 3, 2026, between
Northfield Manufacturing Inc. ("Client") and Meridian Steel Supply ("Vendor"), located at 4410
Industrial Pkwy, Canton, OH.

1. Scope of Services
Vendor shall supply raw steel coil and fabricated components per the attached specification
sheet, delivered to Client's Canton facility on a net-30 payment basis.

2. Payment Terms
Outstanding balance as of this agreement: $18,420.50. Late payments accrue interest at 1.5%
monthly. Primary billing contact: Dana Whitfield, ap@meridiansteel.com, phone 555-0142.

3. Prior Quarter Reference
Q3 2025 total spend with this vendor was $54,900.00 USD across 6 shipments.

4. Term
This Agreement remains in effect through December 31, 2026, renewing annually unless
terminated with 60 days written notice. Signed on behalf of Northfield Manufacturing Inc. by
Marcus Ilic, VP of Procurement.
""",
)

PASSPORT_KINSHIP = EvalCase(
    case_id="passport_kinship",
    source_file="passport_record.txt",
    description="Identification-document-style text -- exercises PII subject attribution and RELATION_SYSTEM's kinship rules.",
    text="""IDENTIFICATION RECORD

Full Name: Priya Ramanathan
Date of Birth: 14 March 1991
Passport Number: X4471829
National ID: 784-1991-7765432-1
Mother's Name: Lakshmi Ramanathan
Father's Name: Suresh Ramanathan
Spouse Name: Arjun Mehta
Address: 22 Marina Walk, Apt 1408, Dubai, UAE
Phone: 555-0299
Email: priya.ramanathan@mailbox.com

This record confirms Priya Ramanathan is currently employed by Blackwell Consulting as a
Senior Legal Analyst, reporting to Daniel Petrov. Annual salary on file: $112,000.00.
""",
)


def _build_long_multi_segment_case() -> EvalCase:
    """Built, not hand-typed, so it reliably crosses SEGMENT_CHARS (8000)
    at least twice -- each "section" below pads past 8500 characters, and
    each section refers to the SAME person under a DIFFERENT name variant,
    positioned near the start of its section so it reliably lands in a
    different LLM segment than the others. See judge_prompts.py's
    CONSISTENCY_JUDGE_SYSTEM -- this is what that judge scores.
    """
    filler_sentence = (
        "The quarterly facilities review continued without incident, and staff "
        "noted no material changes to the standard operating procedure. "
    )

    def section(name_variant: str, extra_sentence: str) -> str:
        header = f"{name_variant} filed the incident report on-site. {extra_sentence} "
        body = filler_sentence * 130  # ~130 * ~135 chars ~= 17,000 chars, safely > SEGMENT_CHARS
        return header + body

    sections = [
        section("Renata Souza", "She has worked at the Canton facility since 2019."),
        section("R. Souza", "Her badge access covers the east and north wings."),
        section("Ms. Souza", "She escalated the matter to the regional safety office."),
    ]
    text = "\n\n".join(sections)
    return EvalCase(
        case_id="long_multi_segment_entity_consistency",
        source_file="incident_log.txt",
        description=(
            "Long document (multiple 8000-char LLM segments) where the same person is named "
            "'Renata Souza', 'R. Souza', and 'Ms. Souza' in different sections -- tests whether "
            "the NER agent's final entity list merges these into one entity instead of three."
        ),
        text=text,
    )


LONG_MULTI_SEGMENT = _build_long_multi_segment_case()

ALL_CASES: list[EvalCase] = [VENDOR_CONTRACT, PASSPORT_KINSHIP, LONG_MULTI_SEGMENT]
