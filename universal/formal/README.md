# Lean证明内核检查

运行：`python universal/kernel_check.py`。

工具链固定Lean4.35.0，官方发行压缩包SHA256已校验，来源和哈希见toolchain.json。仅使用Lean内置库，不依赖mathlib下载。

## 形式化对象

Simulation.lean定义独立的：
- Source：以头为原点的无限函数带；位置为here/left n/right n。
- Target：有限左右列表和当前格，列表头为最近符号。
- 源带写入与位移：直接重定位位置函数，不调用目标执行器。
- 目标执行器：列表push/pop，空列表读取blank。
- 两边独立的转换查找与有界执行，关系保持用归纳证明。

Python列表末尾为最近符号，Lean列表头为最近符号，二者需取reverse对应。这里证明的是Lean中的模型关系，不是Python列表reverse、JSON解析、checkpoint、资源策略或OS执行的形式验证。相对坐标模型不含Python的绝对head元数据。

## 判定要求

- Lean正常退出，warningAsError启用。
- 不允许sorry/admit、自定义axiom、unsafe或native_decide绕过。
- 打印最终定理依赖；不得出现sorryAx。
- 错误命题(0:Nat)=1必须被同一检查器拒绝。
- 保存完整stdout/stderr、源文件/二进制SHA256和编译产物。

Lean标准逻辑公理如propext、Quot.sound可能因函数外延性和simp出现，须如实列出，不能称完全无公理。

整个通用机编码的可计算性、无界模拟的最终通用性结论及Python refinement仍分别属于PROOF.md的构造论证和有限实测范围；不能因局部/有限步模型定理通过便改称全栈完全认证。

## 本次内核结果

Lean4.35.0已通过9个定理：decode_write、decode_left、decode_right、decode_stay、one_step、step_simulation、initial_related、fuel_simulation、input_simulation。最终定理对任意Nat状态/符号、任意有限输入、任意转换函数δ及任意自然数fuel量化；不是只检查几个固定步数样例。

源/目标各自按自身状态与读到的符号查δ，分别运行runSource/runTarget，非人工同步挑选同一分支。转移函数任意性的模拟定理不证明任意δ可计算；有限表编码的有效性仍在PROOF.md构造论证中。

证据：`../results/kernel-887c58d426db/report.json`（相对本目录）。全部依赖为标准propext、Quot.sound，没有sorryAx。故意错误命题被拒绝；正式证明源禁用sorry/admit/custom axiom/unsafe/native_decide绕过。工具链压缩包SHA256与GitHub官方资产digest一致。
