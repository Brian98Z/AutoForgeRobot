# AutoForge：双线受控自迭代原型

基于《Rust与Julia双线自主迭代系统架构调研》，落实报告的 **v0 离线受控自治**，不是 v2 生产自主发布系统。

## 当前能做什么

给出目标文字、公开样例、保留验收样例后，系统自动执行：

1. 冻结任务快照和当前基线。
2. Python 和 Julia 分别提出 WebAssembly 候选。
3. Rust 编译候选，在无宿主导入的 WASM 内执行；公开样例、保留样例、同 ID 历史回归全部必须通过。
4. 将失败的公开用例反馈给对应提案器，进行下一轮修订。保留集的内容和失败详情不发给提案器。
5. 两线同一轮均通过后，重新执行候选、检测故意错误候选、重放旧版本，并在独立本地日志中演练提交和回滚。
6. Ed25519 签名证据，追加发布记录；失败或未知不改变当前构件。
7. 用户随时停止；恢复必须显式执行 `resume`。

**实际演示是程序的变化**：加法目标生成加法程序；改成绝对差目标时，旧程序被拒绝，下一轮生成新程序。不是只修改目标描述、跑固定成功脚本或让模型自报通过。

### 当前输入/输出范围

候选 ABI 固定为 `solve(i64, i64) -> i64`，整数遵循 WASM 64 位回绕语义。离线提案器支持加减乘、最小/最大、回绕差的绝对值、有界仿射函数及常量限幅。它根据样例求解，不按任务名称匹配模板。

这是一台可运行的**迭代控制原型机**，还不是能生成任意 Web、数据库、桌面或业务系统的通用开发代理。未来扩展应在这个闭环上增加受限应用 ABI、构建器、可信验收器及能力代理，而不是给模型开放宿主 shell。

## 快速使用（Windows PowerShell）

在本目录打开终端。当前机器已提供 `target/release/autoforge.exe`、项目内 Julia 便携运行时及 Julia 依赖锁文件。

本机的 `state` 已完成“加法→绝对差”双线迭代，可直接执行 `execute state -7 3` 得到 `10`，或 `replay state` 核验两个版本。下面完整教学流程请使用新的状态目录，例如把所有 `state` 改为 `my-state`；不要重复初始化已有 `state`。

```powershell
.\autoforge.cmd init state
.\autoforge.cmd evolve state examples\add.json
.\autoforge.cmd execute state -7 3
# output: -4

.\autoforge.cmd evolve state examples\absdiff.json
.\autoforge.cmd execute state -7 3
# output: 10；旧候选第一轮不通过，新候选第二轮晋级

.\autoforge.cmd replay state
.\autoforge.cmd rollback state
.\autoforge.cmd execute state -7 3
# output: -4

.\autoforge.cmd kill state
.\autoforge.cmd status state
.\autoforge.cmd resume state
```

`init` 在同目录暂存完整状态后原子发布，不覆盖已有目录。`recover STATE` 显式恢复可识别的旧版半初始化状态、保留密钥身份，并隔离无已签名证据引用的损坏blob；有引用的损坏不自动替换。`rollback` 恢复前一版本，首版可回退至无活动版本；已经无活动版本时幂等返回。`HOLD` 退出码2，错误或停止退出码1，成功退出码0。`resume` 先提交签名恢复授权，再清除STOP；失败后检查status，保持停止时必须显式重试resume，不自动清除新的停止标志。`kill` 使用独立短事务锁，WASM当前调用靠fuel有界结束，不宣称操作系统级瞬时抢占。

直接调用 exe 时，请从项目目录运行，或设置 `AUTOFORGE_PROJECT`。`autoforge.cmd` 自动设置目录。可用 `AUTOFORGE_PYTHON`、`AUTOFORGE_JULIA` 指定解释器路径。

### 工程快照与隔离恢复

`rollback` 只切换构件；整个工程使用 `recovery.py` 快照。快照包含源码、控制器、状态（包括签名私钥）、Python标准库运行时、Julia/Lean运行时、实际Julia依赖、Rust工具链与Cargo锁定依赖缓存。**按私密备份保管，不上传公开仓库；manifest摘要另行可信保存。** 不包含操作系统、Windows SDK/MSVC链接器、外部服务或第三方凭据。

```powershell
python recovery.py snapshot --state state --output F:\OMP\temp\autoforge-snapshot
# 记录输出的manifest_sha256，以下以实际摘要替换SHA256
python recovery.py verify F:\OMP\temp\autoforge-snapshot --sha256 SHA256
python recovery.py restore F:\OMP\temp\autoforge-snapshot --sha256 SHA256 --output F:\OMP\temp\autoforge-restored
python recovery.py run F:\OMP\temp\autoforge-restored replay state
python recovery.py run F:\OMP\temp\autoforge-restored execute state -7 3
```

