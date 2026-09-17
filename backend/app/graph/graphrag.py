"""GraphRAG retrieval + answer generation for the interactive chat.

Retrieval strategy: vector search over Chunk nodes (BGE embeddings, Neo4j
native vector index) for the top-k semantically relevant chunks, then a
1-hop graph expansion from those chunks to their mentioned entities and
that entity's relations. Both are folded into the Kimi2 prompt so answers
can cite specific text passages *and* reason over the structured graph
(ownership chains, cross-document links) — this is what distinguishes it
from plain vector-only RAG.

A second, independent expansion path runs alongside the chunk-based one:
any of the job's known entity names that appear literally in the question
itself get their relations pulled directly from the graph (see
neo4j_client.find_entities_mentioned_in_text / expand_relations_for_entities),
with no dependency on chunk vector search having actually retrieved a
passage that mentions them. This exists because a short, information-dense
passage -- a passport's few lines of text -- can lose out on pure semantic
similarity to a short question ("what is X's mother's name") even when the
relation itself (MOTHER_OF) sits right there in the graph; asking the graph
directly once the person's name is known sidesteps that entirely. Both
sources merge into the same graph_facts list, deduplicated by entity name.

The question doesn't even have to name anyone: if it doesn't (plain "what
is the mother's name?"), the job's dominant PERSON entity is assumed to
be who's meant (see neo4j_client.find_dominant_person_entity) -- the
common case for a single-document job like a passport, where there's one
obvious main subject even though relatives get named too. "Dominant" is
ranked primarily by RELATION-edge degree (kinship relations point from
each named relative TO the document's holder, so the holder ends up a
hub) and falls back to mention count across chunks only when relation
degree ties -- mention count alone isn't enough for a short document
(e.g. a 1-2 chunk passport) where the holder and a relative both surface
in about the same number of chunks.

Before the vector search runs, query_router.infer_categories() takes a
cheap, deterministic pass over the question itself (keyword match, no LLM
call) to guess which file categories it's probably about, and that guess
narrows the search's candidate pool -- faster and less noisy than always
ranking every chunk in the job regardless of format. It's a soft signal:
a wrong or absent guess falls back to an unfiltered search rather than
ever making an answer worse than not guessing at all.

When Neo4j is unreachable (or returns nothing), retrieval falls back to
local_vector_store's brute-force cosine search over the same Chunk
embeddings the pipeline already computed for this job -- see
local_vector_store.py for why that data is available for free. That path
loses the graph-facts expansion (no Neo4j, no relations to traverse) but
still lets chat answer from the document text instead of failing outright.

Model fallback: if the chat role's configured backend (normally Kimi2) is
unreachable, the other backend (Qwen) is tried automatically before
giving up -- an outage on one model no longer takes chat down entirely,
it just answers from the other one. ChatResponse.fallback_model tells the
caller which backend actually answered when this happened.

Before any of that, a question that looks quantitative (see
structured_query.looks_quantitative) and whose job has structured files
(CSV/Excel/Database) gets one attempt at a different strategy entirely:
generate and run real SQL against those files' actual typed data (see
structured_query.py), instead of embedding-similarity search over a
20-row text preview. That branch fails closed -- no structured tables, an
LLM/execution failure, or an unsafe/invalid generated query all fall
straight through to the GraphRAG path below unchanged.
"""
import logging

from app.embeddings.bge import get_embedder
from app.graph import local_vector_store, structured_query
from app.graph.neo4j_client import get_store
from app.graph.query_router import infer_categories
from app.llm.client import get_llm_client
from app.models.schemas import ChatMessage, ChatResponse, Chunk, Citation, Job
from app.pipeline.agent_tracker import finish_activity, start_activity

logger = logging.getLogger(__name__)

CHAT_SYSTEM = """You are an intelligence analysis assistant answering questions about a \
set of documents that were already processed and indexed into a knowledge graph.

You are given:
1. Retrieved text passages (with source file + page).
2. Related graph facts (entities and their relationships) surfaced from those passages.

Rules:
- Answer ONLY using the provided context. If the context doesn't contain the answer, \
say so plainly — do not guess or use outside knowledge.
- Cite sources inline like [source_file p.X] after claims.
- If graph facts and text passages conflict, note the discrepancy.
- Be concise and factual. This is an analyst tool, not a chatbot for chit-chat.
"""


async def answer_question(
    job_id: str, message: str, history: list[ChatMessage], fallback_chunks: list[Chunk] | None = None,
    job: Job | None = None,
) -> ChatResponse:
    """Reports its own "GraphRAG Chat Synthesizer" activity span (see
    agent_tracker.py) so chat turns are visible in the same Agent Activity
    panel as ingestion, alongside Mapping Agent / Insight Agent / BI
    Synthesizer -- job=None (e.g. tests, or a job the caller didn't fetch)
    makes this a no-op, same convention as everywhere else that reports
    activity."""
    activity = start_activity(job, "GraphRAG Chat Synthesizer", job_id)
    try:
        resp = await _answer_question_impl(job_id, message, history, fallback_chunks)
    except Exception as exc:
        logger.exception("answer_question crashed unexpectedly for job %s", job_id)
        finish_activity(activity, "failed")
        return ChatResponse(
            answer=f"Chat failed unexpectedly: {exc}",
            citations=[], uncertain=True,
        )
    finish_activity(activity, "failed" if resp.uncertain else "completed")
    return resp


