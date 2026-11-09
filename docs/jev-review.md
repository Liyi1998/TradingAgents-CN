# Jev 报告复核（试用）

Jev 是 TypeSafe 的结构化判断接口，不支持 Chat Completions，不能配置为 GPT 分析师的替代品。本接入在原分析及交易信号生成后追加 `jev_review_report`，不改原报告、总结或交易信号，也不执行交易。

## 配置

在 `config/jev_review.json` 中填写 `api_key`，`enabled: true` 开启，`false` 关闭。参考无密钥模板 `config/defaults/jev_review.json`。每次任务重新读取，不缓存密钥，不必因修改该文件重启。

- 固定官方地址：`https://api.typesafe.ai/v1/systemone`，拒绝跟随重定向，不通过本地 GPT 网关。
- 默认固定模型版本 `jev-1.13.0`，便于试用对比；API 返回的实际版本也记入复核报告。
- 私有配置被 Git 与 Docker 构建上下文排除；使用现有 `config/` 挂载向容器提供，建议文件权限 `600`。不要把真实密钥写入 defaults 模板或提交聊天。
- `timeout_seconds` 为整体复核等待上限（默认 20 秒）；仅 429/529 在预算内最多重试一次。失败显示“复核未完成”，不会标为通过，也不会抹掉原分析。

## 查看效果

新分析完成后，在任务详情的分析报告、历史报告中查看“Jev 报告复核（试用）”。包含五项判断、概率分布、置信度、模型版本、用量、复核时间和输入范围。旧报告不会自动补跑或改写。主模型用量统计不含 Jev；Jev 用量单列在复核章节。

发送数据仅包含公开股票代码、分析日期和白名单报告片段；不发送完整任务状态、用户消息、模型配置或持仓字段。含持仓、仓位、成本、账户、密钥等词的报告行进一步剔除，可能误删公开经营成本或风险控制段落，因此必须关注“隐私删行模块”。未知的自定义分析师报告不外发。

为限制费用和上下文长度，最多十个报告各取首尾 1700 UTF-8 字节，总结两项各 1000 字节。完整报告保留在本地；复核章节明确列出截取模块。没有检索原始来源，不能独立验证新闻真伪、价格或财报，只能检查已有文本。

置信度不是报告正确率、股价上涨概率或收益概率。低于 60% 或选项为“材料不足”需人工核对；该阈值仅供试用展示，未经 A 股金融语料校准。中文质量和提示注入鲁棒性仍有限，任何结果都不用于自动批准、拦截或修改投资建议。

## 验证

使用项目 Miniconda 环境运行：

```sh
conda run -n tradingagents python -m pytest tests/llm/test_jev_review.py -m 'not ai' -q
conda run -n tradingagents python -m pytest tests/llm/test_jev_review.py -m ai -q
```

第二条会读取本地密钥并发起一次真实官方 API 调用（仅含简单公开示例，产生少量用量），没有密钥则跳过。测试不模拟客户端、数据库或网络。

官方依据：[API](https://docs.typesafe.ai/api)、[模型及语言限制](https://docs.typesafe.ai/models)、[已知局限](https://docs.typesafe.ai/model-jaggedness/jev-1.13)。

## 接续验收（2026-09-22）

Mac 原任务因 Codex `usageLimitExceeded` 在最终交付前中断，不是应用或 Jev 接口故障。接续检查确认本阶段已完成，无需重新部署或重复付费测试。

- 最新保护规则为 `report-review-v1.1`：风险条件或结论比较所需材料被截取、脱敏或缺失时，强制展示“材料不足”；保留原始模型回答用于审计。
- 原任务已用 600875、分析日期 2026-09-21 的现有报告完成真实调用，耗时 1.35 秒，输入 7533 / 输出 215 tokens。独立结果位于 `runtime/results/jev-review-600875-20260922-135219.md`，未改写历史报告或买卖结论。
- 接续时重跑 `tests/llm/test_jev_review.py`、`tests/llm/test_provider_protocol.py`、`tests/engine/test_orchestrator_pipeline.py`，选择 `-m 'not ai'`，结果为 42 passed、2 deselected；没有新增付费调用。
- 已核对运行容器与工作区的 `app/llm/jev_review.py`、`app/engine/runtime.py`、`app/services/report_titles.py`、`app/services/analysis_service.py`，四个文件 SHA-256 均一致；`/api/health` 正常。私有配置被 Git 忽略，权限为 600。
- 此次没有额外启动完整的新股票分析。后续正常新分析完成后，应在任务详情/历史报告中出现“Jev 报告复核（试用）”；旧报告不会自动追加。若验收这一完整路径，使用用户本来需要的新分析，避免为验收重复消耗主模型额度。
- 当前接入仍是文本复核试用，不是事实核验或自动交易判断；本阶段不扩展为自动修改投资建议。现有未提交修改保留，未自动提交或推送。
