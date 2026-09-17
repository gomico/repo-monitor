# 中文文档刷新工作量统计

这是一个 Python CLI，用于统计两个 Git 版本之间中文文档的刷新工作量，也支持使用 `--all` 统计当前仓库所有中文 Markdown 文档的字数和图片数，或使用可重复的 `--file` 统计指定文件。刷新工作量统计口径是句子级 diff：新增或修改的句子按新版本整句计数，删除句子不计数；对比模式中新增或修改的图片文件每个按 200 字折算。

在 Git 仓库内运行时，程序沿用本地仓库流程并基于当前 checked-out 分支读取历史。在非 Git 目录启动交互模式时，程序会先显示 Git URL 输入 TUI，然后通过配置目录中的持久裸仓库缓存读取远端；也可在任意目录使用 `--git-url URL`。`--all` 仍只适用于本地仓库当前工作树，单独使用 `--file` 时可以在任意目录运行。

网络路线示例：

```bash
python3 count_zh_refresh.py --git-url https://example.com/team/repo.git
python3 count_zh_refresh.py --git-url https://example.com/team/repo.git \
  --base-compare-ref BASE_COMPARE_REF --target-compare-ref TARGET_COMPARE_REF
```

网络路线在 Windows 和 Linux 上都使用配置目录中的持久 bare partial clone 缓存，不 checkout 工作树。程序会批量预取本次比较所需的 Markdown Git 对象，图片文件只按路径和变更状态计数，不读取图片内容；交互模式会显示最近多行下载进度、等待时间和已接收数据量。所有网络仓库缓存合计达到 50 MiB 时按最近最少使用顺序清理；远端更新失败时会明确提示并允许继续使用已有缓存。`--git-url` 不能与 `--all` 或 `--file` 组合使用。

## 文件

- `count_zh_refresh.py`：统计脚本
- `zh-refresh-wordcount.sh`：命令行包装脚本
- `USAGE.md`：面向普通使用者的操作说明
- `EXE_USAGE.md`：面向直接使用打包 exe 的操作说明
- `AGENTS.md`：Codex 项目说明
- `zh-refresh-wordcount-plan.md`：原始设计方案

## 运行前提

- 需要 `git`
- 需要 Python 3.10+
- 可选：安装 `prompt_toolkit` 以获得全屏双列 TUI

安装可选依赖：

```bash
python3 -m pip install prompt_toolkit
```

## 使用说明

使用 Python 脚本运行请看 [USAGE.md](USAGE.md)。

也可以直接运行包装命令：

```bash
./zh-refresh-wordcount.sh --help
```

直接使用打包好的 Windows exe 请看 [EXE_USAGE.md](EXE_USAGE.md)。

## 输出

脚本运行结束后，CLI 默认输出 CSV；也会输出 CSV/JSON 归档路径。比较模式随后用分隔线引出“当前总结”和总计。使用 `--txt` 可切换为文本格式，`--csv` 保留为显式指定 CSV 的兼容选项。交互模式默认开启 CSV，主菜单按 `Space` 可切换格式。

对比模式和 TUI 中选择全量统计时，脚本还会把统计总结写入运行时工作目录。对比模式的文件名格式为：

```text
<repo>_compare_<old timestamp>-<new timestamp>.csv
```

全量统计的文件名格式为：

```text
<repo>_full_<last commit timestamp>.csv
```

其中时间戳格式为 `YYYYMMDDHHMMSS`，时间戳内部不包含连字符。

默认扩展名为 `.csv`；使用 `--txt` 或在 TUI 中关闭 CSV 时，扩展名为 `.txt`。

CLI 的“当前总结”区域会显示该总结文件的保存路径。

默认配置目录是：

```text
~/.config/zh-refresh-wordcount
```

每个仓库会生成对应的 CSV 和 JSON 归档文件，并记录上次使用的 ref，方便下次交互式选择时预填默认值。CSV 和 JSON 会保存在配置目录的仓库子目录中；时间戳总结文件会保存在运行脚本时所在的工作目录中。

本地仓库交互模式会先显示当前分支，执行 `git fetch` 获取远端最新数据，并询问是否执行 `git pull --ff-only`；默认选择 yes，直接按 Enter 即执行。非 Git 目录交互模式先要求输入 Git URL，网络路线显示远端默认分支，选项 `1` 显示为“默认分支 ref 比较（默认）”；网络路线不执行 pull，也不提供依赖工作树的全量统计选项。当前/默认分支比较界面的左右两列只列出该分支可达的 commits；“列出各分支 ref”会按最后提交时间从新到旧列出全部 `origin` 分支，并读取所选远端分支的完整提交历史，全程不会 checkout 分支。

在本地仓库的“主菜单”页面可选择：`1` 当前分支 ref 比较（默认）、`2` 列出各分支 ref、`3` 手动输入 old ref 和 new ref、`4` 全量统计仓库内中文字数（适用于全新仓库）、`5` 英文文档缺失统计、`6` 切换分支。切换分支时会显示本地分支及尚无对应本地分支的 `origin` 远端分支；选择远端分支会创建同名本地跟踪分支，选择本地分支后会询问是否执行 `git pull --ff-only`。未跟踪的 exe、txt、csv 等文件不会阻止切换或 pull，但已跟踪文件或暂存区有改动时会阻止操作。选择 `2` 后，选中分支和 ref 并按 Enter 会复制完整 ref hash；随后可将 hash 缓存为 old ref、new ref，或不填入并返回主菜单。缓存状态会显示在主菜单，并自动预填到选项 `3` 的双输入框 TUI 中；缓存只在本次程序运行期间有效。手动输入页面可按上下键、Tab、Shift-Tab 或鼠标点击切换输入框，按 Enter 校验并确认，输入错误会留在当前页面提示。按 `Space` 可切换 CSV 输出；默认关闭，右下角会显示“CSV输出已开启”或“CSV输出已关闭”。

