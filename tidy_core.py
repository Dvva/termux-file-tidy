#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
termux-file-tidy 核心模块

只做三件事：把配置解析成规则块、把文件匹配到规则块、按规则块执行动作。
CLI、日志、通知都在 tasker_file_tidy.py 里，检查器直接复用这里的 parse()。
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

# 手机存储根，Android 上所有路径都相对它
DEFAULT_ROOT = "/storage/emulated/0"

ERROR = "error"
WARN = "warn"

# ============================================================
# 数据结构
# ============================================================


@dataclass
class Issue:
    """一行配置的检查结果"""

    line: int
    level: str
    msg: str


@dataclass
class Param:
    """动作的一个参数，例如 to: /x"""

    key: str
    value: str
    line: int


@dataclass
class Action:
    """一个动作段"""

    name: str
    params: list[Param] = field(default_factory=list)
    line: int = 0

    def get(self, key: str, default: str = "") -> str:
        for p in self.params:
            if p.key == key:
                return p.value
        return default

    def all(self, key: str) -> list[str]:
        return [p.value for p in self.params if p.key == key]


@dataclass
class Block:
    """一个规则块"""

    line: int
    sources: list[str] = field(default_factory=list)
    target: str | None = None
    patterns: list[str] = field(default_factory=list)
    regexes: list[tuple[str, int]] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    regex_excludes: list[tuple[str, int]] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    skip: bool = False
    clean: bool = False
    dirs: list[Path] = field(default_factory=list)  # 展开后的真实目录

    def is_complete(self) -> bool:
        """块是否已经「有内容」，用于判断顶格行是否开新块"""
        return bool(self.target or self.actions or self.clean)

    @property
    def depth(self) -> int:
        """源目录的最大层级，越深越具体"""
        return max((len(d.parts) for d in self.dirs), default=0)


@dataclass
class FileRef:
    """一个待处理的文件"""

    path: Path
    stat: os.stat_result
    index: int = 0
    total: int = 0


@dataclass
class Result:
    """单个文件的处理结果"""

    src: Path
    dst: Path | None = None
    ok: bool = True
    error: str = ""
    note: str = ""


# ============================================================
# 解析器
# ============================================================

ACTION_NAMES = {"move", "copy", "rename", "command", "append", "clean"}

# 每个动作允许的参数（match 对所有动作都可用）
ACTION_PARAMS = {
    "move": {"to"},
    "copy": {"to"},
    "rename": {"to", "replace", "with", "prefix", "suffix", "ext", "case",
               "sanitize", "sequence"},
    "command": {"cmd"},
    "append": {"text", "file"},
    "clean": set(),
}

KNOWN_VARS = {"name", "ext", "full", "dir", "mtime", "ctime", "atime",
              "now", "date", "timestamp", "index", "count", "hash"}

VAR_WITH_ARG = {"mtime", "ctime", "atime", "now", "date", "index", "hash"}

# 动作参数里允许写模板变量的键
TEMPLATE_KEYS = {"to", "text", "file", "cmd"}

PARAM_RE = re.compile(r"^([A-Za-z_]+)\s*:\s*(.*)$")
VAR_RE = re.compile(r"\{([^{}]*)\}")


def _indent_of(line: str) -> str:
    """取行首空白"""
    return line[: len(line) - len(line.lstrip())]


