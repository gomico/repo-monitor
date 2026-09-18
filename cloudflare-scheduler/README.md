# repo-monitor Cloudflare scheduler

这个 Worker 只负责 Cloudflare Cron 调度和调用 GitHub REST API；Python、git clone、SQLite、报表生成、发布分支提交与 push 仍全部运行在 GitHub Actions 中。

## 触发链

Cloudflare Cron 使用 UTC 表达式；具体表达式配置在 `wrangler.jsonc`，应根据目标本地时区换算。触发后执行：

1. Worker 的 `scheduled()` handler 读取 Cloudflare secret `GITHUB_TOKEN`。
2. Worker 调用配置的 GitHub workflow dispatch endpoint。
3. 请求使用配置的分支 ref，并显式传入 `inputs.no_publish: "false"`。
4. GitHub Actions 的 `workflow_dispatch` 启动目标 workflow，继续执行 Python 采集、SQLite 状态库、HTML 报表和发布分支更新。
5. 统计槽位和窗口由仓库的 `monitor.config.json` 配置，包括 `schedule.time` 和 `schedule.window_hours`。

Cloudflare Cron 本身不依赖 `TRIGGER_SECRET`；它只用于下面的受保护 HTTP 手动入口。

## 需要的凭据

### GitHub `GITHUB_TOKEN`

创建 fine-grained personal access token，并设置：

- Repository access：Only select repositories → 仅选择目标 repository
- Repository permissions：Actions → Read and write
- 不需要额外的 repository 权限

GitHub 当前 REST 文档将 workflow dispatch 端点所需的 fine-grained PAT 权限列为 Actions `write`；GitHub 设置中的 `Read and write` 是对应的最小可用选项。不要使用 classic PAT，也不要扩大到所有仓库。

### Cloudflare 登录凭据

下面的交互式部署流程使用 `npx wrangler login`，会在浏览器中完成 Cloudflare 登录；不需要把 Cloudflare API token 写入仓库。Worker 的 GitHub PAT 和 HTTP 手动入口 secret 都通过 Wrangler secret 保存，不写入代码或 `wrangler.jsonc`。

## 安装、配置和部署

在本目录执行：

```bash
npm install
npx wrangler login
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put TRIGGER_SECRET
```

两条 `secret put` 命令会交互式读取 secret。`TRIGGER_SECRET` 应使用独立于 GitHub PAT 的高熵随机值；不要复用 GitHub token。

本地配置（可选）可以放在本目录的 `.dev.vars`，并确保它不会提交：

```dotenv
GITHUB_TOKEN=replace-with-a-test-or-real-token
TRIGGER_SECRET=replace-with-a-separate-random-secret
```

### 本地检查

以下命令只打包并校验 Worker，不部署，也不会调用 GitHub API：

```bash
node --check src/index.js
npm run check
```

启动本地 Worker 后，可先验证不触发 GitHub 的健康检查：

```bash
npm run dev
curl http://localhost:8787/health
```

Wrangler 会提供 Cloudflare 的本地 scheduled 测试入口：

```bash
curl "http://localhost:8787/cdn-cgi/local/scheduled?format=json"
```

这个请求会真实执行 `scheduled()` 并调用 GitHub API；只有在你确实想触发一次 GitHub Action、且本地 `GITHUB_TOKEN` 已准备好时才执行。没有真实 token 时，使用 `/health` 和 `npm run check` 即可完成安全的本地检查。

### 部署 Worker

```bash
npm run deploy
```

`wrangler.jsonc` 是 Cron 配置的唯一来源；部署后不要再单独在 Dashboard 配置另一套 Cron。新建、修改或删除 Cron Trigger 可能需要一段时间传播。

### 确认 Cron Trigger 已生效

```bash
npx wrangler deployments list
npx wrangler tail
```

也可以在 Cloudflare Dashboard 的 Workers & Pages → `<worker-name>` → Settings → Triggers → Cron Triggers 查看配置的表达式。到目标本地时间后，在 `wrangler tail` 或 Worker 日志中应看到 `Cloudflare Cron fired`，随后看到 GitHub dispatch accepted；再到 GitHub Actions 查看 workflow 运行。

### 手动验证 Worker 能触发 GitHub Action

部署完成并确认 `TRIGGER_SECRET` 已配置后，使用 Worker 的 workers.dev URL：

```bash
curl -X POST \
  -H "Authorization: Bearer <TRIGGER_SECRET>" \
  https://<worker>.<account>.workers.dev/trigger
```

只有 `POST /trigger` 且 Bearer secret 完全匹配时才会调用 GitHub；访问 URL、访问 `/health`、缺少 secret 或使用错误 secret 都不会触发。成功后在 GitHub Actions 页面或用 `gh run list --workflow <workflow-file> --limit 1` 确认运行。

## GitHub workflow 说明

目标 workflow 已删除 GitHub 自带的 `schedule`，只保留 `workflow_dispatch`。Worker 显式传 `no_publish=false`，而 workflow 的默认值也设为 `false`，确保 Cloudflare 触发的运行生成并发布报表；冒烟运行仍可显式传 `no_publish=true`。

GitHub workflow 保留 `--catch-up`。同一个槽位重复触发时：workflow 的 `concurrency` 会串行同组运行；`--catch-up` 发现已有完整槽位会跳过；SQLite 的 `repo_daily` 使用 `(date, repo)` 主键和 upsert，文件明细也会按同日仓库覆盖。因此重复 dispatch 不会新增重复汇总行，也不需要为本次迁移修改 Python 采集逻辑。

## 相关官方文档

- [Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)
- [Cloudflare Wrangler configuration](https://developers.cloudflare.com/workers/wrangler/configuration/)
- [Cloudflare Workers secrets](https://developers.cloudflare.com/workers/configuration/secrets/)
- [GitHub Create a workflow dispatch event](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
- [GitHub fine-grained PAT permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)
