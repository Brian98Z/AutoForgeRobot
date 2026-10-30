use wasmi::core::ValType;
use wasmi::{Config, Engine, ExternType, Linker, Module, Store, StoreLimits, StoreLimitsBuilder, TypedFunc};

const MAX_WAT_BYTES: usize = 64 * 1024;
const MAX_WASM_BYTES: usize = 256 * 1024;
const MAX_MEMORY_BYTES: usize = 2 * 64 * 1024;
const MAX_TABLE_ELEMENTS: u32 = 1024;

/// Compile textual WebAssembly with input and output bounds.
pub fn compile(source: &str) -> Result<Vec<u8>, String> {
    if source.len() > MAX_WAT_BYTES {
        return Err(format!("WAT source exceeds {MAX_WAT_BYTES} byte limit"));
    }
    let bytes = wat::parse_str(source).map_err(|error| format!("invalid WAT: {error}"))?;
    if bytes.len() > MAX_WASM_BYTES {
        return Err(format!("compiled Wasm exceeds {MAX_WASM_BYTES} byte limit"));
    }
    let engine = engine();
    let module = Module::new(&engine, &bytes).map_err(|error| format!("invalid Wasm module: {error}"))?;
    validate_module(&module)?;
    Ok(bytes)
}

/// Execute `solve(i64, i64) -> i64` with a fresh bounded store and fuel budget.
pub fn evaluate(bytes: &[u8], input: [i64; 2], fuel: u64) -> Result<i64, String> {
    if bytes.len() > MAX_WASM_BYTES {
        return Err(format!("Wasm binary exceeds {MAX_WASM_BYTES} byte limit"));
    }
    let engine = engine();
    let module = Module::new(&engine, bytes).map_err(|error| format!("invalid Wasm module: {error}"))?;
    validate_module(&module)?;

    let limits = StoreLimitsBuilder::new()
        .memory_size(MAX_MEMORY_BYTES)
        .table_elements(MAX_TABLE_ELEMENTS)
        .instances(1)
        .tables(1)
        .memories(1)
        .trap_on_grow_failure(true)
        .build();
    let mut store = Store::new(&engine, limits);
    store.limiter(|limits| limits);
    // Install fuel before instantiation so a module start function is metered too.
    store.set_fuel(fuel).map_err(|error| format!("cannot set fuel: {error}"))?;

    let linker = Linker::<StoreLimits>::new(&engine);
    let instance = linker
        .instantiate(&mut store, &module)
        .and_then(|pre| pre.start(&mut store))
        .map_err(|error| format!("Wasm instantiation/start failed: {error}"))?;
    let solve: TypedFunc<(i64, i64), i64> = instance
        .get_typed_func(&store, "solve")
        .map_err(|error| format!("required export solve(i64, i64) -> i64 is missing or has the wrong type: {error}"))?;
    solve.call(&mut store, (input[0], input[1]))
        .map_err(|error| format!("Wasm solve trapped or exhausted fuel: {error}"))
}

fn engine() -> Engine {
    let mut config = Config::default();
    config.consume_fuel(true);
    config.enforced_limits(wasmi::EnforcedLimits::strict());
    Engine::new(&config)
}

fn validate_module(module: &Module) -> Result<(), String> {
    if module.imports().next().is_some() {
        return Err("Wasm imports are not permitted".to_owned());
    }
    let mut solve_found = false;
    for export in module.exports() {
        if export.name() == "solve" {
            match export.ty() {
                ExternType::Func(func)
                    if func.params() == [ValType::I64, ValType::I64]
                        && func.results() == [ValType::I64] => solve_found = true,
                ExternType::Func(_) => {
                    return Err("solve must have exact type (i64, i64) -> i64".to_owned());
                }
                _ => return Err("solve export must be a function".to_owned()),
            }
        }
        match export.ty() {
            ExternType::Memory(memory) => {
                let init_large = memory.initial_pages().to_bytes()
                    .map_or(true, |size| size > MAX_MEMORY_BYTES);
                let max_large = memory.maximum_pages().map_or(true, |pages| {
                    pages.to_bytes().map_or(true, |size| size > MAX_MEMORY_BYTES)
                });
                if init_large || max_large {
                    return Err("memory must declare a maximum within the sandbox limit".to_owned());
                }
            }
            ExternType::Table(table) => {
                if table.minimum() > MAX_TABLE_ELEMENTS
                    || table.maximum().map_or(true, |size| size > MAX_TABLE_ELEMENTS)
                {
                    return Err("table must declare a maximum within the sandbox limit".to_owned());
                }
            }
            _ => {}
        }
    }
    if !solve_found {
        return Err("required export solve(i64, i64) -> i64 is missing".to_owned());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_imports() {
        let wat = r#"(module (import "env" "f" (func)) (func (export "solve") (param i64 i64) (result i64) i64.const 0))"#;
        assert!(compile(wat).unwrap_err().contains("imports"));
    }

    #[test]
    fn infinite_loop_is_stopped_by_fuel() {
        let wat = r#"(module (func (export "solve") (param i64 i64) (result i64) (loop $again br $again) i64.const 0))"#;
        let bytes = compile(wat).unwrap();
        assert!(evaluate(&bytes, [0, 0], 100).unwrap_err().contains("fuel"));
    }

    #[test]
    fn rejects_memory_overflow_declaration() {
        let wat = r#"(module (memory (export "memory") 3 3) (func (export "solve") (param i64 i64) (result i64) i64.const 0))"#;
        assert!(compile(wat).unwrap_err().contains("memory"));
    }

    #[test]
    fn computes_signed_i64_arithmetic() {
        let wat = r#"(module (func (export "solve") (param $a i64) (param $b i64) (result i64) local.get $a local.get $b i64.add))"#;
        let bytes = compile(wat).unwrap();
        assert_eq!(evaluate(&bytes, [-7, 3], 100), Ok(-4));
    }

    #[test]
    fn unexported_memory_is_still_bounded() {
        let source = r#"(module (memory 65535) (func (export "solve") (param i64 i64) (result i64) i64.const 0))"#;
        let bytes = compile(source).unwrap();
        assert!(evaluate(&bytes, [0, 0], 100).is_err());
    }

    #[test]
    fn start_function_cannot_escape_fuel_metering() {
        let source = r#"(module (func $start (loop $again br $again)) (start $start) (func (export "solve") (param i64 i64) (result i64) i64.const 0))"#;
        let bytes = compile(source).unwrap();
        assert!(evaluate(&bytes, [0, 0], 100).unwrap_err().contains("fuel"));
    }
}
