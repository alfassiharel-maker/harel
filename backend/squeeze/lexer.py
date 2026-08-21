"""Tokeniser for `.sqz` sources.

The grammar is newline-sensitive: one setting per line inside a block. That is a
deliberate choice over a punctuation-terminated grammar — these files are edited
in review comments and pasted into tickets, and a missing semicolon three lines
up is a bad error message for a policy author who is a backend engineer, not a
compiler author. A `;` is accepted as an explicit separator so that a short
declaration can still be written on one line; it produces the same token a
newline does.

Numbers are read as exact `Fraction`s straight from their decimal text, so
`1.2 KiB` is 1228.8 bytes exactly and never 1228.7999999999999.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

from .diagnostics import DiagnosticBag

__all__ = ["Token", "TokenKind", "tokenize"]


class TokenKind(str, Enum):
    WORD = "word"
    NUMBER = "number"
    STRING = "string"
    LBRACE = "{"
    RBRACE = "}"
    COMMA = ","
    COLON = ":"
    EQUAL = "="
    LBRACKET = "["
    RBRACKET = "]"
    OP = "op"
    NEWLINE = "newline"
    EOF = "eof"


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str
    line: int
    column: int
    #: Populated for NUMBER only.
    value: Fraction | None = None


_SINGLES: dict[str, TokenKind] = {
    "{": TokenKind.LBRACE,
    "}": TokenKind.RBRACE,
    ",": TokenKind.COMMA,
    ":": TokenKind.COLON,
    "=": TokenKind.EQUAL,
    "[": TokenKind.LBRACKET,
    "]": TokenKind.RBRACKET,
}

# Comparison operators used by `require` constraints. `=` alone is assignment,
# so equality in a constraint is written `==`.
_OPERATORS = ("<=", ">=", "==", "!=", "<", ">")

_WORD_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_WORD_BODY = _WORD_START | set("0123456789.-")


def tokenize(source: str, diagnostics: DiagnosticBag) -> list[Token]:
    tokens: list[Token] = []
    line = 1
    column = 1
    index = 0
    length = len(source)

    while index < length:
        char = source[index]

        if char == "\n":
            tokens.append(Token(TokenKind.NEWLINE, "\\n", line, column))
            index += 1
            line += 1
            column = 1
            continue

        if char == ";":
            tokens.append(Token(TokenKind.NEWLINE, ";", line, column))
            index += 1
            column += 1
            continue

        if char in " \t\r":
            index += 1
            column += 1
            continue

        if char == "#":
            while index < length and source[index] != "\n":
                index += 1
            continue

        two = source[index : index + 2]
        if two in _OPERATORS:
            tokens.append(Token(TokenKind.OP, two, line, column))
            index += 2
            column += 2
            continue

        if char in _SINGLES:
            tokens.append(Token(_SINGLES[char], char, line, column))
            index += 1
            column += 1
            continue

        if char in "<>":
            tokens.append(Token(TokenKind.OP, char, line, column))
            index += 1
            column += 1
            continue

        if char == '"':
            start_line, start_column = line, column
            index += 1
            column += 1
            chars: list[str] = []
            closed = False
            while index < length:
                current = source[index]
                if current == "\n":
                    break
                if current == '"':
                    closed = True
                    index += 1
                    column += 1
                    break
                chars.append(current)
                index += 1
                column += 1
            if not closed:
                diagnostics.error(
                    "SQZ0101",
                    "unterminated string literal",
                    start_line,
                    start_column,
                    hint="strings may not span lines",
                )
            tokens.append(Token(TokenKind.STRING, "".join(chars), start_line, start_column))
            continue

        if char.isdigit() or (char == "-" and index + 1 < length and source[index + 1].isdigit()):
            start_line, start_column = line, column
            start = index
            if char == "-":
                index += 1
                column += 1
            seen_dot = False
            while index < length:
                current = source[index]
                if current.isdigit():
                    index += 1
                    column += 1
                elif current == "." and not seen_dot and index + 1 < length and source[index + 1].isdigit():
                    seen_dot = True
                    index += 1
                    column += 1
                elif current == "_":
                    index += 1
                    column += 1
                else:
                    break
            text = source[start:index].replace("_", "")
            tokens.append(Token(TokenKind.NUMBER, text, start_line, start_column, value=Fraction(text)))
            continue

        if char in _WORD_START:
            start_line, start_column = line, column
            start = index
            while index < length and source[index] in _WORD_BODY:
                index += 1
                column += 1
            tokens.append(Token(TokenKind.WORD, source[start:index], start_line, start_column))
            continue

        diagnostics.error(
            "SQZ0102",
            f"unexpected character {char!r}",
            line,
            column,
            hint="settings are `key value` pairs; see docs/23-squeeze-language.md",
        )
        index += 1
        column += 1

    tokens.append(Token(TokenKind.EOF, "", line, column))
    return tokens
