"""HTTP API and static host for the CCP-AI console.

Standard library `http.server` on a threading server. That is a deliberate
constraint, not an oversight: the product's claim lives in `engine/`, and a
reviewer must be able to clone this repository, run one command with no
virtualenv and no packages, and watch real bytes disappear. The production
service is FastAPI behind a gateway; nothing in this file is load-bearing for the
compression claim.

Security posture, since this will be demoed on shared networks:

* Binds to 127.0.0.1 unless `--host` says otherwise, and warns when it does not.
* Every request resolves a tenant id and every store access goes through that
  tenant's repository. There is no endpoint that reads across tenants and no
  admin bypass.
* Optional bearer token via `CCP_API_TOKEN`, compared with `compare_digest`.
* Request bodies are capped; JSON is required to be an object; ids are validated
  against a strict pattern before they touch a path.
* Errors return a short message. Stack traces go to the log, never the client.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import sys
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.generate_models import Arch, DEFAULT_VARIANTS, build_base, build_variant  # noqa: E402
from engine import __version__ as ENGINE_VERSION  # noqa: E402
from engine import codec, metrics, pack, safetensors  # noqa: E402
from store import IdentifierError, RepositoryError, RepositoryRegistry, validate_id  # noqa: E402

from .jobs import JobBusy, JobRunner  # noqa: E402

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
MAX_BODY_BYTES = 8 * 1024
DEFAULT_TENANT = "demo"
_TENANT_HEADER = "X-CCP-Tenant"


class Api:
    """Request handling, independent of the HTTP plumbing so it stays testable."""

    def __init__(self, store_root: str, token: str | None = None) -> None:
        self.registry = RepositoryRegistry(store_root)
        self.jobs = JobRunner()
        self._token = token or None

    # ---- auth and tenancy --------------------------------------------------

    def authorised(self, header_value: str | None) -> bool:
        if not self._token:
            return True
        if not header_value or not header_value.startswith("Bearer "):
            return False
        return hmac.compare_digest(header_value[len("Bearer ") :].strip(), self._token)

    def tenant_of(self, headers, query: dict[str, list[str]]) -> str:
        raw = headers.get(_TENANT_HEADER) or (query.get("tenant") or [DEFAULT_TENANT])[0]
        return validate_id(raw.strip().lower(), "tenant id")

    # ---- read endpoints ---------------------------------------------------

    def health(self) -> dict:
        return {
            "status": "ok",
            "engine_version": ENGINE_VERSION,
            "python": sys.version.split()[0],
            "unix": round(time.time(), 3),
        }

    def state(self, tenant_id: str) -> dict:
        repo = self.registry.get(tenant_id)
        summary = repo.summary()
        job = self.jobs.current(tenant_id)
        ratio = repo.variant_ratio()

        projection = None
        if ratio is not None:
            projection = metrics.project_at_scale(measured_variant_ratio=ratio).as_dict()

        return {
            "engine_version": ENGINE_VERSION,
            "tenant_id": tenant_id,
            "ccp_enabled": summary["mode"] == "ccp",
            "repository": summary,
            "costs": {
                "disk_usd_month_now": metrics.monthly_storage_usd(int(summary["disk_bytes"])),
                "disk_usd_month_if_full": metrics.monthly_storage_usd(int(summary["disk_bytes_if_full"])),
                "disk_usd_month_if_ccp": metrics.monthly_storage_usd(int(summary["disk_bytes_if_ccp"])),
                "unit_prices": {
                    "storage_usd_per_gib_month": metrics.STORAGE_USD_PER_GIB_MONTH,
                    "egress_usd_per_gib": metrics.EGRESS_USD_PER_GIB,
                },
            },
            "projection": projection,
            "job": job.as_dict() if job else None,
            "seeded": summary["base"] is not None,
        }

    def ledger(self, tenant_id: str, limit: int) -> dict:
        repo = self.registry.get(tenant_id)
        ok, error = repo.ledger.verify_chain()
        return {
            "chain_ok": ok,
            "chain_error": error,
            "entries": [e.as_dict() for e in repo.ledger.read(limit=limit)],
        }

    def inspect(self, tenant_id: str, variant_id: str) -> dict:
        """Per-tensor breakdown of one variant: what was copied, deltaed, stored."""
        validate_id(variant_id, "variant id")
        repo = self.registry.get(tenant_id)
        path = os.path.join(repo.root, "plans", f"{variant_id}.json")
        if not os.path.exists(path):
            raise RepositoryError(f"no plan recorded for variant {variant_id!r}")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # ---- write endpoints --------------------------------------------------

    def set_mode(self, tenant_id: str, enabled: bool) -> dict:
        repo = self.registry.get(tenant_id)
        if self.jobs.busy(tenant_id):
            raise JobBusy("a job is running; wait for it to finish before switching modes")
        result = repo.set_mode("ccp" if enabled else "full")
        return {"switch": result, "state": self.state(tenant_id)}

    def verify(self, tenant_id: str) -> dict:
        repo = self.registry.get(tenant_id)
        return repo.verify_all()

    def projection(self, tenant_id: str, params_billions: float, variants: int, downloads: int) -> dict:
        repo = self.registry.get(tenant_id)
        ratio = repo.variant_ratio()
        if ratio is None:
            raise RepositoryError("nothing measured yet — seed the repository first")
        return metrics.project_at_scale(
            measured_variant_ratio=ratio,
            params_billions=params_billions,
            variants=variants,
            downloads_per_variant=downloads,
        ).as_dict()

    def download(self, tenant_id: str, variant_id: str) -> tuple[bytes, float]:
        repo = self.registry.get(tenant_id)
        return repo.materialise(variant_id)

    def seed(self, tenant_id: str, arch: Arch, reset: bool) -> dict:
        """Generate the demo family and register it. Runs as a background job."""
        repo = self.registry.get(tenant_id)

        def work(job) -> dict:
            if reset:
                repo.reset()
            total_units = 1 + len(DEFAULT_VARIANTS)

            job.message = "generating base checkpoint"
            job.steps.append("generating base checkpoint")
            base_blob, tensors = build_base(arch, progress=lambda msg, frac: _tick(job, 0, total_units, frac, msg))
            repo.set_base(base_blob, base_id="base", label=f"Base · {arch.param_count():,} params")
            job.steps.append(f"base registered · {metrics.fmt_bytes(len(base_blob))}")

            registered = []
            for index, spec in enumerate(DEFAULT_VARIANTS, start=1):
                job.message = f"deriving {spec.variant_id}"
                job.steps.append(f"deriving {spec.label}")
                blob = build_variant(
                    tensors, spec, arch, progress=lambda msg, frac: _tick(job, index, total_units, frac * 0.5, msg)
                )
                job.message = f"packing {spec.variant_id}"
                _tick(job, index, total_units, 0.6, f"packing {spec.variant_id}")
                record = repo.add_variant(blob, variant_id=spec.variant_id, label=spec.label, kind=spec.kind)
                self._write_plan(repo, spec, blob, base_blob)
                _tick(job, index, total_units, 1.0, f"stored {spec.variant_id}")
                job.steps.append(
                    f"{spec.variant_id} · {metrics.fmt_bytes(record.raw_bytes)} → "
                    f"{metrics.fmt_bytes(record.container_bytes)}"
                )
                registered.append(record.as_dict())

            return {"base_bytes": len(base_blob), "variants": registered, "state": self.state(tenant_id)}

        return self.jobs.submit(tenant_id, "seed", work).as_dict()

    def _write_plan(self, repo, spec, blob: bytes, base_blob: bytes) -> None:
        """Persist the per-tensor plan for the inspector.

        Written outside the `variants/` directory on purpose: `variants/` is what
        the dashboard measures with `os.stat`, and metadata about a saving must
        not inflate the saving.
        """
        container, _ = pack.pack(base_blob, blob, base_id="base", variant_id=spec.variant_id, verify=False)
        detail = pack.describe(container)
        detail["label"] = spec.label
        detail["kind"] = spec.kind
        detail["note"] = spec.note
        detail["relative_step"] = spec.relative_step
        detail["plane_report"] = self._plane_sample(base_blob, blob)
        directory = os.path.join(repo.root, "plans")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"{spec.variant_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(detail, fh, indent=1)

    @staticmethod
    def _plane_sample(base_blob: bytes, variant_blob: bytes) -> list[dict[str, object]] | None:
        """Byte-plane compressibility of the largest shared tensor.

        This is the panel that explains the mechanism rather than asserting it:
        the sign/exponent plane is nearly free, the low mantissa plane is nearly
        incompressible, and the product is the distance between them.
        """
        base = safetensors.parse(base_blob)
        variant = safetensors.parse(variant_blob)
        base_by_name = base.by_name()
        best = None
        for entry in variant.entries:
            counterpart = base_by_name.get(entry.name)
            if counterpart is None or counterpart.signature() != entry.signature():
                continue
            if base.tensor_bytes(counterpart) == variant.tensor_bytes(entry):
                continue
            if best is None or entry.nbytes > best[0].nbytes:
                best = (entry, counterpart)
        if best is None:
            return None
        entry, counterpart = best
        report = codec.plane_report(base.tensor_bytes(counterpart), variant.tensor_bytes(entry), entry.itemsize)
        return [{"tensor": entry.name, **row} for row in report]


def _tick(job, unit_index: int, total_units: int, unit_fraction: float, message: str) -> None:
    job.fraction = min(0.999, (unit_index + unit_fraction) / total_units)
    job.message = message


# -----------------------------------------------------------------------------
# HTTP plumbing
# -----------------------------------------------------------------------------

_SAFE_STATIC = re.compile(r"^[A-Za-z0-9._-]+$")


class Handler(BaseHTTPRequestHandler):
    server_version = f"ccp-ai/{ENGINE_VERSION}"
    protocol_version = "HTTP/1.1"
    api: Api  # injected on the server instance

    # ---- helpers ----------------------------------------------------------

    def _send(self, status: HTTPStatus, payload: dict | list, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        # The console loads no third-party anything, so the policy can be strict.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send(status, {"error": message})

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("Content-Length is not a number")
        if length > MAX_BODY_BYTES:
            raise BadRequest(f"request body larger than {MAX_BODY_BYTES} bytes")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequest(f"body is not valid JSON: {exc}")
        if not isinstance(parsed, dict):
            raise BadRequest("body must be a JSON object")
        return parsed

    def log_message(self, fmt: str, *args) -> None:
        # No request bodies, no tenant payloads — path and status only.
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {fmt % args}\n")

    # ---- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        path = url.path
        query = parse_qs(url.query)
        api = self.api

        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    return self._error(HTTPStatus.METHOD_NOT_ALLOWED, "static routes are GET only")
                return self._serve_static(path)

            if not api.authorised(self.headers.get("Authorization")):
                return self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid bearer token")

            tenant = api.tenant_of(self.headers, query)

            if method == "GET":
                return self._get(path, query, tenant)
            return self._post(path, query, tenant)

        except BadRequest as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except IdentifierError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except JobBusy as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except (RepositoryError, pack.ContainerError, safetensors.SafetensorsError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except BrokenPipeError:
            pass  # the browser navigated away mid-response
        except Exception:  # noqa: BLE001 — the server must not die on one request
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def _get(self, path: str, query: dict[str, list[str]], tenant: str) -> None:
        api = self.api
        if path == "/api/health":
            return self._send(HTTPStatus.OK, api.health())
        if path == "/api/state":
            return self._send(HTTPStatus.OK, api.state(tenant))
        if path == "/api/job":
            job = api.jobs.current(tenant)
            return self._send(HTTPStatus.OK, job.as_dict() if job else {"state": "idle"})
        if path == "/api/ledger":
            limit = _int_arg(query, "limit", default=50, low=1, high=500)
            return self._send(HTTPStatus.OK, api.ledger(tenant, limit))
        if path == "/api/tenants":
            # Names only, and only so the demo can show that isolation exists.
            # No sizes, no digests, nothing about another tenant's models.
            return self._send(HTTPStatus.OK, {"tenants": api.registry.tenants()})
        if path.startswith("/api/variants/"):
            rest = path[len("/api/variants/") :]
            if rest.endswith("/inspect"):
                return self._send(HTTPStatus.OK, api.inspect(tenant, rest[: -len("/inspect")]))
            if rest.endswith("/download"):
                return self._download(tenant, rest[: -len("/download")])
        return self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    def _post(self, path: str, query: dict[str, list[str]], tenant: str) -> None:
        api = self.api
        body = self._body()

        if path == "/api/mode":
            if "enabled" not in body or not isinstance(body["enabled"], bool):
                raise BadRequest("body must be {\"enabled\": true|false}")
            return self._send(HTTPStatus.OK, api.set_mode(tenant, body["enabled"]))

        if path == "/api/seed":
            arch = Arch(
                d_model=_int_body(body, "d_model", Arch().d_model, 32, 512),
                n_layers=_int_body(body, "layers", Arch().n_layers, 1, 12),
                vocab=_int_body(body, "vocab", Arch().vocab, 256, 32768),
            )
            reset = bool(body.get("reset", True))
            return self._send(HTTPStatus.ACCEPTED, api.seed(tenant, arch, reset))

        if path == "/api/verify":
            return self._send(HTTPStatus.OK, api.verify(tenant))

        if path == "/api/projection":
            return self._send(
                HTTPStatus.OK,
                api.projection(
                    tenant,
                    params_billions=_float_body(body, "params_billions", 7.0, 0.1, 2000.0),
                    variants=_int_body(body, "variants", 10, 1, 100_000),
                    downloads=_int_body(body, "downloads_per_variant", 100_000, 1, 1_000_000_000),
                ),
            )

        if path == "/api/reset":
            api.registry.get(tenant).reset()
            return self._send(HTTPStatus.OK, api.state(tenant))

        return self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    def _download(self, tenant: str, variant_id: str) -> None:
        blob, elapsed = self.api.download(tenant, variant_id)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Content-Disposition", f'attachment; filename="{variant_id}.safetensors"')
        # The proof, on the wire: how long the rebuild took and what came out.
        self.send_header("X-CCP-Rebuild-Seconds", f"{elapsed:.4f}")
        self.send_header("X-CCP-SHA256", codec.sha256(blob))
        self._security_headers()
        self.end_headers()
        self.wfile.write(blob)

    def _serve_static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        if not _SAFE_STATIC.match(name):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        full = os.path.join(WEB_ROOT, name)
        if not os.path.isfile(full):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        with open(full, "rb") as fh:
            body = fh.read()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text/") else ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)


class BadRequest(ValueError):
    """Client sent something malformed."""


def _int_arg(query: dict[str, list[str]], key: str, *, default: int, low: int, high: int) -> int:
    raw = (query.get(key) or [None])[0]
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest(f"{key} must be an integer")
    return max(low, min(high, value))


def _int_body(body: dict, key: str, default: int, low: int, high: int) -> int:
    if key not in body:
        return default
    try:
        value = int(body[key])
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be an integer")
    if not low <= value <= high:
        raise BadRequest(f"{key} must be between {low} and {high}")
    return value


def _float_body(body: dict, key: str, default: float, low: float, high: float) -> float:
    if key not in body:
        return default
    try:
        value = float(body[key])
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be a number")
    if not low <= value <= high:
        raise BadRequest(f"{key} must be between {low} and {high}")
    return value


def build_server(host: str, port: int, store_root: str, token: str | None = None) -> ThreadingHTTPServer:
    api = Api(store_root=store_root, token=token)
    handler = type("BoundHandler", (Handler,), {"api": api})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
