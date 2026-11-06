# 本次实现的证据与适用边界

日期：2026-10-30。

原始输入：`C:/Users/Administrator/Desktop/Rust与Julia双线自主迭代系统架构调研.md`，重点采用第九、十三、十五节的提案制、v0 离线边界和 Rust 主控制面建议。

知识候选：用独立 Rust 验收器裁决 Python/Julia 生成的无 imports WASM，可以在有限整数契约下演示受控自迭代。此次只完成有限任务的本机验证，不把它推广为通用软件自迭代安全结论。

评分：相关性 5、准确性 3、权威性 2、时效性 5、目的偏差 4、复用价值 3。权威性未过硬门，封顶 3 分，不入全局知识库；证据留在本项目源码、验收脚本及 acceptance-result.json。没有独立外部来源支持“原型足以承载生产自治”，不得据此升级评分。

已暴露并修复的实现问题：
- WAT 没有 i64.min_s/max_s 指令，需用有类型的 if 表达式。
- 负系数生成要做 0-term，不能 term-0。
- 嵌套 if 的括号拼接易生成无效 WAT，采用分段表达式组合，并实际编译验证。
- 双线共同错误仍可能发生；编译器与独立 oracle 必须在提案器之外。
- STOP 检查与提交之间存在竞态，需要让停止写入和最终路由提交共用短事务锁，不能仅在函数开头判断一次。

下载来源：Julia 官方下载页及官方 SHA-256 清单；本机直连下载超时后，通过已配置的代理完成分块下载。该网络现象已有知识库同类记录，本次不重复归档。

## 多轮测试补充

12轮演化发现并修复当前JSON3/Parsers组合的Int64负极值解码错误，详情与失败/通过证据见BOOTSTRAP.md。评分相关性5、准确性4、权威性2、时效性5、目的5、复用4；缺少上游独立确认，按硬门封顶3分，只在项目保留，capture-log已记拒绝。

## 通用计算模型补充

新增双栈抽象模型及模拟证明，标准源模型依据SEP的Turing Machines条目；实际Python实现经独立稀疏带oracle有限验证。具体结论见universal/VERDICT.md。关于“本项目已获图灵完备认证”的知识候选仅有内部模型审阅，无证明助手内核或独立外部认证，权威性2，封顶3分，不入全局知识库；不把经典理论命题当作本项目代码的外部认证。

## 内核检查与benchmark补充

后续已用Lean4.35.0通过9个形式模型模拟定理；之前“无内核检查”描述仅适用于当时状态，当前范围见universal/formal/README.md。完整通用机编码和Python refinement仍未认证。benchmark v1的100分为本机固定工程目标达标分；有错误步数的独立副本被拒并得0分。作为一次性项目实测，知识评分权威性2封顶3分留项目，不将100分外推为智能或安全等级。

## 资源与故障注入下一轮

resilience.py的14项本机边界测试通过，见universal/RESILIENCE.md；未发现正式运行时缺陷，测试报告字段冲突已修复。单次停止约6.65ms不可提升为最坏时延承诺。知识候选权威性2、封顶3分，只留项目与capture-log拒绝记录。

## 多文件适配器补充

本轮真实HTTP客户端通过本地协议模拟器，应用运行器可携带上一版源码完成两轮语法修正；不等于真实模型推理或容器运行通过。Windows默认文本编码在中文工程路径和源码JSON中造成错解，已将提案stdout固定UTF-8、CLI结果ASCII转义；中文输出回归通过。相关性5、准确性4、权威性2、时效性5、目的5、复用4，缺独立来源核验，封顶3分留项目，不入全局知识库。当前模型配置和容器引擎缺失，隔离业务运行验收须明确BLOCKED，不能改为宿主裸跑。
## A06 威胁模型与最小可信计算基（2026-11-03）

