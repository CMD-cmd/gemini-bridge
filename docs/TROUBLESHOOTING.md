# 故障排查指南 (TROUBLESHOOTING)

本文档汇总了在安装、连接、选模和执行任务过程中可能遇到的常见报错与异常现象，以及对应的排查和恢复方法。

返回主页：[README.md](../README.md)

---

## 1. 安装与连接阶段报错

### 1.1 `Existing configuration found; no settings were replaced.` 或 `Already exists; preserve and review it before updating`
- **原因**：`script/install.py --setup` 检测到仓库下已存在 `profile.json`、`routing/policy.json`，或 `~/.codex/skills/gemini-bridge` 已存在。为保护你现有的本地配置，脚本设计为绝不覆盖已有文件。
- **处理方法**：
  1. 先运行 `python3 script/install.py --check` 查看哪些配置已经就绪。
  2. 若只需更新某一项，请先人工检查并备份现有文件，移走需要重建的目标文件后再运行对应命令；不要盲目删除正在使用的 `.runtime/` 或历史 `runs/`。

### 1.2 `This exact combination is absent from the example policy. Supply a reviewed policy; do not infer eligibility from catalog presence.`
- **原因**：你在使用 `--adopt-example-policy` 时传入的 `--model` 与 `--effort` 组合（形如 `<model>/<effort>`）不在 `routing/policy.example.json` 的 `external_models` 字典中。
- **处理方法**：
  - 检查你本机 `agy models` 输出的精确模型名称，并与 `routing/policy.example.json` 中的 `external_models` 键名（例如 `gemini-3.8-flash-high` 搭配 `--effort high`，或 `gemini-4-argon-high` 搭配 `--effort high`）核对。
  - 如果你的账号目录中出现了更新的型号，而带日期的示例策略尚未收录，请勿假设它自动具备分发资格；你需要在审阅能力与成本基准后，将该组合补充到自己的 `routing/policy.json` 中。

### 1.3 `gemini_bridge_http is already configured. Existing configuration was preserved`
- **原因**：`~/.codex/config.toml` 中已经存在 `[mcp_servers.gemini_bridge_http]` 配置段，`script/connect_codex.py --write-config` 拒绝重复追加或覆盖现有凭据。
- **处理方法**：
  - 若仓库路径未变，只需确保本地 HTTP MCP 进程正在运行：
    ```sh
    .venv-http/bin/python script/http_mcp_runtime.py ensure
    ```
  - 若你重新生成了 `.runtime/http-mcp.json` 导致 Token 变化，请手动打开 `~/.codex/config.toml`，删除旧的 `[mcp_servers.gemini_bridge_http]` 及其 `.http_headers` 小节后，重新运行 `python3 script/connect_codex.py --write-config`。

### 1.4 `HTTP MCP port 50252 is occupied; existing listener was not changed`
- **原因**：本机回环端口 `50252` 已被另一个进程（可能是另一个仓库副本的 Bridge，或其他本地程序）占用。为防止覆盖无关进程的凭据，`http_mcp_runtime.py` 拒绝启动。
- **处理方法**：
  - 先运行 `.venv-http/bin/python script/http_mcp_runtime.py status` 检查是否就是当前仓库的进程。
  - 若是旧仓库副本遗留的进程，在对应目录运行 `http_mcp_runtime.py stop` 后再试。

---

## 2. 聊天中找不到工具或原生详情面板未显示

### 2.1 只安装了插件或 Skill，无法调用 Gemini
- **原因**：`gemini-bridge-local` 插件和 `gemini-bridge` Skill 仅提供界面入口和工作流指令，不包含后端服务、Python 依赖或官方 CLI。
- **处理方法**：按 [安装指南](INSTALL.md) 依次完成官方 CLI 登录、`script/install.py --setup` 和 `script/connect_codex.py --write-config`。

### 2.2 提示缺少 `bridge_native_panel`、`bridge_summary` 或新增参数未识别
- **原因**：已打开的旧 Codex 聊天会缓存连接建立时的 MCP 工具目录（Schema）；磁盘上的 Skill 或服务端更新后，旧聊天的缓存不会自动同步。
- **处理方法**：
  1. 先**刷新当前聊天**。
  2. 若仍未出现新工具，在 Codex 设置（Settings → MCP servers）中仅将 `gemini_bridge_http` 关闭再重新打开，随后刷新当前聊天。
  3. **不要重启整个 Codex 客户端，也不要打断其他正在工作的聊天**；更不要为了测试卡片显示而重新提交生成任务。
  4. 历史消息中已经渲染出的旧任务卡片会保留当时的缓存模板，这是正常现象。

### 2.3 原生 MCP 返回 `Transport closed` 或短查询超时
- **重要判断**：本地 MCP 查询超时或连接断开 **不代表后台 Gemini 生成任务失败**，也不会取消已提交的任务。
- **处理方法**：
  - 切勿立即用新 `request_id` 重新提交同一任务。
  - 在当前聊天中可由 Codex 临时调用同一后端的本地客户端接续查询：
    ```sh
    python3 client.py status --job-id <JOB_ID>
    python3 client.py view --job-id <JOB_ID>
    ```
  - 若刚提交时尚未拿到 `job_id`，应使用**同一个 `request_id` 和完全相同的请求参数**进行幂等恢复。

