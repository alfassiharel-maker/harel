//! The scanner: source text to a token stream.
//!
//! Errors do not stop scanning. The scanner records the diagnostic, skips the
//! offending character and continues, so one run reports every lexical problem
//! in the file rather than the first.

use crate::token::{keyword, Token, TokenKind, RESERVED_WORDS};
use lml_diagnostics::{Code, Diagnostic, Diagnostics, Span};

/// Largest source text the lexer will accept.
///
/// Chosen to bound memory on untrusted input: the scanner materialises one
/// `(offset, char)` pair per character, so the peak cost is proportional to
/// this. 16 MiB is far above any hand-written program and far below a size that
/// could exhaust a developer machine.
pub const MAX_SOURCE_BYTES: usize = 16 * 1024 * 1024;

/// Turn `source` into tokens.
///
/// The returned stream always ends with exactly one [`TokenKind::Eof`].
///
/// # Errors
/// Returns every lexical diagnostic found, in source order.
pub fn tokenize(source: &str) -> Result<Vec<Token>, Diagnostics> {
    Scanner::new(source).run()
}

struct Scanner<'a> {
    source: &'a str,
    /// `(byte offset, character)` for every character, so that spans are byte
    /// ranges while lookahead is by character.
    chars: Vec<(usize, char)>,
    pos: usize,
    tokens: Vec<Token>,
    diagnostics: Diagnostics,
}

impl<'a> Scanner<'a> {
    fn new(source: &'a str) -> Self {
        Self {
            source,
            chars: source.char_indices().collect(),
            pos: 0,
            tokens: Vec::new(),
            diagnostics: Diagnostics::new(),
        }
    }

    fn run(mut self) -> Result<Vec<Token>, Diagnostics> {
        if self.source.len() > MAX_SOURCE_BYTES {
            let diagnostic = Diagnostic::new(
                Code::SourceTooLarge,
                Span::point(0),
                format!(
                    "source is {} bytes, the limit is {MAX_SOURCE_BYTES}",
                    self.source.len()
                ),
            );
            return Err(diagnostic.into());
        }

        while let Some(ch) = self.peek(0) {
            match ch {
                ' ' | '\t' | '\n' | '\r' => self.whitespace(),
                '#' => self.comment(),
                '"' => self.string(),
                c if c.is_ascii_digit() => self.number(),
                c if is_ident_start(c) => self.name(),
                _ => self.operator(),
            }
        }

        let end = Span::point(self.source.len());
        self.tokens.push(Token::new(TokenKind::Eof, end));
        self.diagnostics.into_result(self.tokens)
    }

    // ---- character access --------------------------------------------------

    fn peek(&self, ahead: usize) -> Option<char> {
        self.chars.get(self.pos + ahead).map(|&(_, ch)| ch)
    }

    /// Byte offset of the character `ahead` positions from the cursor, or the
    /// end of the source if there is none.
    fn offset(&self, ahead: usize) -> usize {
        self.chars
            .get(self.pos + ahead)
            .map_or(self.source.len(), |&(offset, _)| offset)
    }

    fn advance(&mut self) {
        self.pos += 1;
    }

    fn emit(&mut self, kind: TokenKind, start: usize) {
        let span = Span::new(start, self.offset(0));
        self.tokens.push(Token::new(kind, span));
    }

    fn error(&mut self, diagnostic: Diagnostic) {
        self.diagnostics.push(diagnostic);
    }

    // ---- scanners ----------------------------------------------------------

    fn whitespace(&mut self) {
        while matches!(self.peek(0), Some(' ' | '\t' | '\n')) {
            self.advance();
        }
        if self.peek(0) == Some('\r') {
            // `\r\n` is a line terminator; a lone `\r` is not, and silently
            // accepting it would make column numbers wrong for the rest of the
            // line.
            if self.peek(1) == Some('\n') {
                self.advance();
                self.advance();
            } else {
                let start = self.offset(0);
                self.advance();
                self.error(
                    Diagnostic::new(
                        Code::UnexpectedCharacter,
                        Span::new(start, self.offset(0)),
                        "carriage return that is not part of a line ending",
                    )
                    .with_help("use `\\n` or `\\r\\n` line endings"),
                );
            }
        }
    }

