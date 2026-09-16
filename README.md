# Data Loom

*A multi-agent intelligence extraction system.*

An air-gapped, hierarchical multi-agent document intelligence pipeline. Upload
mixed-format files, get entities/PII/financial signals/relationships extracted
by specialist agents, merged into a Neo4j knowledge graph, synthesized into a
BI report + compliance report, and query the results through a GraphRAG-backed
chat interface — all running against your on-premise Qwen and Kimi2 models
with **no outbound internet calls at runtime**.

## Stack

| Layer | Choice |
|---|---|
| Extraction / reasoning model | **Qwen** (on-prem, OpenAI-compatible endpoint) |
| Synthesis / chat model | **Kimi2** (on-prem, OpenAI-compatible endpoint) |
| Embeddings | **BGE** (`BAAI/bge-large-en-v1.5`, local, `sentence-transformers`) |
| PDF parsing | **Docling** + **RapidOCR** fallback (layout-aware text/table extraction; RapidOCR is an ONNX deployment of PaddleOCR's models for scanned pages — see [PDF engine choice](#pdf-engine-choice-docling-vs-mineru-vs-paddleocr)) |
| Knowledge graph + vector index | **Neo4j** (native vector index doubles as the RAG store — no second DB) |
| Backend | FastAPI (Python, async) |
| Frontend | React + Vite, served via nginx |
| Orchestration | Custom async pipeline mirroring the L0→L3 hierarchical agent design |

## Architecture

```
Upload → File Router (MIME/ext classify)
       → L2 Parser (PDF/Docling, Image/OCR, CSV, Excel, JSON — more stubbed)
       → Chunk + Embed (BGE)              ─┐
       → NER / PII / Financial / Relation ─┼─ L2 functional agents (Qwen)
       → DomainResult per file            ─┘
       → Neo4j ingest (entities, relations, chunk vectors)
       → Synthesiser Agent (Kimi2) → BI Report, Compliance Report,
         Knowledge Graph export, Data Dump
       → GraphRAG Chat (BGE query embed → Neo4j vector search →
         1-hop graph expansion → Kimi2 answer with citations)
```

Currently wired end-to-end: **PDF, images (OCR), CSV/TSV, JSON/JSONL, Excel
(.xlsx/.xls), office docs (.docx/.pptx, plus legacy .doc/.ppt, .rtf, and .odt via a
headless LibreOffice conversion -- with header/footer text, grouped
shapes, and PPTX chart data extracted too), code/log/text, archives
(ZIP/TAR), databases
(SQLite, plus Microsoft Access .mdb/.accdb via mdbtools), HTML/XML/GeoJSON,
and email (.eml/.mbox)**. Media (audio/video) is metadata-only (no
speech-to-text). Outlook's proprietary .msg/.pst formats and .ods
spreadsheets are classified and routed but return an honest "not supported"
result — see [Extending file type support](#extending-file-type-support).

## Prerequisites

- Docker + Docker Compose on both a **connected build machine** and the
  **air-gapped target host** (same OS/architecture on both, e.g. linux/amd64).
- On-prem Qwen and Kimi2 deployments reachable from the target host, serving
  an OpenAI-compatible `/v1/chat/completions` API (vLLM, TGI, Ollama, etc).
- Neo4j 5.11+ (bundled via docker-compose already — nothing to install separately).

## Configuration

```bash
cp backend/.env.example backend/.env
```

Edit `backend/.env` — at minimum set your model endpoints:

```env
QWEN_BASE_URL=http://<your-qwen-host>:8025/v1
QWEN_MODEL_NAME=Qwen3.6-35B-A3B-AWQ

KIMI_BASE_URL=http://<your-kimi-host>:8001/v1
KIMI_MODEL_NAME=unsloth/Kimi-K2.6-GGUF

NEO4J_PASSWORD=<pick-a-real-password>
```

`ROLE_EXTRACTION` / `ROLE_SYNTHESIS` / `ROLE_CHAT` / `ROLE_TRANSLATION` map
each pipeline stage to `qwen` or `kimi` — swap them to match your own
deployment. By default, extraction/synthesis/chat all run on Kimi2 and only
translation (non-English → English preprocessing, before extraction) stays
on Qwen.

## Fastest path: pull pre-built images instead of building locally

`.github/workflows/docker-build.yml` builds both images on every push and
publishes them to GitHub Container Registry (GHCR). If building locally is
slow or unreliable (corporate proxy, restricted network — see the
troubleshooting sections below), skip building entirely:

```powershell
docker login ghcr.io -u <your-github-username>
# password prompt: paste a GitHub Personal Access Token with `read:packages`
# scope -- GitHub -> Settings -> Developer settings -> Personal access
# tokens -> Tokens (classic) -> Generate new token

docker compose pull
docker compose up -d
```

`docker compose.yml`'s `backend`/`frontend` services have both `image:`
(the GHCR tag, for pulling) and `build:` (for building locally, e.g. after
you change source code) — `pull` + plain `up` uses the pre-built image;
`up --build` builds locally as before. The GHCR package is private by
default (matches the repo), hence the login step; once authenticated,
`docker compose pull` works the same way every time you want the latest
build after I push a fix — no local build involved at all.

## Building for an air-gapped environment

Images must be built where the model weights (BGE + Docling) and Python/npm
packages can be fetched, then transferred to the air-gapped host as tarballs.

**One command (recommended):** `scripts/package-airgapped.sh` automates
everything below — building (or pulling) every image, saving them, and
bundling them with a clean copy of this repo into a single archive that
needs nothing but Docker on the target machine:

```bash
# On a connected machine, from the repo root:
./scripts/package-airgapped.sh          # builds images locally (needs internet)
# — or —
./scripts/package-airgapped.sh pull     # pulls pre-built images from GHCR instead

# Produces dist/data-loom-airgapped-package.tar.gz
```

Transfer that single archive to the air-gapped host, extract it, then:

```bash
cp backend/.env.example backend/.env   # then edit with real on-prem endpoints
./run-airgapped.sh                     # loads the bundled images and starts the stack
```

`run-airgapped.sh` never touches the network — it only `docker load`s the
tarball already sitting next to it and runs `docker compose up -d --pull
never`. See `PACKAGE_README.txt` inside the extracted archive for the short
version of these same steps.

**Manual equivalent**, if you'd rather run each step yourself (e.g. to
customize what gets included):

**On a connected machine:**

```bash
docker compose build
docker save "$(docker compose config --images backend)" \
            "$(docker compose config --images frontend)" \
            neo4j:5.26-community -o data-loom-images.tar
```

**Transfer** `data-loom-images.tar` (and this repo) to the air-gapped host via
your usual approved transfer process (USB, secure file drop, etc).

**On the air-gapped host:**

```bash
docker load -i data-loom-images.tar
cp backend/.env.example backend/.env   # then edit with real on-prem endpoints
docker compose up -d --pull never
```

The app is served at `http://<host>:8080`. The backend API is at
`http://<host>:8000/api` (proxied through the frontend at `/api` too). Neo4j
Browser (optional, for manually inspecting the graph) is at
`http://<host>:7474`.

> **No runtime network calls**: BGE embedding weights and Docling's model
> artifacts are downloaded once *during the image build* (see
> `backend/Dockerfile`) and baked into the image layer, so the running
> container never needs to reach the internet — only your on-prem
> Qwen/Kimi2/Neo4j hosts.

## Building behind a corporate TLS-inspecting proxy

If you're building on a corporate-managed machine (Zscaler, Netskope,
Fortinet, or similar), the build may fail with `SSL: CERTIFICATE_VERIFY_FAILED:
unable to get local issuer certificate` when downloading model weights (or
even Python/npm packages). This means your network is re-signing HTTPS
traffic with a private root CA — Windows trusts it because IT installed it
via policy, but the minimal Linux build container doesn't know it exists.

