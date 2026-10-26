# 本次服务器复现脚本备份

先读 [安装与修复手册](../../服务器安装与调用链修复手册.md)。这里保存实际运行的脚本，不是一个全新机器的一键安装程序。

| 文件 | 作用与运行前提 |
| --- | --- |
| `prepare-env.py` | 从已有 `npu-test-infra/venv/test` 复制除 torch/torch-npu 外的依赖；目标 venv 必须不存在；源解释器和 CANN 必须仍可用 |
| `run_benchmark_lld.py` | 复建并安装两组库；需要工作区源码、子模块、NCCL、venv、构建工具、LLD 和准备状态；新计时使用全新 `--attempt` |
| `scenario_harness.py` | 从固定 commit 的四个上游测试文件提取十个方法；需要放在 `runtime-scenarios-c03-c05` 中，并存在 `upstream-reference/` |
| `scenario_timing_worker.py` / `run_scenario_timings.py` | 实际运行耗时采集；路径固定到 r7-lld22，写 timing 状态/结果；重新实验应换输出目录，保留旧结果 |
| `test_scenario_trace_warm.py` | 测试前预热，同一原方法随后独立采集；已知失败不隐藏 |
| `run_ledger_captures.py` | 本次整批采集驱动；包含候选插件路径、既有证据复用规则和固定输出目录，不能直接当成全新实验脚本反复运行 |
| `replay_ledger_captures.py` | 本次共享 SQL 重放驱动；等待采集完成；同槽会刷新贡献，需要保留另一个实验时换数据库/槽 |
| `requirements-without-torch.txt` | 82 个基础依赖版本盘点，包含厂商包，不保证全部可从公共 PyPI 下载 |
| `lld22-manifest.json` | 固定 LLD 安装包及依赖校验值；没有把在线“最新版本”当成本次版本 |
| `nccl-source.json` / `test-source-manifest.json` | 外部依赖和上游测试文件的版本、路径及校验依据 |
| `api-ledger-summary.json` | 全部 10,362 项 / 90 类清单的计数及 SHA256；完整清单留在服务器 |

各脚本的工作根为 `/home/workspace-ycy/compile-performance`。只读备份本身不会自动执行；需要迁移目录时先修改并核对所有输入、输出和候选/正式插件路径。大源码、wheel、venv、采集记录和数据库未放入 Git。

推荐日常采集直接使用主手册第 7 节的新输出目录命令，而不是直接重跑这里的历史整批驱动。候选运行目录与正式插件在验证时内容一致，主手册使用正式激活入口。
