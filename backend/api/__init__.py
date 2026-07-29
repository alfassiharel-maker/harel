"""FastAPI transport layer.

Transport only: validate the request, call a service, serialise the result. No
SQL and no business rules live here — that is the layering contract in docs/01 §3,
enforced by the import-linter rule that forbids `backend.api` from importing any
module's `repository`.
"""
