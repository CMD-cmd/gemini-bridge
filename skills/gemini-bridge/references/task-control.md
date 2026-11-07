## 本任务启用与自主分工

默认由 Codex 处理。只有当前聊天中用户直接明确允许使用 Gemini，或主动调用 $gemini-bridge/@gemini-bridge，才启用两档自动分流；不能跨聊天继承，也不能以 Ultra 订阅代替许可。当前聊天后续相关任务可引用同一许可原话，无须反复询问，直到用户撤销或其限定范围结束。只读查看已有任务无需生成许可。用户关闭、限制或指定模型优先；逐任务仍记录目标和材料，历史 scope 不自动复活或扩权，Google 服务端额度仍有效。

同一任务续聊先查询 `bridge_task_mode(action=status, thread_id=当前聊天)`；不传 task_id 时，后端可找回该聊天唯一仍开启的范围及原次数，不能因上下文中忘记编号就另建范围。多个范围会返回候选，由 Codex 按本任务范围选择；已关闭、过期或其他聊天的许可不会复活。返回值区分 `permission_enabled`、`remaining_jobs` 和当前回合能否新提交的 `effective_enabled`，据 `reason` 判断，不把“次数用完”或“当前没有活动回合”称作交付失败。

先运行 `<BRIDGE_ROOT>/routing/resolve.py`，使用实际运行证据中的当前 `thread_id`，调用 `bridge_task_mode(action=enable)`：记录具体 `scope`、当前聊天用户明确许可原话 `authorization`、`allowed_kinds`、所选文件绝对路径 `allowed_files` 和 `material_class`。新范围默认 `allowed_model_tiers=["low","high"]`、`max_jobs=0`、`scope_ttl_seconds=null`，持续到 complete/disable；后端 access.require_chat_opt_in=true，两档共用聊天许可；authorization 可直接写“本聊天可以使用 Gemini”，无需额外点名 Argon，兼容 high_tier_authorization 字段可省略。订阅声明、普通任务请求、条件说明、否定句和单纯提问不视为授权。旧全局 standing_ultra_authorization 不再放行新提交；已有明确本聊天许可的范围可继续使用。未授权时不启用范围；manual/旧 bridge_run_text 也不能绕过。用户明确限定次数或期限时遵守。usage_tier 与 effort 独立，Flash high 思考仍属低档。旧范围缺失字段不自动扩权；manual 不能绕过。授权记录是主 Agent 的声明，不宣称服务端机械核验了真人意图或材料敏感性。

运行日志由 resolver 按本地聊天索引定位，再核对该日志中的 `session_meta`、当前 `task_started` 与 `turn_context`；编辑消息后的日志可带额外后缀，不能按旧文件名或更新时间猜当前回合。索引只提供日志位置，不用其配置值替代实际模型证据。身份检查失败时先区分真实冲突与日志定位/短暂读取故障；可完成本地定位修复并重新核验后继续原获准范围，不应只引用门禁规则就宣称本次内容不合格。重新核验通过不属于重新发起生成调用，确实缺失或冲突的证据仍不放行。

日志写入/索引切换的暂时未就绪会有限重读。已获准入队的任务执行前遇到此情况，最多等待10秒并响应取消；仍未就绪则保存未启动生成的中断记录，保留范围，不因这次读取故障取消同范围其他任务。入队后提交回合自然结束不撤销该任务；执行前仍从最新运行上下文复核模型、档位和同版策略。新提交必须来自活动回合；中止回合、真实身份冲突及不合格模型不放行。

