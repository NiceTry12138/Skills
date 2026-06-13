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

## 工作流（生成 JSON）

本 SKILL.md 只负责**怎么从 utrace 生成 JSON**。**拿到 JSON 之后如何分析、如何下结论 → 读同目录 `ANALYSIS.md`**。

1. **环境**：`python -c "import lz4.block"`，缺则提示用户 `pip install lz4`，不要绕。
2. **跑分析**（**必须 `Bash` 后台运行**，`run_in_background: true` + `timeout: 900000`，trace 越大越久；600 MiB 约 3 min）：
   ```bash
   python <SKILL_DIR>/scripts/analyze_function.py \
       --utrace "<utrace_path>" --function "<function_name>" \
       --top-n 10 --track GameThread \
       --output "<output>.json"
   ```
3. **拿到 JSON 后转入 `ANALYSIS.md`** —— 该文档是分析方法论，包含：
   - 读 meta 判跨度
   - 抽每帧 N、列回归表
   - 判形状（O(N) 累积 / 单点异常 / 混合 / 外部干扰）
   - **★ O(N) / 混合型必须追问 N 的系统上界（读源码外推，不能只看 utrace 的 max(N)）**
   - 单点异常的子树拆解 + 零假设证伪
   - 多轴对比避免误判
   - 输出格式与禁忌

   **不要在 SKILL.md 里重复这套方法论**——以 ANALYSIS.md 为准。

4. **细查子调用时**用摘要脚本，**不要直接 `Read` `frames`**（可能上 MB）：
   ```bash
   python <SKILL_DIR>/scripts/summarize_frames.py "<output>.json" \
       [--top-frames 5] [--top-subs 15]
   ```

## 输出 JSON schema

```jsonc
{
  "meta": {                              // ★ 全集统计（µs 单位整数），优先看这里
    "function": "Tick_UTramSystem",
    "track": "GameThread",
    "total_calls": 6514,                 // 整个 trace 里该 timer 总命中次数
    "hit_frames": 6514,                  // 命中过的帧数（同帧多次合并为 1）
    "total_frames_on_track": 6515,       // 该 track 上的总帧数（命中率 = hit_frames / 此项）
    "duration_us_per_call": {            // 单次调用时长分布
      "min": 412, "p50": 660, "p90": 782, "p99": 908, "max": 1200, "mean": 663
    },
    "duration_us_per_frame_accum": {     // 同一帧累计（多次命中合计）
      "min": 412, "p50": 661, "p90": 783, "p99": 909, "max": 1205, "mean": 664
    },
    "frame_total_us_when_hit": {         // 命中帧本身的整帧时长（看是不是该 timer 拖慢了帧）
      "min": 12100, "p50": 16800, "p90": 28600, "p99": 65300, "max": 74900, "mean": 18900
    },
    "top_n_returned": 10,
    "top_n_covers_all_hits": false       // true ⇒ Top-N 已包含全部命中帧，不必加大 N
  },
  "frames": [{
    "frame": 6368,                       // GameThread 帧号（1-based）
    "useTime": "73.5ms",                 // 整帧时长
    "Time": "9m21.0202090s",             // 帧起点
    "<function_name>": {
      "calls": [{                        // 同帧多次命中 → 多元素
        "Name": "...", "useTime": "11.3ms", "Time": "...",
        "calls": [/* 嵌套子调用 */]
      }]
    },
    "Log": [{                            // 在 function scope 时间窗内的日志
      "Time": "...", "Category": "...", "Message": "...",
      "File": "...", "Line": 122
    }]
  }]
}
```

`frames` 排序：按 *帧内匹配 timer 累计 Duration* 倒序。

## 行为铁律

1. **不要假装跑过**。所有数字都来自 JSON；JSON 没说的不要编（如"调用了 N 次"——这条信息没有）。
2. **utrace 是大二进制（500 MB+），禁止 `Read` 它**——会崩；只把路径喂给脚本。
3. **不要重写解析逻辑**。`utrace/` 包已覆盖 Protocol 5/6/7 + LZ4 + CPU profiler + Log。要扩展先读 `utrace/analyzer.py`。
4. **时间格式直接转交**给用户（已经是 `73.5ms` / `9m21.02s` 人读形式）。
5. **信任 `meta`，不要为分布跑大 `--top-n`**。
   - JSON 顶部 `meta.duration_us_per_call.{min,p50,p90,p99,max}` 就是**全集**分布——基于全部命中算的，不是 Top-N 子集。要回答"这函数有没有异常帧/最大耗时多少/p99 多少"直接读 `meta`。
   - `meta.top_n_covers_all_hits = true` 表示 Top-N 已经包含**所有**命中帧，再加大 N 也不会有新数据。
   - stderr 末尾的 `captured «<func>» trees=N` 与 `meta.total_calls` 同义——表示整份 trace 里该函数共命中 N 次，**Top-N 之外没有更多命中**。
   - 不要为了"看完整分布"重跑一次 `--top-n 全部命中数`：会再多花一次解析时间 + 数 GB JSON，且不会得到 `meta` 没给过的信息。除非用户**明确要求**逐帧明细，否则不要这么做。
6. **拿到 JSON 后必须读 `ANALYSIS.md` 走一遍方法论**——不要只看 max 帧给结论，不要把 utrace 里观测到的 max(N) 当系统上限。

## 常见失败 → 处置

| 现象 | 处置 |
|------|------|
| `captured «<func>» trees=0` | 用 `scripts/list_timers.py <utrace> --match <子串>` 几秒内列出真实 timer 名 |
| 命中很多但都很短 | 子串太宽（如 `Tick`），让用户给更具体名字 |
| `未支持的 ProtocolVersion=N` | 把版本号给用户，确认引擎版本 |
| 没给 `--track` 时找不到线程 | 跑 `scripts/utrace_top_frames.py <utrace> --list-threads` 列出可用 thread name |

详细参数 / schema / 实现细节见同目录 `README.md`；分析方法论见同目录 `ANALYSIS.md`。
