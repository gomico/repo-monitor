# repo-monitor 报表分支

- `latest.html` — 最近一次生成的报表（由 GitHub Actions 覆盖式更新）
- `state/monitor.db.gz` — 采集状态库（gzip 压缩，跨运行持久化；运行前 gunzip、运行后 VACUUM+gzip 回写）
