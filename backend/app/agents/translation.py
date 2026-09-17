"""Translator Agent -- language-detection + Qwen-based translation, run as
a preprocessing step between parsing and extraction so every downstream
agent (NER/PII/Financial/Relation, chunking/embedding) works on a single
consistent language regardless of source-document language.

Runs on the "translation" role (Qwen by default, same backend as
extraction) rather than a dedicated model -- per the confirmed decision to
keep the two-engine setup as-is instead of adding a third model.
"""
import logging

from langdetect import LangDetectException, detect

from app.config import get_settings
from app.llm.client import get_llm_client
from app.models.schemas import ParsedDocument

logger = logging.getLogger(__name__)

TRANSLATE_SYSTEM = (
    "You are a precise document translator. Translate the user's text to {target}. "
    "Preserve meaning, names, numbers, and structure exactly. Output ONLY the "
    "translation, no commentary, no original text."
)

# Batch text blocks into groups under this size per LLM call to keep calls
# fast, while still cutting the total call count well below one-per-block
# for documents with many short blocks.
_BATCH_CHARS = 4000
_BLOCK_SEP = "\n<<<BLOCK>>>\n"


def _detect_language(text: str) -> str | None:
    try:
        return detect(text)
    except LangDetectException:
        return None


async def translate_document(
    doc: ParsedDocument, unreachable_backends: set[str] | None = None
) -> ParsedDocument:
    """Mutates and returns doc. No-op when the text is already the target
    language, too short to reliably detect, or the translation backend is
    known unreachable (extraction then proceeds on the original-language
    text rather than blocking -- degraded, not stalled).
    """
    unreachable_backends = unreachable_backends or set()
    settings = get_settings()
    target = settings.translation_target_lang

    full_text = doc.full_text()
    if len(full_text.strip()) < settings.translation_min_chars:
        return doc

    lang = _detect_language(full_text[:3000])
    doc.detected_language = lang
    if lang is None or lang == target:
        return doc

    backend = get_llm_client().backend_for_role("translation")
    if backend in unreachable_backends:
        doc.warnings.append(
            f"Translation skipped: detected language '{lang}' but '{backend}' "
            f"model endpoint is unreachable -- extraction running on original-language text"
        )
        return doc

    blocks = [b for b in doc.text_blocks if b.text.strip()]
    if not blocks:
        return doc

    client = get_llm_client()
    system = TRANSLATE_SYSTEM.format(target=target)

    # Group blocks into batches under _BATCH_CHARS so long documents don't
    # need one LLM call per block, but a single oversized block still gets
    # its own call rather than being silently truncated.
    batches: list[list[int]] = []
    current: list[int] = []
    current_len = 0
    for i, b in enumerate(blocks):
        blen = len(b.text)
        if current and current_len + blen > _BATCH_CHARS:
            batches.append(current)
            current, current_len = [], 0
        current.append(i)
        current_len += blen
    if current:
        batches.append(current)

    translated_count = 0
    for batch in batches:
        joined = _BLOCK_SEP.join(blocks[i].text for i in batch)
        try:
            resp = await client.complete("translation", system, joined, max_tokens=6144)
        except Exception:
            logger.exception("Translation failed for a batch in %s", doc.source_file)
            doc.warnings.append(
                f"Translation failed for part of the document ({len(batch)} block(s)) -- "
                "left in original language"
            )
            continue

        parts = resp.text.split(_BLOCK_SEP)
        if len(parts) != len(batch):
            # Model didn't preserve the delimiter -- fall back rather than
            # mis-assigning fragments across unrelated blocks.
            doc.warnings.append(
                f"Translation batch delimiter mismatch in {doc.source_file} -- "
                "left original text for this batch"
            )
            continue

        for idx, translated_text in zip(batch, parts):
            blocks[idx].text = translated_text.strip()
            translated_count += 1

    if translated_count:
        doc.translated = True
        doc.metadata["translation_blocks"] = translated_count
        doc.metadata["translation_source_lang"] = lang
        # Not appended to doc.warnings: warnings feed DomainResult.errors,
        # which the Validator agent penalizes -- a successful translation
        # is not a quality issue.

    return doc


def _split_stem_ext(segment: str) -> tuple[str, str]:
    """Splits a path segment into (stem, extension) so translation only
    ever touches the human-readable part -- a literal file extension like
    ".pdf" should never be sent through translation, even though
    TRANSLATE_SYSTEM already asks the model to preserve structure exactly.
    A leading dot (".gitignore"-style) is not treated as an extension."""
    if "." in segment and not segment.startswith("."):
        idx = segment.rfind(".")
        return segment[:idx], segment[idx:]
    return segment, ""


async def translate_path_segments(
    segments: list[str], unreachable_backends: set[str] | None = None
) -> dict[str, str]:
    """Batch-translates folder/file NAME segments (not full paths -- split
    a path into its parts first) to the configured target language, used
    for object-storage bucket listings that may be in a different
    language (e.g. Arabic folder names) than the pipeline's working
    language. An OBS listing can return hundreds of objects that all
    repeat the same handful of folder names, so the caller should collect
    the *distinct* segment set across a whole listing and call this once,
    rather than once per object -- both far cheaper and guarantees the
    same folder name comes back identically translated everywhere it
    appears in that listing.

    Returns a dict of only the segments that were actually translated
    (extension re-attached, stem translated) -- a segment langdetect
    can't confidently call non-target-language (too short, already
    English, ambiguous), or that fails to translate, is left OUT of the
    result entirely. Callers must fall back to the original segment for
    any key not present, never assume every input key comes back.
    """
    unreachable_backends = unreachable_backends or set()
    settings = get_settings()
    target = settings.translation_target_lang

    stems: dict[str, str] = {}  # original segment -> its stem (extension stripped)
    for seg in dict.fromkeys(segments):  # de-dup, preserve first-seen order
        stem, _ext = _split_stem_ext(seg)
        if len(stem.strip()) < 2:
            continue
        lang = _detect_language(stem)
        if lang and lang != target:
            stems[seg] = stem

    if not stems:
        return {}

    backend = get_llm_client().backend_for_role("translation")
    if backend in unreachable_backends:
        return {}

    client = get_llm_client()
    system = TRANSLATE_SYSTEM.format(target=target)
    result: dict[str, str] = {}

    async def _flush(batch_originals: list[str]) -> None:
        if not batch_originals:
            return
        joined = _BLOCK_SEP.join(stems[o] for o in batch_originals)
        try:
            resp = await client.complete("translation", system, joined, max_tokens=4096)
        except Exception:
            logger.exception("Path-segment translation failed for a batch")
            return
        parts = resp.text.split(_BLOCK_SEP)
        if len(parts) != len(batch_originals):
            logger.warning("Path-segment translation delimiter mismatch -- discarding this batch")
            return
        for orig, translated_stem in zip(batch_originals, parts):
            _stem, ext = _split_stem_ext(orig)
            result[orig] = translated_stem.strip() + ext

    # Same batching rationale as translate_document above -- a listing
    # with hundreds of unique folder/file names shouldn't risk one call
    # exceeding the model's practical output size.
    batch: list[str] = []
    batch_len = 0
    for original in stems:
        blen = len(stems[original])
        if batch and batch_len + blen > _BATCH_CHARS:
            await _flush(batch)
            batch, batch_len = [], 0
        batch.append(original)
        batch_len += blen
    await _flush(batch)

    return result