def parse(text: str, source_name: str = "<config>") -> tuple[list[Block], list[Issue], list[tuple[str, int]]]:
    """把配置文本解析成规则块列表、问题列表、全局忽略目录

    ignore: 是全局的，出现在任意位置都作用于所有块。
    """
    blocks: list[Block] = []
    issues: list[Issue] = []
    ignores: list[tuple[str, int]] = []
    cur: Block | None = None
    act: Action | None = None

    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            cur, act = None, None
            continue

        content = raw.strip()
        if content.startswith("#"):
            continue

        indent = len(_indent_of(raw))
        if "\t" in raw[:indent]:
            issues.append(Issue(lineno, ERROR, "缩进用了 Tab，只能用空格"))
        if indent not in (0, 4, 8):
            issues.append(Issue(lineno, ERROR,
                                f"缩进 {indent} 格不合法，只能是 0 / 4 / 8"))

        # ---------- 顶格：全局忽略 / 源目录 ----------
        if indent == 0:
            if content.startswith("ignore:"):
                pat = content[len("ignore:"):].strip()
                if not pat:
                    issues.append(Issue(lineno, ERROR, "ignore 的值为空"))
                else:
                    ignores.append((pat, lineno))
                continue
            if not content.startswith("/"):
                issues.append(Issue(lineno, ERROR,
                                    f"顶格行必须以 / 开头，实际是：{content[:30]}"))
                continue
            if cur is None or cur.is_complete():
                cur = Block(line=lineno)
                blocks.append(cur)
                act = None
            cur.sources.append(content)
            continue

        # 缩进行必须在某个源目录之后
        if cur is None:
            issues.append(Issue(lineno, ERROR, "缩进行出现在任何源目录之前"))
            cur = Block(line=lineno)
            blocks.append(cur)

        # ---------- 4 缩进：目标 / 条件 / 动作 / 其它 ----------
        if indent == 4:
            act = None

            if content.startswith("/"):
                if cur.target is not None:
                    issues.append(Issue(
                        lineno, ERROR,
                        "一个块里出现了两个目标目录（检查上一块是否漏了空行？）"))
                cur.target = content
                continue

            if content.startswith("match:"):
                pat = content[len("match:"):].strip()
                _check_regex(pat, lineno, "match", issues)
                cur.regexes.append((pat, lineno))
                continue

            if content.startswith("exclude:"):
                pat = content[len("exclude:"):].strip()
                _check_regex(pat, lineno, "exclude", issues)
                cur.regex_excludes.append((pat, lineno))
                continue

            if content.startswith("- "):
                cur.excludes.append(content[2:].strip())
                continue

            if content.startswith("skip:"):
                val = content[len("skip:"):].strip().lower()
                if val not in ("true", "false"):
                    issues.append(Issue(lineno, ERROR,
                                        f"skip 只能是 true/false，实际是：{val}"))
                cur.skip = val == "true"
                continue

            if content.startswith("action:"):
                name = content[len("action:"):].strip()
                if name not in ACTION_NAMES:
                    issues.append(Issue(lineno, ERROR, f"未知动作：{name}"))
                if name == "clean":
                    cur.clean = True
                act = Action(name=name, line=lineno)
                cur.actions.append(act)
                continue

            # 裸行 = 通配符条件
            if ":" in content:
                issues.append(Issue(
                    lineno, ERROR,
                    f"裸行里出现冒号，疑似指令拼写错误：{content[:30]}"))
            cur.patterns.append(content)
            continue

        # ---------- 8 缩进：动作参数 ----------
        if indent == 8:
            if act is None:
                issues.append(Issue(lineno, ERROR,
                                    "8 缩进必须紧跟在 action: 之后"))
                continue
            m = PARAM_RE.match(content)
            if not m:
                issues.append(Issue(lineno, ERROR,
                                    f"动作参数必须是 key: value 形式：{content[:30]}"))
                continue
            key, val = m.group(1), m.group(2).strip()
            allowed = ACTION_PARAMS.get(act.name, set()) | {"match"}
            if key not in allowed:
                issues.append(Issue(
                    lineno, ERROR, f"动作 {act.name} 不支持参数 {key}"))
            act.params.append(Param(key=key, value=val, line=lineno))

    # 块级结构校验
    for b in blocks:
        if b.sources and not b.target and not b.actions:
            issues.append(Issue(b.line, WARN, "这个块只有源目录，不会执行任何动作"))
        if b.target and not b.sources:
            issues.append(Issue(b.line, ERROR, "这个块缺少源目录"))

    return blocks, issues, ignores


