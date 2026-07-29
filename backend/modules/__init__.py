"""Service modules.

Each module owns one Postgres schema and exposes a service interface. Modules must
not import each other's `models` or `repository` — only `modules.<other>.service`.
Enforced by the import-linter contract in `pyproject.toml` (docs/01 §3).
"""
