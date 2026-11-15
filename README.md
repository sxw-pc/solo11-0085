# 职迹采（jobtrace）

**合规声明（请先阅读）**

- 本项目仅用于**本地合成数据与授权环境**中的工程评测。
- **不适用于任何真实招聘站点**，也不应接入真实业务数据或生产系统。
- **不采集个人简历、联系方式、账号、Cookie 或任何个人信息**。
- **不实现登录、验证码破解、反爬绕过、代理池、批量投递或自动申请**。
- 仓库内岗位、公司、地点、描述与 URL 均为虚构；运行期只访问 `127.0.0.1` 上的内置 mock 站点。
- 仓库不包含真实密钥、令牌、口令、私有证书、真实公司名、真实域名或真实地址。

职迹采是一个纯本地、完全离线、配置驱动的岗位元数据采集与生命周期审计示例。它从内置合成招聘站点抓取列表页和详情页，解析公开字段，保存原始 HTML 证据，幂等写入 SQLite，并追踪岗位的新增、变更、消失、关闭与重新开放。

## 快速开始

要求 Python 3.11+。在仓库根目录执行：

```bash
python -m pip install -e ".[dev]"
make test          # 全部测试，不依赖外部网络
make demo          # 启动 mock 站点与只读复核台，并跑一次采集
```

`make demo` 会打印：

- mock 招聘站点：`http://127.0.0.1:8765`
- 只读复核台：`http://127.0.0.1:8766`
- 本次运行 ID、场景与完整/不完整状态
- Markdown 与 JSON 报告绝对路径

默认输出位于 `var/`：

```text
var/jobtrace.sqlite3      SQLite 台账
var/raw/objects/          按 SHA-256 内容寻址的原始 HTML
var/raw/manifest.jsonl    追加式证据清单
var/reports/report.md     Markdown 报告
var/reports/report.json   完整 JSON 报告
var/reports/jobs.csv      岗位台账 CSV
var/reports/events.jsonl  最新运行事件 JSONL
var/checkpoint.json       翻页游标
```

演示进程默认常驻；`Ctrl+C` 结束。只跑一次并自动退出：

```bash
python -m jobtrace demo --no-serve --run-id demo-001
```

## 截图占位

> 截图占位：`docs/screenshots/` 预留用于后续补充 mock 列表页、岗位详情页、复核台总览与生命周期事件页的本地截图。当前仓库不附带二进制截图，以避免引入非源码资产。

## 命令

| 命令 | 作用 |
| --- | --- |
| `make demo` | 启动 mock 站点 + 只读复核台，跑一次 baseline 采集，打印地址与报告路径 |
| `make test` | 运行全部 pytest 测试，仅访问 loopback |
| `make collect` | 启动内嵌 mock，采集 baseline |
| `make report` | 从已有 SQLite 重新生成四种报告 |
| `make lint-size` | 检查总代码行与单文件 300 行上限 |
| `make clean` | 删除 `var/` 运行产物 |

常用 CLI：

```bash
python -m jobtrace collect --with-mock --scenario day2 --run-id day2-001
python -m jobtrace collect --base-url http://127.0.0.1:8765 --scenario malformed
python -m jobtrace report --db var/jobtrace.sqlite3 --out var/reports
python -m jobtrace serve-mock --scenario flaky --port 8765
python -m jobtrace serve-review --db var/jobtrace.sqlite3 --port 8766
```

场景也可通过 `JOBTRACE_SCENARIO` 选择；配置覆盖键使用点号形式，详见 [docs/CONFIG.md](docs/CONFIG.md)。

## 合成场景