还原仅允许不存在的新目录，全部哈希校验通过后才发布，失败不覆盖当前工程；运行入口绑定快照内Python、Julia与独立depot。此功能不撤销外部业务副作用，不是OS镜像或硬件断电认证。文件fsync和平台发布接口改善持久化顺序，提交后的清理失败单独警告；源工程内容变动会中止快照。状态复制期间持有控制/提交锁，运行时复制阶段不锁状态。

快照前先运行 `cargo fetch --locked` 补齐锁文件涉及的所有平台依赖缓存；不修改锁文件。仅私钥已存在的半初始化可派生相同公钥；仅公钥存在而私钥丢失时拒绝伪造身份，必须从私密备份恢复或另建状态。`recover` 不自动解除STOP。

原子性修复验收：`python transaction_check.py` 覆盖9项真实CLI故障恢复；汇总证据 `bootstrap-runs/atomicity-fix-verification.json`。本次27次Rust测试、15项原有闭环、256次跨语言边界执行通过；checkpoint 17项通过、真实symlink用例因Windows特权不足未执行。完整快照26642文件/5307210505字节，已从新目录使用随包Python/Julia/depot提案发布、重放旧构件、验证Lean，并使用随包Rust/Cargo缓存离线重新编译（链接器仍来自宿主）。摘要和实际目录见汇总证据。

## 提交用户的新目标

复制 examples 内的契约，修改以下字段，不修改控制面：

```json
{
  "schema": 1,
  "id": "pricing-rule",
  "goal": "输入单价 x 与数量 y，返回 x*y，64 位整数回绕",
  "public_cases": [
    {"input": [0, 10], "expected": 0},
    {"input": [3, 4], "expected": 12},
    {"input": [-2, 5], "expected": -10}
  ],
  "holdout_cases": [
    {"input": [19, 7], "expected": 133},
    {"input": [-13, -3], "expected": 39}
  ],
  "max_rounds": 3
}
```

- **同一个 id** 表示同一任务的兼容演化：该 ID 的全部历史已发布样例保留为回归义务。
- **新 id** 表示用户明确切换新任务：旧实现作为起点，但不要求新任务保留旧任务行为；旧版本仍可回滚。
- 目标文字是上下文，不是自动授权；当前机器验收依据是整数输入/输出样例。
- 单个契约最多 256 个公开＋保留样例，至少各 1 个；最多 8 轮；每次提案最多 90 秒、输出 128 KiB；每个 WASM 用例 100,000 fuel。
- 矛盾样例直接 `Ambiguous`；不支持的任务不得假造结果，将停在 `HOLD`。
- 保留集只能提供有限覆盖，不能证明完整自然语言语义。应由用户或独立领域验收器编写，不交给候选作者自行决定。

## 可选模型提案

默认全离线，不读取本机其他项目密钥，不发生模型计费。Python 提案器实现 OpenAI-compatible `/chat/completions`：

```powershell
$env:AUTOFORGE_PROVIDER = 'llm'
$env:AUTOFORGE_API_BASE = 'https://your-provider.example/v1'
$env:AUTOFORGE_MODEL = 'your-model'
$env:AUTOFORGE_API_KEY = 'your-key'
.\autoforge.cmd evolve state your-task.json
```

启用后会把目标、公开样例、当前 WAT 和公开失败反馈发往指定模型，可能计费；保留集不包含在请求中。当前 Julia 仍为独立离线合成审计线，所以模型更强并不会自动扩大双线允许范围；只有两线都能通过的任务才发布。API 适配未进行真实付费模型验收，不将离线通过冒充在线通过。

## 文件与信任边界

|路径|职责|
|---|---|
|`src/main.rs`|目标契约、状态控制、进程预算、独立验收、签名和本地版本路由|
|`src/sandbox.rs`|无 imports 的 WASM、ABI 校验、fuel、内存及表资源限制|
|`providers/python_agent.py`|样例搜索；可选模型提案，仅输出数据|
|`providers/julia_agent.jl`|独立表达式树、求值器和 WAT 渲染器|
|`providers/Manifest.toml`|Julia 依赖锁定|
|`examples/`|成功目标、新目标、矛盾目标|
|`acceptance.py`|实际 CLI＋Python＋Julia 端到端验收|
|`state/blobs/`|SHA-256 内容寻址的任务、WAT、WASM、签名证据、回滚演练|
|`state/ledger/`|按序号追加、带前项哈希的签名日志，最后路由事件决定当前版本|
|`state/STOP`|用户停止锁存，不由候选解除|
|`state/private.key`、`public.key`|本机原型签名身份，不提交到源码仓库|

发布仅指**本地离线构件指针切换**，没有生产流量、数据库迁移、外部工具写入或真实金丝雀。没有 Web 面板和后台自启动；每次 `evolve` 是有界迭代会话。

### 必须保留的限制

