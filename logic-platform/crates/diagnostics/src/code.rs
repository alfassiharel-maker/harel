//! The error code registry.
//!
//! Codes are stable: a code is never reused for a different meaning, and a code
//! that appears in a released version is never removed. The ranges are fixed by
//! `docs/01_LANGUAGE_CONSTITUTION.md` §7. Ranges reserved for subsystems that do
//! not exist yet (`E6xxx` data/SQL, `E7xxx` security) have no variants here —
//! an unallocated code is never emitted.

use core::fmt;

/// A stable, machine-readable error identity.
///
/// Every variant carries its code string and its human phrasing in one place,
/// so a code and its message cannot drift apart.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[non_exhaustive]
pub enum Code {
    // ---- E1xxx lexical -----------------------------------------------------
    /// A token that is not part of the language.
    UnknownToken,
    /// A numeric literal that cannot be represented, or is malformed.
    MalformedNumber,
    /// A string literal with no closing quote, or containing a raw newline.
    UnterminatedString,
    /// A character that may not appear outside a string or comment.
    UnexpectedCharacter,
    /// Whitespace inside a dotted name (`user . age`).
    SpaceInDottedName,
    /// An escape sequence the language does not define.
    InvalidEscape,
    /// A word reserved for a future language version.
    ReservedWord,

    // ---- E2xxx syntax ------------------------------------------------------
    /// A token appeared where the grammar does not allow it.
    UnexpectedToken,
    /// A construct was started and not finished before end of input.
    UnexpectedEndOfInput,
    /// An expression is nested more deeply than `MAX_EXPR_DEPTH`.
    ExpressionTooDeep,
    /// `rule r: when c` with no `then` clause.
    RuleWithoutThen,
    /// `a < b < c`, which the grammar deliberately does not accept.
    ChainedComparison,

    // ---- E3xxx semantic / type --------------------------------------------
    /// Two `fact` declarations of the same name.
    DuplicateFact,
    /// Two rules with the same name.
    DuplicateRule,
    /// A name that is read or output but that nothing can ever write.
    UnknownName,
    /// A `then` clause writing a name that a `fact` declaration already binds.
    DerivationShadowsFact,
    /// The same name listed twice in one `output`.
    DuplicateOutput,
    /// A `fact` whose value reads another name. A fact is given, not computed:
    /// deriving a value from another value is what a rule is for.
    NonConstantFact,
    /// Rules deriving one name with incompatible types.
    ConflictingNameType,
    /// A rule condition whose type is not `Bool`.
    NonBooleanCondition,
    /// An operator applied to operand types it is not defined for.
    OperatorTypeMismatch,
    /// A name whose type depends on itself.
    CyclicTypeDependency,

    // ---- E4xxx logic / inference ------------------------------------------
    /// Two rules derived different values for one name.
    ConflictingDerivation,

    // ---- E5xxx runtime -----------------------------------------------------
    /// Integer arithmetic left the `i64` range.
    IntegerOverflow,
    /// Division or remainder by zero.
    DivisionByZero,
    /// A float operation produced a non-finite value.
    NonFiniteFloat,
    /// An invariant the earlier stages were supposed to guarantee did not hold.
    /// Always an engine defect; reported rather than panicked (§8).
    InternalInvariant,

    // ---- E8xxx resource limits --------------------------------------------
    /// The inference loop exceeded `MAX_INFERENCE_ROUNDS`.
    InferenceRoundLimit,
    /// The source text exceeded `MAX_SOURCE_BYTES`.
    SourceTooLarge,
    /// The evaluation stack exceeded `MAX_EXPR_STACK`.
    ExpressionStackLimit,
    /// A chain of names whose types depend on one another exceeded
    /// `MAX_TYPE_DEPENDENCY_DEPTH`. Bounds recursion in type inference, which
    /// would otherwise overflow the stack on a long enough chain.
    TypeDependencyTooDeep,
}

impl Code {
    /// The stable code string, e.g. `"E3011"`.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::UnknownToken => "E1001",
            Self::MalformedNumber => "E1002",
            Self::UnterminatedString => "E1003",
            Self::UnexpectedCharacter => "E1004",
            Self::SpaceInDottedName => "E1005",
            Self::InvalidEscape => "E1006",
            Self::ReservedWord => "E1007",

            Self::UnexpectedToken => "E2001",
            Self::UnexpectedEndOfInput => "E2002",
            Self::ExpressionTooDeep => "E2003",
            Self::RuleWithoutThen => "E2004",
            Self::ChainedComparison => "E2005",

