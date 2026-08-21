"""Recursive-descent parser for `.sqz` sources.

Grammar (EBNF; NEWLINE is significant, blank lines and comments are skipped):

    file        := (preamble_setting | block)*
    block       := WORD WORD [':' WORD] '{' NEWLINE? (setting NEWLINE?)* '}'
    setting     := WORD ['='] atom* ('=' is optional sugar and ignored)
    atom        := NUMBER [WORD_unit] | WORD | STRING | '[' atom (',' atom)* ']'
                 | OP                     -- comparison inside `require`

Error recovery: on a bad setting the parser skips to the end of the line; on a
bad block header it skips to the closing brace. One malformed line therefore
costs one diagnostic instead of burying the rest of the file, which matters
because these files are reviewed in one pass.
"""

from __future__ import annotations

from .diagnostics import DiagnosticBag
from .lexer import Token, TokenKind, tokenize
from .nodes import Atom, Block, NumberAtom, QuantityAtom, Setting, SourceFile, StringAtom, WordAtom
from .units import UNITS, Quantity

__all__ = ["BLOCK_KEYWORDS", "parse", "parse_tokens"]

BLOCK_KEYWORDS = frozenset({"codec", "tier", "class", "policy"})
PREAMBLE_KEYS = frozenset({"version", "currency", "policy_name"})


def parse(source: str, diagnostics: DiagnosticBag) -> SourceFile:
    return parse_tokens(tokenize(source, diagnostics), diagnostics)


def parse_tokens(tokens: list[Token], diagnostics: DiagnosticBag) -> SourceFile:
    return _Parser(tokens, diagnostics).parse_file()


