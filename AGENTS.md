# Project Rules

- 每次修改项目内任何代码、配置、脚本后，必须执行 `./restart.sh` 以使服务生效。
- 默认通过 `./start.sh` 启动，保持 `RELOAD=0`（后台运行更稳定）。
- 若需开发期热重载，可显式使用 `RELOAD=1 ./start.sh`。
- 每次完成修改后，必须执行 `git add/commit` 并推送到 `origin/main`（`https://github.com/Shadowell/Alpha`）。
- 当系统架构、页面功能、API 接口、项目结构或技术栈发生变化时，必须同步更新 `README.md`（包括但不限于：核心功能表、界面预览、技术栈、项目结构树、API 接口列表等对应章节）。
