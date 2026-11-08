#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tasker_file_tify.conf 语法检查器（v2）

只做静态检查，不移动任何文件。解析逻辑直接复用 tidy_core.parse，
保证「检查器认为对的配置」和「执行器实际解析的配置」完全一致。

用法：
    python check_conf.py tasker_file_tify.conf
    python check_conf.py --quiet
    python check_conf.py --strict
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from tidy_core import (
    ERROR,
    KNOWN_VARS,
    TEMPLATE_KEYS,
    VAR_WITH_ARG,
    parse,
)

VAR_RE = re.compile(r"\{([^{}]*)\}")


def check_templates(blocks) -> list[tuple[int, str, str]]:
    """检查模板变量：未知变量、给不支持参数的变量传参"""
    issues: list[tuple[int, str, str]] = []
    for block in blocks:
        for action in block.actions:
            for param in action.params:
                if param.key not in TEMPLATE_KEYS:
                    continue
                for m in VAR_RE.finditer(param.value):
                    body = m.group(1)
                    if body == "":
                        continue
                    name, _, arg = body.partition(":")
                    low = name.lower()
                    if low not in KNOWN_VARS:
                        issues.append((param.line, "warn",
                                       f"未知模板变量 {{{body}}}"))
                    elif arg and low not in VAR_WITH_ARG:
                        issues.append((param.line, "warn",
                                       f"变量 {{{name}}} 不支持参数"))
    return issues


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="检查 tasker_file_tify.conf 的缩进、结构与语法",
        epilog="退出码：0 无错误，1 有错误，2 文件不存在",
    )
    ap.add_argument("config", nargs="?", default="tasker_file_tify.conf")
    ap.add_argument("--quiet", "-q", action="store_true", help="只显示错误")
    ap.add_argument("--strict", action="store_true", help="警告也算失败")
    args = ap.parse_args()

    path = Path(args.config)
    if not path.exists():
        print(f"[错误] 找不到配置文件：{path}")
        return 2

    text = path.read_text(encoding="utf-8")
    blocks, issues, ignores = parse(text, str(path))

    entries = [(i.line, i.level, i.msg) for i in issues]
    entries.extend(check_templates(blocks))
    entries.sort(key=lambda x: (x[0], x[1]))

    print(f"检查文件：{path}")
    if not entries:
        print("  ✓ 没有发现问题")
    else:
        for line, level, msg in entries:
            if args.quiet and level != "error":
                continue
            tag = "错误" if level == "error" else "警告"
            print(f"  第 {line:>3} 行 [{tag}] {msg}")

    n_err = sum(1 for e in entries if e[1] == "error")
    n_warn = sum(1 for e in entries if e[1] == "warn")

    print("-" * 56)
    print(f"共 {len(text.splitlines())} 行，{len(blocks)} 个规则块，"
          f"{len(ignores)} 条 ignore，错误 {n_err}，警告 {n_warn}")

    if n_err:
        return 1
    if args.strict and n_warn:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
