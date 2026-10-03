# 桌面管理端

便蹬宝管理端在运行 Codex App 的电脑上管理手机网关。Windows 与 macOS 共用同一套管理接口；桌面窗口和脚本只是两种入口。手机聊天页没有踢出设备、修改账号或本机设置的权限。

## 安装与首次连接

- **Windows x64**：解压完整目录，双击 Biandengbao.exe。不要单独移动 exe；Biandengbao-CLI.exe 和 _internal 也要保留。需要 Windows 10/11、.NET Framework 4.8 和 Edge WebView2 Runtime。缺失时按微软官方指引安装，管理端不静默安装系统组件。
- **Apple Silicon / Intel Mac**：分别下载 macOS-arm64 / macOS-x64 包，解压后把 Biandengbao.app 放到 Applications 再打开。

当前发布包未进行 Windows 代码签名或 Apple Developer ID 签名、公证，可能被 SmartScreen 或 Gatekeeper 拦截。确认下载源后，可在系统提供的安全提示中允许此次运行；不要关闭系统整体安全保护。macOS 可在“系统设置 → 隐私与安全性”处理被阻止的 App。

管理端内置 Python 和界面依赖，不需要另装 Python。Codex App、模型额度或 API 配置仍由你自己的 App 提供。
Windows 自动发现当前适配 Store 版 Codex App；其他分发方式可能需要进一步适配。

1. 保持 Codex App 打开，至少有一个已有聊天。
2. 在管理端“设置”创建网关账号，密码至少 12 位。这不是 OpenAI 或模型 API 密码。
3. 在“连接”点击“读取聊天”，选择一个已有聊天作为桌面工具调用上下文。这里只读列表，不会发送消息。
4. 默认选择“局域网”，保存后到“概览”启动服务，把手机地址复制到手机浏览器。

关闭管理窗口不会停止网关。“停止服务”只断开手机网关，不会结束 Codex App 或 App 中正在执行的任务。

## 连接与自启动

| 模式 | 监听与要求 | 自启动行为 |
| --- | --- | --- |
| 局域网 | 监听局域网地址；手机与电脑在同一网络 | 登录电脑后等待 App，再启动局域网网关 |
| 临时外网 | 选择已安装的 cloudflared 程序；网关仅监听本机，通过临时 HTTPS 地址提供外网访问 | 每次 App 重新打开后恢复网关和隧道；地址可能变化 |
| 已有域名 | 填写不带路径的 HTTPS 源，由你已部署的反向代理转发到本机端口 | 启动网关并允许该域名；不代管代理、域名或证书 |

“登录电脑后自动运行”默认关闭。Windows 使用当前用户计划任务，macOS 使用当前用户 LaunchAgent，不需要保存系统登录密码。这里的“开机自启动”指**登录用户桌面以后**，不是无人登录时的系统服务。不会自动打开、关闭或重启 Codex App。

后台持续观察 App，每次 App 重新打开都重新确认原生进程和工具通道。关闭自启动不停止当前网关。运行时更改连接配置需要确认重启网关，手机需要重新登录，Codex App 不会被重启。

Cloudflare 临时隧道不保证大陆可达或稳定。管理端不自动下载 cloudflared，不修改防火墙或路由器，也不会将本机管理接口穿透出去。网关与隧道分别报告状态，隧道连接慢不会阻塞本机管理。

## 设备与账号

“登录设备”按**浏览器登录会话**列出来源地址、浏览器类型、登录时间、最近活动和到期时间，不做硬件指纹识别。代理模式下来源地址可能是代理地址；同一手机的不同浏览器可能分别出现。

可解除单个会话或退出全部会话。改账号或密码会使所有旧会话失效；等待新消息的连接在下次更新或心跳检查时收到退出通知。已送到手机的内容无法收回。知道密码的人仍能重新登录，需要阻止重新登录时请改密。

密码使用 PBKDF2 加盐存储，不能读回；忘记时在本机重设。修改账号不触碰 Codex provider、模型认证或聊天数据库。

## 版本与更新

概览显示 Codex App、网关的运行状态与可读取到的版本；无法确认的版本明确显示不可用。设置页显示管理端版本、源码提交、配置和日志位置。

“检查 main 更新”只查询本项目 GitHub API，区分一致、有新提交、本地领先和分叉。网络或限流错误明确显示，不自动拉取、执行、覆盖文件或重启进程。main 有新代码不代表已有对应 Release 二进制。

## 脚本和 Agent

Windows 使用 Biandengbao-CLI.exe；Mac 使用 /Applications/Biandengbao.app/Contents/MacOS/Biandengbao；源码使用 python manager.py。非 GUI 命令只需 Python 标准库。以下 PowerShell 示例在 Windows 解压目录执行：

