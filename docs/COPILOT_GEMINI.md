# Gemini conversational Copilot

## Request path and original defect

The existing Graph Explorer textarea, Enter handler, Ask button, suggestion chips,
and retry action all send `POST /api/graph/ai-query` through the existing API helper
and `VITE_API_BASE_URL` convention. They submit the actual question, selected case,
focused node and conversation history.

Previously the SQLite route returned `local_store.copilot_answer`, a deterministic
keyword matcher. It never called Gemini. The Neo4j chat service also substituted
demonstration answers when Gemini was unavailable. Those fallbacks are no longer
used by either Copilot chat entry point. The independent deterministic local QA
utility and the existing case-report features are preserved.

## Environment and deployment

Install the backend requirements: `pip install -r requirements.txt`.
The official Google Gen AI SDK dependency is `google-genai>=2.32.0,<3.0.0`;
local verification used version 2.32.0.
The existing `GeminiService` now owns conversational generation too; the context
builder is a data-access helper, not a second AI provider.

`GEMINI_API_KEY` is required on the **API server**. `GeminiService._server_api_key`
is the common reader for chat and the existing Gemini dossier methods. It checks
`os.environ["GEMINI_API_KEY"]` and then the existing server settings value, trimming
whitespace. `backend/config.py` loads that settings value from the named process
variable or `.env`. It can be supplied by the
process environment or the existing server-side `.env` settings loader. No
`GOOGLE_API_KEY` fallback is used for chat. `.env.example` contains a blank key
placeholder only. Never use a `VITE_` prefix, commit the key, put it in
`vercel.json`, send it from the browser, or paste it into chat/logs.

The default model is `gemini-flash-latest`, Google's supported Flash alias. An
optional server-side `GEMINI_MODEL` override can pin another model available to
the API project. SDK documentation: https://googleapis.github.io/python-genai/.

For the existing production URL, `https://atlas-criminal-network.vercel.app`:

1. Publish the reviewed source changes through the existing repository workflow.
2. In **Vercel Project Settings → Environment Variables**, configure the rotated
   `GEMINI_API_KEY` for **Production** and **Preview** as appropriate.
3. Optionally configure `GEMINI_MODEL`. Leave `VITE_API_BASE_URL` blank for the
   existing same-origin Services routing.
4. Redeploy the updated project after adding the server variable.
5. Sign in, select an authorized persisted case, type an unprompted question,
   then a follow-up, and verify evidence citations and history. Suggestions use
   the identical endpoint. Inspect failed requests for explicit status/errors.

`vercel.json` and hosting providers are unchanged. The Vercel CLI credential was
invalid during implementation; no attempt was made to configure secrets or
redeploy through that CLI. The user will configure the rotated key and redeploy.

Existing storage limitations still apply: Vercel `/tmp` SQLite is neither durable
nor shared across function instances, including its login-session table. Adding
a Gemini credential does not repair that deployment's case/session persistence.
No paid infrastructure or new hosting was provisioned.

## Authentication and grounding

Chat requires a server-issued bearer session, including in development. Selected
case access is checked before the model call. With no selection, context contains
only cases allowed by the existing owner rules, capped at five. Neo4j context
checks ownership before retrieving its subgraph too.

Context includes persisted metadata, normalized roles and provenance, graph edges
and evidence references, document hashes, line-numbered source excerpts, and real
entity/relationship/document counts. It never trusts browser-supplied counts.
It caps each case at 200 entities, 250 relationships and 20 documents, with a
100,000-character total JSON budget. Truncation is disclosed to the model. Text
excerpts are ranked by the current question; missing information must be stated.
Excerpt scans are limited to the first 50,000 characters of each document and
flagged when truncated, so large source files are not loaded wholesale for chat.

Evidence is JSON data in a separate message. System instructions explicitly
forbid following instructions from evidence, filenames or past replies, inventing
findings/citations, changing source roles, or equating connectivity with guilt.
No function tools, code execution or external search are enabled. These controls
reduce prompt-injection risk; model factual correctness still requires review.

Conversation uses at most 12 prior messages and 24,000 history characters.
Individual messages are limited to 6,000 characters. The frontend retains only
successful user/model turns for subsequent requests. It resets history and aborts
pending requests when case scope changes, and ignores stale responses. The server
does not treat caller-supplied history as authoritative case evidence.

## Errors and verification

Empty questions return 400; invalid history returns 422; missing login returns
401; unauthorized cases return 403; absent cases return 404. Missing configuration
or model access returns 503; quota/rate limits return 429; provider failures and
empty/blocked output return 502; timeouts return 504. SDK request timeout is 30
seconds with one attempt; the browser aborts after 45 seconds and offers retry.
Raw provider exceptions, keys and request contents are not logged or returned.
Errors never become successful canned answers.

Automated checks mock the SDK network call while exercising real FastAPI routes,
session checks and SQLite context. They verify arbitrary messages, model output,
multi-turn history, limits, evidence/role/citation context, unauthorized-case
exclusion, missing keys, quota errors, timeouts, empty output and secret redaction.
Live Gemini generation requires the environment credential and was not claimed
when no key was configured.

## Source files changed for this work

- `.env.example`, `requirements.txt`, `backend/config.py`
- `backend/models/copilot.py`
- `backend/main.py` (redact private chat inputs from validation errors/logs)
- `backend/routers/graph.py`
- `backend/services/gemini_service.py`
- `backend/services/copilot_context.py`
- `backend/local_store.py` (keep local reprocessing deterministic when a key exists)
- `frontend/src/components/GraphExplorerModule.tsx`
- `tests/integration/test_gemini_copilot.py`
- `tests/integration/test_local_sqlite_workflow.py`
- `tests/unit/test_ai_insights.py`
- `docs/COPILOT_GEMINI.md`, `VERCEL_DEPLOYMENT.md`
- Generated `frontend/dist/index.html` and `frontend/dist/assets/index-DWTvUHHN.js`
  (replacing the previously tracked JavaScript bundle).

## Results on 2026-10-26

- `pytest -q tests/unit tests/integration`: 270 passed, no failures. Existing SDK
  and Starlette deprecation warnings remain.
- `npm run build`: passed, including TypeScript compilation. Existing large
  JavaScript bundle warning remains.
- Local browser: typed a previously unprompted driver-identity question into the
  real UI, sent it to the real endpoint, saw the explicit missing-key 503, verified
  empty-input validation, retry, and restored Ask button. No fake success was shown.
- Mocked SDK tests verify generated answers, multi-turn messages, real persisted
  grounding, authorization, and provider failure handling without a live key.
- Live Gemini generation: not run; no local GEMINI_API_KEY is configured.
- Read-only production reachability: homepage and `/api/health` returned HTTP 200.
  `/openapi.json` is handled by the frontend catch-all, not the API documentation
  route, so it was not used to infer deployed chat capabilities.
- Git integration was confirmed: `origin` is
  `https://github.com/harshvn08/mlritm-project.git`, and commit `b5beaaf` has a
  successful **Vercel** check for project `atlas-criminal-network`.
- Before publication, production chat returned `ai_model: local-deterministic-qa`
  and `intent: case_required` for a generic, unscoped question. That old route
  never checked the Gemini key, so it did not establish whether the production
  secret was set. Both the local process and server settings had no key.
- The user has now authorized committing and pushing the tested code. A Git push
  can trigger the linked Vercel deployment. Secret configuration remains a manual
  dashboard step; a live generated answer must still be verified after deployment.
