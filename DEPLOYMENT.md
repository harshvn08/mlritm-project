# Deploy ATLAS for a public demo

ATLAS uses the existing React 19/Vite frontend and FastAPI backend. The
backend serves the built frontend and `/api/*` routes from the same origin.
SQLite stores cases, extracted entities, and relationships; uploaded files,
officer accounts, and the evidence ledger are also stored under the persistent
data directory. Local extraction does not require Neo4j or an external AI key.

## Recommended host: Render

This repository includes `render.yaml` and a multi-stage `Dockerfile` for one
Render web service. The Blueprint provisions a 1 GB persistent disk mounted at
`/var/data`, sets SQLite and file-storage paths there, enables production mode,
and configures `/api/health` as the service health check. Render persistent
disks require a paid service plan; the Blueprint uses the `starter` plan.

1. Before publishing the repository, inspect it for private material. Keep
   `.env`, `data/`, uploaded evidence, and local databases out of Git. The
   Docker build independently excludes `.env*`, `data/`, `dataset/`, and test
   files, so local investigation records and credentials are not copied into
   the public image.
2. Push the project repository to a private or public GitHub repository.
3. In Render, choose **New + → Blueprint**, connect that repository, and deploy
   the detected `render.yaml`. Approve the paid web service and persistent disk.
4. Wait for the service health check to pass. Render assigns a public
   `https://<service-name>.onrender.com` URL. Open it, then confirm
   `https://<service-name>.onrender.com/api/health` reports `healthy` and
   `database: sqlite`.
5. Share the service URL with your friend. The API is same-origin, so no
   localhost URL, extra CORS setting, `.env` file, database password, or API
   key is needed. If you later host the frontend separately, set the frontend's
   build-time `VITE_API_BASE_URL` to the backend origin and set the backend's
   `CORS_ORIGINS` to the frontend's exact origin(s), comma-separated.

## Local commands

On Windows, `start.bat` checks prerequisites, initializes SQLite, and starts
the local backend and Vite frontend. For a production-like container build,
from the repository root run:

```powershell
docker build -t atlas-local .
docker run --rm -p 8000:8000 -e PORT=8000 -e ATLAS_DATA_DIR=/var/data -v atlas-data:/var/data atlas-local
```

The local frontend dev server is `http://localhost:3000`; the backend API is
`http://127.0.0.1:8000`, proxied by Vite. In production, FastAPI serves the
built frontend and API together.

## Data and access notes

The deployed service starts with an empty persistent disk; local case records
and user accounts are deliberately not copied to a public deployment. New
records remain on the Render disk across restarts and deploys. Do not upload
real investigation data to a public demo unless you have reviewed who can
access the application and its API. Render disk snapshots are available per
the host's retention and plan policies; maintain a separate backup for
important evidence.

**Authentication limitation:** the existing officer sign-in/register screen
does not currently protect the backend case and graph API routes. Treat data
uploaded to this public service as accessible to anyone who can reach its URL;
use synthetic/demo records only until server-side authorization is added.