| 场景 | 数据变化 | 预期观察 |
| --- | --- | --- |
| `baseline` | 64 条合成岗位，7 页列表 | 64 个岗位；7 个列表响应 + 64 个详情响应；完整运行 |
| `day2` | 新增 `JT-0065`/`JT-0066`，移除 `JT-0003`/`JT-0017`，修改薪资、标题、地点，含 1 张重复 ID 卡片 | 新增 2、变更 3、首次消失 2、重复 ID 1；仍未关闭 |
| `malformed` | 2 个详情 404、1 个必填公司缺失、1 张畸形列表卡片、若可选字段缺失 | 61 个有效观测、3 个详情失败；运行不完整，不处理消失/关闭 |
| `pagination_loop` | 末页“下一页”指回第 2 页 | 第 7 页后识别重复 URL，`stop_reason=pagination_loop`，不完整 |
| `flaky` | 列表页 2/5 各注入一次 503；详情 `JT-0007` 两次、`JT-0033` 一次 | 有限指数退避重试后完整成功 |

场景文件位于 `fixtures/scenarios/`，通过 `extends` 增量继承基线，避免复制大段数据。

## 关键行为

- **loopback 硬限制**：HTTP 客户端和配置层都要求字面 loopback IP；禁用环境代理、重定向、外部 DNS 与超大响应。
- **原始证据**：每个列表/详情响应按 SHA-256 写入内容寻址对象，并在 manifest 记录 URL、状态码、类型、抓取时间；同一内容只存一份对象。
- **幂等入库**：岗位身份为 `site_key + external_id` 的稳定哈希；重复运行不会重复建岗位，岗位快照与事件按运行唯一键去重。
- **生命周期**：事件为 `NEW`、`CHANGED`、`DISAPPEARED`、`CLOSED`、`REOPENED`；连续缺失达到阈值才关闭；不完整运行绝不标记消失或关闭。
- **可恢复翻页**：检查点保存下一页 URL、已访问页与完成状态；`--resume` 可从游标继续，并把本次运行标为不完整。
- **确定性**：时钟、端口、运行 ID 均可注入/固定；报告时间规范化到秒；测试使用固定时钟与隔离临时目录。
- **只读复核台**：仅提供 GET 查询与健康检查，POST 返回 405；数据库缺失时返回 503。

## 目录

```text
jobtrace/            采集、解析、存储、生命周期、报告、CLI
  mock_site/         Flask 合成招聘站点与故障注入
  review/            只读 Flask 复核台
configs/             默认配置与选择器
fixtures/scenarios/  5 个合成场景
fixtures/expected/   golden 期望
tests/               11 个测试文件，包含端到端与行数预算测试
docs/                设计、配置、数据模型、生命周期、合规、运行手册
```

## 测试与规模

```bash
make test
make lint-size
```

测试覆盖配置与 URL 防护、薪资解析、列表/详情解析、翻页循环、幂等、完整生命周期、原始证据、报告 golden、端到端 demo 与代码行数预算。

行数口径：`lint-size` 统计 `.py`、`.html`、`.yaml` 这类代码/配置源文件（含测试），要求总量 2500–4500 行；`.md` 文档不计入总代码量，但所有 `.py`、`.html`、`.yaml`、`.md` 文件均受单文件 300 行上限约束。

## 边界

本项目不是通用爬虫框架、分布式采集平台、代理池、反爬工具、简历解析器、推荐系统、投递 RPA、CRUD 后台或实时流处理系统。它只演示：

1. 在严格 loopback 约束下采集仓库内置的合成公开岗位元数据；
2. 解析、归一化、留档与幂等持久化；
3. 用可审计事件表达岗位生命周期；
4. 用确定性 fixture 与 golden 测试证明行为可复现。

任何真实站点接入、个人信息处理、登录/验证码/反爬对抗或批量投递均超出项目范围，且不应使用本项目实现。

## 更多文档

- [docs/DESIGN.md](docs/DESIGN.md)
- [docs/CONFIG.md](docs/CONFIG.md)
- [docs/DATA_MODEL.md](docs/DATA_MODEL.md)
- [docs/LIFECYCLE.md](docs/LIFECYCLE.md)
- [docs/COMPLIANCE.md](docs/COMPLIANCE.md)
- [docs/RUNBOOK.md](docs/RUNBOOK.md)
