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

按上面的只读数字，**构建预检仍会拒绝构建**。这条门槛没有降低。下面的加载和启动预检是另一条路径，供审查，不是已经授权在 Mac 上执行。

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

当前 Mac 上不要执行 `build.sh`。预构建镜像的加载和启动命令在文末，审查同意之前也不要执行。`start.sh` 现在只接受已经加载并通过层摘要核对的镜像，并且要有 `TRIAL_RUNTIME_APPROVED=1`。

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

## 预构建 linux/arm64 镜像

云端 x86_64 虚拟机安装了 Docker Engine 29.9.3 和 Ubuntu 的 QEMU 用户态模拟，实际构建并加载了 `linux/arm64` 镜像。Mac 不需要再构建。构建门槛保持原样：`bin/preflight.sh` 仍会因 swap 和 Docker 剩余内存拒绝构建。

镜像来自提交 `374207d29025aa07336b6484d2a8e5c139f8b1bc`。后端清单 `sha256:8e0234fb0784dbf9c1fad0b75bb882bd24ffa02288b701010055cb604f27c226`，配置 `sha256:6bc99ceac976c60a06d870c2763e50084d38eec48bd5831775711677752959f6`。前端清单 `sha256:e73ac4cb73cf6cf0d92fbcf134383285bedbc41fe932326868e5ce1791bc99b4`。MongoDB、Redis、Nginx 使用官方 arm64 配置摘要 `813a81839069`、`bbce1773ddd4`、`f9d642c42f7b`。五个镜像打在一个 `docker save` 包里。整包 SHA256 是 `66d74ca1ab39966fb67c19d5881d5295d1b1ce5463c5ec1561a43e4eee919879`，1221839360 字节，约 1165 MiB。加载后镜像存储约 1.222 GB。交付目录单文件超过 100 MiB 时写入会被拒绝，所以整包拆成 13 个不超过 95 MiB 的分卷 `image.tar.part-00` 到 `image.tar.part-12`，分卷摘要在 `deploy/trial/prebuilt-image-parts.sha256`。先拼合再核对整包摘要，拼合本身不加载镜像。

冷加载实测两次。页缓存分别增加 2375 MiB 和 2357 MiB，dockerd 与 containerd 的 RSS 只增加约 9 MiB，耗时约 5 秒。Linux 把这些页缓存算作可回收，所以 `MemAvailable` 几乎没降。Mac 的 Docker 虚拟机会计入这部分缓存，加载门槛按较高的 2375 MiB 再加 1024 MiB 余量，要求 Docker 估算剩余至少 3400 MiB。磁盘按压缩包和镜像存储合计约 2387 MiB，再加 4 GiB 后向上取整，加载门槛是 8 GiB。

启动门槛按容器上限合计 1792 MiB 再加 1024 MiB，要求 Docker 估算剩余至少 2816 MiB，磁盘至少 6 GiB。swap 单独不超过 4096 MiB，这是所述 16 GiB 物理内存的四分之一，不是原来的 1536 MiB 构建门槛。

按 09:06 的数字，这三项都会通过，但余量薄：Docker 剩余 3833 MiB 比 3400 只多 433 MiB，swap 3578 MiB 比 4096 只少 518 MiB。磁盘 25.61 GiB 余量充足。因此脚本即使算出通过，也要先看到 `TRIAL_RUNTIME_APPROVED=1` 才加载或启动。这个开关不改变数字。在审查同意之前不要设置它，也不要在 Mac 上执行加载或启动。

云端还用 QEMU 把这套 arm64 镜像跑到了健康检查：`/api/health` 返回 mongodb 和 redis 都是 healthy，后端约 279 MiB，Redis 约 14 MiB。MongoDB 稳态约 363 MiB，初始化峰值约 575 MiB；当时测量容器给了 1024 MiB，Compose 里的上限仍是 512 MiB。官方 Redis 在 x86 主机的 QEMU 下没有通过自己的 arm64 写时复制检测而退出。那次测量临时加了忽略参数，Compose 没有加这个参数。Mac 上的真实 arm64 内核不在这次测量里。云端 Docker 因关闭 iptables 时桥接网络不通，健康检查改走了主机网络，所以这不是 Compose 在 Mac 上的启动证明。

加载和启动命令，审查通过之后才使用：

```sh
sh deploy/trial/bin/preflight.sh
sh deploy/trial/bin/assemble-prebuilt.sh /分卷目录 /绝对路径/镜像.tar
sh deploy/trial/bin/preflight-load.sh
TRIAL_RUNTIME_APPROVED=1 sh deploy/trial/bin/load-prebuilt.sh /绝对路径/镜像.tar
sh deploy/trial/bin/preflight-runtime.sh
TRIAL_RUNTIME_APPROVED=1 sh deploy/trial/bin/start.sh
```

`load-prebuilt.sh` 先核对 SHA256，再 `docker load`，然后核对五个镜像都是清单里的 arm64 层。`start.sh` 使用 `up -d --no-build --pull never`。两条新预检都不会调用构建，也不会拉取镜像。

## 云端已经做的和 Mac 还要做的

云端做过：应用在隔离的 MongoDB/Redis 上启动、登录、建档、合成结构接口，以及生产构建的页面。之后又在这台云端机器上构建并加载了上面的 arm64 镜像，做过健康检查。没有连接 Mac，没有部署。

Mac 上现在可以先跑 `bin/preflight.sh`，预期仍是拒绝构建。加载和启动要等上面的新门槛被审查同意。不要停止生产，不要 prune，不要删卷。