本节是按当前实现归纳的边界，不是安全认证或对抗性审计。威胁对象包括可能错误、提示注入或伪造数据的模型/provider输出、生成候选源码、项目导入源码及参考包；参考内容和模型文本都是数据，不构成控制授权。工具schema/策略和运行时负责验证参数、路径、基线哈希、允许工具及变更大小；有作用域/会话/有效期/撤销的进程内 capability（`application_policy.py`）并不隔离宿主进程，也不防同权限进程直接改内存或文件。

最小可信计算基（TCB）为：操作者对模型许可、可信适配器命令、容器镜像/部署根和独立验收输入的选择与授权；控制器、工具分派/策略及其本机文件系统；配置的 provider 与 transport 身份/协议边界；`application_builder.py`、受信执行适配器及本地 Docker/Podman daemon、宿主内核和精确镜像；独立业务契约/oracle与其保管者。以上组件失陷、被错误配置或共享错误时，当前机制不能保证安全/正确。模型输出的候选、导入项目与参考材料、provider返回的文本/tool-call意图、候选运行产生的报告均不可信；builder/容器、文件系统与 oracle 虽可作验收权威，也必须按上述 TCB 假设受信，报告本身不是外部独立证明。

代码中已观察到的限定：候选路径/文件内容及候选schema经验证，暂存文件有清单与SHA-256核对；既有工程事务另有导入基线、源冲突检测、原子活动指针和指针回滚（`application_project.py`），源树不会被这些提交/回滚API改写。lock绑定工具链精确镜像ID、包版本、来源allowlist与制品SHA-256；环境计划声明候选运行无网络、候选和cache只读挂载（`application_dependencies.py`）。实际执行隔离仍依赖适配器、容器daemon、宿主内核与镜像，清单声明不是对daemon行为的证明。业务契约由host侧传给受信构建器，应用预检排除与参考文件的完全字节相同oracle，但明确不拦截改写/转述的答案泄漏（`application_build.py`）。

网络及所有权：provider访问是显式配置并由操作者许可的外发路径，源代码/任务上下文可能送往该provider；当前端点检查和无凭据/禁重定向等transport限制不等于provider可信或内容保密。依赖获取只走显式授权HTTPS、allowlist和预期摘要；运行构建网络计划为`none`。候选和参考包都不能自主授予新的宿主工具权限。控制器拥有源基线和提交指针，操作者拥有源码/参考源、provider与镜像选择及验收oracle；candidate只拥有隔离候选快照。持久会话/候选/控制能力文件必须保持私有，不能交给模型；用户可修改源文件并因此触发冲突，而不是被静默覆盖。

已知能力缺口：容器/宿主daemon、provider、控制器、依赖制品及oracle并非在本工作中独立验证；摘要只证明被读字节，不证明来源真实性（除被独立核验的签名/授权外）。secret启发式不是通用DLP；oracle拷贝检查不识别语义泄漏；进程内capability不构成跨进程安全边界；本地宿主权限、拒绝服务、容器逃逸、供应链投毒和共同错误仍在TCB或未覆盖面。所有结论限于已读实现与具体测试，不据此宣称安全认证或生产级安全。

## 有限重试、隐藏验收及经验对照（2026-11-03）

本轮可复用主张：模型请求自动重试必须与工具/外部效果重放分开；成功文件修改不能重置同一目标版本的网络重试额度。隐藏验收在派发前持久预留次数、未知结果阻止后续查询，已知额度耗尽应HOLD而非候选FAIL。只读检查点诊断不能调用会修复mirror事务的load函数。以上分别由`model-retry-verification-20261103.json`、`control-integration-verification-20261103.json`及`oracle-runtime-aaae3f9fd9c0/verification.json`实际验证。

知识评分：相关性5、准确性4、权威性2、时效性5、目的5、复用4；只有内部实现/实测，未独立外部核验，权威性硬门不满足，封顶3分，留项目不入全局知识库。

原子经验摘要对照`atomic-transfer-875cbb2cdd64`中基线PASS、摘要复用HOLD，因此没有收益结论；不能从一组正负样本推断摘要有害或形成稳定学习能力。当前历史记录的自然语言失效条件尚未自动映射到执行器字节，须先解决版本有效性后再做新试验。该结果同样仅项目记录，权威性2、封顶3分。

