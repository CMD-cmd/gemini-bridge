# 安装与配置指南 (INSTALL)

本文档基于仓库当前已实现的脚本（`script/install.py`、`script/connect_codex.py`、`script/http_mcp_runtime.py`、`script/check_cli_update.py`），说明在 macOS 上配置 Gemini Bridge 的完整步骤。本仓库不包含预编译二进制或默认云端配置，请按顺序执行以下步骤。

返回主页：[README.md](../README.md)

---

## 1. 环境与前置条件

在运行本项目的安装脚本前，请确认本机满足以下条件：

1. **操作系统**：macOS（`sys.platform == 'darwin'`）。`script/install.py` 会显式拒绝非 macOS 系统，Linux 与 Windows 尚未经过验证。
2. **Python 版本**：Python `3.10` 或更高版本（需包含标准库 `venv` 与 `sqlite3`）。
3. **官方 Antigravity CLI（`agy`）**：
   - 必须**先行自行安装并完成账号登录**。`script/install.py` 不会下载、安装、登录或自动升级官方 CLI。
   - 请参阅 [官方 Antigravity CLI 文档](https://antigravity.google/docs/cli/overview) 完成安装与账号认证。
   - 验证 CLI 已可执行并查看你当前账号的模型目录：
     ```sh
     /absolute/path/to/agy models
     ```
4. **Codex 客户端**：已安装并可在本地使用（默认配置目录为 `~/.codex`，或由环境变量 `CODEX_HOME` 指定）。
5. **固定的仓库存放路径**：请将本仓库放在一个长期稳定的本地路径下。安装完成后，`bin/agy`、`.venv-http`、已安装 Skill 中的 `runtime-location.json` 以及 Codex 的 MCP 服务管理都依赖该绝对路径；若事后移动仓库目录，原有引用将失效。

> **关于 MCP 的说明**：MCP（Model Context Protocol）是 Codex 与本地服务通信的协议，不需要另行下载或购买名为“MCP”的独立软件。Codex 自带 MCP 客户端，而下文的 `script/install.py` 会在仓库本地 `.venv-http` 中自动安装 Python MCP 服务端依赖（`requirements-http.lock`）。

---

## 2. 第一步：只读环境预检

在仓库根目录下运行 `--check`（或不带 `--setup` 参数），可在不联网、不修改任何文件、不启动模型生成的情况下检查当前状态：

```sh
python3 script/install.py --check
```

若你的 `agy` 不在系统 `PATH` 中，可一并传入 `--cli` 进行检查：

```sh
python3 script/install.py --check --cli /absolute/path/to/agy
```

输出为单行 JSON，各字段含义如下：

- `python`：当前运行的 Python 版本号。
- `platform`：当前系统平台标识（macOS 下为 `darwin`）。
- `official_cli_found`：是否找到可执行的官方 `agy` 程序。
- `profile_configured`：仓库根目录下是否已存在 `profile.json`。
- `routing_policy_configured`：仓库下是否已存在 `routing/policy.json`。
- `mcp_environment_ready`：仓库下是否已创建 `.venv-http/bin/python`。
- `generation_started`：固定为 `false`（确认未触发任何模型调用）。

---

## 3. 第二步：理解“实时模型目录”与“显式路由策略”的区别

在运行 `--setup` 前，需要区分两个概念：

1. **CLI 实时模型目录（`PATH_TO_AGY models`）**：代表你的账号当前在官方 CLI 中能够列出并连接的型号标识（例如 `gemini-3.8-flash-high`、`gemini-4-argon-high` 等，具体取决于你的账号与产品更新）。
2. **显式路由策略映射（`routing/policy.json`）**：定义 Codex 主模型（`parents`）与外部 Gemini 模型（`external_models`）的能力基线、成本上限、允许任务类型（`allowed_kinds`）以及例外规则。

**关键原则**：
- **出现在 CLI 目录中不代表自动具备分发资格**。如果某个 `model/effort` 组合或当前主模型未在 `routing/policy.json` 中映射，Bridge 会停止派遣并提示先配置策略。
- 仓库附带的 [`routing/policy.example.json`](../routing/policy.example.json) 是一份**带日期的示例策略**（版本 `2026-10-31.v11`，基于 `2026-09-30` 与 `2026-10-10` / `2026-10-17` 检索的 Artificial Analysis v4.3.2 数据快照），它不是实时排行榜，也不是对你的账户权益证明。
- 在该示例策略中，会员成本例外（`external_subscription_exception`）与受限专用模型例外（`external_specialist_exception`）默认均为 `"enabled": false`。

---

## 4. 第三步：运行 `script/install.py --setup`

### 4.1 推荐初始化命令（采用示例策略并启用会员/专用模型例外）

从你自己的 `agy models` 输出中选取一个已在示例策略中映射的模型 ID 与对应的思考强度（`effort`），执行：

```sh
python3 script/install.py --setup \
  --cli /absolute/path/to/agy \
  --model EXACT_CATALOG_ID \
  --effort high \
  --install-skills \
  --adopt-example-policy \
  --subscription-exceptions
```

> 若只想采用示例策略中的基础 AA 能力/成本双上限比较，而不启用会员与专用模型例外，可省略 `--subscription-exceptions`。
> 若你准备自行编写并提供审阅后的 `routing/policy.json`，可省略 `--adopt-example-policy` 与 `--subscription-exceptions`，并在安装后自行将策略放置到 `routing/policy.json`（权限建议设为 `0600`）。

### 4.2 `script/install.py` 全部已实现参数说明

| 参数 | 说明 |
|---|---|
| `--check` | 执行只读前置条件检查并输出 JSON，不写入文件、不联网、不触发模型调用。 |
| `--setup` | 执行本地安装与配置。若未传 `--setup`，脚本默认仅执行只读检查。 |
| `--cli PATH` | 指定已安装的官方 `agy` 可执行文件路径；未指定时依次尝试 `shutil.which('agy')` 与 `<仓库根目录>/bin/agy`。 |
| `--model ID` | 必填（配合 `--setup`）：来自你本机 `agy models` 输出的精确模型标识符。 |
| `--effort {low,medium,high,xhigh,max}` | 必填（配合 `--setup`）：默认推理强度档位。注意：若配合 `--adopt-example-policy`，`<model>/<effort>` 组合必须存在于 `routing/policy.example.json` 的 `external_models` 中。 |
| `--install-skills` | 安装主 Skill、渲染运行路径，并备份后追加只针对冗余查看 Skill 的隐藏设置。主流程和插件保持启用。 |
| `--keep-view-shortcut` | 配合 `--install-skills` 使用，保留独立的查看 Skill，不添加默认去重设置。 |
| `--adopt-example-policy` | 显式采用 `routing/policy.example.json` 生成本地 `routing/policy.json`（权限 `0600`）。 |
| `--subscription-exceptions` | 必须与 `--adopt-example-policy` 同时使用。将生成的 `routing/policy.json` 中的 `external_subscription_exception.enabled` 与 `external_specialist_exception.enabled` 设为 `true`，并记录采用时间与说明（仍需每次聊天明确授权）。 |
| `--skip-dependencies` | 跳过 `.venv-http` 虚拟环境创建与 `pip install -r requirements-http.lock`（适用于已提前准备好 `.venv-http` 的情况）。 |
| `--codex-home PATH` | 指定 Codex 主目录，默认读取环境变量 `CODEX_HOME`，未设置时为 `~/.codex`。 |

### 4.3 安装脚本的安全保护行为

- **绝不覆盖已有配置**：如果 `profile.json`、`routing/policy.json`（当使用 `--adopt-example-policy` 时）或 `<codex-home>/skills/gemini-bridge`（当使用 `--install-skills` 时）已存在，或者 `bin/agy` 已指向其他路径，脚本会立即报错退出，保留原有文件不被替换。
- **不触碰全局路由配置**：脚本只在当前仓库内生成 `routing/policy.json`，不会修改共享的 `~/.codex/agent-routing`。
- **生成文件权限收紧**：新建的 `profile.json`、`routing/policy.json` 和 `runtime-location.json` 均自动设为 `0600`（仅当前系统用户可读写）。

---

## 5. 第四步：启动本地 HTTP MCP 并接入 Codex

完成 `install.py --setup` 并确认官方 CLI 已登录后，运行连接脚本：

```sh
python3 script/connect_codex.py --write-config
```

### 5.1 `script/connect_codex.py` 参数与行为

| 参数 | 说明 |
|---|---|
| `--write-config` | 启动归属当前仓库的本地 HTTP MCP 服务，并向 `<codex-home>/config.toml` 追加 `gemini_bridge_http` 配置段。若不带此参数，脚本仅打印提示信息而不做修改。 |
| `--codex-home PATH` | 指定 Codex 主目录，默认为 `$CODEX_HOME` 或 `~/.codex`。 |

执行 `--write-config` 时，脚本按以下顺序工作：
1. 检查 `<codex-home>/config.toml` 中是否已存在 `[mcp_servers.gemini_bridge_http]`。若已存在，脚本拒绝覆写并退出。
2. 使用 `.venv-http/bin/python` 调用 `script/http_mcp_runtime.py ensure`，启动监听在 `http://127.0.0.1:50252/mcp` 的独立 Streamable HTTP MCP 进程，并在 `.runtime/http-mcp.json`（权限 `0600`）中保存随机 Bearer Token 与实例标识（本地任务查看器使用端口 `50251`）。若端口 `50252` 已被其他进程占用，脚本会拒绝启动并保留现有监听者。
3. 若 `config.toml` 已存在，先创建权限为 `0600` 的时间戳备份文件 `config.toml.bridge-backup-<nanoseconds>`。
4. 通过原子替换写入追加了 `[mcp_servers.gemini_bridge_http]` 与 `Authorization = "Bearer ..."` 的新配置，全程不在终端打印任何 Token，也不发起任何 Gemini 模型调用。

写入完成后，请**刷新当前 Codex 聊天**（或在 Codex 设置的 MCP servers 中重连 `gemini_bridge_http`），无需重启整个 Codex 或关闭其他正在进行的聊天。

---

## 6. 第五步（可选）：安装本地插件 `gemini-bridge-local`

除了主 Skill 外，仓库还在 `plugins/gemini-bridge-local` 提供了可选的本地插件（定义见 [`plugins/gemini-bridge-local/plugin.json`](../plugins/gemini-bridge-local/plugin.json)），用于在聊天输入框提供 `@Gemini Bridge` 提及、图标与任务卡片查看辅助。

1. 支持插件市场命令的 Codex CLI 可运行 `codex plugin marketplace add CMD-cmd/gemini-bridge --ref main`，随后在桌面端的插件界面安装 `gemini-bridge-local`。也可在本地插件市场中添加此仓库的 `.agents/plugins/marketplace.json`。具体来源见 [OpenAI 插件打包文档](https://developers.openai.com/plugins/build/plugins)。（不同版本的 Codex 宿主界面入口可能略有差异，请以你当前使用的客户端版本为准，目前不提供一键在线商店安装。）
2. **设计说明**：该插件故意不包含任何用户专属的 `.mcp.json` 或 Bearer Token，而是直接复用上一步通过 `connect_codex.py --write-config` 配置好的 `gemini_bridge_http` 连接。因此，**只安装此插件而不运行前四步是无法工作的**。
3. 安装或更新插件后，若当前聊天未显示新工具或图标，请刷新当前聊天；历史消息中的旧卡片会保留其生成时的缓存模板。

---

## 7. 第六步（可选）：配置页面预览与文档提取依赖

基础安装器（`install.py`）仅安装 HTTP MCP 所需的核心依赖，以下两项扩展能力按需手动准备：

1. **静态页面多视口预览与截图（`preview: true` / `bridge-preview`）**：
   - 需要在仓库根目录下准备独立的 `.venv-preview` 虚拟环境，并在其中安装 Playwright 与 Chromium 浏览器。
2. **本地 PDF / Office 文档文本提取（`document_extract`）**：
   - 需要包含可用 `pypdf` 的 Python 环境（可通过环境变量 `GEMINI_BRIDGE_DOCUMENT_PYTHON` 指定解释器路径）或本机受支持的系统文档转换工具。

下一步请阅读 [使用指南 (docs/USAGE.md)](USAGE.md)。

## 重启电脑后与更新源码后

本首版不添加系统登录项。电脑重启或 HTTP 服务停止后，在仓库目录运行：

```sh
.venv-http/bin/python script/http_mcp_runtime.py ensure
```

公开版会保留有效的本地连接身份，通常不必重写 Codex 配置。若 `.runtime` 被删除，原连接凭据同时丢失，需要重新配置该连接。不要分享这个目录。

更新 Python 服务源码后，等待任务空闲，再重启本仓库的后台和 HTTP 服务；不要终止其他聊天拥有的进程。示例模型策略不会自动覆盖已经采用的 `routing/policy.json`，需要自行审阅与合并。

## 输入框入口去重

安装器在 `--install-skills` 时默认调用 `script/dedupe_skills.py --apply`：先确认主流程已安装并启用，然后用 Codex 官方支持的 `[[skills.config]]` 名称选择器隐藏 `gemini-bridge-local:gemini-bridge-view`。它只追加这条设置，保留其他 Skill、插件、MCP 连接和凭据，并在改动前保存权限为 `0600` 的配置备份。

旧版已安装用户可在仓库目录运行：

```sh
python3 script/dedupe_skills.py
python3 script/dedupe_skills.py --apply
```

第一条仅检查，第二条保存设置；反复执行不会重复追加同一条设置。仅安装了查看插件、尚无主 Skill 的用户不会被隐藏唯一的查看入口。加 `--codex-home PATH` 可指定配置目录。

技能目录存在客户端缓存。先刷新当前聊天；仍显示旧条目时，在任务空闲后重开客户端以重新加载技能目录。无需因此重连 Bridge MCP。彩色图标用于插件入口；技能菜单是否使用自定义图标由宿主控制，详见[显示说明](SKILL_PICKER.md)。
