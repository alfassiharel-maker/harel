"""
    CCPStrategy

The CCP strategy engine: given measurements of a block, decide how to represent
it.

This module is where the product's central rule lives — *never assume DELTA is
better* — so it is written as an explicit cost comparison between every valid
candidate, not as a threshold test. If a delta representation wins it is because
its cost was computed and found lower; if it loses, FULL is chosen and the reason
says so.

The module deliberately performs no I/O and knows nothing about files, the
container format, or where blocks come from. It receives numbers and returns
decisions. That is what keeps the policy auditable in one place and testable
without a repository.
"""
module CCPStrategy

# The JSON subset used on the wire to the data engine. A submodule rather than a
# separate package: it is part of this engine's contract and precompiles with it.
include("MiniJSON.jl")
using .MiniJSON

export MiniJSON
export CostParameters, Candidate, BlockInput, decide_block, decide_all, parameters_from_env

# ---------------------------------------------------------------------------
# Cost parameters
# ---------------------------------------------------------------------------

"""
Weights and limits for the cost model. Every field is a policy choice, and each
default is stated with what it was chosen against rather than tuned in secret.

- `storage_weight`: cost per byte stored at rest. The unit the other weights are
  expressed relative to, so it is 1.0 by definition.

- `read_amplification_weight`: cost per byte that a *future read* must move
  because of a delta chain. Reconstructing a version at depth `d` reads `d+1`
  artifacts' worth of bytes, so a delta is not free even when it stores less. The
  default 0.05 says one byte of extra read traffic costs a twentieth of a byte of
  permanent storage — it makes deltas clearly worthwhile at shallow depth while
  refusing chains whose read cost dwarfs the saving. It has NOT been calibrated
  against a real workload; that is a measurement, and until it exists this number
  is an honest placeholder rather than a claim.

- `max_chain_depth`: hard bound on delta hops. Past this, FULL is forced whatever
  the arithmetic says, so reconstruction latency cannot grow without limit. 16 is
  a bound, not an optimum.
"""
struct CostParameters
    storage_weight::Float64
    read_amplification_weight::Float64
    max_chain_depth::Int
end

CostParameters(; storage_weight = 1.0,
                read_amplification_weight = 0.05,
                max_chain_depth = 16) =
    CostParameters(storage_weight, read_amplification_weight, max_chain_depth)

"""
    parameters_from_env()

Reads overrides from the environment so an operator (or a benchmark sweep) can
change policy without editing code. Unset variables keep the documented defaults.
"""
function parameters_from_env()
    getf(key, default) = haskey(ENV, key) ? parse(Float64, ENV[key]) : default
    geti(key, default) = haskey(ENV, key) ? parse(Int, ENV[key]) : default
    CostParameters(
        storage_weight = getf("CCP_STORAGE_WEIGHT", 1.0),
        read_amplification_weight = getf("CCP_READ_AMPLIFICATION_WEIGHT", 0.05),
        max_chain_depth = geti("CCP_MAX_CHAIN_DEPTH", 16),
    )
end

# ---------------------------------------------------------------------------
# Inputs and candidates
# ---------------------------------------------------------------------------

"""
What the data engine measured about one block. Contains no costs: costing is this
module's job, and receiving a precomputed cost would move the policy elsewhere.
"""
struct BlockInput
    index::Int
    logical_len::Int
    changed_bits::Int
    changed_bytes::Int
    delta_eligible::Bool
end

"""One possible representation, with the bytes it would store and what it implies."""
struct Candidate
    kind::String
    stored_bytes::Int
    "Adds a delta hop for every future read of this version."
    extends_chain::Bool
end

