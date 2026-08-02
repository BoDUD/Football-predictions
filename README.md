# Soccer Predict

面向 Codex 的足球赛前分析、T−30 临场复查和赛后复盘 Skill。项目现在包含可训练、可复现的时间衰减 Poisson/Dixon-Coles 比分基线；1X2、大小球、亚盘、总进球区间、双方进球和波胆统一从同一比分矩阵派生。

> 概率与 EV 都是估计值，不保证盈利。请遵守所在地法律并理性使用。

## 安装

```bash
npx skills add BoDUD/Football-predictions
```

安装后可直接在 Codex 中使用：

```text
使用 soccer-predict 分析 2929664，可视化模式
复盘 2929664
查看 soccer-predict 战绩
```

## 工作流

1. **模型拟合**：用严格早于预测时间的历史比赛拟合带版本、数据哈希和模型哈希的基线。
2. **初始分析**：核验比赛状态与时区，生成统一比分矩阵，抓取完整当前市场和基本面数据，输出可视化分析并在开球前归档。
3. **T−30 临场复查**：固定在开赛前约 30 分钟执行，不提前；重新核验首发、伤停与即时盘口，并明确“主推维持/变更”。
4. **赛后复盘**：只在 Titan 明确显示完场后结算，保存结果来源/采集时间、因果学习、统计与校准快照。

Titan 中文页时间默认按 `Asia/Shanghai` 解析，再转换为 Codex 环境中的用户时区。比赛状态始终以页面明确的未开场、进行中或完场标识为准。

每次初盘、临场复查和赛后复盘都会附带 `可复制纯文本版`，保留主推或明确无主推、比分参考、本场关键及累计战绩等字段。项目不包含微信或其他聊天软件的自动发送、RPA、账号配置或外部消息投递能力。

## 推荐与统计口径

- 每个 active 赛前版本最多一个机器可读的 `primary_pick`；其他合格方向标为 `secondary`。
- 亚盘、大小球、总进球区间、双方进球、角球大小和角球让球进入同一个候选池，不预设亚盘或大小球优先。
- 候选必须先通过可复算审计：明确赔率格式、完整当前市场、no-vig 市场概率、来源与采集时间、公司数、统一模型 provenance、五态结算概率，以及服务端重算的 EV/edge。
- 当前 `strict-oos-market-policy-v1` 将亚盘、半场和半全场设为观察市场；只有未来的版本化政策在干净前向样本证明校准后才能恢复正式主推。
- 通过审计的候选按版本化政策排序；模型概率不能通过“安全率、EV、edge”被包装成三份独立证据。如果领先不稳健，就明确不下注。
- `lineup-check` 替换 active 版本时记录主推 `maintained` 或 `changed`，历史版本保留在 `revisions`。
- 当前战绩、准确率和 ROI 先报告 `strict_oos`：每场最终 active 主推最多计一次，且必须在开球前生成/归档并具备模型与市场审计证据。
- 仅结算每场最终有效主推；次推只作赛前参考，不记录命中/未命中，也不计算金额或 ROI。
- 最终 active 版本无主推的比赛不计入战绩、注额、收益或 ROI，但会作为机器可读的无主推观察样本进入联赛与全局学习。
- 观察候选和精确比分不计入主推命中率或 ROI。
- legacy、backfill、时间异常和曾被强制改写的记录单独隔离；它们仍可用于取证学习，但不得混入当前胜率或 ROI。

## 可复现模型

历史 CSV 至少包含 `date,home_team,away_team,home_goals,away_goals`：

```bash
python scripts/score_model.py fit --input history.csv --output model.json
python scripts/score_model.py predict --model model.json --home-team HOME --away-team AWAY --kickoff 2030-08-10T19:00:00+09:00 --output prediction.json --total over:2.25 --asian home:-0.75
python scripts/score_model.py backtest --input history.csv --output backtest.json --min-train-matches 200 --test-block-size 50
```

未知球队默认失败；只有显式指定 `--unknown-team-policy league_average` 才会回退并留下警告。训练与 walk-forward 评估规则见 [`references/model-validation.md`](references/model-validation.md)。

## 小样本 Guardrail

- EV 和模型边际必须为正，但都不能代替完整市场、统一模型和专属证据。
- 盘口与相关欧赔同时明显反向时，普通低 EV 方向降为观察。只有 EV ≥ 8%、边际 ≥ 4pp、至少 5 家公司且有独立阵容或基本面支持，才可成为正式方向或主推。
- 伤停表与确认首发冲突时，以确认首发为准，旧伤停不再作为让球或进球方向证据。
- 大小球降水不能单独构成主推依据，必须有多家公司一致性和进攻配置或机会质量支持。
- 新市场必须同时通过完整盘口、重新计算的 EV/边际、市场来源与时间、公司深度和独立证据校验；角球盘还要保存四分之一盘五状态概率。
- 单市场 20 个有效样本只是复核下限，不是自动解禁或调参依据；必须同时通过严格 walk-forward 的 log loss、Brier、校准、覆盖率和 ROI 检查。

## 本地数据

每个工作区的数据独立保存在：

```text
<workspace>/.codex/soccer-predict/history.json
<workspace>/.codex/soccer-predict/calibration.json
```

真实历史、个人路径和本机校准文件不应提交到公共仓库。

隔离旧记录不等于删除：`legacy_or_quarantined` 继续保存其描述性战绩和因果复盘，
也可从中提取经核验的赛果作为模型训练原料；但补录/时序不明的旧预测不得混入
新模型的严格样本外胜率、ROI 或参数晋级判断。

常用命令：

```bash
python scripts/memory_store.py --base-dir <workspace> pending
python scripts/memory_store.py --base-dir <workspace> stats
python scripts/memory_store.py --base-dir <workspace> calibrate --write
python scripts/plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind review
```

## 目录

```text
soccer-predict/
├── SKILL.md
├── agents/openai.yaml
├── scripts/score_model.py
├── scripts/memory_store.py
├── scripts/plain_text_formatter.py
├── references/
│   ├── data-collection.md
│   ├── expanded-markets.md
│   ├── model-validation.md
│   ├── plain-text-output.md
│   ├── prediction-framework.md
│   ├── half-time-full-time.md
│   └── review-framework.md
└── tests/test_memory_store.py
```

## 验证

```bash
python -B -X utf8 -m unittest discover -s tests -v
python -B -X utf8 <skill-creator>/scripts/quick_validate.py .
```

## License

See [LICENSE](LICENSE).
