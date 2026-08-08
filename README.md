# Soccer Predict

面向 Codex 的足球赛前分析、T−30 临场复查和赛后复盘 Skill。项目包含可训练、可复现的时间衰减 Poisson/Dixon-Coles 比分基线，以及按联赛训练的半场/全场九分类联合模型。面向用户的 1X2、大小球、亚盘、总进球区间、双方进球、半全场和比分情景必须统一从同一个版本化比赛路径后验派生；角球继续使用独立模型，不与进球路径强行绑定。

当前发布版本：**3.11.0**（2026-08-09）。包/Skill 版本只描述本仓库发行；模型、归档、候选审计和调度策略的 artifact schema/policy 版本独立管理，不随发行版本自动改写。

3.11.0 将 cohort 关闭与 request/rescheduled/replaced/unavailable 事件追加放入同一把跨进程事件日志锁；关闭方持锁直到 immutable closure 和 closed pointer 均已落盘。外部 record manifest 只在 closure 成功后从其内嵌的已验证副本原子发布，竞争失败不会遗留阻塞重试的正式清单。正式主推 gate、模型、市场发布政策和结算口径均未调整。

3.10.0 将 cohort 关闭时间绑定到最后事件、最后归档与关闭时实际观测时间，完整保留微秒精度；正式聚合验证按冻结 package version 强制唯一 closure、record-manifest 与 denominator schema，拒绝重新封装的旧 schema 降级。正式主推 gate、市场发布政策和胜率口径均未调整。

3.9.0 关闭 cohort 事件自绑定与时间因果缺口：当前事件、请求、分母和记录清单必须绑定真实冻结 cohort/policy，最新改期或替换事件严格早于归档且事件时间不可倒退。`fundamental-evidence/3.0.0` 从注册来源适配器推导来源类别，从 canonical market identity 推导候选市场，并对方向证据执行至少 5 场样本门槛；这些证据仍仅用于 shadow，正式主推 gate 未放宽。中文赛事标签与 19 个模型 registry key 现明确分离；旧 schema 只读重放，不得启动或关闭当前 cohort。

> 概率与 EV 都是估计值，不保证盈利。请遵守所在地法律并理性使用。

## 安装

作为 Codex Skill 安装：

```bash
npx skills add BoDUD/Football-predictions
```

仓库同时提供可安装的本地诊断命令。Windows 基础安装会声明 `tzdata`，PNG/JPEG
渲染依赖 Pillow，按需安装 `render` extra：

```bash
python -m pip install .
python -m pip install ".[render]"
soccer-predict doctor --workspace .
```

`doctor` 检查 Python、Pillow、中文字体、IANA 时区、工作区及 `.codex` 写权限、
模型注册表和 scheduler/watchdog 文件与状态 JSON。它默认不联网、不安装或启用
watchdog，也不修改模型或归档；只有显式传入 `--network` 才执行最长 10 秒的外部
连通性检查。`--json` 提供稳定的机器可读结果，`--strict` 可把可选依赖警告也视为失败。

安装后可直接在 Codex 中使用：

```text
使用 soccer-predict 分析 2929664，可视化模式
复盘 2929664
查看 soccer-predict 战绩
```

升级只改变内部数据、模型和审计规则；上述自然语言使用方式与之前相同。