def _check_regex(pattern: str, lineno: int, kind: str, issues: list[Issue]) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        issues.append(Issue(lineno, ERROR, f"{kind} 正则非法（{exc}）：{pattern}"))


# ============================================================
# 路径与通配符
# ============================================================


def has_wildcard(s: str) -> bool:
    return any(c in s for c in "*?[")


def to_real(path_str: str, root: Path) -> Path:
    """把配置里的 /DCIM 转成真实路径"""
    if str(path_str).startswith(str(root)):
        return Path(path_str)
    return root / str(path_str).lstrip("/")


def expand_sources(sources: Iterable[str], root: Path) -> list[Path]:
    """展开源目录（支持通配符）"""
    out: list[Path] = []
    for s in sources:
        if has_wildcard(s):
            pattern = s.lstrip("/")
            try:
                out.extend(sorted(root.glob(pattern)))
            except (OSError, ValueError) as exc:
                log.warning("展开源目录失败 %s：%s", s, exc)
        else:
            p = to_real(s, root)
            if p.is_dir():
                out.append(p)
            else:
                log.warning("源目录不存在：%s", p)
    return out


def expand_ignores(entries: Iterable[tuple[str, int]], root: Path) -> list[Path]:
    """展开配置里的 ignore 目录（支持通配符），只保留真实存在的目录"""
    out: list[Path] = []
    for s, _line in entries:
        if has_wildcard(s):
            try:
                out.extend(p for p in root.glob(s.lstrip("/")) if p.is_dir())
            except (OSError, ValueError):
                pass
        else:
            p = to_real(s, root)
            if p.is_dir():
                out.append(p)
    return out


def _under(path: Path, roots: Iterable[Path]) -> bool:
    """path 是否落在 roots 里的某个目录（含自身）"""
    for r in roots:
        if path == r:
            return True
        try:
            if path.is_relative_to(r):
                return True
        except (OSError, ValueError):
            pass
    return False


def targets_to_skip(targets: Iterable[Path], sources: Iterable[Path]) -> list[Path]:
    """
    只有「落在某个源目录内部、且本身不是源」的目标才需要从扫描中排除。

    否则会出现两种情况之一：
      - 目标在源内部：刚移进去的文件又被扫出来，重复处理
      - 目标本身是另一个块的源（链式规则）：不该排除，否则那条规则永远空转
    """
    src = list(sources)
    out: list[Path] = []
    for t in targets:
        if t in src:
            continue          # 目标本身就是源，必须扫描
        if _under(t, src):
            out.append(t)
    return out


def wildcard_match(name: str, pattern: str) -> bool:
    """通配符匹配；没写通配符时按「包含」处理"""
    if has_wildcard(pattern):
        return fnmatch.fnmatchcase(name, pattern)
    return pattern in name


def match_block(block: Block, name: str) -> bool:
    """
    判断文件名是否命中这个块的条件。

    裸行通配符和 match: 正则合在一起，统一是「或」关系：
    任意一条命中就算匹配，不会因为另一组没命中而被否决。
    """
    has_condition = bool(block.patterns or block.regexes)
    if has_condition:
        hit = any(wildcard_match(name, p) for p in block.patterns)
        if not hit:
            hit = any(re.search(p, name) for p, _ in block.regexes)
        if not hit:
            return False

    for p in block.excludes:
        if wildcard_match(name, p):
            return False
    for p, _ in block.regex_excludes:
        if re.search(p, name):
            return False
    return True


def attach_sources(blocks: Iterable[Block], root: Path) -> None:
    """把每个块的源目录展开成真实路径，供归属判断使用"""
    for b in blocks:
        if not b.dirs:
            b.dirs = expand_sources(b.sources, root)


