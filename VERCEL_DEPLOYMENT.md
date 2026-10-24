# ATLAS on Vercel

The root [`vercel.json`](vercel.json) defines one Vercel project with two
services: the existing Vite application at `frontend/` and the existing
FastAPI app at `backend.main:app`. The `/api` and `/api/*` rewrites are listed
before the frontend catch-all, and preserve the original request path. The
frontend service falls back to `index.html` for client-side navigation.
Vercel Services are currently documented as beta and are available on all
plans. See [Vercel Services](https://vercel.com/docs/services) and
[service routing](https://vercel.com/docs/services/routing).

The browser API client already defaults to same-origin paths such as
`/api/health`; leave `VITE_API_BASE_URL` unset for this deployment. The Vite
development proxy to `127.0.0.1:8000` is used only by `npm run dev` and is not
included in production requests. Same-origin requests do not need CORS setup.

## Deploy manually

1. Review the repository and push it to a Git provider connected to Vercel.
   `.gitignore` and `.vercelignore` exclude `.env` files, runtime databases,
   account files, uploaded evidence, and local datasets. Do not force-add them.
2. In Vercel, create a project from the repository and keep the repository
   root as the project root (do not set `frontend/` as the project root).
   Vercel reads `vercel.json` to build both services.
3. Enable/accept the Vercel Services configuration if prompted, then deploy.
   No Neo4j server, API key, database password, or environment variable is
   required for an ephemeral demo deployment.
4. Open the URL Vercel assigns. Verify `/api/health` returns JSON with
   `"status":"healthy"` and `"database":"sqlite"`; verify `/api/auth/login`
   and `/api/auth/signup` reach FastAPI (the login route returns an expected
   validation/authentication response when called without valid credentials).
5. Register an officer in the existing UI, then test uploads smaller than
   the function request limit and check Graph Explorer. This is a demo only;
   the uploaded records may disappear at any time.

To preview locally with Vercel's service router, install/use the current
Vercel CLI and run from the repository root:

```powershell
npx vercel@latest dev --local
```

This does not deploy. The CLI prints the local URL. Once you have reviewed the
configuration and linked the project yourself, a production deployment is a
separate manual action (`npx vercel --prod` or **Deploy** in Vercel).

## Persistence and request-size limitations

Vercel Functions have a read-only application filesystem; `/tmp` is writable
scratch space, not persistent storage. ATLAS automatically redirects its
SQLite database, uploaded-file directory, officer JSON, and evidence ledger
to `/tmp/atlas` when Vercel sets `VERCEL=1`. This prevents writes from failing
against the read-only deployment bundle, but it does **not** provide durable
or shared state. Each serverless instance can have a separate temporary
directory, and its contents can disappear on a cold start, scale-out, or
redeploy. A healthy SQLite response therefore confirms only that the current
function instance can open its temporary database; it does not confirm
persistence. Do not use this setup for real investigations or as the only
copy of any case.

Durable case storage requires an external persistent database plus durable
document storage (or a database schema that stores the document bytes). The
current SQLite implementation uses SQLite-only SQL and filesystem paths, so
it cannot be made durable by setting a connection string alone; a compatible
PostgreSQL storage adapter and document-storage integration are required.
Those services require their own connection credentials/tokens. No such
service has been provisioned or silently added here. Vercel's own docs list
`/tmp` as the writable area for Functions:
[runtime filesystem](https://vercel.com/docs/functions/runtimes).

Vercel also limits function request bodies to 4.5 MB. ATLAS's local upload
limit is 20 MB per file, so on Vercel the effective limit is the smaller
4.5 MB total multipart request (all selected files plus multipart overhead).
Larger uploads need direct-to-object-storage upload support or a different
hosting path; the Vercel limit cannot be raised in this repository.
[Vercel payload limit](https://vercel.com/docs/errors/function_payload_too_large).

## Authentication and public access

The existing sign-in/register endpoints remain available, and Vercel starts
without the local demo user's seeded credentials. However, the existing
backend does not currently require an authenticated session on case, graph,
or ingestion APIs. Treat this public deployment as accessible to anyone with
the URL and use synthetic data only until server-side authorization is added.
