# 设计说明

## 目标

职迹采演示一条可审计的离线增量采集链路：

```text
配置/allowlist
    → 列表翻页
    → 详情抓取（有限重试、故障隔离）
    → 解析与归一化
    → 原始响应内容寻址留档
    → SQLite 幂等入库
    → 生命周期事件计算
    → Markdown/JSON/CSV/JSONL 报告
    → 只读复核台
```

项目刻意缩小范围：不处理登录、验证码、代理、分布式调度或真实用户数据。所有 HTTP 目标只能是配置 allowlist 中的 loopback 地址。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `config.py` | YAML 严格校验、点号覆盖、环境变量、路径解析、配置指纹 |
| `clock.py` | 系统时钟、固定时钟、ISO-8601 解析与秒级规范化 |
| `http_client.py` | loopback 守卫、超时、有限重试、响应大小上限、请求统计 |
| `pagination.py` | 下一页归一化、最大页数、重复 URL 检测、带页证据的 JSON 游标 |
| `page_epoch.py` | 快照代号内的页链证据、缺口/冲突见证、覆盖范围 |
| `page_contracts.py` | `fixed_snapshot` / `live_observation` 两种完整性裁决 |
| `resume.py` | 恢复时逐字节重验游标前缀 |
| `crawler.py` | 列表/详情编排、重复 ID 去重、故障隔离、完整运行判定 |
| `parser.py` | CSS 选择器解析、缺失统计、畸形卡片隔离 |
| `normalize.py` | 空白/NFKC、地点、经验、雇佣类型、标签清洗 |
| `salary.py` | 中文薪资表达式转月薪 CNY |
| `fingerprint.py` | 岗位 UID、内容哈希、配置哈希 |
| `rawstore.py` | SHA-256 内容寻址对象与追加式 manifest |
| `store.py` | SQLite schema、事务、幂等写入、查询 |
| `lifecycle.py` | NEW/CHANGED/DISAPPEARED/CLOSED/REOPENED 事件 |
| `report.py` | 确定性 Markdown/JSON 与 CSV/JSONL 导出 |
| `pipeline.py` | 单次运行的编排与失败记录 |
| `commands.py` / `cli.py` | demo、collect、report、serve 子命令 |

## 一次运行的时序

1. 加载 YAML，应用环境变量与 CLI 覆盖，校验 loopback allowlist，计算配置指纹。
2. 选择或生成运行 ID，写入 `runs` 初始记录。
3. 从第一页或游标开始抓取列表；每页响应先留档，再解析。
4. 对唯一岗位 ID 抓取详情；单个详情失败不会中断其他详情。
5. 记录所有原始文档元数据；完整运行才进入缺失/关闭处理。
6. 在单个事务内执行岗位 upsert、快照与生命周期事件。
7. 标记 run 完整/不完整及停止原因，生成报告。

## 失败策略

- **列表页 HTTP 失败**：有限次重试后终止本次列表遍历，运行不完整。
- **详情页 HTTP 失败**：记录失败并继续其他详情，运行不完整。
- **必填字段缺失**：该详情不产生观测，记录失败，运行不完整。
- **可选字段缺失**：保留观测，记录选择器缺失与警告。
- **翻页循环**：识别重复归一化 URL 后停止，`stop_reason=pagination_loop`。
- **页数/详情数上限**：达到配置上限后停止并标记不完整。
- **进程异常**：run 以 `failed` 收尾并记录错误类型；已有完整 run 的重放不会被覆盖生命周期。

## 完整运行判定

只有同时满足以下条件才允许处理“缺失岗位”：

- 列表遍历没有被循环、页数上限、详情上限或致命抓取错误截断；
- **某一 `snapshot_id` 的页 1 到终页形成无缺口、无重复、`next_url` 相连的整链证明**
  （自然结束不能替代该证明，见 `docs/PAGE_EPOCH.md`）；
- 采集合同为 `fixed_snapshot`；`live_observation` 只产出分段局部观察，永不完整；
- 没有详情抓取/解析失败；
- 不是从游标恢复的部分运行；
- 发现了岗位且至少产生一个有效观测。

完整运行示例：`baseline`、`flaky`。不完整示例：`malformed`、`pagination_loop`、
恢复运行、翻页期间快照切换。这样可避免因临时故障或跨快照拼接误关闭岗位。

## 确定性

- 测试与 golden 使用 `FixedClock`，时间固定为 `2026-01-15T09:00:00Z` / `2026-01-16T09:00:00Z`。
- 运行 ID 可由 `--run-id` 指定；未指定时由固定时钟、场景与配置指纹前缀生成。
- mock 服务端口可设为 `0` 由系统分配；测试读取实际端口。
- 故障注入按“计数消耗”执行，不使用随机数。
- 报告 JSON 按 key 排序，时间截断到秒；CSV 使用固定列顺序与 `\n` 行尾。
- 测试将数据库、原始证据、报告与游标隔离在 `tmp_path`。

## 安全与合规设计

- 配置层拒绝非 loopback 主机、非字面 IP、凭据 URL、查询/片段以及 allowlist 之外的主机/端口。
- HTTP 客户端在发请求前再次执行 URL 守卫；`trust_env=False` 防止环境代理；`allow_redirects=False` 防止跳转出站。
- 响应按流式读取并限制字节数；超限即失败。
- mock 页面只包含虚构岗位、虚构公司名与合成描述，不包含简历、联系方式或账号。
- 复核台只读打开 SQLite，并启用 `PRAGMA query_only`。

## 取舍

- 使用 SQLite 而非外部数据库，保证一条命令可离线复现。
- 使用追加式 manifest 而非复杂对象存储，便于人工审计和测试哈希。
- 使用显式选择器配置，展示配置驱动解析；不追求对任意站点泛化。
- 使用有限重试而非队列/调度器，保持故障语义可测试。