## 质量实测与离线漏洞快照（2026-11-03）

本轮候选主张：质量门禁的响应相等须以规范JSON字节为准，避免Python中True与1相等导致错误PASS；重复运行分类FLAKY不额外重试；每次执行后核对候选清单，尤其末次运行。离线漏洞检查须把实际查询数据与快照原始字节绑定，缺失/过期/坏JSON不能视作“无漏洞”。仅内部实现及父级CLI/实际Docker证据，无独立外部来源核验，评分相关性5、准确性4、权威性2、时效性5、目的5、复用4，硬门封顶3分，不入全局知识库。

证据：`bootstrap-runs/quality-advisory-parent-20261103/verification.json`、`regressions.json`及`malformed-cli.json`。正常两次容器每次3响应匹配；负向错误契约/1字节输出界限分别被拒。资源只计combined stdout/stderr字节，时延含冷容器启动；安全、可访问性、兼容性未测，整体HOLD。漏洞数据为明确标注的合成快照，不代表外部数据库覆盖。

模型环境变化：原Nanbeige权重路径本轮不存在；可用Qwen3.8-27B的长源码探针已就绪但首请求超出150秒，库存探针另次启动超过90秒未就绪。两者均无完成业务证据，独立记录，不能覆盖历史Nanbeige结论；finally均关停自建服务。不将权重可读、进程启动或模型加载当作自主构建成功。

## 适配器入口、私密部署与当前经验身份（2026-11-03）

可复用主张逐项：通用Agent不能把JSONL固定main.py契约施加给仅解析模块的AST验收器；源码身份变化应由各独立适配器范围及恢复绑定处理。私密源经只读挂载复制进0400 tmpfs后，必须降权到65534再起候选；清理源后晋级/回滚不能无凭据重建，应验证仍在运行的精确自有revision，运行时丢失需重新供应。历史经验的自然语言失效条件不等于字节校验，当前executor_sources必须实际核对，缺失/不符剔除且有原因。

证据：`adapter-entrypoint-fix-20261103.json`及真实Qwen3.5语法8模型/8工具FAIL→PASS；`secret-deploy-cbe70c210b53/verification.json`（真实deploy/promote/rollback、启动失败源清理）；`secret-deploy-cli-20261103/verification.json`；`executor-identity-parent-20261103/verification.json`（匹配1条、缺失/过期0条）。没有外部独立权威核验，评分相关5/准确4/权威2/时效5/目的5/复用4，硬门封顶3分，仅项目记录；不推为生产安全/一般学习收益。

限定完成：`reference-model-3cb87ad44e92`许可自编累加器，Qwen3.5真实读取参考、独立函数实现、双方分别新鲜容器通过整数累加/布尔拒绝/进程清零；P2-01仅源码JSONL类别。库存v2、设备借还及生成Web仍HOLD（预算/重复超界动作），不手工修候选、不改答案、不偷偷加大1500字符界限；自然语言规格仍BLOCKED，不能把测试作者结构化规格当模型语义批准。

测试驱动归因：首次Web Windows spawn错误来自一次性driver缺少__main__保护，修后才获得11模型/5工具的真实HOLD。干净部署root回滚检查必须先晋级首版本再晋级次版本，修driver后真实故障回滚PASS，不将原precondition缺陷归因部署运行时。统一40项32 PASS/8 BLOCKED/0 FAIL，历史和分项live证据不合并冒充全量通过。

## Qwen按需路由与适配器协议（2026-11-03）

原子主张1：HTTP适配器CLI必须输出通用运行时的严格六字段报告，详细HTTP/browser证据另保留；额外字段会把已完成验证误分类为未知效果。原子主张2：main.py缺失是完整性核验后的确定性派发前FAIL，不应自动归为已派发的未知副作用；篡改/隐藏输入错误仍拒绝。证据`http-cli-report-fix-20261103.json`、`http-missing-entrypoint-fix-20261103.json`及service/runtime回归。

