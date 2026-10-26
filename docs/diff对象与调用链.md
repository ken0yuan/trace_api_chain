# diff → 源码对象 → API 调用链

入口是独立文件 `src/changed_objects.py`，只依赖标准库，可复制后单独运行。它读取 unified diff 和旧源码，输出 JSON；可选读取已有 SQLite 库反查 API 路径。目标项目不会被 import 或执行。AST 解析、diff 解析、SQL 匹配都在 pytest 之外运行。

## 运行

```bash
# diff 相对 HEAD：包括工作区里的源码改动
git -C /src/pytorch diff HEAD -- torch > change.diff
python src/changed_objects.py change.diff \
  --repo /src/pytorch --base HEAD --label pytorch \
  --db /data/pytorch-torchnpu.sqlite --out changed-objects.json

# 没有 Git，使用 diff 旧侧的源码快照
python src/changed_objects.py change.diff \
  --before-root /snapshots/pytorch-before --label pytorch \
  --out changed-objects.json
```

安装插件后等价命令是 `trace-changed-objects`。不传 `--db` 就只输出改动对象。`--repo` 默认当前目录，`--base` 默认 HEAD；比较两个提交时必须指定 diff 旧侧的提交。diff 通常只有几行上下文，无法单凭片段可靠知道外层 class/module，因此需要可读取的旧源码。新侧由 patch 严格重建，不依赖当前工作区已经修改后的文件；上下文不匹配、语法错误、非 Python 或二进制文件明确进入 unresolved。空 diff 返回空列表。出现 unresolved 或 ambiguous 时命令退出码为 2。

## 统一身份：必须使用源码名字

| 字段 | 例子 | 规则 |
| --- | --- | --- |
| path | `pytorch/torch/backend.py` | replay 的根标签 + 仓库相对路径；diff 的 --label 与 --source-root 的标签及目录层级必须相同 |
| kind | `module` / `function` / `type` / `binding` | 方法也是 function；类是 type；源码名字绑定是 binding |
| qualname | `Backend.__consume` | AST 中的词法名字，保留双下划线；嵌套定义如 outer.inner |
| source_id | SHA256 字符串 | 共用 `source_identity(path, kind, qualname)`，完全一致才关联 |

模块的 qualname 固定为 `<module>`，文件路径区分不同模块。`GPU_TYPES` 是模块 binding；`Backend.MODE` 是类 binding；`self.__worker = ...` 在函数体内部，diff 归属为包含它的函数，不凭一条赋值猜测类字段定义或对象生命周期。

哈希精确算法是 UTF-8 编码的 `json.dumps([path, kind, qualname, 0], sort_keys=True, ensure_ascii=False)` 的 SHA256，使用 Python 默认 JSON 分隔符。最后的 0 是兼容字段，不是行号。建议直接调用本文件的函数，避免不同语言 JSON 空格差异。源码行号用于定位与证据，不参与身份；运行时 `__name__` / `__qualname__` / 导出别名不参与身份；源码内容哈希单独记录版本。

外部脚本已识别出源码对象时，可以直接复用：

```python
from trace_api_chain.changed_objects import source_identity
source_id = source_identity('pytorch/torch/backend.py', 'function', 'Backend.run')
# 对应 SQL entities.id；paths.entity_id 为调用节点。
# observed_objects.entity_id / source_dependencies.entity_id 为关联对象。
```

同一限定名重复定义的函数/类标为 ambiguous，不用第几个同名定义来猜测。一个绑定被赋值多次仍属于同一个 binding；它表示这个源码命名空间里的名字，并不证明哪条赋值在本次运行生效。文件或定义改名产生旧、新两个身份，不自动猜测重命名等价关系。

