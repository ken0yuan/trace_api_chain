# SQL 数据库说明

一个 SQLite 文件汇总已导入的 PyTorch 与 TorchNPU 证据。只有离线 replay 写库；pytest 不打开数据库；query 和 html 使用只读连接。

| 表 | 用途 |
| --- | --- |
| entities | 归一化源码实体及当前内容版本；未解析函数保留显式 unresolved 身份 |
| aliases | 一个实体的不同运行时标签，不作为实体身份依据 |
| capture_slots | 每个测试/配置的最新证据贡献与原始目录指针，状态 current、partial 或 stale |
| slot_entities | 贡献依赖的源码实体版本，用于版本变化失效 |
| apis 和 api_alias_support | API 实现归属及仍由当前记录支持的导出别名 |
| paths | API 内的源码实体祖先路径；同一实体可出现在不同上下文 |
| path_support | 哪份最新采集支持哪条路径；刷新只替换该贡献 |
| observed_objects 和 object_references | 路径关联的已观测源码对象及引用角色，非业务实例生命周期 |
| pending | 无法唯一确定的对象源码归属，不猜测合并 |
| canonical_relations 视图 | 全局去重的已观测边集合；不能当作一个 API 的完整运行路径 |

事务以 BEGIN IMMEDIATE 开始；错误回滚。相同输入与归一化策略、源码版本重复导入返回 unchanged。源码版本改变时，其他依赖旧版本的贡献标 stale，默认 API 查询排除 stale。不会按“这次没有执行”推断整个程序已删除某个函数。

API 查询必须保留 paths 的祖先上下文；直接在 canonical_relations 上做图遍历可能串接来自不同 API 的分支。数据库不保存每次调用的完整流水；原始 JSONL/二进制证据以及离线 graph.json 保留精确调用记录。

SQL schema 源码如下。

```sql

CREATE TABLE IF NOT EXISTS schema_info(version INTEGER NOT NULL);
INSERT INTO schema_info SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_info);
CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY, path TEXT, kind TEXT NOT NULL,
 name TEXT NOT NULL, source_sha256 TEXT, language TEXT, status TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aliases(entity_id TEXT REFERENCES entities(id), alias TEXT, origin TEXT,
 PRIMARY KEY(entity_id,alias,origin));
CREATE TABLE IF NOT EXISTS capture_slots(id TEXT PRIMARY KEY, test TEXT, input_digest TEXT,
 import_digest TEXT, evidence_directory TEXT, status TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS slot_entities(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 entity_id TEXT REFERENCES entities(id), source_sha256 TEXT, PRIMARY KEY(slot_id,entity_id));
CREATE TABLE IF NOT EXISTS apis(id TEXT PRIMARY KEY, entity_id TEXT REFERENCES entities(id));
CREATE TABLE IF NOT EXISTS api_aliases(api_id TEXT REFERENCES apis(id), alias TEXT,
 PRIMARY KEY(api_id,alias));
CREATE TABLE IF NOT EXISTS api_alias_support(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 api_id TEXT REFERENCES apis(id), alias TEXT, PRIMARY KEY(slot_id,api_id,alias));
CREATE TABLE IF NOT EXISTS paths(id TEXT PRIMARY KEY, api_id TEXT REFERENCES apis(id),
 parent_id TEXT REFERENCES paths(id), entity_id TEXT REFERENCES entities(id), edge_kind TEXT, depth INTEGER);
CREATE TABLE IF NOT EXISTS path_support(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 path_id TEXT REFERENCES paths(id), PRIMARY KEY(slot_id,path_id));
CREATE TABLE IF NOT EXISTS observed_objects(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 path_id TEXT REFERENCES paths(id), entity_id TEXT REFERENCES entities(id), PRIMARY KEY(slot_id,path_id,entity_id));
CREATE TABLE IF NOT EXISTS object_references(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 owner_path TEXT REFERENCES paths(id), source_entity TEXT REFERENCES entities(id),
 target_entity TEXT REFERENCES entities(id), role TEXT, PRIMARY KEY(slot_id,owner_path,source_entity,target_entity,role));
CREATE TABLE IF NOT EXISTS pending(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 observation TEXT, reason TEXT, detail TEXT, PRIMARY KEY(slot_id,observation,reason));
CREATE INDEX IF NOT EXISTS paths_api ON paths(api_id,parent_id);
CREATE INDEX IF NOT EXISTS paths_entity ON paths(entity_id);
CREATE INDEX IF NOT EXISTS support_path ON path_support(path_id);
CREATE INDEX IF NOT EXISTS api_alias_lookup ON api_aliases(alias);
CREATE VIEW IF NOT EXISTS canonical_relations AS
 SELECT DISTINCT parent.entity_id AS src_id,child.entity_id AS dst_id,child.edge_kind
 FROM paths child JOIN paths parent ON parent.id=child.parent_id
 JOIN path_support s ON s.path_id=child.id JOIN capture_slots c ON c.id=s.slot_id
 WHERE c.status IN ('current','partial');

```
