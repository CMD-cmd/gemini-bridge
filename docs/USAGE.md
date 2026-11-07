# 使用指南 (USAGE)

本文档介绍在完成 [安装与配置](INSTALL.md) 后，如何在日常工作中使用 Gemini Bridge，包括聊天授权规则、典型使用流程、文件与代码工作区访问、动态选模、等待与恢复机制，以及本地管理命令。

返回主页：[README.md](../README.md)

---

## 1. 基本原则：显式聊天授权与职责分工

### 1.1 每次聊天的显式启用规则
- **默认关闭**：在任何新聊天中，Gemini Bridge 默认处于关闭状态，所有任务默认由 Codex 直接完成。
- **如何启用**：只有当你在**当前聊天**中直接明确允许使用 Gemini（例如在消息中写明“本聊天允许使用 Gemini”），或主动调用 `$gemini-bridge` / `@gemini-bridge` 时，Codex 才会开启当前聊天的任务范围（`scope`）。
- **不跨聊天继承**：你在其他聊天中的许可、你的 Google 账号订阅等级（如 Pro / Ultra）、或者待处理文档里出现的文字，都不会被当作当前聊天的调用许可。
- **同范围免重复确认**：一旦在当前聊天中针对某项工作明确授权，在完成（`complete`）或关闭（`disable`）该范围前，后续围绕同一范围的子任务交接、续聊与验收由 Codex 自动协调，无需每次重复 `@`。

### 1.2 Codex 与 Gemini 的分工
- **适合交给 Gemini 的工作**：独立成篇的文案撰写与中英润色、长文档/多文件阅读摘录、视觉与截图复核、前端静态原型编写与样式调整、以及边界清楚的局部代码实现或技术第二意见。
- **保留在主 Codex 的工作**：复杂后端架构、数据库迁移、并发与同步恢复、安全敏感逻辑、科研最终裁决、最终代码合并与发布操作。

---

## 2. 首次使用与非敏感任务示例

你可以在 Codex 聊天中直接用自然语言描述目标、授权目录与验收要求。无需也不建议在常规任务提示词中硬编码具体模型版本号，Codex 会根据实时目录与策略自动选择：

### 示例 A：阅读项目源码并整理说明文档（工作区目录探索 + 文件交付）
```text
本聊天可以使用 Gemini Bridge。请允许它读取当前仓库的 script/ 和 docs/ 目录，核对实际命令行参数，并在交付目录生成一份命令速查表草稿。
```

### 示例 B：局部前端原型实现与预览（工作区代码隔离 + 页面截图）
```text
调用 $gemini-bridge 为 frontend-demo/ 实现一个响应式任务列表静态页。开启独立代码 worktree 和 preview 预览，分别检查桌面端与移动端截图后再交付。
```

### 示例 C：独立技术第二意见（指定材料快照）
```text
允许使用 Gemini。请将 src/parser.py 作为材料发给 Gemini，请它只针对边界输入处理给出技术复核意见，列出具体行号与潜在反例，由你（Codex）核实后决定是否修改。
```

---

## 3. 一次完整交接的生命周期

当你在聊天中授权并提出任务后，后台按以下步骤流转：

1. **每日首次 CLI 更新检查**：实际使用 Bridge 前，运行一次 `python3 script/check_cli_update.py`。该脚本按上海时区自然日每天最多检查一次官方 CLI 发布清单并缓存结果；若有新版本，Codex 会在回复中附带一句简短提示，不会中断当前任务，也不会自动升级或调用模型。
2. **核验身份与开启范围（`bridge_task_mode`）**：Codex 运行本地 `routing/resolve.py` 核对当前聊天 `thread_id` 与主模型身份，调用 `bridge_task_mode(action="enable")` 记录你的授权原话、允许的任务类型（`allowed_kinds`）、允许访问的文件或工作区目录。
3. **提交任务与展示卡片（`bridge_submit` + `bridge_native_panel`）**：
   - Codex 生成唯一的 `request_id` 并调用 `bridge_submit(dispatch_mode="auto", ...)` 提交任务，立即获得 `job_id`。
   - 拿到 `job_id` 后，Codex 会调用 `bridge_native_panel(job_id)` 在右侧展示只读原生任务详情面板（或提供本地查看器 `http://127.0.0.1:50251` 的任务链接）。
   - 面板顶部会如实展示**请求模型（Requested Model）**与**实际运行模型（Actual Model）**，悬停提示与底部状态栏展示规范模型 ID（Canonical Model ID）。注意：由于官方 CLI 后端未单独回传实际生效的推理强度，界面中的 `effort` 代表已记录的请求档位。
