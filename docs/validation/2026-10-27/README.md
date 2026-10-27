# 2026-10-27 验证记录

完整说明见 [实验报告](../../编译排除标准库与性能验证-2026-10-27.md)。

- experiment-results：三组环境、22 个实例的功能状态、17 个通过实例的 9 轮计时，以及 fill_ 分段与微测试原始结果。
- build-audit：干净目录、编译命令标志、输出时间与环境一致性审计。未清空操作系统文件缓存。
- callchain-summary：22 份全 API 清单采集/重放、源码归属、旧 API 覆盖比较、入口/出口计数及体积。原始库、记录和数据库仍保留在报告所列服务器目录。
- plugin-callsite-diagnosis 与 mismatch 文本：GCC 将 BFloat16 store 拆分后，两端调用位置不同的实际机器指令证据。
- plugin-regression-tests：修复后 82 项通过。功能测试仍有 5 个三版一致的 NPU 失败，与采集缺口分开记录。

同目录 performance-summary 是便于读取的摘要。上一级 reproduction 是本轮驱动快照，不是无条件重跑入口；复现应先更改实验目录与 case 名称，避免覆盖既有记录。native_scope_counter 是单独诊断，不能混入正常耗时测量。
