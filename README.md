# Gemini Bridge

<img src="assets/gemini.png" alt="Gemini" width="48" height="48">

**Gemini Bridge** is a macOS-first, source-only local integration between **Codex** and the official **Antigravity CLI (`agy`)** (current backend version `0.13.0`). It lets Codex delegate bounded writing, reading, visual inspection, frontend prototyping, and scoped workspace tasks to Gemini models through your own signed-in official CLI—with explicit per-chat opt-in, dynamic model selection, isolated Git worktrees, a local command broker, and a native read-only task panel.

> This repository is a source-only community release (`https://github.com/CMD-cmd/gemini-bridge`). It does **not** bundle the official `agy` binary, accounts, subscriptions, credentials, saved conversations, or benchmark snapshots. Code and documentation are released under the MIT License; third-party marks are described in THIRD_PARTY_NOTICES.md.

---

## 项目简介

Gemini Bridge 用于在 macOS 本机将 Codex 与官方 Antigravity CLI（`agy`）连接起来。它不是独立云服务，也不替代 Codex 的主控角色：只有当你在当前聊天中明确允许使用 Gemini（或主动调用 `$gemini-bridge` / `@gemini-bridge`）时，Codex 才会将边界清楚的子任务交接给本机已登录的官方 CLI 执行，并在任务完成后对交付文件、代码差异与命令输出进行实质验收。

本仓库仅发布桥接源码，不包含官方 CLI 可执行文件、账号凭据、会话记录或测试快照，也不承诺开箱即用。当前已实现功能均针对 macOS（`darwin`）设计，尚未在 Linux 或 Windows 环境验证。

---

## 组件关系与正确安装顺序

初次接触时，请先理清五个概念之间的关系，并严格按顺序配置：

1. **官方 Antigravity CLI（`agy`）**：底层实际连接 Google 服务的官方命令行程序。你需要**最先自行安装并完成账号登录**。Gemini Bridge 不会代你安装、登录或升级该 CLI。
2. **Bridge 本地服务与队列**：运行在本机的 Python 后台服务（`service.py` 与 `http_mcp.py`），负责核对权限与路由策略、管理任务队列、提供隔离工作区并调用 `agy`。
3. **MCP（Model Context Protocol）**：Codex 与本地服务通信所用的开放协议，**不是需要单独购买或安装的同名软件**。Codex 客户端本身已内置 MCP 客户端，而本项目的安装脚本会自动在项目本地虚拟环境（`.venv-http`）中安装 Python MCP 服务端 SDK。
4. **主 Skill（`skills/gemini-bridge`）**：安装到 `~/.codex/skills/gemini-bridge` 的工作流说明，指导 Codex 何时分流、如何动态选模、如何交接工作区和验收结果。
5. **本地插件（`plugins/gemini-bridge-local`）**：可选的轻量级 Codex 本地插件，用于提供 `@` 提及入口、品牌图标与任务查看指引。它复用已配置好的 `gemini_bridge_http` 连接，**不包含凭据或后端服务**。

> **重要提示**：仅安装插件或仅复制 Skill **不会**安装或启动本地服务，也无法调用任何 Gemini 模型。必须按 **“1. 安装并登录官方 CLI → 2. 运行 `script/install.py` 初始化本地环境、策略与 Skill → 3. 运行 `script/connect_codex.py` 启动本地 HTTP MCP 并写入 Codex 配置 → 4. 可选安装本地插件”** 的顺序完成配置。

---

## 输入框里的入口

- `@Gemini Bridge`：彩色 Gemini 图标的插件入口。
- `/gemini`：主 Skill 的显示名为“Gemini 协作流程”，负责交接和验收。当前桌面端的技能选择器可能仍使用统一的方块图标。

安装主 Skill 时，安装器会隐藏插件附带的冗余“查看任务”Skill；原生卡片和详情工具保持可用。已安装用户可运行 `python3 script/dedupe_skills.py --apply`。详见[输入框显示说明](docs/SKILL_PICKER.md)。

