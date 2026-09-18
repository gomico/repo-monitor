# repo-monitor（Cloudflare Cron + GitHub Actions 版）

每天统计配置的仓库各自一个分支的**中文文档新增字数**（句数 / 图片数 / 删除行数一并记录），写入 SQLite，生成可离线打开的单文件 HTML 报表，并发布到 GitHub Pages。

- **代码仓库**：本仓库（公开）。Cloudflare Workers Cron 负责每天定时，GitHub Actions `workflow_dispatch` 负责实际执行，不依赖任何本机常驻进程。
- **不依赖本地克隆**：`repo_source = remote`，每仓用一份 bare + blobless 的远端镜像解析提交，统计走工具的网络路线（`--git-url`）。
- **状态与报表**：都在发布分支 —— `state/monitor.db.gz`（跨运行持久化）与 `latest.html`（Pages 从这里发布）。
- 主程序只使用 Python 标准库；`tools/zh-refresh-wordcount/` 是只读的统计工具副本（来源与被删文件见其 `PROVENANCE.md`）。

## 定时触发与 GitHub Actions 运行方式

工作流：`.github/workflows/nightly.yml`

定时器：`cloudflare-scheduler/`。Cloudflare Cron 的表达式配置在 `cloudflare-scheduler/wrangler.jsonc`；Worker 只调用 GitHub workflow dispatch API，不在 Cloudflare 运行 Python、git、SQLite 或报表生成。

```yaml
on:
  workflow_dispatch:                  # 可传 only / slot / no_publish
concurrency: {group: nightly-report, cancel-in-progress: false}
permissions: {contents: write}
```

Worker dispatch 使用配置的目标分支 ref，并固定传 `no_publish=false`；workflow 的 `no_publish` 默认值也为 `false`，所以定时运行会正常发布。冒烟时再显式传 `no_publish=true`。

手动触发（冒烟、补跑、验证）：

```bash
# 冒烟：指定仓库 + 指定槽位 + 不发布（将占位符替换为实际值）
gh workflow run nightly.yml -f only="<repository>" -f slot="<slot-iso8601>" -f no_publish=true

# 全量 + 发布（等价于 Cloudflare 定时触发）
gh workflow run nightly.yml

# 看结果
gh run list --limit <N>
gh run view <run-id> --log | grep -E 'publish|report:|prune:|ok word='
```

每次运行的步骤与要点：

| 步骤 | 说明 |
|---|---|
| 拉取代码 / 拉取发布分支到发布工作树 | 后者是报表与状态库的工作树，也是发布目标 |
| 缓存远端镜像与统计工具缓存 | 跨运行复用 |
| 恢复状态库 | 从发布分支恢复状态库；没有历史状态时从零开始 |
| 应用 CI 配置覆盖 | `cp config/ci.overrides.json monitor.config.local.linux.json` |
| 运行 | `python monitor.py run`（采集 → 报表 → 发布），`run` 成功后顺带清理过期明细 |
| 回写状态库 | `VACUUM` + gzip → 发布分支状态库，提交并推送 |
| 汇总 / 通知 | 打印最新槽位与库内天数；配了 `secrets.DISCORD_WEBHOOK` 才发 Discord |

要点与已知边界：

- **Cloudflare Cron 使用 UTC**：触发时间配置在 Worker 配置文件中，统计槽位边界配置在 `monitor.config.json` 中。触发器变更需要传播时间；迟到的运行仍会被 `--catch-up` 和槽位窗口逻辑夹取到正确窗口。
- **默认分支必须与 workflow dispatch 的 ref 一致**：`actions/checkout` 不带 `ref` 时检出默认分支。
- **`runtime.concurrency` 应采用串行配置**：统计工具会清理共享 `config-root` 下的临时目录并执行缓存淘汰，多个并发进程可能互相影响。
- **状态库体积**：每次回写前 `VACUUM` 再 gzip，明细按 `keep_detail_days` 保留；报表本身会覆盖 latest 输出。
- 发布失败是**非致命**的（`publish.on_error=warn`）：报表照常生成、发布工作树的提交也已建好，只差一次 push；重试按 `retry_delays_s` 配置进行。

## 首次使用（如需新建实例）

基础配置 `monitor.config.json` 里只有平台无关内容：仓库清单、`schedule`、`report`、`common_paths_filter`。平台相关的一切都放在覆盖层里：

```bash
cp config/ci.overrides.json monitor.config.local.linux.json   # 工作流就是这么做的
python3 monitor.py check-config
```

