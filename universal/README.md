# 通用计算验证组件

## 三个不同的结论

- 抽象双栈解释器：在任意有限程序描述、可扩展无界存储、无全局步数上限假设下，图灵完备的构造性证明见 `PROOF.md`。
- 实际 Python 实现：有预算、有停止和检查点；`verify.py` 检查其与独立稀疏带模型的有限一致性。不声称经过证明助手认证。
- 原 AutoForge：仍为受限 i64 WASM 候选迭代器；此组件不自动接入发布，不解除候选权限，不证明自主生成任意系统。

本次判定及实测计数见 `VERDICT.md`，证明复核记录见 `REVIEW.md`，12批次逐项证据见 `results/batch-summary.json`。

新增内核与基准：`python universal/kernel_check.py` 检查9个Lean形式模型定理；`python universal/benchmark.py --repeats 11` 运行正确性硬门与性能评分。前者范围见 `formal/README.md`，后者规则与实测见 `BENCHMARK.md`；不把模型认证扩大为Python实现认证。

下一轮联合边界测试：`python universal/resilience.py`，覆盖长带、100段恢复、并发checkpoint、运行中停止和Lean/评分故障注入。逐项结果与限制见 `RESILIENCE.md`。

局部优化对照：`optimization_check.py --baseline <受信任的旧machine.py> --repeats 21`，验证旧检查点兼容、双栈排序和复制隔离，并交替计时。冻结基线、复跑命令与实测收益见 `BENCHMARK.md` 的“下一轮局部优化与同进程对照”。

## 实际运行

从 AutoForge 项目目录，在 PowerShell 执行：

```powershell
New-Item -ItemType Directory universal\results\manual
python universal\machine.py run universal\examples\increment.json --input '["1","1","1"]' --steps 3 --cells 100 --output universal\results\manual\paused.json
python universal\machine.py resume universal\examples\increment.json universal\results\manual\paused.json --steps 20 --cells 100 --output universal\results\manual\halted.json
```

第一次是 PAUSED，第2次 HALTED，观察带为位置 −1 的1与位置0、1、2的0，即二进制111加1得到1000。头可以移动到负位置，不截断到0。

无限向右写1的机器：

```powershell
python universal\machine.py run universal\examples\march.json --input '[]' --steps 20 --cells 100 --output universal\results\manual\march.json
```

其结果为 PAUSED，不是 HALTED。此例由转换结构可看出永不进入停机状态；程序并未实现通用停机判定。

停止：加 `--stop-file universal\results\manual\STOP`，由操作者在该路径创建文件。执行每个转换前检查；发现后返回 STOPPED。停止文件不会自动删除；正在完成的单步与文件出现之间没有硬件级原子中断保证。检查点保留当前位置，可在操作者移除该文件后显式resume。

## 预算与恢复

- CLI 每次最多1,000,000转换，最多100,000存储槽；输入/输出检查点文件最多4MiB。
- `--steps 0` 只观察当前终止条件，不推进转换。
- HALTED：显式停机态；STUCK：无定义转换；PAUSED：本次步预算耗尽；RESOURCE_LIMIT：存储配额不够；STOPPED：操作者停止。
- RESOURCE_LIMIT 在越界转换提交前返回，不破坏之前状态。
- 输出路径必须不存在，绝不覆盖旧检查点；用临时文件＋同目录硬链接原子安装。文件系统不支持硬链接时明确失败。
- 校验和绑定机器定义与状态，仅防误改，不是身份签名。拥有文件写权限的人可以重算校验和；restore也不证明该状态一定从初始输入可达。
- 长符号可能先碰到检查点字节上限，即使单元数没越界。写入前拒绝不可再次读取的大文件；这不是硬性进程RSS配额。
- Python库API供测试与可信调用者使用，不是恶意宿主代码的沙箱。CLI之外的宿主进程仍属于可信计算基。

## 验证

```powershell
python universal\verify.py
```

证据写入 `universal/results/` 的独立目录；保存种子、计数、各维度结果、被测文件哈希及真实CLI输出。完整状态机协议见 `CONTRACT.md`。

## 扩展边界

机器的状态数、符号集和转换表属于数据；新增机器不修改解释器。原型可以据此验证变长输入、持久计算状态、暂停恢复和通用控制流。不过把它作为新候选类型接入AutoForge，仍需独立的目标契约、双线提案和仲裁接口；本组件不会自行替换现有安全核心。
