# repo-monitor

`repo-monitor` 每天按仓库分支统计中文文档的新增字数、句数、图片数和删除行数，结果写入本地 SQLite，并生成可离线打开的单文件 HTML 报表。主程序只使用 Python 标准库；`tools/zh-refresh-wordcount/` 是只读的统计工具副本。

## 首次使用

在仓库根目录执行：

```bash
python3 monitor.py init-config
```

程序扫描 `repos_root/*` 中含 `.git` 的目录，生成 `monitor.config.json`。如果该文件已经存在，则不会覆盖，而是生成 `monitor.config.generated.json`。第一次使用前按机器修改 `repos_root`、`counter_script` 和 `python_exe`。Windows 原生 Python 建议填写 `C:\Python314\python.exe`；留空使用当前解释器。

检查配置：

```bash
python3 monitor.py check-config
```

它检查仓库、远端默认分支和生效路径，并将结果写入 `data/config-check.csv`。

## 日常操作

```bash
# 采集全部启用仓库；结束后自动生成 data/reports/latest.html
python3 monitor.py run

# 冒烟采集前 3 个仓库
python3 monitor.py run --limit 3

# 只采集指定仓库
python3 monitor.py run --only msmodelslim docs

# 启动检查并补跑最近缺失槽位，然后执行一次采集
python3 monitor.py run --limit 3 --catch-up

# 只生成报表
python3 monitor.py report --days 7
python3 monitor.py report --from 2026-09-10 --to 2026-09-16
# 手动生成/采集但跳过发布
python3 monitor.py report --no-publish

# 单仓调试/回归（不传 --paths 或传 all 表示全仓）
python3 monitor.py collect --repo msmodelslim \
  --base OLD_COMMIT --target NEW_COMMIT --paths "docs/zh,skills"
python3 monitor.py collect --repo msmodelslim --base OLD_COMMIT --target NEW_COMMIT --paths all

# 任务计划或开机自启使用
python3 monitor.py --once
python3 monitor.py --daemon
```

`run --dry-run` 会执行采集流程但不写 SQLite、原始 CSV 或报表。`run --slot 2026-09-15T19:00:00+08:00` 可手动指定所属槽位。

Windows 控制脚本：

```powershell
.\monitor.ps1 -Action start
.\monitor.ps1 -Action status
.\monitor.ps1 -Action run-now
.\monitor.ps1 -Action report
.\monitor.ps1 -Action stop
.\install-autostart.ps1
.\uninstall-autostart.ps1
```

