//! Expression code: a post-order operation sequence for a stack evaluator.
//!
//! Post-order rather than a tree because it serialises trivially, has one
//! obvious execution rule, and makes the stack depth statically computable —
//! which is how the evaluator can run without a bounds check per operation
//! (`docs/03_EXECUTION_MODEL.md` §3).
//!
//! The operations are defined here rather than reused from the syntax tree: the
//! IR is the contract with the runtime and must survive a change of syntax.

use crate::name::NameId;
use lml_types::Value;

/// Deepest the evaluation stack may go.
///
/// Checked once, when an [`ExprCode`] is built, so the evaluator itself needs
/// no per-operation check. The parser already bounds expression *nesting* to
/// 128; this bounds the *stack*, which is the quantity the evaluator actually
/// allocates.
pub const MAX_EXPR_STACK: usize = 256;

/// One operation.
#[derive(Debug, Clone, PartialEq)]
pub enum Op {
    /// Push a constant.
    Const(Value),
    /// Push the value bound to a name. Fails as *not evaluable* if the name is
    /// unknown — which is not the same as false (`docs/02_FORMAL_SEMANTICS.md` §4.1).
    Load(NameId),
    /// Arithmetic negation.
    Neg,
    /// Logical negation.
    Not,
    /// Addition, or string concatenation.
    Add,
    /// Subtraction.
    Sub,
    /// Multiplication.
    Mul,
    /// Division.
    Div,
    /// Remainder.
    Rem,
    /// Equality.
    Eq,
    /// Inequality.
    Ne,
    /// Less than.
    Lt,
    /// Less than or equal.
    Le,
    /// Greater than.
    Gt,
    /// Greater than or equal.
    Ge,
    /// Conjunction. Both operands are already evaluated: `and` does not
    /// short-circuit, so that *not evaluable* propagates regardless of the
    /// order the author wrote the operands in.
    And,
    /// Disjunction, with the same property.
    Or,
}

impl Op {
    /// How many values the operation pops.
    #[must_use]
    pub const fn arity(&self) -> usize {
        match self {
            Self::Const(_) | Self::Load(_) => 0,
            Self::Neg | Self::Not => 1,
            Self::Add
            | Self::Sub
            | Self::Mul
            | Self::Div
            | Self::Rem
            | Self::Eq
            | Self::Ne
            | Self::Lt
            | Self::Le
            | Self::Gt
            | Self::Ge
            | Self::And
            | Self::Or => 2,
        }
    }

    /// The mnemonic used in the canonical IR text.
    #[must_use]
    pub const fn mnemonic(&self) -> &'static str {
        match self {
            Self::Const(_) => "const",
            Self::Load(_) => "load",
            Self::Neg => "neg",
            Self::Not => "not",
            Self::Add => "add",
            Self::Sub => "sub",
            Self::Mul => "mul",
            Self::Div => "div",
            Self::Rem => "rem",
            Self::Eq => "eq",
            Self::Ne => "ne",
            Self::Lt => "lt",
            Self::Le => "le",
            Self::Gt => "gt",
            Self::Ge => "ge",
            Self::And => "and",
            Self::Or => "or",
        }
    }
}

/// Why a sequence of operations is not a valid expression.
///
/// Both cases mean the lowering pass has a defect: the parser cannot produce an
/// expression that lowers to unbalanced code. They are values rather than
/// panics because a compiler that aborts tells the user nothing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CodeError {
    /// The operations do not leave exactly one value on the stack.
    Unbalanced,
    /// Evaluating would need more stack than [`MAX_EXPR_STACK`].
    StackTooDeep,
}

/// A validated, executable expression.
#[derive(Debug, Clone, PartialEq)]
pub struct ExprCode {
    ops: Vec<Op>,
    stack_depth: usize,
}

impl ExprCode {
    /// Validate a post-order operation sequence.
    ///
    /// # Errors
    /// [`CodeError::Unbalanced`] if the sequence does not evaluate to exactly
    /// one value; [`CodeError::StackTooDeep`] if it needs too much stack.
    pub fn new(ops: Vec<Op>) -> Result<Self, CodeError> {
        let mut depth: usize = 0;
        let mut peak: usize = 0;
        for op in &ops {
            let arity = op.arity();
            if depth < arity {
                return Err(CodeError::Unbalanced);
            }
            depth = depth - arity + 1;
            peak = peak.max(depth);
            if peak > MAX_EXPR_STACK {
                return Err(CodeError::StackTooDeep);
            }
        }
        if depth != 1 {
            return Err(CodeError::Unbalanced);
        }
        Ok(Self {
            ops,
            stack_depth: peak,
        })
    }

    /// The operations, in evaluation order.
    #[must_use]
    pub fn ops(&self) -> &[Op] {
        &self.ops
    }

    /// The stack size evaluation needs, known before it runs.
    #[must_use]
    pub const fn stack_depth(&self) -> usize {
        self.stack_depth
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn int(value: i64) -> Op {
        Op::Const(Value::Int(value))
    }

    #[test]
    fn stack_depth_is_computed_up_front() {
        // 1 + 2  →  const 1, const 2, add
        let code = ExprCode::new(vec![int(1), int(2), Op::Add]).expect("balanced");
        assert_eq!(code.stack_depth(), 2);
    }

    #[test]
    fn unbalanced_code_is_rejected() {
        assert_eq!(ExprCode::new(vec![Op::Add]), Err(CodeError::Unbalanced));
        assert_eq!(
            ExprCode::new(vec![int(1), int(2)]),
            Err(CodeError::Unbalanced)
        );
        assert_eq!(ExprCode::new(vec![]), Err(CodeError::Unbalanced));
    }

    #[test]
    fn deep_code_is_rejected_before_it_runs() {
        let mut ops = vec![int(0); MAX_EXPR_STACK + 1];
        ops.extend(std::iter::repeat(Op::Add).take(MAX_EXPR_STACK));
        assert_eq!(ExprCode::new(ops), Err(CodeError::StackTooDeep));
    }
}
