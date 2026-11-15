# 配置说明

## 文件

- `configs/default.yaml`：站点、抓取、存储、生命周期与时钟配置。
- `configs/selectors.yaml`：列表页/详情页 CSS 选择器。

配置加载是严格的：未知 key、缺失 key、类型错误、越界数值都会抛出 `ConfigError`，不会静默忽略。

## site

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `name` | 站点键，写入岗位 UID 与 run | 非空字符串 |
| `base_url` | mock 站点根地址 | 只允许 `http(s)`、字面 loopback IP、无凭据/路径/查询 |
| `list_path` | 列表路径，如 `/jobs` | 必须以 `/` 开头 |
| `page_param` | 翻页参数名 | 非空字符串 |
| `page_size` | 页面大小元数据 | 1–100 |
| `max_pages` | 最大列表页数 | 1–1000 |
| `max_details` | 单次最多详情数 | 1–10000 |
| `allowed_hosts` | HTTP 主机 allowlist | 每项必须是 loopback 字面 IP |
| `allowed_ports` | HTTP 端口 allowlist | 1–65535 |
| `scenario` | 默认场景 | 对应 `fixtures/scenarios/<name>.json` |

`base_url` 的主机与端口必须同时存在于 allowlist 中。默认使用 `http://127.0.0.1:8765`。

## crawl

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `connect_timeout_seconds` | 连接超时 | 0.05–60 |
| `read_timeout_seconds` | 读取超时 | 0.05–120 |
| `max_retries` | 可重试失败的最多重试次数 | 0–10 |
| `retry_backoff_seconds` | 指数退避基数 | 0–10 |
| `max_response_bytes` | 单响应上限 | 1024–50,000,000 |
| `request_interval_seconds` | 成功后固定间隔 | 0–10 |
| `user_agent` | 请求 UA | 非空字符串 |

仅 `408`、`425`、`429`、`500`、`502`、`503`、`504` 与连接异常会重试；重定向永不跟随。

## storage

四个路径字段均支持相对路径。相对路径以配置文件所在目录为基准；若配置文件位于名为 `configs` 的目录，则上移到仓库根目录。

| 字段 | 默认值 |
| --- | --- |
| `database` | `var/jobtrace.sqlite3` |
| `raw_dir` | `var/raw` |
| `report_dir` | `var/reports` |
| `checkpoint` | `var/checkpoint.json` |

## lifecycle / clock

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `lifecycle.close_after_missing_runs` | 连续缺失多少次完整运行后关闭 | 1–365 |
| `clock.fixed_at` | 固定 ISO-8601 时间；`null` 表示系统时钟 | 可解析时间或 `null` |

固定时钟示例：`"2026-01-15T09:00:00Z"`。

## selectors

`selectors.yaml` 必须包含 `list` 与 `detail` 两个映射。列表选择器包含 `item`、`link`、`job_id_attr`、`title`、`company`、`location`、`salary`、`next_page`；详情选择器包含 `title`、`company`、`location`、`salary`、`experience`、`employment_type`、`description`、`posted_at`、`status`、`tags`。

`list.item` 不能是 `/`，以避免把整页当成一张卡片。所有选择器值都必须是非空字符串。

## 环境变量

| 环境变量 | 覆盖键 |
| --- | --- |
| `JOBTRACE_BASE_URL` | `site.base_url` |
| `JOBTRACE_SCENARIO` | `site.scenario` |
| `JOBTRACE_DB` | `storage.database` |
| `JOBTRACE_RAW_DIR` | `storage.raw_dir` |
| `JOBTRACE_REPORT_DIR` | `storage.report_dir` |
| `JOBTRACE_CHECKPOINT` | `storage.checkpoint` |
| `JOBTRACE_CLOCK` | `clock.fixed_at` |

`JOBTRACE_CLOCK=system`（环境变量）或 `--clock system`（CLI）均表示使用系统时钟。

## CLI 覆盖

通用参数：

```text
--config PATH        YAML 配置
--selectors PATH     选择器 YAML
--db PATH            SQLite 路径
--raw-dir PATH       原始证据目录
--report-dir PATH    报告目录
--checkpoint PATH    游标路径
--base-url URL       loopback 基址
--clock VALUE        固定时间或 system
```

环境变量先应用，CLI/代码覆盖后应用。覆盖键必须是存在的点号路径，例如 `site.allowed_hosts`。

当使用 `--base-url` 或 `demo` 的内嵌 mock 时，程序会自动把该 URL 的主机与端口写入 `site.allowed_hosts` / `site.allowed_ports`，但仍会先验证主机是字面 loopback IP。

## 配置指纹

加载完成后，完整配置与选择器会被规范化为稳定 JSON，并计算 SHA-256。指纹写入每个 run 的 `config_hash`，用于说明该次结果由哪份配置产生。改变时钟、选择器、阈值或 allowlist 都会改变指纹。

## 示例

```bash
python -m jobtrace collect \
  --with-mock \
  --scenario day2 \
  --run-id day2-001 \
  --clock 2026-01-16T09:00:00Z \
  --db var/day2.sqlite3 \
  --report-dir var/day2-reports
```

不要将 `base_url` 指向任何真实站点；非 loopback 地址会在配置校验阶段被拒绝。
