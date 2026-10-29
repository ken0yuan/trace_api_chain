# 大型基础数据容器摘要与两层展开对照（2026-10-29）

用户授权分支 experiment/scalar-container-summary，基于两层实验0e20c33。完整展开仍为默认，正式任务和数据库不切换。本实验允许明确减少对象引用证据，实际函数和原生调用记录不按数据类型、数量或深度删减。

## 三种独立模式

- 完整：--callchain-reference-depth full。
- 两层：--callchain-reference-depth 2。
- 摘要：--callchain-reference-depth full --callchain-scalar-container-summary。

摘要与两层同时开启会拒绝执行，防止把两种取舍混在一组。

## 摘要规则

只接受确切的内建 list、tuple、dict、set、frozenset，元素数严格大于100。完整扫描所有直接成员，不执行自定义迭代器、属性或repr。元素必须全部是同一种确切的 int、float、bool、str 或 bytes。子类、None、complex、函数、Tensor、算子描述对象、自定义实例以及混合类型均不满足规则，继续原有完整展开。字典按条目数计数，键和值分别完整检查，各自必须同质且属于上述白名单；键类型与值类型可以不同。

满足规则时保留容器观察身份、容器类型、长度、元素类型、字典键类型、所属观察调用UID，以及未记录payload和成员身份的声明。这个调用位置是观察上下文，不是已证明的创建点；本版本没有新增完整的容器创建/写入/读取数据流跟踪。已有实际调用和source_read仍保留。

同长度同类型的内容替换不会形成完整数据历史，这是明确允许的实验省略；不能让AI据此声称知道具体值。字符串或标量也可能是控制配置、注册名称，摘要不表示其不影响功能。若之后混入函数、Tensor或其他非基础对象，完整扫描立即发现，原有展开恢复。若长度或类型变化，摘要更新。

缓存验证也遵守同一摘要边界，不在后台悄悄遍历其成员引用；其余对象不因该模式限制递归深度。图签名在摘要模式中保留声明的结构状态，不比较被声明省略的数据身份或内容。

## 证据与隔离

开始和结束回执明确 scalar_container_summary_threshold=100，object_scope_complete=false。事件 data_container_summary；采集状态 captured_summary；图状态 summary_object_scope_validated。gaps为空只说明对应范围的已记录证据校验通过，不代表完整对象图。

摘要贡献必须使用 experiment:scalar-container-summary: 槽位与 .summary.sqlite 文件名，槽状态summary；正式库路径拒绝。摘要详情另存pending，避免只保留计数。所有实验原始数据保留，不调用会清理原始关系目录的append流程。

## 验证

运行实现整套trace269项、API121项通过/9跳过；另新增1项逐边对照通过：相较完整快照，仅已声明摘要容器的成员边被省略，所有其他边、非基础对象及共享数据根保留。真实函数调用拓扑与完整模式一致；混合尾部、超过两层的非基础对象、真实CPU Tensor、字典混合键值、非法回放和正式库隔离都有验证。

profile、trace、trace_event、begin、metadata、source_read的AST与基线相同，证明见 docs/validation/2026-10-29/scalar-summary-call-hook-proof.json；这不是对其他线程/进程/设备执行零遗漏的证明。

2048个同质字符串、600次同类型位置更新的局部快照及落盘：完整0.499426秒/1342023字节；两层0.606996秒/1342136字节；摘要0.017514秒/2005字节。它故意不保留数据内容历史，不能解释为完整NPU用例已经加速同样倍数。

## 真实三组对照

服务器维护根 timeout-tbe-writer-repair-2026-10-29/summary-three-way-v1。同一runtime 0.3.5+summary.1与API0.1.12，共享完整10362 API台账，PyTorch/torch-npu插桩包不变，1800秒预算不变。

- LSTM原ID001229：卡0完整、卡1两层、卡2摘要。
- LinearCE原ID000610：卡3完整、卡4两层、卡5摘要。

普通任务每卡一个任务组，父子进程算同组。这是单次同机并行对照，没有证明冷缓存隔离，不能当稳定统计基准。超时只报告上限未完成，不按截断耗时算精确提速倍数。

首个150秒快照六组均运行中，实际两个插件路径、API台账已核验。自动验证监控等待这些既有任务结束后再离线串行整理成功的采集，保持全局最多8路整理、不写正式库、不删证据。最终读取 validation.json 与 comparison.md，结果未生成前不能报实验成功。

较早独立两层实验depth2-experiment-v1的四组（3两层/1完整）均1800秒超时，原始证据保留；这不是本次匹配三组实验的最终结论。

Assisted-by: OpenAI Codex.