def owner_blocks(blocks: Iterable[Block], path: Path) -> list[Block]:
    """
    找出「源目录真的包含这个文件」的块。

    一个文件可能被父目录的源扫到，但它只属于层级最深的那些块。
    按源深度降序、配置文件行号升序排列，保证具体规则优先于笼统规则。
    """
    cands = [
        b for b in blocks
        if any(path.is_relative_to(d) for d in b.dirs)
    ]
    cands.sort(key=lambda b: (-b.depth, b.line))
    return cands


# ============================================================
# 扫描
# ============================================================


def scan(dirs: Iterable[Path], skip: Iterable[Path],
         ignores: Iterable[Path] = (), include_hidden: bool = False) -> list[FileRef]:
    """递归收集文件，跳过目标目录、ignore 目录和隐藏目录"""
    skip_set = [p.resolve() for p in skip if p]
    ignore_set = [p.resolve() for p in ignores if p]
    seen: set[Path] = set()
    files: list[FileRef] = []

    for d in dirs:
        if not d.is_dir():
            continue
        # 源目录本身也可能在 ignore 列表里，先挡掉
        try:
            if _under(d.resolve(), ignore_set):
                continue
        except OSError:
            continue
        for cur_dir, dirnames, filenames in os.walk(d):
            cur = Path(cur_dir)
            kept = []
            for n in dirnames:
                # 默认不进入隐藏目录（应用缓存、SDK 目录）
                if not include_hidden and n.startswith("."):
                    continue
                full = cur / n
                try:
                    resolved = full.resolve()
                except OSError:
                    continue
                if _under(resolved, skip_set) or _under(resolved, ignore_set):
                    continue
                kept.append(n)
            dirnames[:] = kept

            for name in filenames:
                if not include_hidden and name.startswith("."):
                    continue
                p = cur / name
                if p in seen:
                    continue
                seen.add(p)
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append(FileRef(path=p, stat=st))

    files.sort(key=lambda f: str(f.path))
    for i, f in enumerate(files, 1):
        f.index = i
    for f in files:
        f.total = len(files)
    return files


# ============================================================
# 模板变量
# ============================================================


def _fmt_time(ts: float, fmt: str) -> str:
    return datetime.fromtimestamp(ts).strftime(fmt or "%Y%m%d")


def _var_dir(ctx: FileRef, arg: str) -> str:
    return ctx.path.parent.name


def _var_hash(ctx: FileRef, arg: str) -> str:
    length = int(arg) if arg.isdigit() else 8
    h = hashlib.md5()
    try:
        with ctx.path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:length]


def _var_index(ctx: FileRef, arg: str) -> str:
    try:
        return format(ctx.index, arg) if arg else str(ctx.index)
    except (ValueError, TypeError):
        return str(ctx.index)


VARS: dict[str, Callable[[FileRef, str], str]] = {
    "name": lambda c, a: c.path.stem,
    "ext": lambda c, a: c.path.suffix.lstrip("."),
    "full": lambda c, a: c.path.name,
    "dir": _var_dir,
    "mtime": lambda c, a: _fmt_time(c.stat.st_mtime, a),
    "ctime": lambda c, a: _fmt_time(c.stat.st_ctime, a),
    "atime": lambda c, a: _fmt_time(c.stat.st_atime, a),
    "now": lambda c, a: _fmt_time(time.time(), a),
    "date": lambda c, a: _fmt_time(c.stat.st_mtime, a),
    "timestamp": lambda c, a: str(int(c.stat.st_mtime)),
    "index": _var_index,
    "count": lambda c, a: str(c.total),
    "hash": _var_hash,
}


def render(template: str, ctx: FileRef, quote: bool = False) -> str:
    """把 {name} 这类模板替换成实际值；{} 代表文件完整路径

    quote=True 时给路径加 shell 引号，供 command 使用，
    避免文件名里的空格、括号被 shell 拆开。
    """

    def repl(m: re.Match) -> str:
        body = m.group(1)
        if body == "":
            return shlex.quote(str(ctx.path)) if quote else str(ctx.path)
        name, _, arg = body.partition(":")
        fn = VARS.get(name.lower())
        if fn is None:
            return m.group(0)
        return fn(ctx, arg)

    return VAR_RE.sub(repl, template)


