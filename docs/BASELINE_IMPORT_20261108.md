# 净化后的本机源码基线导入说明

本文件由导入过程写成，用来说明这条新分支相对公开 fork 如何落地，以及审计排除带来的限制。它不是部署说明，也不要求启用任何 `runtime/services` 历史 Compose。

## 归档校验

- 附件：`TradingAgents-CN-audited-source-2e8d253a.tar.gz`
- SHA256：`69d95208117d398153c40d1b7299b80aea894366cf6b4309d8db1e6ee24adf5c`
- 大小：3020530 字节
- tar 成员 1403 个，全部为普通文件；无绝对路径、无 `..`、无符号链接、无硬链接、无设备文件
- 隔离解压后，1400 个载荷文件的字节数和 SHA256 与 `MANIFEST.json`、`SHA256SUMS` 一致
- `SHA256SUMS` 另覆盖 `EXPORT_README.md` 与 `MANIFEST.json`（不覆盖自身），这两项也复核通过
- `app/config/settings.json` 的 `data_dir` / `cache_dir` / `results_dir` 在快照中已是 `runtime/data`、`runtime/cache`、`runtime/results`

本机原提交 `2e8d253a8592eae759e523ebf5fd862e5df49758`（分支 `local/experience-release-20261106`）不在本 fork 历史中。快照不包含本机 `.git`，因此没有、也不能恢复那条私人历史。

## 相对 fork 的处理

导入时公开仓库默认分支 `main` 指向 `ce9f45f72cb4181bfdc1db45bcc8b778f700d5d7`。本分支从该提交分出，保留本仓库 `.git` 与三份许可（根目录 `LICENSE`、`app/LICENSE`、`frontend/LICENSE`）及其中的上游归属。

工作树按审计快照整体替换，而不是在 fork 旧代码上挑选文件拼接：

| 对照 | 数量 |
| --- | ---: |
| 路径与内容都相同 | 615 |
| 路径相同、内容不同 | 377 |
| 仅 fork `main` 有、本分支删除 | 78 |
| 仅快照有、本分支新增 | 408 |
| 快照载荷 | 1400 |
| 其中已审核的 runtime 服务源 / Compose | 52 |
| 另含未提交但已审核的 `config/pricing.json` | 1 |

78 个 fork 独有文件分两类，都不从本机排除物里恢复：

1. 审计明确排除、即使 fork 上仍有公开副本也不带回本分支：`.claude/settings.local.json`、`.env.example`、`deploy/.env.example`、`docs/BG8CFBQQ.jpg`、`docs/superpowers/plans/2026-09-14-screening-factor-funnel.md`、前端论文 PDF 与插图、以及 3 个含 token 样例字面量的测试（`tests/core/test_core_api_key_utils.py`、`tests/llm/test_thinking.py`、`tests/models/test_models_config.py`）。
2. fork 上的旧布局，本机定制树已经改道，快照中不存在。例如 `app/worker/{cn,hk,us}` 与 `app/services/*sync*` 的对应实现在快照里位于 `app/data/sync/`。本分支跟随快照，不把两套同步实现叠在一起。

`.gitignore` 仍忽略整个 `/runtime/` 和 `app/config/*.json`。为让 52 份已审核 runtime 源与净化后的 `app/config/settings.json` 进入版本库，本次使用 `git add -f` 跟踪这些已存在文件，没有放宽忽略规则去收录数据、日志、缓存或其它配置。

## 排除导致的限制

快照不是完整生产备份。以下内容没有、也不会在本分支恢复：

- 本机 Git 历史、环境文件、凭据、证书、私钥和私有服务配置
- 数据库、行情缓存、日志、备份、自选、账户与真实研究报告夹具（含 `tests/fixtures/research_evidence/002241_20261105.json`）
- `graphify-out/`、依赖目录、构建产物和未审计的 PDF / 图片
- 多份操作者笔记（含日内研究、证据与项目状态文档）
- 全市场日线停更是否已修好：本次基线没有验证，后续实现不得宣称全市场覆盖

因此：部分引用被删夹具、媒体或本机服务的测试可能无法运行；前端缺省插图和论文 PDF；环境变量需要私下重建；`runtime/services` 里的 Compose 只是历史操作参考，不表示这些服务应被启用或部署。

额外敏感内容扫描未发现私钥、云厂商访问密钥、`sk-` / `ghp_` 令牌或已填写的真实 API Token。命中项是公开默认占位（如 `tradingagents123`、`admin123`）、环境变量插值、测试邮箱、许可证联系邮箱，以及测试里的保留地址。`/Users/` 本机绝对路径未出现。
