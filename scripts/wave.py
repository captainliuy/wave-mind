#!/usr/bin/env python3
"""
wave.py — RTL 波形 VCD 提取工具（面向大模型分析）
=========================================================
将 VCD 仿真波形转换为适合大模型阅读的文本格式。

依赖要求：
  Python ≥ 3.8，仅使用标准库，无需 pip install
  模块：re, sys, fnmatch, argparse, textwrap, pathlib, dataclasses, typing, json

子命令：
  list      列出 VCD 中所有信号
  peek      查询某一时刻的信号值（单时刻快照）
  dump      输出波形表格（时间 × 信号值）
  trace     列出信号跳变事件（仅列出有变化的时刻）
  find      按条件搜索满足条件的时间戳
  explain   解释信号翻转的上下文（相关信号变化）
  summary   信号统计报告（含异常检测）
  context   生成完整 LLM 分析上下文（推荐入口）

缓存机制：
  自动在 VCD 同目录生成 .wdb 索引文件
  索引不存在或过期时自动重建（类似 Makefile 规则）

快速上手：
  python wave.py list     sim.vcd
  python wave.py peek     sim.vcd --time 1250 --signals clk,rst_n,valid,data
  python wave.py dump     sim.vcd --signals clk,rst_n,valid,data --start 0 --end 1000
  python wave.py trace    sim.vcd --signals valid,ready
  python wave.py find     sim.vcd --when "valid==1 and ready==1"
  python wave.py explain  sim.vcd --signal ack --at 1310 --context 100
  python wave.py summary  sim.vcd --signals "*"
  python wave.py context  sim.vcd --signals valid,data,ack --start 500 --end 2000
"""

import re
import sys
import fnmatch
import argparse
import textwrap
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# 导入 SQLite 索引模块
from wave_db import WaveDB


# ══════════════════════════════════════════════════════════════════════════════
# VCD 解析器
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Signal:
    id_code: str
    name: str
    width: int
    var_type: str
    scope: str = ""
    changes: list = field(default_factory=list)  # [(time, value_str), ...]

    @property
    def full_name(self) -> str:
        return f"{self.scope}.{self.name}" if self.scope else self.name

    def value_at(self, time: int) -> Optional[str]:
        """二分搜索：返回 time 时刻的值。"""
        lo, hi, result = 0, len(self.changes) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.changes[mid][0] <= time:
                result = self.changes[mid][1]
                lo = mid + 1
            else:
                hi = mid - 1
        return result

    def transitions_in(self, start: int, end: int) -> list:
        """返回 [start, end] 内的所有跳变 (time, prev, curr)。"""
        out = []
        prev = self.value_at(start - 1) if start > 0 else None
        for t, v in self.changes:
            if t < start:
                prev = v
                continue
            if t > end:
                break
            if v != prev:
                out.append((t, prev, v))
            prev = v
        return out

    def toggle_count(self, start=0, end=None) -> int:
        end = end if end is not None else float("inf")
        prev = None
        count = 0
        for t, v in self.changes:
            if t > end:
                break
            if t >= start and v != prev:
                count += 1
            prev = v
        return max(0, count - 1)  # 首次赋值不算跳变

    def detect_glitches(self, min_width: int = 1, start=0, end=None) -> list:
        """检测脉宽 < min_width 的毛刺。返回 [(time, pulse_width, value)]。"""
        end = end if end is not None else float("inf")
        glitches = []
        ch = [(t, v) for t, v in self.changes if start <= t <= end]
        for i in range(len(ch) - 2):
            t0, v0 = ch[i]
            t1, v1 = ch[i + 1]
            t2, v2 = ch[i + 2]
            if v1 != v0 and v2 == v0 and (t2 - t1) < min_width:
                glitches.append((t1, t2 - t1, v1))
        return glitches