# ============================================================
# 动作
# ============================================================


def _apply_rename_params(action: Action, name: str, ctx: FileRef) -> str:
    """执行 rename 的参数流水线，返回新文件名"""
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""

    # sanitize
    for p in action.params:
        if p.key == "sanitize" and p.value.lower() == "true":
            stem = re.sub(r'[\\/:*?"<>|]', "_", stem)
            suffix = re.sub(r'[\\/:*?"<>|]', "_", suffix)

    # replace / with 成对处理
    params = action.params
    i = 0
    while i < len(params):
        p = params[i]
        if p.key == "replace":
            repl = ""
            if i + 1 < len(params) and params[i + 1].key == "with":
                repl = params[i + 1].value
                i += 1
            try:
                stem = re.sub(p.value, repl, stem)
            except re.error:
                pass
        i += 1

    # case
    for p in action.params:
        if p.key == "case":
            style = p.value.lower()
            if style == "lower":
                stem = stem.lower()
            elif style == "upper":
                stem = stem.upper()
            elif style == "title":
                stem = stem.title()

    # prefix / suffix
    prefix = "".join(p.value for p in action.params if p.key == "prefix")
    suffix_add = "".join(p.value for p in action.params if p.key == "suffix")
    stem = render(prefix, ctx) + stem + render(suffix_add, ctx)

    # ext
    for p in action.params:
        if p.key == "ext":
            new_ext = render(p.value, ctx).lstrip(".")
            suffix = "." + new_ext if new_ext else ""

    # sequence
    for p in action.params:
        if p.key == "sequence":
            try:
                stem = format(ctx.index, p.value) + stem
            except (ValueError, TypeError):
                pass

    return stem + ("." + suffix if suffix else "")


def apply_rename(action: Action, name: str, ctx: FileRef) -> str:
    to = action.get("to")
    if to:
        rendered = render(to, ctx)
        # to 里没写扩展名时保留原扩展名
        if "." not in Path(rendered).name and ctx.path.suffix:
            rendered += ctx.path.suffix
        return Path(rendered).name
    return _apply_rename_params(action, name, ctx)


# ============================================================
# 执行
# ============================================================


@dataclass
class Plan:
    """一个文件的处理计划"""

    src: Path
    dst: Path
    action: str = "move"
    command: str | None = None
    append_text: str | None = None
    append_file: str | None = None


