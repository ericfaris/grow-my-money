"""Read-only web dashboard (FastAPI + uvicorn).

Runs as a second process inside the existing ``app`` container (see
``entrypoint.sh``). Serves one static HTML page and one JSON data endpoint,
both STRICTLY READ-ONLY: it opens its own ``mode=ro`` SQLite connection per
request (never ``src.state.State``) and never touches an order path, a cap, or
the trade loop. Bind ``0.0.0.0`` inside the container; host exposure is
restricted to loopback by the compose ``ports`` mapping (section 2.7).
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import load_config
from .dashboard_data import PriceProvider, ReadOnlyState, build_payload
from .logging_setup import setup_logging

log = logging.getLogger(__name__)

DB_PATH = "state/grow.db"
_INDEX_HTML = Path(__file__).parent / "dashboard_web" / "index.html"
_FAVICON = Path(__file__).parent / "dashboard_web" / "favicon.ico"
_SHOWCASE_HTML = Path(__file__).parent / "dashboard_web" / "showcase.html"

app = FastAPI(title="grow-my-money dashboard", docs_url=None, redoc_url=None)

_cfg = load_config()
# One module-level PriceProvider so its TTL cache persists across requests.
_price_provider = PriceProvider(_cfg)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(_FAVICON, media_type="image/vnd.microsoft.icon")


@app.get("/design-system", response_class=HTMLResponse, include_in_schema=False)
def design_system() -> HTMLResponse:
    """Static design-system showcase — renders live from index.html's own
    <style> block (fetched client-side from "/"), never a hand-copied one.
    See DESIGN.md at the repo root."""
    return HTMLResponse(_SHOWCASE_HTML.read_text(encoding="utf-8"))


@app.get("/api/data")
def api_data() -> JSONResponse:
    ro = ReadOnlyState(DB_PATH)
    try:
        payload = build_payload(_cfg, ro, _price_provider)
        return JSONResponse(payload)
    finally:
        ro.close()


def main() -> None:
    setup_logging()
    import uvicorn
    host = getattr(_cfg, "dashboard_host", "0.0.0.0")
    port = getattr(_cfg, "dashboard_port", 8420)
    log.info("Starting dashboard on http://%s:%d (read-only)", host, port)
    uvicorn.run(app, host=host, port=port, access_log=False, log_level="info")


if __name__ == "__main__":
    main()
