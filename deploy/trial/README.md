# 隔离试运行包

这个目录是给 Mac 上另一套 Docker project 用的。project 名是 `tradingagents-smc-tpo-trial`。它不读取生产 `.env`，不加入 `tradingagents-cn-hub` 的网络或卷。

操作顺序和资源门槛见 `CHECKLIST.md`。先跑 `bin/preflight.sh`。预检拒绝时就停，不要清理生产 Docker。
