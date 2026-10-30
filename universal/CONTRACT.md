# 通用计算模型与实现契约

本组件独立于原 i64 WASM 发布链路，不授予候选新权限。

## 抽象模型

输入为任意有限确定性单带双向图灵机 M=(Q,Gamma,blank,start,H,delta) 和任意有限输入串。delta 为非停机态与符号上的有限部分函数，值为(next,write,move)，move 属于 L/R/S。显式停机态无出边；未定义转换为 STUCK（有限停止但不是显式 HALTED）。

目标解释器配置为 (state,left,current,right,head,steps)，left/right 是有限符号序列，栈顶在末尾，分别代表头左/右最近的符号。栈外全为 blank。抽象语义对栈长度、转换步数和整数 head/steps 不设上限。每一步先写 current，然后按 L/R/S 更新两栈及头；不执行用户代码。

## 文件格式及Python接口（实现者/验证者共享契约）

机器 JSON 严格字段：schema=1, states:[str], symbols:[str], blank:str, start:str, halt:[str], transitions:[{state,read,next,write,move}]。所有状态/符号必须声明；重复定义/重复状态/重复符号/未知字段/停机态出边拒绝。

`machine.py` API:
- compile_machine(raw:dict) -> Program，验证并冻结定义；Program.raw 保留规范结构；Program.blank/start/halt/table/digest 可读。
- initial(program, tape:list[str]) -> dict 配置，head=steps=0，第一输入为 current，其余逆序存 right，空输入用 blank。
- step(program, config:dict) -> dict 新配置（原配置不改）；停机/无转换则 ValueError。每步 steps+1。
- observe(program, config) -> {state,head,steps,tape:[[absolute_index,symbol],...]}，只包含非 blank 单元，按位置排序。
- run(program,config,step_budget:int,cell_limit:int,stop_path:Path|None=None) -> {status,config,executed}。status 为 HALTED/STUCK/PAUSED/RESOURCE_LIMIT/STOPPED；HALTED/STUCK 优先于 PAUSED；预算耗尽只 PAUSED，不误称非停机。STOPPED 优先全部；超存储不得提交那一步。cell_limit 是 left+right+1 的存储槽位上限，初始已超限返回 RESOURCE_LIMIT；steps 是累计、executed 是本次。step_budget>=0,cell_limit>=1。CLI硬上限：每命令步数1,000,000，存储100,000，输入及输出检查点文件4MiB；可信库调用者自行传入预算，不构成宿主沙箱。
- checkpoint(program,result) -> dict：schema=1,machine_digest,config,status,checksum；checksum 为除checksum外的规范JSON SHA256，规范编码为UTF-8、键排序、紧凑分隔符、ensure_ascii=False、拒绝NaN。仅误改检测不是身份认证。
- restore(program,payload) -> config：验证checksum、machine_digest、配置字段及成员、head/steps整数、steps非负及 abs(head)<=steps；head 可为负，不等于左栈长度。不得假称检查点必然可达。

CLI:
`python universal/machine.py run MACHINE.json --input '["1","1"]' --steps 100 --cells 1000 --output CHECKPOINT.json`
`python universal/machine.py resume MACHINE.json CHECKPOINT.json --steps 100 --cells 1000 --output NEXT.json`
可选 --stop-file PATH，存在则立即STOPPED；不删除停止文件。输出JSON包含status/executed/observation/checkpoint路径。输出拒绝覆盖不同内容；已经存在的同字节普通文件视为幂等成功，链接及重解析目标拒绝。正常HALTED/STUCK/PAUSED/RESOURCE_LIMIT/STOPPED均返回0，提交前格式或文件错误返回1。先写同目录临时文件并fsync，Windows使用无覆盖MoveFileExW WRITE_THROUGH，POSIX使用硬链接发布并同步目录。发布为提交点；提交后临时清理或目录持久化确认失败，以stderr JSON warning报告而不假称未提交。硬件断电持久性未认证；预算和停机的非同义性明确。

## 证明与验收范围

证明：提供任意M的可计算有限编码、解码映射、初始保持、三种移动一步模拟引理、归纳与终止/发散对应；固定解释器而非为每个M定制函数。解释器查表可由有限字符串扫描、复制、比较实现，不预设另一个“通用机oracle”。证明是假设无界可扩展存储的纸面构造性证明，不是Lean/Coq认证。

有限验证：独立稀疏带实现做逐步差分；固定种子随机机器/输入；穷举小机器转换和局部窗口；已知机器（位翻转、unary addition、扫描与回返等）；预算分片恢复与一次运行相等；负头、空带、空白写入、L/R/S、显式halt、stuck、无限循环暂停、内存拒绝、停止、错误格式/篡改检查点。