- 不可信部分只有生成的 WASM。固定 Python/Julia 提案器、Rust、WAT 编译器和操作系统属于可信计算基；Python/Julia 子进程不是安全沙箱，不接受模型生成脚本在宿主执行。
- 两线运行在同一 OS 账户；保留集仅做协议隔离，不是对恶意宿主提案器的文件权限隔离。签名密钥同样不抵抗该账户或管理员替换密钥与全部历史。
- 日志哈希链可检测篡改与中间缺项，但没有外部锚定，不能证明尾部没有被管理员截断。
- 两种语言及两种表达路径不等于独立团队、独立 oracle 或完整 Julia 生命周期控制面；两线共享 Rust 验收器，仍有共因风险。
- WASM 内存限制为每个实例 128 KiB，表 1024 项，输入/构件有长度限制；不构成整进程 RSS、磁盘、CPU 时长或编译器漏洞的硬隔离。
- 运行时不会让候选读取文件、联网、起进程或修改 charter；但尚无硬件/带外 kill、远端签名根、OS 分账户、隐藏集服务、长期漂移测量及生产灰度门。
- 证据记录代码/锁文件/模型配置等哈希，但不是七树完整环境镜像、供应链认证或 SLSA 认证。
- 依赖 JSON3 当前注册表标记 deprecated；本原型固定在 Manifest 版本，后续独立升级需重跑大整数边界与协议验收。

因此不要把此原型接入生产凭据、付款权限或真实外部发布链路。

## 构建与验证

```powershell
cargo build --release --locked
.\runtime\julia-1.13.3\bin\julia.exe --startup-file=no --project=providers -e "using Pkg; Pkg.instantiate()"
python acceptance.py
cargo test --locked
```

Julia 便携发行版来自官方：
https://julialang-s3.julialang.org/bin/winnt/x64/1.13/julia-1.13.3-win64.zip

官方 SHA-256（已核对）：
`1fae8fc95e6199b3d8f6d7a3791810e45e1b3c480eb4b9f5bb211789badfc729`

验收结果写入 `acceptance-result.json`。`runtime/` 和 `target/` 不纳入源码版本控制；只复制源码时需要重建/安装运行时，或使用上文完整工程快照恢复。普通运行的Julia第三方包来自用户depot；`recovery.py run` 使用快照独立depot。

## 在此基础上扩展“各类系统”

扩展顺序必须受验收约束：

1. 先定义目标系统的结构化输入、持久状态、输出和失败语义，增加新任务类型，而不是放宽现有 i64 契约。
2. 为该类型增加隔离构建器与执行适配器；运行真实源码前必须建立 OS/容器沙箱、资源配额及默认禁网。
3. 把文件、数据库、HTTP 等副作用做成显式 capability 服务；候选不能直接拿系统账户或万能 token。
4. 增加独立领域验收、隐藏集服务、持久数据迁移/回滚和漂移检测；先影子运行，未知保持 HOLD。
5. 两条提案线在同契约下独立扩展。控制面、停止开关、签名根和验收器不能由候选自修改或自批准。

这些是后续接入点与必要条件，不是本次已经实现的能力。

## 本次验证记录

- `python acceptance.py`：15 项实际双运行时/CLI 场景通过，报告 `acceptance-result.json`。
- `cargo test --locked`：6 个 WASM 边界测试通过。
- 最终 release 构建成功；演示状态两个版本重放通过，`execute state -7 3` 返回 10。
- `cargo fmt` 未执行成功：本机 rustup 的 stable toolchain manifest 缺失；不影响已经成功的 rustc/cargo 构建与运行。未自动重装用户工具链。

## 多轮自举与扩展验证

完整判定口径、限制和测试说明见 `BOOTSTRAP.md`。本机的有限 fuel、内存和 i64 ABI 不具备严格无界图灵完备性；多轮测试不能证明未来任意系统都可生成。

```powershell
python bootstrap.py
python extension_test.py
.\runtime\julia-1.13.3\bin\julia.exe --startup-file=no --project=providers tests\protocol.jl
cargo test --locked
```

`bootstrap.py` 连续执行 12 个不同目标，在 `bootstrap-runs/` 保留独立状态与逐命令证据；不修改演示 `state/`。`extension_test.py` 通过现有项目协议替换双线提案器，验证 XOR 语法扩展，无需重新编译控制器。测试扩展由操作者编写，不算自主发明能力。

加强的 i64 极值样例发现了当前 JSON3/Parsers 组合的负整数解码错误。`providers/protocol.jl` 使用原始十进制 token 加 `parse(Int64, ...)`，避免精度/符号损失；解码器和 Julia Manifest 哈希现纳入新版本证据。已有签名构件仍按原始证据重放，不重写历史。

多语言兼容性复跑：`python multilingual_test.py`。在独立UTF-8路径状态中，验证Rust控制器→Python/Julia独立提案→WASM执行，覆盖加法/乘法/最小值/最大值、每种64组i64边界输入（含超过2^53的精确整数）、四次发布重放及非法输入拒绝。证据保存在 `bootstrap-runs/multilingual-*/report.json`。

严格数值协议差异已修复：Python入口在复用候选或调用模型前，验证schema/round、公开样例及反馈中的input/expected；仅接受Int64范围内的JSON整数，拒绝bool、浮点/指数形式、数字字符串和越界整数。运行时WASM算术仍允许64位回绕，两者不混淆。`multilingual-e05cd4b8068a` 实测72次非法字段请求由两线全部拒绝，256次合法边界执行通过。报告仅对已测输入给出兼容结论，不宣称所有JSON结构、其他操作系统/运行时或LLM API均等价。旧快照包含修复前版本，若需恢复此修复应重新生成快照。

