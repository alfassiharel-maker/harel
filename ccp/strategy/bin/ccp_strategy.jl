#!/usr/bin/env julia
#
# The strategy engine's process entry point.
#
# Protocol: one JSON request on stdin, one JSON response on stdout. Nothing else
# goes to stdout — diagnostics go to stderr — because the data engine parses stdout
# in full and a stray line would be a protocol error.
#
# The engine is loaded as a package (`using CCPStrategy`) rather than by including
# its source, so Julia serves it from the precompilation cache. That is the
# difference between ~2.0s and a fraction of a second per store operation.
#
# One process per store operation, carrying every block's measurements: process
# startup would otherwise dominate, and a block's decision depends on
# artifact-wide context anyway.

push!(LOAD_PATH, normpath(joinpath(@__DIR__, "..")))

using CCPStrategy
using CCPStrategy.MiniJSON

function main()
    input = read(stdin, String)
    if isempty(strip(input))
        println(stderr, "ccp-strategy: empty request on stdin")
        return 2
    end

    request = try
        MiniJSON.parse_json(input)
    catch e
        println(stderr, "ccp-strategy: cannot parse request: ", e)
        return 2
    end

    response = try
        CCPStrategy.decide_all(request)
    catch e
        println(stderr, "ccp-strategy: decision failed: ", e)
        return 3
    end

    print(MiniJSON.to_json(response))
    return 0
end

exit(main())
