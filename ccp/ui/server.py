"""The application host: a local HTTP server serving the CCP Forge interface.

The product is a desktop application whose window is the system browser. That is
an engineering decision, recorded in `docs/03-application.md`: it needs no GUI
toolkit (none is guaranteed present in a stdlib-only project), it behaves
identically on Windows, macOS and Linux, and it is the only option that can be
driven end to end in tests without a display. The user does not see any of this:
they launch the application and a window opens.

Security posture, because this is a server process on a user's machine:

*   it binds **127.0.0.1 only**, never a routable address;
*   every request must carry a session token minted at startup, so another
    process on the machine cannot drive the user's artifacts by guessing the port;
*   it is not a network service, has no remote access, and reaches nothing
    outbound.

The server is transport. It parses a request, calls the controller, and
serialises the answer. No product logic lives here.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple

from ..integration.errors import InputError
from ..product import ProductError
from .controller import AppController, describe_error
from .web import INDEX_HTML

# An uploaded archive is held in a temporary directory owned by this process.
# Bounded so a drag-and-drop cannot fill the disk.
MAX_UPLOAD_BYTES = 512 << 20

# Any single JSON request body. Generous for a form, far below anything that
# could exhaust memory.
MAX_JSON_BYTES = 1 << 20


class AppServer:
    """Owns the controller, the token, and the HTTP server."""

    def __init__(
        self,
        controller: Optional[AppController] = None,
        commerce_controller = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.controller = controller or AppController()
        self.commerce_controller = commerce_controller
        self.token = secrets.token_urlsafe(24)
        self._upload_dir = tempfile.TemporaryDirectory(prefix="ccp-upload-")
        handler = _make_handler(self)
        # Threading so a slow build cannot block the progress requests that
        # report it. The controller serialises artifact access behind its lock,
        # so the contract's single-caller rule still holds.
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    @property
    def upload_dir(self) -> str:
        return self._upload_dir.name

    def start(self) -> str:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="ccp-http", daemon=True
        )
        self._thread.start()
        return self.url

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        """Stop serving and release everything this server owns."""
        try:
            self._httpd.shutdown()
        finally:
            self._httpd.server_close()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self.controller.close()
            self._upload_dir.cleanup()

    def __enter__(self) -> "AppServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()


def _make_handler(app: AppServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CCPForge"
        sys_version = ""

        # -- plumbing --------------------------------------------------

        def log_message(self, *args) -> None:
            # The application has its own console output; the default handler
            # logs every request to stderr, which is noise in a desktop app.
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # This page loads only what this process serves. The policy makes a
            # stray external reference fail loudly instead of silently working.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: Dict[str, Any], status: int = 200) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _error(self, error: BaseException, status: int) -> None:
            self._json({"error": describe_error(error).as_dict()}, status)

        def _authorised(self, query: Dict[str, list]) -> bool:
            supplied = (query.get("t") or [None])[0]
            if supplied is None:
                supplied = self.headers.get("X-CCP-Token")
            referer = self.headers.get("Referer") or ""
            if supplied is None and referer:
                # The page fetches with a relative URL, so the token rides on the
                # referring document's query string.
                parsed = urllib.parse.urlparse(referer)
                supplied = (urllib.parse.parse_qs(parsed.query).get("t") or [None])[0]
            return supplied is not None and secrets.compare_digest(supplied, app.token)

        def _body(self, limit: int = MAX_JSON_BYTES) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return b""
            if length <= 0:
                return b""
            if length > limit:
                raise InputError(f"request body exceeds {limit} bytes")
            return self.rfile.read(length)

        def _payload(self) -> Dict[str, Any]:
            raw = self._body()
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise InputError("the request body was not valid JSON") from error
            if not isinstance(data, dict):
                raise InputError("the request body must be a JSON object")
            return data

        # -- routing ---------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if not self._authorised(query):
                self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                return
            if parsed.path in ("/", "/index.html"):
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            self._dispatch(_GET_ROUTES, parsed.path, query)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if not self._authorised(query):
                self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                return
            self._dispatch(_POST_ROUTES, parsed.path, query)

        def _dispatch(self, routes: Dict[str, Callable], path: str, query) -> None:
            handler = routes.get(path)
            if handler is None:
                self._json({"error": {"title": "Not found", "what": path,
                                      "why": "No such endpoint.", "next": "",
                                      "kind": "internal"}}, HTTPStatus.NOT_FOUND)
                return
            try:
                status, payload = handler(self, app, query)
            except ProductError as error:
                self._error(error, HTTPStatus.BAD_REQUEST)
            except (ValueError, OSError) as error:
                self._error(error, HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - never leak a traceback
                self._error(error, HTTPStatus.INTERNAL_SERVER_ERROR)
            else:
                self._json(payload, status)

    return Handler


# --------------------------------------------------------------------------
# endpoints — each returns (status, payload)
# --------------------------------------------------------------------------


def _state(handler, app: AppServer, query) -> Tuple[int, dict]:
    return 200, app.controller.snapshot()


def _recents(handler, app: AppServer, query) -> Tuple[int, dict]:
    return 200, {"recents": app.controller.recents()}


def _units(handler, app: AppServer, query) -> Tuple[int, dict]:
    text = (query.get("q") or [""])[0]
    offset = int((query.get("offset") or ["0"])[0])
    limit = min(int((query.get("limit") or ["200"])[0]), 1000)
    return 200, app.controller.units(text, offset, limit)


def _groups(handler, app: AppServer, query) -> Tuple[int, dict]:
    return 200, {"groups": app.controller.groups()}


def _unit(handler, app: AppServer, query) -> Tuple[int, dict]:
    uid = (query.get("uid") or [""])[0]
    return 200, app.controller.unit_detail(uid)


def _analyse(handler, app: AppServer, query) -> Tuple[int, dict]:
    payload = handler._payload()
    return 200, app.controller.analyse(str(payload.get("path") or ""))


def _build(handler, app: AppServer, query) -> Tuple[int, dict]:
    payload = handler._payload()
    app.controller.build_async(str(payload.get("path") or ""), payload.get("label"))
    return 202, {"started": True}


def _cancel(handler, app: AppServer, query) -> Tuple[int, dict]:
    return 200, {"cancelled": app.controller.cancel()}


def _open(handler, app: AppServer, query) -> Tuple[int, dict]:
    payload = handler._payload()
    return 200, app.controller.open_artifact(str(payload.get("path") or "")).as_dict()


def _close(handler, app: AppServer, query) -> Tuple[int, dict]:
    app.controller.close()
    return 200, {"closed": True}


def _verify(handler, app: AppServer, query) -> Tuple[int, dict]:
    return 200, app.controller.verify()


def _read(handler, app: AppServer, query) -> Tuple[int, dict]:
    payload = handler._payload()
    return 200, app.controller.read_range(
        str(payload.get("uid") or ""),
        int(payload.get("offset") or 0),
        int(payload.get("length") or 0),
    )


def _materialize(handler, app: AppServer, query) -> Tuple[int, dict]:
    payload = handler._payload()
    return 200, app.controller.materialize(str(payload.get("uid") or ""))


def _save(handler, app: AppServer, query) -> Tuple[int, dict]:
    payload = handler._payload()
    path = app.controller.save_artifact(
        str(payload.get("path") or ""), bool(payload.get("overwrite"))
    )
    return 200, {"path": path}


def _export(handler, app: AppServer, query) -> Tuple[int, dict]:
    payload = handler._payload()
    return 200, app.controller.export_unit(
        str(payload.get("uid") or ""),
        str(payload.get("dir")) if payload.get("dir") else None,
        payload.get("name"),
        bool(payload.get("overwrite")),
    )


def _clear_error(handler, app: AppServer, query) -> Tuple[int, dict]:
    app.controller.clear_error()
    return 200, {"cleared": True}


def _commerce_state(handler, app: AppServer, query) -> Tuple[int, dict]:
    """Get customer's commercial state (balance, credits, charges, agreement)."""
    if not hasattr(app, "commerce_controller") or not app.commerce_controller:
        return 200, {
            "enabled": False,
            "message": "commercial features not enabled",
        }

    state = app.commerce_controller.get_commercial_state()
    return 200, {
        "enabled": True,
        "customer_id": str(state.customer_id) if state.customer_id else None,
        "customer_name": state.customer_name,
        "agreement_id": str(state.agreement_id) if state.agreement_id else None,
        "contract_version": state.contract_version,
        "terms_accepted": state.terms_accepted,
        "terms_accepted_at": state.terms_accepted_at,
        "account_balance": str(state.account_balance),
        "total_credits": str(state.total_credits),
        "total_charges": str(state.total_charges),
    }


