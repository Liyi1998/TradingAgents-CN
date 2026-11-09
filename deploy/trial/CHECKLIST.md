# SMC/TPO 隔离试运行检查单

固定构建上下文来自本包里的 `SOURCE_COMMIT`。这不是 git checkout，也不改 Mac 上现有仓库。

## 当前机器预算

根据只读预检：Apple M4、Docker 为 linux/arm64、Docker 29.4.2、Compose 5.1.3。主机磁盘约余 25.4 GiB，swap 已用约 3.51 GiB。Docker 虚拟机内存上限 7.75 GiB，可用约 3.67 GiB，现有容器约 3.48 GiB，生产 backend CPU 约 150%。

试运行总预算是 2–2.5 GiB 和 2 CPU。本包把容器上限加总到约 1.8 GiB 和 1.85 CPU。云端实测空载进程大约是后端 223 MiB、MongoDB（缓存 0.25 GiB）189 MiB、Redis 7 MiB。真正危险的是**构建峰值**，不是这个空载数字。

`bin/preflight.sh` 在下面任一条件成立时拒绝构建，并且不会去停生产容器、不会 prune：

- 主机可用磁盘低于 12 GiB
- swap 已用超过 1536 MiB
- Docker 内存总量低于 6 GiB
- Docker 估算剩余内存低于 4096 MiB

按上面的只读数字，**现在会拒绝构建**。镜像还不存在，因此也不能启动。这是安全阻塞，不是已经试运行成功。

磁盘 25.4 GiB 本身通过 12 GiB 门槛。卡住的是 swap 和生产已经占住的 Docker 内存。不要为了试运行停止 `tradingagents-cn-hub`，不要停止 18080/18081 上的 readonly-daily-bridge 和 local-embedding，不要执行 `docker system prune` 或删除任何卷。

## 放置位置

建议解压到：

`/Users/wanghongjie/Projects/TradingAgents-CN/isolated-trial/smc-tpo`

该目录目前不存在。它位于现有 git 工作树里面，复制后 `git status` 会出现未跟踪文件。脚本默认拒绝这个位置。若确认可以出现未跟踪文件，并且不会把目录加进 git，再设 `TRIAL_ALLOW_INSIDE_GIT=1`。脚本不会执行 git add、commit、checkout 或 clean。

不想碰到现有 git 状态时，改解压到家目录外的新文件夹，例如 `$HOME/Trials/tradingagents-smc-tpo-trial`。

## 口令

`bin/install.sh` 只在下面这个文件不存在时生成它，权限 0600：

`$HOME/.config/tradingagents-smc-tpo-trial/trial.env`

终端和本说明都不会打印口令。用户名是 `trial-operator`。没有生产 `.env` 也可以启动，因为这个文件是独立的。

## 命令

在解压后的目录执行：

```sh
sh deploy/trial/bin/install.sh
sh deploy/trial/bin/preflight.sh
sh deploy/trial/bin/build.sh
sh deploy/trial/bin/start.sh
sh deploy/trial/bin/status.sh
```

入口是 `http://127.0.0.1:18082`。启动前脚本会再查一次端口。18080 和 18081 被硬性拒绝。若 18082 已被占用，设 `TRIAL_HTTP_PORT=18083` 后重新预检。MongoDB 和 Redis 不映射到主机。

构建按本机 Docker 架构进行，不写死 arm64 或 amd64。前端用已经打好的静态文件，不再在 Mac 上跑 npm。后端镜像使用官方 `python:3.12-slim-bookworm` 的多架构清单，只装 wheel，不编译、不下载模型。

## 合成档和真实分钟源

默认 `STRUCTURE_AUXILIARY_SYNTHETIC_DIR` 为空，结构辅助在研究档里也是关闭的。

要看合成测试档：

```sh
sh deploy/trial/bin/enable-synthetic.sh
```

股票名是「合成测试-完整日」等，代码 `900001`–`900006`。它们不是真实持仓。

切回真实单股分钟源：

```sh
sh deploy/trial/bin/use-live-quotes.sh
```

切回后这些代码仍然只是测试档。真实股票要另建研究档，再手动打开结构辅助。

## 回滚

只停止新 project：

```sh
sh deploy/trial/bin/rollback.sh
```

这不会删除卷，不会碰 `tradingagents-cn-hub`。确认要删掉试运行自己的卷时：

```sh
sh deploy/trial/bin/rollback.sh --remove-trial-volumes
```

必须再输入 `delete-trial-volumes`。脚本只删除名称以 `tradingagents-smc-tpo-trial_` 开头的卷。

## 云端已经做的和 Mac 还要做的

云端做过：应用在隔离的 MongoDB/Redis 上启动、登录、建档、合成结构接口，以及生产构建的页面。云端机器是 x86_64，没有 Docker，所以没有构建或运行这个 Compose。

Mac 上还要做：预检（预期按当前 swap 拒绝构建）、以后资源够了再构建 arm64 镜像、启动、登录和页面点击。在预检从拒绝变成允许之前，不要启动。