"""
    encoded_size(kind, block, encodings) -> Int

Bytes a representation would occupy, from the encoding parameters the data engine
declares. Kept as data rather than hard-coded so a new encoding in the format does
not require this module to know its internals — only its cost per unit.
"""
function encoded_size(kind::String, block::BlockInput, encodings::Dict)
    if kind == "FULL"
        return block.logical_len
    elseif kind == "IDENTICAL"
        return get(get(encodings, "identical", Dict()), "header_bytes", 0)
    elseif kind == "DELTA_RAW"
        return get(get(encodings, "raw", Dict()), "header_bytes", 0) + block.logical_len
    elseif kind == "DELTA_SPARSE"
        sparse = get(encodings, "sparse", Dict())
        header = get(sparse, "header_bytes", 4)
        per_byte = get(sparse, "bytes_per_changed_byte", 5)
        return header + per_byte * block.changed_bytes
    else
        error("unknown representation kind: $kind")
    end
end

"""
    candidates(block, encodings) -> Vector{Candidate}

Every representation that is *valid* for this block. Validity is a correctness
question, never an economic one:

- `FULL` is always valid — it needs no base, which is what makes it a
  reconstruction root.
- The delta forms require a base block of the same length (`delta_eligible`).
- `IDENTICAL` additionally requires that nothing actually changed.
"""
function candidates(block::BlockInput, encodings::Dict)
    out = Candidate[Candidate("FULL", encoded_size("FULL", block, encodings), false)]
    if block.delta_eligible
        if block.changed_bytes == 0
            push!(out, Candidate("IDENTICAL", encoded_size("IDENTICAL", block, encodings), true))
        end
        push!(out, Candidate("DELTA_SPARSE", encoded_size("DELTA_SPARSE", block, encodings), true))
        push!(out, Candidate("DELTA_RAW", encoded_size("DELTA_RAW", block, encodings), true))
    end
    return out
end

"""
    effective_cost(candidate, block, chain_depth, params) -> Float64

Total modelled cost of a representation: what it costs to keep, plus what it
costs to read back.

The read term is the part that stops the model from optimising storage into
unusable latency. A version stored at chain depth `d` must read `d+1` artifacts'
worth of bytes to reconstruct, so choosing a delta here charges the block for the
extra traffic every future read will move. A FULL block resets that to a single
read of itself.
"""
function effective_cost(c::Candidate, block::BlockInput, chain_depth::Int, params::CostParameters)
    storage = params.storage_weight * c.stored_bytes
    hops = c.extends_chain ? chain_depth + 1 : 0
    read_bytes = hops * block.logical_len
    return storage + params.read_amplification_weight * read_bytes
end

# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

"""
    decide_block(block, chain_depth, encodings, params) -> (kind, reason)

Chooses the lowest-cost valid representation for one block.

Ties resolve to `FULL`: when two options cost the same, the one that does not
lengthen the delta chain is strictly better for every future read, and it keeps
the version closer to a reconstruction root.
"""
function decide_block(block::BlockInput, chain_depth::Int, encodings::Dict, params::CostParameters)
    if !block.delta_eligible
        return ("FULL", "no base block of equal length; FULL is the only valid representation")
    end

    # The chain bound is a correctness-of-experience limit rather than an
    # economic one, so it is applied before the arithmetic and says so.
    if chain_depth + 1 > params.max_chain_depth
        return ("FULL",
                "chain depth would reach $(chain_depth + 1), over the limit of " *
                "$(params.max_chain_depth); FULL forced to bound reconstruction cost")
    end

    options = candidates(block, encodings)
    costs = [effective_cost(c, block, chain_depth, params) for c in options]

    best = 1
    for i in eachindex(options)
        # Strictly less, so an earlier candidate wins a tie — and FULL is first.
        if costs[i] < costs[best]
            best = i
        end
    end

    winner = options[best]
    full_cost = costs[findfirst(c -> c.kind == "FULL", options)]

    reason = if winner.kind == "IDENTICAL"
        "block is unchanged; stored as a reference to the base with no payload"
    elseif winner.kind == "FULL"
        cheapest_delta = minimum(
            [costs[i] for i in eachindex(options) if options[i].kind != "FULL"];
            init = Inf,
        )
        "FULL at cost $(round(full_cost, digits=1)) beats the cheapest delta at " *
        "$(round(cheapest_delta, digits=1)); $(block.changed_bytes) of " *
        "$(block.logical_len) bytes changed"
    else
        "$(winner.kind) stores $(winner.stored_bytes) bytes at cost " *
        "$(round(costs[best], digits=1)) versus FULL at $(round(full_cost, digits=1)); " *
        "$(block.changed_bytes) of $(block.logical_len) bytes changed"
    end

    return (winner.kind, reason)
