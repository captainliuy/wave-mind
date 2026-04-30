# 仿真器 VCD 导出命令速查

各主流仿真器导出 VCD 的方法，**均支持指定时间段**以控制文件体积。

---

## Questa / ModelSim — `wlfman filter` + `wlf2vcd`

`wlf2vcd` **仅负责格式转换**，不支持时间截取和信号过滤。
需先用 `wlfman filter` 裁剪 WLF，再转为 VCD。

### 完整流程（裁剪 + 转换）

```bash
# 1. 截取时间段 + 过滤信号，生成裁剪后的 WLF
wlfman filter vsim.wlf \
    -begin 1000 -end 5000 \
    -r tb/dut \
    -o trim.wlf

# 2. 将裁剪后的 WLF 转为 VCD
wlf2vcd -o sim.vcd trim.wlf
```

### 完整导出（不裁剪）

```bash
wlf2vcd -o sim.vcd vsim.wlf
```

### wlfman filter 参数速查

| 参数 | 说明 |
|------|------|
| `-begin <time>` | 起始时间（默认从文件开头） |
| `-end <time>` | 结束时间 |
| `-s <symbol>` | 包含指定信号（如 `tb/dut/valid`） |
| `-r <object>` | 递归包含指定层次下的所有信号 |
| `-f <list_file>` | 指定信号列表文件（由 `wlfman items` 生成） |
| `-t <resolution>` | 输出 WLF 时间分辨率（默认同源文件） |
| `-o <outwlffile>` | **必需**，输出 WLF 文件名 |
| `<sourcewlffile>` | 源 WLF 文件 |

**信号过滤示例：**

```bash
# 仅导出特定信号
wlfman filter vsim.wlf \
    -begin 1000 -end 5000 \
    -s tb/dut/clk -s tb/dut/valid -s tb/dut/data \
    -o trim.wlf

# 递归导出某层次下所有信号
wlfman filter vsim.wlf \
    -begin 1000 -end 5000 \
    -r tb/dut/axi_if \
    -o trim.wlf
```

---

## Synopsys VCS — `vpd2vcd`

```bash
# VPD → VCD（完整）
vpd2vcd dump.vpd sim.vcd

# 截取时间段
vpd2vcd -s 1000 -e 5000 dump.vpd trim.vcd

# 指定信号层次
vpd2vcd -scope tb.dut dump.vpd out.vcd
```

**仿真时生成 VCD**（在 testbench 加 `$dumpfile`，见下方通用方法）

---

## Cadence Xcelium — `simvision` 批处理

```tcl
# convert.tcl
database open -shm waves.shm -readonly
set signals [database signals -all]
database export -format vcd -output sim.vcd \
    -start 1000ns -end 5000ns              ;# 时间段截取
database close
```

```bash
simvision -batch -input convert.tcl
```

**xrun 命令行直接生成 VCD**：

```bash
xrun -access +rwc tb.sv dut.sv \
     -input <(echo "
       database -open waves -vcd -into sim.vcd
       probe -create -all -depth all
       run 10us
     ")
```

---

## Icarus Verilog / Verilator — 原生 VCD

两者直接在 testbench 中控制，**时间段由仿真逻辑决定**：

```verilog
// Icarus Verilog testbench
initial begin
    $dumpfile("sim.vcd");
    $dumpvars(0, tb);       // 0 = 全层次
    #START_TIME;            // 延迟开始记录
    $dumpon;
    #(END_TIME - START_TIME);
    $dumpoff;              // 停止记录
    $finish;
end
```

```cpp
// Verilator C++ testbench
#include "verilated_vcd_c.h"
Verilated::traceEverOn(true);
auto *tfp = new VerilatedVcdC;
top->trace(tfp, 99);
tfp->open("sim.vcd");

// 仿真循环
uint64_t time = 0;
while (time < END_TIME) {
    top->eval();
    if (time >= START_TIME)
        tfp->dump(time);   // 只在时间窗口内 dump
    time++;
}
tfp->close();
```

---

## GTKWave — 格式互转

GTKWave 附带的命令行工具可在常见格式间互转（需安装 GTKWave 附带工具）：

```bash
# FST → VCD（Verilator 默认格式，比 VCD 小 10x）
# 工具位于 GTKWave 安装目录：fst2vcd / lxt2vcd
fst2vcd sim.fst -o sim.vcd

# LXT2 → VCD
lxt2vcd sim.lxt2 -o sim.vcd

# VCD 截取时间段（利用 wave.py）
python3 wave.py dump full.vcd --start 1000 --end 5000 > trim_context.txt
```

---

## 通用方法 — SystemVerilog `$dumpfile`

适用于所有支持 PLI 的仿真器：

```verilog
module tb;
  initial begin
    $dumpfile("sim.vcd");
    $dumpvars(0, tb);          // 层次深度 0 = 全部
    // 或只 dump 特定信号：
    // $dumpvars(1, tb.dut.valid, tb.dut.data);

    // 时间段控制
    $dumpoff;                  // 默认关闭
    #1000;
    $dumpon;                   // t=1000 开始记录
    #5000;
    $dumpoff;                  // t=6000 停止记录
    $finish;
  end
endmodule
```

---

## 快速选择指南

| 仿真器      | 推荐方式              | 时间段截取                  |
|------------|----------------------|-----------------------------|
| Questa     | `wlfman filter` + `wlf2vcd` | `wlfman filter -begin/-end` |
| VCS        | `vpd2vcd`            | `-s` / `-e`                 |
| Xcelium    | `simvision -batch`   | `-start` / `-end` in Tcl    |
| Icarus     | `$dumpfile` + `$dumpon/off` | testbench 逻辑控制  |
| Verilator  | `VerilatedVcdC`      | C++ 循环条件控制             |
| 任意格式   | GTKWave `fst2vcd`    | 转换后用 `python3 wave.py` 截取 |