class VCDParser:
    """
    VCD 解析器，支持 SQLite 索引缓存（类似 Makefile 规则）。

    自动缓存机制：
      - 索引不存在 → 解析 VCD，生成 .wdb
      - 索引过期（mtime < VCD） → 重建索引
      - 索引有效 → 从 SQLite 加载（跳过解析）
    """

    def __init__(self, path: str):
        self.path = path
        self.timescale = "1ns"
        self.end_time = 0
        self.signals: dict[str, Signal] = {}    # id_code → Signal
        self.by_name: dict[str, Signal] = {}    # full_name → Signal
        self._db: Optional[WaveDB] = None
        self.from_cache = False  # 标记数据来源

        self._db = WaveDB(path)
        if not self._db.needs_rebuild():
            # 索引有效，从 SQLite 加载
            if self._db.load():
                self._load_from_db()
                self.from_cache = True
                print(f"[wave] 从缓存加载 {path} (.wdb)", file=sys.stderr)
                return

        # 无有效索引，解析 VCD 文件并生成索引
        print(f"[wave] 正在解析 {path} ...", file=sys.stderr)
        self._parse()
        self._db.build_from_vcd(self)

    def _load_from_db(self) -> None:
        """从 SQLite 索引加载信号数据"""
        meta = self._db.get_meta()
        self.timescale = meta.get("timescale", "1ns")
        self.end_time = int(meta.get("end_time", 0))

        # 加载信号信息
        sig_info = self._db.get_signals()
        for id_code, info in sig_info.items():
            sig = Signal(
                id_code=id_code,
                name=info["name"],
                width=info["width"],
                var_type=info["var_type"],
                scope=info["scope"],
            )
            # 从数据库加载跳变数据
            sig.changes = self._db.query_all_transitions(id_code)
            self.signals[id_code] = sig
            self.by_name[sig.full_name] = sig

    def _parse(self) -> None:
        with open(self.path, "r", errors="replace") as f:
            text = f.read()

        # ── 解析 header ─────────────────────────
        hdr_end = text.find("$enddefinitions")
        hdr = text[: hdr_end if hdr_end != -1 else len(text)]
        scope_stack: list[str] = []
        tokens = hdr.split()
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t == "$timescale":
                ts = []
                i += 1
                while i < len(tokens) and tokens[i] != "$end":
                    ts.append(tokens[i]); i += 1
                self.timescale = " ".join(ts)
            elif t == "$scope":
                i += 2  # skip type
                scope_stack.append(tokens[i]); i += 1  # skip $end
            elif t == "$upscope":
                if scope_stack: scope_stack.pop()
                i += 1  # skip $end
            elif t == "$var":
                var_type, width, id_code, name = tokens[i+1], int(tokens[i+2]), tokens[i+3], tokens[i+4]
                sig = Signal(id_code, name, width, var_type, ".".join(scope_stack))
                self.signals[id_code] = sig
                self.by_name[sig.full_name] = sig
                i += 5
                while i < len(tokens) and tokens[i] != "$end": i += 1
            i += 1

        # ── 解析值变化 ───────────────────────────
        body = text[hdr_end:] if hdr_end != -1 else ""
        cur_time = 0
        for tok in body.split():
            if tok.startswith("#"):
                cur_time = int(tok[1:])
                self.end_time = max(self.end_time, cur_time)
            elif tok[0] in ("b", "B"):
                self._pending_vec = tok[1:]
            elif tok[0] in ("0", "1", "x", "X", "z", "Z") and len(tok) > 1:
                sig = self.signals.get(tok[1:])
                if sig: sig.changes.append((cur_time, tok[0].lower()))
            elif hasattr(self, "_pending_vec"):
                sig = self.signals.get(tok)
                if sig: sig.changes.append((cur_time, self._pending_vec))
                del self._pending_vec

    def select(self, pattern: str) -> list[Signal]:
        """
        按模式选择信号。支持：
          *          → 全部
          clk,rst_n  → 精确名称（逗号分隔）
          data*      → 通配符
          /addr/     → 正则
        """
        all_sigs = list(self.signals.values())
        if pattern.strip() == "*":
            return all_sigs

        selected, seen = [], set()

        def _add(s: Signal):
            if s.id_code not in seen:
                selected.append(s); seen.add(s.id_code)

        for pat in [p.strip() for p in pattern.split(",")]:
            if pat.startswith("/") and pat.endswith("/"):
                rx = re.compile(pat[1:-1])
                for s in all_sigs:
                    if rx.search(s.full_name): _add(s)
            elif "*" in pat or "?" in pat:
                for s in all_sigs:
                    if fnmatch.fnmatch(s.name, pat) or fnmatch.fnmatch(s.full_name, pat):
                        _add(s)
            else:
                # 精确 → 短名 → 包含
                for s in all_sigs:
                    if pat in (s.name, s.full_name): _add(s); break
                else:
                    for s in all_sigs:
                        if pat.lower() in s.full_name.lower(): _add(s)

        return selected