**Fix: give the build your corporate root CA.**

1. **Find it.** Open PowerShell and list your trusted root CAs:
   ```powershell
   Get-ChildItem -Path Cert:\LocalMachine\Root | Select-Object Subject, Thumbprint
   ```
   Look for one that isn't a well-known public CA (DigiCert, Sectigo,
   GlobalSign, Microsoft, Let's Encrypt, etc) — it's usually named after
   your company or your security vendor (e.g. "Zscaler Root CA", "Netskope
   CA", "Contoso Internal CA").

2. **Export it as base64 PEM**, substituting the matching text for `<name>`:
   ```powershell
   $cert = Get-ChildItem -Path Cert:\LocalMachine\Root | Where-Object { $_.Subject -like "*<name>*" }
   Export-Certificate -Cert $cert -FilePath "$HOME\Downloads\corporate-ca.cer" -Type CERT
   certutil -encode "$HOME\Downloads\corporate-ca.cer" "$HOME\Downloads\corporate-ca.pem"
   ```

3. **Drop it into the repo** as `backend/certs/corporate-ca.crt` (the
   extension must be `.crt` for the file to be picked up):
   ```powershell
   Copy-Item "$HOME\Downloads\corporate-ca.pem" backend\certs\corporate-ca.crt
   ```

4. **Build normally** — `backend/Dockerfile` automatically trusts anything
   found in `backend/certs/` (see the `update-ca-certificates` step). If
   you don't have this issue, leave that folder empty; it's a no-op.

If you're not sure whether this applies to you, the safest check is: does
`docker pull hello-world` succeed, but model downloads fail with a
certificate error specifically (not a timeout)? That combination points
directly at this.

### If pip reports hash mismatches ("packages do not match the hashes")

This is a step further than the certificate issue above: the connection is
trusted, but the *file contents* that arrive don't match what the package
index says they should be — pip's own integrity check catches this and
refuses to install. If this happens on small files too (not just huge ones
where a dropped/resumed download could explain it), it means something
between you and PyPI is actively rewriting content in transit — typically a
corporate proxy's content-inspection/antivirus layer, which sits *inside*
the TLS connection (it terminates and re-establishes TLS itself), so it can
alter bytes without breaking the certificate chain.

`backend/Dockerfile` already installs dependencies as separate layers
(`requirements/*.txt`, one `pip install` per group) specifically so a
corrupted download only costs a retry of that one group, not the whole
build — just re-run `docker compose up --build` and Docker's layer cache
skips everything that already succeeded.

If it keeps recurring across different, unrelated packages, retrying
indefinitely won't converge. The durable fixes, in order of preference:

1. **Ask IT for an SSL-inspection exclusion** for `pypi.org`,
   `files.pythonhosted.org`, `download.pytorch.org`, and `huggingface.co`.
   Many companies already exclude package registries from proxies like
   Zscaler for exactly this reason — it's a common, well-understood request.
2. **Use an internal package mirror** if your company runs one (Artifactory,
   Nexus, devpi) — point `pip` at it instead of the public index, bypassing
   the inspecting proxy entirely for these installs.
3. **Build on a machine that isn't behind the proxy** (a personal machine
   off the corporate network/VPN, a cloud VM, CI) and transfer the built
   images to your target host via `docker save` / `docker load` — see
   [Building for an air-gapped environment](#building-for-an-air-gapped-environment)
   above; this is the same transfer workflow either way.

### If only the embedding model's weights file fails (small HF files work fine)

Hugging Face serves large files (like the BGE embedding model's
`model.safetensors`, ~1.3 GB) from a separate CDN host
(`*.aws.cdn.hf.co`, their "Xet" storage backend) rather than
`huggingface.co` itself. On some corporate networks that CDN host gets a
TLS-inspected certificate whose chain can't be resolved locally, even
though `huggingface.co`'s own certificate (and everything else in this
guide) works fine — trusting the corporate root CA per the section above
doesn't help here because it's often a *different* certificate than the
one signing the main domain.