class _Parser:
    def __init__(self, tokens: list[Token], diagnostics: DiagnosticBag) -> None:
        self._tokens = tokens
        self._pos = 0
        self._diagnostics = diagnostics

    # -- token helpers ----------------------------------------------------
    def _peek(self, offset: int = 0) -> Token:
        index = min(self._pos + offset, len(self._tokens) - 1)
        return self._tokens[index]

    def _advance(self) -> Token:
        token = self._peek()
        if token.kind is not TokenKind.EOF:
            self._pos += 1
        return token

    def _skip_newlines(self) -> None:
        while self._peek().kind is TokenKind.NEWLINE:
            self._pos += 1

    def _skip_line(self) -> None:
        while self._peek().kind not in (TokenKind.NEWLINE, TokenKind.EOF, TokenKind.RBRACE):
            self._pos += 1

    # -- productions ------------------------------------------------------
    def parse_file(self) -> SourceFile:
        blocks: list[Block] = []
        preamble: list[Setting] = []
        version: int | None = None

        while True:
            self._skip_newlines()
            token = self._peek()
            if token.kind is TokenKind.EOF:
                break
            if token.kind is not TokenKind.WORD:
                self._diagnostics.error(
                    "SQZ0201",
                    f"expected a declaration, found {token.text!r}",
                    token.line,
                    token.column,
                    hint="a file contains `version`, `currency` and codec/tier/class/policy blocks",
                )
                self._skip_line()
                continue

            if token.text in BLOCK_KEYWORDS:
                block = self._parse_block()
                if block is not None:
                    blocks.append(block)
                continue

            setting = self._parse_setting()
            if setting is None:
                continue
            if setting.key not in PREAMBLE_KEYS:
                self._diagnostics.error(
                    "SQZ0202",
                    f"unknown file-scope setting {setting.key!r}",
                    setting.line,
                    setting.column,
                    hint=f"file scope accepts {', '.join(sorted(PREAMBLE_KEYS))}",
                )
                continue
            if setting.key == "version":
                version = self._read_version(setting, fallback=version)
            preamble.append(setting)

        return SourceFile(version=version, blocks=tuple(blocks), preamble=tuple(preamble))

    def _read_version(self, setting: Setting, fallback: int | None) -> int | None:
        first = setting.atoms[0] if setting.atoms else None
        if isinstance(first, NumberAtom) and first.value.denominator == 1:
            return first.value.numerator
        self._diagnostics.error(
            "SQZ0203",
            "`version` takes a whole number",
            setting.line,
            setting.column,
            hint="write `version 1`",
        )
        return fallback

    def _parse_block(self) -> Block | None:
        keyword_token = self._advance()
        name_token = self._peek()
        if name_token.kind is not TokenKind.WORD:
            self._diagnostics.error(
                "SQZ0204",
                f"`{keyword_token.text}` must be followed by a name",
                name_token.line,
                name_token.column,
            )
            self._recover_to_block_end()
            return None
        self._advance()

        kind: str | None = None
        if self._peek().kind is TokenKind.COLON:
            self._advance()
            kind_token = self._peek()
            if kind_token.kind is not TokenKind.WORD:
                self._diagnostics.error(
                    "SQZ0205",
                    "expected a kind after `:`",
                    kind_token.line,
                    kind_token.column,
                    hint="for example `class training_samples : timeseries {`",
                )
            else:
                kind = kind_token.text
                self._advance()

        if self._peek().kind is not TokenKind.LBRACE:
            brace_token = self._peek()
            self._diagnostics.error(
                "SQZ0206",
                f"expected `{{` to open the {keyword_token.text} body",
                brace_token.line,
                brace_token.column,
            )
            self._recover_to_block_end()
            return None
        self._advance()

        settings: list[Setting] = []
        while True:
            self._skip_newlines()
            token = self._peek()
            if token.kind is TokenKind.RBRACE:
                self._advance()
                break
            if token.kind is TokenKind.EOF:
                self._diagnostics.error(
                    "SQZ0207",
                    f"unclosed {keyword_token.text} block {name_token.text!r}",
                    keyword_token.line,
                    keyword_token.column,
                )
                break
            setting = self._parse_setting()
            if setting is not None:
                settings.append(setting)

        return Block(
            keyword=keyword_token.text,
            name=name_token.text,
            kind=kind,
            settings=tuple(settings),
            line=keyword_token.line,
            column=keyword_token.column,
        )

    def _recover_to_block_end(self) -> None:
        depth = 0
        while True:
            token = self._peek()
            if token.kind is TokenKind.EOF:
                return
            if token.kind is TokenKind.LBRACE:
                depth += 1
            elif token.kind is TokenKind.RBRACE:
                self._advance()
                if depth <= 1:
                    return
                depth -= 1
                continue
            elif token.kind is TokenKind.NEWLINE and depth == 0:
                self._advance()
                return
            self._advance()

    def _parse_setting(self) -> Setting | None:
        key_token = self._advance()
        if key_token.kind is not TokenKind.WORD:
            self._diagnostics.error(
                "SQZ0208",
                f"expected a setting name, found {key_token.text!r}",
                key_token.line,
                key_token.column,
            )
            self._skip_line()
            return None

        if self._peek().kind is TokenKind.EQUAL:
            self._advance()

        atoms: list[Atom] = []
        while True:
            token = self._peek()
            if token.kind in (TokenKind.NEWLINE, TokenKind.EOF, TokenKind.RBRACE):
                break
            if token.kind is TokenKind.COMMA:
                self._advance()
                continue
            if token.kind in (TokenKind.LBRACKET, TokenKind.RBRACKET):
                # Brackets are cosmetic: `codecs [a, b]` and `codecs a, b` are
                # the same list. Authors write both; rejecting either would be
                # pedantry with no diagnostic value.
                self._advance()
                continue
            atom = self._parse_atom()
            if atom is None:
                self._skip_line()
                break
            atoms.append(atom)

        return Setting(key=key_token.text, atoms=tuple(atoms), line=key_token.line, column=key_token.column)

    def _parse_atom(self) -> Atom | None:
        token = self._advance()
        if token.kind is TokenKind.STRING:
            return StringAtom(line=token.line, column=token.column, text=token.text)
        if token.kind is TokenKind.OP:
            return WordAtom(line=token.line, column=token.column, text=token.text)
        if token.kind is TokenKind.NUMBER:
            if token.value is None:  # unreachable: the lexer always sets it for NUMBER
                return None
            following = self._peek()
            if following.kind is TokenKind.WORD and following.text in UNITS:
                self._advance()
                return QuantityAtom(
                    line=token.line,
                    column=token.column,
                    quantity=Quantity.parse(token.value, following.text),
                )
            return NumberAtom(line=token.line, column=token.column, value=token.value)
        if token.kind is TokenKind.WORD:
            return WordAtom(line=token.line, column=token.column, text=token.text)
        self._diagnostics.error(
            "SQZ0209",
            f"unexpected {token.text!r} in a setting value",
            token.line,
            token.column,
        )
        return None