双线与反馈有效性复验：`bootstrap-runs/20261030-144752-86689494/report.json` 连续12个目标通过，后11个目标两线均先拒绝旧候选再于第二轮修正；隐藏集、历史回归与不支持的XOR均可否决。`extension-1ed322785832/report.json` 验证单线缺失/错误ABI不能靠另一线成功而发布。`feedback-ablation-1805bbb5f9e7/report.json` 固定公开样例与轮数，只增加一条反例，两线候选均从加法改为乘法，实际WASM输入[3,4]由7改为12。这是固定合成器的反馈搜索证据，不是模型权重训练、通用自主学习或统计泛化证明；双线不是按耗时选冠军，要求同轮两线都通过独立验收。

## 外部迭代参数与检查点

`iterate.py` 将总任务轮数与单任务提案预算分开，默认循环12种内置目标，每次使用唯一任务ID；可用 `--tasks tasks.json` 传入非空任务契约数组或任务文件路径数组（路径相对启动目录）。`--rounds 1000` 是1000个目标事务，单目标 `--proposal-rounds` 仍限1..8，不解除Rust预算。`--checkpoint-every` 控制汇总检查点间隔，每次完成仍落不可变逐轮收据；异常、暂停和结束也写检查点。

```powershell
# 只检查计划，不调用提案器、不创建输出目录
python iterate.py --rounds 1000 --checkpoint-every 10 --output bootstrap-runs\long-run --plan-only
# 实际启动；不加max-new-rounds则运行到1000轮或遇到失败/停止
python iterate.py --rounds 1000 --checkpoint-every 10 --output bootstrap-runs\long-run --max-new-rounds 2
# 使用完全相同的冻结参数恢复，max-new-rounds仅限制本次新增轮数
python iterate.py --rounds 1000 --checkpoint-every 10 --resume bootstrap-runs\long-run --max-new-rounds 2
```

恢复校验配置、任务计划、控制器/提案器/运行器摘要、公钥根、检查点链和逐轮收据；以Rust签名日志核对发布，避免晋级后进度未写造成重复发布。配置或源码变化拒绝恢复，应新建运行目录。已停止的状态必须由操作者显式 `autoforge resume STATE`，运行器不自动解除STOP。收据哈希用于检测误改，不抵抗同账户攻击者重写所有本地证据；没有远程可信锚。

实测：`bootstrap-runs/iteration-check-latest.json` 4轮分2+2恢复通过、检查点误改和配置变化拒绝；1000轮仅做参数/计划校验。`iteration-boundaries-9603785b5f/report.json` 验证稀疏检查点丢失后的逐轮恢复及已晋级未记录任务重建，最终恰好4次发布。长期1000轮性能/稳定性未实测，逐轮重放历史会随历史增大产生额外开销。

新增实测：`bootstrap-runs/endurance-4df52271691d/report.json` 在同一累计状态完成100个真实双线目标，分25/25/25/25轮运行并跨进程恢复3次，总计568.344秒，最终100个签名版本重放通过。四段耗时133.781/142.297/145.828/146.219秒，含该机当时负载，不据此线性外推1000轮稳定性。1000轮仍仅参数支持，未执行完整1000轮；硬件断电、恶意同账户攻击、模型权重训练仍不在证明范围。

## 外部拟合与回归回报

`--evaluator-command` 接收JSON字符串数组，不通过shell执行；评估器是操作者信任的宿主程序，不是候选沙箱。`--evaluation-timeout` 默认180秒，超时、停止、非零退出、超过1MiB stdout/stderr、无效/不匹配回报均失败关闭。命令、可识别的脚本/数据文件参数内容摘要、超时冻结进运行配置；间接读取文件/远端服务版本须由操作者自行固定。

内置参考 `external_fit.py` 对每个样例实际调用控制器执行候选，返回MAE、最大误差、精确率、回归失败数；`--max-mae`默认0，回归要求全对。拟合和回归数据文件均为1..256条 `{"input":[1,2],"expected":3}` 的JSON数组，值严格为i64。不把保留回归答案送给提案器。

```powershell
$evaluator = '["python","external_fit.py","--fit","fit.json","--regression","regression.json","--max-mae","0"]'
python iterate.py --rounds 1000 --checkpoint-every 10 --tasks tasks.json --evaluator-command $evaluator --evaluation-timeout 60 --output bootstrap-runs\evaluated
```

任务不同通常需要不同oracle/数据集；参考评估器读取固定数据，不能把加法数据用于默认循环的减法目标。需要按目标调度时实现自己的评估程序。

