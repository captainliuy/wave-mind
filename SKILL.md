---
name: wave-mind
description: >
  RTL 仿真波形分析技能。当用户需要分析 RTL 仿真波形、调试 VCD 文件、定位时序问题、
  检查协议握手（AXI/Valid-Ready）、追踪 X 传播或 CDC 问题时，触发本技能。
  覆盖场景：VCD 信号提取、波形上下文生成、异常检测、LLM 分析提示词组装。
  只要用户提到 VCD、波形、仿真调试、waveform、FST、WLF、FSDB、信号跳变、
  时序违例、X传播、CDC、AXI、握手协议分析，立即使用本技能。
compatibility:
  tools: bash, python3 (≥3.8, 标准库，无需额外安装)
  environments: Claude Code, Cursor, Codex, 任何支持 bash 的 AI 编程环境
---

# RTL 仿真波形分析 Skill

> ⚠️ **只要涉及 VCD/波形/仿真调试，就必须使用本技能，不得跳过工具调用步骤。**

**格式要求**：`wave.py` 仅支持 VCD 格式。如果用户提供 `.fst`/`.wlf`/`.fsdb`/`.shm` 等文件，
必须先转换为 VCD（见 `references/sim_to_vcd.md`）。

**缓存机制**：首次使用时自动生成 `.wdb` 索引文件（SQLite 格式），后续调用速度提升 4x+。
索引在 VCD 文件更新时自动重建，无需手动管理。

将仿真 VCD 波形转换为大模型可读的结构化文本，辅助 RTL 调试与分析。

> 💡 **不知道用什么命令？先用 `context`**，它把分析所需的所有信息都准备好了。

## 子命令选择指南（必读）

遇到问题时，按此表选择子命令：

| 你的目标 | 推荐子命令 | 说明 |
|---------|-----------|------|
| 不知道有哪些信号 | `list` | 先浏览，再决定分析哪些 |
| 查询某一时刻的信号值 | `peek` | 单时刻快照，快速定位问题时刻 |
| 需要看完整波形表格 | `dump` | 时间×信号矩阵，适合看趋势 |
| 只关心变化时刻 | `trace` | 更紧凑，适合分析协议行为 |
| 搜索特定条件/边沿 | `find` | 找握手失败、X传播起点等 |
| 解释信号翻转原因 | `explain` | 查看翻转时刻前后的相关变化 |
| 检测异常（STUCK/GLITCH/X） | `summary` | 一键扫描问题信号 |
| **传给大模型分析** | `context` | **首选入口**，组合以上所有信息 |

---

## 核心工具

**`scripts/wave.py`** — 唯一入口 CLI，**纯 Python 标准库实现，无需安装任何依赖**。

依赖说明：
- Python ≥ 3.6
- 仅使用标准库模块：`re`, `sys`, `fnmatch`, `argparse`, `textwrap`, `pathlib`, `typing`, `json`
- 无需 `pip install` 任何第三方包

```
用法：python3 wave.py <子命令> <vcd文件> [选项]
```

---

## 子命令速查

### `list` — 浏览信号

```bash
# 列出所有信号
python3 wave.py list sim.vcd

# 按跳变次数降序排列（快速找到活跃信号）
python3 wave.py list sim.vcd --sort toggle

# 过滤名称（通配符）
python3 wave.py list sim.vcd --filter "axi*"
python3 wave.py list sim.vcd --filter "/valid|ready/"   # 正则

# JSON 输出（适合 Agent 解析）
python3 wave.py list sim.vcd --format json
```

---

### `peek` — 单时刻快照

查询某一时刻所有信号的值，适合快速定位问题时刻。

```bash
# 查询 t=1250 时刻的信号值
python3 wave.py peek sim.vcd --time 1250 --signals clk,rst_n,valid,data

# 查询某一时刻所有信号
python3 wave.py peek sim.vcd --time 500 --signals "*"

# JSON 输出
python3 wave.py peek sim.vcd --time 1250 --signals valid,data --format json
```

