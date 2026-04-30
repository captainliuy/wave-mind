# LLM 波形分析提示词模板

将 `wave context` 的输出与以下提示词组合，直接传给模型分析。

---

## 通用分析模板

```
以下是 RTL 仿真波形数据（VCD 提取）：

<waveform>
[粘贴 wave context 输出]
</waveform>

请分析：
1. 各信号的行为是否符合预期？
2. 是否存在时序违例或协议违规？
3. 异常现象的可能原因？
4. 建议如何修复？
```

---

## 协议分析：AXI4 / AXI-Stream

```
以下是 AXI-Stream 接口的仿真波形：

<waveform>
[粘贴包含 TVALID/TREADY/TDATA/TLAST 的 context 输出]
</waveform>

请检查：
1. TVALID/TREADY 握手是否正确（TVALID 不能因 TREADY 为低而撤销）
2. TLAST 是否正确标记每个 burst 的最后一拍
3. 背压（backpressure）处理是否正确
4. 是否存在死锁风险？
```

---

## 协议分析：Valid/Ready 握手

```
以下是 Valid/Ready 握手接口的仿真波形：

<waveform>
[粘贴 context 输出]
</waveform>

请分析握手时序：
1. sender 在 ready 为低时是否保持了 valid 和 data 稳定？
2. 每次成功握手（valid & ready）时数据是否正确？
3. 是否存在 valid 提前撤销的问题？
4. 整体吞吐率和延迟如何？
```

---

## 复位分析

```
以下是复位相关信号的仿真波形：

<waveform>
[粘贴包含 rst_n, clk 以及关键状态信号的 context 输出]
</waveform>

请分析复位行为：
1. 复位是否在足够时钟周期内保持有效？
2. 复位释放后各状态信号是否正确初始化？
3. 是否存在亚稳态或复位同步问题的迹象？
4. 异步复位同步释放（ARSR）是否正确实现？
```

---

## X 传播调试

```
以下是含 X 值的仿真波形（已检测到 X 传播）：

<waveform>
[粘贴 context 输出，确保包含 summary 异常报告]
</waveform>

请帮助定位 X 传播的根因：
1. X 值最早出现在哪个信号？在什么时刻？
2. X 是由复位未初始化、多驱动冲突还是未决条件引起的？
3. X 传播路径是怎样的？
4. 如何在 RTL 中修复？
```

---

## 时钟域交叉（CDC）分析

```
以下是跨时钟域接口的仿真波形：

<waveform>
[粘贴包含两个时钟域关键信号的 context 输出]
</waveform>

请分析 CDC 安全性：
1. 同步信号的采样是否在源域稳定后进行？
2. 是否存在亚稳态窗口内的采样？
3. 握手同步方案（如 req/ack）是否正确实现了四相握手？
4. 建议的 CDC 检查点？
```

---

## 流水线分析

```
以下是流水线数据路径的仿真波形：

<waveform>
[粘贴各流水线级 valid/data 信号的 context 输出]
</waveform>

请分析流水线行为：
1. 数据在各流水线级的延迟是否正确？
2. 是否存在气泡（bubble）或停顿（stall）？
3. 数据相关或控制相关是否被正确处理？
4. 吞吐率是否达到预期（每周期一拍）？
```

---

## 命令行直接传给 LLM（需安装 `llm` CLI）

```bash
# 使用 Simon Willison 的 llm CLI
wave context sim.vcd --signals valid,ready,data --start 1000 --end 3000 \
  | llm "这个 AXI-Stream 握手是否正确？valid 是否存在过早撤销的问题？"

# 使用 Claude CLI（Claude Code 环境）
wave context sim.vcd --signals "*" --question "为什么 data_out 在 valid 拉高后还延迟了 3 个周期？" \
  | claude --print

# 使用 OpenAI CLI
wave context sim.vcd --signals rst_n,state,error --start 0 --end 500 \
  | openai api chat.completions.create -m gpt-4o --stdin
```