# ══════════════════════════════════════════════════════════════════════════════
# JSON 输出支持
# ══════════════════════════════════════════════════════════════════════════════

import json

def output_json(data: dict, use_json: bool):
    """根据 --format 参数选择输出格式。"""
    if use_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# 格式化工具
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_val(v: Optional[str], width: int) -> str:
    """将二进制字符串格式化为十六进制（多位信号）或 0/1/x/z（单位）。"""
    if v is None: return "?"
    if width == 1: return v
    if re.fullmatch(r"[01]+", v):
        hex_w = (width + 3) // 4
        return f"{int(v, 2):0{hex_w}X}h"
    return v  # 含 x/z 原样输出


def _timescale_unit(ts: str) -> str:
    m = re.search(r"(f|p|n|u|m)?s", ts)
    return m.group(0) if m else "?"


def _sample_times(signals: list[Signal], start: int, end: int,
                  max_cols: int = 80) -> list[int]:
    """收集时间点并降采样到 max_cols 列。"""
    times = sorted({t for s in signals for t, _ in s.changes if start <= t <= end})
    if len(times) > max_cols:
        step = max(1, len(times) // max_cols)
        times = times[::step]
    return times


# ══════════════════════════════════════════════════════════════════════════════
# 子命令实现
# ══════════════════════════════════════════════════════════════════════════════

def cmd_list(vcd: VCDParser, args):
    """列出所有信号，支持按名称过滤和按跳变次数排序。"""
    sigs = vcd.select(args.filter) if args.filter else list(vcd.signals.values())
    if args.sort == "toggle":
        sigs.sort(key=lambda s: s.toggle_count(), reverse=True)
    else:
        sigs.sort(key=lambda s: s.full_name)

    unit = _timescale_unit(vcd.timescale)

    if output_json({
        "file": vcd.path,
        "timescale": vcd.timescale,
        "end_time": vcd.end_time,
        "signals": [
            {"name": s.full_name, "width": s.width, "type": s.var_type, "toggles": s.toggle_count()}
            for s in sigs
        ],
        "total": len(sigs)
    }, args.format == "json"):
        return

    print(f"VCD: {vcd.path}  |  timescale: {vcd.timescale}  |  "
          f"end_time: {vcd.end_time}{unit}\n")
    print(f"{'#':<5} {'Full Name':<50} {'W':>4}  {'Type':<8}  Toggles")
    print("─" * 78)
    for i, s in enumerate(sigs, 1):
        print(f"{i:<5} {s.full_name:<50} {s.width:>4}b  {s.var_type:<8}  {s.toggle_count()}")
    print(f"\n共 {len(sigs)} 个信号（VCD 合计 {len(vcd.signals)} 个）")


def cmd_peek(vcd: VCDParser, args):
    """
    查询某一时刻的信号值（单时刻快照）。
    适合快速定位问题时刻的信号状态。
    """
    sigs = vcd.select(args.signals)
    if not sigs: _no_match(); return

    time = args.time
    unit = _timescale_unit(vcd.timescale)

    signal_values = {}
    for s in sigs:
        v = s.value_at(time)
        signal_values[s.full_name] = _fmt_val(v, s.width) if v else "?"

    if output_json({
        "time": time,
        "unit": unit,
        "signals": signal_values
    }, args.format == "json"):
        return

    print(f"时刻 t = {time} {unit}\n")
    print(f"{'Signal':<40}  {'Value':>10}")
    print("─" * 52)
    for name, val in signal_values.items():
        print(f"{name:<40}  {val:>10}")


def cmd_dump(vcd: VCDParser, args):
    """
    输出波形表格。
    行 = 时间采样点，列 = 信号。多位信号显示十六进制。
    """
    sigs = vcd.select(args.signals)
    if not sigs: _no_match(); return

    start, end = args.start, args.end if args.end is not None else vcd.end_time
    times = _sample_times(sigs, start, end, args.max_cols)
    if not times:
        print("(该时间段内无跳变)"); return

    unit = _timescale_unit(vcd.timescale)
    col_w = [max(len(s.name), (s.width + 3) // 4 + 1, 4) for s in sigs]

    # 表头
    header = f"{'Time':>12} {unit} | " + " | ".join(
        s.name[:col_w[i]].ljust(col_w[i]) for i, s in enumerate(sigs))
    print(header)
    print("─" * len(header))

    for t in times:
        vals = [_fmt_val(s.value_at(t), s.width).ljust(col_w[i])
                for i, s in enumerate(sigs)]
        print(f"{t:>13} | " + " | ".join(vals))


def cmd_trace(vcd: VCDParser, args):
    """
    列出跳变事件（仅输出有变化的行）。
    比 dump 更适合分析协议握手、边沿触发逻辑。
    """
    sigs = vcd.select(args.signals)
    if not sigs: _no_match(); return

    start, end = args.start, args.end if args.end is not None else vcd.end_time
    unit = _timescale_unit(vcd.timescale)

    # 收集所有跳变
    events: list[tuple[int, str, str, str]] = []  # (time, sig_name, prev, curr)
    for s in sigs:
        for t, pv, cv in s.transitions_in(start, end):
            events.append((t, s.full_name, pv if pv else "?", cv))
    events.sort(key=lambda e: e[0])

    if not events:
        print(f"(该时间段内无跳变)"); return

    print(f"{'Time':>12} {unit}  {'Signal':<40}  {'Prev':>8}  →  Curr")
    print("─" * 72)
    for t, name, pv, cv in events:
        pv_s = _fmt_val(pv, 1)
        cv_s = _fmt_val(cv, 1)
        # 多位信号：尝试十六进制转换
        sig = vcd.by_name.get(name)
        if sig and sig.width > 1:
            pv_s = _fmt_val(pv, sig.width)
            cv_s = _fmt_val(cv, sig.width)
        print(f"{t:>13}   {name:<40}  {pv_s:>8}  →  {cv_s}")


def cmd_find(vcd: VCDParser, args):
    """
    按条件搜索满足条件的时间戳。
    --when 表达式支持：
      信号名 == 值     例：valid == 1
      信号名 != 值     例：state != 0
      逻辑组合         例：valid == 1 and ready == 1
      rising(sig)      信号上升沿
      falling(sig)     信号下降沿
    """
    sigs = {s.name: s for s in vcd.signals.values()}
    sigs.update({s.full_name: s for s in vcd.signals.values()})

    start = args.start
    end = args.end if args.end is not None else vcd.end_time
    unit = _timescale_unit(vcd.timescale)
    condition = args.when

    # 收集所有时间点
    all_times = sorted({t for s in vcd.signals.values()
                        for t, _ in s.changes if start <= t <= end})

    # 替换 rising/falling 语法
    def check_rising(sig_name: str, t: int) -> bool:
        s = sigs.get(sig_name)
        if not s: return False
        transitions = s.transitions_in(max(0, t - 1), t)
        return any(pv in ("0", None) and cv == "1" for _, pv, cv in transitions if cv_t == t
                   for cv_t, _, cv in [(tr[0], tr[1], tr[2]) for tr in transitions])

    results = []
    for t in all_times:
        # 构建当前时刻的信号值 dict
        env = {}
        for name, s in sigs.items():
            v = s.value_at(t)
            if v and re.fullmatch(r"[01]+", v):
                env[name.replace(".", "_")] = int(v, 2)
            elif v:
                env[name.replace(".", "_")] = v

        # 替换 rising/falling 为具体值
        expr = condition
        for m in re.finditer(r"rising\((\w+)\)", expr):
            sn = m.group(1)
            s = sigs.get(sn)
            matched = False
            if s:
                for tr_t, pv, cv in s.transitions_in(t, t):
                    if cv == "1" and pv in ("0", None): matched = True
            expr = expr.replace(m.group(0), "True" if matched else "False")
        for m in re.finditer(r"falling\((\w+)\)", expr):
            sn = m.group(1)
            s = sigs.get(sn)
            matched = False
            if s:
                for tr_t, pv, cv in s.transitions_in(t, t):
                    if cv == "0" and pv == "1": matched = True
            expr = expr.replace(m.group(0), "True" if matched else "False")

        try:
            # 将信号名中的 . 替换为 _ 以适应 Python 标识符
            safe_expr = re.sub(r'\b([a-zA-Z_]\w*(?:\.\w+)+)\b',
                               lambda m: m.group(0).replace(".", "_"), expr)
            if eval(safe_expr, {"__builtins__": {}}, env):
                results.append(t)
        except Exception:
            pass

    if not results:
        print(f"未找到满足条件 [{condition}] 的时间点")
        return

    print(f"条件: {condition}")
    print(f"找到 {len(results)} 个匹配时间点（单位: {unit}）：\n")
    # 合并连续时间段
    groups = []
    cur_start = results[0]
    cur_end = results[0]
    for t in results[1:]:
        if t - cur_end <= 1:
            cur_end = t
        else:
            groups.append((cur_start, cur_end))
            cur_start = cur_end = t
    groups.append((cur_start, cur_end))

    for gs, ge in groups[:args.limit]:
        if gs == ge:
            print(f"  t = {gs}")
        else:
            print(f"  t = {gs} ~ {ge}  (持续 {ge - gs})")
    if len(groups) > args.limit:
        print(f"  ... 共 {len(groups)} 段，使用 --limit 显示更多")


def cmd_explain(vcd: VCDParser, args):
    """
    解释信号翻转的上下文。
    查找目标信号翻转时刻前后 --context 时间窗口内所有相关信号的变化，
    帮助定位翻转原因。
    """
    # 查找目标信号
    target_sigs = vcd.select(args.signal)
    if not target_sigs:
        print(f"未找到信号 [{args.signal}]")
        return
    target = target_sigs[0]

    # 查找翻转时刻
    time = args.at
    transitions = target.transitions_in(time, time)
    if not transitions:
        # 尝试在附近查找
        nearby = target.transitions_in(time - 5, time + 5)
        if nearby:
            time = nearby[0][0]
            transitions = nearby
            print(f"(信号在 t={args.at} 无翻转，使用最近的翻转时刻 t={time})")
        else:
            print(f"信号 [{args.signal}] 在 t={time} 附近无翻转事件")
            return

    # 确定上下文窗口
    ctx_window = args.context
    start = max(0, time - ctx_window)
    end = min(vcd.end_time, time + ctx_window)
    unit = _timescale_unit(vcd.timescale)

    # 收集所有信号在该窗口内的变化
    all_sigs = vcd.select("*")
    related_changes = []
    for s in all_sigs:
        for t, pv, cv in s.transitions_in(start, end):
            if t != time or s.full_name != target.full_name:  # 排除目标信号本身
                related_changes.append({
                    "time": t,
                    "signal": s.full_name,
                    "prev": _fmt_val(pv, s.width) if pv else "?",
                    "curr": _fmt_val(cv, s.width)
                })

    # 按时间排序
    related_changes.sort(key=lambda x: x["time"])

    # 分析可能原因
    likely_causes = []
    for ch in related_changes:
        if ch["time"] < time:
            likely_causes.append(f"{ch['signal']} 变为 {ch['curr']} (t={ch['time']})")

    event_desc = f"{target.full_name} 在 t={time} 由 {_fmt_val(transitions[0][1], target.width)} 变为 {_fmt_val(transitions[0][2], target.width)}"

    if output_json({
        "event": event_desc,
        "window": [start, end],
        "unit": unit,
        "likely_causes": likely_causes[:5],
        "related_changes": related_changes
    }, args.format == "json"):
        return

    print(f"事件: {event_desc}")
    print(f"上下文窗口: {start} ~ {end} {unit}\n")

    if likely_causes:
        print("可能原因（翻转前相关信号变化）：")
        for cause in likely_causes[:5]:
            print(f"  • {cause}")

    print(f"\n相关信号变化（按时间排序）：")
    print(f"{'Time':>10}  {'Signal':<40}  {'Prev':>8}  →  {'Curr':>8}")
    print("─" * 70)
    for ch in related_changes[:30]:
        print(f"{ch['time']:>10}  {ch['signal']:<40}  {ch['prev']:>8}  →  {ch['curr']:>8}")
    if len(related_changes) > 30:
        print(f"... 共 {len(related_changes)} 个变化事件")


def cmd_summary(vcd: VCDParser, args):
    """
    输出信号统计摘要 + 异常检测报告。
    异常类型：stuck（静止不动）、glitch（毛刺）、X 传播、Z 高阻。
    """
    sigs = vcd.select(args.signals)
    if not sigs: _no_match(); return

    start = args.start
    end = args.end if args.end is not None else vcd.end_time
    unit = _timescale_unit(vcd.timescale)

    print(f"{'═'*70}")
    print(f"  波形摘要  |  时间范围: {start}~{end} {unit}  |  {vcd.timescale}")
    print(f"{'═'*70}\n")

    print(f"  {'信号':<44} {'宽度':>4}  {'跳变数':>6}  {'初值':>6}  {'末值':>6}")
    print(f"  {'─'*68}")

    anomalies = []
    for s in sigs:
        toggles = s.toggle_count(start, end)
        init_v = _fmt_val(s.value_at(start), s.width)
        last_v = _fmt_val(s.value_at(end), s.width)
        flag = ""

        # stuck
        if toggles == 0:
            flag = "⚠ STUCK"
            anomalies.append(f"  [STUCK]  {s.full_name} 在 [{start},{end}] 内从未跳变，"
                             f"值恒为 {init_v}")
        # glitch
        glitches = s.detect_glitches(min_width=args.glitch_width, start=start, end=end)
        if glitches:
            flag += " ⚡GLITCH"
            for gt, gw, gv in glitches[:3]:
                anomalies.append(f"  [GLITCH] {s.full_name} t={gt} 脉宽={gw} 值={gv}")

        # X/Z 传播
        x_times = [(t, v) for t, v in s.changes if "x" in v.lower() and start <= t <= end]
        z_times = [(t, v) for t, v in s.changes if "z" in v.lower() and start <= t <= end]
        if x_times:
            flag += " ✗X"
            anomalies.append(f"  [X-PROP] {s.full_name} 在 t={x_times[0][0]} 出现 X 值"
                             f"（共 {len(x_times)} 次）")
        if z_times:
            flag += " ⇅Z"
            anomalies.append(f"  [HI-Z]   {s.full_name} 在 t={z_times[0][0]} 出现 Z 值"
                             f"（共 {len(z_times)} 次）")

        print(f"  {s.full_name:<44} {s.width:>4}b  {toggles:>6}  {init_v:>6}  {last_v:>6}  {flag}")

    if anomalies:
        print(f"\n{'─'*70}")
        print("  异常检测报告：\n")
        for a in anomalies:
            print(a)
    else:
        print(f"\n  ✓ 未检测到异常")

    print(f"\n{'─'*70}")
    print(f"  共分析 {len(sigs)} 个信号，发现 {len(anomalies)} 项异常")


def cmd_context(vcd: VCDParser, args):
    """
    生成完整的 LLM 分析上下文（推荐入口）。
    将 summary + dump + trace 组合成结构化文本，
    直接粘贴给模型或通过管道传入。
    """
    sigs = vcd.select(args.signals)
    if not sigs: _no_match(); return

    start = args.start
    end = args.end if args.end is not None else vcd.end_time
    unit = _timescale_unit(vcd.timescale)

    sep = "═" * 64

    print(sep)
    print("  RTL 仿真波形上下文（供大模型分析）")
    print(sep)
    print(f"  文件     : {vcd.path}")
    print(f"  时间尺度 : {vcd.timescale}")
    print(f"  仿真范围 : 0 ~ {vcd.end_time} {unit}")
    print(f"  分析窗口 : {start} ~ {end} {unit}")
    print(f"  信号数量 : {len(sigs)} 个（共 {len(vcd.signals)} 个）")
    print()

    # 1. 信号总览
    print("─── 1. 信号列表 " + "─" * 48)
    for s in sigs:
        print(f"  {s.full_name:<44} {s.width}b  {s.var_type}")
    print()

    # 2. 波形表格
    print("─── 2. 波形表格（采样点） " + "─" * 38)
    args_dump = argparse.Namespace(
        signals=args.signals, start=start, end=end, max_cols=args.max_cols)
    cmd_dump(vcd, args_dump)
    print()

    # 3. 跳变列表
    print("─── 3. 跳变事件 " + "─" * 48)
    args_trace = argparse.Namespace(
        signals=args.signals, start=start, end=end)
    cmd_trace(vcd, args_trace)
    print()

    # 4. 异常检测
    print("─── 4. 异常检测 " + "─" * 48)
    args_sum = argparse.Namespace(
        signals=args.signals, start=start, end=end, glitch_width=2)
    cmd_summary(vcd, args_sum)
    print()

    print(sep)
    if args.question:
        print(f"  分析问题: {args.question}")
        print(sep)


def _no_match():
    print("未找到匹配信号，请用 list 子命令查看可用信号名。", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════════════════════

def _add_time_args(p: argparse.ArgumentParser):
    p.add_argument("--start", type=int, default=0, metavar="T",
                   help="起始时间（VCD 时间单位，默认 0）")
    p.add_argument("--end", type=int, default=None, metavar="T",
                   help="结束时间（默认：仿真结束）")


def _add_signal_arg(p: argparse.ArgumentParser, default="*"):
    p.add_argument("--signals", "-s", default=default, metavar="PAT",
                   help="信号选择模式：名称/通配符/正则（逗号分隔，默认 *）")


def _add_format_arg(p: argparse.ArgumentParser):
    p.add_argument("--format", "-F", choices=["text", "json"], default="text",
                   help="输出格式（text / json，默认 text）")


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="wave",
        description="RTL VCD 波形提取工具 — 面向大模型分析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = root.add_subparsers(dest="cmd", required=True)

    # list
    p_list = sub.add_parser("list", help="列出 VCD 中的所有信号")
    p_list.add_argument("vcd", help="VCD 文件路径")
    p_list.add_argument("--filter", "-f", default=None, metavar="PAT",
                        help="按名称过滤（支持通配符）")
    p_list.add_argument("--sort", choices=["name", "toggle"], default="name",
                        help="排序方式（name / toggle，默认 name）")
    _add_format_arg(p_list)

    # peek
    p_peek = sub.add_parser("peek", help="查询某一时刻的信号值（单时刻快照）")
    p_peek.add_argument("vcd", help="VCD 文件路径")
    p_peek.add_argument("--time", "-t", type=int, required=True, metavar="T",
                        help="查询时刻（VCD 时间单位）")
    _add_signal_arg(p_peek)
    _add_format_arg(p_peek)

    # dump
    p_dump = sub.add_parser("dump", help="输出波形表格（时间 × 信号值）")
    p_dump.add_argument("vcd", help="VCD 文件路径")
    _add_signal_arg(p_dump)
    _add_time_args(p_dump)
    p_dump.add_argument("--max-cols", type=int, default=80, metavar="N",
                        help="最大采样列数（降采样阈值，默认 80）")

    # trace
    p_trace = sub.add_parser("trace", help="列出跳变事件（只输出有变化的时刻）")
    p_trace.add_argument("vcd", help="VCD 文件路径")
    _add_signal_arg(p_trace)
    _add_time_args(p_trace)

    # find
    p_find = sub.add_parser("find", help="搜索满足条件的时间戳")
    p_find.add_argument("vcd", help="VCD 文件路径")
    p_find.add_argument("--when", "-w", required=True, metavar="EXPR",
                        help="条件表达式（如 'valid==1 and ready==1' / 'rising(clk)'）")
    _add_time_args(p_find)
    p_find.add_argument("--limit", type=int, default=20,
                        help="最多显示结果段数（默认 20）")

    # explain
    p_exp = sub.add_parser("explain", help="解释信号翻转的上下文（相关信号变化）")
    p_exp.add_argument("vcd", help="VCD 文件路径")
    p_exp.add_argument("--signal", "-s", required=True, metavar="SIG",
                       help="目标信号名")
    p_exp.add_argument("--at", "-t", type=int, required=True, metavar="T",
                       help="翻转时刻（VCD 时间单位）")
    p_exp.add_argument("--context", "-c", type=int, default=100, metavar="N",
                       help="上下文窗口大小（前后 N 时间单位，默认 100）")
    _add_format_arg(p_exp)

    # summary
    p_sum = sub.add_parser("summary", help="信号统计 + 异常检测报告")
    p_sum.add_argument("vcd", help="VCD 文件路径")
    _add_signal_arg(p_sum)
    _add_time_args(p_sum)
    p_sum.add_argument("--glitch-width", type=int, default=2, metavar="N",
                       help="毛刺判定阈值（脉宽 < N 则视为毛刺，默认 2）")

    # context
    p_ctx = sub.add_parser("context", help="生成完整 LLM 分析上下文（推荐）")
    p_ctx.add_argument("vcd", help="VCD 文件路径")
    _add_signal_arg(p_ctx)
    _add_time_args(p_ctx)
    p_ctx.add_argument("--max-cols", type=int, default=60, metavar="N",
                       help="波形表格最大采样列数（默认 60）")
    p_ctx.add_argument("--question", "-q", default=None, metavar="Q",
                       help="追加分析问题到上下文末尾（便于直接传给模型）")

    return root


def main():
    parser = build_parser()
    args = parser.parse_args()

    vcd_path = Path(args.vcd)
    if not vcd_path.exists():
        print(f"错误：文件不存在 — {args.vcd}", file=sys.stderr)
        sys.exit(1)

    # 加载 VCD（自动判断是否使用缓存）
    # 类似 Makefile 规则：索引不存在或过期时自动重建
    vcd = VCDParser(str(vcd_path))
    print(f"[wave] 共 {len(vcd.signals)} 个信号，"
          f"仿真时长 {vcd.end_time} ({vcd.timescale})\n", file=sys.stderr)

    dispatch = {
        "list":    cmd_list,
        "peek":    cmd_peek,
        "dump":    cmd_dump,
        "trace":   cmd_trace,
        "find":    cmd_find,
        "explain": cmd_explain,
        "summary": cmd_summary,
        "context": cmd_context,
    }
    dispatch[args.cmd](vcd, args)


if __name__ == "__main__":
    main()
