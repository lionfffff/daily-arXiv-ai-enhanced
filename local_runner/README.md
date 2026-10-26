# Windows 本机定时运行

由 Windows 任务计划程序直接运行 Python，抓取、筛选、中文总结和摘要翻译均在本机完成，再推送 `data` 分支供原 GitHub Pages 网站读取。运行过程不经过 GitHub Actions 的触发和机器队列。

## 时间与错过后的补跑

- 默认美东周日至周四 20:05（arXiv 公告后 5 分钟），自动处理夏令时：北京时间周一至周五夏令时 08:05、冬令时 09:05。
- 任务以本地 00:00 为起点每 5 分钟检查是否到期，目标时刻本身就在检查序列中。重复检查不等于重复抓取。
- 关机或睡眠错过时间：开机并登录当前 Windows 账户后约 15 秒检查；已经登录但锁屏也能运行。
- 断网错过时间：网络连接事件后约 10 秒检查；连接事件遗漏时，5 分钟轮询补查。
- 只有 GitHub、arXiv 和 AI 接口可连接时才开始。没有联网或未设置密钥，不计作失败。
- 每个公告批次成功一次后不再重复运行；失败至少间隔 10 分钟重试，最多自动尝试 3 次。达到上限时需查看日志，或修复问题后手动运行。
- 电脑关机、系统繁忙或网络服务不可用时，无法保证绝对准点；这些限制与 GitHub 排队无关。启动时间也不同于 AI 处理和网站更新完成时间。
- 若连续关机跨过多个公告批次，补跑读取当时可用的 `/new` 列表，不会伪造已过期列表或把今日论文标成历史日期。历史遗漏需单独恢复。

## 安装布局

这台电脑推荐使用 `D:\ruc\daily-arXiv\windows-runner`，项目代码放在其中的 `app` 子目录。三个 PowerShell 脚本未指定 `-Workspace` 时默认使用当前用户的“文档”目录下 `DailyArxiv`；下文明确传入 D 盘路径，以保证配置密钥、注册任务和手动执行使用同一目录。

不要将代码、运行目录或基础 Python 放在 `AppData` 下。通过 Codex 等 MSIX 应用创建的 AppData 文件可能被 Windows 重定向到该应用的 `LocalCache`；普通任务计划程序看到的路径不同，会报 `0x8007010B`（目录无效）或 `0xFFFD0000`（PowerShell 无法加载脚本）。使用 AppData 外的目录并通过真实任务验证，可以避免这种不一致。

| 路径 | 用途 |
|---|---|
| `app\` | 独立安装的项目代码和 `.venv` |
| `python\` | uv 安装的基础 Python，供 `.venv` 使用 |
| `api-key.dpapi` | 当前 Windows 账户加密的 DeepSeek API Key，不提交 Git |
| `config.json` | 模型、接口、分类和筛选模式等非密钥设置 |
| `scheduler-state.json` | 最近完成批次和重试状态 |
| `status.json` | 最近一次实际处理结果，包括无新增/失败 |
| `logs\` | 运行日志 |
| `runs\日期\尝试编号\` | 原始抓取、处理结果和独立 Git checkout，便于恢复 |

每次运行使用独立的工作副本，不切换用户原有仓库分支。发布前验证所有论文都有完整中文摘要和 AI 报告；推送冲突会重新合并远端结果。

准备 Git 和 uv，并在当前 Windows 账户下完成 GitHub 的 Git Credential Manager 登录。下面命令用于首次安装；已有 `app` 目录时不要重新克隆，直接进入该目录更新。`uv` 不在 PATH 中时，这台电脑可使用 `$env:USERPROFILE\.local\bin\uv.exe` 的完整路径调用。

```powershell
$arxivWorkspace = 'D:\ruc\daily-arXiv\windows-runner'
$arxivApp = Join-Path $arxivWorkspace 'app'
New-Item -ItemType Directory -Force -Path $arxivWorkspace | Out-Null
git clone https://github.com/lionfffff/daily-arXiv-ai-enhanced.git $arxivApp
Set-Location $arxivApp

# 基础 Python 与虚拟环境均保存在普通 Windows 进程可见的位置。
$env:UV_PYTHON_INSTALL_DIR = Join-Path $arxivWorkspace 'python'
uv python install 3.12
$arxivPython = uv python find --no-project --managed-python 3.12
uv sync --frozen --python $arxivPython

