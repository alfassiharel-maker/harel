"""The dependency contract, enforced by reading the imports.

The specification promises three things that are easy to say and easy to break:
the scoring layer depends on nothing, only one module touches the network, and
nothing below the pipeline reaches upward. A static check keeps those true
without adding a lint dependency, and runs on a bare machine like the rest.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "scout"

#: Everything the package is allowed to import beyond the standard library.
#: Deliberately short: `anthropic` is the only third-party name in the project,
#: and it may appear in exactly one module.
THIRD_PARTY = frozenset({"anthropic"})

STDLIB_ALLOWED = frozenset(
    {
        "__future__",
        "argparse",
        "ast",
        "dataclasses",
        "datetime",
        "json",
        "os",
        "pathlib",
        "re",
        "sys",
        "typing",
    }
)

#: Which scout modules each module may import. A module absent from this map may
#: import nothing from the package at all.
ALLOWED_INTERNAL: dict[str, frozenset[str]] = {
    "cli": frozenset({"llm", "models", "pipeline", "report"}),
    "__main__": frozenset({"cli"}),
    "pipeline": frozenset({"llm", "models", "prompts", "report"}),
    "report": frozenset({"models", "scoring"}),
    "models": frozenset({"scoring"}),
    "llm": frozenset(),
    "prompts": frozenset(),
    "scoring": frozenset(),
}


def modules() -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        trees[path.stem] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return trees


def imports(tree: ast.Module) -> set[str]:
    """Top-level names imported anywhere in the module, lazy imports included."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module.split(".")[0] if node.level == 0 else node.module)
    return names


def scout_imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("scout"):
            parts = node.module.split(".")
            if len(parts) > 1:
                names.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "scout" and len(parts) > 1:
                    names.add(parts[1])
    return names


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trees = modules()
        self.assertIn("scoring", self.trees, "the package did not parse")

    def test_every_module_is_covered_by_the_contract(self) -> None:
        """A new module must state its dependencies, or this test fails."""
        uncovered = set(self.trees) - set(ALLOWED_INTERNAL) - {"__init__"}
        self.assertEqual(set(), uncovered)

    def test_scoring_imports_nothing_from_this_project(self) -> None:
        self.assertEqual(set(), scout_imports(self.trees["scoring"]))

    def test_scoring_needs_nothing_installed(self) -> None:
        self.assertEqual(set(), imports(self.trees["scoring"]) & THIRD_PARTY)

    def test_internal_dependencies_follow_the_declared_layering(self) -> None:
        for name, tree in self.trees.items():
            if name == "__init__":
                continue
            with self.subTest(module=name):
                self.assertLessEqual(scout_imports(tree), ALLOWED_INTERNAL[name])

    def test_only_the_provider_boundary_imports_a_provider(self) -> None:
        for name, tree in self.trees.items():
            with self.subTest(module=name):
                third_party = imports(tree) & THIRD_PARTY
                if name == "llm":
                    self.assertEqual({"anthropic"}, third_party)
                else:
                    self.assertEqual(set(), third_party)

    def test_only_the_provider_boundary_reads_the_environment(self) -> None:
        """The key is read in one place, so there is one place to audit."""
        for name, tree in self.trees.items():
            with self.subTest(module=name):
                if name == "llm":
                    continue
                self.assertNotIn("os", imports(tree))

    def test_no_undeclared_third_party_dependency_creeps_in(self) -> None:
        allowed = STDLIB_ALLOWED | THIRD_PARTY | {"scout"}
        for name, tree in self.trees.items():
            with self.subTest(module=name):
                self.assertLessEqual(imports(tree), allowed)


class SecretHygieneTests(unittest.TestCase):
    def test_the_key_name_appears_only_where_it_is_read_and_documented(self) -> None:
        offenders = [
            path.name
            for path in sorted(PACKAGE.glob("*.py"))
            if "ANTHROPIC_API_KEY" in path.read_text(encoding="utf-8")
            and path.name != "llm.py"
        ]
        self.assertEqual([], offenders)

    def test_no_source_file_contains_a_key_shaped_literal(self) -> None:
        for path in sorted(PACKAGE.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertNotIn("sk-ant-", path.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
