---
name: utrace-analysis
description: 当用户提供 .utrace 文件 + 一个有问题的函数 / timer 名，需要定位该函数最慢的若干帧、查看完整调用子树和同时间窗日志时触发。Skill 用纯 Python 解析器（不依赖 UnrealInsights.exe）抽出最耗时的 N 帧并输出 JSON，供 Claude 提炼瓶颈和优化建议。关键词：utrace、Unreal Insights、timing 分析、卡帧、性能瓶颈、慢函数、函数耗时。
---

# utrace-analysis SKILL

## 何时触发

用户给你一份 `.utrace` 文件路径 + 一个怀疑慢的函数 / timer / scope 名，想知道它在哪几帧最卡、调用栈是什么、当时打了什么日志。例如：

- "帮我看下 `MyTrace.utrace` 里 `PrepareFillNPCDataByNum` 最慢的 10 帧"
- "这是 utrace，`UWorld::Tick` 在哪几帧卡，调用栈是什么"

## 输入契约

**必填**：

- `utrace_path`：`.utrace` 绝对路径
- `function_name`：函数 / timer 名（**子串匹配，区分大小写**）

**选填**：`top_n`（默 10）、`track`（默 GameThread；其它常见 `RenderThread 0` / `FAsyncLoadingThread`）

任何一个必填项缺失就主动追问，不要硬猜。

## 工作流

1. **环境**：`python -c "import lz4.block"`，缺则提示用户 `pip install lz4`，不要绕。
2. **跑分析**（**必须 `Bash` 后台运行**，`run_in_background: true` + `timeout: 900000`，trace 越大越久；600 MiB 约 3 min）：
   ```bash
   python <SKILL_DIR>/scripts/analyze_function.py \
       --utrace "<utrace_path>" --function "<function_name>" \
       --top-n 10 --track GameThread \
       --output "<output>.json"
   ```
3. **读 JSON 提炼**给用户：Top-N 帧概览表 + 各帧 Top 子调用（**重点指出反复出现的子 timer**）+ Log 里反复出现的 warning/error + 改进方向假设（标 ⚠️）。**不要原样贴 JSON**——它可能上 MB。

## 输出 JSON schema

```jsonc
[{
  "frame": 6368,                         // GameThread 帧号（1-based）
  "useTime": "73.5ms",                   // 整帧时长
  "Time": "9m21.0202090s",               // 帧起点
  "<function_name>": {
    "calls": [{                          // 同帧多次命中 → 多元素
      "Name": "...", "useTime": "11.3ms", "Time": "...",
      "calls": [/* 嵌套子调用 */]
    }]
  },
  "Log": [{                              // 在 function scope 时间窗内的日志
    "Time": "...", "Category": "...", "Message": "...",
    "File": "...", "Line": 122
  }]
}]
```

排序：按 *帧内匹配 timer 累计 Duration* 倒序。

## 行为铁律

1. **不要假装跑过**。所有数字都来自 JSON；JSON 没说的不要编（如"调用了 N 次"——这条信息没有）。
2. **utrace 是大二进制（500 MB+），禁止 `Read` 它**——会崩；只把路径喂给脚本。
3. **不要重写解析逻辑**。`utrace/` 包已覆盖 Protocol 5/6/7 + LZ4 + CPU profiler + Log。要扩展先读 `utrace/analyzer.py`。
4. **时间格式直接转交**给用户（已经是 `73.5ms` / `9m21.02s` 人读形式）。

## 常见失败 → 处置

| 现象 | 处置 |
|------|------|
| `captured «<func>» trees=0` | 用 `scripts/list_timers.py <utrace> --match <子串>` 几秒内列出真实 timer 名 |
| 命中很多但都很短 | 子串太宽（如 `Tick`），让用户给更具体名字 |
| `未支持的 ProtocolVersion=N` | 把版本号给用户，确认引擎版本 |
| 没给 `--track` 时找不到线程 | 跑 `scripts/utrace_top_frames.py <utrace> --list-threads` 列出可用 thread name |

详细参数 / schema / 实现细节见同目录 `README.md`。