async def _answer_question_impl(
    job_id: str, message: str, history: list[ChatMessage], fallback_chunks: list[Chunk] | None = None,
) -> ChatResponse:
    if structured_query.looks_quantitative(message):
        try:
            structured_resp = await structured_query.answer_structured_question(job_id, message)
        except Exception:
            logger.exception("Structured query branch crashed for job %s -- falling back to GraphRAG.", job_id)
            structured_resp = None
        if structured_resp is not None:
            return structured_resp

    embedder = await get_embedder()
    store = get_store()
    query_vec = await embedder.embed_query(message)

    # Deterministic, keyword-based guess at which file categories this
    # question is probably about (see query_router.py) -- None means "no
    # opinion," searched exactly like before this existed. A guess only
    # ever narrows the candidate pool; if it comes back empty below, the
    # search is retried unfiltered rather than trusting the guess over
    # having an answer at all.
    categories = infer_categories(message)

    # Fast preflight, same rationale as the chat-model check below: a dead
    # or unauthorized Neo4j should surface quickly rather than bubbling up
    # as an unhandled exception -> 500 from the route. It no longer bails
    # out immediately, though -- the local fallback below may still be able
    # to answer from this job's already-computed chunk embeddings.
    neo4j_reachable, neo4j_detail = await store.check_reachable(timeout_s=8.0)

    degraded = False
    hits: list[dict] = []
    graph_facts: list[dict] = []

    if neo4j_reachable:
        try:
            hits = await store.vector_search(job_id, query_vec, top_k=8, categories=categories)
            if not hits and categories:
                # The category guess narrowed to zero results -- retry
                # unfiltered rather than answering "nothing found" when
                # the job may simply not have any chunks in that category
                # (or the guess was just wrong).
                hits = await store.vector_search(job_id, query_vec, top_k=8)
        except Exception:
            logger.exception("Vector search failed for job %s", job_id)
            hits = []
        if hits:
            try:
                graph_facts = await store.expand_entities_for_chunks(job_id, [h["chunk_id"] for h in hits])
            except Exception:
                logger.exception("Graph expansion failed for job %s", job_id)
                graph_facts = []

        # Directly resolve any of this job's known entities named in the
        # question itself, and pull THEIR relations too -- independent of
        # whether chunk vector search above happened to retrieve a passage
        # mentioning them. This is what makes "what is John Smith's
        # mother's name" reliable: the passport's few lines of text can
        # easily lose out to other, longer chunks on pure semantic
        # similarity to a short question, even though the relation
        # (MOTHER_OF) sits right there in the graph once you know his
        # name. Runs regardless of whether `hits` found anything, and
        # merged in deduplicated by entity name.
        try:
            named_entities = await store.find_entities_mentioned_in_text(job_id, message)
            if not named_entities:
                # The question may not name anyone at all ("what is the
                # mother's name?" rather than "what is John Smith's
                # mother's name?"). Falls back to the job's dominant
                # PERSON entity, ranked mainly by RELATION-edge degree and
                # only by mention count as a tiebreaker (see
                # find_dominant_person_entity's docstring for the full
                # history of why "exactly one PERSON entity" and later
                # "mention count alone" both fell short); stays silent
                # (None) when no entity clearly dominates, since guessing
                # among comparably-ranked people would be worse than not
                # answering.
                dominant = await store.find_dominant_person_entity(job_id)
                if dominant:
                    named_entities = [dominant]
            if named_entities:
                direct_facts = await store.expand_relations_for_entities(job_id, named_entities)
                # Union rather than "first source wins" -- both sources
                # query the same directional (e)-[r]->(other) / (e)<-[r]-(other)
                # shape for a found entity, so in practice neither is ever
                # strictly richer, but merging relation lists instead of
                # keeping only whichever list showed up first is what
                # actually guarantees no real fact gets dropped just
                # because the entity happened to also turn up via a chunk.
                by_entity = {f["entity"]: f for f in graph_facts}
                for fact in direct_facts:
                    existing = by_entity.get(fact["entity"])
                    if existing is None:
                        graph_facts.append(fact)
                        by_entity[fact["entity"]] = fact
                    else:
                        existing_related = existing.setdefault("related", [])
                        for rel in fact.get("related", []):
                            if rel not in existing_related:
                                existing_related.append(rel)
        except Exception:
            logger.exception("Direct entity-name graph lookup failed for job %s", job_id)
    else:
        logger.warning("Neo4j unreachable for job %s (%s) — trying local vector fallback.", job_id, neo4j_detail)

    if not hits and fallback_chunks:
        hits = local_vector_store.search(fallback_chunks, query_vec, top_k=8, categories=categories)
        if hits:
            degraded = True

    if not hits and not graph_facts:
        if not neo4j_reachable:
            return ChatResponse(
                answer=f"The knowledge graph is currently unreachable: {neo4j_detail} "
                       f"No local fallback content is available for this job either (it may have been "
                       f"processed by a different backend instance, or hasn't finished parsing yet). "
                       f"Verify Neo4j is up and the credentials match, then try again.",
                citations=[], uncertain=True,
            )
        return ChatResponse(
            answer="I don't have any indexed content to answer this from yet. "
                   "Make sure the analysis job has completed before asking questions.",
            citations=[], uncertain=True,
        )

    context_parts = []
    if hits:
        context_parts.append("--- Retrieved passages ---")
        for h in hits:
            loc = f"{h['source_file']}" + (f" p.{h['page']}" if h.get("page") else "")
            context_parts.append(f"[{loc}] (score={h['score']:.2f})\n{h['text']}")

    if graph_facts:
        context_parts.append("\n--- Related graph facts ---")
        for fact in graph_facts:
            # Render each relation as an explicit subject-verb-object
            # sentence using its captured direction, e.g. "Jane Doe
            # MOTHER_OF Ankit" -- NOT "{r['type']} -> {r['other']}" (that
            # older format silently assumed `fact['entity']` was always the
            # relation's source, which is backwards for e.g. kinship
            # relations extracted FROM a named relative TO the document's
            # holder; looking up the holder produced a fact that read as
            # if the holder were the relative's mother).
            sentences = []
            for r in fact.get("related", []):
                if not r.get("other") or not r.get("type"):
                    continue
                if r.get("direction") == "in":
                    sentences.append(f"{r['other']} {r['type']} {fact['entity']}")
                else:
                    sentences.append(f"{fact['entity']} {r['type']} {r['other']}")
            related_str = "; ".join(sentences)
            context_parts.append(f"{fact['entity']} ({fact['type']}): {related_str or 'no known relations'}")

    context = "\n\n".join(context_parts)

    history_str = ""
    if history:
        history_str = "\n".join(f"{m.role}: {m.content}" for m in history[-6:]) + "\n"

    user_prompt = f"{history_str}Context:\n{context}\n\nQuestion: {message}"

    client = get_llm_client()
    chat_backend = client.backend_for_role("chat")
    other_backend = "kimi" if chat_backend == "qwen" else "qwen"

    # Fast preflight before the real (multi-retry, up-to-180s-per-attempt)
    # call -- a genuinely dead chat endpoint should fail in ~8s with a clear
    # message, not leave the user waiting minutes per message with no
    # feedback while it silently retries. If the configured chat backend
    # is down, automatically try the other backend before giving up --
    # this is what turns a Kimi2 outage into "chat still works, just via
    # Qwen" instead of "chat is down until someone edits .env and
    # restarts the container."
    effective_backend = chat_backend
    fallback_model: str | None = None
    reachable, detail = await client.check_reachable(chat_backend, timeout_s=8.0)
    if not reachable:
        fallback_reachable, fallback_detail = await client.check_reachable(other_backend, timeout_s=8.0)
        if fallback_reachable:
            effective_backend = other_backend
            fallback_model = other_backend
            logger.warning(
                "Chat backend '%s' unreachable (%s) -- falling back to '%s' for job %s.",
                chat_backend, detail, other_backend, job_id,
            )
        else:
            return ChatResponse(
                answer=f"Both chat models are currently unreachable: '{chat_backend}' ({detail}) "
                       f"and '{other_backend}' ({fallback_detail}). Verify at least one endpoint "
                       f"is up and reachable from the backend, then try again.",
                citations=[], uncertain=True,
            )

    try:
        resp = await client.complete(
            "chat", CHAT_SYSTEM, user_prompt, temperature=0.2, max_tokens=2048,
            backend_override=effective_backend,
        )
        answer_text = resp.text.strip()
    except Exception as exc:
        logger.exception("Chat completion failed for job %s", job_id)
        return ChatResponse(
            answer=f"The chat model ('{effective_backend}') failed to respond: {exc}",
            citations=[], uncertain=True,
        )

    citations = [
        Citation(source_file=h["source_file"], chunk_text=h["text"][:300], page=h.get("page"), score=h["score"])
        for h in hits[:5]
    ]
    uncertain = any(p in answer_text.lower() for p in ["don't know", "not contain", "no information", "cannot find"])

    return ChatResponse(
        answer=answer_text, citations=citations, uncertain=uncertain,
        degraded=degraded, fallback_model=fallback_model,
    )
