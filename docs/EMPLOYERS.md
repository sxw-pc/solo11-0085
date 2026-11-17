# 雇主集中度审计（单次完整采集）

`jobtrace concentration` 针对**一个显式选择的完整 run**，回答“这一批岗位由哪些雇主
发布、集中到什么程度”。分析只读：不改写 `job_uid`、原始 `company`、生命周期事件或
任何已存快照，也不把跨站相似岗位合并为一个招聘需求。

## 受控雇主注册表

`configs/employers.yaml` 是本地合成注册表（仅虚构实体），带 `registry_version` 版次：

- `employers`：法律雇主 ID、法定名、可选的管理集团 `group`。
- `groups`：管理集团 ID 与名称。
- `aliases`：展示名 → 法律雇主，附 `valid_from`/`valid_to`（含端点）。同名在不同
  日期可指不同实体；改名（不同展示名）仍可指同一实体。展示名与采集端一样做
  NFKC/空白归一化，保证与快照中的公司文本处于同一空间。
- `mutex`：两两展示名的受控互斥关系——同一 run 内二者不得解析为同一法律雇主。

同一展示名与同一雇主的重叠有效期规则视为注册表冲突，加载即报错。

## 计数语义

- 查询必须给出 `--run-id`，且该 run 必须 `status=complete` 且 `is_complete`；
  incomplete/failed/不存在的 run 不当作完整市场快照，只输出原因与观测计数。
- 每个独立 `job_uid` 在该 run 的一份岗位观测记一次“刊登数”；公司文本取自
  `job_snapshots` 的原始载荷，**不**从当前 `jobs` 行回写旧 run。
- 同一雇主在多个站点的岗位都是独立刊登，但不推断它们是同一岗位实体。
- 未知/冲突公司名、快照缺公司字段、注册表未覆盖、详情观测缺口（coverage gap）
  都会使结果变为 `unresolvable`：输出原因与已知/未知刊登数，HHI 置空；未知雇主
  **不**被各自当成互不相关的公司计入。

## HHI 可行区间与见证

解析以“展示名 × 观测日期”为单位做**全局**指派：同一别名在一个 run 内只能指向
一个法律雇主，且所有指派共同满足互斥关系。对所有可行指派分别计算两种有业务授权
的统计合同：

- `legal_employer`（法律雇主合同）：按法律雇主聚合份额。
- `management_group`（集团管控合同）：按管理集团聚合（无集团雇主自成一组）。

每个合同给出 HHI（份额平方和）的可行最小/最大值、分母（刊登总数）、两端的组织
归属与份额，以及完整的 `job_uid → 雇主` 指派见证。共享候选雇主的多个展示名不会
被逐岗位拼接局部最优——例如「甲牌」「乙牌」同日均可指 A 或 B 且互斥时，逐岗位
拼接会得到 0.34，而全局可行区间是 `[0.38, 0.46]`。

## 版次与回读

结果 JSON 记录 `registry_version`、`rules_version` 与输入指纹（快照内容哈希）。
注册表改版或后续 run 中公司改名后，旧 run 仍按其原始快照与当时注册表版次回读；
分析规则升级只产生新的分析结果，不会在站点岗位上伪造 `CHANGED` 事件。

## 用法

```bash
python -m jobtrace collect --with-mock --scenario baseline --run-id run-1
python -m jobtrace concentration --run-id run-1
# 输出 concentration.md / concentration.json 到报告目录
```
