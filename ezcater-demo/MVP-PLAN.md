# MVP Plan — demo → professional (Go backend + deep ontology)

Research-backed plan (4-agent deep research, 2026-07-10; sources inline). Working
method: incremental cutover, every phase verified live before the next starts.

## The stack verdict (researched, not guessed)

**Go — definitively. Not Java.**

- Staff SWE, Search Platform (Greenhouse 5160456007, live): *"Strong proficiency
  in Go, or a willingness to adopt it as a primary language."*
- Director of Engineering posting (5168483007), verbatim: *"The stack is modern
  and intentional: **Go, PostgreSQL, Kafka (MSK), Temporal, Vespa, SpiceDB, EKS**."*
- EM, Search Platform (5180479007): *"We're actively replatforming search …
  Go experience is preferred."*
- Java appears in **one** posting company-wide (Finance Tech) and only as "Go,
  Ruby on Rails, Java or similar". **No search posting mentions Java.** Where
  Java *would* appear in a Vespa shop is inside the engine itself (custom
  Searchers/document processors are JVM components) — Vespa is fully usable
  without them, and nothing suggests ezCater writes any.
- Legacy = Ruby on Rails monolith (StackShare, GitHub org, blog); the Rails→Go
  transition is visible in "ideally in Go or Ruby on Rails" postings.

**MVP mirror:** Go query service + Go/py indexing around a declaratively
configured Vespa (schemas + rank profiles, no Java), Python retained as the ML
sidecar. Optional stretch (only if we want engine depth): one custom Java
Searcher.

## Target architecture

```
React UI ──► search-api (Go, :8090)  ──────────► Vespa (:8080)
                │  owns the search contract       BM25 ∪ ANN ∪ filters, RRF,
                │  YQL via @param substitution     in-engine e5 embedding
                │
                └──► ml-sidecar (Python, :8009 slimmed)
                     LLM understanding + semantic cache + vision PDF
                     (ecosystem reality: ML stays Python — same split real shops use)

ingestion (Python batch)  ──enrich──► ontology graph (build artifact) ──compile──► Vespa fields
```

Key patterns (from the Go/Vespa + Go-architecture research):
- **No official Go Vespa client** (vespa-engine/vespa#30413 parked) → thin
  hand-rolled typed client, one shared `http.Client`, fixed YQL shapes,
  **all user text via `@param` substitution** (documented injection defense).
  This is the Vinted "search contract" gateway pattern.
- Layout per go.dev/doc/modules/layout: `cmd/` + `internal/`, **no pkg/**.
  Stdlib ServeMux (1.22 pattern routing) — no router dep. `run(ctx, getenv)`
  entrypoint (Mat Ryer pattern), slog JSON, graceful shutdown.
- Feeding: `vespa feed` CLI (Go, HTTP/2) for bulk; `document/v1` partial
  updates from services. LLM: official `openai-go` v3; SSE via `http.Flusher`.
- Semantic cache in Go: **don't** run local ONNX initially — delegate embedding
  to Vespa (`embed(@text)`) or the sidecar; revisit only if latency demands.

## Phases

### Phase 1 — Go scaffold + first parity endpoint ✅ DONE (94e4093)
`search-api/`: cmd/ + internal/{config,vespa,server}. /healthz, /readyz,
/v1/typeahead. **Verified:** build+vet clean; typeahead parity vs FastAPI 4/4;
injection probe returns data-not-syntax.

### Phase 2 — full search parity in Go
- /v1/search: keyword | semantic | hybrid | browse, facet filters
  (cuisine/diet/price/headcount/source), result mapping (match-features,
  serves/price_pp), dedupe, totals. Parity harness: same query → Go vs FastAPI
  → identical hits.
- /v1/understand_stream: Go orchestrates — calls sidecar for concepts (cache
  hit or LLM stream), relays SSE tokens (http.Flusher), builds the YQL itself
  (graph expansion terms come from the sidecar response), queries Vespa.
- Slim the FastAPI service to ML-only endpoints (/understand, /transcribe,
  /upload_pdf internals); UI switches to :8090.
- Done = UI runs fully against Go; FastAPI no longer serves search traffic.

### Phase 3 — ontology deepening (the "go very deep" track)
Leveled roadmap from the ontology research — the through-line: keep the two
correct instincts (KG = build artifact compiled into Vespa fields; deterministic
checks gate the LLM) and make them **auditable → governed → interoperable**.

- **L1 Auditable:** provenance on every edge (`source ∈ {curated,llm,imported}`,
  model, prompt_version, created_at — PROV-O shape); confidence on LLM edges;
  jurisdiction-tagged closed allergen lists (EU-14 Reg 1169/2011, US-9
  FALCPA+FASTER); reproducible build script with content-hash snapshots.
- **L2 Governed:** three-band entity resolution (auto-merge / review-queue /
  auto-reject) — the merge game becomes the review UI; query-time entity
  linking constrained to ANN-retrieved candidates (DoorDash/Instacart
  guardrail); semver + changelog for the graph; SHACL-style invariant checks
  failing the build.
- **L3 Interoperable:** `foodon_id` alignment column (OntoFox/MIREOT subset
  import, not all 27k classes); SKOS for cuisine/category + synonyms; USDA FDC
  nutrition + Open Food Facts taxonomies as enrichment sources.
- **L4 Scale (deferred until warranted):** storage stays in-memory while edits
  are batch; → Postgres (review-queue + transactional edits) → Neo4j (analyst
  traversal) → RDF+SPARQL (external reasoning) only on demonstrated need.
  Serving path never touches the graph store.

### Phase 4 — professional hardening
OpenAPI contract (hand-written yaml, no codegen yet), otelhttp traces/metrics,
golangci-lint + GitHub Actions CI, multi-stage Dockerfile (distroless, ~15MB),
docker-compose with `service_healthy` gating (Vespa is slow to start), e2e
tests that boot the server and poll /readyz.

### Phase 5 — the ezCater-shaped differentiators (post-MVP)
Two-stage availability filtering (zone/lead-time pre-filter → Vespa), LightGBM
LambdaMART second-phase (redeploy, no reindex), LLM-as-judge eval harness on
catering queries, e5 query:/passage: prefix fix (the one true reindex).

## Cutover rules
FastAPI keeps serving until Go proves parity per endpoint. No big bang. Each
phase = its own commits, verified live. `.env` stays gitignored; never push
without an explicit go-ahead.