end

"""
    decide_all(request) -> Dict

Decides every block in a request. `request` is the parsed protocol object from the
data engine; the returned dictionary is serialised straight back to it.
"""
function decide_all(request::Dict, params::CostParameters = parameters_from_env())
    protocol = get(request, "protocol", 0)
    protocol == 1 || error("unsupported strategy protocol version: $protocol")

    encodings = get(request, "encodings", Dict())
    chain_depth = Int(get(request, "chain_depth", 0))
    blocks = get(request, "blocks", [])

    decisions = Vector{Any}(undef, length(blocks))
    for (i, b) in enumerate(blocks)
        block = BlockInput(
            Int(b["index"]),
            Int(b["logical_len"]),
            Int(b["changed_bits"]),
            Int(b["changed_bytes"]),
            Bool(b["delta_eligible"]),
        )
        kind, reason = decide_block(block, chain_depth, encodings, params)
        decisions[i] = Dict("index" => block.index, "kind" => kind, "reason" => reason)
    end

    return Dict(
        "protocol" => 1,
        "engine" => "julia-ccp-strategy-0.1.0",
        # Echoed so a stored decision can be explained later against the exact
        # policy that produced it.
        "parameters" => Dict(
            "storage_weight" => params.storage_weight,
            "read_amplification_weight" => params.read_amplification_weight,
            "max_chain_depth" => params.max_chain_depth,
        ),
        "decisions" => decisions,
    )
end

# ---------------------------------------------------------------------------
# Precompilation workload
# ---------------------------------------------------------------------------
#
# Julia caches native code for methods actually executed during precompilation
# (1.9+). Without this, loading the package was cheap but the first `decide_all`
# call paid ~1.6s of JIT — on the write path of every store operation, since the
# engine runs as a fresh process per store. Exercising one representative request
# here moves that cost into the build.
#
# The workload deliberately covers every decision branch (ineligible, unchanged,
# sparse-wins, dense-loses) so each is compiled, and both JSON directions.
let
    encodings = Dict(
        "sparse" => Dict("header_bytes" => 4, "bytes_per_changed_byte" => 5),
        "raw" => Dict("header_bytes" => 0),
        "identical" => Dict("header_bytes" => 0),
    )
    request = Dict{String,Any}(
        "protocol" => 1,
        "block_size" => 4096,
        "chain_depth" => 0,
        "encodings" => encodings,
        "index_entry_bytes" => 32,
        "blocks" => Any[
            Dict{String,Any}("index" => 0, "logical_len" => 4096, "changed_bits" => 0,
                             "changed_bytes" => 0, "delta_eligible" => true),
            Dict{String,Any}("index" => 1, "logical_len" => 4096, "changed_bits" => 80,
                             "changed_bytes" => 10, "delta_eligible" => true),
            Dict{String,Any}("index" => 2, "logical_len" => 4096, "changed_bits" => 32768,
                             "changed_bytes" => 4096, "delta_eligible" => true),
            Dict{String,Any}("index" => 3, "logical_len" => 17, "changed_bits" => 0,
                             "changed_bytes" => 0, "delta_eligible" => false),
        ],
    )
    response = decide_all(request, CostParameters())
    MiniJSON.parse_json(MiniJSON.to_json(response))
end

end # module
