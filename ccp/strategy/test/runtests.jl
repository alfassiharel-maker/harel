# Tests for the strategy engine.
#
# These are the tests that guard the product's central rule: a delta must be
# chosen because it was measured cheaper, and FULL must win when it is not. Each
# case below is a decision the cost model has to get right for the system to be
# worth anything.

using Test

push!(LOAD_PATH, normpath(joinpath(@__DIR__, "..")))

using CCPStrategy
using CCPStrategy.MiniJSON

const ENCODINGS = Dict(
    "sparse" => Dict("header_bytes" => 4, "bytes_per_changed_byte" => 5),
    "raw" => Dict("header_bytes" => 0),
    "identical" => Dict("header_bytes" => 0),
)

params() = CostParameters()

block(; index = 0, len = 4096, bits = 0, bytes = 0, eligible = true) =
    BlockInput(index, len, bits, bytes, eligible)

@testset "MiniJSON" begin
    v = Dict("a" => 1, "b" => [1, 2, 3], "c" => "x\"y", "d" => nothing, "e" => true, "f" => 0.25)
    round_tripped = MiniJSON.parse_json(MiniJSON.to_json(v))
    @test round_tripped["a"] == 1
    @test round_tripped["b"] == [1, 2, 3]
    @test round_tripped["c"] == "x\"y"
    @test round_tripped["d"] === nothing
    @test round_tripped["e"] === true
    @test round_tripped["f"] == 0.25

    # Integers must survive as integers, not become floats.
    @test MiniJSON.parse_json("4096") === Int64(4096)
    @test MiniJSON.to_json(4096) == "4096"
    @test MiniJSON.to_json(1.0) == "1"

    @test_throws Exception MiniJSON.parse_json("{")
    @test_throws Exception MiniJSON.parse_json("[1,]")
    @test_throws Exception MiniJSON.parse_json("{} trailing")
end

@testset "sparse delta wins when the change is sparse" begin
    # 10 changed bytes in a 4 KiB block: sparse costs 4 + 50 = 54 bytes against
    # 4096. This is the case the whole product exists for.
    kind, reason = decide_block(block(bytes = 10, bits = 80), 0, ENCODINGS, params())
    @test kind == "DELTA_SPARSE"
    @test occursin("54", reason)
end

@testset "FULL wins when the change is dense" begin
    # Every byte changed: sparse would cost 4 + 5*4096 = 20484, five times the
    # block. A system that chose delta here would be worse than no system.
    kind, _ = decide_block(block(bytes = 4096, bits = 4096 * 8), 0, ENCODINGS, params())
    @test kind == "FULL"
end

@testset "the sparse break-even is where the arithmetic puts it" begin
    # The boundary is not simply "sparse is smaller than the block": the delta
    # also carries the read cost of one more chain hop. At depth 0 that is
    # 0.05 * 4096 = 204.8, so sparse wins while 4 + 5n + 204.8 < 4096, i.e.
    # n < 777.44. Asserting the exact boundary proves the decision is computed
    # from the full cost model rather than from a size comparison.
    @test decide_block(block(bytes = 777), 0, ENCODINGS, params())[1] == "DELTA_SPARSE"
    @test decide_block(block(bytes = 778), 0, ENCODINGS, params())[1] == "FULL"
end

@testset "an unchanged block is stored as a reference" begin
    kind, reason = decide_block(block(bytes = 0, bits = 0), 0, ENCODINGS, params())
    @test kind == "IDENTICAL"
    @test occursin("unchanged", reason)
end

@testset "a block with no base can only be FULL" begin
    for bytes in (0, 10, 4096)
        kind, reason = decide_block(block(bytes = bytes, eligible = false), 0, ENCODINGS, params())
        @test kind == "FULL"
        @test occursin("no base block", reason)
    end
end

@testset "read amplification makes deltas dearer as the chain grows" begin
    # A change sparse enough to win at depth 0 must eventually lose as the read
    # cost of the chain accumulates. This is the guard against optimising storage
    # into unusable reconstruction latency.
    b = block(bytes = 500)
    @test decide_block(b, 0, ENCODINGS, params())[1] == "DELTA_SPARSE"
    deep = decide_block(b, 40, ENCODINGS, CostParameters(max_chain_depth = 1000))[1]
    @test deep == "FULL"
end

@testset "the chain depth limit forces FULL regardless of cost" begin
    p = CostParameters(max_chain_depth = 4)
    # A single changed byte is as cheap as a delta ever gets, and it still must
    # lose once the bound is reached.
    kind, reason = decide_block(block(bytes = 1), 4, ENCODINGS, p)
    @test kind == "FULL"
    @test occursin("over the limit", reason)
    @test decide_block(block(bytes = 1), 3, ENCODINGS, p)[1] == "DELTA_SPARSE"
end

@testset "ties resolve to FULL" begin
    # Encoding parameters rigged so raw delta costs exactly what FULL costs. The
    # option that does not lengthen the chain must win.
    encodings = Dict(
        "sparse" => Dict("header_bytes" => 0, "bytes_per_changed_byte" => 1_000_000),
        "raw" => Dict("header_bytes" => 0),
        "identical" => Dict("header_bytes" => 0),
    )
    p = CostParameters(read_amplification_weight = 0.0)
    kind, _ = decide_block(block(bytes = 4096), 0, encodings, p)
    @test kind == "FULL"
end

@testset "decide_all handles a whole request" begin
    request = Dict(
        "protocol" => 1,
        "block_size" => 4096,
        "chain_depth" => 0,
        "encodings" => ENCODINGS,
        "index_entry_bytes" => 32,
        "blocks" => [
            Dict("index" => 0, "logical_len" => 4096, "changed_bits" => 0,
                 "changed_bytes" => 0, "delta_eligible" => true),
            Dict("index" => 1, "logical_len" => 4096, "changed_bits" => 80,
                 "changed_bytes" => 10, "delta_eligible" => true),
            Dict("index" => 2, "logical_len" => 4096, "changed_bits" => 32768,
                 "changed_bytes" => 4096, "delta_eligible" => true),
            Dict("index" => 3, "logical_len" => 100, "changed_bits" => 0,
                 "changed_bytes" => 0, "delta_eligible" => false),
        ],
    )
    response = decide_all(request, params())
    kinds = [d["kind"] for d in response["decisions"]]
    @test kinds == ["IDENTICAL", "DELTA_SPARSE", "FULL", "FULL"]
    # Decisions must come back in request order; the data engine relies on it.
    @test [d["index"] for d in response["decisions"]] == [0, 1, 2, 3]
    @test response["protocol"] == 1
    @test haskey(response, "parameters")

    # And the response must survive the serialiser the data engine will read.
    reparsed = MiniJSON.parse_json(MiniJSON.to_json(response))
    @test [d["kind"] for d in reparsed["decisions"]] == kinds
end

@testset "an unsupported protocol is refused" begin
    @test_throws Exception decide_all(Dict("protocol" => 99, "blocks" => []), params())
end

println("strategy: all tests passed")