If you hit this, there's no need to keep fighting the certificate chain:
download the file once through a channel that already works for you (a
regular browser handles it fine), and vendor it into the build instead of
letting the container fetch it live.

1. Download in your browser:
   `https://huggingface.co/BAAI/bge-large-en-v1.5/resolve/main/model.safetensors`
2. Save it as `backend/models_cache/model.safetensors` (gitignored — this
   file never gets committed).
3. Rebuild normally: `docker compose up -d --build`. `backend/Dockerfile`
   automatically detects the vendored file and uses it instead of
   downloading — every other file (config, tokenizer, etc) still comes
   from the network as usual. If you don't have this issue, leave the
   folder empty; it's a no-op and the model downloads live as normal.

The same thing can happen for Docling's PDF-parsing models
(`ds4sd/docling-models`) right after — it has three large files that hit
the same CDN. If that build step fails the same way, download each of
these from your browser and save them at the matching path:

| Download URL | Save as |
|---|---|
| `https://huggingface.co/ds4sd/docling-models/resolve/main/model_artifacts/layout/model.safetensors` | `backend/models_cache/docling/model_artifacts/layout/model.safetensors` |
| `https://huggingface.co/ds4sd/docling-models/resolve/main/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors` | `backend/models_cache/docling/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors` |
| `https://huggingface.co/ds4sd/docling-models/resolve/main/model_artifacts/tableformer/fast/tableformer_fast.safetensors` | `backend/models_cache/docling/model_artifacts/tableformer/fast/tableformer_fast.safetensors` |

Then rebuild the same way. These are also gitignored and never committed.

## Data persistence