**输出示例：**
```
时刻 t = 1250 ns

Signal                                      Value
────────────────────────────────────────────────────
tb.dut.clk                                     1
tb.dut.rst_n                                   1
tb.dut.valid                                   0
tb.dut.data                                  A3B2h
```

**JSON 输出示例：**
```json
{
  "time": 1250,
  "unit": "ns",
  "signals": {
    "tb.dut.clk": "1",
    "tb.dut.rst_n": "1",
    "tb.dut.valid": "0",
    "tb.dut.data": "A3B2h"
  }
}
```

---

### `dump` — 波形表格

输出时间 × 信号的矩阵，多位信号显示十六进制。

```bash
# 指定信号，全时间范围
python3 wave.py dump sim.vcd --signals clk,rst_n,valid,data

# 截取时间段（单位与 VCD timescale 一致）
python3 wave.py dump sim.vcd --signals valid,data --start 1000 --end 5000

# 控制采样密度（--max-cols 越大越详细，默认 80 列）
python3 wave.py dump sim.vcd --signals "*" --max-cols 40

# 通配符选择信号组
python3 wave.py dump sim.vcd --signals "axi_*"

# 正则选择
python3 wave.py dump sim.vcd --signals "/^tb\.dut\.(valid|data|ack)$/"
```

**输出示例：**
```
      Time ns | clk  | rst_n | valid | data
─────────────────────────────────────────────
            0 | 0    | 0     | 0     | 0000h
           10 | 1    | 0     | 0     | 0000h
           20 | 0    | 1     | 0     | 0000h
           50 | 1    | 1     | 1     | A3B2h
           60 | 0    | 1     | 1     | A3B2h
```

---

### `trace` — 跳变事件

只输出有变化的时刻，比 `dump` 更适合分析协议行为和边沿事件。

```bash
python3 wave.py trace sim.vcd --signals valid,ready,data

# 指定时间窗口
python3 wave.py trace sim.vcd --signals "axi_*" --start 500 --end 2000
```

**输出示例：**
```
      Time ns  Signal                               Prev  →  Curr
────────────────────────────────────────────────────────────────────
           20  tb.dut.rst_n                            0  →  1
           50  tb.dut.valid                            0  →  1
           50  tb.dut.data                         0000h  →  A3B2h
           60  tb.dut.ready                            0  →  1
          120  tb.dut.valid                            1  →  0
```

---

### `find` — 条件搜索

搜索满足条件的时间点，支持逻辑表达式和边沿检测。

```bash
# 找到握手成功（valid & ready 同时为高）的时刻
python3 wave.py find sim.vcd --when "valid == 1 and ready == 1"

# 找到 data 不为零的时刻
python3 wave.py find sim.vcd --when "data != 0"

# 找到上升沿
python3 wave.py find sim.vcd --when "rising(valid)"

# 找到下降沿
python3 wave.py find sim.vcd --when "falling(rst_n)"

# 时间范围内搜索
python3 wave.py find sim.vcd --when "error == 1" --start 0 --end 10000

# 显示更多结果
python3 wave.py find sim.vcd --when "state != 0" --limit 50
```

**输出示例：**
```
条件: valid == 1 and ready == 1
找到 3 个匹配时间点（单位: ns）：

  t = 60
  t = 120 ~ 130  (持续 10)
  t = 250
```

---

### `explain` — 解释信号翻转原因

分析信号翻转时刻前后相关信号的变化，帮助定位翻转根因。

```bash
# 解释 ack 在 t=1310 翻转的原因
python3 wave.py explain sim.vcd --signal ack --at 1310 --context 100

# 指定更大的上下文窗口
python3 wave.py explain sim.vcd --signal valid --at 500 --context 200

# JSON 输出
python3 wave.py explain sim.vcd --signal ack --at 1310 --format json
```