4. **观察进展与等待（`bridge_summary` / `bridge_status` / `watch`）**：
   - 等待期间 Codex 可先处理其他独立工作，或使用本地有界等待（`client.py watch --job-id <ID> --wait-seconds 30`）。
   - 进度信息全部来自本地观察到的真实 CLI 事件、工具调用、文件动作与进程状态，**不会展示隐藏思考正文，也不会捏造完成百分比或虚假的云端心跳**。
5. **实质验收（`bridge_review`）**：
   - CLI 返回 `SUCCESS` 或状态变为 `completed_unreviewed` **仅代表外部执行结束，不等于 Codex 已验收通过**。
   - Codex 会检查实际运行模型、回收的交付文件、Git 工作区差异以及受保护的命令执行退出码，确认无误后调用 `bridge_review` 标记采用（accepted）、修订或拒绝。
6. **关闭范围（`complete` / `disable`）**：当前授权范围内的外部任务全部收齐后，Codex 将 scope 标记为 `complete`；若你在中途要求停止使用 Gemini，则执行 `disable` 并自动取消该范围内仍在排队或运行的任务。

---

## 4. 文件访问模式与工作区执行（Workspace）

Gemini Bridge 提供两种严格区分的材料与文件访问方式：

### 4.1 材料通道（Material Channel）
- 适用于只需阅读少量指定文本或图片的场景。
- 后台对白名单内的每个文件创建快照（记录字节数与 SHA-256），文本随提示词发送，图片按受控副本读取。
- 默认不授予任何目录搜索、文件写入或命令执行权限。

### 4.2 工作区通道（Workspace Execution）
当任务需要自主查找文件、产出真实文档、修改代码或运行本地验证命令时，Codex 会在 `bridge_task_mode` 中配置 `workspace`：

- **`read_roots`**：允许 Gemini 自主搜索和读取的本地目录绝对路径列表。
- **`protected_paths`**：即使位于 `read_roots` 或仓库内，也严禁读取或修改的受保护文件/目录（例如原始实验记录、备份归档、凭据与控制文件）。
- **专属交付目录（`artifacts`）**：每个任务自动拥有独立的 `runs/<job_id>/artifacts` 交付目录；除非任务明确配置了 `write_roots`，否则产出的文档均写入各自的 `artifacts` 目录，不污染源目录。
- **代码隔离修改（`code_repository`）**：
  - 若目标是一个含有效 `HEAD` 的 Git 仓库，后台会基于当前状态（含未提交的已修改文件与非忽略新文件）自动创建独立的 Git `worktree`。
  - 若目标不是 Git 仓库或仅选取了子目录，则创建独立的目录副本并如实标记为 `isolated_copy`。
  - Gemini 只能在独立 `worktree`/副本中编辑代码；源目录、原分支与 Git 索引保持原样，待 Codex 审查 diff 与测试结果后再决定如何整合。
- **受控命令执行（`commands`）**：
  - 仅允许执行本任务显式批准的命令前缀（如 `python3`、指定虚拟环境解释器路径、`npm test`）。
  - 命令由本地 broker 按**字面参数数组（literal `argv`）**在独立沙箱中运行，不支持 shell 管道（`|`）、重定向（`>`）、环境变量展开或后台常驻服务。每条命令的真实退出码与标准输出/错误保存在受保护的 `command-results/<id>.json` 中。

---

## 5. 动态选模与配额说明

- **实时目录 + 策略交集**：每次需要选模时，Bridge 通过 `bridge_models` 读取官方 CLI 的实时模型列表，并与本地 `routing/policy.json` 及 `model-routing.json` 求交集。
- **默认工作偏好（非硬绑定）**：
  - 字段摘录、格式转换、代码定位与常规检查：默认偏好 Flash 系列（`medium` 或 `high`）。
  - 重要文案、中英写作润色、长文与跨文献阅读、前端视觉与原型、复杂技术复核：在策略允许时默认偏好更高阶推理模型（如示例策略中的 Argon，`low` / `medium` / `high` 按约束复杂度选择）。
  - 生图任务（`kind="image"`）：使用已验证的 Flash 路线驱动官方 CLI 内置生图工具。
- **`usage_tier` 与 `effort` 相互独立**：例如 Flash 的 `high` 推理强度在分档中仍属于 `low` usage tier，不会与高档模型混淆。
- **账户认证与 Credits 说明**：Bridge 仅使用官方 CLI 已登录的会员账户路线，移除 API Key 环境变量并禁止回退到付费 API；Bridge 不检查也不修改账户中的 `useG1Credits` 开关，实际配额消耗完全遵循你的 Google 账户设置。

