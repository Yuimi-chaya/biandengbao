# 旧自启动迁移

如果旧源码网关仍占着端口，而桌面管理端显示未运行或后台状态为 `existing_listener`，两套程序并没有共享管理权限。需要先停旧监听，再把网关交给管理端。仅修改仓库源码不会更新已编译程序内置的后端。

## 适用范围

仓库里的 `tools/migrate_startup.py` 可通过 v0.2.0 已有的本机管理接口完成迁移，**无需重新编译或替换管理程序**。

- 当前支持 Windows 旧任务 `Biandengbao-LAN`，任务必须属于当前用户，使用 `pythonw.exe -B "旧监听脚本绝对路径"`。
- 必须明确提供旧网关配置、旧监听状态目录和旧监听脚本；不扫描、关闭未知服务或其他用户的任务。
- 目标管理端已打开、已设置密码、已选定聊天调用上下文，Codex App 正在运行。旧网关和管理端须使用同一端口。
- **保留目标管理端的账号密码、端口、连接模式和自启动开关**，不会把旧密码覆盖到管理端。手机切换后需使用管理端账号重新登录。
- 不停止 Codex App，不修改模型认证、聊天数据库或任务。不执行聊天发送、压缩或审批。

其他源码安装使用 `configure.py` 注册的带后缀任务、macOS LaunchAgent 或未知启动方式，不在这个工具的迁移范围内。应使用其原安装的停用命令，正常停止网关后再切换。

## 预检与执行

迁移工具需要 Python 3.9+，仅使用标准库。已有管理端本身不要求安装 Python；没有 Python 时，可由部署 Agent 在已具备 Python 的环境执行迁移。

以下 PowerShell 示例假定旧安装文件位于 `C:\Bridge`，新管理端已使用默认配置打开。请把旧安装的三个路径换成真实路径：

```powershell
$migrationArgs = @(
    '--legacy-config', 'C:\Bridge\work\codex-mobile-dev\.local\config.json',
    '--legacy-control', 'C:\Bridge\outputs\.bridge-autostart',
    '--legacy-worker', 'C:\Bridge\outputs\bridge_autostart.py'
)
py -3 -B '.\tools\migrate_startup.py' @migrationArgs
```

默认只读检查，返回 JSON。`state: ready` 表示满足迁移条件，不代表已经切换。默认目标配置是 `%LOCALAPPDATA%\Biandengbao\config.json`；自定义安装需追加 `--target-config` 和绝对路径。

确认预检中的端口、连接模式和 `targetAutostart` 正确后：

```powershell
py -3 -B '.\tools\migrate_startup.py' @migrationArgs --apply --yes
```

执行顺序：

1. 保存两套配置、监听选项、旧任务 XML 和带校验值的迁移记录。
2. 暂停目标监听程序，等待其退出，防止提前抢占旧端口。
3. 禁用旧计划任务并写入旧监听的协作停用标记，等待旧监听退出。
4. 核对旧网关 PID、进程创建时间、配置及端口归属，通过网关停止接口正常关闭。
5. 按目标原有自启动开关恢复目标监听并启动网关，确认唯一监听者属于管理端，检查账号配置字节未变。

成功返回 `state: migrated` 和 `backupDirectory`。再次执行已完成的迁移返回 `already_migrated`，不会再停服务或创建第二份备份。未启用目标自启动时只启动当前会话，不擅自启用登录自启动。

默认备份保存在目标配置目录的 `migration-backups` 下，可用 `--backup-dir` 指定其他绝对目录。**备份含账号验证数据，禁止上传、分享或放进 Git 仓库。**工具拒绝 Git 仓库内的备份目录。

## 验收与故障恢复

在管理端确认网关运行、手机地址和登录设备可读取，再用管理端账号从手机重新登录。旧任务应处于禁用状态，新任务是否启用取决于迁移前的管理端设置。手机的真实发送、附件和压缩仍需在专用测试聊天中单独验收。

超时、身份变化或端口被未知程序占用时，工具会停止并报告失败阶段和备份位置；不会强杀、自动回滚或无条件重复请求。先查看备份中的 `manifest.json`、管理端状态和日志，确认当前归属。失败不保证所有步骤都没执行，不要只看终端报错就同时启用两套自启动。

需要回到旧安装时：

1. 使用目标管理端关闭自启动并正常停止网关，确认目标监听已退出、原端口已释放。关闭自启动本身不会停止当前网关。
2. 确认旧配置和旧监听选项未变；如需恢复，先核对备份的 SHA-256 和原文件范围，不能覆盖仍在运行的服务配置。
3. 只有迁移前旧监听没有停用标记时，才移除旧状态目录的 `disabled` 文件。
4. 只有 `manifest.json` 的 `legacyEnabled` 为 `true` 时，才重新启用并启动旧任务 `Biandengbao-LAN`。工具未删除任务，不需要重新注册。

回退示例适用于原任务启用、原停用标记不存在，并且新网关和监听已确认退出的情况：

```powershell
Remove-Item -LiteralPath 'C:\Bridge\outputs\.bridge-autostart\disabled'
Enable-ScheduledTask -TaskName 'Biandengbao-LAN' -TaskPath '\' | Out-Null
Start-ScheduledTask -TaskName 'Biandengbao-LAN' -TaskPath '\'
```

回退后使用旧网关账号登录。不要在新网关仍占端口时启动旧任务，也不要以重启 Codex App 代替停止网关。
