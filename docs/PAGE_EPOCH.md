# 列表快照证据与完整性裁决

## 问题

翻页期间站点可能从一个合成列表快照切换到另一个。页 1 与页 2 都可能返回
`200`、都有正常的卡片与 `next_url`，但合并后的卡片不属于任何一个完整快照。
“翻到自然结束”因此不能证明列表完整。

本仓库把列表页采集证据建模为**快照代号（snapshot_id）内的有序页链**，并要求
整条链在 URL、序号、响应摘要与 `next_url` 关系上相互吻合，才承认某个列表快照完整。

## 合成页声明

每个列表页必须声明三项信息：

| 声明 | HTML 来源 | 含义 |
| --- | --- | --- |
| `snapshot_id` | `div.page-state[data-snapshot-id]` | 该页所属快照代号 |
| 当前页序号 | `div.page-state[data-page]` | 必须与该页 URL 的 `page` 参数一致 |
| 总页数 | `div.page-state[data-total-pages]` | 同一快照内必须恒定 |

选择器位于 `configs/selectors.yaml` 的 `list.page_state`、`list.snapshot_id_attr`、
`list.page_index_attr`、`list.total_pages_attr`。`list.prev_page` 绑定回链
`a.prev-page`。缺少声明会产生 `declaration_missing` / `declaration_invalid` 见证。

## 观测绑定

`parser.parse_list_page` 返回 `ListPage`，其中 `declaration` 保存三项声明。
`parser.page_observation` 把解析结果与实际响应（URL、HTTP 状态、原始字节的
SHA-256）绑定为 `PageObservation`：

- `response_digest`：URL + 摘要 + 状态 + 声明 + `next_url` 的稳定哈希；
- `item_digest`：卡片 payload（而非到达顺序）的哈希。

## 证据账本与链证明

`page_epoch.EvidenceLedger` 累积 `PageObservation`，按 `snapshot_id` 建立
`EpochChain`。一个 epoch **完整**当且仅当：

1. 页码集合恰为 `1..total_pages`，无缺口、无重复；
2. 每个相邻对的 `next_url` 确实指向下一页 URL（链相连）；
3. 所有页声明的 `total_pages` 一致；
4. 没有该 epoch 的任何冲突见证。

`coverage_ranges` 给出已证明的连续覆盖，缺口会切分段。自然结束（`next_url`
为空）本身不参与完整性判定。

## 冲突见证

以下情况保留冲突双方的响应摘要与来源，绝不按到达顺序覆盖：

| 见证类型 | 触发条件 |
| --- | --- |
| `content_changed` | 同一 URL 在不同抓取中字节不同 |
| `duplicate_page` | 同一快照同一序号出现两份不同内容 |
| `chain_conflict` | 回链、跳页、`next_url` 不指向下一页、`total_pages` 变化、末页仍前链 |
| `declaration_missing` | 缺少 `snapshot_id` 或页序号 |
| `declaration_invalid` | 声明自相矛盾（如总页数小于当前页） |

每条见证包含 `url`、`sha256`、`response_digest`、`snapshot_id`、`page_index`。
冲突页不会被并入 epoch 的页链。

## 两种采集合同

`page_contracts` 提供两个明确用途：

### `fixed_snapshot`（默认）

- 仅当**同一快照**的整条分页链齐备时输出完整岗位列表（`items`）；
- 否则 `complete=False`，`items=[]`，并在 `witnesses`/`segments` 中给出缺页与冲突见证；
- S1 页 1 + S2 页 2 的输入不会报为完整 S1 或完整 S2。

### `live_observation`

- 按 `snapshot_id` 保留局部观察，输出各自已证明的 `coverage_ranges`；
- 永不把不同代号的分段合并为完整岗位集合，`items` 始终为空；
- 局部观察**不得**推进岗位缺失/关闭；只有 `claims_all_jobs` 为真才允许生命周期完整处理。

对 S1 页 1 + S2 页 2 的同一输入：固定合同不输出完整列表；实时合同输出
S1 `[[1,1]]` 与 S2 `[[2,2]]` 两个分段。两者都不能声称找到了“所有岗位”。

即便后续拿到 S2 页 1，只有其 URL/序号/摘要/`next_url` 与先前 S2 页 2 确实相接时，
S2 才会补成完整快照；仅凭相同 `snapshot_id` 不足以拼接。

## 游标保存与恢复

`pagination.Cursor` 额外保存：

- `snapshot_id`、`contract`；
- `pages`：已验证页的摘要、序号、URL、`next_url`、`item_digest` 等。

游标与页证据**原子**写入（临时文件 + `os.replace`）。`jobtrace/resume.py` 在恢复时：

1. 把已接受前缀重放回账本；
2. 重新抓取每个已存页并逐字节比较摘要；
3. 校验序号、`snapshot_id` 与 `next_url` 关系；
4. 任一处不一致即放弃旧链、从第 1 页开始新分段，而不是把 `cursor.next_url` 当作快照证明。

因此“中断于页 1 后站点切换”或“中断后同一 URL 内容变化”都会在恢复时被识别为
分段/冲突，而非被静默拼接。

## 生命周期边界

本模型不改变岗位生命周期状态机。固定合同在恢复后若始终无法形成完整链，
本轮 `complete=False`，已发布岗位与事件保持原样、可复核；报告只把该轮列表标为
不完整。实时合同的分段观察同理不触发 `DISAPPEARED`/`CLOSED`。

## 运行

```bash
python -m jobtrace collect --with-mock --scenario baseline                    # fixed_snapshot
python -m jobtrace collect --with-mock --scenario baseline --contract live_observation
python -m jobtrace collect --resume --contract fixed_snapshot
```

测试：`python -m unittest discover -s tests -v`（或 `pytest`）。
