# trace-api-chain

一个工具包，三个隔离阶段。pytest 只采集；离线重放刷新同一个 SQLite 数据库；HTML 独立从 SQL 查询生成。AI 不参与调用图生成或实体合并。

源码文件直接放在 `src/`，没有 `src/trace_api_chain/` 包目录；`pyproject.toml` 将 `src/` 映射为安装后的 `trace_api_chain` 包，pytest 插件名和命令不变。验证记录、共享数据库和生成页面统一在项目内的 `data/`，整个目录由 `.gitignore` 排除。

## 安装

```bash
python -m pip install /path/to/trace-api-chain
```

验证环境为 Linux/glibc、CPython 3.11。完整原生采集需要 gcc，目标 PyTorch/TorchNPU 库需要使用 `-finstrument-functions` 构建；`-g1` 为离线定位提供调试信息。离线源码定位使用 elfutils 的 `eu-addr2line` 和 libdw；Ubuntu 可安装 `build-essential elfutils libdw-dev`。包内 C 定义读取器由离线命令编译并缓存，每个库的解析进程有 180 秒 CPU、240 秒墙钟和 8 GiB 地址空间上限，失败不替换 SQL 中已有贡献。仅 Python 对象模式不要求插桩库。

## 第一步 pytest 只采集

```bash
python -m pytest test/test_torch.py::TestTorchDeviceTypeCUDA::test_fill_barebones_unsigned_cuda_uint16 --callchain-out=/tmp/raw/fill
```

插件通过 pytest11 自动加载；设置了 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 时加入 `-p trace_api_chain.pytest_plugin`。

每份记录当前对应一个明确的 pytest nodeid，输出目录必须不存在。不传 `--callchain-out` 时插件不启用、不重复执行测试。可以由现有测试调度器产生多份记录，后续统一入库。

```bash
python -m pytest tests/test_backend.py::test_one --callchain-mode=objects --callchain-namespaces=torch,torch_npu,my_backend --callchain-out=/tmp/raw/backend
```

默认根范围为已加载 torch 和 torch_npu 公开绑定的运行时身份目录。自定义后端在 torch API 外部执行时，需要将它的模块命名空间加入范围；采集器不从 torch 调用向外猜测后端对象。

采集产物只有 `events.jsonl`、原生 `segment-*.bin`、窗口/模块映射、API 目录和测试回执。此阶段不解析 DWARF、不写主 SQL 库、不生成 HTML。`capture-result.json` 的 `stage=capture_only` 可以核对隔离状态。

## 第二步独立重放刷新共享 SQL 库

```bash
trace-api-chain replay /tmp/raw/fill /tmp/raw/backend --db /tmp/pytorch-torchnpu.sqlite --source-root pytorch=/home/user/src/pytorch --source-root torch_npu=/home/user/src/torchnpu
```

每个输入根据其采集回执选择 full 或 objects 模式。`--source-root LABEL=PATH` 将构建机绝对路径规范成源码根标签下的路径，便于同一套源码身份复用。torch 与 torch_npu 可以进入同一个数据库，不为每个 API 单独建库。

重放校验事件顺序和窗口，解析源码定义，合并运行时别名及实例的源码归属，在事务内刷新该测试的贡献。同一证据重复重放不增加实体和路径。默认刷新槽由测试源码位置、nodeid、采集命名空间和模式组成；需要保留同一个测试的多种设备/配置时，分别使用 `--slot cuda_variant` 等显式槽名。

这一步不启动 pytest，也不生成 HTML。新记录校验或事务写入失败时不替换库中已有成功贡献。一个测试刷新不会删掉其他测试对同一 API 的贡献。源码版本改变会将依赖旧版本的其他贡献标为 stale；它们须重新采集才会进入当前查询。

数据库是全部已导入观测的统一图，不会把没有执行过的 PyTorch/TorchNPU 分支伪造为已采集。旧版 schema v3 数据库不会被本版本覆盖；新库使用独立可识别的 schema。

## 第三步从数据库查询和生成 C03

