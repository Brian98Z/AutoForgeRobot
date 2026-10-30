using SHA
using JSON3

struct ExactWireInt
    value::Int64
end
struct RawWireValue
    source::String
end
JSON3.StructTypes.StructType(::Type{ExactWireInt}) = JSON3.RawType()
JSON3.StructTypes.StructType(::Type{RawWireValue}) = JSON3.RawType()
raw_source(raw::JSON3.RawValue) = String(raw.bytes[raw.pos:raw.pos + raw.len - 1])
function JSON3.StructTypes.construct(::Type{ExactWireInt}, raw::JSON3.RawValue)
    token = raw_source(raw)
    occursin(r"^-?(0|[1-9][0-9]*)$", token) || error("expected decimal integer token")
    ExactWireInt(parse(Int64, token))
end
JSON3.StructTypes.construct(::Type{RawWireValue}, raw::JSON3.RawValue) = RawWireValue(raw_source(raw))

const WireCase = NamedTuple{(:input, :expected), Tuple{Tuple{ExactWireInt, ExactWireInt}, ExactWireInt}}
const WireTask = NamedTuple{(:id, :goal, :public_cases), Tuple{String, String, Vector{WireCase}}}
const WireRequest = NamedTuple{(:schema, :task, :round, :baseline_wat, :feedback),
    Tuple{ExactWireInt, WireTask, ExactWireInt, Union{Nothing,String}, Vector{RawWireValue}}}
plain_case(c) = (input=(c.input[1].value, c.input[2].value), expected=c.expected.value)

function validate_json_lexically(source::String)
    bytes = codeunits(source); n = length(bytes); i = 1
    while i <= n && bytes[i] in (0x20,0x09,0x0a,0x0d); i += 1; end
    # Scan strings independently; JSON3 performs full document grammar parsing below.
    function string_token!(i)
        i += 1
        while i <= n
            b = bytes[i]
            b == 0x22 && return i + 1
            b < 0x20 && error("unescaped control character in JSON string")
            if b == 0x5c
                i += 1; i <= n || error("incomplete JSON escape")
                e = bytes[i]
                if e == 0x75
                    i + 4 <= n || error("incomplete Unicode escape")
                    hex = String(Vector{UInt8}(bytes[i+1:i+4])); all(isxdigit, hex) || error("invalid Unicode escape")
                    u = parse(UInt16, hex; base=16); i += 4
                    if 0xd800 <= u <= 0xdbff
                        i + 6 <= n && bytes[i+1] == 0x5c && bytes[i+2] == 0x75 || error("unpaired high surrogate")
                        lowhex = String(Vector{UInt8}(bytes[i+3:i+6])); all(isxdigit, lowhex) || error("invalid Unicode escape")
                        0xdc00 <= parse(UInt16, lowhex; base=16) <= 0xdfff || error("invalid surrogate pair")
                        i += 6
                    elseif 0xdc00 <= u <= 0xdfff
                        error("unpaired low surrogate")
                    end
                elseif !(e in (0x22,0x5c,0x2f,0x62,0x66,0x6e,0x72,0x74))
                    error("invalid JSON escape")
                end
            end
            i += 1
        end
        error("unterminated JSON string")
    end
    # JSON3.Object may collapse duplicate keys; capture decoded names lexically per object.
    stack = Vector{Set{String}}(); i = 1; depth=0; completed=false
    while i <= n
        b = bytes[i]
        if completed && !(b in (0x20,0x09,0x0a,0x0d));error("trailing data after JSON document");end
        if b == 0x22
            start = i; i = string_token!(i)
            j = i
            while j <= n && bytes[j] in (0x20,0x09,0x0a,0x0d); j += 1; end
            if j <= n && bytes[j] == 0x3a
                isempty(stack) && error("object key outside JSON object")
                key = JSON3.read(String(Vector{UInt8}(bytes[start:i-1])), String)
                key in stack[end] && error("duplicate JSON object key")
                push!(stack[end], key)
            end
        elseif b == 0x7b
            push!(stack, Set{String}()); depth+=1; i += 1
        elseif b == 0x5b
            depth+=1; i+=1
        elseif b == 0x5d
            depth-=1;depth>=0 || error("unbalanced JSON array");i+=1
            depth==0 && (completed=true)
        elseif b == 0x7d
            isempty(stack) && error("unbalanced JSON object")
            pop!(stack);depth-=1;i+=1
            depth==0 && (completed=true)
        else
            i += 1
        end
    end
    isempty(stack) && depth==0 && completed || error("incomplete JSON document")
    nothing
