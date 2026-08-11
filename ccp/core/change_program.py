"""The CCP change representation: a change program.

This is the heart of the Core. A unit is not stored as bytes; it is stored as a
*program* that reconstructs it from a base:

    COPY(src_offset, length)   take a run of bytes from the base   -- the Copy
    ADD(literal)               bytes that exist only in this unit  -- the Change

Executing that program against the base yields the unit -- the Paste.

Why a program and not an XOR list. The storage experiment represented a change
as `(position, xor_value)` pairs against a fixed-size region. That encoding has
two structural blind spots which are recorded as measured findings in
`experiments/ccp/FINDINGS.md`: it cannot see a duplicate that is shifted by even
one byte (positions no longer line up), and it costs ~3 bytes per changed byte,
so it collapses as soon as changes are dense. A COPY/ADD program has neither
property: a shifted run is still one COPY with a different source offset, and a
long changed span is one ADD rather than thousands of pairs.

The deeper reason is that a program is *inspectable*. An XOR blob can only be
applied. A change program can be read without being run -- which instructions
cover a byte range, whether two units carry identical programs, how much of a
unit is base and how much is genuinely new. Every execution capability in
`ccp.capabilities` is built on reading programs instead of materialising bytes.

Encoding is a byte stream of opcode + unsigned varints, so the cost of a change
is a real measured length, never a formula.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Sequence, Tuple, Union

# Opcodes. Kept to two: every edit is expressible as "reuse a run of the base" or
# "these bytes are new". A third opcode (e.g. a run-length fill) was considered
# and rejected -- it would be a compression feature, and the Core is not a
# compressor. ENGINEERING DECISION.
OP_ADD = 0x00
OP_COPY = 0x01

# A guard against a malformed or hostile container claiming an absurd length and
# making the decoder allocate. Nothing legitimate approaches it.
MAX_INSTRUCTION_LENGTH = 1 << 34


class CCPFormatError(ValueError):
    """A change program or container is malformed. Never silently tolerated."""


# ---------------------------------------------------------------------------
# instructions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Copy:
    """Reuse `length` bytes of the base starting at `src_offset`."""

    src_offset: int
    length: int

    def __post_init__(self) -> None:
        if self.src_offset < 0 or self.length <= 0:
            raise ValueError("Copy requires src_offset >= 0 and length > 0")


@dataclass(frozen=True)
class Add:
    """Bytes that are not taken from the base."""

    data: bytes

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("Add requires at least one byte")

    @property
    def length(self) -> int:
        return len(self.data)


Instruction = Union[Copy, Add]


# ---------------------------------------------------------------------------
# varints
# ---------------------------------------------------------------------------


def write_uvarint(out: bytearray, value: int) -> None:
    if value < 0:
        raise ValueError("uvarint cannot encode a negative value")
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return


def read_uvarint(data: bytes, pos: int) -> Tuple[int, int]:
    """Return (value, new_pos). Raises CCPFormatError on a truncated varint."""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise CCPFormatError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise CCPFormatError("varint exceeds 64 bits")


def uvarint_size(value: int) -> int:
    """Encoded width, so cost can be computed without building the bytes."""
    if value < 0:
        raise ValueError("uvarint cannot encode a negative value")
    size = 1
    while value >= 0x80:
        value >>= 7
        size += 1
    return size


# ---------------------------------------------------------------------------
# the program
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeProgram:
    """An ordered instruction sequence reconstructing one unit from one base.

    Immutable on purpose: a program is an identity. Two units with equal programs
    against the same base are equal units, and `ccp.capabilities` relies on that.
    """

    instructions: Tuple[Instruction, ...]

    def __iter__(self) -> Iterator[Instruction]:
        return iter(self.instructions)

    def __len__(self) -> int:
        return len(self.instructions)

    @property
    def output_length(self) -> int:
        """Size of the unit this program produces, without producing it."""
        return sum(
            ins.length if isinstance(ins, Copy) else len(ins.data)
            for ins in self.instructions
        )

    @property
    def copied_bytes(self) -> int:
        """Bytes taken from the base -- the reuse this unit gets for free."""
        return sum(ins.length for ins in self.instructions if isinstance(ins, Copy))

    @property
    def added_bytes(self) -> int:
        """Bytes unique to this unit."""
        return sum(len(ins.data) for ins in self.instructions if isinstance(ins, Add))

    @property
    def reuse_ratio(self) -> float | None:
        """Fraction of the unit that came from the base, or None if empty.

        None rather than 0.0: a zero-length unit has no meaningful reuse ratio,
        and a plausible fake would propagate into the build statistics.
        """
        total = self.output_length
        if total == 0:
            return None
        return self.copied_bytes / total

    def encoded_size(self) -> int:
        """Exact serialised length in bytes, computed without serialising."""
        size = uvarint_size(len(self.instructions))
        for ins in self.instructions:
            size += 1  # opcode
            if isinstance(ins, Copy):
                size += uvarint_size(ins.src_offset) + uvarint_size(ins.length)
            else:
                size += uvarint_size(len(ins.data)) + len(ins.data)
        return size

    def encode(self) -> bytes:
        out = bytearray()
        write_uvarint(out, len(self.instructions))
        for ins in self.instructions:
            if isinstance(ins, Copy):
                out.append(OP_COPY)
                write_uvarint(out, ins.src_offset)
                write_uvarint(out, ins.length)
            else:
                out.append(OP_ADD)
                write_uvarint(out, len(ins.data))
                out += ins.data
        encoded = bytes(out)
        # The cost model is used to decide whether a unit is worth storing as a
        # delta, so a drift between it and the real encoder would corrupt every
        # storage decision silently. Same discipline as the storage encoder,
        # which asserts its accounting against the filesystem.
        if len(encoded) != self.encoded_size():
            raise RuntimeError(
                f"change program cost model drift: encoded {len(encoded)} bytes "
                f"but model said {self.encoded_size()}"
            )
        return encoded

    @staticmethod
    def decode(data: bytes) -> "ChangeProgram":
        count, pos = read_uvarint(data, 0)
        if count > len(data):
            raise CCPFormatError("instruction count exceeds available bytes")
        instructions: List[Instruction] = []
        for _ in range(count):
            if pos >= len(data):
                raise CCPFormatError("truncated instruction stream")
            opcode = data[pos]
            pos += 1
            if opcode == OP_COPY:
                src_offset, pos = read_uvarint(data, pos)
                length, pos = read_uvarint(data, pos)
                if length == 0:
                    raise CCPFormatError("COPY of zero length")
                if length > MAX_INSTRUCTION_LENGTH:
                    raise CCPFormatError("COPY length exceeds the sanity bound")
                instructions.append(Copy(src_offset, length))
            elif opcode == OP_ADD:
                length, pos = read_uvarint(data, pos)
                if length == 0:
                    raise CCPFormatError("ADD of zero length")
                if length > MAX_INSTRUCTION_LENGTH:
                    raise CCPFormatError("ADD length exceeds the sanity bound")
                if pos + length > len(data):
                    raise CCPFormatError("ADD payload runs past the buffer")
                instructions.append(Add(bytes(data[pos : pos + length])))
                pos += length
            else:
                raise CCPFormatError(f"unknown opcode 0x{opcode:02x}")
        if pos != len(data):
            raise CCPFormatError("trailing bytes after change program")
        return ChangeProgram(tuple(instructions))


# ---------------------------------------------------------------------------
# Paste
# ---------------------------------------------------------------------------


def paste(base: bytes, program: ChangeProgram) -> bytes:
    """Execute a change program against a base. This is the Paste step.

    Every COPY is bounds-checked against the base. A program that reaches outside
    its base is a corrupt program, and corrupt input fails loudly rather than
    producing a plausible wrong unit.
    """
    out = bytearray()
    base_len = len(base)
    for ins in program:
        if isinstance(ins, Copy):
            end = ins.src_offset + ins.length
            if end > base_len:
                raise CCPFormatError(
                    f"COPY({ins.src_offset}, {ins.length}) runs past a "
                    f"{base_len}-byte base"
                )
            out += base[ins.src_offset : end]
        else:
            out += ins.data
    return bytes(out)


def instruction_spans(
    program: ChangeProgram,
) -> Iterator[Tuple[int, int, Instruction]]:
    """Yield (output_start, output_end, instruction) for each instruction.

    This is what makes partial execution possible: the output range an
    instruction is responsible for is known without running any of them.
    """
    cursor = 0
    for ins in program:
        length = ins.length if isinstance(ins, Copy) else len(ins.data)
        yield cursor, cursor + length, ins
        cursor += length


def merge_adjacent(instructions: Sequence[Instruction]) -> Tuple[Instruction, ...]:
    """Fuse instructions that are contiguous in both base and output.

    Two COPYs that are adjacent in the base become one COPY; consecutive ADDs
    become one ADD. This is not cosmetic -- each fused pair removes an opcode and
    a varint pair from the encoded cost, and shortens the walk that every
    capability performs.
    """
    merged: List[Instruction] = []
    for ins in instructions:
        if not merged:
            merged.append(ins)
            continue
        last = merged[-1]
        if (
            isinstance(last, Copy)
            and isinstance(ins, Copy)
            and last.src_offset + last.length == ins.src_offset
        ):
            merged[-1] = Copy(last.src_offset, last.length + ins.length)
        elif isinstance(last, Add) and isinstance(ins, Add):
            merged[-1] = Add(last.data + ins.data)
        else:
            merged.append(ins)
    return tuple(merged)
