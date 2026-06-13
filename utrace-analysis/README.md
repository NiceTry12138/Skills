# utrace-analysis SKILL

一份**可被 Claude 当作 skill 调用**的工具：给定一个 `.utrace` 文件 + 一个有问题的函数名，
返回该函数在 trace 里最耗时的 N 帧（含完整调用子树 + 同时间窗的日志）。

底层是纯 Python 解析器，**不依赖 `UnrealInsights.exe`**。

> 本目录是一个独立 skill 的源文件，目前**没有**导入到 Claude 环境。
> Claude 选中本 skill 是通过读取 `SKILL.md` 顶部 frontmatter 的 `description`。

## 目录结构

```
utrace-analysis/
├─ SKILL.md                       Claude 读这个；frontmatter + 调用工作流
├─ README.md                      你正在看的这份；面向人类开发者
├─ scripts/
│  ├─ analyze_function.py         skill 主入口：function + utrace → JSON
│  ├─ list_timers.py              快速列 utrace 里全部 timer 名（5~10s）
│  └─ utrace_top_frames.py        通用 CLI（支持 --list-threads 等高级用法）
└─ utrace/                        纯 Python 协议解析包
   ├─ __init__.py
   ├─ protocol.py                 magic / transport / aux header bit 布局常量
   ├─ transport.py                TidPacket 切包 + LZ4 解压
   ├─ types.py                    NewEvent → EventType 注册表
   ├─ events.py                   important 流（落地）+ normal 流（流式回调）
   └─ analyzer.py                 CpuProfiler / Logging / Misc / $Trace 处理
```

## 依赖

- Python 3.8+
- `pip install lz4`（packet 大多 LZ4 块压缩，避不开）

## 作为 skill 被调用

Claude 收到 "utrace 文件 + 问题函数" 类请求时，按 `SKILL.md` 工作流：

1. 检查 `lz4` 装好
2. 调 `scripts/analyze_function.py`：

```bash
python <SKILL_DIR>/scripts/analyze_function.py \
    --utrace "<utrace 绝对路径>" \
    --function "<问题函数名>" \
    --top-n 10 \
    --track GameThread \
    --output "<输出 JSON 路径>"
```

3. 读输出 JSON，**摘录关键信息**（不直接贴原文）给用户：
   - Top-N 帧概览表
   - 每帧 Top 子调用 + 反复出现的子 timer
   - Log 里反复出现的 warning/error
   - 改进方向假设

详细规则见 `SKILL.md`。

## 直接当 CLI 用（人类视角）

### `analyze_function.py`（skill 入口的薄壳）

```text
python scripts/analyze_function.py --utrace <utrace> --function <name>
                                   [--top-n N] [--track NAME] [--output PATH]
```

| 选项                 | 必需 | 默认                                             | 说明                                  |
|----------------------|------|--------------------------------------------------|---------------------------------------|
| `--utrace PATH`      | ✅   | —                                                | `.utrace` 文件路径                    |
| `--function NAME`    | ✅   | —                                                | 问题函数名（子串匹配，区分大小写）    |
| `--top-n N`          |      | `10`                                             | 返回耗时最久的前 N 帧                 |
| `--track NAME`       |      | `GameThread`                                     | 函数所在 track / thread name          |
| `--output PATH`      |      | `<utrace 同目录>/utrace_analysis_<func>.json`    | JSON 输出路径                         |

### `list_timers.py`（确认 timer 名是否拼对）

```text
python scripts/list_timers.py <utrace> [--match SUBSTR] [--limit N] [--json]
```

只解析 events / importants 流（10 MB 量级），5~10 秒返回所有 timer 名 + 源码位置。
当 `analyze_function.py` 报 `captured trees=0` 时用它确认 function 名拼写。

```bash
python scripts/list_timers.py "C:/.../My.utrace" --match PrepareFill
# UNPCDataManager::PrepareFillNPCDataByNum  @ .../NPCDataManager.cpp:128
```

### `utrace_top_frames.py`（通用 CLI，更多控制）

```text
python scripts/utrace_top_frames.py <utrace> [-t TIMER] [-n TOP_N]
                                              [--track TRACK] [-o OUTPUT]
                                              [--list-threads]
```

| 位置 / 选项               | 必需                       | 默认            | 说明                                                                              |
|---------------------------|----------------------------|-----------------|-----------------------------------------------------------------------------------|
| `utrace`                  | ✅                         | —               | `.utrace` 文件路径                                                                |
| `-t`, `--timer TIMER`     | ✅（除非 `--list-threads`） | —               | timer 名字 **子串**，区分大小写。命中后整棵子树都列入结果                         |
| `-n`, `--top-n TOP_N`     |                            | `10`            | 返回耗时最久的前 N 帧                                                             |
| `--track TRACK`           |                            | `GameThread`    | track 名（thread name）。该 track 上 `Misc.Begin*Frame/End*Frame` 决定帧边界      |
| `-o`, `--output OUTPUT`   |                            | stdout          | 输出 JSON 文件路径；不传则直接打印                                                |
| `--list-threads`          |                            | `False`         | 列出所有 thread 及其事件数（按事件数倒序），不做帧筛选；此时 `--timer` 可省略    |
| `-h`, `--help`            |                            | —               | 显示帮助                                                                          |

> **timer 匹配规则**：`needle in timer.name`。例如 `--timer PrepareFillNPCData` 命中
> `UNPCDataManager::PrepareFillNPCDataByNum`、`STAT_PrepareFillNPCDataInternal` 等。
> 大小写敏感（与 Insights 自身搜索一致）。
>
> **排序口径**：按 *帧内匹配 timer 累计 Duration* 倒序——一帧多次命中会累加，不是整帧时长。

