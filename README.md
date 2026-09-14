<h1 align="center">SSH 磁盘管理器</h1>

<p align="center">
  一个现代化的 Windows 桌面应用，将远程 SSH 文件系统挂载为 Windows 盘符，集中管理 SSH 访问。<br/>
  基于 <a href="https://github.com/winfsp/sshfs-win">sshfs-win</a> 和 <a href="https://github.com/winfsp/winfsp">WinFsp</a> 构建。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows-0078D6?style=for-the-badge" alt="Platform: Windows"/>
  <img src="https://img.shields.io/badge/python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/PyQt-6-41CD52?style=for-the-badge&logo=qt&logoColor=white" alt="PyQt6"/>
</p>

管理多个 SSH 连接，一键挂载，按用户切换语言，在 Windows 资源管理器中像本地磁盘一样浏览远程路径。

---

## 功能特性

- **一键挂载**远程 SSH 文件系统为 Windows 盘符（通过 SSHFS-Win / WinFsp）。
- **内置 SSH 终端**：每个连接可直接打开终端，使用 Windows OpenSSH 或 PuTTY。
- **内置文件浏览器**：支持 SFTP、FTPS 和 FTP，包含上传/下载、重命名、删除、在线编辑。
- **密码认证**：凭据加密存储，无需 SSH 密钥即可免密登录。
- **公钥认证**。
- **SSH 证书认证**。
- **实时远程系统信息面板**：OS、CPU、内存、磁盘、运行时间、负载、温度。
- **多用户账户**：凭据加密存储（SQLite + cryptography）。
- **按用户设置语言**：简体中文、English、Deutsch、Español、Русский、Nederlands、العربية（阿拉伯语界面自动右对齐）。
- **系统托盘**：快速挂载切换，最小化到托盘。
- **Auto 盘符自动选择**：自动选取最小可用盘符，避免冲突。
- **连接复制粘贴**：选中连接后 Ctrl+C / Ctrl+V 快速复制，或右键菜单操作。
- **记住密码**：机器绑定加密存储，下次启动自动填充。
- "开机启动"和"断线自动重连"选项。
- **可选 CLI  companion**：用于脚本/自动化集成。

## 前置条件

运行前请安装：

| 工具 | 下载 |
| --- | --- |
| **WinFsp** | <https://github.com/winfsp/winfsp/releases> |
| **SSHFS-Win** | <https://github.com/winfsp/sshfs-win/releases> |
| **Python 3.11+** *（仅从源码运行时需要）* | <https://www.python.org/downloads/> |

## 快速开始（开发模式）

```powershell
git clone <你的仓库地址>
cd SSHDriveMgr

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
python main.py
```

首次启动会提示创建管理员用户。之后输入的所有 SSH 凭据都会用该用户密码派生的密钥加密存储。

## 构建为 .exe

项目包含 PyInstaller spec 文件（`SSHDriveMgr.spec`）和 PowerShell 构建脚本（`build_dual.ps1`）。双构建同时产出：

- `SSHDriveMgr.exe` — GUI 应用（窗口子系统）。
- `SSHDriveMgr-cli.exe` — CLI companion（控制台子系统），用于脚本化 CLI 访问。

```powershell
.\build_dual.ps1
```

输出在 `dist/` 目录。

## CLI Companion

CLI companion（`SSHDriveMgr-cli.exe`）不直接读取数据库，而是向已运行的 GUI 应用请求已启用 CLI 访问的连接。

使用前：

1. 启动 `SSHDriveMgr.exe` 并登录。
2. 在添加/编辑对话框中打开目标连接。
3. 为该连接启用 CLI 访问。
4. 生成或复制那里显示的 CLI 访问密钥。

在当前终端中打开交互式 SSH 会话：

```powershell
.\dist\SSHDriveMgr-cli.exe --connect-cli "<access_key>"
```

运行单个远程命令并将输出返回到当前终端：

```powershell
.\dist\SSHDriveMgr-cli.exe --connect-cli "<access_key>" --exec "uname -a"
```

更多示例：

```powershell
.\dist\SSHDriveMgr-cli.exe --connect-cli "<access_key>" --exec "whoami"
.\dist\SSHDriveMgr-cli.exe --connect-cli "<access_key>" --exec "hostname"
.\dist\SSHDriveMgr-cli.exe --connect-cli "<access_key>" --exec "cd /var/www && ls -la"
```

开发期间也可以直接用源码运行：

