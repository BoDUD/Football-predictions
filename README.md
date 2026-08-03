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

默认输出改为“扫盘图片 + 正常分析文字”：图片集中展示比赛、最强方向、总进球、半全场和比分参考，正式主推后标红色 `★`，未过门禁的重点观察用 `◇`，两者不会混淆。文字补充概率、赔率、EV/edge、证据和风险，不再重复追加一份 `可复制纯文本版`；只有明确要求纯文字或复制版时才输出该块。赛后即使没有主推，仍保留 `主推：无正式推荐（不结算、不计战绩）`、本场关键及累计战绩。项目不包含微信或其他聊天软件的自动发送、RPA、账号配置或外部消息投递能力。

## 推荐与统计口径

- 每个 active 赛前版本最多一个机器可读的 `primary_pick`；其他合格方向标为 `secondary`。
- 亚盘、大小球、总进球区间、双方进球、角球大小和角球让球进入同一个候选池，不预设亚盘或大小球优先。
- 候选必须先通过可复算审计：明确赔率格式、完整当前市场、no-vig 市场概率、来源与采集时间、公司数、统一模型 provenance、五态结算概率，以及服务端重算的 EV/edge。
- 当前 `strict-oos-market-policy-v1` 将亚盘、半场、半全场、角球大小和角球让球设为观察市场；只有未来的版本化政策在干净前向样本证明校准后才能恢复正式主推。角球历史 walk-forward 只决定模型能否进入下一阶段，不能单独解禁角球主推。
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

供应的 14 项赛事工作簿先经过严格转换，范围为巴甲、挪超、日职、美职联、五大联赛、韩 K1、瑞典超、芬超、欧冠和亚冠；原始 XLSX、逐场 CSV 和最终模型默认保存在被 Git 忽略的本地运行目录：

```bash
python scripts/history_importer.py "C:\path\to\workbooks" \
  --output-dir .codex/soccer-predict/datasets/league-history-expanded \
  --source-timezone Asia/Shanghai \
  --as-of-date 2026-08-03

python scripts/htft_model.py fit \
  --input .codex/soccer-predict/datasets/league-history-expanded/brazil-serie-a-scores.csv \
  --output .codex/soccer-predict/models/league-history-expanded/brazil-serie-a.json \
  --competition-key brazil_serie_a \
  --dataset-manifest-hash sha256:MANIFEST_HASH

python scripts/htft_holdout_evaluator.py \
  --dataset-dir .codex/soccer-predict/datasets/league-history-expanded \
  --include-opening-market \
  --output .codex/soccer-predict/evaluations/htft-fixed-seasons.json

python scripts/league_model_manager.py train \
  --dataset-dir .codex/soccer-predict/datasets/league-history-expanded \
  --model-dir .codex/soccer-predict/models/league-history-expanded \
  --evaluation-artifact .codex/soccer-predict/evaluations/htft-fixed-seasons.json

python scripts/league_model_manager.py inspect \
  --model-dir .codex/soccer-predict/models/league-history-expanded \
  --output .codex/soccer-predict/models/league-history-expanded/inspection.json
```

半全场模型分别拟合半场和全场 Dixon-Coles 边际，再用训练窗的九格历史关联和 IPF 构成一致的 HH–AA 联合矩阵。扩展导入覆盖 14 项赛事，并保存 Titan 展示的所有阶段所对应的 `format_version`、`phase_group`、`season_status` 和 `competition_regime`，供数据质量审计与评估切片使用。保存阶段标签不等于已经为每个阶段训练独立模型：当前注册 manager 只用 `competition_regime=regular` 拟合，其他赛制保留排除计数与漂移证据。

不要在文档中固化某次导出的总行数、评估分数或各联赛 `candidate`/`shadow` 名单。当前准确总量以数据集 `manifest.json` 为准；log loss、Brier、Top-1/Top-2、覆盖率和基线差值以同源、通过校验的 evaluation artifact 为准；部署状态与分联赛 pair-gate 证据以当前 `registry.json` 为准。`candidate` 至少需要 100 场固定 2025 留出中的已知球队样本，并且该切片的 log loss、Brier 相对训练窗经验频率基线的均值及 paired-bootstrap 95% 置信区间上界都小于零；否则只能是 `shadow`。所有 `partial_as_of_*`（包括尚未完整的 2026 赛季）只进入 research/shadow，不能参与晋级证据；所有 HT/FT 注册模型仍必须保持 `formal_htft_eligible=false`，直到完整九路可执行价格和干净 live-forward 验证同时满足。

