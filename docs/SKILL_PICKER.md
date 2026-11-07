# 输入框里的 Gemini 入口

`@Gemini Bridge` 的彩色标记来自插件 `interface.composerIcon`。`/gemini` 中的“Gemini 协作流程”是主 Skill；两者使用同一个 Bridge 后台。应用、文件夹和聊天的搜索结果属于各自的类别，不代表又运行了一套桥接。

插件另附一个只读查看 Skill，历史版本将它也命名为“Gemini Bridge”，造成两个相同名字。主流程已经包含打开原生卡片的指引，因此安装主 Skill 时默认通过配置隐藏这个冗余快捷项。文件保留，卡片、详情和状态工具继续可用。若只安装查看插件，仍可单独使用它。

已有用户运行：

```sh
python3 script/dedupe_skills.py --apply
```

脚本会检查主流程、备份配置、只关闭 `gemini-bridge-local:gemini-bridge-view` 的显示入口，并保留其他设置。需要两个独立 Skill 的用户可在初次安装主 Skill 时加入 `--keep-view-shortcut`。

彩色插件图标与 Skill 图标由不同界面组件绘制。当前部分桌面端的 Skill 列表固定使用方块，即使 `skills/list` 已返回有效的 `iconSmall` 路径；这个宿主问题无法靠继续修改 Bridge 图片来修复。源资源仍保留彩色 PNG/SVG，供支持自定义 Skill 图标的宿主使用。

技能名称与启用状态可能被客户端缓存。先刷新当前聊天；仍不更新时，可在任务空闲后重开客户端，不必重连 Bridge MCP。

参考：[Codex 技能启停配置](https://learn.chatgpt.com/docs/build-skills?product=breeze)、[桌面技能列表图标问题](https://github.com/openai/codex/issues/40224)。
