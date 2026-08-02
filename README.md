# Soccer Predict

面向 Codex 的足球赛前分析、T−30 临场复查和赛后复盘 Skill。项目包含可训练、可复现的时间衰减 Poisson/Dixon-Coles 比分基线，以及按联赛训练的半场/全场九分类联合模型；1X2、大小球、亚盘、总进球区间、双方进球和波胆仍统一从同一全场比分矩阵派生。

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

升级只改变内部数据、模型和审计规则；上述自然语言使用方式与之前相同。

## 工作流

1. **模型拟合**：用严格早于预测时间的历史比赛分别拟合联赛全场与半全场模型，固化数据、联赛、配置和模型哈希。
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

### 历史工作簿与半全场模型

供应的四个联赛工作簿先经过严格转换；原始 XLSX、逐场 CSV 和最终模型默认保存在被 Git 忽略的本地运行目录：

```bash
python scripts/history_importer.py "C:\path\to\workbooks" \
  --output-dir .codex/soccer-predict/datasets/league-history \
  --source-timezone Asia/Shanghai

python scripts/htft_model.py fit \
  --input .codex/soccer-predict/datasets/league-history/brazil-serie-a-scores.csv \
  --output .codex/soccer-predict/models/league-history/brazil-serie-a.json \
  --competition-key brazil_serie_a \
  --dataset-manifest-hash sha256:MANIFEST_HASH

python scripts/league_model_manager.py train \
  --dataset-dir .codex/soccer-predict/datasets/league-history \
  --model-dir .codex/soccer-predict/models/league-history

python scripts/htft_holdout_evaluator.py \
  --dataset-dir .codex/soccer-predict/datasets/league-history \
  --include-opening-market \
  --output .codex/soccer-predict/evaluations/htft-fixed-seasons.json
```

半全场模型分别拟合半场和全场 Dixon-Coles 边际，再用训练窗的九格历史关联和 IPF 构成一致的 HH–AA 联合矩阵。固定 2025 模型组件评估共 1,510 场，Top-1 为 30.07%，Top-2 为 48.48%。但 2025 与部分 2026 结果后来参与了最终两场景选择器的开发，因此它们不是 `probability_top2_v3_post_selection` 的最终端到端 untouched 验证；这些数字只能作为开发后模型组件证据，不能宣称提高了实盘胜率或 ROI。最终选择器是否有效，必须由未来从未查看、开球前固化的 live-forward 样本确认。

2025 按生产可识别球队拆分后，known-team 组为 1,370 场（log loss 1.91190、Top-2 49.42%），联赛均值 fallback 组为 140 场（log loss 2.00162、Top-2 39.29%）。注册表 manager 在生产预测中默认对未知球队报错；只有显式启用 fallback 才可输出，并必须单独标记，不能把总体数字当作默认生产表现。

`0.46` 只是选择器开发阶段的描述性模型两项概率和门槛，不是已确认置信门槛。2025 分联赛覆盖样本/命中为：巴甲 125/72（57.60%）、日职 66/32（48.48%）、挪超 110/63（57.27%）、MLS 208/114（54.81%）；对应 Wilson 95% 下界分别约 48.84%、36.85%、47.94%、48.02%，均未超过 50%。因此 ranker 必须接收 `league_key` 并显示该联赛证据，但当前不会给任何联赛“已确认置信”标签；缺少或不支持的 `league_key` 同样保持观察。

半全场价格诊断还必须同时取得同一当前可执行快照的完整 9 路赔率、来源和赛前采集时间，并由 ranker 自行去水；完整 9 路赔率和 `league_key` 缺一不可，部分赔率加外部概率不能通过资格检查。

注册表预测会同时输出半全场 artifact、同源全场比分 artifact 和 bundle manifest，并拒绝数据/模型哈希、训练截止、联赛或全场 1X2 边际不一致。完整、去水、带来源与赛前时间戳的半场 1X2 可显式锚定半场边际；在系统尚不能把外部全场边际同步写回同一 canonical 比分矩阵前，注册表路径会拒绝全场外部锚定，避免生成两个互相冲突的全场观点。

工作簿“开盘”代理的 2025 研究结果中，概率质量为 log loss 1.90470、Brier 0.81813；两项概率和达到 0.50 时 Top-2 命中 61.56%、覆盖 21.19%。因为这些是没有精确采集时间的全场 1X2 开盘代理，`0.50` 和 `61.56%` 只属于 evaluator 的研究对照，不能迁移到当前半场盘口、半全场实盘或注册表生产路径。

这 9,211 场仅覆盖巴甲、日职、挪超和 MLS。日职 2026 的 180 场记录属于区域分组特别赛制，标记为 `competition_regime=2026_vision_regional`；manager 的 `regular-only-production-v1` 训练会排除它们，evaluator 也不将它们计入正式 shadow 指标，只保留排除数量用于赛制漂移审计。供应数据没有韩K联样本，因此对韩K没有已验证的迁移收益：韩K仍走原有通用赛前分析并保持半全场 observation，不能引用上述四联赛命中率。

完整输入契约见 [`references/history-workbook-data.md`](references/history-workbook-data.md)，半全场构造与选择规则见 [`references/half-time-full-time.md`](references/half-time-full-time.md)。
本地数据哈希、分联赛门槛、fallback 与赛制漂移的可执行核验见
[`analysis/htft_history_validation.ipynb`](analysis/htft_history_validation.ipynb)。

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
<workspace>/.codex/soccer-predict/datasets/league-history/
<workspace>/.codex/soccer-predict/models/league-history/
```

真实历史、个人路径和本机校准文件不应提交到公共仓库。

隔离旧记录不等于删除：原有 25 场主推记录继续保留，`legacy_or_quarantined` 继续保存其描述性战绩和因果复盘，
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
├── analysis/htft_history_validation.ipynb
├── scripts/score_model.py
├── scripts/history_importer.py
├── scripts/htft_model.py
├── scripts/htft_ranker.py
├── scripts/htft_holdout_evaluator.py
├── scripts/league_model_manager.py
├── scripts/memory_store.py
├── scripts/plain_text_formatter.py
├── references/
│   ├── data-collection.md
│   ├── expanded-markets.md
│   ├── history-workbook-data.md
│   ├── model-validation.md
│   ├── plain-text-output.md
│   ├── prediction-framework.md
│   ├── half-time-full-time.md
│   └── review-framework.md
└── tests/
```

## 验证

```bash
python -B -X utf8 -m unittest discover -s tests -v
python -B -X utf8 <skill-creator>/scripts/quick_validate.py .
```

## License

See [LICENSE](LICENSE).
