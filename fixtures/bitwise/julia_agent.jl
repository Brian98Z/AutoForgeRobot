include(joinpath(@__DIR__, "protocol.jl"))

request = read_request(read(stdin, String))
request.schema == 1 || error("unsupported protocol")
Set(String.(keys(request.task))) == Set(["id", "goal", "public_cases"]) || error("unexpected task fields")

function synthesize(cases)
    for (op, instruction) in [(|,"or"), (&,"and"), (xor,"xor")]
        valid = true
        for sample in cases
            if op(Int64(sample.input[1]), Int64(sample.input[2])) != Int64(sample.expected)
                valid = false
                break
            end
        end
        if valid
            source = "(module (func (export \"solve\") (param i64 i64) (result i64) local.get 0 local.get 1 i64.$instruction))"
            return (wat=source, strategy="operator-extension:julia-bitwise:$instruction")
        end
    end
    error("public examples not expressible in bitwise grammar")
end
println(JSON3.write(synthesize(request.task.public_cases)))
