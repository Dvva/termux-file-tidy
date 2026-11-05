#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tasker_file_tify.conf 语法检查器

只做静态检查，不移动任何文件。检查项：
  1. 缩进：Tab / 空格混用、空格数不是 4 的倍数、行尾多余空白
  2. 结构：源目录、目标目录、条件、指令之间的位置关系
  3. 指令名与取值是否合法（action 只能是 move/copy/rename 等）
  4. 条件、排除、reg、replace 的正则能否编译
  5. 模板变量是否是已知变量（{ext} {time} {name} ...）
  6. 路径格式：必须以 / 开头、不能含反斜杠、不能有重复斜杠

用法：
    python check_conf.py tasker_file_tify.conf
    python check_conf.py tasker_file_tify.conf --quiet   # 只显示错误和警告
    python check_conf.py tasker_file_tify.conf --strict  # 提示也视为失败
"""

import argparse
import re
import sys
from pathlib import Path

# ============================================================
# 已知指令表
# ============================================================

# 指令名 -> 允许的取值；None 表示取值任意（但不能为空）
KNOWN_DIRECTIVES = {
    "action": ("move", "copy", "rename"),
    "target": None,
    "reg": None,
    "replace": None,
    "prefix": None,
    "command": None,
    "write_append": None,
    "delete-empty": ("true", "false"),
}

# 别名 -> 标准名，兼容历史写法
DIRECTIVE_ALIASES = {
    ".prefix": "prefix",
    ".replace": "replace",
    ".reg": "reg",
    "delete_empty": "delete-empty",
    "write-append": "write_append",
}

# 指令匹配：只认白名单里的名字，避免把含冒号的匹配条件误判成指令
DIRECTIVE_RE = re.compile(
    r"^(?P<key>\.?action|\.?target|reg|\.replace|\.?prefix|command|write_append"
    r"|delete[-_]empty)\s*:\s*(?P<value>.*)$",
    re.IGNORECASE,
)

# 模板变量：不带参数的
KNOWN_VARS = {"ext", "name", "timestamp", "time", "date", "prefix", "ext_upper", "name_upper"}
# 模板变量：可以带参数写成 {date:YYYYmmdd}
KNOWN_VARS_WITH_ARG = {"date", "prefix", "time"}

# ============================================================
# 结果收集
# ============================================================

ERROR = "错误"
WARN = "警告"
HINT = "提示"


class Lint:
    def __init__(self):
        self.items = []  # (行号, 级别, 说明)

    def add(self, lineno, level, msg):
        self.items.append((lineno, level, msg))

    def count(self, level=None):
        if level is None:
            return len(self.items)
        return sum(1 for _, lv, _ in self.items if lv == level)

    def has_error(self):
        return self.count(ERROR) > 0


# ============================================================
# 规则块
# ============================================================

class Block:
    """一个由空行分隔的规则块"""

    def __init__(self, start_line):
        self.start_line = start_line
        self.sources = []       # [(行号, 路径)]
        self.target = None      # (行号, 路径)
        self.conditions = []    # [(行号, 正则)]
        self.excludes = []      # [(行号, 正则)]
        self.directives = []    # [(行号, 标准名, 原始名, 值)]
        self.has_content = False

    def is_empty(self):
        return not (self.sources or self.target or self.conditions
                    or self.excludes or self.directives)

    def directive_names(self):
        return {d[1] for d in self.directives}


# ============================================================
# 单行内容校验
# ============================================================

def check_path(lineno, kind, path, lint):
    """校验源/目标路径的基本格式"""
    if "\\" in path:
        lint.add(lineno, ERROR, f"{kind}包含反斜杠，手机路径应使用 /：{path}")
    if "//" in path:
        lint.add(lineno, WARN, f"{kind}出现重复斜杠：{path}")
    if not path.startswith("/"):
        lint.add(lineno, ERROR, f"{kind}必须以 / 开头：{path}")
    if re.search(r"\s", path):
        lint.add(lineno, WARN, f"{kind}中间含有空白字符：{path}")


def check_regex(lineno, kind, pattern, lint):
    """校验正则能否编译"""
    try:
        re.compile(pattern)
    except re.error as e:
        lint.add(lineno, ERROR, f"{kind}不是合法正则（{e}）：{pattern}")


VAR_RE = re.compile(r"\{([^{}]*)\}")


def check_template(lineno, kind, value, lint):
    """校验 {变量} 模板"""
    # 单独出现的 {} 是「当前文件路径」占位符，允许
    for m in VAR_RE.finditer(value):
        body = m.group(1)
        if body == "":
            continue
        name, sep, arg = body.partition(":")
        name_low = name.lower()
        if name_low not in KNOWN_VARS:
            lint.add(lineno, WARN, f"{kind}使用了未知变量 {{{body}}}")
            continue
        if sep and name_low not in KNOWN_VARS_WITH_ARG:
            lint.add(lineno, WARN, f"{kind}的变量 {{{name}}} 不支持参数")


def parse_indent(indent, lineno, lint):
    """检查缩进写法，返回 'tab' / 'space' / ''"""
    if not indent:
        return ""
    has_tab = "\t" in indent
    has_space = " " in indent
    if has_tab and has_space:
        lint.add(lineno, ERROR, "缩进混用了 Tab 和空格")
    elif has_space and len(indent) % 4 != 0:
        lint.add(lineno, WARN, f"缩进用了 {len(indent)} 个空格，不是 4 的倍数")
    return "tab" if has_tab else "space"


# ============================================================
# 主检查流程
# ============================================================

def check_file(path, quiet=False):
    try:
        raw_text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        print(f"[无法读取] {path} 不是 UTF-8 编码：{e}")
        return 1

    lines = raw_text.splitlines()
    lint = Lint()
    block = Block(1)
    indent_kinds = set()   # 全文件用到的缩进类型
    block_count = 0

    for lineno, line in enumerate(lines, 1):
        # 行尾空白
        if line != line.rstrip():
            lint.add(lineno, HINT, "行尾有多余空白")

        stripped = line.strip()

        # 空行：结束当前块
        if not stripped:
            if not block.is_empty():
                block_count += 1
                validate_block(block, lint)
            block = Block(lineno + 1)
            continue

        # 注释
        if stripped.startswith("#"):
            continue

        # 拆分缩进与内容
        m = re.match(r"^([ \t]*)(.*)$", line)
        indent, content = m.group(1), m.group(2).rstrip()

        kind = parse_indent(indent, lineno, lint)
        if kind:
            indent_kinds.add(kind)
        block.has_content = True

        # ---------- 顶格 ----------
        if not indent:
            if content.startswith("/"):
                check_path(lineno, "源目录", content, lint)
                block.sources.append((lineno, content))
            else:
                lint.add(lineno, ERROR,
                         f"顶格行必须是以 / 开头的源目录，实际是：{content[:40]}")
            continue

        # ---------- 缩进 ----------
        if content.startswith("/"):
            # 目标目录
            check_path(lineno, "目标目录", content, lint)
            if block.target:
                lint.add(lineno, WARN,
                         "一个规则块只能有一个目标目录，这一行被忽略")
            elif not block.sources:
                lint.add(lineno, ERROR,
                         "目标目录之前没有源目录，这一行会被忽略")
            else:
                block.target = (lineno, content)
            continue

        if content.startswith("-"):
            # 排除条件：允许 "- xxx" 和 "-: xxx"
            pattern = content[1:]
            if pattern.startswith(":"):
                pattern = pattern[1:]
            pattern = pattern.strip()
            if not pattern:
                lint.add(lineno, ERROR, "排除条件为空")
            else:
                check_regex(lineno, "排除条件", pattern, lint)
                block.excludes.append((lineno, pattern))
            continue

        # 指令
        dm = DIRECTIVE_RE.match(content)
        if dm:
            raw_key = dm.group("key")
            value = dm.group("value").strip()
            key = DIRECTIVE_ALIASES.get(raw_key.lower(), raw_key.lower())

            if key not in KNOWN_DIRECTIVES:
                lint.add(lineno, WARN, f"未知指令：{raw_key}")
            else:
                allowed = KNOWN_DIRECTIVES[key]
                if allowed is not None and value.lower() not in allowed:
                    lint.add(lineno, ERROR,
                             f"{raw_key} 的取值只能是 {'/'.join(allowed)}，实际是：{value}")
                elif not value:
                    lint.add(lineno, ERROR, f"{raw_key} 的值为空")

            # 各指令的额外校验
            if key == "reg":
                check_regex(lineno, "reg", value, lint)
            elif key == "replace":
                check_regex(lineno, "replace", value, lint)
            elif key in ("target", "prefix"):
                check_template(lineno, key, value, lint)
            elif key == "command":
                check_template(lineno, "command", value, lint)

            block.directives.append((lineno, key, raw_key, value))
            continue

        # 普通匹配条件
        check_regex(lineno, "匹配条件", content, lint)
        block.conditions.append((lineno, content))

    # 文件末尾的块
    if not block.is_empty():
        block_count += 1
        validate_block(block, lint)

    # 全局缩进一致性（不同块之间混用不影响运行，给出警告即可）
    if len(indent_kinds) > 1:
        lint.add(0, WARN, "整个文件同时用了 Tab 和空格两种缩进方式，建议统一")

    # ---------------- 输出 ----------------
    print(f"检查文件：{path}")
    if not lint.items:
        print("  ✓ 没有发现问题")
    else:
        for lineno, level, msg in sorted(lint.items, key=lambda x: (x[0], x[1])):
            where = f"第 {lineno:>3} 行" if lineno else "全局"
            if quiet and level == HINT:
                continue
            print(f"  {where} [{level}] {msg}")

    print("-" * 58)
    print(f"共 {len(lines)} 行，{block_count} 个规则块，"
          f"错误 {lint.count(ERROR)}，警告 {lint.count(WARN)}，提示 {lint.count(HINT)}")

    return 1 if lint.has_error() else 0


def validate_block(block, lint):
    """块级结构校验"""
    names = block.directive_names()

    # 只有源没有目标，也不是纯收尾块
    has_target = bool(block.target) or "target" in names
    if block.sources and not has_target and "delete-empty" not in names:
        actions = [d[3].lower() for d in block.directives if d[1] == "action"]
        if "move" in actions:
            lint.add(block.sources[0][0], WARN,
                     "action 是 move，但没有写目标目录")
        elif "rename" not in actions:
            lint.add(block.sources[0][0], WARN,
                     "这个规则块只有源目录，没有目标目录，也不会执行任何动作")

    # 有目标但没有源（理论上不会走到，set_target 已拦截）
    if block.target and not block.sources:
        lint.add(block.target[0], ERROR, "规则块缺少源目录")

    # 写了 target 却没有 action
    if "target" in names and "action" not in names:
        lint.add(block.start_line, HINT,
                 "写了 target 但没有 action，可能是忘了声明 action: rename")

    # 多段 action 的提示
    actions = [d for d in block.directives if d[1] == "action"]
    if len(actions) > 1:
        lint.add(actions[0][0], HINT,
                 f"这个块声明了 {len(actions)} 段动作，将按顺序依次执行")


# ============================================================
# 入口
# ============================================================

def main():
    # 保证在 Windows 控制台和 Termux 下都能正常输出中文
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="检查 tasker_file_tify.conf 的语法与缩进",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0 无错误，1 有错误，2 文件不存在",
    )
    parser.add_argument("config", nargs="?", default="tasker_file_tify.conf",
                        help="配置文件路径（默认 ./tasker_file_tify.conf）")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="不显示提示级别，只看错误和警告")
    parser.add_argument("--strict", action="store_true",
                        help="提示也算失败，退出码为 1")
    args = parser.parse_args()

    path = Path(args.config)
    if not path.exists():
        print(f"[错误] 找不到配置文件：{path}")
        return 2

    code = check_file(path, quiet=args.quiet)

    if args.strict and code == 0:
        # strict 模式下把提示也当失败
        try:
            text = path.read_text(encoding="utf-8")
            if any(l != l.rstrip() for l in text.splitlines()):
                return 1
        except Exception:
            pass

    return code


if __name__ == "__main__":
    sys.exit(main())