def _accept_terms(handler, app: AppServer, query) -> Tuple[int, dict]:
    """Accept commercial terms."""
    if not hasattr(app, "commerce_controller") or not app.commerce_controller:
        raise ValueError("commercial features not enabled")

    payload = handler._payload()
    terms_version = int(payload.get("terms_version", 1))
    state = app.commerce_controller.accept_terms(terms_version)

    return 200, {
        "accepted": state.terms_accepted,
        "terms_version": state.contract_version,
        "accepted_at": state.terms_accepted_at,
    }


def _measurements(handler, app: AppServer, query) -> Tuple[int, dict]:
    """Get customer's measurement history."""
    if not hasattr(app, "commerce_controller") or not app.commerce_controller:
        return 200, {"measurements": []}

    measurements = app.commerce_controller.get_customer_measurements()
    return 200, {
        "measurements": [
            {
                "measurement_id": str(m.measurement_id),
                "artifact_digest": m.artifact_digest,
                "baseline_bytes": m.baseline_bytes,
                "ccp_bytes": m.ccp_bytes,
                "result_type": m.result_type,
                "calculated_credit": str(m.calculated_credit),
                "calculated_charge": str(m.calculated_charge),
                "status": m.status,
            }
            for m in measurements
        ]
    }


def _dashboard(handler, app: AppServer, query) -> Tuple[int, dict]:
    """Get dashboard summary (agreement, size, savings, balance)."""
    if not hasattr(app, "commerce_controller") or not app.commerce_controller:
        return 200, {
            "enabled": False,
            "message": "commercial features not enabled",
        }

    state = app.commerce_controller.get_commercial_state()
    artifact_summary = app.controller.artifact_summary if app.controller.artifact_summary else None

    return 200, {
        "enabled": True,
        "customer_name": state.customer_name,
        "terms_accepted": state.terms_accepted,
        "account_balance": str(state.account_balance),
        "total_credits": str(state.total_credits),
        "total_charges": str(state.total_charges),
        "baseline_bytes": artifact_summary.original_bytes if artifact_summary else 0,
        "ccp_bytes": artifact_summary.artifact_bytes if artifact_summary else 0,
        "saving_ratio": artifact_summary.saving if artifact_summary else None,
        "verified": artifact_summary.verified if artifact_summary else False,
    }