~~~powershell
& '.\Biandengbao-CLI.exe' status
& '.\Biandengbao-CLI.exe' contexts
& '.\Biandengbao-CLI.exe' start
& '.\Biandengbao-CLI.exe' devices
& '.\Biandengbao-CLI.exe' check-update
~~~

设置账号时，用系统凭据输入框获得输入，密码通过标准输入传入，不进入进程参数：

~~~powershell
$credential = Get-Credential -UserName 'admin' -Message '设置便蹬宝网关账号，不是 OpenAI 密码'
$credential.GetNetworkCredential().Password | & '.\Biandengbao-CLI.exe' account --username $credential.UserName --password-stdin --yes
~~~

例如，已绑定聊天的电脑要切回局域网并启用自启动：

~~~powershell
'{"network":{"mode":"lan"}}' | & '.\Biandengbao-CLI.exe' configure --json-stdin --yes
& '.\Biandengbao-CLI.exe' autostart --enabled true
~~~

configure 保留未指定的端口、数据目录和调用上下文；--yes 在运行时表示允许重启**网关**。

| 操作 | 命令 |
| --- | --- |
| 撤销一个会话 | revoke --id 从 devices 返回的 ID --yes |
| 全部退出 | revoke-all --yes |
| 停网关 | stop --yes |
| 关闭自启动 | autostart --enabled false |
| 保存管理端深色外观 | appearance --mode dark |
| 退出管理后台，保留网关 | quit-manager --yes |
| 输出到文件 | 查询命令追加 --output 绝对文件路径 |

成功返回包含 ok 和 result 的 JSON，失败返回 JSON 错误和非零退出码。变更超时后先查 status，不要自动重放。

### 本机 HTTP 接口

GUI 与 CLI 都调用 127.0.0.1 上的 POST /api/v1/操作名。请求使用 application/json 和独立的 Bearer 能力令牌。后台首次启动生成随机端口、令牌，记录在配置目录的 .manager/control.json；不要公开该文件、令牌或带令牌的日志。它与手机密码、模型 API key 无关。

操作包括 status、contexts、devices、devices/revoke、devices/revoke-all、account、settings、service/start、service/stop、autostart、autostart/remove 和 updates/check。参数和校验共用 bridge/manager.py、bridge/gateway_admin.py，不另设脚本专属配置副本。

接口校验 Host、Origin、能力令牌，拒绝跨站浏览器请求。手机登录不能获得管理权限；没有任意 shell、任意文件读取或远程下载安装接口。同一操作系统用户本来就能修改本机配置，因此它不是抵御本机账号失陷的边界。

## 配置与已有部署

默认配置：Windows 为 %LOCALAPPDATA%/Biandengbao/config.json；Mac 为 ~/Library/Application Support/Biandengbao/config.json。构建产物不包含个人密码、聊天、App 认证或本机部署路径。

源码 run.py、stop.py、configure.py 与管理端使用同一默认配置。不同安装目录的 CLI、GUI 和配置脚本会复用该配置的本机管理后台、网关与自启动任务；账号修改和设备解除登录作用于同一个网关。关闭任一管理窗口不停止服务。

同一配置只保留一个自启动归属，继续使用已经保存的启动程序和连接选项，不因打开另一份源码或二进制再注册任务。旧版本按安装目录命名的单套任务会沿用；发现同配置已有多套记录时拒绝创建新任务，需先停用旧归属。安装目录必须保留，移动前仍需从原安装停用/移除自启动。

已有源码部署可在所有命令追加同一个 --config 绝对路径，复用已有网关账号。**先停止旧网关和旧监听程序，再升级或切换到二进制；不要让两套自启动管理同一端口。**管理端不会默默迁移其他目录的任务或接管未知服务。旧版缺少本机管理接口时会明确提示升级，不会伪造设备列表。

Windows 旧 `Biandengbao-LAN` 任务可使用仓库的[自启动迁移工具](STARTUP-MIGRATION.md)，通过已有本机接口交接，无需重新编译管理端。默认只读预检，正式执行需要确认；保留管理端账号与连接设置，并备份旧配置和任务。

自启动依赖安装目录不变。移动安装目录前先关闭/移除旧安装的自启动，新路径需重新启用。勿在下载临时目录或 macOS App Translocation 路径中配置自启动。

## 构建和验证边界

各平台安装 packaging/requirements-build.txt 固定的构建依赖，然后运行 tools/build_manager.py。Windows 生成 GUI 和 JSON CLI，Mac 分别构建 Apple Silicon、Intel App。Release 提供 SHA-256 文件；不打包 .local、.tmp 或个人配置。

tools/smoke_manager.py 用临时配置和故意不存在的 App 通道，验证打包程序、登录撤销、改密和协作停止，不对真人聊天执行操作。真实 App 绑定、重复打开、自启动、隧道可达性仍需对应平台实机验收；离线测试和构建成功不等于这些场景已通过。