## 统计口径

- 以句子为 diff 单位，`insert` 和 `replace` 计入新版本句子。
- `equal` 不计入。
- `delete` 不计入。
- 标题、段落、列表、Markdown 表格内容参与统计。
- fenced code block 和 AsciiDoc block 中含中文的代码行按整行参与统计；inline code 中的中文参与统计。
- Markdown 图片 alt text 和图片路径中的中文参与统计。
- 图片数量按完整的 Markdown 图片语法和 HTML `<img>` 标签统计；支持空 alt、可选 caption，例如 `![alt](path)`、`![](path "caption")`。同一图片被引用多次按多次计算。
- 对比模式显示新增图片数；`--all` 和 `--file` 显示当前文档图片总数。代码块中的图片示例不计入。
- 对比模式还统计常见图片文件（如 `.png`、`.jpg`、`.jpeg`、`.gif`、`.svg`、`.webp`）的新增或修改：每个图片文件折算为 200 字；删除或单纯重命名不增加字数。
- HTML 锚点和 HTML 标签内容中的中文参与统计。
- 普通 URL 不参与统计。
- 支持 Git rename 追踪：重命名文件按旧路径读取旧版本、按新路径读取新版本，避免整文件误计为新增。
- TUI 支持鼠标点击、鼠标滚轮、左右键、WASD、PageUp/PageDown；比较列表只显示当前分支的 commits，分支 ref 浏览列表直接读取 `origin` 远端 refs，列表高度和 commit message 宽度会随终端窗口变化。
- CLI 输出在 TTY 下会启用颜色，重定向时自动退回纯文本。

## 分句逻辑

文档先经过归一化处理：去掉 Markdown 标记（标题标记、列表标记、引用标记、转义符等）、链接文本保留、链接和图片路径参与统计、URL 排除、inline code 不特殊处理。fenced code block 和 AsciiDoc block 内含中文的行按整行参与统计。

当前只有 `。`、`！`、`？`、`……` 会触发分句；英文句号、逗号、分号、冒号不会触发分句。

分句过程如下：

1. **正文段落**：连续的非特殊行（不是标题、列表、表格、空行、代码块）合并为一段，用空格拼接后按句子终止符（`。` `！` `？` `……`）切分。
2. **标题**：每行标题单独成句（不含计数则不纳入）。
3. **列表项**：每项单独使用终止符分句。
4. **表格**：按单元格解析。不含终止符的单元格整体为一句；含终止符的按普通规则分句；分隔行跳过。
5. **段落末尾残余文本**（最后一个终止符之后的内容）：仅当包含统计字符时才成句，否则丢弃。
6. **代码块**：含统计字符的代码行整行作为一句，不按句子终止符继续切分；不含统计字符的代码行不计入。
7. **空行、标题、列表、表格、代码块**都会触发当前段落结束并 flush，确保段落之间不会串句。

## 表格规则

Markdown 表格按单元格解析：

- 不含句子终止符的单元格整体计为一句。
- 含句子终止符的单元格按普通句子分句。
- 表格分隔行不计入统计。
- 支持转义竖线 `\|`。

## 计数字符范围

当前统计范围包括：

- 简体和繁体常用 CJK 字符
- CJK 扩展汉字
- CJK 部首、笔画、兼容字符和符号
- CJK 标点
- emoji 和部分符号字符

## 帮助

```bash
python3 /mnt/d/repos/local/zh-refresh-wordcount/count_zh_refresh.py -h
```

## 英文文档缺失统计

交互主菜单中的“英文文档缺失统计”会检查目标分支的 Markdown 中文文档是否有对应英文文档。路径含 `/zh/` 时映射到 `/en/`；同一路径下的 `filename.zh.md`（或 `.zh.markdown`）对应 `filename.md`；其他路径检查同目录下的 `-en` 或 `_en` 后缀。

本地仓库默认使用当前分支 HEAD；也可以通过 `--target-absent-ref` 指定目标分支、tag 或 commit。可选的 `--base-absent-ref` 用于指定基线版本，区分 `[缺失]`、`[基线已有英文，中文有变化]` 和 `[基线已有英文，中文无变化]`。结果文件名为 `<repo>_absent_en_YYYYMMDDHHMMSS.txt`，使用 `--csv` 时扩展名为 `.csv`。

命令行示例：

```bash
python3 count_zh_refresh.py --absent-en
python3 count_zh_refresh.py --absent-en --base-absent-ref origin/main
python3 count_zh_refresh.py --git-url URL --absent-en --target-absent-ref main --base-absent-ref origin/release
python3 count_zh_refresh.py --absent-en --target-absent-ref e497ab4a --base-absent-ref 7e8c5fe0
```
