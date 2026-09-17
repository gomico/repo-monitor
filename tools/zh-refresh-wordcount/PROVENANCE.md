# 本目录来源（provenance）

这里是 `zh-refresh-wordcount`（中文字数统计工具）的**只读副本**，供本仓库的 GitHub Actions 使用。
请勿在本目录直接改工具逻辑；要升级就重新从源仓库复制，并更新下面这段记录。

| 项 | 值 |
|---|---|
| 源目录 | `/mnt/d/repos/local/zh-refresh-wordcount`（Windows: `D:\repos\local\zh-refresh-wordcount`） |
| 源仓库 commit | `5b172139cc70cd56e05f46949e501eed2b9ed3a8`（2026-08-28 15:45:44 +08:00）《统计 ref 对比中的图片文件字数》 |
| 复制时间 | 2026-09-16（本仓库 2026-09-17 建立时同步） |
| 本目录保留 | `count_zh_refresh.py` `main.py` `counter.py` `git_utils.py` `models.py` `output.py` `absent_en.py` |
| 本目录未保留 | `README.md` `USAGE.md`（工具自带文档，本仓库不再需要）；`build/` `dist/` `*.spec` `test_*.py` `tools/`（源仓库保留） |

**依赖关系**（删文件前必看）：`count_zh_refresh.py` → `main.py` → `absent_en.py`，
且 `counter.py` / `git_utils.py` / `models.py` / `output.py` 互相引用。
7 个 `.py` **全部必需**，纯标准库、无第三方依赖。

两条路线（本仓库只用后者）：

```bash
# 本地路线：必须在 git 工作树内运行，摘要 CSV 落在 CWD
python3 count_zh_refresh.py --base-compare-ref <BASE> --target-compare-ref <TIP>

# 网络路线（本仓库使用）：任意 CWD，bare blobless 缓存于 <config-root>/git-cache/<repo>-<hash>.git
python3 count_zh_refresh.py --git-url <URL> \
  --base-compare-ref <BASE> --target-compare-ref <TIP> --config-root <DIR>
```

已知口径与坑：

- 改计删不计；新增/修改的常见图片文件每个折算 200 字（`models.py` 的 `IMAGE_FILE_CHAR_COUNT`）
- 必须**同时**给 `--base-compare-ref` 与 `--target-compare-ref` 才是非交互模式，否则进 TUI（非 TTY 下会卡住）
- 摘要 CSV 落在**执行命令的 CWD**（网络路线也一样）
- 网络路线每次调用都会清理自己 `config-root` 下的 `.zh-refresh-cache-*` 临时目录，并按硬编码的
  `NETWORK_CACHE_LIMIT = 50 MiB` 做 LRU 淘汰 → **多个并发进程共用同一 `--config-root` 会互删对方正在克隆的目录**
  （实测：并发 4 时 4 个仓里 2 个报 `could not lock config file …: No such file or directory`），
  所以本仓库的 `config/ci.overrides.json` 把 `runtime.concurrency` 设为 **1**