            Self::DuplicateFact => "E3001",
            Self::DuplicateRule => "E3002",
            Self::UnknownName => "E3003",
            Self::DerivationShadowsFact => "E3004",
            Self::DuplicateOutput => "E3005",
            Self::NonConstantFact => "E3006",
            Self::ConflictingNameType => "E3010",
            Self::NonBooleanCondition => "E3011",
            Self::OperatorTypeMismatch => "E3012",
            Self::CyclicTypeDependency => "E3013",

            Self::ConflictingDerivation => "E4001",

            Self::IntegerOverflow => "E5001",
            Self::DivisionByZero => "E5002",
            Self::NonFiniteFloat => "E5003",
            Self::InternalInvariant => "E5099",

            Self::InferenceRoundLimit => "E8001",
            Self::SourceTooLarge => "E8002",
            Self::ExpressionStackLimit => "E8003",
            Self::TypeDependencyTooDeep => "E8004",
        }
    }

    /// The compilation or execution phase the code belongs to.
    #[must_use]
    pub const fn phase(self) -> Phase {
        // Derived from the code string's first digit so that the range table in
        // the constitution and this function cannot disagree.
        match self.as_str().as_bytes()[1] {
            b'1' => Phase::Lexical,
            b'2' => Phase::Syntax,
            b'3' => Phase::Semantic,
            b'4' => Phase::Logic,
            b'5' => Phase::Runtime,
            b'8' => Phase::Resource,
            _ => Phase::Runtime,
        }
    }

    /// Every code, for the registry test that checks the codes are unique.
    #[must_use]
    pub const fn all() -> &'static [Self] {
        &[
            Self::UnknownToken,
            Self::MalformedNumber,
            Self::UnterminatedString,
            Self::UnexpectedCharacter,
            Self::SpaceInDottedName,
            Self::InvalidEscape,
            Self::ReservedWord,
            Self::UnexpectedToken,
            Self::UnexpectedEndOfInput,
            Self::ExpressionTooDeep,
            Self::RuleWithoutThen,
            Self::ChainedComparison,
            Self::DuplicateFact,
            Self::DuplicateRule,
            Self::UnknownName,
            Self::DerivationShadowsFact,
            Self::DuplicateOutput,
            Self::NonConstantFact,
            Self::ConflictingNameType,
            Self::NonBooleanCondition,
            Self::OperatorTypeMismatch,
            Self::CyclicTypeDependency,
            Self::ConflictingDerivation,
            Self::IntegerOverflow,
            Self::DivisionByZero,
            Self::NonFiniteFloat,
            Self::InternalInvariant,
            Self::InferenceRoundLimit,
            Self::SourceTooLarge,
            Self::ExpressionStackLimit,
            Self::TypeDependencyTooDeep,
        ]
    }
}

impl fmt::Display for Code {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The stage of the pipeline an error belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    /// Text to tokens.
    Lexical,
    /// Tokens to syntax tree.
    Syntax,
    /// Names and types.
    Semantic,
    /// Inference.
    Logic,
    /// Execution.
    Runtime,
    /// A configured limit was exceeded.
    Resource,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    #[test]
    fn codes_are_unique() {
        let codes: BTreeSet<&str> = Code::all().iter().map(|c| c.as_str()).collect();
        assert_eq!(
            codes.len(),
            Code::all().len(),
            "an error code is used twice"
        );
    }

    #[test]
    fn codes_are_well_formed() {
        for code in Code::all() {
            let text = code.as_str();
            assert_eq!(text.len(), 5, "{text} is not E + 4 digits");
            assert!(text.starts_with('E'), "{text} does not start with E");
            assert!(
                text[1..].chars().all(|c| c.is_ascii_digit()),
                "{text} is not numeric"
            );
        }
    }

    #[test]
    fn phase_follows_the_range() {
        assert_eq!(Code::MalformedNumber.phase(), Phase::Lexical);
        assert_eq!(Code::NonBooleanCondition.phase(), Phase::Semantic);
        assert_eq!(Code::ConflictingDerivation.phase(), Phase::Logic);
        assert_eq!(Code::InferenceRoundLimit.phase(), Phase::Resource);
    }

    #[test]
    fn reserved_ranges_are_unallocated() {
        // E6xxx (data/SQL) and E7xxx (security) belong to subsystems that do not
        // exist. If one appears here, a stub has been added somewhere.
        for code in Code::all() {
            let first = code.as_str().as_bytes()[1];
            assert!(
                first != b'6' && first != b'7',
                "{code} allocates a reserved range"
            );
        }
    }
}
