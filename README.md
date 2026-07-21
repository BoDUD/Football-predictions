# Soccer Predict

面向 Codex 的足球赛前分析、T−30 临场复查和赛后复盘 Skill。数据主要来自 Titan007，覆盖亚盘、大小球、欧赔、半场、半全场、阵容、伤停和角球。

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

1. **初始分析**：核验比赛状态与时区，抓取专业市场和基本面数据，输出可视化分析并归档。
2. **T−30 临场复查**：固定在开赛前约 30 分钟执行，不提前；重新核验首发、伤停与即时盘口，并明确“主推维持/变更”。
3. **赛后复盘**：只在 Titan 明确显示完场后结算，保存因果学习、统计与校准快照。

Titan 中文页时间默认按 `Asia/Shanghai` 解析，再转换为 Codex 环境中的用户时区。比赛状态始终以页面明确的未开场、进行中或完场标识为准。

## 推荐与统计口径

- 每个 active 赛前版本最多一个机器可读的 `primary_pick`；其他合格方向标为 `secondary`。
- `lineup-check` 替换 active 版本时记录主推 `maintained` 或 `changed`，历史版本保留在 `revisions`。
- 默认战绩、准确率和 ROI 先报告 `primary`：每场最终 active 主推最多计一次。
- `all_formal` 保留亚盘、大小球、半场和半全场的全部正式方向，作为次级统计。
- 观察候选和精确比分不计入主推命中率或 ROI。

## 小样本 Guardrail

- 盘口与相关欧赔同时明显反向时，普通低 EV 方向降为观察。只有 EV ≥ 8%、边际 ≥ 4pp、至少 5 家公司且有独立阵容或基本面支持，才可成为正式方向或主推。
- 伤停表与确认首发冲突时，以确认首发为准，旧伤停不再作为让球或进球方向证据。
- 大小球降水不能单独构成主推依据，必须有多家公司一致性和进攻配置或机会质量支持。
- 单市场至少 20 个有效样本并具备特征级证据后，才允许考虑全局权重调整。

## 本地数据

每个工作区的数据独立保存在：

```text
<workspace>/.codex/soccer-predict/history.json
<workspace>/.codex/soccer-predict/calibration.json
```

真实历史、个人路径和本机校准文件不应提交到公共仓库。

常用命令：

```bash
python scripts/memory_store.py --base-dir <workspace> pending
python scripts/memory_store.py --base-dir <workspace> stats
python scripts/memory_store.py --base-dir <workspace> calibrate --write
```

## 目录

```text
soccer-predict/
├── SKILL.md
├── agents/openai.yaml
├── scripts/memory_store.py
├── references/
│   ├── data-collection.md
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

[MIT](LICENSE)
