# QuantLab v0.1.0-preview.2

本次预览版将桌面更新入口改为 GitHub Releases 手动下载安装，并完善安装包内置后端的启动、项目切换与验收。

- 增加 MCP 服务配置、启停、探测和删除界面。
- 增加视觉回归截图与像素差异检查。
- 改进 Word 和 PPT 排版，增加 LibreOffice 页面渲染、截图检查和产物下载。
- 改进研究 Python 环境探测，支持用户安装的 pandas 等依赖。
- 桌面安装包包含前端与 Python sidecar，构建时执行独立启动验收并生成 SHA-256 校验文件。

安装：下载 Windows NSIS 安装包，安装后在托盘“检查更新”可打开此发布页。首次使用需要选择本地项目文件夹。研究子进程仍需 Python 3.11+；Word/PPT 页面预览需本机 LibreOffice。
