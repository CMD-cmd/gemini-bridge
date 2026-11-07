# 安全与权限边界说明 (SECURITY)

Gemini Bridge 的设计目标是在不交出主机完全控制权、不泄露本地凭据、不静默消耗付费 API 的前提下，让 Codex 能够受控地借助官方 Antigravity CLI 完成边界清楚的子任务。本文档如实说明已实现的安全控制机制及其实际作用边界。

返回主页：[README.md](../README.md)

---

## 1. 账号认证与计费安全边界

- **仅使用既有官方 CLI 账号认证**：Bridge 调用本机已登录的官方 `agy` 可执行文件，并在启动子进程时清理潜在的 API Key 环境变量。
- **严禁付费 API 回退（`api_fallback: "forbidden"`）**：无论遇到网络异常、模型目录缺失还是配额耗尽，Bridge 均**不会**切换到按量计费的 Google/Gemini API Key 路线。
- **不探测、不篡改 `useG1Credits`**：Bridge 不会调用 `/settings` 去读取或覆盖你账户的 `useG1Credits` 设置；是否使用会员 credits 完全遵循你在官方账户中的既有设置。
- **不作虚假额度承诺**：Bridge 维护本地调用账本（用于工程审计与范围统计），但本地账本不代表 Google 服务端的剩余配额。本项目不保证任何订阅等级（包括 Pro 或 Ultra）拥有固定免费次数或特定扣费比例。

---

## 2. 显式授权与会话身份核验

- **每次聊天默认关闭（Per-Chat Opt-in）**：未在当前聊天中获得用户明确授权（或主动调用 `$gemini-bridge` / `@gemini-bridge`）前，任何外部生成请求均被拒绝。授权不跨聊天共享，历史已关闭的 `scope` 不会自动复活或扩权。
- **只读会话身份核验**：每次启用范围或提交任务前，`routing/resolve.py` 通过只读方式核对 `<codex-home>/state_*.sqlite` 与 `<codex-home>/sessions/` 下的当前会话日志，确认请求来自当前活动回合（Active Turn）且主模型身份与策略匹配。若日志正在轮转或身份不明，拒绝放行。
- **不污染全局配置**：`script/install.py` 仅在当前仓库目录内写入 `profile.json` 与 `routing/policy.json`，不修改全局 `~/.codex/agent-routing`；`script/connect_codex.py` 在修改 `~/.codex/config.toml` 前会先创建权限为 `0600` 的备份文件，且只追加 `gemini_bridge_http` 小节。

---

## 3. 工作区文件系统隔离与路径保护

在工作区模式（Workspace Execution）下，Bridge 通过多层机制限制外部模型对本机文件的访问：

1. **白名单目录授权**：
   - 仅允许搜索和读取 `workspace.read_roots` 中显式列出的目录。
   - 默认仅允许向当前任务专属的 `runs/<job_id>/artifacts`（以及显式指定的 `workspace.write_roots`）写入文件。
   - 代码修改在独立的 Git `worktree` 或 `isolated_copy` 副本中进行，源仓库的工作区、暂存区与分支历史保持不动。
2. **受保护路径（`protected_paths`）与控制文件拦截**：
   - 用户或主 Agent 可在 `workspace.protected_paths` 中指定禁止读取或修改的原始记录、归档或敏感子目录。
   - 凭据目录与运行控制路径（如 `.runtime/`、`.agents/` 和任务控制文件）受专门保护。Bridge 源码配置和受保护命令记录禁止由模型改写；任务产生的 `command-results/` 状态与日志可按授权读取，以核实实际执行结果。具体判断由真实路径、读写类型、保护清单和任务目录共同决定。
3. **PreToolUse Hook 与符号链接检查**：
   - 原生文件读取、搜索与编辑工具在执行前均经过 PreToolUse hook 校验真实解析路径（防范通过相对路径或递归符号链接越界）。
   - 对于新接入的原生工具别名（如已精确放行的 `read_file({"Path": "/absolute/path"})`），同样强制执行相同的目录范围检查并记录文件 SHA-256 哈希；未知工具别名默认拒绝。
