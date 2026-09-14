# 更新日志

本项目基于 [NEO SSH-Win Manager](https://github.com/gregorkrebs/NeoSSHWinManager) 二次开发。以下记录本分支的所有重要变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [未发布]

### 新增
- 简体中文界面支持（`src/translations/zh.json`）。
- Auto 盘符自动选择：连接设置中盘符可选 "Auto"，自动选取最小可用盘符。
- 连接复制粘贴：选中连接后 Ctrl+C / Ctrl+V 快速复制，或右键菜单操作。
- 登录页"记住密码"：机器绑定 AES-GCM 加密存储，下次启动自动填充。

### 移除
- Pro 商业授权系统（`src/pro_manager.py`）。
- 遥测模块（`src/telemetry.py`、`src/ui/dialogs/telemetry_prompt_dialog.py`）。

### 优化
- 登录流程异步化，消除登录到主界面切换时的闪烁。
- 盘符默认值改为 "Auto"。

### 依赖
- `psutil` 版本要求从 `==5.9.0` 改为 `>=6.0`（兼容 Python 3.14）。
