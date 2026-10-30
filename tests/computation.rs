#[path = "../src/sandbox.rs"]
mod sandbox;

use sandbox::{compile, evaluate};

fn compiled(source: &str) -> Vec<u8> {
    compile(source).expect("valid test module should compile")
}

#[test]
fn iterative_euclidean_gcd_uses_loop_branch_and_unsigned_remainder() {
    let wasm = compiled(
        r#"(module
            (func (export "solve") (param $a i64) (param $b i64) (result i64)
                (local $r i64)
                (block $done
                    (loop $again
                        local.get $b
                        i64.eqz
                        br_if $done
                        local.get $a
                        local.get $b
                        i64.rem_u
                        local.set $r
                        local.get $b
                        local.set $a
                        local.get $r
                        local.set $b
                        br $again))
                local.get $a))"#,
    );
    assert_eq!(evaluate(&wasm, [1071, 462], 10_000), Ok(21));
    assert_eq!(evaluate(&wasm, [0, 37], 10_000), Ok(37));
    assert_eq!(evaluate(&wasm, [48, 0], 10_000), Ok(48));
}

#[test]
fn recursive_factorial_handles_zero_and_bounded_inputs() {
    let wasm = compiled(
        r#"(module
            (func $fact (param $n i64) (result i64)
                local.get $n
                i64.eqz
                if (result i64)
                    i64.const 1
                else
                    local.get $n
                    local.get $n
                    i64.const 1
                    i64.sub
                    call $fact
                    i64.mul
                end)
            (func (export "solve") (param $n i64) (param i64) (result i64)
                local.get $n
                call $fact))"#,
    );
    assert_eq!(evaluate(&wasm, [0, 0], 10_000), Ok(1));
    assert_eq!(evaluate(&wasm, [1, 0], 10_000), Ok(1));
    assert_eq!(evaluate(&wasm, [10, 0], 10_000), Ok(3_628_800));
}

#[test]
fn linear_memory_load_store_and_evaluations_start_with_fresh_memory() {
    let wasm = compiled(
        r#"(module
            (memory 1 1)
            (data (i32.const 0) "\07\00\00\00\00\00\00\00")
            (func (export "solve") (param $value i64) (param i64) (result i64)
                local.get $value
                i64.eqz
                if
                else
                    i32.const 0
                    local.get $value
                    i64.store
                end
                i32.const 0
                i64.load))"#,
    );
    assert_eq!(evaluate(&wasm, [123_456, 0], 1_000), Ok(123_456));
    assert_eq!(evaluate(&wasm, [0, 0], 1_000), Ok(7));
}

#[test]
fn indirect_call_dispatches_through_a_bounded_table() {
    let wasm = compiled(
        r#"(module
            (type $unary (func (param i64 i64) (result i64)))
            (table 2 2 funcref)
            (elem (i32.const 0) $double $add_one)
            (func $double (type $unary) (param $x i64) (param i64) (result i64)
                local.get $x i64.const 2 i64.mul)
            (func $add_one (type $unary) (param $x i64) (param i64) (result i64)
                local.get $x i64.const 1 i64.add)
            (func (export "solve") (param $x i64) (param $index i64) (result i64)
                local.get $x
                i64.const 0
                local.get $index
                i32.wrap_i64
                call_indirect (type $unary)))"#,
    );
    assert_eq!(evaluate(&wasm, [21, 0], 1_000), Ok(42));
    assert_eq!(evaluate(&wasm, [41, 1], 1_000), Ok(42));
}

