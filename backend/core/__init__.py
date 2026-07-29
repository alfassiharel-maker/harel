"""Cross-cutting primitives: settings, ids, errors, request context, security, logging.

`core` may be imported by every other layer. It must never import `modules`,
`api`, `workers` or `integrations` — the dependency arrow points only inward.
"""
