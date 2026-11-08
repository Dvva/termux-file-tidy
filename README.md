# termux-file-tidy

配置驱动的文件分类整理工具，供 Termux（Android）每天定时运行。

用一个纯文本配置描述「把哪个目录里、名字符合什么条件的文件，移动到哪个目录，要不要改名」。

> 当前状态：**v2 语法已实现**，配置、解析器、执行器、检查器、单元测试齐全。

---

## 一、文件说明

| 文件 | 作用 |
|---|---|
| `tasker_file_tify.conf` | 配置，人工维护 |
| `tidy_core.py` | 解析器 + 匹配器 + 执行器 |
| `tasker_file_tidy.py` | 命令行入口 |
| `check_conf.py` | 语法检查器，复用 `tidy_core` |
| `tests/test_core.py` | 单元测试与端到端样本 |
| `README.md` | 本文件，v2 语法规范 |

---

## 二、缩进规则（严格）

缩进**只有三档**，且**只能用空格**：

| 缩进 | 用途 |
|---|---|
| 0 | 源目录 |
| 4 空格 | 目标目录、通配符、`match:`、`-`、`exclude:`、`action:` |
| 8 空格 | 动作的参数 |

约束：

- 每级必须正好 **4 个空格**，2 / 6 / 3 等等都是错误
- **禁止 Tab**，出现即报错
- **禁止跳级**，8 缩进必须出现在 `action:` 之后

---

## 三、规则块

一个块的结构：

```conf
<源目录>                    # 0 缩进，可多行
ignore: <路径>              # 0 缩进，全局忽略目录，可多行
    <目标目录>              # 4 缩进，裸路径 / 开头
    <通配符>                # 4 缩进，裸行 = 通配符匹配
    match: <正则>           # 4 缩进
    - <通配符>              # 4 缩进，通配符排除
    exclude: <正则>         # 4 缩进，正则排除
    action: <动作名>        # 4 缩进，开启动作段
        <参数>: <值>        # 8 缩进，属于上一个 action
    skip: true              # 4 缩进，整块跳过
```

最少只需要一个源目录，别的都可以省略。

### 块的边界

有两种情况会结束当前块：

1. **空行**
2. **顶格行**（0 缩进）—— 但只在当前块**已经有目标或动作**时才成立

第 2 条是为了容错：漏写空行时不会把两个块粘成一个。

```conf
# 前一块已经完整，这里自动开新块
/DCIM/Pictures/MagiaExedra
    /__我的文件/__游戏截图
/Pictures/Screenshots              ← 顶格，自动开新块
    /__我的文件/__游戏截图
```

但如果当前块**只有源、还没有目标/动作**，顶格行会被当成同一个块的另一个源：

```conf
/Download/Seal                     ← 源 1
/Download/BILIBILIAS               ← 源 2，同块
    /__我的文件/收藏视频
```

所以「多个源共用一套规则」依然成立。

### 3.1 源目录

- 以 `/` 开头，**顶格**书写
- **可以连续写多行**，多个源共用后面的规则（逻辑上是「或」）
- 支持通配符：`/Pictures/同人图*`、`/__我的文件/梗图*`
- 路径一律相对手机存储根 `/storage/emulated/0`

```conf
/Download/Seal
/Download/BILIBILIAS
```

### 3.2 目标目录

- 4 缩进，`/` 开头
- 一个块**最多一个**，写第二个会报错
- 目录不存在时自动创建
- 可以省略（纯重命名 / 清理的块不需要）

> 它是 `action: move` + `to:` 的简写。需要显式指定动作时，写成 `action: move` 加 8 缩进的 `to: /路径` 等价。

```conf
/DCIM/Screenshots
    /__我的文件/_归档文件/_待处理/代办事项
```

### 3.3 匹配条件：裸行（通配符）

**日常用这个，最省事。**

缩进的裸行就是一个通配符模式：

```conf
/DCIM/Screenshots
    /__我的文件/_归档文件/_待处理/代办事项
    com.microsoft.todos
```

- `*` 匹配任意多个字符，`?` 匹配一个字符
- **没写 `*` 时按「包含」处理**，等价于 `*com.microsoft.todos*`
- 大小写敏感，匹配对象是**文件名**（不含路径）
- 可以写多行，多条之间是「或」
- 一条都不写 = 全部文件