`check-config` 会打印 `repo_source=… mirrors_root=…`；remote 模式下它**不建镜像**，分支存在性用 `git ls-remote --heads` 判断，并跳过生效路径的存在性校验（需要时用 `check-config --deep`，它会建/用镜像做校验）。结果写入 `data/config-check.csv`。

## 日常操作

```bash
# 采集全部启用仓库；结束后自动生成 data/reports/latest.html 并发布
python3 monitor.py run

# 冒烟采集前 N 个仓库 / 只采集指定仓库
python3 monitor.py run --limit <N>
python3 monitor.py run --only <repository> [<repository> ...]

# 启动检查并补跑最近缺失槽位，然后执行一次采集
python3 monitor.py run --limit <N> --catch-up

# 只生成报表 / 手动生成但跳过发布
python3 monitor.py report --days <days>
python3 monitor.py report --no-publish

# 单仓调试/回归（不传 --paths 或传 all 表示全仓）
python3 monitor.py collect --repo <repository> \
  --base <old-commit> --target <new-commit> --paths "<path>,<path>"

# 明细清理
python3 monitor.py prune --dry-run
python3 monitor.py prune --keep-days <days>
```

`run --dry-run` 会执行采集流程但不写 SQLite、原始 CSV 或报表。`run --slot <slot-iso8601>` 可手动指定所属槽位。

## 槽位与启动补跑

`schedule.window_mode` 为 `slot` 时，每天 `schedule.time` 是一个槽位的结束时刻；窗口由该槽位结束时刻和 `schedule.window_hours` 共同决定：

```text
槽位结束时刻 - schedule.window_hours < commit 时间 <= 槽位结束时刻
```

`rolling` 使用当前时间计算窗口，适合调试；`since_last_run` 从最近一次成功采集的 tip 起算。补跑使用固定 slot 窗口，不会改变正常槽位。**补跑（以及任何迟到的运行）会把 tip 夹到窗口末端**（`--before=<窗口末>`），否则会把窗口之后的变更算进那一天。

`--once` 和 `run --catch-up` 会检查最近到点槽位。完整判据是：`repo_daily` 中 `slot_end=<slot>` 的行数大于等于最近一次 `runs.repo_total`；没有历史 `runs` 时一律视为缺失。已有完整数据时打印 `槽位 <slot> 已有数据，跳过补跑`。缺失时最多补跑多少个槽位由 `schedule.catch_up_max_slots` 控制。

同一槽位通过 `(date, repo)` upsert 幂等复用，不会增加重复的 `repo_daily` 行，也不会把已有 `ok` 数字重置为 `no_change/0`。迁移到 Cloudflare 后，workflow 的 `concurrency` 仍会串行重复 dispatch；重复运行在槽位完整时直接跳过，因此不需要修改采集逻辑。

## 配置字段

| 字段 | 说明 |
|---|---|
| `repo_source` | `local`（用 `repos_root` 下的本地工作树）或 `remote`（用远端 bare 镜像 + 工具网络路线） |
| `mirrors_root` | remote 模式的镜像根；每仓一份 `<name>.git`（bare + blobless），带标记文件，无标记/无 ref 视为坏镜像并重建 |
| `tool_cache_root` | 传给统计工具的 `--config-root`，工具的克隆缓存落在这里 |
| `repos_root` | local 模式下监控仓库父目录（remote 模式不使用） |
| `counter_script` | 字数工具路径；相对路径按 `monitor.py` 所在目录解析 |
| `python_exe` | 运行字数工具的解释器；空值使用当前解释器 |
| `schedule.time` | 本地时区每日槽位结束时间，格式 `HH:MM` |
| `schedule.window_hours` | 窗口小时数 |
| `schedule.window_mode` | `slot`、`rolling` 或 `since_last_run` |
| `schedule.missed_run_grace_minutes` | 到点后的正常补跑宽限时间 |
| `schedule.catch_up_on_start` | 启动时是否检查补跑 |
| `schedule.catch_up_max_slots` | 每次启动最多补跑的槽位数 |
| `runtime.concurrency` | 并发仓库数；共享工具缓存时应采用串行配置 |
| `runtime.fetch_timeout_s` / `compare_timeout_s` | fetch / 字数比较超时 |
| `runtime.keep_raw_days` | `data/raw/<date>` 的保留天数 |
| `runtime.keep_detail_days` | `repo_daily_files` 文件级明细的保留天数；聚合行 `repo_daily` 永久保留 |
| `report.default_days` / `top_n_changed` | 报表默认天数和排行条数 |
| `common_paths_filter` | 所有仓库共有的路径过滤器 |
| `repos[].paths_filter` | 单仓路径过滤器 |
| `repos[].paths_mode` | `union` 与公共路径合并；`replace` 只用本仓路径 |
| `repos[].branch` | `auto` 解析远端默认分支，也可填写固定分支 |
| `repos[].enabled` | `false` 时跳过仓库 |
| `publish.enabled` | 启用报表发布 |
| `publish.method` | `git`（Pages 工作树）、`copy`（本地目录）或 `scp` |
| `publish.target` / `remote` / `branch` | 目标工作树或目录；Git 发布使用配置的 remote 与 branch |
| `publish.files` | 控制 latest、dated 等报表文件的发布范围 |
| `publish.publishers` | 可选多目标数组；每项继承顶层配置并独立处理成功/失败 |
| `publish.on_error` | `warn` 记录警告并保持成功；`fail` 令命令失败 |
| `publish.retry_delays_s` | 传输失败后的重试等待秒数列表；设为空列表可关闭重试 |

