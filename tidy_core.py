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
import json
import logging
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
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
    max_depth: int = 0          # 0 = 无限递归；1 = 只看源目录这一层
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

ACTION_NAMES = {"move", "copy", "rename", "command", "append", "clean",
                "dedupe", "compress-image", "compress-music", "compress-video"}

# 每个动作允许的参数（match 对所有动作都可用）
ACTION_PARAMS = {
    "move": {"to"},
    "copy": {"to"},
    "rename": {"to", "replace", "with", "prefix", "suffix", "ext", "case",
               "sanitize", "sequence"},
    "command": {"cmd"},
    "append": {"text", "file"},
    "clean": set(),
    # 去重：keep 是主目录，重复时保留这里的那份，可写多行按顺序优先
    "dedupe": {"keep"},
    # 压缩图片：quality 是 jpg 质量刻度；max_edge 不写 = 不改尺寸
    # flatten 把带透明通道的 png 拍成白底 jpg（默认开）
    # limit 是单次最多压几个（定时任务用），preserve_time 是否保留原时间戳
    "compress-image": {"quality", "max_edge", "flatten", "limit", "preserve_time"},
    # 压缩音乐：format 目标格式，bitrate 码率
    "compress-music": {"format", "bitrate", "quality", "limit", "preserve_time"},
    # 压缩视频：resolution 按视频网站口径写 1080p / 720p；height 是短边数值；
    # max_edge 是长边上限（1080 会得到 1080×608 这种）
    "compress-video": {"resolution", "max_edge", "height", "crf", "preset",
                       "limit", "preserve_time"},
}

# 定时任务里每类压缩默认最多处理多少个，配置里的 limit: 优先
SCHEDULED_LIMIT = {
    "compress-image": 100,
    "compress-music": 10,
    "compress-video": 2,
}

# 原地操作的动作（不产生 src -> dst 移动），走单独的压缩分支
COMPRESS_ACTIONS = {"compress-image", "compress-music", "compress-video"}

KNOWN_VARS = {"name", "ext", "full", "dir", "mtime", "ctime", "atime", "btime",
              "now", "date", "timestamp", "index", "count", "hash", "rand"}

