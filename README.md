# repo-monitor（GitHub Actions 版）

每天统计 46 个 Ascend 仓库各自一个分支的**中文文档新增字数**（句数 / 图片数 / 删除行数一并记录），写入 SQLite，生成可离线打开的单文件 HTML 报表，并发布到 GitHub Pages。

- **代码仓库**：本仓库（公开）。运行形态 = GitHub Actions，不依赖任何本机常驻进程。
- **不依赖本地克隆**：`repo_source = remote`，每仓用一份 bare + blobless 的远端镜像解析提交，统计走工具的网络路线（`--git-url`）。
- **状态与报表**：都在 `gh-pages` 分支 —— `state/monitor.db.gz`（跨运行持久化）与 `latest.html`（Pages 从这里发布）。
- 主程序只使用 Python 标准库；`tools/zh-refresh-wordcount/` 是只读的统计工具副本（来源与被删文件见其 `PROVENANCE.md`）。
- Windows 控制脚本与开机自启（`monitor.ps1` / `install-autostart.ps1` / `uninstall-autostart.ps1`）**不在本仓库**，它们属于本机那套（`D:\repos\local\repo-monitor`）。

## GitHub Actions 运行方式

工作流：`.github/workflows/nightly.yml`

```yaml
on:
  schedule: [{cron: '0 1 * * *'}]   # 01:00 UTC = 09:00 Asia/Shanghai；作业内设 TZ=Asia/Shanghai
                                    # 运行时刻 ≠ 槽位边界：槽位边界见 monitor.config.json 的 schedule.time（19:00）
  workflow_dispatch:                  # 可传 only / slot / no_publish
concurrency: {group: nightly-report, cancel-in-progress: false}
permissions: {contents: write}
```

手动触发（冒烟、补跑、验证）：

```bash
# 冒烟：单仓 + 指定历史槽位 + 不发布
gh workflow run nightly.yml -f only=msmodelslim -f slot='2026-09-09T19:00:00+08:00' -f no_publish=true

# 全量 + 发布（等价于定时那次）
gh workflow run nightly.yml

# 看结果
gh run list --limit 5
gh run view <run-id> --log | grep -E 'publish|report:|prune:|ok word='
```

每次运行的步骤与要点：

| 步骤 | 说明 |
|---|---|
| 拉取代码 / 拉取 `gh-pages` 到 `site/` | 后者是报表与状态库的工作树，也是发布目标 |
| 缓存 `data/mirrors` + `data/toolcache` | 远端镜像与统计工具缓存，跨运行复用（约几百 MB） |
| 恢复状态库 | 有 `site/state/monitor.db.gz` 就 gunzip 到 `data/monitor.db`，否则首次从零开始 |
| 应用 CI 配置覆盖 | `cp config/ci.overrides.json monitor.config.local.linux.json` |
| 运行 | `python monitor.py run`（采集 → 报表 → 发布），`run` 成功后顺带清理过期明细 |
| 回写状态库 | `VACUUM` + `gzip -n -9` → `site/state/monitor.db.gz`，提交并推送 |
| 汇总 / 通知 | 打印最新槽位与库内天数；配了 `secrets.DISCORD_WEBHOOK` 才发 Discord |

要点与已知边界：

- **cron 常延迟 5–30 分钟**：口径不受影响 —— 迟到的运行会被 `--before=<窗口末>` 夹取到正确槽位窗口，只有"报表上线时刻"会漂。
- **默认分支必须是 `main`**：`actions/checkout` 不带 `ref` 时检出默认分支；曾因 `gh-pages` 先生成被误设为默认分支。
- **`runtime.concurrency` 必须是 1**：统计工具每次调用都会清理自己 `config-root` 下的临时目录并按硬编码 50 MiB 做 LRU 淘汰，多个并发进程共用同一 `--config-root` 会互删对方正在克隆的目录（实测并发 4 时 46 仓里 25 仓报 `could not lock config file …: No such file or directory`）。全量一次约 6–10 分钟。
- **状态库体积**：每次回写前 `VACUUM` 再 gzip（实测 1.1 MB → 116 KB），明细按 `keep_detail_days` 保留；报表本身每天覆盖 `latest.html`。
- 发布失败是**非致命**的（`publish.on_error=warn`）：报表照常生成、站点工作树的提交也已建好，只差一次 push；重试按 `retry_delays_s` 自动进行（默认 60s、180s）。

## 首次使用（如需新建实例）

基础配置 `monitor.config.json` 里只有平台无关内容：46 仓清单、`schedule`、`report`、`common_paths_filter`。平台相关的一切都放在覆盖层里：

```bash
cp config/ci.overrides.json monitor.config.local.linux.json   # 工作流就是这么做的
python3 monitor.py check-config
```

`check-config` 会打印 `repo_source=… mirrors_root=…`；remote 模式下它**不建镜像**，分支存在性用 `git ls-remote --heads` 判断，并跳过生效路径的存在性校验（需要时用 `check-config --deep`，它会建/用镜像做校验）。结果写入 `data/config-check.csv`。