end

function exact_fields(obj, required, optional=())
    obj isa JSON3.Object || error("expected JSON object")
    keys_seen = Set(String(k) for k in keys(obj))
    all(k -> k in keys_seen, required) || error("missing required object field")
    all(k -> k in required || k in optional, keys_seen) || error("unknown object field")
end

function read_request(source::String)
    isvalid(source) || error("invalid UTF-8")
    validate_json_lexically(source)
    root = JSON3.read(source)
    exact_fields(root, ("schema","task","round","baseline_wat","feedback"), ("rejected_wats",))
    request = JSON3.read(source, WireRequest)
    request.schema.value == 1 || error("unsupported input schema")
    exact_fields(root.task, ("id","goal","public_cases"), ("references",))
    root.task.id isa String && root.task.goal isa String || error("task id and goal must be strings")
    root.task.public_cases isa JSON3.Array && root.feedback isa JSON3.Array || error("cases and feedback must be arrays")
    foreach(c -> exact_fields(c, ("input","expected")), root.task.public_cases)
    references=haskey(root.task,"references") ? root.task.references : []
    (references isa JSON3.Array || references isa Vector) && length(references)<=4 || error("invalid references")
    for ref in references
        exact_fields(ref,("id","content_sha256","content","kind","origin","license"))
        all(v->v isa String,values(ref)) || error("reference values must be strings")
        ref.kind in ("wat","source","rea-evidence") && ncodeunits(ref.content)<=32768 && bytes2hex(sha256(ref.content))==ref.content_sha256 || error("reference integrity mismatch")
    end
    root.baseline_wat === nothing || root.baseline_wat isa String || error("baseline_wat must be string or null")
    haskey(root,"rejected_wats") && (!(root.rejected_wats isa JSON3.Array) || length(root.rejected_wats)>8 || !all(x->x isa String,root.rejected_wats)) && error("invalid rejected_wats")
    for raw in request.feedback
        value = JSON3.read(raw.source)
        if haskey(value,"input") || haskey(value,"expected")
            exact_fields(value, ("input","expected"), ("actual","error"))
            value.input isa JSON3.Array && length(value.input)==2 || error("feedback input must contain two integers")
            if haskey(value,"actual") && value.actual !== nothing
                fields = JSON3.read(raw.source, Dict{String,RawWireValue})
                JSON3.read(fields["actual"].source, ExactWireInt)
            end
            haskey(value,"error") && value.error !== nothing && !(value.error isa String) && error("feedback error must be string or null")
        else
            exact_fields(value, ("error",)); value.error isa String || error("feedback error must be string")
        end
    end

    feedback = Any[]
    for raw in request.feedback
        value = JSON3.read(raw.source)
        if haskey(value,"input") && haskey(value,"expected")
            push!(feedback, plain_case(JSON3.read(raw.source, WireCase)))
        else
            push!(feedback, (error=value.error,))
        end
    end
    (schema=request.schema.value, task=(id=request.task.id, goal=request.task.goal, public_cases=map(plain_case,request.task.public_cases),references=references),
     round=request.round.value, baseline_wat=request.baseline_wat, feedback=feedback,
     rejected_wats=haskey(root,"rejected_wats") ? String.(root.rejected_wats) : String[])
end