### 例子

```bash
# 1) 默认场景：找最慢的 10 帧 PrepareFillNPCDataByNum
python scripts/analyze_function.py \
    --utrace "C:/Users/me/AppData/Local/UnrealEngine/Common/UnrealTrace/Store/001/My.utrace" \
    --function PrepareFillNPCDataByNum

# 2) 不知道 thread 名时先列线程
python scripts/utrace_top_frames.py "C:/.../My.utrace" --list-threads

# 3) RenderThread 上找最慢的 5 帧某渲染 timer
python scripts/utrace_top_frames.py "C:/.../My.utrace" \
    -t "FRDGBuilder::Execute" -n 5 --track "RenderThread 0" -o render_top.json

# 4) stdout 直接给 jq
python scripts/utrace_top_frames.py "C:/.../My.utrace" -t MyTimer -n 3 | jq '.[].frame'
```

### 退出码

| 码 | 含义                                                                                  |
|----|---------------------------------------------------------------------------------------|
| 0  | 正常完成                                                                              |
| 1  | 出错并打印诊断：utrace 不存在 / 协议版本不支持 / track 名找不到 / 命令行参数缺失 等   |

### 进度日志（stderr，不污染 JSON）

```
[1/4] 读入 568.0 MiB
[1/4] TransportVersion=4 ProtocolVersion=7
[2/4] 切包 + 解压 完成，24 条线程流，sync=3，耗时 3.5s
[3/4] importants 解析完成，147056 timers, 970 log specs，耗时 0.5s
[4/4] 业务线程解析完成，22 条线程，耗时 173.5s
      Frames: GameThread=6419 RenderThread=6418; scope events total=211358216;
      captured «PrepareFillNPCDataByNum» trees=10
[done] 写入 result.json
```

## 输出 JSON 结构

```jsonc
[
  {
    "frame": 6368,                       // GameThread 帧序号（1-based）
    "useTime": "73.5ms",                 // 整帧时长
    "Time": "9m21.0202090s",             // 帧起点相对 trace 起点
    "<function_name>": {                 // key 是用户传的 function 名
      "calls": [
        {
          "Name": "UNPCDataManager::PrepareFillNPCDataByNum",
          "useTime": "11.3ms",
          "Time": "9m21.0344000s",
          "calls": [/* 嵌套子调用，深度可达 10+ */]
        }
        // 同一帧多次命中 → 多元素
      ]
    },
    "Log": [
      {
        "Time": "9m21.0260000s",
        "Category": "LogVATSystem",
        "Message": "RequestAppearanceAsync GroupID [901] ...",
        "File": "...VATAvatarManagerBase.cpp",
        "Line": 122
      }
      // 在匹配函数 scope 时间窗内发生的全部日志
    ]
  }
]
```

## 性能 / 内存

| utrace 大小（压缩后） | 解码后字节流 | scope 事件数 | 跑完时长 | 峰值内存 |
|----------------------|-------------|-------------|---------|---------|
| ~100 MiB             | ~200 MiB    | ~5 千万     | ~40 s   | ~0.6 GiB |
| ~600 MiB             | ~1.2 GiB    | ~2 亿       | ~3 min  | ~2 GiB  |
| ~2 GiB               | ~4 GiB      | ~7 亿       | ~10 min | ~6 GiB  |

> 关键省内存技巧：分析时只对**含 function 子串的子树**构造 `TimingEvent` 对象；
> 其它 99% 的 scope 只用 `(start_time, name)` 浅栈跟踪。
> `--list-threads` 模式（不传 `--function`）走纯计数路径，更省。

## 实现概览

参考引擎源码：

- 字节解码：`Engine/Source/Developer/TraceAnalysis/Private/Analysis/Engine.cpp`
  - `FMagicStage` / `FMetadataStage` / `FEstablishTransportStage` / `FProtocol{5,6,7}Stage`
- TidPacket 切包 + LZ4：`Transport/TidPacketTransport.cpp`
- CPU profiler 7-bit varint：`CpuProfilerTraceAnalysis.cpp::ProcessBufferV2`
- Log args printf 重放：`TraceServices/Private/Common/FormatArgs.cpp`
- 帧切分：`MiscTraceAnalysis.cpp::OnEvent BeginGameFrame/EndGameFrame`

## 已知限制

1. 仅支持 `ProtocolVersion 5/6/7`、`TransportVersion 3/4`（TidPacket / TidPacketSync）。
   UE 5.x 全是这个组合。
2. 不实现 Protocol5 的 24-bit Serial 全局重排——单线程内事件本就有序。
3. Log 的 `printf` 重放是简化版（不严格处理 `%*.*f`）。
4. 帧来自 GameThread 上的 `Misc.BeginGameFrame/EndGameFrame`（或新版
   `BeginFrame/EndFrame`）。RenderThread 帧亦然。`--track` 指其它线程时
   仍按 GameThread 帧索引（其它线程不发帧事件）。

## 把这个 skill 装进 Claude Code

如果你后续想真正激活它（默认没装）：

```bash
# 全局可用
cp -r S:/SP/Tools/utrace-analysis ~/.claude/skills/

# 或者只在本仓库可用
cp -r S:/SP/Tools/utrace-analysis S:/SP/.claude/skills/
```

之后 Claude 看到 `.utrace + 问题函数` 类请求会自动选用。
