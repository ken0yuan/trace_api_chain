# 真实 PyTorch diff → 源码对象 → API 调用链

分析格式：draft-v0。验证日期：2026-10-26。分析与实现协作：Codex。

## 结论

使用 PyTorch 上游 [PR #201098](https://github.com/pytorch/pytorch/pull/201098) 的真实提交，不修改目标源码、不替换 PyTorch 函数、不使用伪造 Tensor。完整 diff 自动识别 **20 个源码对象、0 个 unresolved**。原有共享 SQL 库匹配为 0；补充真实 CPU、CUDA API 采集并离线重放后，两个改动函数匹配到同一个公开 API：

```text
torch.nn.attention.flex_attention.flex_attention
├── _validate_device
│   └── torch._C._get_privateuse1_backend_name  [实测 C callable 边界]
└── _enforce_mem_layouts
```

两个函数都有 `called_definition` 证据，不仅是名字读取或静态导入候选。CPU、CUDA 两份独立记录支持相同源码路径；两个函数的 SQL 文件版本均匹配 diff 新侧。其余 **18 个对象没有当前 API 路径匹配**，未据此推断它们不受影响。

本次检验范围是公开 API 的前置校验。输入是实际 CPU/CUDA float32 Tensor，shape 为 `(1, 4, 8)`；PyTorch 先调用这两个改动函数，再按预期抛出“必须是 4D Tensor”的错误，pytest 验证该错误并通过。没有进入 torch.compile 或 attention 内核，也没有验证 PrivateUse1/openreg/NPU 的功能正确性。

## 源码与证据身份

- 新提交：[1ee9fbabf8ea81cd586204a656515d96ccf6917f](https://github.com/pytorch/pytorch/commit/1ee9fbabf8ea81cd586204a656515d96ccf6917f)，标题 `Allow PrivateUse1 devices in flex attention (#201098)`。
- 父提交：`72e7a5c2d71f8f121f2de0e221caca789843a1d0`。父提交从上游 Git 获取；未切换或修改正在使用的 PyTorch 构建工作区。
- 实际导入版本：`2.16.0a0+git1ee9fba`，源码来自 `/home/ken0yuan/src/pytorch-instrumented/torch/`。
- [原始 diff](pytorch-201098.diff)：完整 `git diff BASE HEAD`，5 个文件，128 行新增、6 行删除。SHA256：`3bbc0dffeed0a21f4834a4f700f63daf277fd6d7e05253a66befb9ab672f357b`。
- [完整机器结果](pytorch-201098-result.json)：20 个对象的身份/旧新版本、所有匹配关系、API 路径、运行编号、源码定义和证据哈希。
- [实际测试代码](../../examples/test_pytorch_diff.py)：直接调用 PyTorch 公开函数。参数化 CPU/CUDA，Tensor 在 fixture 中分配，不对目标函数做 monkey patch。

共享数据库仍为 `data/pytorch-torchnpu.sqlite`；CPU/CUDA 贡献新增到同一库。原始记录保存在 `data/pytorch-201098/{cpu,cuda}/`，由 Git 忽略；本报告、原始 diff 和小型 JSON 结果随仓库上传。

## 识别出的全部源码对象

路径缩写：

- A：`test/cpp_extensions/open_registration_extension/torch_openreg/tests/test_ops.py`
- B：`test/inductor/test_triton_extension_backend.py`
- C：`torch/_higher_order_ops/flex_attention.py`
- D：`torch/nn/attention/flex_attention.py`
- E：`torchgen/model.py`

所有路径统一带 `pytorch/` 根标签。下表来自完整 diff 的自动结果，未手工筛掉测试文件或未命中对象。

| 文件 | 源码对象 | 类型 | 变更 | 当前关联 |
| --- | --- | --- | --- | --- |
| A | `<module>` | module | modified | 未观测匹配 |
| A | `TestFlexAttention` | type | added | 未观测匹配 |
| A | `TestFlexAttention.test_flex_attention_autocast` | function | added | 未观测匹配 |
| A | `TestFlexAttention.test_flex_attention_forward_backward` | function | added | 未观测匹配 |
| A | `TestFlexAttention.test_flex_attention_forward_backward.causal` | function | added | 未观测匹配 |
| A | `TestFlexAttention.test_flex_attention_forward_backward.rel_bias` | function | added | 未观测匹配 |
| A | `create_block_mask` | binding | added | 未观测匹配 |
| A | `flex_attention` | binding | added | 未观测匹配 |
| A | `instantiate_device_type_tests` | binding | added | 未观测匹配 |
| B | `TritonExtensionBackendTests` | type | modified | 未观测匹配 |
| B | `TritonExtensionBackendTests.test_flex_attention_open_device_registration` | function | added | 未观测匹配 |
| B | `create_block_mask` | binding | added | 未观测匹配 |
| B | `flex_attention` | binding | added | 未观测匹配 |
| B | `has_triton_package` | binding | added | 未观测匹配 |
| C | `<module>` | module | modified | 未观测匹配 |
| C | `_flex_attention_autocast_impl` | function | modified | 未观测匹配 |
| C | `flex_attention_autocast_privateuse1` | function | added | 未观测匹配 |
| D | `_enforce_mem_layouts` | function | modified | flex_attention，CPU/CUDA |
| D | `_validate_device` | function | modified | flex_attention，CPU/CUDA |
| E | `DispatchKey.AutocastPrivateUse1` | binding | added | 未观测匹配 |

这个 diff 中的 `supported_devices` 是函数内局部变量，工具按固定规则把它归属到 `_validate_device`，没有额外伪造一个模块全局对象。`DispatchKey.AutocastPrivateUse1` 是源码 class binding，因此独立输出；当前运行没有执行 torchgen 代码生成器，未强行关联运行时同名 C 扩展枚举。

## 实测路径与改动语义

以下 UID 只在各自的 run_id 与原始文件哈希内有效。CPU 和 CUDA 恰好都使用相同局部 UID，不能仅凭 UID 把两次运行拼在一起。

| 调用节点 | 父节点 | 源码位置 / 证据 |
| --- | --- | --- |
| `p:2 flex_attention` | API 根 | code:1，D:2409，源码函数体；API 目录中的真实 code/globals 身份 |
| `p:11 _validate_device` | p:2 | code:5，D:2238；调用点 D:2491 |
| `p:12 _get_privateuse1_backend_name` | p:11 | C callable 事件；调用点 D:2261，无原生函数体展开 |
| `p:13 _enforce_mem_layouts` | p:2 | code:6，D:2280；调用点 D:2492 |

实测关系来自原始 call 的 parent 字段，经事件栈校验后重放，未根据函数名拼接。两次测试各捕获 15 次 Python/C callable 调用，均 gaps 为空；完整记录还包括其他前置校验、Tensor.dim/size 和目录内重导出绑定的调用。本图只展示与改动相关的子图。

源码可解释的变化：

1. `_validate_device` 每次调用读取当前 PrivateUse1 后端名，将其加入支持设备集合。此次不仅进入该函数，还观察到了新增 `_get_privateuse1_backend_name` 调用（p:12）。这证明新增代码在 CPU/CUDA 校验中执行过；没有证明 PrivateUse1 Tensor 能完成 attention。
2. `_enforce_mem_layouts` 增加 `query.device.type == "cuda"` 条件。CPU float32 输入在该条件处短路；CUDA float32 输入在后续 FP8 dtype 条件处短路。函数调用是真实观测；具体条件分支解释来自源码和受控输入，本采集器没有记录每个比较表达式的值。本次未执行 FP8 布局转换。
3. C 文件中的 autocast 与 PrivateUse1 注册、E 文件中的代码生成枚举没有对应本次路径。不能从同名或模块导入推断它们在本次 API 中执行。

源码身份和 SQL 路径：

| 对象 | source_id | SQL path_id |
| --- | --- | --- |
| flex_attention | `fcc739ac33bed7c30a32eff27be5f1553814bfdbca98cbf90b285c4fb1c7f869` | `2f13e1975c85497a60dc0033a5a3dcea6600f1ebb7a621688d728d2ce2fcf676` |
| _validate_device | `2b3c22541deed29d3e3516a8eb5f6053ff1fc44fedfd04882b104f8d73856fe4` | `087032e85dd3e16fdbf654c052ad34ab6ad83ec5fe656735d6bf30b7631dcfae` |
| _enforce_mem_layouts | `b819989270836625f2614689e609445a98bd53276384158b02405c1ddb6ee2ff` | `3d5dd724e3e6520673f0bc2ec7d12ba9dd7d0329bbcf6ad145229b7d9d37d3c4` |

上述函数文件 SHA256 为 `eeb1d7994cc9b8b122a9fd0c1d8de09f7143169b19187eaa6d3bd7582d94184b`。每条 called_definition 匹配的 `matching_diff_sides=["new"]`。两个改动函数还可以有 observed_binding_read / observed_object / defined_in 关系；这些关系单独标记，不当作额外函数调用。

## 真实验证暴露并修复的通用问题

首次重放时 `_validate_device` 和 `_enforce_mem_layouts` 已能定位，但 API 根 flex_attention 因四条 `@overload` 声明加一个实际函数体被标为 ambiguous。该问题没有靠 PyTorch 特例绕过。

现在 AST 解析显式导入的 `typing.overload` / `typing_extensions.overload`（含别名），区分声明与唯一实际实现；源码身份仍是文件＋类型＋词法名字。运行时 code 的首行只与实际实现匹配。自定义同名装饰器、重绑定、缺少实现或多个实际实现仍拒绝猜测。对同一份未修改的原始事件重新重放后，API 根正确归并，CPU/CUDA 路径共享同一个源码身份。

新增 7 项回归覆盖上述情况，完整测试 **65 passed**。实现见 `src/changed_objects.py::index_python` 与 `src/source_entities.py::python`，测试见 `tests/test_overload_identity.py`。

## 证据边界与复现

CPU run_id：`pytest:examples/test_pytorch_diff.py::test_flex_attention_validation[cpu]`，原始 events.jsonl SHA256：`dd4a35facd024d6ba34fa79f59012141e85330f2be124b3fe2e59c645b956df7`。

CUDA run_id：`pytest:examples/test_pytorch_diff.py::test_flex_attention_validation[cuda]`，原始 events.jsonl SHA256：`87eb850c9e2a4585a7b9e02ad20100396d6a4cab1622f9141532dbec00121c03`。

两次均使用 objects 模式：Python 调用和 C callable 边界有事件，C++ 函数体、设备内核没有采集。每份重放有 32 条 pending 对象/名字归属，仍保留未确定状态；gaps 为空不表示所有对象和原生函数均有源码定义。私有后端和 autocast 的未覆盖部分需要专门环境和输入；当前不声明上游 PR 的功能测试全部通过。

在安装了此插件、实际 PyTorch 版本为该新提交的环境中，在本插件仓库根目录运行：

```bash
# 原始 diff；BASE 的 Git 对象必须可读取
git -C /src/pytorch diff \
  72e7a5c2d71f8f121f2de0e221caca789843a1d0 \
  1ee9fbabf8ea81cd586204a656515d96ccf6917f > /tmp/201098.diff

# pytest 只采集。两个输出目录事先必须不存在。
python -m pytest 'examples/test_pytorch_diff.py::test_flex_attention_validation[cpu]' \
  --callchain-mode=objects --callchain-namespaces=torch.nn.attention.flex_attention \
  --callchain-out=/tmp/201098-cpu -q
python -m pytest 'examples/test_pytorch_diff.py::test_flex_attention_validation[cuda]' \
  --callchain-mode=objects --callchain-namespaces=torch.nn.attention.flex_attention \
  --callchain-out=/tmp/201098-cuda -q

# 独立重放，统一进入同一个库
trace-api-chain replay /tmp/201098-cpu /tmp/201098-cuda \
  --db /tmp/pytorch-torchnpu.sqlite --source-root pytorch=/src/pytorch

# 独立 diff 解析和只读匹配
trace-changed-objects /tmp/201098.diff --repo /src/pytorch \
  --base 72e7a5c2d71f8f121f2de0e221caca789843a1d0 --label pytorch \
  --db /tmp/pytorch-torchnpu.sqlite --out /tmp/201098-result.json
```

若没有 CUDA，CUDA 测试会跳过；不能将 skip 当作 CUDA 采集成功。本次实际环境 CUDA 可用，两项测试均为 passed。完整改动对象和未匹配列表可直接查阅随仓库上传的 JSON，无须访问这台机器的 data 目录。
