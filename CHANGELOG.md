# 更新日志

本项目基于 [NEO SSH-Win Manager](https://github.com/gregorkrebs/NeoSSHWinManager) 二次开发。以下记录本分支的所有重要变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [1.4.3] - 2026-10-30

### 修复
- 检查更新使用进程级五分钟缓存、ETag 条件重验证与并发去重，避免启动检查和手动检查重复消耗匿名 GitHub API 额度。
- 识别真正的 API 403/429 限流，遵循 Retry-After / X-RateLimit-Reset 退避；非限流的 403、网络或元数据错误不会被假装成“无更新”。
- API 限流时可通过 GitHub 官网 latest 的受限 HTTPS 重定向查询版本。此降级只用于发现版本、打开官方页面，不提供未经 API 校验的自动下载或安装；原有 SHA256、同类型及执行前重新验证保持不变。
- 官网同样不可达时显示明确恢复时间和官方查看入口，并对失败作短期冷却；下载阶段也保留具体错误原因。
- 补齐七种语言的限流提示，避免异步更新检查在切换设置页后操作已销毁按钮。

## [1.4.2] - 2026-10-30

### 修复
- 根据 CI 原生日志修正 Inno VERSIONINFO 字符串尾部填充：仅忽略末尾 ASCII 空格和 NUL，不放宽版本数字、前缀或其他后缀。
- 安装包脚本和两条发布工作流共享同一版本门；文本使用 Ordinal 精确比较，同时逐项验证 FileVersion/ProductVersion 的四段固定数字元数据。
- 新增不依赖 Inno 的填充/错误元数据回归，并修正真实编译回归对填充的比较规则。
- 新增只读权限、手动触发且不发布 Release 的 Validate Build 工作流；先真实编译验证完整安装包，通过后再创建发布标签。
- 保留原 v1.4.0、v1.4.1 标签不动；两次失败发布的界面、稳定性与安全改进由本版继承。

## [1.4.1] - 未发布（内容并入 1.4.2）

### 修复
- 显式设置 Inno Setup 安装包的固定 FileVersion/ProductVersion 和文本版本字段，使三段文本版本与源码、GUI/CLI 和标签一致，修复安装包构建后的版本校验失败。
- 保留严格版本门，并输出安装包预期及实际 ProductVersion/FileVersion，便于定位后续编译器差异。
- CI 在测试前安装 Inno Setup，使用微型真实编译检查两组版本的 PE 元数据；仅编译、读取版本，不执行安装器。
- 保留原 v1.4.0 标签；1.4.0 自动发布构建失败，本版包含其全部界面、稳定性与安全改进。

## [1.4.0] - 未发布（内容并入 1.4.2）

### 界面
- 统一登录、连接工作区、设置和弹窗的主题样式；增加带文字的导航与操作式空状态。
- 检查间隔改为单外框输入组，按 Qt 实际盒模型保证 40px 外框；新增六主题、七语言和 Windows 缩放尺寸回归。

### 稳定性与安全
- 登录校验值改为带版本及域隔离的格式，避免旧 PBKDF2 校验值可同时充当密钥包装材料；数据库启动迁移不改用户盐或已有加密数据。
- **升级注意**：首次启动会迁移旧登录校验值。回退旧版需使用安全保管的迁移前备份；旧备份可能仍保留旧校验材料，不应公开或随代码分发。
- 按 Windows 用户 SID 隔离单实例和 CLI 管道，收紧 IPC 权限；GUI 与 CLI 应一起更新，避免混用新旧管道协议。
- 终端输入与控制帧分离并限制协议边界；远程文件名按 Windows 路径规则校验，避免本地路径穿越。
- 串行化文件传输并隔离过期回调，完善关闭流程和账户范围内的挂载清理。

### 更新与发布
- 严格按当前运行类型选择更新：安装版选择 Setup，便携单文件版选择 GUI EXE，OneDir 选择对应版本的完整 ZIP。
- OneDir 明确通过浏览器下载完整目录包、退出后解压到新目录启动；暂不自动替换目录，不将 ZIP 放入可执行更新链路。
- 忽略其他分发类型的共享待更新缓存，并在下载、已下载状态和执行更新前再次核对类型，保留完整性及提权限制。
- 构建入口改为显式 `gui / cli / onedir / all`；GitHub Actions 使用 `all -SkipStartupCheck`，仍执行测试、版本与发布产物校验。
- 手动构建旧标签时预先检查构建参数契约；旧版 `-OneDir` 接口不能冒充支持新的 `all` 接口。
- 版本源与 Windows 版本资源升为 1.4.0；正式发布需推送对应标签并通过发布流程。

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
