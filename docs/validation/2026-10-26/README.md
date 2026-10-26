# 验证记录索引

主说明见 [服务器安装与调用链修复手册](../../服务器安装与调用链修复手册.md)。

- `benchmark-report.*`：统一 LLD 的两组源码构建耗时；`cache-audit.json`、`config-comparison.json`：缓存及参数依据。
- `runtime-report.*`：17 个通过实例的运行耗时，另有 5 个两版均失败的 NPU 实例；测试版本与库版本不同，口径见报告。
- `callchain-validation-report.*`：22 份全清单紧凑采集/重放，体积、源码归并和未解析统计。
- `address-cache-validation.json`：离线地址缓存的输出一致性及耗时。
- `npu-tls-compatibility-proof.json`：同一小栈线程程序在新旧预加载钩子下的结果。
- `source-reuse-regression.log`：74 项候选插件回归。
- `installation-validation.json`、`installed-wheel-tests.log`：从真实 wheel 安装产物运行的 74 项回归，以及文档命令/脚本语法验证。

报告保留各阶段生成时的原文。构建阶段的“67 项通过”“尚未提交或推送”等字样描述当时状态；后来完成紧凑采集与源码复用修复，最终是 74 项通过。本次 Git 提交备份了修复与文档，以仓库历史为准。

测试通过、采集无缺口和源码全部解析是三个不同结论。失败前缀、线程/设备边界、未解析对象和非冷缓存限制不能从汇总中省略。大型原始事件、数据库、API 全清单及 HTML 页面保存在手册列出的服务器目录，不包含在 Git 中。
