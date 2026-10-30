using Test
include(joinpath(@__DIR__, "..", "providers", "protocol.jl"))

@testset "exact wire integer boundaries and failure feedback" begin
    for value in [typemin(Int64), typemin(Int64)+1, -9007199254740993, -1, 0, 9007199254740993, typemax(Int64)]
        source = """{"schema":1,"task":{"id":"boundary","goal":"exact","public_cases":[{"input":[$value,1],"expected":$value}]},"round":2,"baseline_wat":null,"feedback":[{"input":[$value,1],"expected":$value,"actual":0,"error":null},{"error":"compile failed"}]}"""
        result = read_request(source)
        @test result.task.public_cases[1].input[1] == value
        @test result.task.public_cases[1].expected == value
        @test result.feedback[1].input[1] == value
        @test result.feedback[1].expected == value
        @test result.feedback[2].error == "compile failed"
    end
    for bad in ["9223372036854775808", "-9223372036854775809", "1.5", "1e3", "\"12\""]
        source = """{"schema":1,"task":{"id":"bad","goal":"reject","public_cases":[{"input":[$bad,1],"expected":0}]},"round":1,"baseline_wat":null,"feedback":[]}"""
        @test_throws Exception read_request(source)
    end
end
@testset "strict request structure and Unicode parity" begin
    base = """{"schema":1,"task":{"id":"ok","goal":"unicode ☃","public_cases":[{"input":[1,2],"expected":3}]},"round":2,"baseline_wat":null,"feedback":[{"input":[1,2],"expected":3,"actual":3,"error":null}],"rejected_wats":["α"]}"""
    @test read_request(base).task.goal == "unicode ☃"
    @test read_request(base).feedback[1].expected == 3
    for bad in [replace(base, "\"schema\":1"=>"\"schema\":1,\"schema\":1"),
                replace(base, "\"schema\":1"=>"\"schema\":1,\"\\u0073chema\":1"),
                replace(base, "\"feedback\":["=>"\"feedback\":[{\"input\":[1,2],\"expected\":3,\"extra\":0},"),
                replace(base, "\"goal\":\"unicode ☃\""=>"\"goal\":\"unicode \\ud800\""),
                base * " {}"]
        @test_throws Exception read_request(bad)
    end
    for actual in ["1.5", "1e3", "9223372036854775808", "true"]
        bad = replace(base, "\"actual\":3"=>"\"actual\":"*actual)
        @test_throws Exception read_request(bad)
    end
end
