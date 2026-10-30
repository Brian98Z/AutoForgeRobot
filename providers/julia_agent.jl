include(joinpath(@__DIR__, "protocol.jl"))


wrap64(n::Integer) = reinterpret(Int64, UInt64(mod(BigInt(n), BigInt(1) << 64)))
function emit(body::String)
    return "(module (func (export \"solve\") (param \$x i64) (param \$y i64) (result i64) " * body * "))"
end

function cases_from(task)
    [(Int64(c.input[1]), Int64(c.input[2]), Int64(c.expected)) for c in task.public_cases]
end


abstract type ExprNode end
struct InputNode <: ExprNode; index::Int; end
struct ConstNode <: ExprNode; value::Int64; end
struct OpNode <: ExprNode; op::Symbol; args::Vector{ExprNode}; end

function evaluate(n::InputNode, x, y)
    n.index == 1 ? BigInt(x) : BigInt(y)
end
evaluate(n::ConstNode, x, y) = BigInt(n.value)
function evaluate(n::OpNode, x, y)
    v = map(arg -> evaluate(arg,x,y), n.args)
    n.op == :plus && return v[1] + v[2]
    n.op == :minus && return v[1] - v[2]
    n.op == :times && return v[1] * v[2]
    n.op == :lower && return min(v[1],v[2])
    n.op == :upper && return max(v[1],v[2])
    n.op == :distance && return abs(BigInt(wrap64(v[1]-v[2])))
    n.op == :bound && return max(v[2],min(v[3],v[1]))
    error("unsupported expression node")
end

render(n::InputNode) = "(local.get \$" * (n.index == 1 ? "x" : "y") * ")"
render(n::ConstNode) = "(i64.const " * string(n.value) * ")"
function render(n::OpNode)
    a = map(render,n.args)
    n.op == :plus && return "(i64.add " * a[1] * " " * a[2] * ")"
    n.op == :minus && return "(i64.sub " * a[1] * " " * a[2] * ")"
    n.op == :times && return "(i64.mul " * a[1] * " " * a[2] * ")"
    n.op == :lower && return "(if (result i64) (i64.lt_s " * a[1] * " " * a[2] * ") (then " * a[1] * ") (else " * a[2] * "))"
    n.op == :upper && return "(if (result i64) (i64.gt_s " * a[1] * " " * a[2] * ") (then " * a[1] * ") (else " * a[2] * "))"
    if n.op == :distance
        difference = "(i64.sub $(a[1]) $(a[2]))"
        return "(if (result i64) (i64.lt_s $difference (i64.const 0)) (then (i64.sub (i64.const 0) $difference)) (else $difference))"
    end
    if n.op == :bound
        upper = "(if (result i64) (i64.gt_s $(a[1]) $(a[3])) (then $(a[3])) (else $(a[1])))"
        return "(if (result i64) (i64.lt_s $(a[1]) $(a[2])) (then $(a[2])) (else $upper))"
    end
    error("unsupported expression node")
end

function matches_tree(tree, cases)
    all(c -> wrap64(evaluate(tree,c[1],c[2])) == c[3], cases)
end

function affine_tree(a,b,c)
    leaves = ExprNode[]
    for (coefficient, variable) in ((a,InputNode(1)),(b,InputNode(2)))
        coefficient == 0 && continue
        term = variable
        if abs(coefficient) != 1
            term = OpNode(:times,ExprNode[ConstNode(Int64(abs(coefficient))),variable])
        end
        coefficient < 0 && (term = OpNode(:minus,ExprNode[ConstNode(0),term]))
        push!(leaves,term)
    end
    c != 0 && push!(leaves,ConstNode(Int64(c)))
    isempty(leaves) && return ConstNode(0)
    foldl((l,r)->OpNode(:plus,ExprNode[l,r]),leaves)
end
function offline(task, roundno, baseline, feedback, rejected=String[])
    roundno == 1 && baseline !== nothing && !isempty(String(baseline)) && return (String(baseline), "reuse-baseline")
    for ref in task.references
        ref.kind=="wat" && !(ref.content in rejected) && return (String(ref.content),"reference:"*String(ref.id))
    end
    cs = cases_from(task)
    append!(cs, [(f.input[1], f.input[2], f.expected) for f in feedback if haskey(f,:input) && haskey(f,:expected)])
    x,y = InputNode(1),InputNode(2)
    trees = ExprNode[OpNode(:plus,[x,y]),OpNode(:minus,[x,y]),OpNode(:times,[x,y]),OpNode(:lower,[x,y]),OpNode(:upper,[x,y]),OpNode(:distance,[x,y])]
    for tree in trees
        matches_tree(tree,cs) && !(emit(render(tree)) in rejected) && return (emit(render(tree)),"expression-tree")
    end
    for c in -32:32, b in -8:8, a in -8:8
        tree=affine_tree(a,b,c)
        matches_tree(tree,cs) && !(emit(render(tree)) in rejected) && return (emit(render(tree)),"linear-form:$a,$b,$c")
    end
    for arg in (y,x), high in -16:16, low in -16:high
        tree=OpNode(:bound,ExprNode[arg,ConstNode(Int64(low)),ConstNode(Int64(high))])
        matches_tree(tree,cs) && !(emit(render(tree)) in rejected) && return (emit(render(tree)),"bounded-form")
    end
    error("offline synthesizer found no supported expression matching public examples")
end


function main()
    try
        bytes = read(stdin, 262145)
        length(bytes) <= 262144 || error("input exceeds 256 KiB")
        source=String(bytes)
        obj = read_request(source)
        rejected = obj.rejected_wats
        wat, strategy = offline(obj.task, Int(obj.round), obj.baseline_wat, obj.feedback, rejected)
        println(JSON3.write((wat=wat, strategy=strategy)))
    catch e
        println(stderr, "julia_agent: ", sprint(showerror, e))
        exit(2)
    end
end

main()