**输出示例：**
```
事件: tb.dut.ack 在 t=1310 由 0 变为 1
上下文窗口: 1210 ~ 1410 ns

可能原因（翻转前相关信号变化）：
  • tb.dut.req 变为 1 (t=1210)
  • tb.dut.state 变为 WAIT_ACK (t=1240)
  • tb.dut.ready 变为 1 (t=1300)

相关信号变化（按时间排序）：
     Time  Signal                                     Prev  →     Curr
──────────────────────────────────────────────────────────────────────
    1210  tb.dut.req                                    0  →        1
    1240  tb.dut.state                               IDLE  →  WAIT_ACK
    1300  tb.dut.ready                                  0  →        1
    1310  tb.dut.ack                                    0  →        1
```

---

### `summary` — 统计与异常检测

输出每个信号的跳变统计，并自动检测：
- `STUCK` — 信号在分析窗口内从未跳变
- `GLITCH` — 脉宽小于阈值的毛刺
- `X-PROP` — 出现 X 值（未初始化/多驱动）
- `HI-Z` — 出现 Z 值（高阻）

```bash
python3 wave.py summary sim.vcd --signals "*"

# 指定时间窗口
python3 wave.py summary sim.vcd --signals "*" --start 100 --end 5000

# 调整毛刺判定阈值（脉宽 < 3 视为毛刺）
python3 wave.py summary sim.vcd --signals data,valid --glitch-width 3
```

---

### `context` — 完整 LLM 上下文（推荐主入口）

将 list + dump + trace + summary 组合成结构化文本块，直接传给大模型。

```bash
# 基本用法
python3 wave.py context sim.vcd --signals valid,ready,data

# 带时间窗口 + 附加问题
python3 wave.py context sim.vcd \
    --signals "axi_*" \
    --start 1000 --end 5000 \
    --question "为什么 axi_valid 在 reset 释放后延迟了 5 个周期才拉高？"

# 管道传给大模型（Claude Code 环境）
python3 wave.py context sim.vcd --signals valid,data --start 0 --end 2000 \
    | claude --print "请分析这段波形的握手行为"

# 保存到文件供粘贴
python3 wave.py context sim.vcd --signals "*" > wave_context.txt
```

---

## 信号选择语法汇总

| 写法               | 含义                        |
|-------------------|-----------------------------|
| `*`               | 全部信号                    |
| `clk,rst_n,valid` | 逗号分隔的精确名称           |
| `axi_*`           | 通配符（fnmatch）            |
| `*data*`          | 包含 data 的所有信号         |
| `/valid\|ready/`  | 正则表达式（用 `/` 包裹）    |
| `tb.dut.clk`      | 完整层次路径                |

---

## 仿真器 VCD 导出（简明速查）

> 详细命令参见 `references/sim_to_vcd.md`

| 仿真器     | 导出命令                                          | 时间段截取方法        |
|-----------|--------------------------------------------------|--------------------|
| Questa    | `wlfman filter` + `wlf2vcd`                     | `wlfman filter -begin/-end` |
| VCS       | `vpd2vcd dump.vpd sim.vcd`                       | `-s T -e T`        |
| Xcelium   | `simvision -batch -input convert.tcl`            | Tcl 中 `-start/-end` |
| Icarus    | `$dumpfile` + `$dumpon` / `$dumpoff`             | testbench 逻辑控制  |
| Verilator | `VerilatedVcdC::dump()` 条件调用                 | C++ 循环条件控制    |
| GTKWave   | `fst2vcd sim.fst -o sim.vcd`                    | 转后用 `python3 wave.py` |

**通用方法（任意仿真器）**：

```verilog
initial begin
    $dumpfile("sim.vcd");
    $dumpvars(0, tb);   // 0 = 全层次
    $dumpoff;           // 默认不记录
    #START_TIME $dumpon;
    #WINDOW_SIZE $dumpoff;
    $finish;
end
```

---

## 完整工作流示例（必须按序执行）

