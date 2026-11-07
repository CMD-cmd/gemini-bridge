# 贡献指南 (CONTRIBUTING)

感谢关注并参与 **Gemini Bridge**（`https://github.com/CMD-cmd/gemini-bridge`）。在提交 Issue 或 Pull Request 之前，请先阅读本指南以确保改动符合项目的安全边界与工程规范。

返回主页：[README.md](README.md)

---

## 1. 核心设计守则

向本仓库贡献代码或文档时，请遵守以下基本原则：

1. **保持显式聊天授权（Per-Chat Opt-in）**：任何改动不得将外部模型分流改为默认全局开启，也不得以账号订阅状态、环境变量或静态文件文字替代当前聊天的用户明确授权。
2. **严禁付费 API 回退与越权探测**：保留官方 CLI 的既有账号认证路线，不得引入 API Key 自动回退逻辑，不得添加探测或修改用户 `useG1Credits` 账户设置的代码。
3. **不硬编码未来模型**：Skill、提示词模板与业务代码应通过实时模型目录（`bridge_models` / `agy models`）与策略配置文件（`routing/policy.json`、`model-routing.json`）动态选模，不要在代码中把某个特定型号写死为永久唯一选项。
4. **绝不静默覆盖用户已有配置**：安装与连接脚本（`script/install.py`、`script/connect_codex.py`）在遇到已存在的 `profile.json`、`routing/policy.json`、已安装 Skill 或已有 `gemini_bridge_http` 配置段时，必须保持拒绝覆盖并提示用户审阅的行为；修改外部配置前必须保留 `0600` 备份。
5. **如实表述验证边界**：区分“已在当前 macOS 开发环境实测通过的实现”与“尚未验证的平台或宿主版本”。在未取得真实跨平台或新 CLI 版本实跑证据前，不在文档或报错信息中声称支持 Linux/Windows、一键商店安装或特定会员免费额度。

---

## 2. 仓库关键模块说明

- `script/install.py`：本地初始化脚本（检查前置条件、创建 `.venv-http`、建立 `bin/agy` 符号链接、生成 `profile.json` / `routing/policy.json`、安装主 Skill）。
- `script/connect_codex.py` 与 `script/http_mcp_runtime.py`：受控 HTTP MCP 进程生命周期管理与 Codex `config.toml` 配置追加。
- `http_mcp.py` / `mcp_server.py` / `client.py`：Streamable HTTP MCP 服务端、工具 Schema 适配器与同服务本地客户端。
- `service.py` / `viewer.py`：核心任务队列、工作区与命令 Broker 协调器及本地只读详情查看器。
- `routing/resolve.py` / `routing/policy.example.json` / `model-routing.json`：会话身份只读核验、带日期的示例策略模板与任务复杂度路由规则。
- `skills/gemini-bridge/` 与 `plugins/gemini-bridge-local/`：主 Skill 文档及本地 Codex 插件配置。

---

## 3. 维护路由策略与 CLI 工具兼容性

### 3.1 更新示例路由策略（`routing/policy.example.json`）
- `routing/policy.example.json` 中的基准分数与单任务成本必须注明明确的来源版本（如 `index_version`）与检索时间戳（`retrieved_at`）。
- 不要混合不同大版本的评测基线，也不要用无推理档位的笼统分数冒充具体 `effort` 档位的成绩；缺失精确档位证据时应保持字段为 `null` 并写明 `qualification` 与 `automatic_reason`。
- 示例策略中的 `external_subscription_exception.enabled` 与 `external_specialist_exception.enabled` 在仓库模板中必须保持默认 `false`，仅由用户在运行 `install.py --setup --adopt-example-policy --subscription-exceptions` 时显式开启。

### 3.2 跟进官方 CLI 原生工具兼容性
- 当新版本 Antigravity CLI 在 ACP 模式下引入新的原生工具名称或参数结构（例如 Antigravity CLI `1.5.3` 中观察到的 `read_file({"Path": "/absolute/path"})`）时：
  1. 必须先在真实运行记录中核实其精确调用格式。
  2. 仅对该精确别名放行，且必须复用现有的 `read_roots` / `write_roots` / `protected_paths` / 符号链接检查与 SHA-256 哈希审计，不得为图方便而全局放开未知工具名。

---

## 4. 提交前的隐私与安全自检清单

本仓库为公开源码仓库，在创建 Commit、Pull Request 或粘贴调试信息前，请逐项确认**没有包含**以下任何本地私密内容：

- [ ] 本地生成的配置文件：`profile.json`、`routing/policy.json`、`skills/gemini-bridge/runtime-location.json`
- [ ] 本地符号链接与虚拟环境：`bin/agy`、`.venv-http/`、`.venv-preview/`
- [ ] 运行时状态与凭据：`.runtime/`（含 `service.json`、`http-mcp.json`、`http-mcp.log`、`catalog-diagnostics/`）及 `http_mcp_runtime.py headers` 的任何输出
- [ ] 任务历史与材料快照：`runs/`、`provider-sessions/`、本地 benchmark 记录或含个人/科研数据的测试样本
- [ ] 个人路径与账号信息：本机用户名绝对路径（如 `/Users/<your-name>/...`）、Google 账号邮箱、Bearer Token、Session ID 或 `~/.codex/config.toml` 原文

---

## 5. 许可证说明 (License)

代码与文档采用 [MIT 许可证](LICENSE)。Gemini 名称与图标归 Google LLC 所有，品牌标记不在代码许可授权范围内，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。