评估程序从stdin读取请求：`schema=1,request_id,task_sha256,release_id,wasm_sha256,evaluator_id,task,candidate,execution`。task仅含id/goal/public_cases；candidate含wat/wasm_path；execution含controller/state供实际执行。stdout必须只有一个JSON对象，字段严格为：原样回传六项身份字段 `schema,request_id,task_sha256,release_id,wasm_sha256,evaluator_id`，再加 `metrics`（1..64个有限数值）、`fit_pass`/`regression_pass`（严格bool）、`feedback`（最多256条公开input/expected，或空数组）。不得重用旧request_id，不允许NaN/重复键。诊断写stderr。

两门全通过才计为完成。默认 `--external-repairs 0` 仍只记录反馈并在否决后停止；显式配置1..8次修正预算后，只有拟合失败、回归通过且含新公开反例时，才允许回滚并修订公开任务继续提案。`external_fit.py --public-feedback` 显式授权输出失败拟合行，绝不输出回归行。与原holdout重叠、矛盾标签、非i64或超过256总样例的修订拒绝；原holdout不变。修订使用唯一ID和不可变修正链，恢复时重验评估器、收据、有效任务与原任务关系。不训练权重，不替代内部双线/隐藏集验收。外部门仍在本地晋级之后，STOP阻止回滚时保留停止；不是生产发布前原子门，也不承诺撤销任意外部副作用。

收据位于运行目录 `evaluations/`，逐轮结果 `rounds/`，汇总检查点 `checkpoints/`。恢复未完成候选会重新评估，已完成收据验证摘要/身份后复用。实测 `external-check-55620fedecd8/report.json` 拟合通过、错误回归否决、过期身份/NaN/错误布尔类型拒绝、数据变化改变评估器身份、超时和STOP关闭；`iteration-boundaries-9603785b5f/report.json` 外部否决后完成计数0、活动版本null。

公开反例自动修正示例：

```powershell
$evaluator = '["python","external_fit.py","--fit","fit.json","--regression","regression.json","--public-feedback"]'
python iterate.py --rounds 10 --tasks tasks.json --external-repairs 2 --evaluator-command $evaluator --output bootstrap-runs\repair-run
python repair_check.py
python protocol_check.py
python endurance_check.py --rounds 100 --segment 25
```

`repair_protocol.py` 校验反例并生成纯控制器契约；关联信息留在 `repairs/`，不向Rust契约塞入未知字段。`repair_check-4eeb9249e5ac/report.json` 实测加法→乘法自动修正、完成后重放、零预算否决、保留集/矛盾标签拒绝和修正意图提交后中断恢复。严格协议24项语料/48次真实提案器调用通过，Julia另有51项数值/结构断言通过；证据 `protocol-evidence/22809b90899840f2ad62a22bdfc6bb1e.json`（位于bootstrap-runs）。测试覆盖完整单文档、重复键（含转义等价键）、未知字段、合法Unicode/转义、非法UTF8和精确i64实际值反馈；不把有限语料通过冒称所有解析器性质的形式化证明。汇总见 `bootstrap-runs/advancement-91c8fb0d284d/report.json`。

```powershell
python iteration_check.py
python external_check.py
```

## 自主闭环审计与拓展测试

当前自主范围：操作者提供可验证目标、预算和评估标准后，无逐轮人工批准地完成双线提案、验证、候选修正、外部评估、收据提交和检查点恢复。失败停止、条件回滚、操作员STOP保留。不是任意系统生成，不自动训练模型权重，也不自动发明验收标准。1000轮支持参数化，但完整长期运行尚未认证。

审计修复：
- 完成收据在控制/提交锁内检查活动候选、签名前驱关系、STOP、源码和评估器摘要；外部评估返回PASS但候选已变化不能假完成。
- `autoforge rollback STATE EXPECTED_RELEASE` 在Rust控制锁内比较预期活动版本；运行器用此条件式接口，不回滚并发晋级的其他候选。
- 收据是完成提交点；提交后KeyboardInterrupt不会再补偿回滚，恢复从持久收据继续。
- 验证晋级轮的双线PASS，不误拒绝某线先通过、另一线后通过的合法历史。
- `run-intent.json` 在初始化前固定参数；Rust状态初始化成功而frozen-config未写的窗口可恢复且保留公钥身份。
- 隐藏集否决仅反馈候选源码排除列表 `rejected_wats`，不反馈隐藏输入/答案；两线跳过已否决候选，已通过一线继续参与同轮验收。固定搜索空间耗尽仍HOLD，不宣称总能找到正确程序。
- 评估器身份覆盖 `--fit=path` 等等号文件参数；执行结束再核对身份及STOP。超时/STOP使用Windows taskkill进程树、POSIX进程组终止；不是恶意进程逃逸隔离。指标巨大整数规范化拒绝，不泄漏未分类OverflowError。

复跑 `python audit_check.py`。证据 `bootstrap-runs/audit-check-12eff766d561/report.json` 包含隐藏否决继续搜索、非同步PASS历史、条件回滚、提交后中断及初始化窗口。`audit-expansion-fa06b742172e/before.json` 与 `after.json` 保留活动候选变化导致假完成的失败/修复证据。收据哈希不是对同账户恶意改写的认证；外部评估仍在本地晋级之后，不是生产事务。