---

## 3. 模型目录查询与选模异常

### 3.1 报错 `authentication`、`catalog_network`、`catalog_timeout` 或 `catalog_access_denied`
- **原因分析**：
  - `catalog_network` / `catalog_timeout`：官方 CLI 在查询模型列表或刷新令牌时遇到本机网络波动或代理超时。
  - `catalog_access_denied`：服务端拒绝访问（单独一次 `403` 不一定代表账号已退出登录）。
  - `authentication`：认证检查未通过。注意：CLI 刷新令牌时的网络中断也可能表现为认证错误，不能仅凭分类名立刻断定会员失效或额度耗尽。
- **排查步骤**：
  1. 查看私密诊断记录 `.runtime/catalog-diagnostics/<diagnostics_id>.json`（或任务目录下的 `catalog-diagnostics.json`）中的具体错误分类（切勿将原始诊断全文或 Token 粘贴到公开渠道）。
  2. 在终端直接运行 `/absolute/path/to/agy models` 测试官方 CLI 本身是否能正常列出模型。
  3. 若确系出现 `invalid_grant` 或令牌被撤销，请按官方 CLI 文档重新登录后再试。

### 3.2 `effective_enabled: false`（任务模式未放行）
- **常见原因（查看返回的 `reason` 字段）**：
  - 当前聊天尚未记录用户的明确 Gemini 授权原话。
  - 当前 Codex 主模型或 `<model>/<effort>` 组合未在 `routing/policy.json` 中映射，或命中了 `disabled_parent_models`。
  - 所选外部模型的 AA 能力分或单任务成本高于当前主模型上限，且未开启对应的 `external_subscription_exception` / `external_specialist_exception`。
  - 当前聊天回合已结束或日志索引尚未就绪（`RuntimeNotReady` / `InactiveTurnError`）。
- **处理方法**：根据 `reason` 对症处理；若属于策略限制，直接由 Codex 本身完成当前任务，不要尝试通过 `manual` 模式或伪造 `task_purpose` 绕过门禁。

---

## 4. 任务运行、等待与版本更新问题

### 4.1 状态显示 `no_recent_stream_activity: true` 或 `provider_status: not_exposed_by_cli`
- **含义**：官方 CLI 在无头/ACP 模式下不提供云端实时推理心跳。当高阶推理模型进入较长的内部思考阶段、超过静默提示阈值（低档 90 秒，高档 180/300/420 秒）未输出新流式分片时，系统会如实标记该状态。
- **处理方法**：只要本地进程仍在且未超过 `effective_deadline_seconds`，请使用 `client.py watch --job-id <JOB_ID> --wait-seconds 30` 分段观察，不要将静默误判为云端死锁而盲目取消重跑。

### 4.2 状态为 `SUCCESS`，但提示 `cli_print_timeout: true` 或 `requires_attention: true`
- **原因**：官方 CLI 在自身打印超时退出时，有时仍会返回退出码 `0`（`SUCCESS`）并输出截断的部分内容。
- **处理方法**：检查 `progress` 诊断字段与实际交付文件是否完整，不要仅凭 `SUCCESS` 字样认定任务已完整交付。

### 4.3 `Bridge daemon version mismatch; restart the local service before submitting`
- **原因**：你更新了仓库源码，但后台仍在运行旧版本的 `service.py` 守护进程。此时只读查看（`status`、`list`）和取消（`cancel`）仍受支持，但新任务提交会被拦截以防版本混用。
- **处理方法**：
  - 等待当前正在运行的任务结束，然后运行安全重启脚本：
    ```sh
    python3 script/restart_service.py
    ```
  - 若 `restart_service.py` 提示 `Bridge has active jobs; wait or explicitly cancel them first`，说明仍有排队或运行中的任务，请先等待其完成或明确取消后再重启，切勿使用 `kill -9` 强杀正在工作的后台进程。

### 4.4 工作区任务报错 `tool call denied by pre-tool hook`
- **常见原因**：
  - `Tool is not authorized: <tool_name>`：当前 CLI 版本发出了尚未在 Bridge 白名单中登记的原生工具别名（例如目前仅精确放行了已核实的 `read_file({Path:绝对路径})` 等受控工具，未知写入别名仍被拦截）。
  - `Credential or control path is protected`：模型尝试读取或修改 `.runtime/`、`.agents/`、`command-results/`、`profile.json` 或 `protected_paths` 中的受保护文件。
- **处理方法**：
  - 对受保护路径的拦截属于预期安全边界，无需放开。
  - 若因原生写入工具别名被拦截，可让模型按任务约定在最终回复中输出结构化 JSON 文件数组（`{"files":[{"path":"...","content":"..."}]}`），由主控 Codex 核验后无损写入目标目录。
