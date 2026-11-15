# 岗位生命周期

## 定义

岗位生命周期由五个事件表达：

| 事件 | 触发条件 |
| --- | --- |
| `NEW` | 当前数据库中不存在该 `(site_key, external_id)` 岗位，且本次产生有效观测 |
| `CHANGED` | 已存在岗位的任一跟踪字段与上次台账值不同 |
| `DISAPPEARED` | 完整运行中，一个 open 岗位未出现 |
| `CLOSED` | 连续缺失次数达到 `close_after_missing_runs` |
| `REOPENED` | 已关闭岗位再次出现 |

跟踪字段：`title`、`company`、`location`、`salary`、`experience`、`employment_type`、`tags`。其中：

- `location` 使用原始地点字符串；
- `salary` 使用规范化薪资文本（例如 `25000-35000元/月·14薪`）；
- `tags` 按解析顺序以 `、` 连接；
- 比较时缺失值归一为空字符串。

## 状态机

```text
不存在 --NEW--> open
open --字段变化--> open + CHANGED
open --完整运行未出现--> open + DISAPPEARED, missing_streak=1
open --再次完整运行未出现且达到阈值--> closed + DISAPPEARED + CLOSED
closed --再次出现--> open + REOPENED, reopen_count += 1, missing_streak=0
```

关闭阈值默认 2：岗位必须连续在两个**完整运行**中缺失才会关闭。第一次缺失只增加 `missing_streak` 并产生 `DISAPPEARED`。

## 示例：baseline → day2

基线有 64 个岗位。`day2`：

- 删除 `JT-0003`、`JT-0017`；
- 修改 `JT-0005` 薪资、`JT-0008` 标题、`JT-0010` 地点；
- 新增 `JT-0065`、`JT-0066`。

第一次 day2 结果：

- `NEW=2`；
- `CHANGED=3`；
- `DISAPPEARED=2`（`JT-0003`、`JT-0017`）；
- `CLOSED=0`。

第二次 day2 结果：

- 两个岗位 `missing_streak` 从 1 升到 2；
- 产生 `DISAPPEARED` 与 `CLOSED`，`CLOSED=2`。

再跑 baseline：

- `JT-0003`、`JT-0017` 重新出现；
- `REOPENED=2`，`reopen_count=1`，状态回到 `open`；
- `JT-0065`、`JT-0066` 在 baseline 中消失，开始新的缺失计数。

## 不完整运行

以下情况标记运行不完整，并且**不执行**缺失/关闭处理：

- 列表翻页循环或触发页数上限；
- 详情数上限；
- 任一详情 HTTP 失败；
- 必填字段缺失导致详情无观测；
- 从游标恢复的部分运行；
- 发现岗位数为零；
- 其他未预期异常（run 记为 `failed`）。

因此 `malformed` 场景中 `JT-0012`、`JT-0022`、`JT-0031` 不会被误判为消失，所有岗位保持 `open` 且 `missing_streak=0`。

## 幂等与重放

- 每个运行只应用一次生命周期；若 `runs.finished_at` 已存在则返回 `skipped=True`。
- 事件唯一键 `(job_uid, run_id, event_type, field)` 防止同一运行重复写入。
- 同一内容重复观测仍会写快照，但不会产生重复 `CHANGED`。
- 岗位内容哈希基于规范化 payload，而不是原始 HTML；页面结构噪声不会制造字段变更。

## 变更判断细节

`JobObservation.tracked_values()` 产生七个比较值；`LifecycleEngine._row_tracked()` 从数据库行重建同形结构。逐字段比较后：

1. 写入 `CHANGED`；
2. 更新 `changed_events` 与 `changed_fields` 统计；
3. 更新岗位行的所有观测字段、`last_seen_run_id`、`last_seen_at`、`updated_at`，并把状态恢复为 `open`、缺失计数清零、`closed_at` 清空。

如果没有任何字段变化且没有重开，则计入 `unchanged`。

## 缺失处理细节

对每个数据库中 `open` 且本次未出现的岗位：

1. `missing_streak += 1`；
2. 写 `DISAPPEARED`，`new_value="missing_<streak>"`；
3. 若达到阈值，写 `CLOSED`，设置 `status=closed`、`closed_at=now`；
4. 否则保持 `open`，仅更新 `missing_streak` 与 `updated_at`。

`closed` 岗位不重复累计缺失，也不会产生活动事件，直到重新出现。

## 报告中的生命周期

最新 run 的 `notes_json.lifecycle` 保存：

```json
{
  "new_jobs": 2,
  "changed_events": 3,
  "disappeared": 2,
  "closed": 0,
  "reopened": 0,
  "unchanged": 59,
  "skipped": false,
  "changed_fields": {"location": 1, "salary": 1, "title": 1}
}
```

Markdown 报告展示事件总数与最新事件表；JSONL 每个事件一行；CSV 展示岗位当前状态、缺失计数与关闭时间。golden 测试固定了 day2 的事件顺序与值。