Everything that needs to survive a restart -- job uploads/outputs, job
history, the persistent cross-job structured-data library
(`dataset_library.db`), and OBS (object storage) settings including the
Fernet encryption key and encrypted credentials -- lives under `/app/data`
inside the backend container, bind-mounted to a `./data` folder next to
this file (`docker-compose.yml`'s `backend.volumes`). Because it's a real
host folder rather than a Docker-managed named volume:

- It's visible and backupable directly from the host -- just copy `./data`.
- `docker compose build`, `up -d`, `up -d --force-recreate`, and `restart`
  never touch it, the same as before.
- Unlike a named volume, **`docker compose down -v` does not delete it
  either** -- `-v` only removes volumes Docker itself manages, and a bind
  mount isn't one.
- `./data` is gitignored (it holds an encryption key and encrypted
  credentials) -- never commit it.

**Migrating from an older checkout that used the `iex_data` named volume:**
switching the mount type doesn't move existing data automatically -- do
this once, in order, before pulling/rebuilding with the new
`docker-compose.yml`:

```powershell
# 1. With the OLD docker-compose.yml still in place and the backend
#    container running, copy its data out to a new local ./data folder:
docker cp iex-backend:/app/data .\data

# 2. Now pull the updated docker-compose.yml (switches the mount to ./data):
git pull origin claude/repo-connection-setup-ss6t4p

# 3. Recreate the backend so it picks up the new bind mount:
docker compose up -d --pull never

# 4. Verify your OBS settings/job history are still there, then optionally
#    remove the now-unused old volume (find its exact name first):
docker volume ls
docker volume rm <project-name>_iex_data
```

## Using it

1. Open the frontend, drop in files (PDF, images, CSV, JSON, Excel — mix
   freely), click **Analyze**.
2. Watch per-file progress (Parsing → Extracting → Building Graph →
   Synthesizing → Complete).
3. Once complete, four output tabs are populated:
   - **📋 BI Report** — executive summary, key entities, financial highlights, risks, market signals
   - **🛡️ PII & Compliance** — full PII inventory by severity, gap flags, remediation actions, CSV export
   - **🕸️ Knowledge Graph** — interactive force-directed entity/relation graph
   - **📦 Data Dump** — extracted tables + full report/graph/PII export downloads
4. **💬 Chat** unlocks once the knowledge graph is built (you don't have to
   wait for the full synthesis step) — ask natural-language questions and get
   answers grounded in retrieved passages + graph facts, with citations.

## PDF engine choice: Docling vs MinerU vs PaddleOCR

All three were evaluated for the PDF Agent:

- **Docling** — best table-structure fidelity (TableFormer) and outputs a
  structured, reading-order-aware document object; fast; Apache-2.0. Best
  fit for the business/legal/financial document types this system targets
  (reports, contracts, forms).
- **MinerU** — best layout-detection accuracy and by far the best formula
  (LaTeX) recognition, but that edge mainly matters for multi-column
  scientific papers, not the primary use case here. Internally it already
  uses PaddleOCR for its own OCR step.
- **PaddleOCR** — not a full document parser on its own (no native-text
  extraction, no reading-order/table structure), but the strongest raw OCR
  recognition of the three, especially multilingual.

**Decision**: Docling stays the primary PDF parser (layout + tables +
reading order), with its OCR fallback for scanned pages switched from
Tesseract to **RapidOCR** — an ONNX-runtime deployment of PaddleOCR's
detection/recognition/classification models, same accuracy family as
PaddleOCR without the heavy PaddlePaddle framework dependency. Its default
models ship inside the `rapidocr-onnxruntime` pip wheel, so there's no
separate model-download step even on a cold start on the air-gapped host —
a meaningful plus over Tesseract/EasyOCR for this deployment model. See
`backend/app/parsers/pdf_parser.py`.

If you later process a lot of scientific/formula-heavy PDFs, MinerU is a
reasonable addition as an alternate PDF pipeline (selectable per-job) —
its output would need a translation layer into `ParsedDocument`, the same
pattern described below for any new parser.

## Extending file type support

Add a new L2 parser in `backend/app/parsers/`, implementing `BaseParser`
(see `pdf_parser.py` for the pattern), then register it in
`backend/app/parsers/router.py`'s `_get_parser()`. Everything downstream
(chunking, NER/PII/financial/relation extraction, graph ingest, synthesis,
chat) works automatically on any parser that returns a `ParsedDocument`.

## Development (non-air-gapped, for local iteration)

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
uvicorn app.main:app --reload

# Frontend
cd frontend
npm install
npm run dev

# Neo4j (or run the full docker-compose stack and point NEO4J_URI at it)
docker run -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/changeme neo4j:5.26-community
```

## Notes on the agent hierarchy

This implementation collapses the diagram's L0–L3 message-passing agents
into direct async function calls within a single Python process (see
`backend/app/pipeline/job_manager.py` for the L0 orchestrator equivalent,
`backend/app/agents/domain_managers.py` for L1, and `backend/app/agents/
extraction.py` / `chunking.py` for L2). This keeps the system simple to
operate and debug in a single-node air-gapped deployment while preserving
the same tiered responsibilities and data contracts (`DomainResult`,
`SynthesisOutput`, etc. in `backend/app/models/schemas.py`). If you need
true distributed multi-agent execution (separate processes/containers per
agent tier with real message queues), that's a natural next step built on
top of these same module boundaries.