## 核心能力概览

- **显式聊天授权（Per-Chat Opt-in）**：默认关闭。必须在当前聊天中明确允许使用 Gemini 或主动调用 Skill/插件后才启用分流；其他聊天的授权或账号订阅状态不会自动跨聊天生效。
- **实时模型目录与策略交集选模**：每次通过官方 CLI 实时查询可用模型（`agy models` / `bridge_models`），结合本机显式配置的路由策略（`routing/policy.json`）与任务需求（`model-routing.json`）动态选择模型与推理强度（`effort`），不在提示词或代码中把未来选模硬编码为固定型号。
- **双通道任务执行**：
  - **材料通道**：对获准分享的指定文件生成只读快照（记录字节数与 SHA-256），传递文本与图片。
  - **工作区执行（Workspace）**：按任务开放授权读取目录（`read_roots`）、受保护路径（`protected_paths`）、专属 `artifacts` 交付目录、独立 Git `worktree`（或非 Git 目录的 `isolated_copy`），以及由本地 broker 在受限沙箱中按精确 `argv` 执行的获准命令。
- **原生任务详情面板与本地查看器**：支持在 Codex 中通过 `bridge_native_panel` 打开只读原生面板（以及本地 `127.0.0.1:50251` 查看器），查看请求型号与实际运行型号、实时观察到的工具/文件活动与交付物，不因查看状态额外消耗模型回合。
- **完整任务生命周期控制**：支持紧凑摘要与详细状态（`bridge_summary` / `bridge_status`）、同会话增量续聊（`continue_job_id`）、运行中纠正（`bridge_steer`）、断线后只读上下文恢复（`bridge_recover`）、明确取消（`bridge_cancel`）、受控有限重试（`bridge_retry`，同一逻辑任务在满足未交付暂时性故障条件下最多 3 次尝试；生图任务单张失败最多补试 1 次）以及 Codex 终态验收（`bridge_review`）。
- **可选扩展能力**：按任务注入的受控项目工具（文件指纹、CSV 统计、Crossref/PubMed/Europe PMC 检索、指定域名公开网页提取、本地文档文本提取）、静态页面多视口预览与有限交互截图（`bridge-preview`），以及单层原生子角色（`reader` / `builder` / `reviewer`）。

---

## 开始使用

完整的参数说明与分步核对请参见 [安装指南](docs/INSTALL.md)。以下是最简配置流程：

### 1. 准备官方 CLI 并查看可用模型

确保你已在 macOS（Python 3.10+）上安装并登录官方 `agy` CLI，随后查看当前账号实际列出的模型 ID：

```sh
/absolute/path/to/agy models
```