`collect --paths` 的路径过滤不传参数或传 `all`/`*` 表示全仓；空字符串参数（`""`、`''`）和纯空白也按全仓处理。显式路径没有匹配文件时会打印 `warn: 生效路径 [...] 未匹配任何文件`。

## 配置覆盖

配置按以下顺序做顶层浅合并，后者优先级更高：基础 `monitor.config.json` → 共享 `monitor.config.local.json`（可选）→ 当前平台文件（可选）。runner 是 Linux，所以只读 `monitor.config.local.linux.json` —— 工作流用 `config/ci.overrides.json` 生成它：

覆盖层的具体值应以当前部署配置为准；README 不复制仓库清单、路径、分支、时间或保留周期等实例参数。

发布会将报表以二进制方式复制到目标工作树，只有内容变化时才提交，再执行 `git push`。传输失败后会按 `publish.retry_delays_s` 依次等待重试；重试只包住传输步骤，因此 push 重试推的仍是首次尝试已建好的提交，不会因工作区已干净而误报 `unchanged`。

## 数据库

跨运行的状态库保存在发布分支的 `state/monitor.db.gz`（gzip 压缩，运行前 gunzip 到 `data/monitor.db`，运行后 `VACUUM` + `gzip -n` 回写）。表结构：

- `repos`：当前配置的仓库、分支、路径模式和启用状态。
- `runs`：每轮运行摘要，包括 `slot_end`、窗口、成功/失败计数和 `note`。
- `repo_daily`：唯一汇总事实源，主键为 `(date, repo)`；包含独立的 `base_commit_time/base_commit_subject` 和全仓 `whole_matched_files`；失败行的增量字段保持 `NULL`。
- `repo_daily_files`：展开报表所需的文件级明细，按 `(date, repo, path)` 同日覆盖；按 `keep_detail_days` 清理，聚合行不动。

时间统一保存为带时区的 ISO8601。`word_delta` 已含图片折算；`text_only_delta = word_delta - 200 * image_delta`。修改和删除不计入变化量。

报表输出为 `data/reports/YYYY-MM-DD.html` 和 `data/reports/latest.html`（后者被发布）。HTML 内联 CSS/JS，无外部资源，日期切换和文件明细展开在浏览器本地完成。

报表工具栏的排序下拉默认按当日变化量降序，也可切换为仓库名称升序（**排序只在档位分组内部生效**）；文件明细始终按路径名称升序。

## 排查

- `仓库目录不存在`：local 模式才有；确认 `repo_source` 与覆盖层配置正确。
- 镜像相关报错：坏镜像（无标记/无 ref）会被自动删除重建，日志形如 `mirror: 发现坏镜像 …（无标记），重建`；残留的 `.*.tmp.*` 目录会在启动时清理。
- 计数器报 `could not lock config file …: No such file or directory`：并发共用了 `--config-root`，将 `runtime.concurrency` 调整为串行。
- `fetch 失败`：检查网络与远端 URL；单仓失败不会中止其它仓库，详情见 `logs/collect-YYYY-MM-DD-name.log`（CI 里随作业日志输出）。
- `compare 超时`：增大 `runtime.compare_timeout_s`。
- 线上报表没更新：先看运行日志里有没有 `publish: ok`；`publish: failed` 多为代理/TLS 抖动，重试或重跑一次即可，也可在发布工作树手动 `git push`（提交通常已经建好）。
- Pages 构建可能有延迟，验证时轮询几次再下结论。

## 约束

本项目不安装第三方 Python 包，不使用 pytest；Python 主程序不提供 Web 服务。Cloudflare scheduler 仅提供健康检查和受 secret 保护的手动触发入口，不负责 Python、git 或报表处理。