`typing.overload` / `typing_extensions.overload` 是明确例外：从显式 import/from 解析装饰器来源，且词法作用域中没有冲突绑定时，把重载声明与唯一实际实现归为同一源码函数；运行时 code object 仍必须匹配实际实现的位置。支持导入别名及类方法，函数局部作用域中的装饰器来源不猜测。仅仅叫 overload 的自定义装饰器、导入后重绑定、只有声明没有实现或有多个实际实现，都不能用此规则消除歧义。这条规则由真实 PyTorch flex_attention 验证发现并加入，未写死任何 PyTorch 函数名。

## diff 输出与归属规则

`objects` 是列表，因为一个 diff 可改动多个对象。每项包含 `source_id/path/kind/qualname/object_type/status/change/sides`。`sides.old/new` 提供整份源文件的 SHA256、定义范围及真正改动的行号。纯插入/删除函数内语句仍算 modified；新增/删除定义分别算 added/deleted。

- 对改动行取最内层词法定义。普通函数体归属函数，方法体归属方法，类头/基类改变归属类，装饰器归属被装饰定义。
- 模块或类中的直接名字赋值、解包赋值、导入别名归属 binding。函数内局部变量改动归属函数。
- 无定义覆盖的模块级语句、空白或注释归属 module。函数内注释在 AST 定义范围内时归属函数；范围之外的尾部注释可能归属 module，这是固定语法规则。
- 新增/删除文件同时产生 module 对象；文件改名列出各个命名对象的旧、新身份。
- 只解析 Python `.py`，不把 C++ diff 当 Python 猜测。现有 C++ 地址/DWARF 身份及采集流程不受本功能改变。
- Git copy 检测产生的 copy from/to 元数据明确报告 unresolved；请用 `git diff --no-renames` 导出普通新增文件 diff。文件 rename 元数据支持，不能把复制误报成删除旧对象。

## 如何对应调用链

| relation | 证据含义 | 不能据此声称 |
| --- | --- | --- |
| called_definition | 这个源码函数在路径中有调用节点 | 所有改动行都执行过 |
| observed_object | 路径快照观察到归属这个源码定义的对象 | 观察到了它的所有方法调用 |
| observed_binding_read | 当前 Python 调用中，LOAD_GLOBAL/LOAD_NAME 命中全局字典，或受支持的模块 LOAD_ATTR 读取名字 | 序列化了值或证明改动改变了行为 |
| static_import_origin | 被读取的 binding 在源码中唯一导入了这个对象 | 运行时没有 monkey patch 或动态重绑定 |
| defined_in | 被调用/观察对象词法上定义在该类、函数或模块内 | 类体/模块顶层在该 API 调用期间重新执行了 |
| read_from_container | 被读取的 binding 属于该源码容器 | 容器中的其他名字也被读取 |
| static_import_container | 静态导入来源所属的源码容器 | 已观测到容器的执行 |

例如 `from config import GPU_TYPES as SUPPORTED_DEVICES`：执行时先记下消费者模块的 `SUPPORTED_DEVICES` 读取，离线将它与 `config.py::GPU_TYPES` 建立明确标成静态来源的关系。直接 `config.GPU_TYPES` 的支持形式则能留下 config 模块本身的名字读取证据。绝不把读取边加进函数调用栈。

`--db` 输出每个对象的 matches，包含 API 别名、测试、slot、完整祖先 call_path、关系类型、证据及 `matching_diff_sides`。通过 API 和祖先上下文查询，不把不同 API 的分支串成一条路。只匹配 current/partial 贡献，忽略 stale。`version_status=matched` 表示捕获对应的源文件版本与 diff 旧侧或新侧一致；不同版本仍可同名匹配，但会明确标为 `different_or_unavailable_source_version`。

输出 `not_observed_in_current_database` 只表示当前证据没有匹配，不表示没有调用者、不受影响或可以删除。需要分析新增对象的实际路径时，要运行覆盖它的新测试并重新采集。

## 采集边界和源码证据

采集入口仍为选中 pytest 测试线程的 API 子树，仍排除约定的 torch.compile 子树。新增读取事件按调用 UID、指令位置、绑定名去重；不为了这个功能反复复制常量值。