```conf
    *.jpg                 # 所有 jpg
    Screenshot_*          # 以 Screenshot_ 开头
    *com.tencent.mm*      # 含 com.tencent.mm
    IMG_2026??_*.png      # 中间两位任意
```

### 3.4 匹配条件：`match:`（正则）

需要捕获组、`^` `$` 锚点、`[0-9]{4}` 这类写法时才用它。

```conf
/DCIM/Camera
    /__我的文件/_归档文件/购买建议/购买建议-奶茶
    match: ^[0-9]{2}-.*(奶|茶)$
```

- 值是正则，对文件名做 `search`（包含）匹配
- 可写多行，多条之间是「或」
- 可以和裸行混用，条件之间仍然是与「或」关系

> 裸行和 `match:` 的区别只有一处：**裸行是通配符，`match:` 是正则**。通配符里 `(` `)` `[` `]` `.` 都是普通字符。

### 3.5 排除条件

两种写法，对应用「跳过」的文件：

```conf
    - *.jpg               # 通配符排除（推荐）
    - *.png
```

```conf
    exclude: \.bak$       # 正则排除
```

- `- ` 开头 = 通配符排除
- `exclude:` = 正则排除
- 命中即跳过，不移动也不改名

```conf
/Download
    /Download/new_dir
    [0-9a-z]{20,}
    - *.jpg
    - *.txt
```

### 3.6 跳过整块 `skip:`

临时不想让某个块生效，加一行 `skip: true`，不用把整段注释掉：

```conf
/DCIM/Screenshots
    /__我的文件/_归档文件/_待处理/代办事项
    skip: true
```

- 整块不执行，含块内所有动作和 `clean`
- 运行时在日志里记一条「跳过第 N 行规则块」
- 想恢复就把这行删掉或改成 `skip: false`

> 为什么不用 `pass`：裸行是通配符，`pass` 会被当成「文件名含 pass」的匹配条件。带冒号的 `skip:` 不会和通配符冲突。

### 3.7 全局忽略目录 `ignore:`

顶格书写，作用于**所有规则块**：这些目录既不扫描，也不参与 `clean` 清理。

```conf
ignore: /Pictures/.gs
ignore: /Download/.csj
ignore: /DCIM/*/thumbnails
```

- 可写多行，每行一个
- 支持通配符
- 路径同样相对 `/storage/emulated/0`
- 写的是目录，其下所有子目录和文件一并跳过

**默认已经跳过 `.` 开头的隐藏目录**，所以 `ignore:` 主要用来排除普通名字的目录，比如相机 RAW、手动分类的相册目录。

想要连隐藏目录一起处理，运行时加 `--include-hidden`。

---

## 四、动作段

`action:` 开启一个动作段，**它后面 8 缩进的参数都属于这个动作**，直到：

- 出现下一个 4 缩进的 `action:`
- 或块结束（空行）

一个块可以有多个动作段，**按出现顺序依次执行**。

```conf
/__我的文件/收藏视频
    action: rename
        replace: BV([0-9a-z]{4,})_
        with:
    action: command
        cmd: video-add-file {}
    action: append
        text: \1
        file: .video.link
```

上例：先删掉文件名里的 `BVxxxx_`，再对每个文件执行 `video-add-file`，再往 `.video.link` 追加内容。

> 没有任何 `action:` 的块，默认动作为 **move**（移动到目标目录）。

块级目标目录和 `action:` 可以并存：**目标是「去哪」，动作是「做什么」**。例如同时写目标目录和 `action: rename`，表示移动到该目录并改名。

---

## 五、动作清单

所有「改名」都走 `rename`，用参数区分具体怎么改。

| 动作 | 参数 | 说明 |
|---|---|---|
| `move` | `to:` | 移动文件（**默认**） |
| `copy` | `to:` | 复制文件，源保留 |
| `rename` | 见下表 | 重命名，所有改名操作统一入口 |
| `command` | `cmd:` | 执行 shell 命令，`{}` 代表当前文件路径 |
| `append` | `text:`、`file:` | 向 `file:` 追加 `text:` |
| `clean` | —— | 收尾时清空该源目录下的空文件夹 |

### rename 的参数