def _support(handler, app: AppServer, query) -> Tuple[int, dict]:
    """Get support information."""
    return 200, {
        "support_email": "support@example.com",
        "documentation_url": "https://docs.example.com/ccp-forge",
        "version": "1.0.0",
        "commercial_enabled": hasattr(app, "commerce_controller") and app.commerce_controller is not None,
    }


def _upload(handler, app: AppServer, query) -> Tuple[int, dict]:
    """Receive a dropped archive into this process's temporary directory.

    The client-supplied name is never used as a path: only its basename is kept,
    and even that is sanitised, so a name like `../../x.zip` cannot escape the
    upload directory.
    """
    raw_name = (query.get("name") or ["upload.zip"])[0]
    safe = os.path.basename(raw_name.replace("\\", "/")) or "upload.zip"
    safe = "".join(c for c in safe if c.isalnum() or c in "-._") or "upload.zip"
    if not safe.lower().endswith(".zip"):
        raise InputError("only .zip archives can be dropped onto the window")

    data = handler._body(MAX_UPLOAD_BYTES)
    if not data:
        raise InputError("the uploaded file was empty")
    destination = os.path.join(app.upload_dir, safe)
    with open(destination, "wb") as target:
        target.write(data)
    return 200, {"path": destination, "bytes": len(data)}


_GET_ROUTES: Dict[str, Callable] = {
    "/api/state": _state,
    "/api/recents": _recents,
    "/api/units": _units,
    "/api/groups": _groups,
    "/api/unit": _unit,
    "/api/commerce/state": _commerce_state,
    "/api/commerce/measurements": _measurements,
    "/api/commerce/dashboard": _dashboard,
    "/api/support": _support,
}

_POST_ROUTES: Dict[str, Callable] = {
    "/api/analyse": _analyse,
    "/api/build": _build,
    "/api/cancel": _cancel,
    "/api/open": _open,
    "/api/close": _close,
    "/api/verify": _verify,
    "/api/read": _read,
    "/api/materialize": _materialize,
    "/api/save": _save,
    "/api/export": _export,
    "/api/upload": _upload,
    "/api/error/clear": _clear_error,
    "/api/commerce/accept-terms": _accept_terms,
}