权限开关与模型可用性分开：返回 `effective_enabled=false` 时读原因并由 Codex 接着完成，不改主模型，不静默换 Gemini 档位，不转用 manual 绕过。外部映射在 `policy.json.external_models`，执行前重新读取当前回合身份与生效策略；普通候选核对同版AA能力、每任务成本；普通候选的未知组合或缺失证据会拒绝；用户明确授权的限定用途专用模型按external_specialist_exception检查，不伪造缺失的AA成绩。仅在安装者明确采用后启用 `external_subscription_exception` 列明的会员路线成本例外：能力上限仍保留，使用已有账户认证、禁止 API 回退；不会把会员额度记作零 AA 成本。其他组合继续检查 AA 成本上限。自动任务省略 model/effort 时，后端依据 model-routing.json 的任务族与推理档位偏好，从实时目录与外部策略交集中选择；手动任务保持 profile，明确的成对 model/effort 始终优先，不静默换档。

通过后，每个自主选择的子任务使用 `bridge_submit(dispatch_mode=auto, task_id=返回编号, thread_id=当前聊天)`，附验收要求及必要材料。只传本任务必要材料；整篇阅读可传完整正文和参考文献，不因篇幅较长默认截断或强行分阶段。文件须在本任务白名单内；新增材料需用户已有授权支持并建立明确范围；原任务补试须遵循[有限重试](operations.md#有限重试)，不能借范围更新重置尝试次数。

关闭命令“本任务关闭 Gemini Bridge”对应 `action=disable`；本任务最终交付后对应 `action=complete`。这两种操作关闭权限并取消该任务仍排队/运行的调用。符合补试条件的暂时失败保留范围与原记录，其他失败沿用服务端关闭规则；是否允许补试按[有限重试](operations.md#有限重试)判断；运行身份不可核验时停止生成，不以新范围绕过。新范围不设固定有效期，直到 complete/disable；历史范围保留原期限与权限，不因账户资格自动复活。账本保留已提交、取消与预检状态；用户另行限定次数时按其限制计数。

## 通道断开时的同服务客户端

正常使用优先原生 MCP。原生工具未加载、缓存旧参数、返回 `Transport closed` 或短查询一直未返回时，可临时使用同一后端的 `client.py` 诊断和接续原任务，用户继续留在当前聊天：

```sh
python3 "<BRIDGE_ROOT>/client.py" task-mode --request-file <绝对路径JSON请求文件>
python3 "<BRIDGE_ROOT>/client.py" submit --request-file <绝对路径JSON请求文件>
python3 "<BRIDGE_ROOT>/client.py" status --job-id <任务编号>
python3 "<BRIDGE_ROOT>/client.py" review --job-id <任务编号> --request-file <绝对路径JSON请求文件>
```

用文件工具写 JSON，避免把用户文本插进 shell；这些调用执行同样的任务、身份及成本检查。它不修复宿主 MCP 连接，不能称作原生 MCP 已恢复。需原生工具时，在 Codex Settings → MCP servers 中重连 gemini_bridge；后台更新只使用项目的 `script/restart_service.py`，该脚本拒绝重启繁忙服务。不要终止宿主拥有的 stdio MCP 进程。若同服务客户端快速返回、私有 `.runtime/mcp/stdio-*.jsonl` 又没有对应 `received`，说明本次请求尚未到达适配器，应恢复宿主连接而非重交生成任务。重连后实际调用原生只读工具验证恢复；诊断日志仅含时间、PID、方法、请求号和工具名，不记提示、参数及返回正文。

模型列表查询是零生成调用的启动检查。0.5.1 起，查询失败或超时最多补试一次，两次合计约45秒；明确认证、额度或执行文件错误直接停止。每次任务的原始诊断保存在私有 `catalog-diagnostics.json`，公开错误只返回分类，不泄露原始 stderr。列表始终来自实时 CLI，不使用旧缓存放行，不切换 API 或模型。这个只读查询补试不等于生成任务重试。

后台与客户端版本不同时，已有任务的查看、任务范围查询和精确取消仍可用，回传 `service_compatibility` 如实标明旧后台版本；新提交等其余写操作要求当前后台。不要为恢复查看而终止正在执行的任务；后台更新仍只用空闲重启脚本。

## 交接

使用 `bridge_submit` 提交独立任务，传入 `prompt`、`kind`、验收要求 `acceptance`，以及必要时的所选文件绝对路径 `files`。它立即返回任务编号。拿到每个新 `job_id` 后，主 Agent 必须在当前聊天调用已加载的 `gemini_bridge_http.bridge_native_panel(job_id)` 展示原生卡片；stdio／本地客户端提交同样适用，展示不再调用 Gemini。恢复同一任务已有卡片则不重复展示；卡片失败只补展示或提供同任务链接，禁止为展示重新提交生成。可用 `bridge_status` 读状态和结果、`bridge_cancel` 取消、`bridge_review` 保存核验结论。当前会话未发现工具时说明尚未加载，使用新聊天或重新连接 MCP；不要称文件存在即工具生效。

每份提交使用并保留唯一 `request_id`（可生成 UUID）。返回丢失或需要恢复时，以同一 ID 和完全相同的请求查询/重传，后端返回原任务，不重复生成或消耗新的范围次数。同一请求的幂等恢复不创建新尝试；失败后的新尝试使用[有限重试](operations.md#有限重试)，由后台关联原任务，不能用新 ID 或预检中断恢复绕过计数。不要为同一未确认提交生成新的 ID。宿主未认识新增参数时用同服务本地客户端。

默认材料通道保留文本／图片Agent：文本直接返回，图片用view_file，声明不等于硬隔离。可选[ACP材料通道](acp-transport.md)直接附带图片内容，不需要读图工具。需要目录读取、落盘、局部实现或运行检查时，使用[工作区执行](native-workspace.md)的独立workspace授权及原生Agent；旧scope和旧任务保持原权限。两种方式都审核实际材料与交付，不能用纯文本推测替代视觉证据。

可传 `output_contract` 指定期望格式与长度，例如 `{"format":"json"}` 或 `{"format":"text","max_chars":5500}`。普通任务默认 `strict=false`：超长、JSON 围栏或格式偏差记录为 `output_warnings`，正文可继续验收，不使任务执行失败或关闭分发权限。Codex 可去掉围栏、压缩重复说明、重排字段，保存原始返回与另存的采用版本，在验收说明写明修改及事实保真检查；不得丢失必需内容来换取形式合格。字段、版本数、事实和科学结论仍由主 Agent 实质核对。

需要下游程序直接解析、用户明确要求硬上限或精确协议时设置 `strict=true`，如 `{"format":"json","max_chars":5500,"strict":true}`；不符合即标记失败，不静默修复机器输入。`max_chars` 统计整个返回正文，包含空格、标题、Markdown 和 JSON 语法。宿主缓存未认识 `strict` 参数时使用同服务客户端。格式提示、空正文、被拒绝动作或越界工具使用本身不构成补试理由；完整判定见[有限重试](operations.md#有限重试)。

本地 `input-limits.json` 的通用安全工程预算：64 个文件、任务指令262,144字符、完整文本提示包8 MiB、单附件32 MiB、总附件128 MiB、本地HTTP请求16 MiB。所有文本／视觉任务共用；输出长度按具体交付物确定，不另设用途专属默认目标。这些预算不是模型 token 容量；完整提示包包括指令、合同、验收与文本材料。越界时在授权范围内调整配置或选合适通道；实际超过所选模型容量时保留关键材料并明确分阶段，不静默截断。CLI以官方 stream-json 单条 user 事件从 stdin 接收完整提示包，避免 argv 长度限制。

材料通道中，原文件先快照并保存字节数与SHA-256，文本随提示发送、图片读取副本；PDF/Word/PPT先提取所需正文与参考文献并保留位置。工作区通道则由Gemini按授权目录自主读取，按需调用本地转换工具，目录内容不预先整包发送，附件计数上限不用于限制目录搜索。视觉审查仍需读取实际图片。超出模型上下文或有独立阶段交付需求时，保留关键材料和阅读记录，不静默截断。
