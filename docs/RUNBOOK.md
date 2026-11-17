# 运行手册

## 环境准备

要求 Python 3.11+，工作目录为仓库根目录。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# POSIX:   source .venv/bin/activate
python -m pip install -e ".[dev]"
```

若依赖已就绪，可直接执行 Makefile 目标。

## 验证

```bash
make test
make lint-size
```

预期：

- pytest 全部通过，且只访问 loopback；
- `lint-size` 输出代码总行数（2500–6500）且无单文件超过 300 行。

## 一键演示

```bash
make demo
```

输出包括 mock 地址、复核台地址、run ID、运行状态、报告路径。浏览器访问：

- `http://127.0.0.1:8765/jobs?page=1`：合成岗位列表；
- `http://127.0.0.1:8765/scenario`：当前场景与可用场景；
- `http://127.0.0.1:8766/`：只读复核台总览；
- `http://127.0.0.1:8766/events`：生命周期事件；
- `http://127.0.0.1:8766/runs`：运行历史。

`Ctrl+C` 停止两个本地服务。

只跑一次并退出：

```bash
python -m jobtrace demo --no-serve --run-id demo-001
```

## 采集指定场景

内嵌 mock，一次完成：

```bash
python -m jobtrace collect --with-mock --scenario baseline --run-id base-001 \
  --clock 2026-01-15T09:00:00Z
```

```bash
python -m jobtrace collect --with-mock --scenario day2 --run-id day2-001 \
  --clock 2026-01-16T09:00:00Z
```

外部已启动的 mock：

```bash
python -m jobtrace serve-mock --scenario flaky --port 8765
# 另一终端
python -m jobtrace collect --base-url http://127.0.0.1:8765 \
  --scenario flaky --run-id flaky-001
```

同一数据库连续运行才会产生生命周期事件。默认数据库为 `var/jobtrace.sqlite3`。

## 恢复翻页

若上次列表抓取中断，`var/checkpoint.json` 存在未完成游标：

```bash
python -m jobtrace collect --base-url http://127.0.0.1:8765 --resume --run-id resume-001
```

恢复运行只抓取剩余页，不重复已抓详情；本次运行会被标记为 `resumed_partial` 且不完整，因此不会关闭岗位。检查点完成后会被标记 `completed=true`。

## 重新生成报告

```bash
python -m jobtrace report --db var/jobtrace.sqlite3 --out var/reports
```

输出：

```text
var/reports/report.md
var/reports/report.json
var/reports/jobs.csv
var/reports/events.jsonl
```

报告生成只读打开数据库，不修改台账。

## 只读复核台

```bash
python -m jobtrace serve-review --db var/jobtrace.sqlite3 --port 8766
```

数据库不存在时返回 503，并提示先运行采集。复核台不提供写操作。

## 场景切换

运行中的 mock 支持：

```bash
curl http://127.0.0.1:8765/__control/scenarios
curl -X POST http://127.0.0.1:8765/__control/scenario \
  -H "Content-Type: application/json" \
  -d "{\"scenario\":\"day2\"}"
```

注意：故障注入计数在切换场景后重置。curl 仅用于本地 mock 控制接口。

## 观察点

### baseline

- 7 个列表响应、64 个详情响应，共 71 个原始文档；
- 64 个岗位，全部 `NEW`；
- 完整运行，无警告。

### day2

- 65 张列表卡片，64 个唯一岗位，1 个重复 ID；
- 新增 2、变更 3、首次消失 2；
- 第二次 day2 关闭 2 个岗位。

### malformed

- 61 个有效观测，3 个详情失败；
- `details_failed=3`，运行不完整；
- 不产生 `DISAPPEARED` / `CLOSED`。

### pagination_loop

- 7 页后检测到重复 URL；
- `stop_reason=pagination_loop`，运行不完整；
- 64 个详情仍可解析。

### flaky

- 列表页 2/5 与详情页注入 503；
- 有限重试后 `complete=true`；
- 请求统计可查看尝试次数与重试次数。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `cannot bind mock site` | 端口被占用；换端口或停止占用进程 |
| `config_error` | 检查 YAML、allowlist、URL 是否字面 loopback |
| `unsafe_url` | 目标不在 allowlist，或使用了主机名/非 loopback 地址 |
| 复核台 503 | 数据库不存在；先执行采集 |
| 复核台 404 | `job_uid` 不存在 |
| 报告缺失 | 采集时加了 `--no-report`，可执行 `report` 子命令 |
| 运行不完整 | 查看报告的失败与警告、`notes_json` 或复核台运行页 |
| 生命周期未关闭 | 阈值可能大于 1，需要连续完整运行缺失 |

## 清理

```bash
make clean
```

删除 `var/`（数据库、原始证据、报告、游标）。清理前确认不需要保留审计产物。

## 测试隔离

pytest 使用系统分配的临时端口与 `tmp_path` 数据库，不读写仓库 `var/`。测试中的故障注入通过传入 `sleep=lambda _: None` 跳过真实退避等待。

## 验收清单

- [ ] `make test` 全部通过；
- [ ] `make lint-size` 通过；
- [ ] `make demo` 打印两个 loopback 地址与报告路径；
- [ ] 报告四个文件均存在；
- [ ] 复核台 GET 页面可访问，POST 返回 405；
- [ ] 默认配置与场景数据无真实域名/个人信息；
- [ ] `day2` 的 golden 事件与期望一致。
