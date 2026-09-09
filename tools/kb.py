"""Knowledge-base RAG pipeline: chunk -> embed (Voyage) -> Chroma -> cited answer (Claude).

Reused pattern from the prior RAG project, with one change carried over from
that project's eval post-mortem: chunks embed with their document title and
section heading prepended. A bare paragraph like "This is handled by a human
reviewer" is nearly meaningless in isolation; "Shipping Policy > Consignments
presumed lost: ... handled by a human reviewer" is retrievable. The heading is
context the chunk cannot supply about itself.

Run `python tools/kb.py ingest` once, then `python tools/kb.py query "..."`.
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import CHROMA_PATH, DOCS_PATH  # noqa: E402

EMBED_MODEL = os.environ.get("VOYAGE_MODEL", "voyage-3")
ANSWER_MODEL = os.environ.get("KB_MODEL", "claude-haiku-4-5")
COLLECTION = "support_policies"
TARGET_CHARS = 900          # ~200-250 tokens; policy clauses are short
OVERLAP_CHARS = 150


_QUERY_CACHE: dict[str, dict] = {}   # question -> full answer payload
_LAST_CALL = [0.0]                   # module-level clock for proactive pacing
MIN_INTERVAL_S = 21.0                # free tier is 3 requests/minute


def _pace():
    """Wait out the free-tier rate limit BEFORE calling, not after being refused.

    Reacting to 429s is not enough here: the agent retries a failed tool call,
    so one rate limit turns into several, and the agent eventually concludes the
    knowledge base is broken. That scores an infrastructure limit as a reasoning
    failure. Spacing calls proactively keeps the tool honest.
    """
    elapsed = time.monotonic() - _LAST_CALL[0]
    if _LAST_CALL[0] and elapsed < MIN_INTERVAL_S:
        time.sleep(MIN_INTERVAL_S - elapsed)
    _LAST_CALL[0] = time.monotonic()


def _embed(client, texts, input_type, attempts=6, base_wait=22.0):
    """Embed with backoff on rate limits.

    Voyage's free tier allows 3 requests/minute. That is low enough that a
    normal eval run trips it, and an un-retried 429 would surface to the agent
    as a broken knowledge base -- scoring a rate limit as a reasoning failure.
    Waits are long because the limit is per-minute, not per-second.
    """
    import voyageai.error

    for attempt in range(attempts):
        try:
            _pace()
            return client.embed(texts, model=EMBED_MODEL, input_type=input_type).embeddings
        except voyageai.error.RateLimitError:
            if attempt == attempts - 1:
                raise
            wait = base_wait * (attempt + 1)
            print(f"    [voyage rate limit; waiting {wait:.0f}s]", file=sys.stderr, flush=True)
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def chunk_document(path: Path):
    """Split one markdown policy doc into heading-scoped chunks."""
    text = path.read_text()
    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem

    # Split on ## section headings, keeping the heading with its body.
    parts = re.split(r"^(##\s+.+)$", text, flags=re.MULTILINE)
    sections = []
    if parts[0].strip():
        sections.append(("Preamble", parts[0].strip()))
    for i in range(1, len(parts), 2):
        heading = parts[i].lstrip("# ").strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if body:
            sections.append((heading, body))

    chunks = []
    for heading, body in sections:
        for piece in _split_to_size(body):
            chunks.append({
                "text": f"{title} > {heading}\n\n{piece}",
                "raw": piece,
                "doc": title,
                "section": heading,
                "source": path.name,
            })
    return chunks


def _split_to_size(body: str):
    """Split a section body on paragraph boundaries into ~TARGET_CHARS pieces."""
    if len(body) <= TARGET_CHARS:
        return [body]
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    pieces, current = [], ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > TARGET_CHARS:
            pieces.append(current)
            # Overlap: carry the tail of the previous piece for continuity.
            current = current[-OVERLAP_CHARS:] + "\n\n" + para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        pieces.append(current)
    return pieces


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
def ingest(verbose: bool = True):
    import chromadb
    import voyageai

    docs = sorted(DOCS_PATH.glob("*.md"))
    if not docs:
        raise SystemExit(f"No policy documents found in {DOCS_PATH}")

    chunks = []
    for path in docs:
        doc_chunks = chunk_document(path)
        chunks.extend(doc_chunks)
        if verbose:
            print(f"  {path.name}: {len(doc_chunks)} chunks")

    vo = voyageai.Client()
    embeddings = []
    for i in range(0, len(chunks), 64):
        batch = [c["text"] for c in chunks[i:i + 64]]
        embeddings.extend(_embed(vo, batch, "document"))

    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    try:
        client.delete_collection(COLLECTION)
    except Exception:
        pass
    col = client.create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})
    col.add(
        ids=[f"c{i}" for i in range(len(chunks))],
        embeddings=embeddings,
        documents=[c["text"] for c in chunks],
        metadatas=[{"doc": c["doc"], "section": c["section"], "source": c["source"]}
                   for c in chunks],
    )
    print(f"Ingested {len(chunks)} chunks from {len(docs)} documents into {CHROMA_PATH}")
    return len(chunks)


# ---------------------------------------------------------------------------
# Retrieve + cited answer
# ---------------------------------------------------------------------------
def retrieve(question: str, k: int = 5):
    import chromadb
    import voyageai

    vo = voyageai.Client()
    qvec = _embed(vo, [question], "query")[0]
    col = chromadb.PersistentClient(path=str(CHROMA_PATH)).get_collection(COLLECTION)
    res = col.query(query_embeddings=[qvec], n_results=k)
    return [
        {"text": doc, "doc": meta["doc"], "section": meta["section"],
         "source": meta["source"], "distance": dist}
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])
    ]


ANSWER_SYSTEM = """You answer questions about a retailer's support policies using ONLY the \
numbered policy excerpts provided. 