#[test]
fn bytecode_register_machine_decodes_and_branches_over_a_nontrivial_program() {
    let wasm = compiled(
        r#"(module
            (memory 1 1)
            ;; Finite test program; bounded by sandbox fuel, not a Turing-completeness proof.
            (data (i32.const 0) "\01\05\02\00\03\00\04\01\00\00")
            (func (export "solve") (param i64 i64) (result i64)
                (local $pc i32) (local $op i32) (local $arg i32)
                (local $counter i32) (local $sum i64)
                (block $halt
                    (loop $dispatch
                        local.get $pc
                        i32.load8_u
                        local.set $op
                        local.get $pc
                        i32.const 1
                        i32.add
                        i32.load8_u
                        local.set $arg
                        local.get $op
                        i32.eqz
                        br_if $halt
                        local.get $op
                        i32.const 1
                        i32.eq
                        if
                            local.get $arg
                            local.set $counter
                        else
                            local.get $op
                            i32.const 2
                            i32.eq
                            if
                                local.get $sum
                                local.get $counter
                                i64.extend_i32_u
                                i64.add
                                local.set $sum
                            else
                                local.get $op
                                i32.const 3
                                i32.eq
                                if
                                    local.get $counter
                                    i32.const 1
                                    i32.sub
                                    local.set $counter
                                else
                                    local.get $op
                                    i32.const 4
                                    i32.eq
                                    if
                                        local.get $counter
                                        i32.eqz
                                        if
                                            local.get $pc
                                            i32.const 2
                                            i32.add
                                            local.set $pc
                                        else
                                            local.get $arg
                                            i32.const 2
                                            i32.mul
                                            local.set $pc
                                        end
                                    end
                                end
                            end
                        end
                        local.get $op
                        i32.const 4
                        i32.ne
                        if
                            local.get $pc
                            i32.const 2
                            i32.add
                            local.set $pc
                        end
                        br $dispatch))
                local.get $sum))"#,
    );
    assert_eq!(evaluate(&wasm, [0, 0], 10_000), Ok(15));
}

#[test]
fn functions_compose_across_calls() {
    let wasm = compiled(
        r#"(module
            (func $square (param $x i64) (result i64)
                local.get $x local.get $x i64.mul)
            (func $offset (param $x i64) (result i64)
                local.get $x i64.const 7 i64.add)
            (func (export "solve") (param $x i64) (param i64) (result i64)
                local.get $x call $square call $offset))"#,
    );
    assert_eq!(evaluate(&wasm, [5, 0], 1_000), Ok(32));
}

#[test]
fn i64_arithmetic_has_wasm_wraparound_and_signed_division_overflow_traps() {
    let add = compiled(
        r#"(module (func (export "solve") (param $a i64) (param $b i64) (result i64)
            local.get $a local.get $b i64.add))"#,
    );
    assert_eq!(evaluate(&add, [i64::MAX, 1], 1_000), Ok(i64::MIN));

    let divide = compiled(
        r#"(module (func (export "solve") (param $a i64) (param $b i64) (result i64)
            local.get $a local.get $b i64.div_s))"#,
    );
    assert!(evaluate(&divide, [i64::MIN, -1], 1_000).is_err());
    assert_eq!(evaluate(&divide, [i64::MIN, 1], 1_000), Ok(i64::MIN));
}

#[test]
fn divide_by_zero_reports_a_runtime_trap() {
    let wasm = compiled(
        r#"(module (func (export "solve") (param $a i64) (param $b i64) (result i64)
            local.get $a local.get $b i64.div_s))"#,
    );
    assert!(evaluate(&wasm, [100, 0], 1_000).is_err());
    assert_eq!(evaluate(&wasm, [100, 4], 1_000), Ok(25));
}

#[test]
fn out_of_bounds_memory_access_reports_a_runtime_trap() {
    let wasm = compiled(
        r#"(module (memory 1 1)
            (func (export "solve") (param i64 i64) (result i64)
                i32.const 65_532 i64.load))"#,
    );
    assert!(evaluate(&wasm, [0, 0], 1_000).is_err());
}

#[test]
fn memory_growth_beyond_declared_resource_limit_fails_without_expanding_memory() {
    let wasm = compiled(
        r#"(module (memory 1 3)
            (func (export "solve") (param i64 i64) (result i64)
                i32.const 2
                memory.grow
                i64.extend_i32_s))"#,
    );
    assert!(evaluate(&wasm, [0, 0], 1_000).is_err());
}

#[test]
fn wrong_solve_abi_is_rejected_with_exact_type_diagnostic() {
    let error = compile(
        r#"(module (func (export "solve") (param i32 i64) (result i64)
            local.get 0 i64.extend_i32_s))"#,
    ).unwrap_err();
    assert!(error.contains("exact type"), "unexpected ABI rejection: {error}");
}