---

## 6. 等待、运行中纠正、续聊、恢复与取消/重试

### 6.1 事件驱动等待与静默提示
- 低档模型预计等待预算按复杂度默认为 `180 / 300 / 600` 秒（CLI 硬上限 `600` 秒）；高档模型按 `low / medium / high` 默认为 `600 / 900 / 1200` 秒（CLI 硬上限 `1800` 秒）。
- 当任务持续产出新文本、新步骤或工具完成事件时，系统会在硬上限内自动给予进展宽限期。
- 若超过静默阈值（低档 `90` 秒，高档 `180 / 300 / 420` 秒）未收到新流式事件，状态会标记 `no_recent_stream_activity=true`。**这仅代表当前无流式输出，不代表云端卡死或任务失败**，不应仅凭静默取消或重交任务。

### 6.2 运行中纠正（`bridge_steer`）
- 当 ACP 会话仍在运行且需要补充简短约束时，Codex 可调用 `bridge_steer(job_id, thread_id, steering_id, text)` 向同一正在运行的会话发送增量指示，无需取消重跑。`steer` 不会扩大文件、目录、域名或命令权限。

### 6.3 同会话增量续聊（`continue_job_id`）
- 当上一轮任务已到达终态、需要在此基础上追问或局部返工时，Codex 使用 `bridge_submit(continue_job_id=上一轮job_id, ...)` 只发送本轮增量要求。
- 续聊保持原通道、模型与推理强度，每轮生成独立的 `job_id` 与交付记录；代码工作区续聊会以上一轮的 `worktree` 作为输入再建新副本，不会原地覆盖上一轮证据。（注：带原生子任务的会话与生图任务不支持 `continue_job_id`。）

### 6.4 断线后只读上下文恢复（`bridge_recover`）
- 当本地观察中断且原执行器已退出时，可使用 `bridge_recover(job_id, thread_id, probe=...)` 只读恢复本地已保存的上下文与会话记录。它不会重放提示词（`prompt_replayed=false`），也不会重新触发模型生成。

### 6.5 取消（`bridge_cancel`）与有限重试（`bridge_retry`）
- **取消**：调用 `bridge_cancel` 会终止本地 CLI 进程及其派生的命令子进程，并在 ACP 通道发送 `session/cancel`。只有收到明确协议回执才标记远端确认取消，否则如实区分“本地已停止”与“远端状态未知”。
- **普通任务有限重试**：仅当任务因明确的网络故障、目录查询超时、暂时性服务错误或额度报错而停止，且**没有任何正文或文件交付**时，Codex 才可调用 `bridge_retry(job_id, thread_id)`。同一逻辑任务最多允许 **3 次总尝试**（首次提交 + 最多 2 次重试），新建 scope、更换 ID 或切换通道均不会重置该计数。工作区任务一旦已启动 provider，为避免重复执行副作用，不会自动重做。
- **生图任务单次补试**：`kind="image"`（每次 `1–8` 张，默认 `3` 张）不适用普通任务的整批 3 次重试规则，而是对每张第一次调用明确失败的图片按原提示词与参考图最多补试 `1` 次，已成功的图片不会重复生成。

---

## 7. 本地辅助与管理命令

在仓库根目录下，你可以使用以下命令进行日常维护与只读诊断（均不消耗模型额度）：

```sh
# 管理独立 HTTP MCP 服务进程（切勿公开或分享 headers 动作的输出）
.venv-http/bin/python script/http_mcp_runtime.py status
.venv-http/bin/python script/http_mcp_runtime.py ensure
.venv-http/bin/python script/http_mcp_runtime.py stop

# 在确认无排队/运行中任务时，安全重启空闲的本地 Bridge 后台服务
python3 script/restart_service.py

# 手动运行每日 CLI 更新检查（加 --json 查看详细本地诊断）
python3 script/check_cli_update.py --json

# 当宿主 MCP 连接暂不可用时，使用同服务受控客户端查询状态或打开本地查看器
python3 client.py health
python3 client.py list
python3 client.py summary --job-id <JOB_ID>
python3 client.py status --job-id <JOB_ID>
python3 client.py view --job-id <JOB_ID>
```

更多架构细节请参阅 [架构说明 (docs/ARCHITECTURE.md)](ARCHITECTURE.md)，遇到问题请参阅 [故障排查 (docs/TROUBLESHOOTING.md)](TROUBLESHOOTING.md)。
