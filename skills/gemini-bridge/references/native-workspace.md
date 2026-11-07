# 工作区执行

适用于需要自行找材料、写文件、实现局部代码或运行检查的任务。主Codex选模型和思考强度、确定任务目录及交付物，Gemini在该范围自主执行。普通目录读取不逐文件确认；已获授权的必要命令不逐条再问用户。

## 授权与提交

沿用当前聊天的明确Gemini许可，运行resolver取得实际thread_id。`bridge_task_mode(action=enable)`在现有scope参数之外传入：

```json
{
  "workspace": {
    "read_roots": ["/绝对路径/本任务资料"],
    "write_roots": [],
    "commands": ["python3"],
    "protected_paths": ["/绝对路径/本任务资料/原始记录"]
  }
}
```

字段均可省略；目录必须存在。`read_roots`授权目录内自主定位和读取。`write_roots`只填用户任务允许写入的目录，通常留空：每个job自动有自己的`artifacts`交付目录。`commands`是按任务选择的本地程序／参数前缀，例如`python3`、某个venv解释器绝对路径（路径含空格时加引号）、`npm test`；先确认本机有该执行器。不要给任务不需要的程序或宽泛系统控制命令。`protected_paths`可列文件或目录，记录应保留的科研原件、备份、发布归档及其他人的内容。

然后`bridge_submit(dispatch_mode=auto, task_id, thread_id, workspace={})`使用该scope的目录授权。需要子集时可提交更窄的workspace对象；不能扩目录、命令或移除原保护。省略workspace保持原材料通道，旧scope不获得新权限。仍选现有kind/task_purpose，model/effort根据实时目录与现行策略判断，不固定型号。

任务提示写清目标、输出文件及实质验收要求，告诉Gemini自行读目录。不要先把整个目录内容读入Codex上下文或逐份转成提示词。PDF/Word/PPT 可按需用document_extract 提取文本，或调用已获准的本地转换程序；需要图表／排版证据时仍须实际渲染读图，格式支持以真实结果为准。

## 代码修改

在workspace中增加`code_repository: "/绝对路径/项目根目录"`。有合适Git仓库与HEAD时，后台建立独立worktree，捕获当前工作区的已修改文件和非忽略新文件，保留原目录、分支和索引。不通过stash、reset、clean或源目录提交来准备任务。没有可用HEAD、非Git目录或仅选择仓库子目录时，使用独立项目副本，并如实标记`isolated_copy`。

Gemini只修改任务的worktree／副本，Codex检查实际差异、测试与影响后再整合。保护文件、符号链接及子模块等未复制部分会记入工作区记录；不能据此声称这些材料已被读取。忽略的依赖／构建缓存不会自动复制，需要时由Codex在原任务授权内准备依赖或选已有虚拟环境解释器，不能为让测试通过切换API或全局安装。

## 工具与边界

v0.13 auto 按已开放能力为普通工作区选 ACP，保留显式 headless。按[任务专属 MCP](project-tools.md)启用必要文件／统计／文献／文档工具；网络只授权所需公开 HTTPS 域名。未验收能力不据此开放。

前端任务需要实际看页面时，可在本任务 workspace 中启用 `preview: true`。Gemini 随后可自行调用 `bridge-preview /绝对路径/index.html`，读取桌面和手机截图，再修改并重新预览；具体流程见 [页面预览与修正](workspace-preview.md)。它是 Bridge 内置动作，不需要加入 `commands`，也不要求用户逐次确认。旧范围默认没有此能力。

- 自主启用原生读取、目录搜索、文件编辑和必要命令。命令按literal argv执行：不解释管道、重定向、shell变量或常驻服务；复杂转换写成任务脚本再运行。程序路径固定并核对，保留venv启动路径。官方工具未提供Cwd字段时使用该任务的可信默认工作目录；不要为此让模型拼接cd或shell组合。
- 原生文件工具通过PreToolUse检查实际路径、读写范围、保护路径及递归链接；源目录写入和任务目录中的凭据／控制文件另有macOS内核约束。CLI自身的会员认证bootstrap保留正常读取，不宣称整个CLI进程只有任务文件读取权限。
- 模型发出的命令由任务专属本地broker执行：原生工具提交已通过hook的精确argv，Bridge后台在独立的严格目录读写、禁止网络的系统沙箱中运行，清除继承凭据环境，并限制读取其他进程信息。每条命令保留受保护的`command-results/<id>.json`及日志；长命令用`view_file`读取状态／日志，不猜测原生CommandId。仅此工作区会话改用上述命令沙箱，旧材料通道的终端设置保留。取消、超时和结束会清理本任务可识别的命令后代，包含普通setsid脱组进程。对此处实际验证范围如实说明，不把它当作虚拟机级的任意恶意代码隔离。
- 工作区可按新 scope 选择[单层原生子任务](native-readers.md)；builder 写独占输出目录，由父整合，reader／reviewer 只读，均走 headless。角色与槽位权限不得从旧范围继承扩大。
- 不下放自扩权、修改计费／审计／验收、导出凭据、任意浏览器／MCP、超出已启用单层子任务的再委派和系统控制。部署、远程推送、发送消息、跨目录或新第三方上传由主Codex依据当前任务授权处理。
- 保留已有会员认证，禁止API回退；不检查或覆盖`useG1Credits`，credits沿用账户设置，不增加本地次数或Argon配额限制。读取模型目录成功不等于实际生成成功。

## 交付与验收

取得job_id后立即展示同job_id的`bridge_native_panel`。卡片／详情显示真实回收的交付文件及目录范围；查看和下载不发送聊天消息。文本／HTML附件按文本展示，运行HTML原型需使用独立预览环境。

后台保存命令／文件操作审计、文件哈希、源与修改差异，回收交付文件为只读副本。原生工具DONE只表示调用返回，不保证终端成功启动；结合command_validation的requested/started/not_started与受保护记录的实际exit_code判断。模型自行填写的“通过”JSON或说明不代替实跑证据。Codex核对实际文件、事实、测试输出及原目录是否保留，用bridge_review记录具体采用结论。文件存在可作为交付，不要求强行重复粘贴进最终文字；缺少必需文件仍不接受。普通格式问题或已阻断的工具尝试不自动令整份可用内容失败。

查看、刷新、模型目录的只读恢复与原request_id接续不新建生成。任务结束关闭scope，保留worktree、交付和证据，不自动清理现场。稳定 ACP 工作区续聊每轮重新绑定有效授权，规则见[通道与续聊](acp-transport.md)。后台升级只在空闲时进行，保留旧stdio、HTTP和同服务客户端及原生面板地址／凭据。
