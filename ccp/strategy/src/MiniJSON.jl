"""
    MiniJSON

The small JSON subset exchanged with the data engine.

Written here rather than pulled from a package for the same reason the Rust side
hand-rolls its own: this boundary is part of the engine's contract, it must work
in a bare Julia with no package registry reachable, and the subset is small enough
that owning it is cheaper than depending on it. It parses objects, arrays,
strings, numbers, booleans and null, and rejects anything else instead of
guessing.
"""
module MiniJSON

export parse_json, to_json

# ---- parsing --------------------------------------------------------------

mutable struct Reader
    s::String
    i::Int
end

@inline peek(r::Reader) = r.i <= lastindex(r.s) ? r.s[r.i] : '\0'

function skipws!(r::Reader)
    while r.i <= lastindex(r.s) && r.s[r.i] in (' ', '\t', '\n', '\r')
        r.i = nextind(r.s, r.i)
    end
end

function expect!(r::Reader, c::Char)
    skipws!(r)
    peek(r) == c || error("expected '$c' at index $(r.i)")
    r.i = nextind(r.s, r.i)
end

function parse_json(text::AbstractString)
    r = Reader(String(text), 1)
    v = parse_value!(r)
    skipws!(r)
    r.i > lastindex(r.s) || error("trailing input at index $(r.i)")
    return v
end

function parse_value!(r::Reader)
    skipws!(r)
    c = peek(r)
    if c == '{'
        return parse_object!(r)
    elseif c == '['
        return parse_array!(r)
    elseif c == '"'
        return parse_string!(r)
    elseif c == 't'
        parse_literal!(r, "true"); return true
    elseif c == 'f'
        parse_literal!(r, "false"); return false
    elseif c == 'n'
        parse_literal!(r, "null"); return nothing
    elseif c == '\0'
        error("unexpected end of input")
    else
        return parse_number!(r)
    end
end

function parse_literal!(r::Reader, word::String)
    stop = r.i + length(word) - 1
    stop <= lastindex(r.s) && r.s[r.i:stop] == word || error("expected '$word' at index $(r.i)")
    r.i = stop + 1
end

function parse_object!(r::Reader)
    expect!(r, '{')
    out = Dict{String,Any}()
    skipws!(r)
    if peek(r) == '}'
        r.i = nextind(r.s, r.i)
        return out
    end
    while true
        skipws!(r)
        key = parse_string!(r)
        expect!(r, ':')
        out[key] = parse_value!(r)
        skipws!(r)
        c = peek(r)
        if c == ','
            r.i = nextind(r.s, r.i)
        elseif c == '}'
            r.i = nextind(r.s, r.i)
            return out
        else
            error("expected ',' or '}' at index $(r.i)")
        end
    end
end

function parse_array!(r::Reader)
    expect!(r, '[')
    out = Any[]
    skipws!(r)
    if peek(r) == ']'
        r.i = nextind(r.s, r.i)
        return out
    end
    while true
        push!(out, parse_value!(r))
        skipws!(r)
        c = peek(r)
        if c == ','
            r.i = nextind(r.s, r.i)
        elseif c == ']'
            r.i = nextind(r.s, r.i)
            return out
        else
            error("expected ',' or ']' at index $(r.i)")
        end
    end
end

function parse_string!(r::Reader)
    expect!(r, '"')
    buf = IOBuffer()
    while true
        r.i <= lastindex(r.s) || error("unterminated string")
        c = r.s[r.i]
        r.i = nextind(r.s, r.i)
        if c == '"'
            return String(take!(buf))
        elseif c == '\\'
            r.i <= lastindex(r.s) || error("unterminated escape")
            e = r.s[r.i]
            r.i = nextind(r.s, r.i)
            if e == 'n'; print(buf, '\n')
            elseif e == 't'; print(buf, '\t')
            elseif e == 'r'; print(buf, '\r')
            elseif e == 'b'; print(buf, '\b')
            elseif e == 'f'; print(buf, '\f')
            elseif e == '"'; print(buf, '"')
            elseif e == '\\'; print(buf, '\\')
            elseif e == '/'; print(buf, '/')
            elseif e == 'u'
                stop = r.i + 3
                stop <= lastindex(r.s) || error("truncated \\u escape")
                print(buf, Char(parse(UInt32, r.s[r.i:stop]; base = 16)))
                r.i = stop + 1
            else
                error("bad escape \\$e")
            end
        else
            print(buf, c)
        end
    end
end

function parse_number!(r::Reader)
    start = r.i
    while r.i <= lastindex(r.s) && (isdigit(r.s[r.i]) || r.s[r.i] in ('-', '+', '.', 'e', 'E'))
        r.i = nextind(r.s, r.i)
    end
    text = r.s[start:prevind(r.s, r.i)]
    # Integers stay integers: block offsets and lengths must not acquire a
    # floating-point representation on the way through.
    v = tryparse(Int64, text)
    v === nothing || return v
    f = tryparse(Float64, text)
    f === nothing && error("bad number '$text' at index $start")
    return f
end

# ---- serialising ----------------------------------------------------------

to_json(v) = sprint(write_json, v)

write_json(io::IO, ::Nothing) = print(io, "null")
write_json(io::IO, v::Bool) = print(io, v ? "true" : "false")
write_json(io::IO, v::Integer) = print(io, v)

function write_json(io::IO, v::AbstractFloat)
    isfinite(v) || error("cannot serialise non-finite number $v")
    # Whole floats are written without a decimal point so the Rust side's
    # integer accessors accept them.
    if v == round(v) && abs(v) < 9.0e15
        print(io, Int64(v))
    else
        print(io, v)
    end
end

function write_json(io::IO, s::AbstractString)
    print(io, '"')
    for c in s
        if c == '"'; print(io, "\\\"")
        elseif c == '\\'; print(io, "\\\\")
        elseif c == '\n'; print(io, "\\n")
        elseif c == '\r'; print(io, "\\r")
        elseif c == '\t'; print(io, "\\t")
        elseif UInt32(c) < 0x20; print(io, "\\u", string(UInt32(c); base = 16, pad = 4))
        else; print(io, c)
        end
    end
    print(io, '"')
end

function write_json(io::IO, v::AbstractVector)
    print(io, '[')
    for (i, x) in enumerate(v)
        i > 1 && print(io, ',')
        write_json(io, x)
    end
    print(io, ']')
end

function write_json(io::IO, d::AbstractDict)
    print(io, '{')
    # Sorted keys so output is byte-stable across runs, which makes the exchange
    # diffable and testable.
    for (i, k) in enumerate(sort!(collect(keys(d)); by = string))
        i > 1 && print(io, ',')
        write_json(io, string(k))
        print(io, ':')
        write_json(io, d[k])
    end
    print(io, '}')
end

end # module