角球采用独立的角球数模型和注册表。现阶段角球 manager 只绑定历史数据、模型和 walk-forward 回测，`formal_corner_total_eligible` 与 `formal_corner_handicap_eligible` 必须为 `false`；赛前可展示 `◇` 观察，但不能使用 `★` 或写入正式主推。只有未来 manager 绑定独立 strict live-forward 评估并显式放行相应 formal flag，同时当前盘口、证据与结算审计全部通过，才允许进入正式候选池。

同一份用户 Excel 可以保留角球审计数据并继续用于足球/HTFT 导入：赛事主表前 87 列仍是严格兼容区，后面只允许登记的 12 个角球审计列；`角球盘口` 与 `数据质量` 是只读辅助 sheet。`history_importer.py` 只把主表前 87 列导入足球/HTFT，不会把赛后角球或辅助 sheet 偷渡成同场特征。角球训练从采集器的 source-bound JSON 独立生成 CSV：

```bash
python scripts/corner_history_dataset_builder.py \
  --input .codex/soccer-predict/corner-history-expanded/corner_history.json \
  --output-dir .codex/soccer-predict/datasets/corner-history-expanded \
  --as-of-date 2026-08-03

python scripts/corner_model_manager.py train \
  --input .codex/soccer-predict/datasets/corner-history-expanded/finland_veikkausliiga-corners.csv \
  --model-dir .codex/soccer-predict/models/corner-history-expanded \
  --league-key finland_veikkausliiga \
  --league 芬超
```

完整扩展流程必须固定同一组 schedule 与 `as-of-date`，依次执行离线 schedule
`--check`/`--in-place`、可迁移断点的角球赛果采集、company 8 单公司研究价格采集、
source-bound dataset build、14 联赛顺序训练和统一 `inspect`，最后才导出工作簿并重跑
football/HTFT import、evaluation 与 registry。company 8 不能满足三公司门槛；
`2026-08-03` 冻结快照的两条 `fetch_error` 保持缺失并从训练排除，绝不能补零。训练命令不得并发写同一
`registry.json`。命令、迁移规则和最终工作簿衔接见
[`references/expanded-history-runbook.md`](references/expanded-history-runbook.md)。

半全场价格诊断还必须同时取得同一当前可执行快照的完整 9 路赔率、来源和赛前采集时间，并由 ranker 自行去水；完整 9 路赔率和 `league_key` 缺一不可，部分赔率加外部概率不能通过资格检查。工作簿开盘价格没有精确采集时间且只有全场市场，只能作为研究代理，不能验证半全场真实 EV 或 ROI。

注册表预测会同时输出半全场 artifact、同源全场比分 artifact 和 bundle manifest，并拒绝数据/模型哈希、训练截止、联赛、部署状态或全场 1X2 边际不一致。完整、去水、带来源与赛前时间戳的半场 1X2 可显式锚定半场边际；在系统尚不能把外部全场边际同步写回同一 canonical 比分矩阵前，注册表路径会拒绝全场外部锚定，避免生成两个互相冲突的全场观点。未知球队默认报错；显式使用联赛均值 fallback 时必须单独标记。

暂停的 HT/FT 不再是“算完后丢弃”。`memory_store.py record` 可通过 `--htft-observation-model-file` 与 `--htft-observation-ranker-file` 固化矩阵、Top 2、pair mass、模型/预测/制品哈希和逐门槛失败原因。赛后只计算观察用 Top-1/Top-2、九分类 Brier 与 log loss，并在 `stats`/`calibrate` 输出 gate funnel；它始终不计主推、注额、胜负、收益或 ROI，复盘文字继续保留 `主推：无正式推荐（不结算、不计战绩）`。

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
<workspace>/.codex/soccer-predict/datasets/league-history-expanded/
<workspace>/.codex/soccer-predict/models/league-history-expanded/
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
├── scripts/corner_model.py
├── scripts/corner_model_manager.py
├── scripts/corner_ranker.py
├── scripts/corner_history_dataset_builder.py
├── scripts/titan_corner_history_collector.py
├── scripts/titan_schedule_snapshot_normalizer.py
├── scripts/titan_corner_odds_collector.py
├── scripts/memory_store.py
├── scripts/plain_text_formatter.py
├── scripts/prediction_card_renderer.py
├── references/
│   ├── data-collection.md
│   ├── expanded-history-runbook.md
│   ├── expanded-markets.md
│   ├── history-workbook-data.md
│   ├── image-output.md
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