### 场景：AXI-Stream 握手异常

执行步骤：

1. **【必须】从仿真器导出 VCD**
   ```bash
   # Questa/ModelSim：先用 wlfman filter 裁剪，再转 VCD
   wlfman filter vsim.wlf -begin 0 -end 10000 -o axi.wlf
   wlf2vcd -o axi.vcd axi.wlf
   ```
   跳过此步将无法进行任何波形分析。

2. **浏览信号，找到相关信号名**
   ```bash
   python3 wave.py list axi.vcd --filter "*tvalid*,*tready*,*tdata*"
   ```

3. **搜索握手失败的位置**
   ```bash
   python3 wave.py find axi.vcd --when "tvalid == 1 and tready == 0" --start 0 --end 10000
   ```

4. **【必须】在问题区域提取完整上下文**
   ```bash
   python3 wave.py context axi.vcd \
       --signals tvalid,tready,tdata,tlast \
       --start 3200 --end 3800 \
       --question "tvalid 在 t=3500 后为何持续拉低？是否违反了 AXI-Stream 规范？" \
       > bug_context.txt
   ```
   跳过此步大模型将缺乏足够的波形上下文进行分析。

5. **【必须】传给大模型分析**
   ```bash
   cat bug_context.txt | claude --print
   ```
   这是获取诊断结论的唯一途径。

---

### 场景：复位后 X 传播

执行步骤：

1. **【必须】直接 summary 检测异常**
   ```bash
   python3 wave.py summary sim.vcd --signals "*" --start 0 --end 500
   ```

2. **锁定 X 传播时间点**
   ```bash
   python3 wave.py find sim.vcd --when "rising(rst_n)"
   ```

3. **使用 peek 快速查看复位释放时刻**
   ```bash
   python3 wave.py peek sim.vcd --time 100 --signals rst_n,state,data_out
   ```

4. **使用 explain 解释 X 出现原因**
   ```bash
   python3 wave.py explain sim.vcd --signal data_out --at 105 --context 50
   ```

5. **【必须】在复位释放附近提取上下文**
   ```bash
   python3 wave.py context sim.vcd \
       --signals rst_n,state,data_out,valid \
       --start 95 --end 200 \
       --question "rst_n 释放后 data_out 出现 X，根因是什么？"
   ```
   跳过此步无法确定 X 传播的具体路径和原因。

---

## 在 Claude Code / Codex 中的使用方式

Claude Code 和 Codex 可以直接调用 bash 工具运行这些命令，然后分析输出。

**推荐指令模板（复制给用户）**：

```
分析这个仿真波形，VCD 文件在 /path/to/sim.vcd：

步骤 1：先用 python3 wave.py list 浏览信号，找到 valid/ready/data 相关信号
步骤 2：用 python3 wave.py find 搜索握手异常（valid=1 且 ready=0）
步骤 3：用 python3 wave.py context 在问题时间窗口提取完整上下文
步骤 4：基于上下文分析根因，给出修复建议
```

**Claude Code 一键分析脚本：**

```bash
#!/bin/bash
# analyze_wave.sh — 一键生成波形分析上下文
VCD=$1
SIGNALS=${2:-"*"}
START=${3:-0}
END=${4:-""}

END_ARG=""
[ -n "$END" ] && END_ARG="--end $END"

python3 wave.py context "$VCD" \
    --signals "$SIGNALS" \
    --start "$START" $END_ARG \
    --question "请分析这段波形，指出任何异常或潜在问题"
```

```bash
chmod +x analyze_wave.sh
./analyze_wave.sh sim.vcd "valid,ready,data" 1000 5000
```

---

## 参考文档

- `references/sim_to_vcd.md` — 各仿真器详细导出命令和时间段截取方法
- `references/llm_prompts.md` — 针对常见调试场景的 LLM 提示词模板
  （AXI、Valid/Ready、复位、X传播、CDC、流水线）