## 通用计算模型与构造性证明

新增独立组件 `universal/`：任意有限图灵机描述作为输入，固定双栈解释器支持变长带状态、预算暂停、检查点恢复和停止文件。操作说明 `universal/README.md`，完整模拟证明及未证义务 `universal/PROOF.md`。

```powershell
python universal\verify.py
```

图灵完备性命题针对**无界可扩展存储、无全局步数限制的抽象模型**；代码运行保留预算。新增Lean4.35.0内核检查已验证9个核心模型定理，尚非整个通用性结论或Python全链路认证。该组件不自动进入原WASM发布链路，不改变现有安全核心，也不证明原提案器已能自主生成任意系统。

```powershell
python universal\kernel_check.py
python universal\benchmark.py --repeats 11
```

基准规则、原始指标和正确性失败归零机制见 `universal/BENCHMARK.md`；内核范围见 `universal/formal/README.md`。分数是工程目标达标度，不是图灵完备性或智能评分。

## 参考资源与 REA 最小机制集成

目标是建立可调用、可组合、可验证的能力，不保证每个任务结果成功。新增 `references.py` 提供显式本地资源的不可变快照、SHA-256来源绑定、关键词检索和引用。普通源码不反编译；REA产物按原始Evidence JSON导入，保留evidence_id、authority、confidence和limitations，不把反编译文本当原源码、不将静态观察当运行证明。包中观察/推断/未知分开存储；未建立自动推理或未知项闭环。哈希检测误改，不证明上游真实性、版权许可或抵抗同账户攻击。

```powershell
python references.py ingest --source owned-source --store reference-store --origin "自己的参考项目" --license "操作者声明：自有代码" --kind source
python references.py ingest --source exported-rea.json --store reference-store --origin "REA静态分析导出" --license "操作者确认可用" --kind rea-evidence
python references.py query --store reference-store --query "库存 transaction"
python references.py export --store reference-store --id PACKAGE_SHA256 --output reference-export.json
python references.py attach --store reference-store --id PACKAGE_SHA256 --file example.wat --task task.json --output task-with-reference.json
.\autoforge.cmd evolve state task-with-reference.json
```

`attach`不覆盖原任务。每任务最多4条引用，每条32KiB；引用内容及来源进入Rust签名任务快照。Python/Julia离线线可将WAT作为候选，经原有公共/隐藏/历史回归门验证；失败候选进入拒绝列表，不因参考来源获得豁免。普通源码和REA证据是上下文，不会被离线表达式搜索器翻译为任意程序。Python可选LLM线会发送引用内容到配置模型，启用前须确认内容可外发；未做在线付费验收。

来源目录最多128个支持的UTF-8文件、总计2MiB、单文件256KiB；敏感文件名、链接/重解析点被拒绝。文件名过滤不是内容级秘密检测，操作者仍须只指定可共享资源。不会自动扫描整机、抓仓库、执行参考代码或授予网络/文件能力。

实测 `bootstrap-runs/reference-e2e-a30294bb3646/report.json`：参考XOR WAT经真实双语言提案通过，执行 `123 XOR 77` 得54，签名重放通过；篡改摘要拒绝、错误参考HOLD。实际REA导出见 `bootstrap-runs/rea-controller-evidence.json`，已入库并可检索。此证据是本机控制器PE资源静态观察，不是业务应用重建成功证明。

新增协议回归 `bootstrap-runs/protocol-evidence/bcbd61c14f10495da86730c1bd5fcf40.json`：29项语料/58次真实提案器调用通过，包含引用摘要、数量、类型和未知字段拒绝。错误参考还实测在第2轮由两线独立修正成功。原15项生命周期与外部反例修正/恢复回归通过；Julia51项断言通过。

## 多文件应用能力通道

`applications.py` 与原Rust/WASM发布线分离：接收需求和参考包、通过操作者提供的提案器生成多文件候选、固定候选摘要、调用可信外部构建/检查适配器、将失败反馈送回下一轮，保留每轮文件与报告。最多8轮；候选只写入新目录，不覆盖现有工程。请求为 `{schema:1,goal:"…",references:["包SHA256"]}`；候选为 `{schema:1,goal:"…",references:["包SHA256"],files:[{path:"app.py",content:"…"}]}`。参考内容只能来自验证通过的本地包。

```powershell
python applications.py stage --candidate candidate.json --output candidate-workspace
python applications.py cycle --request request.json --reference-store reference-store --proposer-command '["python","my_proposer.py"]' --builder-command '["python","my_isolated_builder.py"]' --trusted-adapter --rounds 3 --output application-run
python application_check.py
```

上述 `my_*.py` 是自定义适配器示例名；现在也提供内置 `application_proposer.py`（模型提案）与 `application_builder.py`（容器业务验收），见下节。提案器stdin收到需求、已核验参考内容、上轮报告和上一版候选源码，stdout返回候选JSON；构建器stdin收到 `workspace_manifest` 与 `candidate_sha256`，stdout必须返回 `{schema:1,candidate_sha256:"…",build_pass:true,test_pass:true,observations:[],limitations:[]}`。`--trusted-adapter`只表示明确授权运行操作者代码，不创建沙箱、不证明隔离有效。候选文件不能作为适配器命令直接执行。