VAR_WITH_ARG = {"mtime", "ctime", "atime", "btime", "now", "date", "index",
                "hash", "rand"}

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

            if content.startswith("depth:"):
                val = content[len("depth:"):].strip()
                if not val.isdigit() or int(val) < 1:
                    issues.append(Issue(lineno, ERROR,
                                        f"depth 必须是正整数，实际是：{val}"))
                else:
                    cur.max_depth = int(val)
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
                # 配置里引用了手机上已不存在的目录：只写日志，不刷终端
                log.debug("源目录不存在：%s", p)
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

    块的 max_depth 会限制文件相对源目录的层数：
      max_depth=1 时，源目录直接下的文件才算，子目录里的不算。
    """
    cands: list[Block] = []
    for b in blocks:
        hit = False
        for d in b.dirs:
            try:
                rel = path.relative_to(d)
            except ValueError:
                continue
            if b.max_depth and len(rel.parts) > b.max_depth:
                continue
            hit = True
            break
        if hit:
            cands.append(b)
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
    # %s 表示 Unix 秒级时间戳，各平台 strftime 支持不一致，这里直接算
    if fmt == "%s":
        return str(int(ts))
    return datetime.fromtimestamp(ts).strftime(fmt or "%Y%m%d")


def _birth_time(st: os.stat_result) -> float:
    """
    取文件创建时间。

    Linux / Android 多数文件系统不提供 birthtime，拿不到时回退到 mtime，
    保证模板变量始终有值，不会因为缺字段报错。
    """
    bt = getattr(st, "st_birthtime", None)
    if bt:
        return float(bt)
    return float(st.st_mtime)


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


def _var_rand(ctx: FileRef, arg: str) -> str:
    """随机数字，参数是位数，默认 3 位。用于给同秒文件加区分后缀。"""
    try:
        width = int(arg) if arg else 3
    except ValueError:
        width = 3
    width = max(1, min(width, 9))
    return str(random.randint(0, 10 ** width - 1)).zfill(width)


VARS: dict[str, Callable[[FileRef, str], str]] = {
    "name": lambda c, a: c.path.stem,
    "ext": lambda c, a: c.path.suffix.lstrip("."),
    "full": lambda c, a: c.path.name,
    "dir": _var_dir,
    "mtime": lambda c, a: _fmt_time(c.stat.st_mtime, a),
    "ctime": lambda c, a: _fmt_time(c.stat.st_ctime, a),
    "atime": lambda c, a: _fmt_time(c.stat.st_atime, a),
    "btime": lambda c, a: _fmt_time(_birth_time(c.stat), a),
    "now": lambda c, a: _fmt_time(time.time(), a),
    "date": lambda c, a: _fmt_time(c.stat.st_mtime, a),
    "timestamp": lambda c, a: str(int(c.stat.st_mtime)),
    "rand": _var_rand,
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
                     include_hidden: bool = False,
                     on_dir: Callable[[Path, int], None] | None = None) -> list[Path]:
    """自底向上删除空目录，默认跳过隐藏目录和 ignore 目录

    on_dir(处理的源目录, 已删除个数)：每清完一个源目录回调一次，用于进度显示。
    """
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
        if on_dir is not None:
            on_dir(d, len(removed))
    return removed


# ============================================================
# 去重
# ============================================================


def _sha256(path: Path) -> str:
    """算文件内容哈希，分块读取，避免大文件吃内存"""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dedupe_block(block: Block, root: Path, dry_run: bool,
                 ignores: Iterable[Path] = (),
                 include_hidden: bool = False,
                 dest_root: Path | None = None,
                 on_hash: Callable[[int, int], None] | None = None,
                 on_group: Callable[[int, int, int], None] | None = None,
                 ) -> tuple[int, int, list[str]]:
    """对一个块做去重，返回 (重复组数, 移动文件数, undo 记录)

    判定标准是「同名 + 同大小 + 同哈希」，三者全中才算重复，
    避免把同名不同版本的书写成重复。保留 keep 目录下的那份，
    keep 没写或都不在时按路径排序保留第一个。

    on_hash(已读字节, 总字节)：哈希阶段的进度，去重最慢的就是读盘。
    on_group(已完成组, 总组数, 已移动)：搬文件阶段的进度。
    """
    keep: list[Path] = []
    for a in block.actions:
        if a.name == "dedupe":
            keep.extend(to_real(v, root) for v in a.all("keep"))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = dest_root or (root / "_重复文件" / stamp)
    # 已经搬走的重复文件不再参与比较，否则重复目录会越滚越大
    files = scan(block.dirs, [base], ignores, include_hidden)
    if not files:
        return 0, 0, []

    # 先按「同名 + 同大小」粗筛，只有这些才值得读全文件算哈希
    buckets: dict[tuple[str, int], list[Path]] = {}
    for f in files:
        buckets.setdefault((f.path.name.lower(), f.stat.st_size), []).append(f.path)

    total_bytes = 0
    for paths in buckets.values():
        if len(paths) >= 2:
            total_bytes += sum(p.stat().st_size for p in paths if p.exists())

    hashed_bytes = 0
    same_groups: list[list[Path]] = []
    for key, paths in buckets.items():
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for p in paths:
            try:
                h = _sha256(p)
                size = p.stat().st_size
            except OSError as exc:
                log.warning("读取失败 %s：%s", p, exc)
                continue
            by_hash.setdefault(h, []).append(p)
            hashed_bytes += size
            if on_hash is not None:
                on_hash(hashed_bytes, max(total_bytes, 1))
        same_groups.extend(same for same in by_hash.values() if len(same) >= 2)

    dup_groups = 0
    moved = 0
    records: list[str] = []
    planned: set[str] = set()
    for same in same_groups:
        same.sort(key=lambda p: (0 if _under(p, keep) else 1, str(p)))
        for p in same[1:]:
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = Path(p.name)
            dst = base / rel
            n = 1
            while str(dst) in planned:
                dst = base / rel.with_name(f"{rel.stem} ({n}){rel.suffix}")
                n += 1
            planned.add(str(dst))
            if not dry_run:
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(dst))
                except OSError as exc:
                    log.error("移动失败 %s：%s", p, exc)
                    continue
            records.append(f"move\t{p}\t{dst}")
            moved += 1
        dup_groups += 1
        if on_group is not None:
            on_group(dup_groups, len(same_groups), moved)
    return dup_groups, len(records), records


# ============================================================
# 进度显示
# ============================================================

BAR_WIDTH = 20
# 压缩留下的临时文件后缀，异常退出后要清掉
TMP_MARK = ".tidy-tmp"


def fmt_duration(sec: float) -> str:
    """耗时：0.8s / 12.4s / 2m31s / 1h05m"""
    if sec < 60:
        return f"{sec:.1f}s"
    total = int(round(sec))
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def fmt_bar(fraction: float, width: int = BAR_WIDTH) -> str:
    """进度条：实心 █，空白用空格，不依赖终端配色"""
    f = min(max(fraction, 0.0), 1.0)
    filled = int(round(f * width))
    if f > 0 and filled == 0:
        filled = 1          # 只要动过就别显示成全空
    return "█" * filled + " " * (width - filled)


class Progress:
    """单个规则块的进度条：一个块一条，块跑完就收尾，块之间互不影响。

    终端里按 interval 秒节流重绘（不刷屏）；非终端（定时任务 / 重定向）
    只在收尾时打一行终态，避免日志被刷新行淹没。

    line 布局（移动/重命名/去重/清理）：
        15:02:52  移动  [████████████████████] 100%  5/5 组  /DCIM/Screenshots  142/142 个  0.8s

    三行布局（压缩，当前文件单独一行）：
        15:26:12  压缩进度 [█████████████       ]  75%
                  /path/7633.mp4  (64.3 MB)  省 62.1 MB   剩 6m
                  视频  9/12    失败 0
    """

    def __init__(self, label: str, total: int, unit: str = "个", *,
                 layout: str = "line", stream=None, interval: float = 1.0,
                 force: bool | None = None, group: str = "", groups: int = 0,
                 human: bool = False, current: str = "",
                 indent: str = " " * 10):
        self.label = label
        self.total = max(int(total), 0)
        self.unit = unit
        self.layout = layout
        self.stream = stream if stream is not None else sys.stdout
        self.interval = interval
        self.group = group
        self.groups = groups
        self.group_done = 0
        self.human = human
        self.indent = indent
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.current = current
        self.extra = ""
        self.started = time.monotonic()
        self._last_draw = 0.0
        self._drawn = 0
        if force is None:
            force = bool(getattr(self.stream, "isatty", lambda: False)())
        self.enabled = force

    # ---------- 内部 ----------
    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def _fraction(self) -> float:
        if self.total <= 0:
            return 1.0
        return min(self.done / self.total, 1.0)

    def _eta(self) -> str:
        """按已完成部分的平均速度估算剩余时间"""
        if self.done <= 0 or self.done >= self.total:
            return ""
        left = self.total - self.done
        return f"剩 {fmt_duration(self.elapsed / self.done * left)}"

    def eta_text(self) -> str:
        """剩余时间文本，给调用方拼进 extra 用"""
        return self._eta()

    def _counters(self) -> str:
        """完成/总数，human=True 时按 KB/MB/GB 显示"""
        if self.human:
            return f"{_human_size(self.done)}/{_human_size(self.total)}"
        return f"{self.done}/{self.total}"

    def _render(self) -> list[str]:
        pct = int(self._fraction() * 100)
        bar = fmt_bar(self._fraction())
        ts = datetime.now().strftime("%H:%M:%S")
        head = f"{ts}  {self.label}"
        if self.layout == "block":
            lines = [f"{head} [{bar}] {pct:3d}%"]
            detail = self.current
            if self.extra:
                detail = f"{detail}  {self.extra}" if detail else self.extra
            lines.append(self.indent + detail)
            tail = f"{self.group}  {self.done}/{self.total}" if self.group \
                else f"{self.done}/{self.total} {self.unit}"
            if self.failed:
                tail += f"    失败 {self.failed}"
            if self.skipped:
                tail += f"    跳过 {self.skipped}"
            lines.append(self.indent + tail)
            return lines

        parts = [f"{head}  [{bar}] {pct:3d}%"]
        if self.groups:
            parts.append(f"{self.group_done}/{self.groups} 组")
        if self.current:
            parts.append(self.current)
        parts.append(f"{self._counters()} {self.unit}")
        if self.failed:
            parts.append(f"失败 {self.failed}")
        if self.extra:
            parts.append(self.extra)
        parts.append(fmt_duration(self.elapsed))
        return ["  ".join(parts)]

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            self.enabled = False

    def _draw(self) -> None:
        if not self.enabled:
            return
        lines = self._render()
        out = ""
        if self._drawn:
            out += "\r"
            if self._drawn > 1:
                out += f"\x1b[{self._drawn - 1}A"
            out += "\x1b[J"
        out += "\n".join(lines)
        self._write(out)
        self._drawn = len(lines)

    # ---------- 对外 ----------
    def set_groups(self, total: int, done: int = 0) -> None:
        self.groups, self.group_done = int(total), int(done)

    def advance_group(self) -> None:
        if self.group_done < self.groups:
            self.group_done += 1

    def update(self, *, done: int | None = None, failed: int | None = None,
               skipped: int | None = None, current: str | None = None,
               extra: str | None = None, force: bool = False) -> None:
        if done is not None:
            self.done = done
        if failed is not None:
            self.failed = failed
        if skipped is not None:
            self.skipped = skipped
        if current is not None:
            self.current = current
        if extra is not None:
            self.extra = extra
        now = time.monotonic()
        if not force and now - self._last_draw < self.interval:
            return
        self._last_draw = now
        self._draw()

    def finish(self, extra: str | None = None) -> None:
        """收尾：补上剩余时间，画最后一次"""
        if extra is not None:
            self.extra = extra
        if not self.enabled:
            # 非终端不留刷新行，但压缩的三行布局信息（当前文件/省/计数）要保住
            if self.layout == "block":
                self._write("\n".join(self._render()) + "\n")
                return
            # 其它块只留一行终态，方便看日志
            pct = int(self._fraction() * 100)
            bits = [f"{self.label}", f"[{fmt_bar(self._fraction())}]",
                    f"{pct:3d}%"]
            if self.groups:
                bits.append(f"{self.group_done}/{self.groups} 组")
            if self.current:
                bits.append(self.current)
            bits.append(f"{self._counters()} {self.unit}")
            if self.failed:
                bits.append(f"失败 {self.failed}")
            if self.extra:
                bits.append(self.extra)
            bits.append(fmt_duration(self.elapsed))
            self._write("  ".join(bits) + "\n")
            return
        self._draw()
        self._write("\n")
        self._drawn = 0


class CompressIndex:
    """压缩过的文件清单，存在被压缩目录下的隐藏文件 .tidy-compressed 里。

    一行一条 JSON：{"p": 相对路径, "s": 压缩后大小, "m": 压缩后修改时间}
    - 判断依据是「大小 + mtime 都没变」，所以文件被换掉、重新下载过会自动重新压缩
    - 只记 ok 和「压完没变小」，失败的会重试
    - 文件头一行记压缩参数的签名，参数改了（比如 bitrate 从 320k 改成 96k）
      旧记录自动作废，不会因为「上次说压过」而永远跳过

    放在目录里而不是 ~/.local/state，是为了跟着文件夹一起移动/备份/删除。
    """

    NAME = ".tidy-compressed"

    def __init__(self, base: Path, signature: str = ""):
        self.base = base
        self.path = base / self.NAME
        self.signature = signature
        self.entries: dict[str, tuple[int, int]] = {}
        self._fh = None
        self._wrote_header = False

    def load(self) -> int:
        """读旧记录，返回有效条数"""
        if not self.path.exists():
            return 0
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return 0
        old_sig = None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue        # 中断留下的半行
            if "__sig" in rec:
                old_sig = rec["__sig"]
                continue
            try:
                self.entries[str(rec["p"])] = (int(rec["s"]), int(rec["m"]))
            except (KeyError, TypeError, ValueError):
                continue
        if old_sig != self.signature:
            # 压缩参数变过，旧结论不再成立
            self.entries.clear()
            self._wrote_header = True   # 下次写入时重写签名
            return 0
        return len(self.entries)

    def _key(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.base))
        except ValueError:
            return str(path)

    def is_done(self, path: Path) -> bool:
        rec = self.entries.get(self._key(path))
        if rec is None:
            return False
        try:
            st = path.stat()
        except OSError:
            return False
        return rec == (st.st_size, int(st.st_mtime))

    def mark(self, path: Path, result: str) -> None:
        """记一笔；fail 不记，下次还会重试"""
        if result not in ("ok", "skip"):
            return
        try:
            st = path.stat()
        except OSError:
            return
        key = self._key(path)
        self.entries[key] = (st.st_size, int(st.st_mtime))
        if self._fh is None:
            try:
                self.base.mkdir(parents=True, exist_ok=True)
                self._fh = self.path.open("a", encoding="utf-8")
            except OSError as exc:
                log.warning("无法写压缩记录 %s：%s", self.path, exc)
                return
            if not self._wrote_header and self.signature:
                self._fh.write(json.dumps({"__sig": self.signature},
                                          ensure_ascii=False) + "\n")
                self._wrote_header = True
        self._fh.write(json.dumps({"p": key, "s": st.st_size,
                                   "m": int(st.st_mtime)},
                                  ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def compress_signature(action: Action) -> str:
    """压缩参数的指纹：参数一改，目录里的旧记录就作废"""
    params = sorted(f"{p.key}={p.value}" for p in action.params
                    if p.key not in ("limit", "preserve_time"))
    raw = "|".join([action.name] + params)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]


def cleanup_tmp(dirs: Iterable[Path], dry_run: bool = False) -> int:
    """清掉上次中断留下的 .xxx.tidy-tmp.* 临时文件，返回删除个数"""
    removed = 0
    for d in dirs:
        if not d.is_dir():
            continue
        for cur, dirnames, filenames in os.walk(d):
            # 目标目录里的“重复文件”归档也顺手清一下
            dirnames[:] = [n for n in dirnames if not n.startswith(".")]
            for name in filenames:
                if TMP_MARK not in name:
                    continue
                p = Path(cur) / name
                if dry_run:
                    log.info("将清理临时文件 %s", p)
                else:
                    try:
                        p.unlink()
                    except OSError as exc:
                        log.warning("清理临时文件失败 %s：%s", p, exc)
                        continue
                removed += 1
    return removed


# ============================================================
# 续传
# ============================================================

RESUME_VERSION = 1


class Resume:
    """运行状态记录：异常退出后下次接着做，不用从头再来。

    存储是 JSONL：第一行 header，之后每行一条已完成记录。
    追加写、一行一条，进程被 kill 最多丢最后半行，不会把整个文件写坏。
    配置改过了也照样接着做（只影响压缩这种耗时的块，其他动作本来就是幂等的）。
    """

    def __init__(self, state_dir: Path, conf_path: Path, enabled: bool = True,
                 writable: bool = True):
        self.enabled = enabled
        self.writable = writable
        self.conf_path = conf_path
        key = hashlib.md5(str(conf_path).encode("utf-8")).hexdigest()[:12]
        self.dir = state_dir / "resume"
        self.path = self.dir / f"{key}.jsonl"
        self.done: set[str] = set()
        self.started = ""
        self.count = 0
        self.previous = 0
        self._fh = None

    def load(self) -> int:
        """读上次进度，返回上次已完成的文件数"""
        if not self.path.exists():
            return 0
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue        # 中断时留下的半行，跳过
            if "v" in rec:
                self.started = rec.get("started", "")
                continue
            p = rec.get("p")
            if p:
                self.done.add(p)
        self.previous = len(self.done)
        return self.previous

    def is_done(self, path: Path) -> bool:
        return str(path) in self.done

    def mark(self, path: Path, stage: str, result: str, saved: int = 0) -> None:
        if not self.enabled or not self.writable:
            return
        if self._fh is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            is_new = not self.path.exists()
            self._fh = self.path.open("a", encoding="utf-8")
            if is_new:
                self._fh.write(json.dumps({
                    "v": RESUME_VERSION,
                    "conf": str(self.conf_path),
                    "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }, ensure_ascii=False) + "\n")
        rec = {"p": str(path), "s": stage, "r": result}
        if saved:
            rec["b"] = int(saved)
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        self.done.add(str(path))
        self.count += 1

    def close(self, success: bool) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        if success and self.writable:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass

    def summary(self) -> str:
        if not self.previous:
            return ""
        when = f"（{self.started} 启动）" if self.started else ""
        return f"上次未完成的任务{when}，已记录 {self.previous} 个文件"


# ============================================================
# 压缩
# ============================================================


def _human_size(n: int) -> str:
    """字节数转可读单位，小文件别显示成 0.0 MB"""
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


# 视频网站的分辨率档位，单位是「短边像素」
RESOLUTION_ALIAS = {
    "8k": 4320,
    "4k": 2160,
    "2k": 1440,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "576p": 576,
    "480p": 480,
    "360p": 360,
}


def parse_resolution(text: str) -> int:
    """把 1080p / 720P / 4k / 1080 解析成短边像素数，认不出来返回 0"""
    s = (text or "").strip().lower()
    if not s:
        return 0
    if s in RESOLUTION_ALIAS:
        return RESOLUTION_ALIAS[s]
    if s.endswith("p"):
        s = s[:-1]
    return int(s) if s.isdigit() else 0


def video_scale_filter(action: Action) -> str | None:
    """算出视频的缩放滤镜；返回 None 表示完全不改尺寸

    优先级：resolution（视频网站档位）> height（短边数值）> max_edge（长边上限）。
    短边档位只降不升：本来就是 720p 的不会被拉到 1080p，宽高比保持不变。
    三个都不写就不缩放，只靠 crf 降码率——和图片「不写 max_edge 就不缩」一致。
    """
    short = parse_resolution(action.get("resolution"))
    if short <= 0:
        short = parse_resolution(action.get("height"))
    if short > 0:
        # 横屏按高、竖屏按宽，另一边用 -2 自动算并保持偶数
        return (f"scale='if(gt(iw,ih),-2,min(iw,{short}))':"
                f"'if(gt(iw,ih),min(ih,{short}),-2)'")

    edge = parse_resolution(action.get("max_edge"))
    if edge > 0:
        return (f"scale='min({edge},iw)':'min({edge},ih)'"
                f":force_original_aspect_ratio=decrease:force_divisible_by=2")
    return None


def _probe_image(path: Path) -> tuple[str, int, int]:
    """用 ffprobe 取像素格式和宽高，返回 (pix_fmt, width, height)

    拿不到就返回 ("", 0, 0)，调用方按「无透明、尺寸未知」处理。
    """
    fp = shutil.which("ffprobe")
    if fp is None:
        return "", 0, 0
    try:
        out = subprocess.run(
            [fp, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=pix_fmt,width,height",
             "-of", "default=noprint_wrappers=1", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "", 0, 0
    info: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        info[key.strip()] = value.strip()
    pix = info.get("pix_fmt", "").lower()
    try:
        width = int(info.get("width", 0))
        height = int(info.get("height", 0))
    except ValueError:
        width = height = 0
    return pix, width, height


def _has_alpha(pix_fmt: str) -> bool:
    """像素格式带不带 alpha 通道（rgba / ya8 / yuva420p 这类）"""
    return "a" in (pix_fmt or "").lower()


def compress_file(path: Path, action: Action, dry_run: bool = False) -> tuple[str, str, int]:
    """按 compress-image / compress-music / compress-video 压缩单个文件并原地替换

    - 默认只降清晰度、不动尺寸；写了 max_edge 才等比缩小
    - 压缩后体积没变小就保留原文件，不算失败
    - preserve_time: true（默认）时把原文件的访问 / 修改时间写回新文件
      注意：Android / Linux 上「创建时间」是只读的，改不了，能保留的是
      修改时间和访问时间
    - 返回 (状态, 说明, 省下的字节数)：ok 已替换 / skip 无需或不能压 / fail 出错
    """
    ff = shutil.which("ffmpeg")
    if ff is None:
        return "fail", "找不到 ffmpeg（pkg install ffmpeg）", 0

    preserve = action.get("preserve_time", "true").strip().lower() != "false"
    st = path.stat()
    before = st.st_size
    ext = path.suffix.lower()

    if action.name == "compress-image":
        quality = action.get("quality", "4").strip() or "4"
        max_edge = action.get("max_edge", "").strip()
        flatten = action.get("flatten", "true").strip().lower() != "false"
        tmp = path.with_name(f".{path.stem}{TMP_MARK}{ext}")
        target = path
        timeout = 600

        if ext in (".jpg", ".jpeg"):
            cmd = [ff, "-y", "-loglevel", "error", "-i", str(path)]
            if max_edge and max_edge != "0":
                cmd += ["-vf",
                        f"scale='min({max_edge},iw)':'min({max_edge},ih)'"
                        f":force_original_aspect_ratio=decrease"]
            cmd += ["-q:v", quality, str(tmp)]
        elif ext == ".png":
            pix, width, height = _probe_image(path)
            if flatten and _has_alpha(pix) and width and height:
                # 带透明通道的 png：垫一层白底再压成 jpg，
                # 直接丢弃 alpha 会让透明区域变成黑色
                tmp = path.with_name(f".{path.stem}{TMP_MARK}.jpg")
                target = path.with_suffix(".jpg")
                filters = ["[1:v][0:v]overlay=format=auto"]
                if max_edge and max_edge != "0":
                    filters.append(
                        f"scale='min({max_edge},iw)':'min({max_edge},ih)'"
                        f":force_original_aspect_ratio=decrease")
                cmd = [ff, "-y", "-loglevel", "error", "-i", str(path),
                       "-f", "lavfi", "-i", f"color=c=white:s={width}x{height}",
                       "-filter_complex", ",".join(filters),
                       # 白底源是无限帧，不锁一帧会被当成序列输出而报错
                       "-frames:v", "1", "-q:v", quality, str(tmp)]
            else:
                # 不透明的 png 是无损格式，没有质量刻度，靠 zlib 级别压；
                # 6 比 9 快 2-3 倍，体积差 1-2%
                cmd = [ff, "-y", "-loglevel", "error", "-i", str(path)]
                if max_edge and max_edge != "0":
                    cmd += ["-vf",
                            f"scale='min({max_edge},iw)':'min({max_edge},ih)'"
                            f":force_original_aspect_ratio=decrease"]
                cmd += ["-compression_level", "6", str(tmp)]
        else:
            # gif / webp 先不碰：动图转码容易把动画弄坏
            return "skip", f"不支持的图片格式：{ext or '无扩展名'}", 0
    elif action.name == "compress-video":
        video_exts = (".mp4", ".mkv", ".mov", ".avi", ".webm",
                      ".ts", ".m4v", ".flv", ".wmv", ".3gp")
        if ext not in video_exts:
            return "skip", f"不支持的视频格式：{ext or '无扩展名'}", 0
        crf = action.get("crf", "23").strip() or "23"
        preset = action.get("preset", "veryfast").strip() or "veryfast"
        tmp = path.with_name(f".{path.stem}{TMP_MARK}.mp4")
        vf = video_scale_filter(action)
        cmd = [ff, "-y", "-loglevel", "error", "-i", str(path)]
        if vf:
            cmd += ["-vf", vf]
        cmd += [
            "-c:v", "libx264", "-crf", crf, "-preset", preset,
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(tmp),
        ]
        target = path if ext == ".mp4" else path.with_suffix(".mp4")
        timeout = 3600  # 视频慢，给足一小时
    else:
        # 音乐块和图片/视频一样要有白名单，否则 .txt / .jpg 封面也会被塞给 ffmpeg
        audio_exts = (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg",
                      ".opus", ".wma", ".ape", ".aif", ".aiff", ".alac",
                      ".m4b", ".mp2", ".mpga", ".oga")
        if ext not in audio_exts:
            return "skip", f"不是音频文件：{ext or '无扩展名'}", 0
        fmt = (action.get("format", "mp3") or "mp3").strip().lower()
        bitrate = (action.get("bitrate", "320k") or "320k").strip()
        codec = {"mp3": "libmp3lame", "aac": "aac", "opus": "libopus"}.get(fmt)
        if codec is None:
            return "skip", f"不支持的音频格式：{fmt}", 0
        tmp = path.with_name(f".{path.stem}{TMP_MARK}.{fmt}")
        cmd = [ff, "-y", "-loglevel", "error", "-i", str(path),
               "-c:a", codec, "-b:a", bitrate, str(tmp)]
        target = path if ext == f".{fmt}" else path.with_suffix(f".{fmt}")
        timeout = 600

    if dry_run:
        return "ok", f"将压缩（当前 {_human_size(before)}）", 0

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        tmp.unlink(missing_ok=True)
        return "fail", str(exc), 0

    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = lines[-1][:200] if lines else f"ffmpeg 退出码 {proc.returncode}"
        return "fail", msg, 0

    try:
        after = tmp.stat().st_size
    except OSError:
        return "fail", "临时文件不见了", 0

    if after >= before:
        tmp.unlink(missing_ok=True)
        return "skip", f"压完没变小，保留原文件（{_human_size(before)}）", 0

    try:
        tmp.replace(target)
        # 格式变了（如 wav -> mp3）时，原来的文件要删掉，
        # 顺序放在 replace 之后，万一替换失败原文件还在
        if target != path:
            path.unlink(missing_ok=True)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return "fail", str(exc), 0

    if preserve:
        # 创建时间（birthtime）在 Android / Linux 上改不了，
        # 能还原的是修改时间和访问时间
        try:
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError as exc:
            log.warning("还原时间戳失败 %s：%s", target, exc)

    return "ok", (f"{_human_size(before)} -> {_human_size(after)}，"
                  f"省 {_human_size(before - after)}"), before - after


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
