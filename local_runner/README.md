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

推荐安装目录 `%LOCALAPPDATA%\DailyArxiv`：

| 路径 | 用途 |
|---|---|
| `app\` | 独立安装的项目代码和 `.venv` |
| `api-key.dpapi` | 当前 Windows 账户加密的 DeepSeek API Key，不提交 Git |
| `config.json` | 模型、接口、分类和筛选模式等非密钥设置 |
| `scheduler-state.json` | 最近完成批次和重试状态 |
| `status.json` | 最近一次实际处理结果，包括无新增/失败 |
| `logs\` | 运行日志 |
| `runs\日期\尝试编号\` | 原始抓取、处理结果和独立 Git checkout，便于恢复 |

每次运行使用独立的工作副本，不切换用户原有仓库分支。发布前验证所有论文都有完整中文摘要和 AI 报告；推送冲突会重新合并远端结果。

在专用 `app` checkout 中安装锁定依赖（Python 3.12+）：

```powershell
uv sync --frozen --python 3.12
```

配置密钥与注册任务：

```powershell
powershell -NoProfile -STA -ExecutionPolicy Bypass -File .\local_runner\windows\Set-ApiKey.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\local_runner\windows\Install-WindowsTask.ps1
```

任务名称为 `DailyArxiv Research Update`，以当前账户运行，不要求保存 Windows 登录密码。只有登录该账户后才能解密密钥并使用已有 Git Credential Manager 登录。先完成一次本机验证，再关闭仓库中的自动 `schedule`，保留 GitHub 手动工作流作为备用。

## 检查与手动执行

在 `app` 目录运行：

```powershell
# 查看到期时间、完成批次和失败重试状态（不会调用 AI）
powershell -NoProfile -ExecutionPolicy Bypass -File .\local_runner\windows\Launch-DailyArxiv.ps1 -Status

# 立即运行，即使本批次此前已完成；仍执行论文版本去重
powershell -NoProfile -ExecutionPolicy Bypass -File .\local_runner\windows\Launch-DailyArxiv.ps1 -RunNow

# 正常触发：如果本批次已完成就跳过
Start-ScheduledTask -TaskName 'DailyArxiv Research Update'
```

本机 API Key 与 GitHub Secrets 相互独立；更换密钥时重新打开 `Set-ApiKey.ps1`。GitHub 凭据失效时，在原仓库完成一次 `git fetch` 的登录后再运行。不要把密钥放进 `config.json`。

网站仅将有已发布论文的日期列入日历，因此“没有日期”不等于“没有运行”。例如 2026-10-25 旧任务曾因日期错误中断，后续复抓确认同批列表中保留的 8 个版本已恢复至 10-24，其余 31 个候选仍判为无关。
