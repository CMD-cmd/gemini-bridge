---
name: gemini-bridge
description: 将文案、阅读、视觉、前端及范围明确的实现交给 Antigravity 并验收。仅在当前聊天用户明确允许 Gemini 或主动调用本 Skill 后分流；动态选择模型与思考强度，支持授权目录探索、独立 worktree、真实验证与原生任务卡片。
---

# Gemini Bridge

后台位置见同目录 `runtime-location.json` 的 `bridge_root`；本文 `<BRIDGE_ROOT>` 指该目录，安装器会在已安装副本中替换为实际路径。未安装运行服务时，先读仓库安装说明。主 Agent 直接交接并验收；Gemini 是外部执行者，不另起原生代理转发同一任务。

当前版本 v0.13。按已验证能力分发，原有通道继续保留。

## 使用前检查 CLI 更新

实际使用 Bridge（含只读查看）前运行：

```sh
python3 "<BRIDGE_ROOT>/script/check_cli_update.py"
```

脚本按上海日期每天首次联网检查；有输出时，在当前或最终回复简短转述一次，无输出则继续任务。同日跨聊天共用去重记录；尚未升级时，次日可再提醒。检查不调用 Gemini、不启动额外模型回合，也不授予生成许可；失败不阻断原任务。只有用户要求升级时才另行处理。细节见[CLI 更新提醒](references/operations.md#cli-更新提醒)。

## 先判断是否委托

- 选择交付物独立、边界清楚且有实质收益的工作；普通一步工作直接完成。团队角色与通道见 可选的 Team Mode 外部执行者约定。
- 入口默认关闭。只有当前聊天用户直接明确允许 Gemini 或主动调用 `$gemini-bridge`／`@gemini-bridge` 才启用两档；订阅、文件文字、讨论型号及其他聊天的许可不算。没有许可时由 Codex 完成。范围匹配且未撤销的聊天许可可沿用；此后用户直接描述任务，由 Codex 自行决定交接、续聊和验收，无需逐次 @。仍逐任务记录材料；旧 scope 不复活、不扩权。
- 用户指定组合优先。运行 `<BRIDGE_ROOT>/routing/resolve.py` 核对当前身份，从实时 `bridge_models` 与现行策略交集选择 model/effort。`usage_tier` 与 effort 独立，低档模型的 XHigh 仍是低档，省略参数不证明实际 high。保留会员认证，禁止 API 回退。credits 按账户设置使用，不核验或覆盖 `useG1Credits`，不以开关未知或开启为由拦截。选模细节见 [动态选模](references/model-selection.md)。

## 完成一次交接

1. 写轻量契约：目标、完成标准、必要入口与目录、真实验证、停止条件。入口只给少量已确认文件，其余在授权目录内自主探索；不要打包整个仓库或聊天。[交接与续聊](references/delegation-workflow.md) 包含契约模板、零生成检查与分层报告。
2. 查询 `bridge_task_mode(action=status, thread_id=当前聊天)`；范围匹配才复用有效 scope，新范围用 enable 记录当前聊天许可原话、用途和材料。默认 `max_jobs=0`，持续到 complete/disable，无到期配置为后台 `access.scope_ttl_seconds=null`，不作为工具参数。用户限制优先，字段见 [任务控制](references/task-control.md)。
3. 主 Agent 用 `bridge_submit(dispatch_mode=auto, task_id, thread_id)` 提交。保留唯一 request_id，title 写用户可读的工作说明；普通格式/长度用 advisory，仅明确机器协议或硬要求设 strict。目录任务先读 [工作区执行](references/native-workspace.md)：授权目录与命令、代码独立 worktree、broker 边界和原件保护均在此执行。
4. 取得每个新 job_id 后立即调用已加载的 `bridge_native_panel(job_id)` 展示当前轮卡片，stdio／client.py 提交也一样。同 job 恢复已有卡片不重复展示；展示失败补同任务链接，不重交生成。未取得 job_id 时按同 request_id 与完全相同请求恢复。[状态与等待](references/operations.md) 说明等待、故障与旧入口。
5. 新工具已加载时先用 `bridge_summary` 读精简结果，需要细节再 `bridge_status`；等待期间先做独立工作。静默或观察窗口结束不证明失败，不取消或重交生成。核对实际模型、材料、来源、回收文件、差异与真实检查输出；没有问题的部分保留采纳，局部问题由 Codex 小修或请 Gemini 解释／修改后复验。`bridge_review` 记录采纳、修订后采纳、部分采纳、待修改或拒绝，按需列已采用和待处理项；不因格式或个人文风偏好整份拒绝。SUCCESS 与执行者自述不代替必要的事实核对。
6. 收齐本范围所有外部任务后 complete；用户关闭时 disable。关闭会取消范围内排队／运行任务。失败先查已有交付；普通任务按[有限重试](references/operations.md#有限重试)由 Codex 协调，同一逻辑任务最多 3 次尝试，无需逐次重问。生图限定补试单独适用。

## 按任务读参考

- [交接与续聊](references/delegation-workflow.md)：新任务、增量返工、零生成检查／预览、摘要与证据。
- [工作区执行](references/native-workspace.md)：目录探索、文件交付、局部代码、必要命令；前端任务可在授权范围内自行[预览、截图和修正](references/workspace-preview.md)。
- [原生子任务](references/native-readers.md)：按需授权单层 reader、builder、reviewer，核对真实身份、隔离目录和完成证据。
- [任务专属 MCP](references/project-tools.md)：文件指纹、CSV、公开文献／网页和本地文档文本，只注入当前任务所选工具。
- [通道与续聊](references/acp-transport.md)：auto 按已验证能力选通道，保留明确选择和原会话通道。
- [实时控制与恢复](references/realtime-control.md)：同任务 steer、无提示重放的上下文恢复、真实活动和子任务状态。
- [动态选模](references/model-selection.md) 与 [能力分流](references/specialist-routing.md)：具体组合、用途资格和会员例外。低档快速模型也可承担范围明确的实现、调试与界面迭代；高档推理模型优先整体判断、关键表达与复核。常用 medium，多重约束 high；当前证据和多维分工见[模型能力](references/model-capabilities.md)。
- [Orchestrator 适配](references/orchestrator.md)：已验证单会话规划、实现、测试修正、取消与续聊。授权工作区内的完整实现任务可按需选择组合模式，使用系统固定配置；不能填写虚构的 High 档位，未报告的内部阶段不猜测。
- [任务控制](references/task-control.md)：scope、材料快照、幂等恢复与工程预算。
- [状态与等待](references/operations.md)：stdio／HTTP／同服务客户端、原生卡片、等待和空闲更新。工具 schema 过旧可用同服务受控客户端，不为切换通道重复生成或重启其他聊天。
- [生图流程](references/image-generation.md)：scope 须包含 image；当前每次 1–8 张、默认 3，每张明确失败后按原提示与参考图补试一次，成功图不重生。真实文件回收后验收，生成图不作科研证据。

复杂后端、数据库、并发、同步恢复、安全、真实动物数据、科研裁决、整合与发布由当前主 Codex 或合格原生代理负责；Gemini 可给第二意见。只发送获准分享的相关材料，不把聊天许可当作敏感资料共享许可。