升级不会追改旧会话中已经发送的文字、图片或赛前冻结记录。同步或重新安装同一提交版本的 Skill 后，旧会话的新一轮可以调用新版渲染器；历史初盘/临场缺少当时有效联合制品时仍显示 `数据不足`，已复盘记录也不得重新结算或补填。待赛记录的赛事中文名只能通过实时抓取并校验 Titan 比赛页头部的来源证据修正，已复盘记录禁止再附加；修正不改模型预测，但会产生新的归档版本哈希以绑定可见赛事身份。具体迁移与 `attach-competition-evidence` 步骤见 [`references/image-output.md`](references/image-output.md#historical-archives-and-old-codex-tasks)。

## 工作流

1. **模型拟合**：用严格早于预测时间的历史比赛分别拟合联赛全场与半全场模型，固化数据、联赛、配置和模型哈希。
2. **初始分析**：核验比赛状态与时区，生成比分先验和统一比赛路径后验，抓取完整当前市场和基本面数据，输出可视化分析并在开球前归档。
3. **T−30 临场复查**：固定在开赛前约 30 分钟执行，不提前；重新核验首发、伤停与即时盘口，并明确“主推维持/变更”。Titan 没有双方首发时，继续检查官方比赛中心或俱乐部公告，再用带数字 `gameId` 的 ESPN 阵容页与精确 event ID 的 Sofascore 页面做通用回退；预测阵容绝不冒充确认首发，也不自动抓取条款禁止自动化访问的站点。
4. **赛后复盘**：只在 Titan 明确显示完场后结算，保存结果来源/采集时间、因果学习、统计与校准快照。

Titan 中文页时间默认按 `Asia/Shanghai` 解析，再转换为 Codex 环境中的用户时区。比赛状态始终以页面明确的未开场、进行中或完场标识为准。

默认输出为“简单图片 + 正常分析文字”。初盘和临场复查统一使用同一张 8 列表格：`编号`、`时间`、`赛事`、`主队 vs 客队`、`主推`、`联合首选情景总球`、`半全场`、`波胆`；日期、标题与阶段副标题全部从归档派生，同一张图禁止混合阶段、比赛日期或同一场的两个版本。`主推` 只显示通过全部门槛的正式主推；没有正式方向时固定显示 `无正式主推`，绝不拿独立 1X2 或总进球 marginal 首位补位。图片中的 `联合首选情景总球` 先显示由冻结联合事件 Rank 1 比分确定性映射的一个区间，再显示从完整归档联合分布重算的 Top-2 累计概率、其余情景质量和版本化不确定度；不显示 Rank 2 区间，也不拿独立总进球 marginal Top-1 替换联合区间。随附文字会另列 `总进球边际第一` 及概率，并明确它只用于边际审计、不替代联合情景。`半全场` 与 `波胆` 只展示冻结且通过校验的全局联合事件 Top 2，严格按联合概率降序。每一行的 HT/FT、全场比分与联合概率是不可拆分的同一事件；若两个事件拥有相同 HT/FT 但比分不同，保留两行并重复该标签。绝不展示第三项，也不按半场根节点补齐胜平负分支。系统内部仍完整计算并校验两个事件各自的 score-derived goal range，以及半场、1X2、全部总进球区间、BTTS、EV/edge 与证据审计。独立 1X2/总进球 marginal、独立 HT/FT Top 2 与独立无条件波胆 Top 2 只保留审计，不得占用或重排公开位置。正式主推后标红色 `★`；通过完整 v3 重放且非发布 Gate 全过的唯一观察可在表外发布状态区、随附文字或审计中标 `◇`，不占 `主推` 栏且不计战绩。严禁独立排列半全场与比分后暗示对应，也严禁按终场方向硬配比分或手填 fallback；没有通过校验并绑定指定 `analysis_stage` 与版本哈希的联合后验 artifact 时，`联合首选情景总球`、`半全场`、`波胆` 三栏必须整体显示 `数据不足`，同时主推栏仍如实保留正式主推或 `无正式主推`。

主表的 8 列与正式主推定义保持不变。主表下新增独立的“观察与发布状态”区，它不是第 9 列：仅当归档 v3 候选完整重放、非发布 Gate 全过且 `shadow_selected=true` 时才显示唯一 `◇ 观察首选`，同时列出 EV、edge、`不下注、不计战绩` 和数据/价值/政策/安全阻断；否则显示 `— 无可用方向`。初盘提示待 T−30 核验首发与即时盘口，临场提示已升级为正式主推、仍受阻、改变或消失。旧归档没有当时的 v3 审计时不会用现模型回填观察方向。

所有图片都禁止用 `…` 或三个点截断内容；必须通过语义换行、缩小至可读字号、增加行高或扩展画布显示完整文字。赛后复盘同样生成同风格图片，绑定最终有效赛前版本和已核验赛果，不得根据结果回改赛前结论；无主推复盘图片保留 `主推：无正式推荐（不结算、不计战绩）`，配套文字继续保留本场关键及联赛/累计战绩。项目不包含微信或其他聊天软件的自动发送、RPA、账号配置或外部消息投递能力，也不承诺胜率、收益或盈利。

## 推荐与统计口径

- 每个 active 赛前版本最多一个机器可读的 `primary_pick`；其他合格方向标为 `secondary`。
- 亚盘、大小球、总进球区间、双方进球、角球大小和角球让球进入同一个候选池，不预设亚盘或大小球优先。
- 候选必须先通过可复算审计：明确赔率格式、完整当前市场、no-vig 市场概率、来源与采集时间、公司数、统一模型 provenance、五态结算概率，以及服务端重算的 EV/edge。
- 当前市场只能在版本化严格前向校准证明相应条件化方法有效时进入联合后验；参与条件化的同一价格不能再作为一份独立 EV/edge 证据。未达到该条件时，市场仅用于审计比较。
- 市场证据按归档阶段设硬时效：初盘最多 60 分钟，`lineup-check` 最多 30 分钟；过期证据整字段关闭，不能以“临场”名义复用。纯 `model_only` 联合分布不依赖该盘口时效。
- 面向卡片的联合事件只能由通过哈希、时序、归一化、尾部质量和边际一致性校验的联合后验 artifact 产生；缺少 artifact 时不得从旧字段、文字结论或人工映射补齐。
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

目标 19 项赛事工作簿先经过严格转换，范围为巴甲、巴西杯、挪超、日职、美职联、五大联赛、葡超、荷乙、英联杯、韩 K1、瑞典超、芬超、欧冠、欧国联和亚冠；只有当前 manifest 和 registry 实际列出 19 项时才能称为完成重建。原始 XLSX、逐场 CSV 和最终模型默认保存在被 Git 忽略的本地运行目录：

```bash
python scripts/history_importer.py "C:\path\to\workbooks" \
  --output-dir .codex/soccer-predict/datasets/league-history-expanded \
  --source-timezone Asia/Shanghai \
  --as-of-date 2026-08-07

python scripts/htft_model.py fit \
  --input .codex/soccer-predict/datasets/league-history-expanded/brazil-serie-a-scores.csv \
  --output .codex/soccer-predict/experiments/htft/brazil-serie-a.json \
  --competition-key brazil_serie_a \
  --dataset-manifest-hash sha256:MANIFEST_HASH

python scripts/htft_holdout_evaluator.py \
  --dataset-dir .codex/soccer-predict/datasets/league-history-expanded \
  --include-opening-market \
  --output .codex/soccer-predict/evaluations/htft-fixed-seasons.json

python scripts/league_model_manager.py train \
  --dataset-dir .codex/soccer-predict/datasets/league-history-expanded \
  --model-dir .codex/soccer-predict/models/league-history-expanded-3.6.0-staging \
  --evaluation-artifact .codex/soccer-predict/evaluations/htft-fixed-seasons.json

python scripts/league_model_manager.py inspect \
  --model-dir .codex/soccer-predict/models/league-history-expanded-3.6.0-staging \
  --output .codex/soccer-predict/models/league-history-expanded-3.6.0-staging/inspection.json
```

半全场模型分别拟合半场和全场 Dixon-Coles 边际，再用训练窗的九格历史关联和 IPF 构成一致的 HH–AA 联合矩阵。Dixon-Coles 现在联合优化攻击、防守、主场优势和受动态安全边界约束的 `rho`，并保存收敛、迭代、目标函数、投影梯度和边界诊断；旧制品继续只读兼容。正式 evaluator 与 manager 将九格关联的指数时间半衰期冻结为 365 天，与全场边际的时间尺度一致，并把该值写入评估、模型与注册表后重放校验；缺失、非正或不同半衰期的制品会失败关闭。扩展导入覆盖 19 项赛事，并保存 Titan 展示的所有阶段所对应的 `format_version`、`phase_group`、`season_status` 和 `competition_regime`，供数据质量审计与评估切片使用。注册 manager 使用按赛事冻结的赛制白名单：常规联赛（含葡超、荷乙）只用 `regular`，巴西杯与英联杯分别只用自己的 `national_knockout_cup`，欧国联只用自己的 `national_team_league_and_knockout`；不同赛事始终分别拟合，不能把球队强度混在同一尺度。挪超赛程文本明确标记的保级/降级附加赛使用 `relegation_playoff` phase/regime，留在完整历史中审计，但不进入常规赛生产训练或晋级 cohort。荷乙比赛 `2871575` 只踢到第 88 分钟，KNVB 确定的 2-1 不是严格 FT90 训练目标，因此在足球与角球链路按不可变比赛 ID 隔离。其他未放行赛制保留排除计数与漂移证据。

面向用户的比赛情景不是 HT/FT 九格与全场比分两个边际榜单的拼接。联合路径 artifact 必须在同一状态空间中表示半场进球和下半场进球，使全场比分由路径相加得到，并验证其全场比分、半场边际和 HT/FT 九格边际全部与绑定的 canonical artifacts 一致。新 artifact 使用紧凑四维路径 kernel，并在 IPF 前执行全部 Hall 支持可行性审计；验证器会从 kernel 重建 HT、下半场、FT、HT/FT、所有派生市场及排序事件，任一篡改都关闭输出。归档中冻结且通过重建校验的全局联合事件 Top 2 是半全场与波胆栏唯一允许展示的两项，按联合概率降序，并保留每项自身不可拆分的 HT/FT、全场比分与联合概率；即使两项拥有相同 HT/FT，也不得去重。验证器为两个事件都从比分确定性映射总进球区间，但公共卡片只展示 Rank 1 对应的一个区间，并从完整归档联合分布重算 Top-2 累计概率、其余质量和版本化熵不确定度；普通文字另列审计用的总进球边际第一，明确不能替代联合区间。联合概率必须从路径单元求和，不能用两个边际概率相乘，也不能为满足终场方向而替换任一结果。独立 1X2/总进球 marginal、独立 HT/FT Top 2 与独立无条件比分 Top 2 继续只作内部审计。历史冻结的有效 artifact 继续只读兼容；新版渲染器可从其 Rank 1 冻结比分投影区间并从完整冻结分布重算集中度，但不会修改档案、artifact 或 archive hash。

```bash
python scripts/joint_scenario_model.py predict \
  --model registered-htft-model.json \
  --score-prediction canonical-score.json \
  --htft-prediction htft-prediction.json \
  --market-evidence timestamped-market-evidence.json \
  --expected-match-id MATCH_ID \
  --generated-at 2030-08-10T18:20:00+09:00 \
  --output joint-scenarios.json

python scripts/joint_scenario_model.py validate --prediction joint-scenarios.json
```

结构化市场证据必须绑定同一联赛、主客队、开球时间和比赛 ID。当前 schema 只将未通过严格前向融合校准的盘口保存为诊断证据，不改变模型概率，也不授权使用同一价格重复证明 EV。

不要在文档中固化某次导出的总行数、评估分数或各联赛 `candidate`/`shadow` 名单。当前准确总量以数据集 `manifest.json` 为准；log loss、Brier、Top-1/Top-2、覆盖率和基线差值以同源、通过校验的 evaluation artifact 为准；部署状态与分联赛 pair-gate 证据以当前 `registry.json` 为准。`candidate` 至少需要 100 场固定 2025 留出中的已知球队样本，并且该切片的 log loss、Brier 相对训练窗经验频率基线的均值及 paired-bootstrap 95% 置信区间上界都小于零；否则只能是 `shadow`。所有 `partial_as_of_*`（包括尚未完整的 2026 赛季）只进入 research/shadow，不能参与晋级证据；所有 HT/FT 注册模型仍必须保持 `formal_htft_eligible=false`，直到完整九路可执行价格和干净 live-forward 验证同时满足。

角球采用独立的角球数模型和注册表。现阶段角球 manager 只绑定历史数据、模型和 walk-forward 回测，`formal_corner_total_eligible` 与 `formal_corner_handicap_eligible` 必须为 `false`；赛前可展示 `◇` 观察，但不能使用 `★` 或写入正式主推。除非未来另有通过严格样本外验证的进球—角球联合模型，否则角球只能放在独立面板，不能与某条比分/半全场路径相乘、配对或写成同一证据链。只有未来 manager 绑定独立 strict live-forward 评估并显式放行相应 formal flag，同时当前盘口、证据与结算审计全部通过，才允许进入正式候选池。

同一份用户 Excel 可以保留角球审计数据并继续用于足球/HTFT 导入：赛事主表前 87 列仍是严格兼容区，后面只允许登记的 12 个角球审计列；`角球盘口` 与 `数据质量` 是只读辅助 sheet。`history_importer.py` 用前 87 列生成足球/HTFT 数据，并且只把追加列中的 `Titan比赛ID` 用作不可变比赛身份、重复和不可训练赛果排除；赛后角球、角球盘口和辅助 sheet 不会偷渡成同场特征。角球训练从采集器的 source-bound JSON 独立生成 CSV。Excel 中必须保留它实际使用的采集 bundle lineage；若后续合并训练 bundle 重新抓取了相同比赛，即使逐场比赛、阶段、90 分钟角球和排除状态完全一致，也不得把不同的采集时间或原始响应哈希宣称为同一 source lineage，必须单独记录语义对账结果：

```bash
python scripts/corner_history_dataset_builder.py \
  --input .codex/soccer-predict/corner-history-expanded/corner_history.json \
  --output-dir .codex/soccer-predict/datasets/corner-history-expanded \
  --as-of-date 2026-08-07

python scripts/corner_model_manager.py train \
  --input .codex/soccer-predict/datasets/corner-history-expanded/finland_veikkausliiga-corners.csv \
  --model-dir .codex/soccer-predict/models/corner-history-expanded-3.6.0-staging \
  --league-key finland_veikkausliiga \
  --league 芬超
```

完整扩展流程必须固定同一组 schedule 与 `as-of-date`，依次执行离线 schedule
`--check`/`--in-place`、可迁移断点的角球赛果采集、company 8 单公司研究价格采集、
source-bound dataset build、19 赛事顺序训练和统一 `inspect`，最后才导出工作簿并重跑
football/HTFT import、evaluation 与 registry。company 8 不能满足三公司门槛；
冻结 artifact 中的每条 `fetch_error` 都保持缺失并从训练排除，确定性的 header/fallback 身份或终态冲突则冻结为
带双来源 URL/hash/error 证据且不再重试的 `conflicting`；两类都绝不能补零。3.5 全量重训必须使用一个
事先不存在的 staging model 目录；旧 live registry 会被新 manager 有意拒绝，不能把第一条训练命令
指向旧目录。所有 19 项均通过 `inspect` 后，按 runbook 先把不可覆盖的备份移到同卷但不属于现役扫描范围的
`.codex/soccer-predict/model-archives/<timestamp>/`，再以目录重命名切换；失败时回滚，绝不自动删除旧制品。
训练命令不得并发写同一 `corner-registry.json`。命令、迁移规则、
安全切换和最终工作簿衔接见
[`references/expanded-history-runbook.md`](references/expanded-history-runbook.md)。

半全场价格诊断还必须同时取得同一当前可执行快照的完整 9 路赔率、来源和赛前采集时间，并由 ranker 自行去水；完整 9 路赔率和 `league_key` 缺一不可，部分赔率加外部概率不能通过资格检查。工作簿开盘价格没有精确采集时间且只有全场市场，只能作为研究代理，不能验证半全场真实 EV 或 ROI。

注册表预测会同时输出半全场 artifact、同源全场比分 artifact 和 bundle manifest，并拒绝数据/模型哈希、训练截止、联赛、部署状态或全场 1X2 边际不一致。完整、去水、带来源与赛前时间戳的半场 1X2 可显式锚定半场边际；在系统尚不能把外部全场边际同步写回同一 canonical 比分矩阵前，注册表路径会拒绝全场外部锚定，避免生成两个互相冲突的全场观点。未知球队默认报错；显式使用联赛均值 fallback 时必须单独标记。

暂停的 HT/FT 不再是“算完后丢弃”。`memory_store.py record` 可通过 `--htft-observation-model-file` 与 `--htft-observation-ranker-file` 固化矩阵、诊断 Top 2、pair mass、模型/预测/制品哈希和逐门槛失败原因；该诊断 Top 2 只服务内部校准，不能与独立比分榜单组成用户卡片。赛后只计算观察用 Top-1/Top-2、九分类 Brier 与 log loss，并在 `stats`/`calibrate` 输出 gate funnel；它始终不计主推、注额、胜负、收益或 ROI，复盘文字继续保留 `主推：无正式推荐（不结算、不计战绩）`。

正常初盘与临场归档现在默认要求有效的 `--joint-scenario-file`；`record` 会以完整分析模式拒绝缺失、过期或身份不一致的联合路径，避免先生成一张“数据不足”的完成图。有效联合模型存在但正式盘口门槛未通过时，图片固定显示 `无正式主推`，并展示由冻结 Rank 1 比分映射的联合首选情景总球、从完整联合分布重算的集中度/不确定度，以及冻结且通过校验的全局联合事件 Top 2；每项的半全场与代表波胆保持原始配对，它们不计正式主推、注额或收益。独立 1X2 与总进球 marginal 首位只能作为分布审计。通过完整 v3 重放且非发布 Gate 全过的唯一观察可在表外发布状态区、随附文字或审计中单独标注，仍不占主推栏。旧冻结版本若当时有有效联合制品，可从其 Rank 1 比分重渲染区间并重算集中度而不改档案或哈希；若当时没有联合制品，三栏仍如实显示 `数据不足`，不得用赛果或其他版本回填。

新的正常初盘与临场归档还必须传入当前 `candidate-evaluation/3.0.0` 与 `--require-candidate-evaluations`，把亚盘、大小球、半场、半全场、总进球区间、BTTS 和两类角球市场逐一记为已评估或明确不可用。候选制品的生成时间不得早于其使用的盘口快照或联合/角球上游模型；系统从冻结 source payload、活动版本及模型/证据绑定重算完整盘口、五态 EV/edge、门槛、信心排序和 shadow 选择。四分盘的 edge 按半赢/半输各半注折算，push 不进入有效赢亏质量。只因市场发布政策暂停而失败的候选仍可进入每场每市场最多一个的 shadow 样本，但永远不占主推、不下注、不计战绩或 ROI。赛后复盘会按最终比分重新结算并核对冻结诊断；改派生字段后重算自哈希、或仅改变 JSON 格式/字节哈希复制同场样本，都不能污染 `stats`/`calibrate`。单市场达到 20 个已结算 shadow 只触发人工模型/政策复核，不会自动解禁、调参或回改旧档案。旧 `candidate-evaluation/2.0.0` 仅用于历史隔离读取，不能进入新的活动 cohort 写入。

真正的“未触碰前向验证”由 `scripts/cohort_scope.py`、`scripts/forward_policy.py`、`scripts/source_evidence.py` 和 `scripts/forward_validation.py` 组成。先冻结用户请求赛事范围，再在任何分析前把每个用户请求写入 append-only、hash-chained 事件日志；结项时每个请求必须对应一条冻结预测记录或一条明确的 terminal-unavailable 处置，且预测记录中的 request-event hash 必须逐条重放一致。policy 同时冻结足球历史数据、角球历史数据、HT/FT 模型 registry、角球模型 registry，以及每个联赛实际注册的模型 hash，不能再用一个模糊的单 registry 代表全部模型。当前运行时只允许 `local-integrity-shadow-v2`，不能作为 promotion 证据；正式主推 gate、市场 observation-only 状态和阈值没有放宽。

活动 cohort 的每场归档必须同时提供 `source-evidence/2.0.0` 与 `fundamental-evidence/3.0.0`。后者从注册的 `adapter_id + host/domain + adapter parser version` 推导来源类别，并将内容寻址原始导出响应的 SHA-256 纳入 evidence；候选市场只能从 canonical market identity 推导，调用方不能另报不一致的 `market`。方向证据至少需要主客双方各 5 场同类样本，低于门槛会明确记为 `insufficient_sample_matches`。冲突首发、非首发攻击手和与所选方向矛盾的证据仍失败关闭；新规则仍为 shadow-only，调用方布尔参数不能自行解锁正式 gate。盘口共识价格仍只用于市场基线；如果 forward ledger 声称真实成交，必须另附 `execution-offer-evidence/1.0.0`，绑定具体 firm、地区、receipt、报价/接受时间、可下上限、实际 stake 和接受价格，且成交价不得优于已保存报价。同一 firm/account/receipt 在 cohort 内只能计一次，firm 接受价与决策时共识 no-vig 分开报告。当前 source/fundamental adapter 保存的是可见页面导出的原始 JSON 与 HTTP 元数据，并非通用 raw-HTTP body 抓取器；因此系统仍只声明本地可重放 shadow，不宣称已经解决外部可信时间戳、原始 HTTP、独立 closing snapshot 或 promotion。

当前活动 policy 是 `forward-policy/3.0.0`；kind-less 的 policy v2 与 v1 都只能做结构化历史重放。当前 record binding 为 `forward-policy-binding/3.0.0`/`3.1.0`，内含 `forward-provenance-binding/2.0.0`，显式写入 `cohort_kind`、`assurance_scope=local_integrity_only` 和 `promotion_evidence_eligible=false`。旧 binding 2.x/provenance 1.0 仍可验证既有不可变记录，但其旧 `untouched_confirmation_eligible=true` 只表示旧版赛前完整性，不表示可 promotion；这些记录在汇总中进入 defect quarantine，不能正式导出或续写承诺。observations v1 可以计算描述性统计，但完整性与 promotion 永远失败。当前本地 memory-store 也没有独立的赛前收盘快照重放适配器，因此真实 local shadow 的 execution/CLV/总体统计门槛保留 blocker；只有五态模型空间 proper-score 子门槛可以在冻结基线上如实评估。

内部历史字段 `confidence_score` 仅表示启发式的“综合稳定性排序分”，不是命中概率或经过校准的置信度；用户界面不得把它解释为概率。

```bash
python scripts/source_evidence.py build --source-file visible-page-export.json --output-dir .codex/soccer-predict/source-evidence
python scripts/source_evidence.py verify --evidence .codex/soccer-predict/source-evidence/MATCH-source-evidence.json
python scripts/fundamental_evidence.py build --source-file visible-fundamentals.json --output-dir .codex/soccer-predict/fundamental-evidence
python scripts/execution_evidence.py build --source-file accepted-firm-offer.json --output-dir .codex/soccer-predict/execution-evidence
python scripts/cohort_scope.py build --scope-id SCOPE_ID --competition-key LEAGUE_KEY --starts-at TIMEZONE_AWARE_ISO --output cohort-scope.json
python -m scripts.forward_policy --base-dir . --repo-root . freeze --dataset-manifest FOOTBALL_DATASET_MANIFEST --model-registry FOOTBALL_MODEL_REGISTRY --corner-dataset-manifest CORNER_DATASET_MANIFEST --corner-model-registry CORNER_MODEL_REGISTRY --cohort-scope-file cohort-scope.json --expected-final-merge-commit FINAL_MERGE_GIT_SHA --cohort-kind local-integrity-shadow-v2
python -m scripts.forward_policy --base-dir . --repo-root . start --policy-file POLICY_JSON --cohort-id COHORT_ID --cohort-kind local-integrity-shadow-v2
python scripts/memory_store.py --base-dir . close-forward-cohort --cohort-id COHORT_ID --closed-at TIMEZONE_AWARE_ISO
python scripts/memory_store.py --base-dir . export-forward-validation --cohort-id COHORT_ID --cohort-closure-file .codex/soccer-predict/forward-cohorts/COHORT_ID-closure.json --output forward-observations.json
python scripts/forward_validation.py --input forward-observations.json --output forward-validation.json
```

冻结政策要求工作树已经提交，因此本次代码修复本身不能作为未来效果证据；必须在最终 merge commit 上冻结 policy，再启动新的前向 cohort。外部可信时间戳服务可以不配置来运行 `local-integrity-shadow-v2`，但它不是 promotion 的可选增强：没有外部锚及其可验证适配器就不能冻结或启动 `promotable-confirmation-v2`，本地内容哈希与 Git 边界也不能冒充第三方时间见证。SQLite 迁移和更多赛前协变量仍属于后续工程，当前没有为了迎合 review 虚构完成状态。

完整输入契约见 [`references/history-workbook-data.md`](references/history-workbook-data.md)，半全场构造与选择规则见 [`references/half-time-full-time.md`](references/half-time-full-time.md)。
本地数据哈希、分联赛门槛、fallback 与赛制漂移的可执行核验见
[`analysis/htft_history_validation.ipynb`](analysis/htft_history_validation.ipynb)。

## 小样本 Guardrail

`stats`、`calibrate` 和独立 `gate-stats` 诊断输出最近 50/100 个不同比赛的候选漏斗。窗口按每场最新赛前归档时间取样，并对入窗比赛的所有冻结初盘/临场 v3 版本分阶段重放。待赛与已复盘都纳入收集/发布摩擦诊断，但该漏斗不是战绩或 ROI。旧版本缺少 v3 审计、重放失败或市场显式 unavailable 都保留为覆盖率/数据缺口；窗口不足时必须标记不完整，不得凭感觉放宽 Gate。

- EV 和模型边际必须为正，但都不能代替完整市场、统一模型和专属证据。
- 盘口与相关欧赔同时明显反向时，普通低 EV 方向降为观察。只有 EV ≥ 8%、边际 ≥ 4pp、至少 5 家公司且有独立阵容或基本面支持，才可成为正式方向或主推。
- 伤停表与确认首发冲突时，以确认首发为准，旧伤停不再作为让球或进球方向证据。
- 大小球降水不能单独构成主推依据，必须有多家公司一致性和进攻配置或机会质量支持。
- 新市场必须同时通过完整盘口、重新计算的 EV/边际、市场来源与时间、公司深度和独立证据校验；角球盘还要保存四分之一盘五状态概率。
- 单市场 20 个正式或 shadow 有效样本只是人工复核下限，不是自动解禁或调参依据；必须同时通过严格 walk-forward 的 log loss、Brier、校准、覆盖率和可执行市场检查。

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
├── scripts/joint_scenario_model.py
├── scripts/joint_path_kernel.py
├── scripts/public_market_outlook.py
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
├── scripts/review_card_renderer.py
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
python -m pip install -e ".[dev,render,notebook]"
python -m pytest -q
python -B -X utf8 <skill-creator>/scripts/quick_validate.py .
```

CI 在 Python 3.11/3.12 和 Windows 上运行测试，并自动汇总 `unit`、`property`、
`schema_contract`、`documentation_contract`、`integration_replay`、`live_canary`、
`e2e` 与 `uncategorized` 分类。没有相应测试的类别会如实显示 0，新文件没有明确分类时
进入 `uncategorized` 而不冒充单元测试；分类清单不等同于已具备真实 provider
或端到端覆盖。`soccer_predict/`、`scripts/` 与 `tests/` 全部执行 ruff lint/format；
CLI/doctor 另执行 mypy 和 80% 覆盖率门槛。HT/FT notebook 在 CI 中使用明确标记为非模型证据的
最小 fixture 真执行全部单元格；默认本地模式仍只验证 `.codex` 中的真实哈希制品。

## License

See [LICENSE](LICENSE).
