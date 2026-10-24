"""Resolve ATLAS writable state paths for local and serverless deployments."""
import os
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    """Use Vercel's writable scratch area when deployed there.

    Vercel functions have an immutable application filesystem. This location
    only makes SQLite/file writes possible during execution; it is ephemeral
    and must not be treated as durable storage.
    """
    configured = os.environ.get("ATLAS_DATA_DIR")
    if configured:
        return Path(configured)
    if os.environ.get("VERCEL") == "1":
        # tempfile.gettempdir() is /tmp on Vercel Linux and remains usable when
        # running `vercel dev -L` from Windows during local verification.
        return Path(tempfile.gettempdir()) / "atlas"
    return PROJECT_ROOT / "data"
