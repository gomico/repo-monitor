# 使用说明

这份说明面向普通使用者，不需要了解脚本内部实现。常用功能见[交互式界面怎么操作](#交互式界面怎么操作)和[结果会显示什么](#结果会显示什么)。

## 这是做什么的

这个工具用来统计两个 Git 版本之间，中文文档到底改了多少“句子”和图片引用；对比模式还会把新增或修改的图片文件按每个 200 字计入。

它适合用来回答这类问题：

- 某次文档更新大概改了多少内容
- 两个版本之间，中文说明文档的刷新工作量有多大
- 某个文档目录这次更新了多少句子

统计结果里，新增或修改的句子会计入，删除的句子不计入；删除或单纯重命名的图片文件也不会增加字数。

## 使用前准备

你需要：

- 安装了 `git`
- 安装了 Python 3.10 或更高版本
- 本地路线：进入你要分析的 Git 仓库目录
- 网络路线：可以在任意目录运行，随后在 TUI 输入 Git URL，或使用 `--git-url URL`

如果想要更方便地选择版本，可以额外安装 `prompt_toolkit`：

```bash
python3 -m pip install prompt_toolkit
```

## 怎么运行

比较 Git 版本或使用 `--all` 时，进入目标仓库内的任意目录，运行脚本：

```bash
python3 /mnt/d/repos/local/zh-refresh-wordcount/count_zh_refresh.py
```

也可以使用仓库提供的 shell 包装命令：

```bash
/mnt/d/repos/local/zh-refresh-wordcount/zh-refresh-wordcount.sh
```

它会把所有参数原样传给 Python 脚本。安装到 `~/.local/bin` 后，可直接使用 `zh-refresh-wordcount`。

如果当前目录不是 Git 仓库，直接运行会先打开 Git URL 输入 TUI。也可以显式指定远端地址：

```bash
python3 /mnt/d/repos/local/zh-refresh-wordcount/count_zh_refresh.py \
  --git-url https://example.com/team/repo.git
```

已知两个版本时可完全使用非交互 CLI：

```bash
python3 /mnt/d/repos/local/zh-refresh-wordcount/count_zh_refresh.py \
  --git-url https://example.com/team/repo.git \
  --base-compare-ref BASE_COMPARE_REF \
  --target-compare-ref TARGET_COMPARE_REF
```

网络路线在 Windows 和 Linux 上都使用配置目录中的持久 bare partial clone 缓存，仅按需读取 Git 对象，不 checkout 工作树。统计前会批量预取本次比较所需的 Markdown blob；图片文件只按路径和变更状态计数，不读取图片内容。分析页会显示最近多行下载进度、等待时间和已接收数据量。所有网络仓库缓存合计达到 50 MiB 时按最近最少使用顺序清理；远端更新失败时会提示并允许继续使用已有缓存。网络路线使用远端默认分支。`--git-url` 不能与依赖本地工作树的 `--all` 或 `--file` 组合使用。

如果只想统计当前仓库所有中文 Markdown 文档的字数和图片数：

```bash
python3 /mnt/d/repos/local/zh-refresh-wordcount/count_zh_refresh.py --all
```

`--all` 不读取 ref，也不执行 `git fetch` 或 `git pull`，只统计当前工作树中 Git 已跟踪的 Markdown 文件。

CLI 默认将 `--all`、`--file` 和比较结果输出为 CSV；`--csv` 可显式指定 CSV，`--txt` 切换为文本格式。交互模式默认开启 CSV，在“主菜单”页面按 `Space` 可切换格式。

如果只想统计指定文件或目录的中文字数和图片数，使用 `--file`；这个选项可以重复使用：

```bash
python3 /mnt/d/repos/local/zh-refresh-wordcount/count_zh_refresh.py \
  --file docs \
  --file docs/faq.md
```

`--file` 按当前工作树读取文件，可以指定仓库外的文件或目录；相对路径按启动命令的目录解析。传入目录时会递归统计其中的 `.md` 和 `.markdown` 文件，并跳过英文目录和英文文件名后缀。它不能和 `--all`、`--base-compare-ref`、`--target-compare-ref` 组合使用。

如果某个指定文件不存在，结果会标注“不存在”，TOTAL 不会计入该文件，并在 TOTAL 下列出缺失路径；其他文件仍会继续统计。

单独使用 `--file` 时不要求当前目录是 Git 仓库。

如果你使用的是打包好的 Windows exe，请看 [EXE_USAGE.md](EXE_USAGE.md)。

本地仓库运行后会先显示当前分支，获取远端最新数据，并询问是否执行 `git pull --ff-only` 更新当前分支。默认选择 yes，直接按 Enter 即执行；输入 `n` 才会跳过。网络路线会显示远端默认分支并直接进入主菜单：

- `1`：当前分支 ref 比较（默认）
- `2`：列出各分支 ref
- `3`：手动输入 old ref 和 new ref
- `4`：全量统计仓库内中文字数（适用于全新仓库）
- `5`：英文文档缺失统计
- `6`：切换分支

网络路线没有工作树，因此只显示选项 `1` 到 `4`，其中选项 `4` 为英文文档缺失统计，选项 `1` 的文字为“默认分支 ref 比较（默认）”。

安装 `prompt_toolkit` 时，主菜单支持数字键、键盘上下、鼠标点击；默认开启 CSV，按 `Space` 可切换为文本，右下角显示输出状态；按 Enter 确认。未安装时使用数字输入。

选择 `6` 会列出本地分支，以及尚无同名本地分支的 `origin` 远端分支。选择远端分支时会创建同名本地跟踪分支；选择本地分支后会询问是否执行 `git pull --ff-only`。未跟踪文件不会阻止切换或 pull；已跟踪文件或暂存区有未提交改动时，程序会拒绝操作。若未跟踪文件与目标分支或远端更新中的文件同路径，Git 仍会拒绝覆盖该文件。

选择 `3` 会进入手动 ref 输入 TUI：在 `old ref` 和 `new ref` 输入框中填写分支名、tag 或 commit hash，按 `↑` / `↓`、`Tab` / `Shift-Tab` 或鼠标点击切换输入框，按 Enter 校验并确认，按 `Esc` / `Ctrl-C` 返回主菜单。从分支 ref 列表缓存的 old/new hash 会自动预填。无效 ref 和相同 commit 会直接在当前页面提示，不会退出 TUI。未安装 `prompt_toolkit` 时使用普通 CLI 输入。

选择比较模式后，再选择两个 Git 版本：

- `old ref`：旧版本
- `new ref`：新版本

如果你安装了 `prompt_toolkit`，会看到一个全屏选择界面。没有安装时，也可以用数字方式选择。

“列出各分支 ref”会按最后提交时间从新到旧列出全部 `origin` 分支。选择分支后会列出该远端分支的全部 commits；选中 ref 并按 Enter 会复制完整 hash，然后可选择缓存为 old ref、new ref，或不填入并返回主菜单。缓存状态会显示在主菜单，并预填到手动 ref 输入 TUI；缓存只在本次程序运行期间有效。这个过程只读取 Git refs，不会 checkout 或改变当前分支。

## 交互式界面怎么操作

界面分成左右两列：

- 左边是旧版本 `old ref`
- 右边是新版本 `new ref`

每一列里都列出了当前分支最近的 Git 提交，你只要把左右两边分别选到你要比较的版本即可。

常用按键：

- `Tab`：在左右两列之间切换
- `←` / `→`：切换到左列或右列
- `A` / `D`：也可以切换左右列
- `↑` / `↓`：在当前列里往上或往下选一条
- `W` / `S`：也可以上下移动
- `PageUp` / `PageDown`：一次移动半页
- 鼠标点击：直接选择某一列中的某个提交
- 鼠标滚轮：在对应列中向上或向下移动一条
- `Enter`：确认选择并开始统计
- `q` / `Esc` / `Ctrl-C`：返回主菜单

界面里会高亮当前选中的提交。窗口大小变化时，列表会自动调整显示范围。

在主菜单按 `q` / `Esc` / `Ctrl-C` 才会退出程序。

如果你不确定选哪个，通常可以把 `old ref` 选成修改前的版本，把 `new ref` 选成修改后的版本。

## 也可以直接指定版本

如果你已经知道要比较哪两个版本，可以直接写出来，不必进入交互界面：

```bash
python3 /mnt/d/repos/local/zh-refresh-wordcount/count_zh_refresh.py \
  --base-compare-ref BASE_COMPARE_REF \
  --target-compare-ref TARGET_COMPARE_REF
```

这里的 `BASE_COMPARE_REF` 和 `TARGET_COMPARE_REF` 可以是：

- 分支名
- tag
- commit SHA

例如：

```bash
python3 /mnt/d/repos/local/zh-refresh-wordcount/count_zh_refresh.py \
  --base-compare-ref 7e8c5fe0 \
  --target-compare-ref e497ab4a
```

## 结果会显示什么

脚本运行完后，会输出：

- 本次计入工作量的每一句，方便你检查为什么这些句子被统计
- 每个文件改了多少句
- 总共改了多少句
- 每个文件和总计新增了多少图片；使用 `--all` 或 `--file` 时显示当前图片总数
- 对比模式中，新增或修改的常见图片文件每个按 200 字计入新增字数
- 只有删除内容时，会显示删除了多少行，新增字数仍为 0
- CSV 和 JSON 归档文件保存到了哪里
- 分隔线下方的当前总结，以及工作量总结文件保存到了哪里

CSV 和 JSON 归档结果会保存在默认目录：

```text
~/.config/zh-refresh-wordcount
```

对比模式和 TUI 中选择全量统计时，脚本还会在你运行命令时所在的目录生成一个工作量总结文件。对比模式的文件名格式为：

```text
<repo>_compare_<old timestamp>-<new timestamp>.csv
```

全量统计的文件名格式为：

```text
<repo>_full_<last commit timestamp>.csv
```

其中时间戳格式为 `YYYYMMDDHHMMSS`，时间戳内部不包含连字符。

默认扩展名为 `.csv`；使用 `--txt` 或在 TUI 中关闭 CSV 时，扩展名为 `.txt`。

这个文件只包含 CLI 中`当前总结`分隔线及之后的总结内容，不包含前面的句子调试明细，也不包含 CSV/JSON 路径。

你下次再用这个工具时，程序也会记住一些上次使用过的版本，方便再次选择。

## 句子是怎么切分的

工具不是按行或按字数来统计，而是先识别出文档里的每一句中文：

- 只有 `。`、`！`、`？`、`……` 会触发分句；英文句号、逗号、分号、冒号不会触发分句。
- **正文段落**：连续多行合成一段，按句号、感叹号、问号、省略号（`。` `！` `？` `……`）切分为句子。
- **标题**：每行标题单独作为一句（没有中文则不纳入）。
- **列表项**：每项单独切分。
- **表格**：按单元格处理——如果有句号等终止符就按普通句子切，没有的话整个单元格算一句。
- **代码块**：fenced code block 和 AsciiDoc block 里，含中文的代码行整行作为一句参与统计；不含中文的代码行不计入。

段落末尾如果最后一个终止符后面还有文字，只有包含中文或其他统计字符时才会计入。

## 小提示

- 本地路线在 Git 仓库里运行；非 Git 目录可通过 TUI 输入 Git URL 或使用 `--git-url URL`
- 如果两个版本选成了同一个提交，程序会报错
- 比较模式只统计当前分支；“列出各分支 ref”可直接浏览 `origin` 分支历史，且不会 checkout 分支
- `git pull --ff-only` 确认默认选择 yes；已跟踪文件或暂存区有未提交改动时，即使确认 pull 也会跳过，未跟踪文件不会触发该检查
- 如果当前分支没有上游分支，选择 pull 时 Git 会报“no tracking information”；先用 Git 设置 upstream，或手动执行 `git pull <remote> <branch>`
- 如果你只想快速看大概工作量，直接用默认交互模式就行
- 如果你已经知道版本号，直接指定 `--base-compare-ref` 和 `--target-compare-ref` 更快

## 英文文档缺失统计

主菜单追加“英文文档缺失统计”。目标版本默认使用本地当前分支 HEAD，远端交互模式默认选择目标分支；也可以使用 `--target-absent-ref` 直接指定分支、tag 或 commit。使用 `--base-absent-ref` 可指定可选基线分支、tag 或 commit；不指定基线时只检查目标版本是否缺少英文文档。`/zh/` 路径对应 `/en/` 路径，`filename.zh.md` 对应同路径的 `filename.md`，其他同目录文档对应 `-en` 或 `_en` 后缀。

也可以直接使用：

```bash
python3 count_zh_refresh.py --absent-en
python3 count_zh_refresh.py --absent-en --base-absent-ref origin/main
python3 count_zh_refresh.py --git-url URL --absent-en --target-absent-ref main --base-absent-ref origin/release
python3 count_zh_refresh.py --absent-en --target-absent-ref e497ab4a --base-absent-ref 7e8c5fe0
```

输出文件名默认为 `<repo>_absent_en_YYYYMMDDHHMMSS.csv`；加 `--txt` 输出 TXT，`--csv` 可显式指定 CSV。基线模式下，英文存在但中文变化的文档标记为 `[基线已有英文，中文有变化]`，中文未变化的文档标记为 `[基线已有英文，中文无变化]`；其余标记为 `[缺失]`。