| 参数 | 说明 |
|---|---|
| `to:` | 完整的**目标文件名模板**，与下面所有参数互斥 |
| `replace:` + `with:` | 正则替换，`with:` 为空表示删除匹配部分 |
| `prefix:` | 文件名前加内容 |
| `suffix:` | 扩展名前加内容 |
| `ext:` | 修改扩展名 |
| `case:` | `lower` / `upper` / `title` |
| `sanitize:` | `true`，去掉文件名里的非法字符 |
| `sequence:` | 编号格式，如 `{index:03d}` |

**执行顺序固定**，与书写顺序无关：

```
sanitize → replace → case → prefix → suffix → ext → sequence
```

写了 `to:` 就不用其他参数（同时写会报错），因为 `to:` 已经直接给出最终结果。

```conf
# 加前缀
action: rename
    prefix: __

# 去掉 BV 号前缀
action: rename
    replace: BV([0-9a-z]{4,})_
    with:

# 按时间整体重命名
action: rename
    to: {mtime:%Y%m%d_%H%M%S}.{ext}
```

### 通用参数

所有动作都支持，含义随动作变化：

| 参数 | 用于 | 说明 |
|---|---|---|
| `match:` | 全部 | 在该动作内部再筛一次；可多行，也可省略 |
| `to:` | move / copy | 目标目录 |
| `to:` | rename | 目标文件名模板 |
| `text:` | append | 要追加的内容 |
| `file:` | append | 追加到哪个文件 |
| `cmd:` | command | 要执行的命令，`{}` = 文件路径 |

---

## 六、模板变量

写在 `to:` / `text:` / `file:` / `cmd:` 里，格式 `{变量}` 或 `{变量:参数}`。

### 文件名相关

| 变量 | 说明 | 示例 |
|---|---|---|
| `{name}` | 主文件名（不含扩展名） | `Screenshot_20260101` |
| `{ext}` | 扩展名（不含点） | `jpg` |
| `{full}` | 完整文件名 | `Screenshot_20260101.jpg` |
| `{dir}` | 所在目录名 | `Screenshots` |

### 时间相关（参数是 strftime 格式串）

| 变量 | 说明 |
|---|---|
| `{mtime:%Y%m%d}` | 文件修改时间 |
| `{ctime:%Y%m%d}` | 文件状态变更时间 |
| `{atime:%Y%m%d}` | 文件访问时间 |
| `{now:%Y%m%d}` | 脚本执行时刻 |
| `{date:%Y%m%d}` | 同 `{mtime}`，语义化的别名 |

不写参数时用默认格式 `%Y%m%d`。

> Android 上 `ctime` 是 inode 变更时间，**不等于**创建时间。要「按产生时间排序」应该用 `mtime`。

### 序号与统计

| 变量 | 说明 |
|---|---|
| `{index}` / `{index:03d}` | 当前批次序号，从 1 开始 |
| `{count}` | 本批处理的文件总数 |
| `{hash}` / `{hash:8}` | 文件内容的短哈希，用于防重名 |
| `{rand}` / `{rand:3}` | 随机数字，参数是位数（默认 3） |

### 组合示例

```conf
action: rename
    to: {mtime:%Y%m%d_%H%M%S}_{name}.{ext}
```

`Screenshot_20260101.jpg` → `20260101_120000_Screenshot_20260101.jpg`

---

## 七、完整示例

```conf
# 归并待 todo 图片
/DCIM/Screenshots
    /__我的文件/_归档文件/_待处理/代办事项
    com.microsoft.todos

# 待消费
/DCIM/Screenshots
    /__我的文件/_归档文件/_待处理/待消费
    net.hpoi
    com.taobao.taobao
    com.xunmeng.pinduoduo

# 收藏视频：移动
/Download/Seal
/Download/BILIBILIAS
    /__我的文件/收藏视频

# 收藏视频：重命名 + 登记
/__我的文件/收藏视频
    match: BV([0-9a-z]{4,})_
    action: rename
        replace: BV([0-9a-z]{4,})_
        with:
    action: command
        cmd: video-add-file {}

# AI 截图按时间重命名
/Pictures/Screenshots
    /Pictures/Deepseek
    com.deepseek.chat
    com.openai.chatgpt
    action: rename
        to: {mtime:%Y%m%d_%H%M%S}.{ext}

# 收尾：清理空目录
/DCIM
    action: clean

/Download
    action: clean
```

---

## 八、语法检查器