构建器负责其后代进程的完整结束；本地截止时间和停止处理不构成恶意进程隔离。通过只表示声明范围内的外部适配器检查通过，不产生原Rust控制器的签名发布，不自动部署、迁移业务数据库或执行公网副作用。当前检查器用真实Python AST解析演练两轮语法修正；未验证应用运行、业务功能、依赖安装或生产环境。

能力状态：参考学习、固定多文件候选、基于上一版源码与反馈的修正已有可执行路径；模型HTTP与容器业务检查适配器已提供，但真实模型和容器运行须分别满足配置并验收。API/UI领域验收、部署及持续运行观测仍未接入。本机探测未发现docker/podman，WSL未安装可用Linux环境；不会以宿主裸跑绕过这一前置条件。“学习”指可追溯资源复用与反馈修正，不是权重训练或所有业务结果保证。

应用通道验收：`python application_check.py` 真实启动提案/检查子进程，两轮语法纠正通过，异候选摘要报告HOLD。`bootstrap-runs/reference-e2e-a30294bb3646/application-boundaries.json` 记录路径穿越、协议目录覆盖、大小写冲突、Windows保留名、总量超限、构建器改写源码、进程输出超限及STOP中断的拒绝证据。这些验证的是能力接口和失败边界，不是业务正确性或生产发布能力。

### 内置多文件模型提案器

`application_proposer.py` 调用显式配置的OpenAI-compatible `/chat/completions`，返回完整候选文件，不执行它们。环境变量沿用 `AUTOFORGE_API_BASE`、`AUTOFORGE_MODEL`、`AUTOFORGE_API_KEY`；未设置时拒绝，不寻找其他项目凭据。非loopback端点须HTTPS及密钥；本机loopback可无密钥。禁止HTTP重定向，检查输入/响应大小、完整JSON、重复键、截断/refusal、文件安全及目标/引用一致性。

```powershell
python application_proposer.py --check-config
python proposer_check.py
```

配置检查不调用模型、不打印密钥。启用真实端点后，用户需求、明确引用的内容、上一版源码和公开失败回报会发送给模型，可能计费；独立业务验收答案不发送。当前实测为本地HTTP协议模拟器驱动真实客户端，含中文源码与失败拒绝；不是在线模型质量或生成成功证明。`bootstrap-runs/app-adapters-768a45e0bdb6/cycle/report.json` 记录真实HTTP适配器→运行器→AST检查的两轮反馈修正，模型端为协议模拟器、执行门为语法检查。

### 内置容器业务验收器

`application_builder.py` 支持Docker/Podman的Linux服务，要求**已在本机引擎存在的精确 `sha256:<64hex>` 镜像ID**（包含Python标准库）。不拉取镜像、不安装依赖、不执行候选Dockerfile。固定 `main.py` 入口，stdin/stdout为JSON-lines；可导入同目录其他模块。运行设置为禁网、只读根和源码挂载、非root UID65534、移除capabilities、no-new-privileges、CPU/内存/PID配额及临时 `/tmp`。镜像本身及其中环境由操作者信任；容器不是内核漏洞防护证明，控制器强杀/断电后的清理也未认证。

业务契约在候选目录之外：`fixtures/business/contract.json`；公开需求另见 `fixtures/business/request.json`。host只向候选发送请求，不挂载答案；精确比较返回JSON，错误回报不包含预期答案。两个场景共13次请求，覆盖SQLite库存添加、查询、同SKU更新、排序、零值、负数/布尔/整数越界/空SKU/未知操作拒绝及拒绝后状态不变；整数范围明确为0到Int64最大值。尚未在容器实跑，不能宣称SQLite业务能力已验收。

```powershell
# $image 设为操作者已审核的本地Python镜像ID，不能用浮动tag
python application_builder.py --engine docker --image $image --probe
python business_check.py --engine docker --image $image
$proposer = '["python","application_proposer.py"]'
$builder = ConvertTo-Json -Compress -InputObject @('python','application_builder.py','--engine','docker','--image',$image,'--contract','fixtures/business/contract.json')
python applications.py cycle --request fixtures/business/request.json --reference-store reference-store --proposer-command $proposer --builder-command $builder --trusted-adapter --rounds 3 --output application-run-live
```

`python business_check.py` 在没有引擎/镜像时仍检查契约、中文目标的真实staging摘要与篡改拒绝，随后明确输出 `BLOCKED / NOT_TESTED`；退出0只表示前置静态检查完成，不代表业务运行通过。模型配置缺失同样不生成候选。验收汇总 `bootstrap-runs/app-adapters-768a45e0bdb6/verification.json`：协议与修正通过，真实模型生成、容器业务运行未测。不同于此前只接外部接口，本次提供了实际实现；执行能力仍须配置后实测，不等于已经覆盖几乎所有应用。