4. **实际安全边界说明（非虚拟机）**：
   - 源目录写入拦截与任务凭据保护结合了 PreToolUse hook 与 macOS 系统沙箱约束；但官方 CLI 进程本身仍需读取其自身的会员认证配置以完成启动。
   - **请如实看待这一边界**：这套机制旨在防止模型误操作、越界读写或未授权命令调用，它依赖于 macOS 本地进程与沙箱机制，**不等同于硬件虚拟机（VM）或容器级隔离**，不应将其用于运行来历不明的恶意代码。

---

## 4. 任务专属命令 Broker 与网络边界

- **仅按字面 `argv` 执行获准命令**：模型发出的命令请求必须命中当前任务 `workspace.commands` 允许的程序/参数前缀白名单，并交由本地任务 Broker 执行。Broker 直接以 `argv` 数组启动进程，**不解析 shell 管道、重定向、变量展开或后台守护进程**。
- **命令沙箱隔离**：Broker 执行的任务命令运行在独立的严格目录读写、默认禁止网络的 macOS 系统沙箱中，清除继承的凭据环境变量，并限制读取其他系统进程信息。任务取消、超时或结束时，会清理该任务派生的命令进程组（含普通 `setsid` 子进程）。
- **受控网络域名白名单（`network_hosts`）**：当启用任务专属 MCP 工具（如 `public_page`、`literature_search`、`pubmed_search`）时，仅允许访问当前 `scope` 显式授权的公开 HTTPS 域名（在每次请求及重定向时逐次校验），禁止访问内网地址、本地回环接口或携带用户浏览器登录态。
- **受控页面预览（`bridge-preview`）**：预览运行器仅加载当前任务受控回环的静态资源，最多执行 20 个有界 CSS 交互动作（`click`、`fill`、`select`、`check`、`press`、`wait_for`），不执行模型注入的任意外部 URL 或脚本，并在任务结束时清理浏览器实例与临时端口。

---

## 5. 本地回环服务鉴权与私密文件规范

### 5.1 回环 HTTP MCP 与内部服务保护
- **仅绑定本地回环（`127.0.0.1`）**：HTTP MCP 服务（`127.0.0.1:50252`）、本地查看器（`127.0.0.1:50251`）与核心守护进程均仅监听本机回环地址，不对外网或局域网暴露。
- **严格的请求头与鉴权校验**：`http_mcp.py` 强制校验 `Host` 与 `Origin` 头，拒绝包含重复 `Host` / `Origin` / `Authorization` 头的歧义请求，限制最大请求体大小（`MAX_HTTP_BYTES` = 16 MiB），并使用常量时间比较（`secrets.compare_digest`）核对随机生成的 Bearer Token。
- **安全进程生命周期**：`http_mcp_runtime.py` 与 `restart_service.py` 在停止或重启服务前，均通过 `ps` 核对目标 PID 的完整启动命令行与进程启动时间（`lstart`），确认属于当前仓库的受控进程且经过健康检查后才发送 `SIGTERM`，绝不盲目终止未知进程。

### 5.2 私密文件权限与脱敏提醒
以下目录与文件包含本机随机凭据、路径或运行日志，均已设置严格权限（目录 `0700`，文件 `0600`）：
- `.runtime/` 下的所有文件（特别是 `.runtime/http-mcp.json`、`.runtime/service.json`、`.runtime/catalog-diagnostics/`）
- `profile.json`、`routing/policy.json`、`skills/gemini-bridge/runtime-location.json`
- `~/.codex/config.toml` 及其 `.bridge-backup-*` 备份文件

> **安全守则**：在提交 Issue、Pull Request、截图或向他人分享日志时，**切勿上传 `.runtime/` 目录内容，切勿分享 `.venv-http/bin/python script/http_mcp_runtime.py headers` 的输出，也不要将聊天许可当作分享未脱敏科研/商业机密数据的许可**。