class Engine:
    """执行引擎"""

    def __init__(self, root: Path, dry_run: bool = False,
                 undo_path: Path | None = None):
        self.root = root
        self.dry_run = dry_run
        self.undo_path = undo_path
        self.undo_lines: list[str] = []

    # ---------- 规划 ----------
    def plan(self, block: Block, f: FileRef) -> Plan | None:
        """计算这个文件最终要去哪，不落盘"""
        name = f.path.name
        dest_dir = f.path.parent

        if block.target:
            dest_dir = to_real(render(block.target, f), self.root)

        op = "move"
        command = None
        append_text = None
        append_file = None

        for action in block.actions:
            if action.name == "rename":
                name = apply_rename(action, name, f)
            elif action.name in ("move", "copy"):
                op = action.name
                to = action.get("to")
                if to:
                    raw = render(to, f)
                    p = to_real(raw, self.root)
                    if raw.endswith("/") or Path(raw).suffix == "":
                        dest_dir = p
                    else:
                        dest_dir = p.parent
                        name = p.name
            elif action.name == "command":
                # command 里的路径要加 shell 引号，文件名常含空格和括号
                command = render(action.get("cmd"), f, quote=True)
            elif action.name == "append":
                append_text = render(action.get("text"), f)
                append_file = render(action.get("file"), f)
            elif action.name == "clean":
                return None

        dst = dest_dir / name
        # 原地不动但仍有副作用动作（command / append）时，必须继续往下走
        if dst == f.path and not command and append_text is None:
            return None
        return Plan(src=f.path, dst=dst, action=op, command=command,
                    append_text=append_text, append_file=append_file)

    # ---------- 冲突 ----------
    @staticmethod
    def resolve_conflict(dst: Path, taken: set[Path]) -> Path:
        if not dst.exists() and dst not in taken:
            return dst
        stem, suffix = dst.stem, dst.suffix
        i = 1
        while True:
            cand = dst.with_name(f"{stem} ({i}){suffix}")
            if not cand.exists() and cand not in taken:
                return cand
            i += 1

    # ---------- 执行 ----------
    def run_plan(self, plan: Plan, taken: set[Path]) -> Result:
        in_place = plan.dst == plan.src
        dst = plan.src if in_place else self.resolve_conflict(plan.dst, taken)

        if self.dry_run:
            return Result(src=plan.src, dst=dst, ok=True, note="dry-run")

        try:
            if not in_place:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if plan.action == "copy":
                    shutil.copy2(plan.src, dst)
                else:
                    shutil.move(str(plan.src), str(dst))
        except OSError as exc:
            return Result(src=plan.src, ok=False, error=str(exc))

        if not in_place:
            taken.add(dst)
            self.undo_lines.append(f"{plan.action}\t{plan.src}\t{dst}")

        if plan.command:
            try:
                subprocess.run(plan.command, shell=True, check=False,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60)
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("命令执行失败 %s：%s", plan.command, exc)

        if plan.append_text is not None and plan.append_file:
            try:
                target = Path(plan.append_file)
                if not target.is_absolute():
                    target = dst.parent / target
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as fh:
                    fh.write(plan.append_text + "\n")
            except OSError as exc:
                log.warning("追加写入失败 %s：%s", plan.append_file, exc)

        return Result(src=plan.src, dst=dst, ok=True)

    def save_undo(self) -> None:
        if self.dry_run or not self.undo_path or not self.undo_lines:
            return
        self.undo_path.parent.mkdir(parents=True, exist_ok=True)
        with self.undo_path.open("a", encoding="utf-8") as fh:
            for line in self.undo_lines:
                fh.write(line + "\n")


def clean_empty_dirs(dirs: Iterable[Path], dry_run: bool,
                     ignores: Iterable[Path] = (),
                     include_hidden: bool = False) -> list[Path]:
    """自底向上删除空目录，默认跳过隐藏目录和 ignore 目录"""
    ignore_set = [p.resolve() for p in ignores if p]
    removed: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for cur, dirnames, filenames in os.walk(d, topdown=False):
            p = Path(cur)
            if p == d:
                continue
            try:
                rel = p.relative_to(d)
            except ValueError:
                continue
            # 隐藏目录及其子目录一律不碰
            if not include_hidden and any(part.startswith(".") for part in rel.parts):
                continue
            try:
                resolved = p.resolve()
            except OSError:
                continue
            if _under(resolved, ignore_set):
                continue
            try:
                if not any(p.iterdir()):
                    if not dry_run:
                        p.rmdir()
                    removed.append(p)
            except OSError:
                pass
    return removed


def undo(undo_path: Path, dry_run: bool = False) -> tuple[int, int]:
    """撤销上一次运行记录的操作"""
    if not undo_path.exists():
        return 0, 0
    lines = undo_path.read_text(encoding="utf-8").splitlines()
    ok = fail = 0
    for line in reversed(lines):
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        op, src, dst = parts
        src_p, dst_p = Path(src), Path(dst)
        try:
            if not dst_p.exists():
                continue
            if dry_run:
                ok += 1
                continue
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            if op == "copy":
                dst_p.unlink(missing_ok=True)
            else:
                shutil.move(str(dst_p), str(src_p))
            ok += 1
        except OSError as exc:
            log.warning("撤销失败 %s：%s", line, exc)
            fail += 1
    return ok, fail