Rules:
- Cite the excerpt number inline for every factual claim, like [2].
- If the excerpts do not contain the answer, say so plainly. Do not use general \
knowledge about how returns or warranties usually work — this retailer's policy is \
the only authority, and guessing produces confidently wrong support answers.
- Quote exact figures (day counts, dollar limits) rather than paraphrasing them.
- Be brief: three sentences or fewer unless the question genuinely needs more."""


def answer(question: str, k: int = 5):
    """Retrieve and produce a cited answer. Returns dict with answer + sources.

    Answers are cached by question text. Agents commonly re-ask a question
    verbatim after a transient tool error; without a cache that doubles the
    embedding spend and can re-trip the rate limit that caused the retry.
    """
    import anthropic

    cache_key = f"{question}|{k}"
    if cache_key in _QUERY_CACHE:
        return _QUERY_CACHE[cache_key]

    hits = retrieve(question, k=k)
    if not hits:
        return {"answer": "No policy documents are indexed.", "sources": []}

    excerpts = "\n\n".join(
        f"[{i + 1}] ({h['doc']} > {h['section']})\n{h['text']}"
        for i, h in enumerate(hits)
    )
    # Haiku 4.5 rejects the effort parameter outright (400). Only send it on
    # models that accept it -- this task is extraction from supplied text, so
    # low effort is right where it is available and no loss where it is not.
    kwargs = {}
    if not ANSWER_MODEL.startswith("claude-haiku"):
        kwargs["output_config"] = {"effort": "low"}

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=ANSWER_MODEL,
        max_tokens=4096,
        system=ANSWER_SYSTEM,
        messages=[{"role": "user",
                   "content": f"Policy excerpts:\n\n{excerpts}\n\nQuestion: {question}"}],
        **kwargs,
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    out = {
        "answer": text,
        "sources": [{"n": i + 1, "doc": h["doc"], "section": h["section"],
                     "source": h["source"]} for i, h in enumerate(hits)],
    }
    _QUERY_CACHE[cache_key] = out
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest")
    q = sub.add_parser("query")
    q.add_argument("question")
    q.add_argument("-k", type=int, default=5)
    dry = sub.add_parser("chunks")  # inspect chunking without any API key
    args = ap.parse_args()

    if args.cmd == "ingest":
        ingest()
    elif args.cmd == "chunks":
        total = 0
        for p in sorted(DOCS_PATH.glob("*.md")):
            cs = chunk_document(p)
            total += len(cs)
            print(f"\n=== {p.name} ({len(cs)} chunks) ===")
            for c in cs:
                head = c["text"].split("\n")[0]
                print(f"  [{len(c['raw']):4d} chars] {head}")
        print(f"\ntotal: {total} chunks")
    else:
        out = answer(args.question, k=args.k)
        print(out["answer"])
        print("\nSources:")
        for s in out["sources"]:
            print(f"  [{s['n']}] {s['doc']} > {s['section']} ({s['source']})")
