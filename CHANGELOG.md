# 更新日志

本项目基于 [NEO SSH-Win Manager](https://github.com/gregorkrebs/NeoSSHWinManager) 二次开发。以下记录本分支的所有重要变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [1.3.0] - 2026-09-20

### 修复
- 内置终端无法打开：打包时误裁剪 QtPrintSupport / Qt6Svg 等 Qt 绑定导致 QtWebEngineWidgets 初始化失败；恢复所需绑定，并在创建 QApplication 前设置 `AA_ShareOpenGLContexts`，提前导入失败不再被静默吞掉。
- 设置面板"检查间隔"步进器与主题菜单按钮在深色模式下显示白底：移除硬编码浅色 QSS，改为 6 套主题（dark / light / ocean / forest / sunset / aurora）统一的 objectName 选择器规则。
- 意外错误弹窗在深色模式下白底、按钮文字残留德语"Details kopieren"：弹窗显式套用当前主题样式表，按钮改为可翻译文案（7 种语言）。
- 桌面快捷方式图标空白：改用原生 IShellLinkW 创建快捷方式（修复中文名称编码失败），保存后调用 SHChangeNotify 强制刷新 Windows 图标缓存。
- 自动更新下载类型不匹配：安装版下载 Setup 安装包，便携版下载单文件 exe（含备份 / 覆盖 / 回滚）。
- `SecureBytes` 析构时在 `__del__` 内调用 `gc.collect()` 造成 GC 重入；bytearray 输入改为拷贝而非共享调用方缓冲。
- 终端桥服务 `websockets.serve()` 启动失败时被静默吞掉、`ready.wait()` 永久等待：worker 线程异常现在会传回调用方。
- 登录失败计数在锁外读取（TOCTOU，CWE-362）：`_record_failure` 直接返回锁定时长与当前计数。
- SQLite 列迁移去 UNIQUE 重试时对整个 DDL 执行 `upper()`，会破坏字符串默认值：改为正则仅移除 UNIQUE 关键字。
- 命名管道 IPC 的 kernel32 调用未声明 `restype/argtypes`：显式声明 64 位 HANDLE 签名（main / cli / IPC 监听器）。
- 启动致命崩溃报告写入当前工作目录且无 ACL 保护：统一写入 `%APPDATA%\SSHDriveMgr\` 并收紧权限。

### 优化
- 启动速度：keyring 改为运行时懒加载（应用本身未使用），主窗口模块延迟到登录成功后导入，登录窗不再被重依赖链阻塞。
- 新增 onedir 免安装打包形态（`SSHDriveMgr-onedir.spec`），冷启动无需自解压，出窗速度约为 onefile 的 3 倍。
- 打包体积裁剪：剔除未使用的 Qt DLL / 插件与重复打包的 src 数据目录（onefile 177.6 → 144.8 MB，CLI 29.9 → 15.4 MB）；CLI 关闭 UPX。
- 主窗口状态栏提示增加关闭按钮与 8 秒自动消失，错误 / 信息提示不再一直驻留。
- `build_dual.ps1` 支持 `-OneDir`、`-NoClean` 参数与 `--help` / `-?` 帮助。

### 持续集成
- Auto Release 与 Manual Build 工作流现在同时构建 onedir 版本，打包为 `SSHDriveMgr-onedir-<版本>.zip` 并生成校验值、随 Release 发布。

### 依赖
- 移除未使用的 pillow。

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