```bash
trace-api-chain apis --db /tmp/pytorch-torchnpu.sqlite
trace-api-chain query --db /tmp/pytorch-torchnpu.sqlite --api torch.Tensor.fill_ --out /tmp/fill.json
trace-api-chain html --db /tmp/pytorch-torchnpu.sqlite --api torch.Tensor.fill_ --out /tmp/fill-page
```

打开 `/tmp/fill-page/C03.html`，移动页面时保留 `C03.data/`。HTML 只读 SQL；原采集目录、源码和构建环境离线或移走后，仍可生成已入库 API 的页面。页面是已观测源码路径的汇总，不冒充某一次按时间排序的调用记录。

## 从 diff 反查源码对象与调用链

```bash
python src/changed_objects.py change.diff --repo /home/user/src/pytorch --base HEAD --label pytorch --db /tmp/pytorch-torchnpu.sqlite --out /tmp/changed-objects.json
```

`changed_objects.py` 可单文件运行，只依赖 Python 标准库；安装后也可调用 `trace-changed-objects`。`--base` 必须对应 diff 的旧侧版本；没有 Git 时用 `--before-root` 指定旧源码快照。只识别对象时省略 `--db`。同一个 diff 可以返回多个对象，规则覆盖模块、函数、类、方法以及模块/类绑定，不依赖 GPU_TYPES 等具体名称。

真实验证见 [PyTorch PR #201098：源码对象与 API 路径](docs/validation/pytorch-201098.md)：原始提交 diff 识别出 20 个对象，CPU/CUDA 实测将其中两个改动函数对应到 flex_attention API；其余 18 个保持未观测状态。包含原始 diff、完整 JSON 结果与复现命令，65 项回归通过。

diff 与重放共用 `源码根标签/相对文件 + kind + 源码词法限定名`，运行时别名、地址和行号不进入身份。`--label` 必须与 replay 的 `--source-root` 标签及根目录层级一致。读取全局绑定和直接模块属性时，新采集器写出 source_read；重放将读取证据与静态导入来源分别存入 source_dependencies。旧记录没有读取事件的部分需要重新采集。详见 [diff 对象与调用链](docs/diff对象与调用链.md)。

## 合并规则与边界

Python 函数优先使用运行时 code object 的文件/首行与 AST 定义匹配，展示名或导出名改变不改变源码归属。实例以自身 Python 方法的代码位置寻找唯一类归属；不能唯一定位的对象进入 pending，不按名字相似强行合并。空类、动态生成类或复杂方法替换可能缺少足够源码锚点。

C++ 使用匹配 ELF 的 PC 和 DWARF 声明位置、名称及 linkage identity。没有唯一 DWARF 定义时保持二进制地址实体并标注未解析；不拿执行行当作函数定义，不随意删除模板参数或合并重载。C++ 定义位置移动时可能产生新身份；旧版本贡献通过版本失效处理，不猜测跨版本迁移。

合并的是源码实体，不是业务实例的生命周期，也不把各次调用事件当作同一个实例。数据库保留 API、祖先路径、边类型及证据目录指针；完整事件仍保存在原始证据目录。全局关系表的可达路径不自动视为一次真实调用，API 导出使用带上下文的 paths 表。

`torch.compile` 调用子树、采集期间返回的包装器，以及 torch._dynamo/torch._inductor 子树明确排除，留下 excluded_call 记录。后两个是较宽的编译子系统边界。当前只归属测试线程；Python 对象是快照与引用观测，C++ 只记录主机函数事件，不记录 C++ 实例、局部变量或 GPU 内核内部执行。

预算：`--callchain-max-events` 每个 native 分片默认 262144 条；`--callchain-max-references` 每个 Python 快照默认 100000 次引用访问；`--callchain-timeout` 默认 3600 秒。溢出、钩子替换、错误序号与不配对事件不得伪装完整。测试退出码与采集状态分别记录。

详细对象范围和后端类案例见 `docs/对象采集与源码归一化.md`；SQL 结构见 `docs/SQL数据库说明.md`；代码职责见 `代码树.txt`。
