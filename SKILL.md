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

将仿真 VCD 波形转换为大模型可读的结构化文本，辅助 RTL 调试与分析。

## 核心工具

**`scripts/wave.py`** — 唯一入口 CLI，纯 Python 标准库，无需安装依赖。

```
用法：python wave.py <子命令> <vcd文件> [选项]
```

---

## 子命令速查

### `list` — 浏览信号

```bash
# 列出所有信号
python wave.py list sim.vcd

# 按跳变次数降序排列（快速找到活跃信号）
python wave.py list sim.vcd --sort toggle

# 过滤名称（通配符）
python wave.py list sim.vcd --filter "axi*"
python wave.py list sim.vcd --filter "/valid|ready/"   # 正则
```

---

### `dump` — 波形表格

输出时间 × 信号的矩阵，多位信号显示十六进制。

```bash
# 指定信号，全时间范围
python wave.py dump sim.vcd --signals clk,rst_n,valid,data

# 截取时间段（单位与 VCD timescale 一致）
python wave.py dump sim.vcd --signals valid,data --start 1000 --end 5000

# 控制采样密度（--max-cols 越大越详细，默认 80 列）
python wave.py dump sim.vcd --signals "*" --max-cols 40

# 通配符选择信号组
python wave.py dump sim.vcd --signals "axi_*"

# 正则选择
python wave.py dump sim.vcd --signals "/^tb\.dut\.(valid|data|ack)$/"
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
python wave.py trace sim.vcd --signals valid,ready,data

# 指定时间窗口
python wave.py trace sim.vcd --signals "axi_*" --start 500 --end 2000
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
python wave.py find sim.vcd --when "valid == 1 and ready == 1"

# 找到 data 不为零的时刻
python wave.py find sim.vcd --when "data != 0"

# 找到上升沿
python wave.py find sim.vcd --when "rising(valid)"

# 找到下降沿
python wave.py find sim.vcd --when "falling(rst_n)"

# 时间范围内搜索
python wave.py find sim.vcd --when "error == 1" --start 0 --end 10000

# 显示更多结果
python wave.py find sim.vcd --when "state != 0" --limit 50
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

### `summary` — 统计与异常检测

输出每个信号的跳变统计，并自动检测：
- `STUCK` — 信号在分析窗口内从未跳变
- `GLITCH` — 脉宽小于阈值的毛刺
- `X-PROP` — 出现 X 值（未初始化/多驱动）
- `HI-Z` — 出现 Z 值（高阻）

```bash
python wave.py summary sim.vcd --signals "*"

# 指定时间窗口
python wave.py summary sim.vcd --signals "*" --start 100 --end 5000

# 调整毛刺判定阈值（脉宽 < 3 视为毛刺）
python wave.py summary sim.vcd --signals data,valid --glitch-width 3
```

---

### `context` — 完整 LLM 上下文（推荐主入口）

将 list + dump + trace + summary 组合成结构化文本块，直接传给大模型。

```bash
# 基本用法
python wave.py context sim.vcd --signals valid,ready,data

# 带时间窗口 + 附加问题
python wave.py context sim.vcd \
    --signals "axi_*" \
    --start 1000 --end 5000 \
    --question "为什么 axi_valid 在 reset 释放后延迟了 5 个周期才拉高？"

# 管道传给大模型（Claude Code 环境）
python wave.py context sim.vcd --signals valid,data --start 0 --end 2000 \
    | claude --print "请分析这段波形的握手行为"

# 保存到文件供粘贴
python wave.py context sim.vcd --signals "*" > wave_context.txt
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

| 仿真器     | 导出命令                                          | 时间段截取参数        |
|-----------|--------------------------------------------------|--------------------|
| Questa    | `wlf2vcd -o sim.vcd vsim.wlf`                   | `-start T -end T`  |
| VCS       | `vpd2vcd dump.vpd sim.vcd`                       | `-s T -e T`        |
| Xcelium   | `simvision -batch -input convert.tcl`            | Tcl 中 `-start/-end` |
| Icarus    | `$dumpfile` + `$dumpon` / `$dumpoff`             | testbench 逻辑控制  |
| Verilator | `VerilatedVcdC::dump()` 条件调用                 | C++ 循环条件控制    |
| GTKWave   | `fst2vcd sim.fst -o sim.vcd`                    | 转后用 wave  |

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

## 完整工作流示例

### 场景：AXI-Stream 握手异常

```bash
# 1. 从仿真器导出 VCD（以 Questa 为例）
wlf2vcd -start 0 -end 10000 -o axi.vcd vsim.wlf

# 2. 先浏览信号，找到相关信号名
python wave.py list axi.vcd --filter "*tvalid*,*tready*,*tdata*"

# 3. 搜索握手失败的位置（valid 高但 ready 低超过 5 周期）
python wave.py find axi.vcd --when "tvalid == 1 and tready == 0" --start 0 --end 10000

# 4. 在问题区域提取完整上下文
python wave.py context axi.vcd \
    --signals tvalid,tready,tdata,tlast \
    --start 3200 --end 3800 \
    --question "tvalid 在 t=3500 后为何持续拉低？是否违反了 AXI-Stream 规范？" \
    > bug_context.txt

# 5. 传给大模型分析
cat bug_context.txt | claude --print
```

### 场景：复位后 X 传播

```bash
# 直接 summary 检测异常
python wave.py summary sim.vcd --signals "*" --start 0 --end 500

# 锁定 X 传播时间点
python wave.py find sim.vcd --when "rising(rst_n)"

# 在复位释放附近提取上下文
python wave.py context sim.vcd \
    --signals rst_n,state,data_out,valid \
    --start 95 --end 200 \
    --question "rst_n 释放后 data_out 出现 X，根因是什么？"
```

---

## 在 Claude Code / Codex 中的使用方式

Claude Code 和 Codex 可以直接调用 bash 工具运行这些命令，然后分析输出。

**推荐指令模式：**

```
分析这个仿真波形，VCD 文件在 /path/to/sim.vcd：
1. 先用 wave list 浏览信号
2. 重点看 valid/ready/data 相关信号
3. 找出握手异常的时间段
4. 生成上下文后告诉我根因
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

python wave.py context "$VCD" \
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
