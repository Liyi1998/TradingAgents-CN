# 结构辅助（TPO 近似 + SMC + 已有威科夫背景）

这是单只已建档 A 股的旁路研究，接在「短线持续研究」页面。它不选股、不交易、不改历史报告。

## 怎么打开

1. 打开 `/analysis/research`，选中已经建档的股票。
2. 在「结构辅助」里打开开关。默认关闭；关闭时不取分钟线，也不改盘中跟踪和 19:00 研究更新。
3. 需要图上的接受区或结构线时，再勾选「图上显示接受区与结构」。历史版本和旧研究起点上不叠加这些图层。

刷新结构只读取这一只股票的免费 30 分钟 K 线和库里已经有的交易日历、复权因子、日线、沪深 300。不会因此做全市场补数，也不会调用模型。

## 三块证据分别是什么

- **日线威科夫背景**：原样显示当前研究版本里已经保存的阶段和趋势。没有保存结论就写不可判断，不会新造一个阶段。
- **30 分钟 TPO**：标注为「30分钟 OHLC 区间覆盖的 bar-based TPO 近似」。它不是价格停留时长、成交笔数或 Volume Profile。每根完整 K 线的最高价到最低价覆盖到的每个价格档最多计一次。档宽默认是 0.01 元，可以按最小报价单位的整数倍配置。只对一个交易日里 8 根都齐全的轮廓计算 POC、VAL、VAH。
- **30 分钟 SMC**：先做客观结构。局部高低点要等右侧 K 线收完才第一次可知。BOS 是收盘突破已经确认的结构价。CHoCH 是趋势已经形成之后的第一笔反向收盘突破。FVG 只看同一天连续三根完整 K 线。刺穿已确认高低点后又收回的，先记为 sweep 候选。Order Block 这一版不算。

隔夜价格差单独列出，不写成 FVG。11:30 到 13:30 的空档是午休，不是缺失 K 线。

## 数据不够时

分钟长度不够、K 线未过发布等待期、交易日历缺失、当天有缺口、来源冲突、复权因子不一致或出现除权断点时，对应结果是「不可判断」，并写明原因。程序不补造 K 线，也不把缺口填平。

指数、板块和个股的相对变化分开写时点。缺任何一侧，就不能算相对表现，更不能因为指数下跌就说这只股票异常。板块序列这一版没有接入已核验来源。

TPO、SMC 和威科夫背景放在同一只股票的同一次观察里，用来对照。它们不是三张可以投票的独立证据。方向不一致时页面保留冲突，不合成买卖、仓位或目标价。原 HOLD/SELL 和威科夫阶段保持原值。

## 这一版明确不做

- 不做全市场选股，也不宣称已经修好全市场日线停更。
- 不自动下单，不给仓位或目标价指令。
- 不调用付费模型，不购买数据。
- 不把合成测试数据写成收益或回测成绩。
- 不部署，不合并，不改动本机 Docker。

参数版本是 `structure-auxiliary-v1` / `bar-tpo-m30-v1` / `smc-m30-v1`。观察记录如果写入成功，只进旁路集合 `research_structure_auxiliary`，30 天后过期。

## 数据门槛（复核后）

缺交易日历或复权因子**不会永久封存**这一次观察。下次请求如果带上同一窗口内一致的日历和因子，会重新计算。缺证据的这一次里：

- 单个完整交易日的原始 30 分钟轮廓仍可算出 TPO。页面必须同时显示「复权不可判断」或「日历不可判断」，整体状态是 `partial`，不能写成已通过。
- 跨日 SMC、隔夜价差是否相邻，在日历未证明时保持不可判断，不把缺日当成假期或停牌。
- 复权因子缺失、冲突或除权断点时，不把两侧原始价格连成同一结构，也不把缺失因子当成 1。
- 新鲜度单独记。最后一根 K 线对不齐当前应完成的时段时标成陈旧；陈旧不删除更早的完整日轮廓，但也不能把它当成当前盘口。
- 这些门槛没有为了让页面出现价位而放宽。