原子主张3：经验有效期不能靠把历史验证报告的环境改成当前源码哈希；须对原始before/after字节实际重验再生成新记录。`experience-current-revalidation-5e15606e7d9b`对不可变原before/after在当前容器实测FAIL→PASS，原5+8响应，旧经验保持拒绝；新记录才进入任务前检索。

`current-atom-pair-1874d7960ead`新优惠券任务同目标/契约/预算单对照：基线3模型/3工具、31.219秒，一次PASS；摘要复用5模型/5工具、69.938秒，FAIL→PASS，工具事件证明读取精确摘要。未证明收益，不据单样本断言有害。P2-03仍开放。新的配对驱动为`experience_paired_live_driver.py`；实跑须用既有`local_model_test.py`独占管理Qwen服务（同端口已占用时拒绝）：`python local_model_test.py --port 8082 --startup-timeout 180 --test-timeout 1200 --server-argv '["C:/Users/Administrator/.unsloth/llama.cpp/build/bin/Release/llama-server.exe","-m","H:/ComfyUI_Mie_2026_V8.0/ComfyUI/models/LLM/Qwen3.6-35B-A3B/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_NL.gguf","--host","127.0.0.1","--port","8082","-ngl","99","-c","32768","-np","1","--jinja","--log-disable","--chat-template-kwargs","{\"enable_thinking\":false}"]' --test-argv '["python","experience_paired_live_driver.py","--port","8082"]'`。驱动先在锁定Docker image上重验原before/after快照，再仅凭本次成对报告登记新经验，然后运行同一任务无参考/参考组；旧报告不重标，benefit始终记录为`not_proven`。要求该llama-server与模型文件、Docker及精确本地image可用。
`experience_paired_live_driver.py`现要求任务标签全覆盖才投放经验；当前券码任务与库存修复经验无标签匹配，故无经验时跳过reuse臂，不把政策跳过误写成收益证据。

限定业务新增完成：Qwen3.6原库存自主从零5模型/4工具59.438秒原13响应PASS；该源码数据库集成迁移/恢复/版本拒绝/重开PASS；生成无状态HTTP服务独立封包部署PASS。Web续建24模型/21工具381.719秒预算HOLD，未取得完整服务浏览器PASS；规格3.6流式提案截断BLOCKED，无伪造批准。最终统一40项32PASS/8BLOCKED/0FAIL。

以上内部源码/实跑可追溯，无独立外部权威核验；六维相关5/准确4/权威2/时效5/目的5/复用4，硬门封顶3分，仅项目记录，不入全局库。

## 分阶段提案与显式thinking（2026-11-03）

原子候选：输出预算同为128 token时，一次Qwen3.6公开源码标记探针默认length、无标记；显式enable_thinking=false为stop、14 completion token并匹配标记。两次输入相同但第二次命中prompt cache，不能把时延差全部归因thinking，也不能推断复杂任务质量。原始证据`thinking-comparison-421bd53c04d5/verification.json`只保留计数而无私有推理。

分阶段提案把完整规格拆为需求/实体/任务，隐藏ID不传模型，冲突先阻塞；任务获得全部义务。首次真实设备借还仍因公开ID覆盖不一致BLOCKED，不能以结构通过代替语义批准。新增供应链gate只复用锁/缓存/本地镜像ID及操作者快照，不具备外部签名/许可权威验证，保持HOLD，A07仍开放。

以上内部源码和单次实跑可追溯，缺独立外部来源，相关5/准确4/权威2/时效5/目的5/复用4，硬门封顶3分；不入全局库。

## 显式设计输入与阻塞证据（2026-11-03）

公开工程设计与原目标分开输入、标为design并保留在spec摘要中，避免把路径/协议补充冒充用户原话。CLI存在输出时在模型前拒绝。阻塞需求阶段通过SpecRefused.proposal_stage保留公开结构与未批准/未语义验证标记，不含控制器隐藏ID；不能把该结构当合法spec执行。定向回归覆盖设计字节上限、摘要篡改、隐藏ID隔离与冲突早停；CLI烟测见`design-context-cli-smoke-20261103.json`。