`business_check.py` 另以替换容器传输的方式检查host判定器：失败不可被后续成功覆盖、类型/数量不符、重复键/非有限JSON、请求总量超限、STOP和答案不泄露。该部分标记 `MOCKED_TRANSPORT_NOT_REAL_RUNTIME`，与真实容器业务运行严格分开。

### 应用构建暂停与恢复

`applications.py cycle` 新增 `--max-new-rounds N` 和 `--resume`。总预算仍为 `--rounds 1..8`；前者仅限制本次进程新提交的轮数，不重置总预算。需求、参考快照、总预算、适配器命令、可识别的文件参数/解释器、运行器源码和模型endpoint/model配置固定；变化则拒绝恢复。API密钥不写入检查点，不承诺识别同名模型服务端行为或任意外部依赖变化。

```powershell
# 沿用同一需求、适配器与输出目录；第一轮提交后主动暂停
python applications.py cycle --request request.json --reference-store reference-store --proposer-command $proposer --builder-command $builder --trusted-adapter --rounds 3 --max-new-rounds 1 --output application-run
# 重新启动进程，核对检查点后继续剩余预算
python applications.py cycle --request request.json --reference-store reference-store --proposer-command $proposer --builder-command $builder --trusted-adapter --rounds 3 --resume --output application-run
python application_resume_check.py
```

`cycle-state.json` 是权威恢复记录；带SHA-256完整性包络，通过fsync+原子replace提交，目录锁阻止并发执行。成功后再次resume核对已有源码与证据，不重新调用提案器/构建器；暂停以HOLD退出码2表示。普通检查FAIL可继续修正；调用超时、中途STOP、进程强杀或无法绑定的外部报告保留inflight并拒绝自动重放，不能用删除标志规避；需操作者核实外部副作用并另开运行。旧版没有检查点的目录不能恢复。

恢复不是任意外部副作用exactly-once协议，也不是硬件断电/恶意同账户认证。适配器的隐藏依赖、远端服务状态、镜像内容由操作者约束；哈希只检测变化，不替代独立信任根。候选源码仍不在宿主执行，容器和真实模型前置条件仍适用。

本轮验收汇总：`bootstrap-runs/application-resume-verification.json`。`application_resume_check.py` 实际启动CLI进程，验证轮边界暂停后续跑、终态无重复调用、适配器/候选/请求/检查点变更拒绝、并发锁拒绝、调用中强杀后拒绝重放。检查器只解析生成源码，不执行它。`application_check.py`、`business_check.py` 的host判定部分、`proposer_check.py`、`reference_check.py` 联合通过；真实模型生成和容器业务运行仍未测。

### 最小工具调用Agent与统一入口（2026-10-30）

`agent_transport.py`通过共享HTTP传输接收原生function tool_calls；完整解析并校验后才派发。`agent_runtime.py`按模型选择串行执行list_files、read_file、write_file、read_reference、validate。新文件需null基线，改已有文件需匹配SHA-256；引用按需读取，写入保存candidate.json，验证产生不可变版本，只有绑定摘要的独立builder PASS可结束成功。events.jsonl记录模型/工具事件与外部调用意图；无shell、无隐藏答案工具。

`application_build.py check`只预检输入/参考/契约、模型配置及容器。`run`默认agent模式，必须显式授权模型与适配器；缺引擎时在模型调用前BLOCKED。旧完整候选循环可通过 `--mode proposal` 使用，其 `--resume` 不适用于Agent。

```powershell
python application_build.py check --request fixtures/business/request.json --contract fixtures/business/contract.json --reference-store reference-store --image $image
python application_build.py run --request fixtures/business/request.json --contract fixtures/business/contract.json --reference-store reference-store --image $image --output agent-run --allow-model --trusted-adapter --max-turns 12
python agent_runtime_check.py
python application_build_check.py
```

真实本地Nanbeige工具循环已验证：`bootstrap-runs/live-agent-ac5b92082c94/run/report.json`，6轮6工具调用，读取错误参考→写入→AST失败→读取版本→修复→AST通过；`live-agent-a0910621731d`为3轮新建语法通过。前者使用 `fixtures/business/ast_verifier.py` 独立解析源码，**没有执行生成代码，不证明业务正确或容器可用**。失败证据 `live-agent-ddb518a5e3b3`（超时）、`live-agent-aed834c3e53e`（陈旧编辑拒绝后截断HOLD）；最早 `live-agent-0a8757152a68` 被执行宿主中断，无终态报告，不自动恢复。

当前工具循环采用完整响应而非SSE流式；最多12模型轮、96工具调用、2MiB上下文，单HTTP读取超时60秒。停止阻止后续工具，但在途同步HTTP需等待返回/超时，并非立即网络取消。未提供工具级恢复、上下文压缩、steering/follow-up、远端RPC或费用硬预算。它是已验证的最小交互路径，不等同完整P0-A完成。测试用本地服务无需API密钥；环境变量只在验证进程设置，没有写入用户永久配置。
