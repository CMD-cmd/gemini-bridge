# 页面预览与修正

v0.13 已实测页面实现、截图阅读、自主修正、搜索／筛选交互与完整页面截图。普通工作区可走已开放的 auto/ACP，headless 保留。

在新 workspace scope 中启用 preview:true，代码用 code_repository 建立独立 worktree，旧 scope 不扩权。模型在授权目录实现／构建后调用 `bridge-preview /绝对路径/index.html`，读取受保护截图，修改再预览。该内置动作无需加入 commands。

## 有限交互

需要检查按钮／表单状态时，写本任务 actions.json，再调用：

```text
bridge-preview /绝对路径/index.html --actions /绝对路径/actions.json
```

```json
{"actions":[{"action":"fill","selector":"#query","value":"示例"},{"action":"click","selector":"#submit"},{"action":"wait_for","selector":"#result","state":"visible"}]}
```

只接受最多 20 个有界 CSS 动作：click、fill、select、check、press、wait_for。press 仅允许固定导航／确认键；不执行模型提供的 JavaScript、URL 或任意浏览器脚本。每个桌面／手机视口独立执行，逐项核对动作结果与截图。动作失败、无效选择器或导航越界如实记失败。

## 截图与验收

使用 1280×900 和 390×844 视口；完整页面截图限制宽 4096、高 12000、总 2400 万像素。核对 manifest 中各 screenshots[].coverage 的完整／截断状态，不能将受限截图称为整页覆盖。实际用 view_file 打开截图，检查首屏及下方区域；日志／布局指标不代替读图。

可信运行器冻结静态源文件，保存源码／PNG 哈希、交互结果、布局指标、截图覆盖与清理证据；模型不能改这些记录。取消／结束回收浏览器、监听端口和临时目录，已有交付保留。只加载本次受控回环资源，不接用户登录态、外网接口、常驻开发服务器或动态后端；这些需求由 Codex 按任务单独安排。

Codex 核对原件、实际差异、动作结果、截图读取记录及交付源码哈希，实际查看关键截图后记录验收。执行成功、截图生成、截图读过与最终采用分别判断。
