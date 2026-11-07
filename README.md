# termux-file-tidy

配置驱动的文件分类整理工具，供 Termux（Android）每天定时运行。

用一个纯文本配置描述「把哪个目录里、名字符合什么条件的文件，移动到哪个目录，要不要改名」。

> 当前状态：**语法 v2 已定稿，主程序与检查器尚未适配**。仓库里目前是本文档，加上 v1 版的配置与检查器。

---

## 一、文件说明

| 文件 | 作用 |
|---|---|
| `tasker_file_tify.conf` | 正式配置（**当前仍是 v1 写法，待迁移到 v2**） |
| `tasker_file_tify.py` | 解释器（待实现） |
| `check_conf.py` | 语法检查器（**当前仍是 v1 版，v2 适配待做**） |
| `README.md` | 本文件，v2 语法规范 |

---

## 二、缩进规则（严格）

缩进**只有三档**，且**只能用空格**：

| 缩进 | 用途 |
|---|---|
| 0 | 源目录 |
| 4 空格 | 目标目录、`match:`、`exclude:`、`action:` |
| 8 空格 | 动作的参数 |

约束：

- 每级必须正好 **4 个空格**，2 / 6 / 3 等等都是错误
- **禁止 Tab**，出现即报错
- **禁止跳级**，8 缩进必须出现在 `action:` 之后

---

## 三、规则块

空行分隔一个块。一个块的结构：

```conf
<源目录>                    # 0 缩进，可多行
    <目标目录>              # 4 缩进，裸路径 / 开头
    match: <正则>           # 4 缩进，可多行
    exclude: <正则>         # 4 缩进，可多行
    action: <动作名>        # 4 缩进，开启动作段
        <参数>: <值>        # 8 缩进，属于上一个 action
```

最少只需要一个源目录，别的都可以省略。

### 3.1 源目录

- 以 `/` 开头，**顶格**书写
- **可以连续写多行**，多个源共用后面的规则（逻辑上是「或」）
- 支持正则：`/Pictures/(教程)`、`/__我的文件/梗图.+`
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

### 3.3 匹配条件 `match:`

- 4 缩进，**可写多行**，多条之间是「或」
- 值是**正则**，对**文件名**（不含路径）做「包含」匹配
- 一条都不写 = 全部文件

```conf
/DCIM/Camera
    /__我的文件/_归档文件/购买建议/购买建议-奶茶
    match: [0-9]{2}-.*(奶|茶)
```

`26-奶茶.jpg` 命中；`咖啡.jpg` 不命中。

> 条件按正则解释，`com.microsoft.todos` 里的 `.` 是任意字符。要精确匹配点号得写 `com\.microsoft\.todos`。

### 3.4 排除条件 `exclude:`

- 4 缩进，可多行
- 命中的文件**跳过不动**

```conf
/Download
    /Download/new_dir
    match: [0-9a-z]{20,}
    exclude: .+(jpg|txt)
```

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
    match: com.microsoft.todos

# 待消费
/DCIM/Screenshots
    /__我的文件/_归档文件/_待处理/待消费
    match: net.hpoi
    match: com.taobao.taobao
    match: com.xunmeng.pinduoduo

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
    match: com.deepseek.chat
    match: com.openai.chatgpt
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
| 结构 | 源目录必须顶格且 `/` 开头；目标最多一个；8 缩进必须跟在 `action:` 后 |
| 动作 | 动作名是否认识；必需参数是否缺失；参数名是否属于该动作 |
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

## 九、从 v1 迁移

旧配置用的是 v1 语法，主要差异：

| v1 写法 | v2 写法 |
|---|---|
| 裸正则行当条件 | `match: 正则` |
| `- 正则` / `-: 正则` | `exclude: 正则` |
| `target:` 作重命名模板 | `action: rename` + 8 缩进的 `to:` |
| `.Prefix:` | `action: rename` + `prefix:` |
| `.replace:` | `action: rename` + `replace:` / `with:` |
| `reg:` | 放回 `action:` 段下的 `match:` |
| `write_append:` | `action: append` |
| `delete-empty: true` | `action: clean` |

迁移后，检查器遇到 v1 特征会提示「这是旧语法」，而不是逐行报错。

---

## 十、Termux 定时运行

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