> ⚠️ 现有 `check_conf.py` 是 v1 版，只认旧语法。适配 v2 的工作还没做，在那之前请手动对照本文档检查。

适配后计划检查：

| 类别 | 内容 |
|---|---|
| 缩进 | 只允许 0 / 4 / 8 空格；禁止 Tab；禁止跳级 |
| 结构 | 源目录必须顶格且 `/` 开头；块边界（空行或顶格）；目标最多一个；8 缩进必须跟在 `action:` 后 |
| 跳过 | `skip:` 的取值只能是 `true` / `false` |
| 动作 | 动作名是否认识；必需参数是否缺失；参数名是否属于该动作 |
| 通配符 | 裸行是否形似已知指令（如 `matchs:`，疑似拼写错误） |
| 正则 | `match:` / `exclude:` / `replace:` 能否编译 |
| 变量 | 是否在已知变量表里；时间变量参数是否合法 |
| 路径 | 必须 `/` 开头；不能含反斜杠；不能有重复斜杠 |

参数：

| 参数 | 说明 |
|---|---|
| `--quiet` / `-q` | 不显示提示级 |
| `--strict` | 提示也算失败 |

退出码：`0` 无错误，`1` 有错误，`2` 文件不存在。

---

## 九、运行与调试

```bash
python tasker_file_tidy.py --dry-run      # 预览，不动文件
python tasker_file_tidy.py                # 真正执行
python tasker_file_tidy.py --only 3       # 只跑第 3 个规则块
python tasker_file_tidy.py --undo         # 撤销上一次运行
python tasker_file_tidy.py --verbose      # 输出详细日志
```

| 参数 | 说明 |
|---|---|
| `--config` / `-c` | 配置文件路径，默认同目录下的 `tasker_file_tify.conf` |
| `--dry-run` / `-n` | 只打印「源 → 目标」，不落盘 |
| `--undo` | 按 `undo.log` 反向恢复 |
| `--only N` | 只跑第 N 个规则块 |
| `--root` | 覆盖存储根 `/storage/emulated/0`（测试用） |
| `--verbose` / `-v` | 输出详细日志 |
| `--no-notify` | 不推送 Termux 通知 |
| `--include-hidden` | 也处理 `.` 开头的隐藏目录（默认跳过） |

### 状态文件

| 内容 | 位置 |
|---|---|
| 日志 | `~/.local/state/file_tidy/tidy_YYYYMMDD.log` |
| 撤销记录 | `~/.local/state/file_tidy/undo.log` |

可用环境变量 `FILE_TIDY_STATE` 指定其它目录。

### 匹配优先级

一个文件可能同时落在多个块的源目录里（例如 `/Pictures` 和 `/Pictures/Screenshots`）。规则是：

1. **源目录越深越优先**
2. 同深度按配置文件行号

所以 `/Pictures/Screenshots` 的规则一定先于 `/Pictures` 的规则命中，不会被笼统规则抢走。

### 安全约定

- 目标同名时自动加 ` (1)`，绝不覆盖
- 单文件失败记日志后继续，不中断整批
- `--dry-run` 与实际执行走同一套代码，只在最后一步分叉
- 每次移动都写 undo 记录，`--undo` 可以反悔

---

## 十、从 v1 迁移

旧配置用的是 v1 语法，主要差异：

| v1 写法 | v2 写法 |
|---|---|
| 裸正则行当条件 | 裸行改通配符；需要正则时写 `match:` |
| `- 正则` / `-: 正则` | `- 通配符` 或 `exclude: 正则` |
| `target:` 作重命名模板 | `action: rename` + 8 缩进的 `to:` |
| `.Prefix:` | `action: rename` + `prefix:` |
| `.replace:` | `action: rename` + `replace:` / `with:` |
| `reg:` | 放回 `action:` 段下的 `match:` |
| `write_append:` | `action: append` |
| `delete-empty: true` | `action: clean` |

迁移后，检查器遇到 v1 特征会提示「这是旧语法」，而不是逐行报错。

---

## 十一、Termux 定时运行

```bash
pkg install python termux-api
chmod +x ~/termux-file-tidy/tasker_file_tify.py
termux-job-scheduler \
  --script ~/termux-file-tidy/tasker_file_tify.py \
  --period-ms 86400000 \
  --persisted true \
  --battery-not-low false
```

`--persisted true` 保证重启后计划仍生效。