> 请参考 [官方 Antigravity CLI 安装与登录说明](https://antigravity.google/docs/cli/overview)。

### 2. 检查本机前置条件（只读、零网络、零生成）

将仓库克隆到一个路径稳定的本地目录：

```sh
git clone https://github.com/CMD-cmd/gemini-bridge.git
cd gemini-bridge
```

随后运行：

```sh
python3 script/install.py --check
```

### 3. 初始化本地环境、默认配置与主 Skill

从 `agy models` 输出中选择一个与路由策略匹配的模型 ID，运行安装脚本：

```sh
python3 script/install.py --setup \
  --cli /absolute/path/to/agy \
  --model EXACT_CATALOG_ID \
  --effort high \
  --install-skills \
  --adopt-example-policy \
  --subscription-exceptions
```

- 该命令会在仓库内创建 `.venv-http` 虚拟环境并安装 `requirements-http.lock` 依赖、建立 `bin/agy` 符号链接、生成 `profile.json`（权限 `0600`）和 `routing/policy.json`（权限 `0600`），并将主 Skill 安装到 `~/.codex/skills/gemini-bridge`。
- `--adopt-example-policy` 表示显式采用仓库附带的带日期示例策略（`routing/policy.example.json`，版本 `2026-10-31.v11`）；若你选取的模型组合不在该示例策略中，脚本会停止并要求你提供经审阅的策略文件，而不会仅凭模型出现在 CLI 目录中就自动放行。
- `--subscription-exceptions`（需配合 `--adopt-example-policy`）表示显式启用示例策略中的会员路线与专用模型例外配置；若不加该参数，示例策略中的例外保持关闭（`enabled: false`）。启用该配置不等同于验证了你的 Google 账户额度，也不会绕过每次聊天的显式授权。

### 4. 启动本地 HTTP MCP 并接入 Codex

```sh
python3 script/connect_codex.py --write-config
```

该脚本会启动受控的本地 HTTP MCP 服务（默认监听 `http://127.0.0.1:50252/mcp`），在先备份 `~/.codex/config.toml`（权限 `0600`）的前提下仅追加 `gemini_bridge_http` 配置段，不会打印任何凭据，也不会修改其他 MCP 服务或全局 `~/.codex/agent-routing`。

### 5. 在聊天中首次使用（非敏感示例）

刷新或重新连接当前 Codex 聊天后，你可以在对话中直接授权并描述任务（无需在提示词中硬编码具体模型版本号）：

```text
本聊天允许使用 Gemini Bridge。请阅读当前项目 docs/ 目录下的说明草稿，检查安装步骤是否与 script/install.py 的实际命令行参数一致，并将修订后的文档写入交付目录。
```

Codex 会核对当前会话身份、按实时目录与策略选择合适模型、开启受控工作区范围、提交任务并打开原生任务面板，待执行结束后核对实际产出的文件并记录验收结论。

---

## 实现与验证边界说明

- **已验证代码实现 vs. 持续中的全新环境安装核验**：核心桥接服务、工作区隔离、命令 broker、ACP/headless 传输与任务生命周期管理已在开发环境中实现并实测；部分可选特性（如完整页面渲染预览、原生子任务、新 CLI 版本的工具别名映射及全新机器从零安装流程）仍随宿主与 CLI 版本演进持续核验中。
- **账户与额度边界**：Bridge 始终沿用你本机官方 CLI 的既有账号认证，严禁回退到付费 API Key 路线；不检查、不修改账户的 `useG1Credits` 设置。Google 服务端配额与扣减规则完全由你的账户设置决定，本项目不保证 Pro/Ultra 会员享有特定免费额度或固定次数。

---

## 文档导航

- **[安装指南 (docs/INSTALL.md)](docs/INSTALL.md)**：前置要求、参数详解、策略映射说明、MCP 接入、插件与可选依赖配置。
- **[使用指南 (docs/USAGE.md)](docs/USAGE.md)**：聊天授权规则、典型任务示例、工作区文件与代码修改、动态选模、等待/续聊/纠正/恢复/重试与本地命令行工具。
- **[架构说明 (docs/ARCHITECTURE.md)](docs/ARCHITECTURE.md)**：四层架构、运行时目录结构、身份核验机制、ACP/headless 传输与协议兼容细节。
- **[故障排查 (docs/TROUBLESHOOTING.md)](docs/TROUBLESHOOTING.md)**：安装报错、面板未加载、模型目录异常、长任务等待与版本不匹配处理。
- **[安全与边界 (docs/SECURITY.md)](docs/SECURITY.md)**：认证与计费隔离、工作区路径保护、命令沙箱、回环服务鉴权与本地私密文件规范。
- **[贡献指南 (CONTRIBUTING.md)](CONTRIBUTING.md)**：开发约定、策略与兼容性维护、隐私脱敏要求与提交规范。

## 许可与验证

代码与文档采用 [MIT 许可](LICENSE)，图标归属见 [第三方说明](THIRD_PARTY_NOTICES.md)。本次公开版的实际检查范围见 [验证说明](docs/VALIDATION.md)。
