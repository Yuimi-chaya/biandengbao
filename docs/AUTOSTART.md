# Windows 登录自启动（可选）

自启动默认关闭，只在你明确启用后创建当前用户的计划任务。登录 Windows 后，后台持续等待和监测 Codex App；同一次开机内每次打开 App，都会启动或重新绑定带密码的局域网服务。不会自动打开或重启 App，不需要管理员权限或 Windows 密码，也不会开启外网隧道。

## 配置

先按 README 运行一次 `run.py --lan`，建立网关密码配置。首次启用建议交给 Codex App 内的 Agent，保留当前聊天调用上下文，以支持新建线程和按需连接旧线程。普通终端缺少这个上下文时会拒绝首次启用，不猜测聊天。

Windows / PowerShell，在仓库根目录运行：

```powershell
# 启用，并立即尝试启动；已有同端口服务时不会重复启动
py -3 -B .\configure.py --autostart enable

# 查看是否启用和最近一次后台等待/启动结果
py -3 -B .\configure.py --autostart status

# 关闭后续自启动，等待中的程序会协作退出
py -3 -B .\configure.py --autostart disable

# 关闭并移除计划任务，保留诊断日志
py -3 -B .\configure.py --autostart remove
```

也可以双击 `configure.cmd` 进入本机选项菜单，回车取消，不会默认启用。如果没有 `py`，使用 `python`。

自定义部署时，配置入口和网关必须使用同一份 `--config`。例如网关配置在 `.local/bedroom.json`，端口为 `8788`：

```powershell
py -3 -B .\configure.py --autostart enable --config '.\.local\bedroom.json' --port 8788
py -3 -B .\configure.py --autostart status --config '.\.local\bedroom.json'
py -3 -B .\configure.py --autostart disable --config '.\.local\bedroom.json'
py -3 -B .\configure.py --autostart remove --config '.\.local\bedroom.json'
```

启用时可传 `--codex-home` 保存实际 App 数据目录；不传时沿用已保存的值或当前 `CODEX_HOME`。重复启用且不传 `--port` 时保留原端口。后台运行仍需你已经配置好的可用模型认证，不会复制或更改 App 认证。

## 行为与边界

- 通常在打开 App 后约 15 秒内启动。连接可能因 App 初始化延长；手机和电脑需在同一局域网。电脑 IP 改变后需使用新地址。
- 后台程序从正在运行的 Store App 找到实际运行时，通过管道所属进程与只读工具清单确认唯一有效通道；不使用旧版本固定路径，不在多个有效通道中随便选择。
- 后台监测程序启动成功后继续运行。App 或其原生运行时退出时，只协作停止本程序启动的网关；再次打开后发现新的进程身份和工具通道，再启动网关。手机需要刷新，网关重启后需重新登录。短暂找不到工具通道时等待，不关闭 App。
- 手动运行 `stop.py` 后，在当前 App 启动周期内保持停止；下次重新打开 App 才自动启动。若要持续关闭服务，先关闭自启动再停止网关。关闭或移除自启动不会停止当前网关或 App；自定义部署传同一 `--config`。
- 同配置目录已有手动网关或端口被占用时，只等待、不接管也不强杀。首次启用前如有手动网关，先用 `stop.py` 停止它，让监测程序启动自己的实例。启动未确认或协作停止超时会报告错误，不盲目重试；查看状态和日志后处理。App 更新改变内部接口时仍可能需要适配。
- 任务以 `Biandengbao-LAN-` 开头，按安装目录和配置路径区分。状态、选项与日志保存在配置文件旁的 `autostart-*` 目录，不包含模型 API key，但不要公开本机目录或聊天调用上下文。同目录的多个配置不会同时启动多个网关；独立实例应使用不同配置目录和端口。
- 自启动保存安装目录的绝对路径。移动仓库、改配置路径或删除 Python 前，先从旧目录移除任务，再从新目录配置。发现同名但不属于此安装的任务时拒绝改动。
- 目前只支持 Windows Store 版 Codex App。macOS 不安装任务、不修改启动设置，仍用手动启动方式。自动测试与当前会话内试运行不等于完整重启登录验收。