## 日常操作

```bash
# 采集全部启用仓库；结束后自动生成 data/reports/latest.html 并发布
python3 monitor.py run

# 冒烟采集前 3 个仓库 / 只采集指定仓库
python3 monitor.py run --limit 3
python3 monitor.py run --only msmodelslim docs

# 启动检查并补跑最近缺失槽位，然后执行一次采集
python3 monitor.py run --limit 3 --catch-up

# 只生成报表 / 手动生成但跳过发布
python3 monitor.py report --days 7
python3 monitor.py report --no-publish

# 单仓调试/回归（不传 --paths 或传 all 表示全仓）
python3 monitor.py collect --repo msmodelslim \
  --base OLD_COMMIT --target NEW_COMMIT --paths "docs/zh,skills"

# 明细清理（默认 14 天，run 之后自动执行）
python3 monitor.py prune --dry-run
python3 monitor.py prune --keep-days 14
```

`run --dry-run` 会执行采集流程但不写 SQLite、原始 CSV 或报表。`run --slot 2026-09-15T19:00:00+08:00` 可手动指定所属槽位。

## 槽位与启动补跑

默认 `schedule.window_mode` 是 `slot`。每天 `schedule.time` 是一个槽位的结束时刻。例如 `19:00`，在 2026-09-16 10:00 启动时，最近到点槽位是 `2026-09-15T19:00:00+08:00`，窗口为：

```text
2026-09-14T19:00:00+08:00 < commit 时间 <= 2026-09-15T19:00:00+08:00
```

`rolling` 使用当前时间计算窗口，适合调试；`since_last_run` 从最近一次成功采集的 tip 起算。补跑使用固定 slot 窗口，不会改变当天 19:00 的正常槽位。**补跑（以及任何迟到的运行）会把 tip 夹到窗口末端**（`--before=<窗口末>`），否则会把窗口之后的变更全算进那一天（实测虚高过 9 倍）。

`--once` 和 `run --catch-up` 会检查最近到点槽位。完整判据是：`repo_daily` 中 `slot_end=<slot>` 的行数大于等于最近一次 `runs.repo_total`；没有历史 `runs` 时一律视为缺失。已有完整数据时打印 `槽位 <slot> 已有数据，跳过补跑`。缺失时最多补跑 `schedule.catch_up_max_slots` 个槽位，默认 1。

同一槽位通过 `(date, repo)` upsert 幂等复用，不会增加重复的 `repo_daily` 行，也不会把已有 `ok` 数字重置为 `no_change/0`。

## 配置字段

| 字段 | 说明 |
|---|---|
| `repo_source` | `local`（默认，用 `repos_root` 下的本地工作树）或 `remote`（用远端 bare 镜像 + 工具网络路线）。**本仓库用 `remote`** |
| `mirrors_root` | remote 模式的镜像根，默认 `data/mirrors`；每仓一份 `<name>.git`（bare + blobless），带标记文件，无标记/无 ref 视为坏镜像并重建 |
| `tool_cache_root` | 传给统计工具的 `--config-root`，默认 `data/toolcache`（工具的克隆缓存落在这里） |
| `repos_root` | local 模式下监控仓库父目录；**本仓库基础配置里没有这个键**（remote 模式不使用） |
| `counter_script` | 字数工具路径；相对路径按 `monitor.py` 所在目录解析（本仓库用默认值 `tools/zh-refresh-wordcount/count_zh_refresh.py`） |
| `python_exe` | 运行字数工具的解释器；空值使用当前解释器（本仓库留空） |
| `schedule.time` | 本地时区每日槽位结束时间，格式 `HH:MM` |
| `schedule.window_hours` | 窗口小时数，默认 24 |
| `schedule.window_mode` | `slot`、`rolling` 或 `since_last_run` |
| `schedule.missed_run_grace_minutes` | 到点后的正常补跑宽限时间 |
| `schedule.catch_up_on_start` | 启动时是否检查补跑，默认 `true` |
| `schedule.catch_up_max_slots` | 每次启动最多补跑的槽位数，默认 1 |
| `runtime.concurrency` | 并发仓库数（**本仓库必须为 1**，原因见上） |
| `runtime.fetch_timeout_s` / `compare_timeout_s` | fetch / 字数比较超时 |
| `runtime.keep_raw_days` | `data/raw/<date>` 的保留天数 |
| `runtime.keep_detail_days` | `repo_daily_files` 文件级明细的保留天数，默认 14（`0` 关闭清理）；聚合行 `repo_daily` 永久保留 |
| `report.default_days` / `top_n_changed` | 报表默认天数和排行条数 |
| `common_paths_filter` | 所有仓库共有的路径过滤器 |
| `repos[].paths_filter` | 单仓路径过滤器 |
| `repos[].paths_mode` | `union` 与公共路径合并；`replace` 只用本仓路径 |
| `repos[].branch` | `auto` 解析远端默认分支，也可填写固定分支 |
| `repos[].enabled` | `false` 时跳过仓库 |
| `publish.enabled` | 启用报表发布；默认 `false` |
| `publish.method` | `git`（Pages 工作树）、`copy`（本地目录）或 `scp` |
| `publish.target` / `remote` / `branch` | 目标工作树或目录；Git 发布使用配置的 remote 与 branch（本仓库：`site` / `origin` / `gh-pages`） |
| `publish.files` | `latest` 始终发布；`dated` 额外发布当天报表（Git 发布只提交 `latest.html`） |
| `publish.publishers` | 可选多目标数组；每项继承顶层配置并独立处理成功/失败 |
| `publish.on_error` | `warn` 记录警告并保持成功；`fail` 令命令失败 |
| `publish.retry_delays_s` | 传输失败后的重试等待秒数列表，默认 `[60, 180]`（共 3 次尝试）；设为 `[]` 关闭重试 |

