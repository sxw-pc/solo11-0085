# 技能经验资格查询（离线 · 合成数据）

`jobtrace/skill_exp.py`（数据模型）与 `jobtrace/skill_eval.py`（求值引擎）提供完全离线、
无个人信息的技能经验资格查询。输入只能是测试中构造的**虚构履历**与**受限岗位要求**；
查询为只读，不修改采集台账的岗位生命周期事件，也不写入站点内容指纹
（`JobObservation.content_payload` 的字段集合保持不变，有测试守护）。

## 受限岗位要求表达式

只接受明确的 AND 合取与各技能最低整月数：

```
Python>=18 AND SQL>=12
Python至少18个月且SQL至少12个月
```

- 连接词仅 `AND`（大小写不限）或 `且`；不接受 `OR`、逗号、范围或年限。
- 每个子句为 `技能 >= 正整月数`（也接受 `≥`、`至少`、`个月`/`月`/`months` 写法）。
- 同一技能重复出现、月数为 0、空表达式均抛出 `RequirementError`。
- 岗位文本里的“3-5 年”“应届”“不限”仍由 `normalize_experience` 归一为
  min/max 年区间，**不会**被解释成多技能要求；把它们传给 `parse_requirement`
  会显式报错。

## 虚构履历模型与处置规则

每段履历为：半开日期区间 `[start, end)`（不含 end 当日）、技能集合、每月投入比例
`(0, 1]`、可选标签。处置规则全部写入审计 `issues`：

| 情形 | 处置 | issue code |
| --- | --- | --- |
| 跨月截断 | 按当月覆盖天数/当月天数折算（`Fraction` 精确） | — |
| 同月多段并存 | 当月可计总投入上限 1 FTE，超出按 1 计 | `month_capped` |
| 完全重复的记录 | 去重，保留首条 | `duplicate_entry` |
| 起止倒置或空区间 | 剔除 | `inverted_dates` |
| 无技能 | 剔除 | `empty_skills` |
| 比例不在 (0, 1] | 剔除 | `invalid_ratio` |

技能没有任何可核实投入时，子句与结论均为 `insufficient_evidence`（证据不足），
不断言“零年经验”。

## 两种合同（调用方必须显式选择）

- **`exposure`（接触经验）**：同一个月可为 Python 与 SQL 各记一次接触，只要当月确有
  该技能投入；各技能独立计数。
- **`dedicated`（专属投入）**：每月 1 FTE 容量须在技能间分配，同一个 0.5 投入不能
  同时完整满足两项要求。判定所有技能下限是否存在一组**全局可行分配**
  （分数精确的最大流），不按子句顺序贪心。失败时输出容量冲突见证：需求合计、
  可分配上限、缺口、饱和月份、多技能争用月份与说明。

示例：履历 R 为 18 个月连续全职、每月兼用 Python 与 SQL。对 `Python>=18 AND SQL>=12`，
接触合同合格（18/18 个月）；专属合同不合格——总需求 30 个月超过 18 个月容量，
见证缺口 12。履历 S 中各技能个体上界均为 3 个月，但只有 2024-03 一个共享月可调度，
`Python>=3 AND SQL>=3` 的专属合同给出缺口 1 的冲突见证，不会按两个局部通过报合格。

## 只读查询 / 报告入口

```python
from jobtrace.skill_eval import evaluate, evaluate_query
from jobtrace.skill_exp import make_entry
from jobtrace.report import render_qualification_markdown

report = evaluate([make_entry("2024-01-01", "2025-07-01", ("Python", "SQL"), 1)],
                  "Python>=18 AND SQL>=12", contract="dedicated")
print(report.verdict, report.witness)
print(render_qualification_markdown(report))   # 子句审计 + 见证 + 履历处置说明
```

本地合成输入可存为独立 JSON（不写进站点内容指纹），经 CLI 查询：

```json
{"requirement": "Python>=18 AND SQL>=12", "contract": "dedicated",
 "resume": [{"start": "2024-01-01", "end": "2025-07-01",
             "skills": ["Python", "SQL"], "ratio": 1, "label": "R-全职"}]}
```

```bash
python -m jobtrace qualify --input query.json          # Markdown 审计报告
python -m jobtrace qualify --input query.json --json   # 机器可读 JSON
```

审计明细包含每项要求用到的月份与履历段（`seg-id`），不只是通过布尔值。