# 确认当前账户的 GitHub 凭据可用；第一次可能需要浏览器登录。
git fetch origin data
```

安装后不要直接搬动 `.venv` 或 `python` 目录，虚拟环境包含基础解释器的绝对路径。迁移时在新位置重新运行上述 Python 安装和 `uv sync`。如果在 Codex 中安装，最终应执行下方的真实任务验证，不能只以 Codex 终端中的成功为准。

在 `app` 目录配置密钥与注册任务：

```powershell
$arxivWorkspace = 'D:\ruc\daily-arXiv\windows-runner'
powershell -NoProfile -STA -ExecutionPolicy Bypass -File .\local_runner\windows\Set-ApiKey.ps1 -Workspace $arxivWorkspace
powershell -NoProfile -ExecutionPolicy Bypass -File .\local_runner\windows\Install-WindowsTask.ps1 -Workspace $arxivWorkspace
```

第一条命令打开本机输入窗口，密钥用当前 Windows 账户的 DPAPI 加密保存。不要将密钥写入聊天或 `config.json`。如需更换模型，可在运行目录的 `config.json` 设置 `MODEL_NAME`、`OPENAI_BASE_URL`、`LANGUAGE`、`CATEGORIES`、`RELEVANCE_MODE`；默认使用 DeepSeek V4 Pro、中文、`cs.CV,cs.AI,cs.LG`、语义筛选。

任务名称为 `DailyArxiv Research Update`，以当前账户运行，不要求保存 Windows 登录密码。必须登录该账户后才能运行；冷启动停留在登录界面时不会运行，已登录但锁屏可以运行。安装器只注册任务，不强制开始一次付费处理。

先启动真实任务并检查本机结果，再关闭仓库 `.github/workflows/run.yml` 中的自动 `schedule`，保留 `workflow_dispatch` 手动工作流作为备用。两套定时同时开启可能重复调用 AI；Windows 本机处理不需要 GitHub 的计划任务。

## 检查与手动执行

在 `app` 目录运行：

```powershell
$arxivWorkspace = 'D:\ruc\daily-arXiv\windows-runner'
Set-Location (Join-Path $arxivWorkspace 'app')

# 查看到期时间、完成批次和失败重试状态（不会调用 AI）
powershell -NoProfile -ExecutionPolicy Bypass -File .\local_runner\windows\Launch-DailyArxiv.ps1 -Workspace $arxivWorkspace -Status

# 立即运行，即使本批次此前已完成；仍执行论文版本去重
powershell -NoProfile -ExecutionPolicy Bypass -File .\local_runner\windows\Launch-DailyArxiv.ps1 -Workspace $arxivWorkspace -RunNow

# 正常触发：如果本批次已完成就跳过
Start-ScheduledTask -TaskName 'DailyArxiv Research Update'

# Start-ScheduledTask 返回仅表示已请求启动；稍后检查实际进程结果。
Get-ScheduledTask -TaskName 'DailyArxiv Research Update' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'DailyArxiv Research Update' | Select-Object LastRunTime, LastTaskResult, NextRunTime
Get-Content -LiteralPath (Join-Path $arxivWorkspace 'scheduler-state.json') -Encoding UTF8
Get-Content -LiteralPath (Join-Path $arxivWorkspace 'status.json') -Encoding UTF8
```

任务仍为 `Running` 时应等待完成。`LastTaskResult=0` 也可能表示等待联网、等待密钥或该批次已完成，应同时看 `scheduler-state.json` 的状态；`status.json` 仅在实际处理开始后创建。抓取到 0 篇新增、全部被去重或筛选后没有相关论文都可以是成功完成。具体步骤日志保存在 `runs\日期\尝试编号\logs`，调度日志保存在 `logs\scheduler.log`。

本机 API Key 与 GitHub Secrets 相互独立；更换密钥时重新打开 `Set-ApiKey.ps1` 并传入相同的 `-Workspace`。GitHub 凭据失效时，在 `app` 仓库完成一次 `git fetch` 的登录后再运行。连续失败达到重试上限时，先修复日志中的问题，再使用 `-RunNow` 手动验证；手动运行仍要求联网和有效密钥。

任务的单次总时限为 55 分钟；Python 调度器在 45 分钟后会终止超时的处理进程及其子进程，然后记录失败。休眠时不会主动唤醒电脑，恢复后通过错过任务补跑或下一次 5 分钟检查继续。登录和网络连接事件提供更快触发，5 分钟轮询作为补查。

网站仅将有已发布论文的日期列入日历，因此“没有日期”不等于“没有运行”。例如 2026-10-25 旧任务曾因日期错误中断，后续复抓确认同批列表中保留的 8 个版本已恢复至 10-24，其余 31 个候选仍判为无关。
