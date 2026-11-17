# 数据模型

## 概述

SQLite schema 见 `jobtrace/schema.py`，当前 `schema_version=1`。写库使用显式 `BEGIN IMMEDIATE` 事务；只读复核台启用 `query_only`。

表：

| 表 | 用途 |
| --- | --- |
| `meta` | schema 版本 |
| `runs` | 每次采集运行的配置、状态、计数、诊断 |
| `jobs` | 岗位生命周期台账，每个 `(site_key, external_id)` 一行 |
| `job_snapshots` | 每次运行观测到的岗位内容哈希与完整 payload |
| `events` | 生命周期事件，按运行去重 |
| `raw_documents` | 原始响应内容对象元数据 |
| `run_documents` | 某次 run 抓取的 URL 与对象映射 |

## runs

关键字段：

- `run_id`：主键；可由 CLI 指定，否则按固定时钟、场景、配置指纹生成。
- `site_key`、`scenario`、`base_url`、`config_hash`：运行上下文。
- `started_at`、`finished_at`：规范 ISO-8601 UTC。
- `status`：`running`、`complete`、`incomplete`、`failed`。
- `is_complete`：冗余布尔标记，便于查询。
- `pages_fetched`、`list_cards`、`unique_jobs`、`details_ok`、`details_failed`、`duplicate_ids`、`parse_warnings`。
- `resumed`：是否从游标继续。
- `stop_reason`：`natural_end`、`detail_failures`、`pagination_loop`、`pagination_limit`、`detail_limit`、`resumed_partial`、`no_jobs`、`exception` 等。
- `notes_json`：失败摘要、警告、选择器缺失、请求统计与生命周期计数。

相同 `run_id` 使用 `INSERT OR IGNORE`。若对已完成 run 重放，生命周期引擎会跳过，避免重复事件。

## jobs

身份与跟踪字段：

- `job_uid = "job_" + sha256(site_key + 0x1f + external_id)[:24]`。
- `external_id`：合成岗位编号，如 `JT-0001`。
- `title`、`company`、`location_raw`、`city`、`district`、`salary_*`、`experience_*`、`employment_type`、`description`、`tags_json`、`detail_url`、`posted_at`、`source_status`。
- `status`：`open` / `closed`。
- `missing_streak`：连续完整运行未出现的次数。
- `reopen_count`：关闭后重新出现的次数。
- `first_seen_*`、`last_seen_*`、`closed_at`、`content_hash`、`updated_at`。

唯一约束为 `(site_key, external_id)`，保证重复列表卡片或重复运行不会产生重复岗位。

## job_snapshots

主键 `(job_uid, run_id)`。每次成功观测写入：

- `content_hash`：规范化 `JobObservation` payload 的 SHA-256。
- `payload_json`：稳定 key 顺序的完整观测。
- `observed_at`：该次运行的处理时间。

重复运行同一岗位会产生新的 snapshot，但不会产生重复 job 行。相同 run 不重复插入。

## events

事件类型与字段：

| 事件 | 典型字段 | 说明 |
| --- | --- | --- |
| `NEW` | `job` | 首次出现 |
| `CHANGED` | `title`/`company`/`location`/`salary`/`experience`/`employment_type`/`tags` | 跟踪字段变化 |
| `DISAPPEARED` | `presence` | 完整运行中一次或多次缺失 |
| `CLOSED` | `status` | 连续缺失达到阈值 |
| `REOPENED` | `status` | 已关闭岗位重新出现 |

唯一键为 `(job_uid, run_id, event_type, field)`，因此同一运行内重复处理不会重复插入事件。

`old_value` 与 `new_value` 以文本存储；岗位级事件字段为 `job` 或 `presence`。事件查询会 join `jobs` 以返回 `external_id`、`title`、`company`。

## raw_documents / run_documents

`raw_documents` 以 `sha256` 为主键，记录 `size_bytes`、`content_type`、`first_seen_at`。相同内容只保存一行元数据，对象文件也只存一份。

`run_documents` 主键 `(run_id, sha256, url)`，记录：

- `kind`：`list` / `detail`
- `http_status`
- `fetched_at`
- `relative_path`：如 `objects/ab/<sha256>.html`
- `size_bytes`

因此同一个对象可以被多次运行、多个 URL 引用；每次抓取仍保留独立证据记录。

## 内容寻址

对象路径：

```text
<raw_dir>/objects/<sha256 前两位>/<sha256>.html
```

`manifest.jsonl` 每行是一个 canonical JSON 的 `RawDocument`，按追加顺序保存。写入使用临时文件 + `os.replace`，保证对象原子落盘。读取时校验 SHA-256 与字节数。

## 幂等性

- 岗位使用唯一约束与显式 upsert，不依赖“先查再插”的竞态路径。
- 事件 `INSERT OR IGNORE`，同一运行同一字段只保留一个事件。
- 快照按 `(job_uid, run_id)` 去重。
- 原始对象按内容哈希去重，manifest 按抓取追加。
- run 生命周期：重复的已完成 run 不会再次应用生命周期。

## 报告投影

`report.py` 从数据库中读取：

- 最新 run、最近 20 次 run、最多 10000 个岗位与事件；
- 岗位台账与 open/closed 计数；
- 最新 run 的事件计数与原始文档数。

JSON 报告的 key 排序稳定；CSV 固定 18 列；JSONL 每行一个事件。所有时间字段使用 `iso_z()` 规范化为秒级 UTC。

## 只读访问

复核台每次请求只读打开数据库，不共享长连接。数据库缺失返回 HTTP 503；岗位不存在返回 404；所有写方法（POST/PUT/PATCH/DELETE）由 Flask 返回 405。

## 薪酬约束层（不入库、不改事件）

`JobObservation.compensation` 是内存中的 `CompensationPlan`，由详情页薪酬原文经
`comp_parser` 解析得到。它**不进入** `content_payload()`/`tracked_values()`，因此
jobs 表结构、content hash 与全部生命周期事件均与旧链路完全一致；`comp_report`
只通过只读 Store 方法读取 jobs 行与 `job_snapshots.payload_json` 中的薪酬原文，
用 Fraction 重新求解可行年度收入集合，绝不基于 `salary_min/salary_max` 排序。
语法、求解规则与两种比较合同见 [COMPENSATION.md](COMPENSATION.md)。