    fn comment(&mut self) {
        while let Some(ch) = self.peek(0) {
            if ch == '\n' {
                return;
            }
            self.advance();
        }
    }

    fn string(&mut self) {
        let start = self.offset(0);
        self.advance(); // opening quote
        let mut value = String::new();
        loop {
            match self.peek(0) {
                None | Some('\n') => {
                    self.error(
                        Diagnostic::new(
                            Code::UnterminatedString,
                            Span::new(start, self.offset(0)),
                            "string literal is not closed",
                        )
                        .with_help("a string literal may not span lines; add a closing `\"`"),
                    );
                    return;
                }
                Some('"') => {
                    self.advance();
                    self.emit(TokenKind::Str(value), start);
                    return;
                }
                Some('\\') => {
                    let escape_start = self.offset(0);
                    self.advance();
                    match self.peek(0) {
                        Some('"') => value.push('"'),
                        Some('\\') => value.push('\\'),
                        Some('n') => value.push('\n'),
                        Some('t') => value.push('\t'),
                        Some('r') => value.push('\r'),
                        Some(other) => {
                            self.advance();
                            self.error(
                                Diagnostic::new(
                                    Code::InvalidEscape,
                                    Span::new(escape_start, self.offset(0)),
                                    format!("`\\{other}` is not an escape sequence"),
                                )
                                .with_help(r#"valid escapes are \" \\ \n \t \r"#),
                            );
                            continue;
                        }
                        None => continue, // handled as unterminated on the next turn
                    }
                    self.advance();
                }
                Some(ch) => {
                    value.push(ch);
                    self.advance();
                }
            }
        }
    }

    fn number(&mut self) {
        let start = self.offset(0);
        let Some(integer_part) = self.digits(start) else {
            return;
        };

        let is_float =
            self.peek(0) == Some('.') && self.peek(1).is_some_and(|c| c.is_ascii_digit());
        if !is_float {
            // A trailing `.` with no digits after it: `1.` is not a float and
            // not an integer followed by anything meaningful.
            if self.peek(0) == Some('.') {
                self.advance();
                self.error(Diagnostic::new(
                    Code::MalformedNumber,
                    Span::new(start, self.offset(0)),
                    "a float literal needs at least one digit after the point",
                ));
                return;
            }
            match integer_part.parse::<i64>() {
                Ok(value) => self.emit(TokenKind::Int(value), start),
                Err(_) => {
                    let span = Span::new(start, self.offset(0));
                    self.error(
                        Diagnostic::new(
                            Code::MalformedNumber,
                            span,
                            "integer literal is malformed or outside the range of `Int`",
                        )
                        .with_help("`Int` holds -9223372036854775808 to 9223372036854775807"),
                    );
                }
            }
            return;
        }

        self.advance(); // the point
        let Some(fraction) = self.digits(start) else {
            return;
        }; // already reported
        let text = format!("{integer_part}.{fraction}");
        match text.parse::<f64>() {
            Ok(value) if value.is_finite() => self.emit(TokenKind::Float(value), start),
            _ => {
                let span = Span::new(start, self.offset(0));
                self.error(
                    Diagnostic::new(
                        Code::MalformedNumber,
                        span,
                        "float literal is malformed or not finite",
                    )
                    .with_help("`Float` holds finite IEEE-754 binary64 values only"),
                );
            }
        }
    }

    /// Consume a run of digits with `_` separators, returning the digits alone.
    ///
    /// `None` means a diagnostic was reported and the caller should stop.
    fn digits(&mut self, literal_start: usize) -> Option<String> {
        let mut text = String::new();
        let mut previous_was_digit = false;
        loop {
            match self.peek(0) {
                Some(ch) if ch.is_ascii_digit() => {
                    text.push(ch);
                    previous_was_digit = true;
                    self.advance();
                }
                Some('_') => {
                    self.advance();
                    let followed_by_digit = self.peek(0).is_some_and(|c| c.is_ascii_digit());
                    if !previous_was_digit || !followed_by_digit {
                        self.error(
                            Diagnostic::new(
                                Code::MalformedNumber,
                                Span::new(literal_start, self.offset(0)),
                                "`_` in a numeric literal must sit between two digits",
                            )
                            .with_help("write `1_000`, not `1_` or `1__000`"),
                        );
                        return None;
                    }
                    previous_was_digit = false;
                }
                _ => return Some(text),
            }
        }
    }

    fn name(&mut self) {
        let start = self.offset(0);
        let mut segments: Vec<String> = Vec::new();
        loop {
            let mut segment = String::new();
            while let Some(ch) = self.peek(0) {
                if is_ident_part(ch) {
                    segment.push(ch);
                    self.advance();
                } else {
                    break;
                }
            }
            segments.push(segment);

            let continues = self.peek(0) == Some('.') && self.peek(1).is_some_and(is_ident_start);
            if !continues {
                break;
            }
            self.advance(); // the dot
        }

        let span = Span::new(start, self.offset(0));

        if segments.len() == 1 {
            if let Some(kind) = keyword(&segments[0]) {
                self.tokens.push(Token::new(kind, span));
                return;
            }
        }

        for segment in &segments {
            if RESERVED_WORDS.contains(&segment.as_str()) {
                self.error(
                    Diagnostic::new(
                        Code::ReservedWord,
                        span,
                        format!("`{segment}` is reserved for a future version of the language"),
                    )
                    .with_help("choose a different name"),
                );
                return;
            }
            if keyword(segment).is_some() {
                self.error(
                    Diagnostic::new(
                        Code::ReservedWord,
                        span,
                        format!("`{segment}` is a keyword and cannot be part of a name"),
                    )
                    .with_help("choose a different name"),
                );
                return;
            }
        }

        self.tokens
            .push(Token::new(TokenKind::Ident(segments.join(".")), span));
    }

    fn operator(&mut self) {
        let start = self.offset(0);
        // Maximal munch: two-character operators are matched before their
        // one-character prefixes.
        let two = match (self.peek(0), self.peek(1)) {
            (Some('='), Some('=')) => Some(TokenKind::Eq),
            (Some('!'), Some('=')) => Some(TokenKind::Ne),
            (Some('<'), Some('=')) => Some(TokenKind::Le),
            (Some('>'), Some('=')) => Some(TokenKind::Ge),
            _ => None,
        };
        if let Some(kind) = two {
            self.advance();
            self.advance();
            self.emit(kind, start);
            return;
        }

        let Some(ch) = self.peek(0) else { return };
        let one = match ch {
            '=' => Some(TokenKind::Assign),
            '<' => Some(TokenKind::Lt),
            '>' => Some(TokenKind::Gt),
            '+' => Some(TokenKind::Plus),
            '-' => Some(TokenKind::Minus),
            '*' => Some(TokenKind::Star),
            '/' => Some(TokenKind::Slash),
            '%' => Some(TokenKind::Percent),
            '(' => Some(TokenKind::LParen),
            ')' => Some(TokenKind::RParen),
            ':' => Some(TokenKind::Colon),
            ',' => Some(TokenKind::Comma),
            _ => None,
        };
        if let Some(kind) = one {
            self.advance();
            self.emit(kind, start);
            return;
        }

        self.advance();
        let span = Span::new(start, self.offset(0));
        if ch == '.' {
            self.error(
                Diagnostic::new(
                    Code::SpaceInDottedName,
                    span,
                    "`.` may only join the segments of a name, with no spaces",
                )
                .with_help("write `user.age`, not `user . age`"),
            );
        } else if ch.is_ascii() {
            self.error(Diagnostic::new(
                Code::UnknownToken,
                span,
                format!("`{ch}` is not part of the language"),
            ));
        } else {
            self.error(
                Diagnostic::new(
                    Code::UnexpectedCharacter,
                    span,
                    format!("`{ch}` may only appear inside a string literal or a comment"),
                )
                .with_help("names and operators are ASCII"),
            );
        }
    }
}

fn is_ident_start(ch: char) -> bool {
    ch.is_ascii_alphabetic() || ch == '_'
}

fn is_ident_part(ch: char) -> bool {
    is_ident_start(ch) || ch.is_ascii_digit()
}
