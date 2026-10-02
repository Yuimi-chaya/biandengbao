# Windows 登录自启动（可选）

自启动默认关闭，只在你明确启用后创建当前用户的计划任务。登录 Windows 后，后台等待你打开 Codex App，再启动带密码的局域网服务。不会自动打开或重启 App，不需要管理员权限或 Windows 密码，也不会开启外网隧道。

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
- 每次登录只启动一次网关，启动成功后后台等待程序即退出。同配置目录已有服务或端口被占用时不会再启动。手动停止服务后不会被反复拉起。关闭或移除自启动也不停止正在运行的网关，更不会关闭 App；停止网关仍用 `stop.py`，自定义部署传同一 `--config`。
- App 在网关启动后重启可能使工具通道失效。先协作停止网关，再重新执行启用命令以绑定当前 App；不保证自动恢复。App 更新改变内部接口时可能需要适配。
- 任务以 `Biandengbao-LAN-` 开头，按安装目录和配置路径区分。状态、选项与日志保存在配置文件旁的 `autostart-*` 目录，不包含模型 API key，但不要公开本机目录或聊天调用上下文。同目录的多个配置不会同时启动多个网关；独立实例应使用不同配置目录和端口。
- 自启动保存安装目录的绝对路径。移动仓库、改配置路径或删除 Python 前，先从旧目录移除任务，再从新目录配置。发现同名但不属于此安装的任务时拒绝改动。
- 目前只支持 Windows Store 版 Codex App。macOS 不安装任务、不修改启动设置，仍用手动启动方式。自动测试与当前会话内试运行不等于完整重启登录验收。
