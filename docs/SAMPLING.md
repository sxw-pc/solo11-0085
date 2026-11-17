# 有界详情抽样

当 `max_details` 小于列表发现数时，`jobtrace/crawler.py` 不再简单地取
`items[:max_details]`。它先用本轮完整、去重后的列表卡片建立**抽样框**，再按
两种业务授权合同之一选择详情，并把整个决策过程写入运行的 `notes_json`，由
`report.py` 呈现。无论哪种合同，本轮都不会因抽样而变成完整运行；未抓详情的
岗位不参与任何“消失/关闭”判定。

## 抽样框

`jobtrace/sampling.py` 的 `SamplingFrame` 固定：

- `run_id` 与去重后的卡片集合（`external_id`、标题、公司、地点、薪资）；
- 列表页响应的 URL 与 SHA-256（`list_document_digests`）；
- 列表卡片数（含重复）与重复 ID 集合；
- 分层字段固定为列表可见的 `company`。

`digest = sha256(canonical_json(卡片集 + 列表响应哈希 + 重复 ID))`。它不含端口或
抓取时间，因此同一页内容在重跑时得到同一权重；换页内容则必然改变 digest。
`audit_selection` 会核对该摘要与分层大小，不一致时给出
`frame digest differs ...`，旧样本权重不得沿用。

## 两种合同

调用方通过 `SamplingRequest` 注入：

```python
SamplingRequest("targeted", target_ids=("A1", "A2"))
SamplingRequest("probability", seed=20260115)
```

定向合同只抓明确授权的卡片 ID（按授权顺序，受预算上限约束），未知 ID 记入
`unresolved_target_ids`，被预算推迟的 ID 记入警告。它报告所选 ID 的真实字段
与未覆盖清单，`estimate` / `bounds` 为 `None`，审计拒绝携带任何入样概率。

概率合同按 `company` 分层：

1. 预算 ≥ 层数时，每层先分配 1；
2. 剩余预算逐张分给“剩余容量”最大的层（同容量按层名排序），不超过层大小；
3. 预算 < 层数时，按层大小降序（同大小按层名）给前 `budget` 层各 1，其余层
   入样概率为 0；
4. 每层内用 `random.Random(f"{seed}:{frame.digest}:{层名}")` 无放回抽样。

每张卡片的入样概率为 `allocation / stratum_size`，与种子、分配规则、摘要一起
持久化。重跑同一种子得到同一选择；换种子不会复用旧权重。

## 估计、界限与缺失

`jobtrace/sampling_eval.py` 把详情结果归为 `ok` / `fetch_error`（含 404）/
`missing_required` / `error`。**404、必填字段缺失、未知 `employment_type` 都按
`unknown` 处理，绝不当作“非实习”。**

- 已验证个数、已验证实习数、失败/未知数分别计数；
- 每层可证界为 `[已证实习数, 已证实习数 + (层大小 - 已验证数)]`，整页界为各层之和；
- 仅当预算覆盖整帧、每层入样概率为正、且所选详情全部验证时，才输出设计加权
  估计 `Σ 层大小 × (层内实习数/层内样本数)`，并标注
  `design-weighted estimate; not a verified count`；
- 每层只抽一张时，层内方差不可估，因此不给方差或置信区间，并写明原因；
- 定向合同、预算低于层数、或出现失败/未知时，
  `claims.whole_page_unbiased_proportion=false`；覆盖整帧且全部验证的概率样本
  才会给出设计加权估计与 `=true` 的同页无偏声明。

## 报告字段

`report.py` 的 JSON `latest_run.sampling` 与 Markdown“有界详情抽样”一节区分为：

`list_discovered`（列表发现数）、`list_unique`（去重样本框）、
`duplicates_removed`、`detail_attempted`（详情尝试数）、`detail_succeeded`、
`detail_failed`、`verified`（已验证个数）、`verified_positive`、`unknown_field`、
`not_attempted`，以及 `estimate`、`bounds`、`variance_reason`、
`claims.coverage_scope`、`claims.whole_page_unbiased_proportion`。

同时保留 `contract`、`seed`、`allocations`、`uncovered_strata`、
`uncovered_ids` 与 `audit`（只读复算结果），让复核人员能判断：所看岗位是定向
核查对象，还是概率样本；覆盖结论是否只适用于所选子集。