`Recorder.trace` 只支持全局命名空间命中，以及连续的 LOAD_GLOBAL/LOAD_NAME → LOAD_ATTR 模块链；只检查精确 ModuleType 字典中已有的名字。`local_module.GPU_TYPES`、`getattr(module, name)`、任意对象/class 属性、模块 __getattr__ 的动态结果不作为本功能的读取证据。class binding 可被 diff 识别，但当前不承诺自动捕获 `Class.MODE` 的读取。普通实例/函数捕获仍按原有规则执行。

`SourceEntities.named` 校验采集时的文件大小/mtime 与离线源码是否一致，再从 AST 找到该源码名字；不通过运行时对象字符串拼出定义。文件大小/mtime 是变更检测，不是加载字节码与磁盘源码一致的密码学证明。需要可信结果，应使用采集与重放一致的源码快照。源文件哈希在离线阶段生成；源码缺失、发生变更、名字动态产生或定义不唯一时进入 pending。

静态导入来源只解析唯一显式 import/from（支持相对导入），来源模块必须在已记录的模块文件目录中并通过源码版本检查。星号导入、动态 import、导入后多次源码赋值、无法解析来源等情况不会凭同名猜测。

旧采集没有 source_read 事件，重放可以保留原来的函数身份，并补充可核验的词法包含关系，但不能补造旧运行的全局读取。需要重新采集相应测试。`active_capture_coverage[].source_read_policy` 为 null 表示该贡献缺少新读取策略；表存在不等于所有测试已覆盖。

代码位置：

- `changed_objects.py`：source_identity、index_python、identify_changes、match_callchains；唯一公共身份与 diff 入口。
- `object_capture.py`：Recorder.source_read / trace；运行时只记录读取位置与命名空间锚点。
- `replay.py`：source_read 分支校验 UID、活动调用和 code 文件，不生成推测调用边。
- `source_entities.py`：python / named / import_origins / containers；统一源码身份与明确的静态来源。
- `database.py`：refresh 的 source_dependencies、query；事务刷新与独立只读查询。
- `tests/test_changed_objects.py`：源码位置/名字变更、增删改名、歧义拒绝以及 pytest → 重放 → SQL → diff 匹配的回归。

对象范围总表与提取自源码的证据片段在 [对象采集与源码归一化](对象采集与源码归一化.md)，SQL 表结构在 [SQL 数据库说明](SQL数据库说明.md)。

## 仓库内可复现示例

在仓库根目录使用 CPython 3.11 和已安装的插件运行：

```bash
python -m pytest examples/test_source_identity.py::test_source_identity \
  --callchain-mode=objects --callchain-namespaces=source_identity_backend \
  --callchain-out=/tmp/source-identity-capture
trace-api-chain replay /tmp/source-identity-capture \
  --db /tmp/source-identity.sqlite --source-root demo="$PWD"
python src/changed_objects.py examples/source_identity.diff \
  --before-root "$PWD" --label demo --db /tmp/source-identity.sqlite \
  --out /tmp/source-identity-result.json
```

示例不修改源码文件，diff 描述拟议改动。预期五个改动对象：模块、Backend、Backend.__consume、GPU_TYPES 有关联；UNUSED_TYPES 未被本次测试读取，匹配为空。运行时类名被改成 RuntimeBackend，仍对应源码的 Backend。实际验证记录位于 `data/source-identity-validation/`，Git 忽略原始数据；示例源码和 diff 随仓库提交。

2026-10-26 验证：完整回归 58 项通过；示例独立 pytest 测试通过。示例捕获 3 次 Python 调用，重放形成 5 个归一化路径、27 条分类型源码依赖，gaps 为空；3 个 pending 是未能归属源码定义的对象观测，并未伪装为源码实体。

随后增加真实 PyTorch 验证与 overload 修复，完整回归更新为 **65 项通过**。真实提交的 20 个对象及 CPU/CUDA 调用证据见 [PR #201098 验证报告](validation/pytorch-201098.md)，不是前述 fixture 示例的重复运行。