`collect --paths` 的路径过滤不传参数或传 `all`/`*` 表示全仓；空字符串参数（`""`、`''`）和纯空白也按全仓处理。显式路径没有匹配文件时会打印 `warn: 生效路径 [...] 未匹配任何文件`。

## 配置覆盖

配置按以下顺序做顶层浅合并，后者优先级更高：基础 `monitor.config.json` → 共享 `monitor.config.local.json`（可选）→ 当前平台文件（可选）。runner 是 Linux，所以只读 `monitor.config.local.linux.json` —— 工作流用 `config/ci.overrides.json` 生成它：

```json
{
  "repo_source": "remote",
  "mirrors_root": "data/mirrors",
  "tool_cache_root": "data/toolcache",
  "counter_script": "tools/zh-refresh-wordcount/count_zh_refresh.py",
  "python_exe": "",
  "runtime": {"concurrency": 1, "keep_raw_days": 7, "keep_detail_days": 14},
  "publish": {"enabled": true, "method": "git", "target": "site", "branch": "gh-pages", "files": ["latest"]}
}
```

Git Pages 发布会将 `latest.html` 以二进制方式复制到目标工作树，只有内容变化时才提交，再执行 `git push`。`git push`、`scp` 与本地复制失败后会按 `publish.retry_delays_s` 依次等待重试（默认 60s、180s，共 3 次尝试）；重试只包住传输步骤，因此 push 重试推的仍是首次尝试已建好的提交，不会因工作区已干净而误报 `unchanged`。

## 数据库

跨运行的状态库保存在 `gh-pages` 分支的 `state/monitor.db.gz`（gzip 压缩，运行前 gunzip 到 `data/monitor.db`，运行后 `VACUUM` + `gzip -n` 回写）。表结构：

- `repos`：当前配置的仓库、分支、路径模式和启用状态。
- `runs`：每轮运行摘要，包括 `slot_end`、窗口、成功/失败计数和 `note`。
- `repo_daily`：唯一汇总事实源，主键为 `(date, repo)`；包含独立的 `base_commit_time/base_commit_subject` 和全仓 `whole_matched_files`；失败行的增量字段保持 `NULL`。
- `repo_daily_files`：展开报表所需的文件级明细，按 `(date, repo, path)` 同日覆盖；按 `keep_detail_days`（默认 14）清理，聚合行不动。

时间统一保存为带时区的 ISO8601，例如 `2026-09-15T19:00:00+08:00`。`word_delta` 已含图片折算；`text_only_delta = word_delta - 200 * image_delta`。修改和删除不计入变化量。

报表输出为 `data/reports/YYYY-MM-DD.html` 和 `data/reports/latest.html`（后者被发布）。HTML 内联 CSS/JS，无外部资源，日期切换和文件明细展开在浏览器本地完成。

报表工具栏的排序下拉默认按当日变化量降序，也可切换为仓库名称升序（**排序只在档位分组内部生效**）；文件明细始终按路径名称升序。

## 排查

- `仓库目录不存在`：local 模式才有；本仓库应确认 `repo_source=remote` 且覆盖层已生成。
- 镜像相关报错：坏镜像（无标记/无 ref）会被自动删除重建，日志形如 `mirror: 发现坏镜像 …（无标记），重建`；残留的 `.*.tmp.*` 目录会在启动时清理。
- 计数器报 `could not lock config file …: No such file or directory`：并发共用了 `--config-root`，把 `runtime.concurrency` 调回 1。
- `fetch 失败`：检查网络与远端 URL；单仓失败不会中止其它仓库，详情见 `logs/collect-YYYY-MM-DD-name.log`（CI 里随作业日志输出）。
- `compare 超时`：增大 `runtime.compare_timeout_s`。
- 线上报表没更新：先看运行日志里有没有 `publish: ok`；`publish: failed` 多为代理/TLS 抖动，重试或重跑一次即可，也可在 `gh-pages` 工作树手动 `git push`（提交通常已经建好）。
- Pages 构建有约 1 分钟延迟；验证时轮询几次再下结论。

## 约束

本项目不安装第三方 Python 包，不使用 pytest，不提供 Web 服务、通知（除可选的 Discord webhook）、打包 exe 或多分支监控。