## 合成测试和真实免费行情

合成 K 线只存在测试进程里，`evidence_origin=synthetic_test`。它们用来检查确认延迟、冲突和门槛，不代表收益。

真实免费行情是腾讯公开 30 分钟接口的单股响应。2026-11-09 07:36（北京时间）对 `000001` 取到 160 根，最后一根是 `2026-11-06 15:00:00`。没有提供日历和复权因子时，整体状态是 `partial`：20 个完整日可以留下当日 TPO，19 条隔夜价差全部是不可判断，SMC 状态是 `undetermined`，新鲜度是陈旧。这不是交易结论，也没有写入数据库。

## 云端验收

已通过：

```bash
python3 -m pytest tests/engine/test_structure_auxiliary.py \
  tests/services/test_structure_auxiliary_isolation.py \
  tests/engine/test_intraday_research.py -q --tb=line --noconftest
# 54 passed

cd frontend && npx eslint src/components/StructureAuxiliary.vue \
  src/utils/structureAuxiliary.ts src/components/ResearchTrackingBoard.vue \
  src/api/researchTracking.ts tests/structureAuxiliary.test.mjs
# 0 errors

cd frontend && node --test tests/structureAuxiliary.test.mjs tests/intradayResearch.test.mjs
# 6 passed

cd frontend && npm run build
# vue-tsc && vite build，exit 0
```

隔离集成使用内存 `SimulatedMongoDB` 和 FastAPI `TestClient`，不连接用户数据库。覆盖了默认关闭不取数、打开后的合成完整日、来源冲突、确认延迟、陈旧门槛，以及原 HOLD 和威科夫字典不被改写。既有 `tests/engine/test_intraday_research.py` 同时通过，没有改盘中条件算法。

没有跑全量 pytest。项目 `conftest` 和 `app.main` 仍会继续碰到未安装的 `chromadb`，这台机器也没有 `mongod`、`redis-server` 或 Docker。因此没有把历史夹具失败算成这次功能的回归。

## 云端整页验收

2026-11-09 在这台云端 VM 上用公开发行的依赖装好了项目，并用 Ubuntu 软件源的 Redis 与 MongoDB 官方 8.0 社区版在 `127.0.0.1:6380` / `127.0.0.1:27018` 起了空库 `tradingagents_validation`。没有 Docker。应用以 `uvicorn app.main:app` 启动，前端用已经通过的 `npm run build` 产物经 `vite preview` 提供。登录、建档和结构接口都走真实路由。

测试账号和口令只留在本机权限 `600` 的文件里，不写入仓库或 PR。研究档是合成的 `900001`–`900006`，名称都带「合成」。`STRUCTURE_AUXILIARY_SYNTHETIC_DIR` 只在这次隔离进程里设置；目录为空时仍走真实单股行情。行情入库、冷启动补数、股票基础信息启动同步和 Tushare 都关掉了。调度器里的 cron 仍会注册，但没有立即打外部全市场同步。

页面确认：默认关闭；打开后完整日显示合成 TPO（VAL 10.42、VAH 11.39）和 SMC sweep，并标明合成测试行情；来源冲突、缺日历、缺复权、缺失 K 线和陈旧都给出不可判断或陈旧，没有为了显示价位而放宽门槛。原「继续持有研究」和威科夫「下跌 / markdown」在开关之后仍是原值。窄屏下证据卡排成一列。

## 部署前检查

1. 不要合并这个 PR，也不要把它当成已经部署到用户机器。
2. 本机试运行需要自己的环境变量、自己的 MongoDB/Redis，以及一个非生产库。不要复用这次云端口令，也不要从审计排除文件里找回 `.env`。
3. 结构辅助默认关闭。`STRUCTURE_AUXILIARY_SYNTHETIC_DIR` 留空才会读取真实分钟源。
4. `runtime/services` 里的 Compose 不是启用清单。
5. 云端已经跑通隔离全页，所以下一步可以在本机用独立库试运行；仍不要自动合并或部署。