首次显式设计实跑`spec-design-live-d587bb08e542`仍在85.844秒后报告空白规则歧义，尽管设计已写no extra whitespace normalization；这不是新的用户问题或规格PASS。内部证据无独立外部来源，相关5/准确4/权威2/时效5/目的5/复用4，硬门封顶3分，仅项目记录。

## 完整持久化验收与可信见证边界（2026-11-04）

有状态参考的多次进程调用不能展开成互不保留状态的普通 JSONL 场景。新增专用适配器消费精确清单并调用完整持久化验收；作者参考七场景通过、故意错误候选七场景失败。完成的业务 FAIL 必须以成功的适配器协议退出交付 `test_pass=false`，否则执行器会将其判为未知副作用。实际 live4 暴露了该协议错误；修复后的 trusted-executor 负例通过。live5 仍因十二次模型调用预算耗尽 HOLD，不是生成验收 PASS。

Windows Sandbox 内候选与报告写入者处于同一权限域时，运行 nonce 和文件摘要不能证明报告可信。捆绑控制器显式标记 guest_writable_untrusted，平台拒绝据此授权 PASS；独立受信见证尚未实现。当前宿主亦缺 Windows Sandbox，不运行候选或修改系统功能。

Web 修复 v2/v3 均九次调用耗尽保守 token 预算，未编辑或验证候选。私密挂载路径与作者服务成功不替代生成应用修复成功。统一检查 39 PASS、7 BLOCKED，无 FAIL；范围见 `bootstrap-runs/integrated-development-final-20261104.json`。

以上为内部源码与实跑证据，缺少独立外部权威核验；相关5、准确4、权威2、时效5、目的5、复用4，硬门封顶3分，仅项目记录，不入全局知识库。

## 诊断修复与经验来源边界（2026-11-05）

1. 固定SQL/类型诊断可以避免回传异常原文，但本次两个本地Qwen候选各三轮仍未修复成功；这是限定样本负结果，不推断通用效果。
2. 助手工程修复通过库存预留28与钱包27条原合同响应，另通过12条边界请求及坏JSON恢复；两条配对经验已入项目经验库。工程修复成功、经验可检索、模型自主成功、跨任务收益是不同结论，不能互相替代。

原始来源：`bootstrap-runs/diagnostic-repair-20261105-d9bad700/closeout.json`。两条均按相关5、准确4、权威2、时效5、目的5、复用4评分，缺少独立外部权威核验，硬门封顶3分；仅留项目记录，不入全局知识库。拒绝记录已由capture_knowledge.py写入capture-log.jsonl。


## 发布质量复测后的知识捕获（2026-11-06）

- 已入库原子主张：测试同步外部脚本加载前状态时，不应先等DOMContentLoaded；异步route持有响应，在释放前断言、释放后再验证。Playwright官方wait_until说明、MDN事件语义及同候选三引擎held-route实测相互核对；相关5/准确5/权威4/时效5/目的5/复用5，加权5分。最终笔记`F:/OMP/assets/知识库/自动归档/编程开发/2026-11-06-测试脚本加载前状态必须在DOMContentLoaded之前持有响应.md`，capture脚本已索引且重建启动索引。
- 仅项目保留：本机Docker /build tmpfs实测noexec导致tsc退出126，显式exec及LF wrapper/Linux执行位修复后四生态repeat PASS。Docker官网及原始文档本次访问失败，不能把本机默认值推广到所有版本。相关5/准确4/权威2/时效5/目的5/复用4，权威性硬门不满足封顶3分，capture拒绝已记录，不入全局库。
- 证据来源保持：`bootstrap-runs/release-quality-20261106-e65ce82f/ui-quality/evidence-hashes.json`、`bootstrap-runs/blocked-retest-20261106-closeout-461545b2/{tmpfs-exec-reproduction.json,toolchain-full-command.json}`。本次清理只删除可再生成缓存，不删除这些原始来源或失败证据。
