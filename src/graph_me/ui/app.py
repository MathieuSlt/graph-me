"""The local web UI: search, graph, entity pages, provenance and status. Read-only.

Safety (plan, "Web UI"):

- binds to 127.0.0.1 only, and rejects requests whose Host is not localhost (DNS rebinding);
- a random token is printed in the startup URL; opening it sets an HttpOnly, SameSite=Strict
  cookie, and every request needs that cookie or the token;
- GET and HEAD only: there is nothing to write;
- every text is escaped (Jinja autoescape), message HTML is never rendered, and a strict
  Content-Security-Policy allows only the app's own scripts;
- like MCP, no page can reveal redacted secrets: `graph-me query --reveal` stays in the terminal.
"""

from __future__ import annotations

import hmac
import re
import secrets
from pathlib import Path
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from graph_me import __version__
from graph_me.service import Service, StoreMissing

HERE = Path(__file__).parent
COOKIE = "graph_me_token"
KINDS = ("file", "email", "message", "contact")
ENTITY_KINDS = ("person", "project", "document", "org", "place")
GRAPH_LIMIT = 2000
_BLANK_LINES = re.compile(r"\s*\n\s*\n\s*")
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",  # the token never leaves in a Referer header
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def new_token() -> str:
    return secrets.token_urlsafe(24)


class _Guard(BaseHTTPMiddleware):
    """Token check, GET-only, security headers."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self.token = token

    def _ok(self, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate, self.token)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in ("GET", "HEAD"):
            response: Response = PlainTextResponse("graph-me is read-only.", status_code=405)
        elif self._ok(request.query_params.get("token")):
            # Trade the URL token for a cookie, and drop it from the address bar and history.
            rest = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
            target = request.url.path + (f"?{urlencode(rest)}" if rest else "")
            response = RedirectResponse(target, status_code=303)
            response.set_cookie(
                COOKIE, self.token, httponly=True, samesite="strict", secure=False, path="/"
            )
        elif self._ok(request.cookies.get(COOKIE)):
            response = await call_next(request)
        else:
            response = PlainTextResponse(
                "Open the URL printed by `graph-me ui` (it carries the access token).",
                status_code=403,
            )
        response.headers.update(SECURITY_HEADERS)
        return response


def _dates(request: Request) -> tuple[str | None, str | None]:
    return request.query_params.get("since") or None, request.query_params.get("until") or None


def build_app(service: Service, token: str) -> Starlette:
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(version=__version__, kinds=KINDS, entity_kinds=ENTITY_KINDS)
    templates.env.filters["squeeze"] = lambda text: _BLANK_LINES.sub("\n", text or "")

    def page(request: Request, name: str, status_code: int = 200, **context) -> Response:
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def sources() -> list[str]:
        return sorted(service.cfg.sources)

    async def search(request: Request) -> Response:
        q = (request.query_params.get("q") or "").strip()
        kind = request.query_params.get("kind") or None
        source = request.query_params.get("source") or None
        since, until = _dates(request)
        result = None
        if q:
            result = service.search(
                q, limit=25, kind=kind if kind in KINDS else None, source=source,
                since=since, until=until,
            )  # fmt: skip
        context = dict(q=q, kind=kind, source=source, since=since, until=until,
                       sources=sources(), result=result)  # fmt: skip
        # htmx asks for the results only; a plain request (or a reload) gets the whole page.
        if request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore"):
            return page(request, "_results.html", **context)
        return page(request, "search.html", **context)

    async def entity(request: Request) -> Response:
        info = service.entity_page(request.path_params["eid"])
        if info is None:
            return page(request, "missing.html", status_code=404, what="entity")
        return page(request, "entity.html", e=info)

    async def item(request: Request) -> Response:
        info = service.item_page(request.path_params["iid"])
        if "error" in info:
            return page(request, "missing.html", status_code=404, what="item")
        return page(request, "item.html", item=info)

    async def graph_page(request: Request) -> Response:
        return page(request, "graph.html", sources=sources())

    async def graph_api(request: Request) -> Response:
        kinds = [k for k in request.query_params.getlist("kind") if k in ENTITY_KINDS] or None
        since, until = _dates(request)
        try:
            limit = min(int(request.query_params.get("limit", GRAPH_LIMIT)), GRAPH_LIMIT)
        except ValueError:
            limit = GRAPH_LIMIT
        data = service.graph_view(
            limit=max(limit, 1), kinds=kinds, source=request.query_params.get("source") or None,
            since=since, until=until,
        )  # fmt: skip
        return JSONResponse(data)

    async def neighbours_api(request: Request) -> Response:
        return JSONResponse(service.neighbourhood(request.path_params["eid"]))

    async def status(request: Request) -> Response:
        return page(request, "status.html", s=service.status())

    async def no_store(request: Request, exc: Exception) -> Response:
        return page(request, "missing.html", status_code=503, what="store", detail=str(exc))

    return Starlette(
        routes=[
            Route("/", search),
            Route("/search", search),
            Route("/entity/{eid}", entity),
            Route("/item/{iid}", item),
            Route("/graph", graph_page),
            Route("/api/graph", graph_api),
            Route("/api/graph/{eid}", neighbours_api),
            Route("/status", status),
            Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        ],
        middleware=[
            Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"]),
            Middleware(_Guard, token=token),
        ],
        exception_handlers={StoreMissing: no_store},
    )