```powershell
python cli_main.py --connect-cli "<access_key>" --exec "hostname"
```

注意：

- `--connect-cli` 是首选参数。`-connectssh` 也接受以保持兼容。
- 如果省略 `--exec`，CLI 会在当前终端中打开交互式 shell。
- 如果 GUI 应用未运行、未登录、密钥无效或该连接未启用 CLI 访问，命令会以错误退出。

## 挂载原理

sshfs-win 将远程 SSH 路径暴露为 Windows `net use` 可识别的 UNC 格式：

```
net use X: \\sshfs.r\user@host!22\var\www /persistent:no
```

应用根据连接设置自动构建此命令，并通过盘符枚举跟踪挂载状态。

## 项目结构

```
SSHDriveMgr/
├── main.py                        # GUI 入口
├── cli_main.py                    # CLI 入口（companion exe）
├── requirements.txt
├── SSHDriveMgr.spec               # PyInstaller spec（GUI）
├── SSHDriveMgr-cli.spec           # PyInstaller spec（CLI）
├── build_dual.ps1                 # 双构建脚本（GUI + CLI exe）
├── assets/                        # 应用图标
├── src/
│   ├── config.py                  # 数据模型 + JSON 配置
│   ├── database.py                # SQLite  schema + 迁移
│   ├── auth_manager.py            # 用户、会话、加密凭据
│   ├── connection_manager.py      # 连接 CRUD
│   ├── sshfs_controller.py        # 通过 net use 挂载/卸载
│   ├── drive_utils.py             # 盘符工具
│   ├── machine_crypto.py          # 机器绑定加密（记住密码）
│   ├── i18n.py                    # 翻译加载器
│   ├── translations/
│   │   ├── zh.json                # 简体中文
│   │   ├── en.json                # English（默认）
│   │   ├── de.json                # Deutsch
│   │   ├── es.json                # Español
│   │   ├── ru.json                # Русский
│   │   ├── nl.json                # Nederlands
│   │   └── ar.json                # العربية（RTL）
│   └── ui/
│       ├── main_window.py
│       ├── connection_card.py
│       ├── system_info_panel.py
│       ├── system_tray.py
│       ├── theme.py
│       └── dialogs/
│           ├── add_edit_dialog.py
│           ├── settings_dialog.py
│           ├── about_dialog.py
│           ├── login_dialog.py
│           └── system_info_dialog.py
└── tests/
    └── test_config.py
```

## 添加语言

语言按用户存储。添加新语言：

1. 复制 `src/translations/en.json` 为 `src/translations/<code>.json` 并翻译值。
2. 在 `src/i18n.py` 的 `_SUPPORTED` 中添加代码，并在 `src/ui/dialogs/settings_dialog.py` 和 `src/ui/main_window.py` 的 `_LANG_LABELS` 映射中添加。
3. 对于右对齐语言，还需在 `src/i18n.py` 的 `_RTL` 中添加代码——应用会自动镜像布局。
4. 重启应用。

缺失的键会自动回退到英语。

## 运行测试

```powershell
python -m pytest tests/ -v
```

## 关于本项目

本项目基于 [NEO SSH-Win Manager](https://github.com/gregorkrebs/NeoSSHWinManager) 二次开发，在原版基础上进行了以下改造：

- 删除了 Pro 商业授权系统和遥测模块，改为完全自用的免费版本。
- 新增简体中文界面支持。
- 新增 Auto 盘符自动选择功能（自动选取最小可用盘符）。
- 新增连接复制粘贴功能（Ctrl+C / Ctrl+V / 右键菜单）。
- 新增登录页"记住密码"功能（机器绑定 AES-GCM 加密存储）。
- 优化登录流程，消除登录到主界面切换时的闪烁。

## 原作者信息

本项目派生自 **NEO SSH-Win Manager**，由以下原作者开发：

- **Den4ik53** — <https://github.com/Den4ik53>
- **Gregor Krebs** — <https://github.com/gregorkrebs>

原项目仓库：<https://github.com/gregorkrebs/NeoSSHWinManager>
原项目官网：<https://www.neosshwinmanager.org>

NEO SSH-Win Manager 的灵感来自最初的 **SSHWinManager**（由另一位作者用 JavaScript / Electron 编写），是一次完全从零开始的 Python (PyQt6) 重写。

## 许可证

[MIT](LICENSE) — 保留原作者版权声明的前提下，可自由使用、修改和分发。
