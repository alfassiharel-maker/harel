"""Tokenising and parsing: shape only, no semantics."""

from __future__ import annotations

import unittest
from fractions import Fraction

from backend.squeeze.diagnostics import DiagnosticBag
from backend.squeeze.lexer import TokenKind, tokenize
from backend.squeeze.nodes import NumberAtom, QuantityAtom, WordAtom
from backend.squeeze.parser import parse


class TestLexer(unittest.TestCase):
    def test_comments_and_blank_lines_are_dropped(self) -> None:
        bag = DiagnosticBag()
        tokens = tokenize("# a comment\nversion 1\n", bag)
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds.count(TokenKind.WORD), 1)
        self.assertFalse(bag.has_errors())

    def test_numbers_are_exact_fractions(self) -> None:
        bag = DiagnosticBag()
        tokens = tokenize("ratio 3.4", bag)
        self.assertEqual(tokens[1].value, Fraction(17, 5))

    def test_underscore_digit_separators(self) -> None:
        bag = DiagnosticBag()
        tokens = tokenize("population 25_000 users", bag)
        self.assertEqual(tokens[1].value, Fraction(25000))

    def test_unterminated_string_is_reported_once(self) -> None:
        bag = DiagnosticBag()
        tokenize('note "no closing quote\n', bag)
        self.assertEqual([d.code for d in bag.errors], ["SQZ0101"])

    def test_unexpected_character_is_located(self) -> None:
        bag = DiagnosticBag()
        tokenize("version 1\nratio ?\n", bag)
        self.assertEqual(len(bag.errors), 1)
        self.assertEqual((bag.errors[0].line, bag.errors[0].column), (2, 7))


class TestParser(unittest.TestCase):
    def test_block_with_settings(self) -> None:
        bag = DiagnosticBag()
        tree = parse("version 1\ncodec zlib6 {\n  kind lossless\n  ratio 6.0\n  cpu 2.5 ms per MiB\n}\n", bag)
        self.assertFalse(bag.has_errors())
        self.assertEqual(tree.version, 1)
        self.assertEqual(len(tree.blocks), 1)
        block = tree.blocks[0]
        self.assertEqual((block.keyword, block.name, block.kind), ("codec", "zlib6", None))
        self.assertEqual([s.key for s in block.settings], ["kind", "ratio", "cpu"])
        self.assertIsInstance(block.settings[1].atoms[0], NumberAtom)
        self.assertIsInstance(block.settings[2].atoms[0], QuantityAtom)
        self.assertEqual(block.settings[2].words(), ("per", "MiB"))

    def test_class_kind_annotation(self) -> None:
        bag = DiagnosticBag()
        tree = parse("version 1\nclass streams : timeseries {\n  record 24 bytes\n}\n", bag)
        self.assertEqual(tree.blocks[0].kind, "timeseries")

    def test_brackets_and_commas_are_cosmetic(self) -> None:
        bag = DiagnosticBag()
        with_brackets = parse("version 1\ntier hot {\n  codecs [a, b]\n}\n", bag)
        without = parse("version 1\ntier hot {\n  codecs a b\n}\n", bag)
        self.assertFalse(bag.has_errors())
        self.assertEqual(with_brackets.blocks[0].settings[0].words(), without.blocks[0].settings[0].words())

    def test_equals_sign_is_optional_sugar(self) -> None:
        bag = DiagnosticBag()
        tree = parse("version 1\nclass s : blob {\n  record = 24 bytes\n}\n", bag)
        self.assertFalse(bag.has_errors())
        self.assertEqual(tree.blocks[0].settings[0].key, "record")

    def test_recovery_keeps_reading_after_a_bad_line(self) -> None:
        bag = DiagnosticBag()
        tree = parse("version 1\n?\ncodec a {\n  kind lossless\n}\ncodec b {\n  kind lossy\n}\n", bag)
        self.assertTrue(bag.has_errors())
        # One bad line costs one diagnostic; both codecs still parse.
        self.assertEqual([b.name for b in tree.blocks], ["a", "b"])

    def test_missing_version_is_not_a_parse_error(self) -> None:
        bag = DiagnosticBag()
        tree = parse("codec a {\n  kind lossless\n}\n", bag)
        self.assertIsNone(tree.version)
        self.assertFalse(bag.has_errors())

    def test_unknown_file_scope_setting(self) -> None:
        bag = DiagnosticBag()
        parse("version 1\nvolume 3\n", bag)
        self.assertEqual([d.code for d in bag.errors], ["SQZ0202"])

    def test_operator_in_require_becomes_a_word_atom(self) -> None:
        bag = DiagnosticBag()
        tree = parse("version 1\npolicy p {\n  require fidelity >= 0.98\n}\n", bag)
        atoms = tree.blocks[0].settings[0].atoms
        self.assertIsInstance(atoms[1], WordAtom)
        self.assertEqual(atoms[1].text, ">=")


if __name__ == "__main__":
    unittest.main()
