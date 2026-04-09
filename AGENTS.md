# Project Rules

- 每次修改项目内任何代码、配置、脚本后，必须执行 `./restart.sh` 以使服务生效。
- 默认通过 `./start.sh` 启动，保持 `RELOAD=0`（后台运行更稳定）。
- 若需开发期热重载，可显式使用 `RELOAD=1 ./start.sh`。