自启只通过 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\repo-monitor.lnk` 快捷方式实现，不写注册表。`install-autostart.ps1` 创建快捷方式失败会直接报错退出；卸载时删除 `repo-monitor.lnk` 即可（也可以直接手动删除该文件）。

## 槽位与启动补跑

默认 `schedule.window_mode` 是 `slot`。每天 `schedule.time` 是一个槽位的结束时刻。例如 `19:00`，在 2026-09-16 10:00 启动时，最近到点槽位是 `2026-09-15T19:00:00+08:00`，窗口为：

```text
2026-09-14T19:00:00+08:00 < commit 时间 <= 2026-09-15T19:00:00+08:00
```

`rolling` 使用当前时间计算窗口，适合调试；`since_last_run` 从最近一次成功采集的 tip 起算。补跑使用固定 slot 窗口，不会改变当天 19:00 的正常槽位。

`--daemon` 启动、`--once` 和 `run --catch-up` 会检查最近到点槽位。完整判据是：`repo_daily` 中 `slot_end=<slot>` 的行数大于等于最近一次 `runs.repo_total`；没有历史 `runs` 时一律视为缺失。已有完整数据时打印 `槽位 <slot> 已有数据，跳过补跑`。缺失时最多补跑 `schedule.catch_up_max_slots` 个槽位，默认 1；更早未补的槽位会写入 `logs/monitor-YYYY-MM-DD.log` 和该次 `runs.note`。补跑完成会记录：

```text
补跑完成 slot=2026-09-15T19:00:00+08:00 ok=3 failed=0
```

同一槽位通过 `(date, repo)` upsert 幂等复用，不会增加重复的 `repo_daily` 行，也不会把已有 `ok` 数字重置为 `no_change/0`。

## 配置字段

| 字段 | 说明 |
|---|---|
| `repos_root` | 监控仓库父目录，仓库路径为 `repos_root/name` |
| `counter_script` | 字数工具路径；相对路径按 `monitor.py` 所在目录解析 |
| `python_exe` | 运行字数工具的解释器；空值使用当前解释器 |
| `schedule.time` | 本地时区每日槽位结束时间，格式 `HH:MM` |
| `schedule.window_hours` | 窗口小时数，默认 24 |
| `schedule.window_mode` | `slot`、`rolling` 或 `since_last_run` |
| `schedule.missed_run_grace_minutes` | 到点后的正常补跑宽限时间 |
| `schedule.catch_up_on_start` | 启动时是否检查补跑，默认 `true` |
| `schedule.catch_up_max_slots` | 每次启动最多补跑的槽位数，默认 1 |
| `runtime.concurrency` | 并发仓库数 |
| `runtime.fetch_timeout_s` / `compare_timeout_s` | fetch / 字数比较超时 |
| `runtime.keep_raw_days` | `data/raw/<date>` 的保留天数 |
| `report.default_days` / `top_n_changed` | 报表默认天数和排行条数 |
| `common_paths_filter` | 所有仓库共有的路径过滤器 |
| `repos[].paths_filter` | 单仓路径过滤器 |
| `repos[].paths_mode` | `union` 与公共路径合并；`replace` 只用本仓路径 |
| `repos[].branch` | `auto` 解析远端默认分支，也可填写固定分支 |
| `repos[].enabled` | `false` 时跳过仓库 |
| `publish.enabled` | 启用报表发布；默认 `false` |
| `publish.method` | `git`（Pages 工作树）、`copy`（本地目录）或 `scp` |
| `publish.target` / `remote` / `branch` | 目标工作树或目录；Git 发布使用配置的 remote 与 branch |
| `publish.files` | `latest` 始终发布；`dated` 额外发布当天报表（Git 发布只提交 `latest.html`） |
| `publish.publishers` | 可选多目标数组；每项继承顶层配置并独立处理成功/失败 |
| `publish.on_error` | `warn` 记录警告并保持成功；`fail` 令命令失败 |
| `publish.retry_delays_s` | 传输失败后的重试等待秒数列表，默认 `[60, 180]`（共 3 次尝试）；设为 `[]` 关闭重试 |

`collect --paths` 的路径过滤不传参数或传 `all`/`*` 表示全仓；空字符串参数（`""`、`''`）和纯空白也按全仓处理。显式路径没有匹配文件时会打印 `warn: 生效路径 [...] 未匹配任何文件`。

## 按平台配置覆盖

配置按以下顺序做顶层浅合并，后者优先级更高：基础 `monitor.config.json` → 共享 `monitor.config.local.json`（可选）→ 当前平台文件（可选）。Windows（`os.name == "nt"`）只读取 `monitor.config.local.windows.json`；Linux/WSL 只读取 `monitor.config.local.linux.json`，另一平台文件不会被读取。三份覆盖文件都不存在时，行为与只使用基础配置一致。

WSL 侧可创建 `monitor.config.local.linux.json`：

```json
{
  "repos_root": "/mnt/d/repos/atomgit/Ascend",
  "counter_script": "tools/zh-refresh-wordcount/count_zh_refresh.py",
  "python_exe": "/usr/bin/python3"
}
```

共享文件适合所有平台相同的本机覆盖；平台文件适合 Windows 与 Linux/WSL 各自的路径和解释器设置。

Git Pages 发布会将 `latest.html` 以二进制方式复制到目标工作树，只有内容变化时才提交，再执行 `git push`；可在 Linux/WSL 的平台覆盖文件中只覆盖 `publish.target`。`git push`、`scp` 与本地复制失败后会按 `publish.retry_delays_s` 依次等待重试（默认 60s、180s，共 3 次尝试），重试只包住传输步骤，因此 push 重试推的仍是首次尝试已建好的提交，不会因工作区已干净而误报 `unchanged`。

## 数据库

数据库位置为 `data/monitor.db`，主要表如下：

- `repos`：当前配置的仓库、分支、路径模式和启用状态。
- `runs`：每轮运行摘要，包括 `slot_end`、窗口、成功/失败计数和 `note`。
- `repo_daily`：唯一汇总事实源，主键为 `(date, repo)`；包含独立的 `base_commit_time/base_commit_subject` 和全仓 `whole_matched_files`；失败行的增量字段保持 `NULL`。
- `repo_daily_files`：展开报表所需的文件级明细，按 `(date, repo, path)` 同日覆盖。

时间统一保存为带时区的 ISO8601，例如 `2026-09-15T19:00:00+08:00`。`word_delta` 已含图片折算；`text_only_delta = word_delta - 200 * image_delta`。修改和删除不计入变化量。

报表输出为 `data/reports/YYYY-MM-DD.html` 和 `data/reports/latest.html`；区间报表输出为 `data/reports/D1_D2.html`。HTML 内联 CSS/JS，无外部资源，日期切换和文件明细展开在浏览器本地完成。

报表工具栏的排序下拉默认按当日变化量降序（失败仓永远最后，同值按名称），也可切换为仓库名称 A→Z；排序只作用于当前日期，主行和展开行作为同一组移动。文件明细始终按路径名称升序，不受仓库排序影响。

## 排查

- `仓库目录不存在`：确认 `repos_root/name` 存在且含 `.git`。
- `远端默认分支解析失败`：先在仓库内确认 `origin`、`refs/remotes/origin/HEAD` 或直接把 `repos[].branch` 改为固定分支。
- `fetch 失败`：检查网络、凭据和远端 URL；单仓失败不会中止其它仓库，详情见 `logs/collect-YYYY-MM-DD-name.log`。
- `compare 超时`：增大 `runtime.compare_timeout_s`；统计工具运行时的 stdout/stderr 已重定向到同一日志。
- 报表没有文件明细：确认采集成功；错误仓只保存失败原因，无增量和文件行。
- 补跑重复执行只看到“槽位已有数据”：这是预期的幂等行为。可在 `runs.slot_end` 和 `repo_daily.slot_end` 中核对槽位、窗口开始和窗口结束。

## 约束

本项目不安装第三方 Python 包，不使用 pytest，不提供 Web 服务、通知、打包 exe 或多分支监控。运行字数工具时会在被监控仓库内部创建临时 scratch 目录作为 CWD，执行结束后无论成功、失败或超时都会删除它。
