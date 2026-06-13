# utrace JSON 分析方法论

> 拿到 `analyze_function.py` 输出的 JSON 之后该怎么读、怎么下结论。  
> `SKILL.md` 只管“怎么生成 JSON”，本文管“怎么从 JSON 得出正确诊断”。

---

## 总原则

1. **先判样本可信度，再读分位数**  
   `total_calls` 很少时，`p90` / `p99` 只是 top 样本附近的排序值，不是稳定统计分位。小样本结论要标注置信度。

2. **从 `meta` 开始**  
   能在 `meta` 层回答的问题就不要往下钻。先看总体分布、长尾、帧预算占比，再决定是否拆帧和拆子树。

3. **形状优先于极值**  
   先判 O(N) 累积 / 单点异常 / 混合 / 外部干扰，再谈某一帧多卡。不要被 max 帧牵着走。

4. **绝对耗时 AND 相对跨度都要看**  
   `max/p50` 只说明是否长尾，不说明是否可接受。稳定 8ms 也是严重问题，哪怕跨度不大。

5. **观测 ≠ 上限**  
   utrace 是一段录像，里面的 max(N) 不是系统上限，是**这次跑到的最大值**。系统上限要去源码、配置、数据规模、上层调用循环里找。

6. **占比 ≠ 根因**  
   `sum_us` 排第一的 sub-timer 可能是根因，也可能只是被 N 放大的高频小项。必须做“零假设”证伪。

7. **结论用 AND，不用 OR**  
   一个函数可以**同时**是结构性问题和异常帧的放大器。  
   “是 N 的问题，不是日志的问题”是错误二分法；往往两个都成立，要分层写。

8. **Inclusive time 陷阱**  
   调用树里父节点 `useTime` 包含所有子节点；摘要里 `sum_us` 把同名子 timer 累加，可能跨层嵌套。  
   **禁止把多个层级的 `sum_us` 直接相加再和父节点比**，否则会重复计数。

9. **Async 命名不代表真的异步**  
   函数名带 `Async` / `Queue` / `Defer`，不代表一定跨帧执行。cache 命中、快路径、回调可能仍然同步发生。必须用时间窗验证。

10. **裁剪 JSON 不能替代完整帧上下文**  
    `analyze_function.py` 输出的是目标函数局部视角。异常帧的真凶可能在函数外：GC、AsyncLoading、IO、锁、日志后端、Render/RHI 同步等。

---

## Step 0 — 判断样本可信度

在读 `meta` 的分位数之前，先判断这份 JSON 的统计可信度。

重点看：

- `total_calls`：目标函数总命中次数。
- `hit_frames`：命中帧数。
- `top_n_returned`：返回了多少帧详情。
- `top_n_covers_all_hits`：`frames[]` 是否覆盖全部命中。
- trace 是否覆盖完整场景：冷启动 / 稳态 / 资源加载 / 压力峰值 / 玩家关键路径。

### 样本量经验判断

| 样本量 | 结论可信度 |
|---:|---|
| `< 10` | 只能做案例分析，不适合做分布结论 |
| `10 ~ 30` | 可以做形状假设，但 `p90/p99` 置信度较低 |
| `30 ~ 100` | 可以做较可靠的趋势判断 |
| `> 100` | 分位数和回归分析更可信 |

### 小样本下的 p90 / p99 注意事项

当 `total_calls` 很少时：

- `p99` 基本接近 max；
- `p90` 也只是 top 样本附近；
- 不要写成“99 分位稳定达到 xx”。

建议表述为：

> 当前样本量较少，`p99/max` 更适合作为 top 慢样本参考，不代表稳定长期分位。

### top_n 截断风险

如果：

```json
"top_n_covers_all_hits": false
```

说明 `frames[]` 只包含 top 慢帧，不代表全量帧分布。此时：

- 可以分析慢帧案例；
- 不要用 `frames[]` 推断全局比例；
- 全局分布以 `meta` 为准；
- 回归表结论要标注“基于 top 慢帧”。

---

## Step 1 — 读 meta，判跨度和预算影响

只看 JSON 顶部 `meta`：

- `total_calls`
- `hit_frames`
- `top_n_covers_all_hits`
- `duration_us_per_call`
- `duration_us_per_frame_accum`
- `frame_total_us_when_hit`

### 1.1 判断是否长尾

重点看：

```text
max / min
p99 / p50
mean vs p50
```

经验判断：

| 现象 | 初步判断 |
|---|---|
| 跨度 `< 2×` | 稳定单点，可能是稳定热点 |
| 跨度 `2~3×` | 可疑，建议继续 |
| 跨度 `> 3×` | 有变量在变，必须找出“什么变量在变” |
| mean 远大于 p50 | 长尾拉高均值，不是稳定慢 |

这些倍数是经验启发，不是数学定律。

### 1.2 同时看绝对帧预算

倍数只回答“是否长尾”，不回答“是否可接受”。

参考：

| 目标帧率 | 单帧预算 |
|---|---:|
| 60 FPS | 16.6ms |
| 30 FPS | 33.3ms |

经验上：

| 单函数 GameThread 耗时 | 关注程度 |
|---:|---|
| `> 1ms` | 值得关注 |
| `> 3ms` | 通常需要优化 |
| `> 8ms` | 高风险 |
| `> 16ms` | 60 FPS 下单函数已超帧预算 |

项目可以按实际目标调整阈值。

### 1.3 看命中帧整帧时长

`frame_total_us_when_hit` 用来判断目标函数与整帧卡顿的关系。

如果目标函数耗时只占整帧 `< 20%`：

> 它通常不是该帧唯一主因；但它仍可能是 hot path 结构性瓶颈或异常放大器，需要结合 N、调用频率和上下文继续判断。

不要因为占整帧比例低就直接放掉它。

---

## Step 2 — 抽 N，并校验 N

N 是驱动函数耗时变化的“业务规模变量”。

### 2.1 如何选择 N

从：

```text
frames[].<func>.calls[].calls[]
```

里统计主要 sub-timer 的出现次数。

优先用 `summarize_frames.py` 的 GLOBAL hot sub-timers 表，选：

1. count 高；
2. 语义对应主循环每轮一次；
3. 和源码或日志中的业务数量能对上。

例如：

```text
STAT_Tick_UNPC_SpawnNPC
```

如果源码是：

```cpp
for (int32 i = 0; i < DataNum; ++i)
{
    SpawnNPC(...);
}
```

那么 `STAT_Tick_UNPC_SpawnNPC` count 可以作为 `N_timer`。

### 2.2 N 的一致性校验

选出 N 后，必须校验：

- timer count 是否等于日志中的 `final num` / `DataNum` / `Spawned`？
- timer 是否真的每轮循环只调用一次？
- 是否存在提前 return？
- 是否存在失败重试？
- 是否有分支跳过？
- 是否有同名 timer 在不同层级重复出现？
- 是否存在一个业务 item 多次调用同一个 timer？

必要时区分：

```text
N_business：业务数量，例如 final num / DataNum / Spawned
N_timer：timer 出现次数，例如 SpawnNPC count
```

二者不一致时，不要硬等同。差异本身就是线索。

### 2.3 不要手动 Read 巨大 frames

frames 可能很大。优先用脚本抽取：

```bash
python <SKILL_DIR>/scripts/summarize_frames.py "<output>.json" \
    --top-frames 10 \
    --top-subs 20
```

或用 Bash + Python 一次性统计，不要手工读完整 `frames[]`。

---

## Step 3 — 建回归表：按 N 升序

**必须按 N 升序排，不要按耗时排。**

按耗时排会让你从 max 帧开始看，容易错过整体形状。

基础表：

```text
frame | N | 总耗时 | 单次均值 = 总耗时 / N
```

更推荐：

```text
frame | N | 总耗时 | 单次均值 | 单次中位 | 单次 max
```

### 注意：`总耗时 / N` 是均值，不是中位数

错误写法：

```text
单次中位 = 总耗时 / N
```

正确写法：

```text
单次均值 = 总耗时 / N
```

如果要算“单次中位”，必须拿到每个 item 的耗时列表，排序后取 median。

### 为什么要看 median / max

例如某帧：

```text
26 个 Spawn = 50us
1 个 Spawn = 1000us
```

这时：

- `mean_per_item` 会被 1000us 拉高；
- `median_per_item` 仍然正常；
- `max_item` 能揭示少数 item 长尾。

判断：

| 现象 | 解释 |
|---|---|
| mean 高，median 正常 | 少数 item 长尾 |
| mean 高，median 也高 | 全体 item 系统性变慢 |
| median 接近，N 差很多 | 结构性 O(N) |
| N 相同，median/max 同步放大 | 外部或全局放大器 |

---

## Step 4 — 判形状：O(N)、异常、混合、外部干扰

根据回归表判断：

| 总耗时与 N 的关系 | 结论 | 下一步 |
|---|---|---|
| 线性相关，斜率稳定 | **O(N) 累积型** | Step 5：追问 N 上界 |
| 弱相关，个别帧单次飙升 | **单点异常型** | Step 6：拆异常帧子树 |
| 强相关，个别帧单次也飙升 | **混合型** | Step 5 + Step 6 |
| 无相关 | 倾向外部干扰或 N 选错 | 先校验 N，再查整帧上下文 |

### 双层诊断：避免二选一陷阱

混合型必须分两层陈述：

#### 第 1 层：结构性问题

函数是 O(N) 批量放大器。

```text
健康帧成本 ≈ N × 单元成本
```

即使没有异常，N 大时也会卡。

#### 第 2 层：异常帧问题

异常帧中单元成本被 K 倍放大：

```text
异常帧成本 ≈ N × (K × 单元成本)
```

最终被 N 次循环放大成尖峰。

### 输出顺序取决于用户问题

如果用户问：

> 这个函数是否有架构风险？

先答结构性 O(N)，再答异常放大器。

如果用户问：

> 为什么 max 帧这么慢？

先答异常帧放大器，再补结构性 O(N)。

但无论顺序如何，两层都必须给。

---

## Step 5 — O(N) / 混合型后，强制追问 N 的系统上界

这一步不能跳。  
跳过会把“暂时还行”误报成“无病”，把会随配置 / 数据规模线性恶化的炸弹放过。

### 5.1 不要把 trace 里观测到的 max(N) 当系统上限

utrace 只是一段录像。  
这次录到：

```text
max observed N = 31
```

不代表系统永远跑不到：

```text
N = 100 / 200 / 300
```

N 的真实上界来自：

- 配置项；
- 数据表；
- 场景密度；
- Spline 容量；
- 上层调用循环；
- 多个调用源同帧叠加；
- 玩家行为；
- 动态刷怪规则。

### 5.2 必须回答三问

#### 问题 1：N 是谁传进来的？

用 grep 找 caller：

```bash
rg "PrepareFillNPCDataByNum|FunctionName" Source/ Plugins/
```

关注：

```cpp
Func(DataNum);
```

以及上层是否还有循环：

```cpp
for (auto& Pair : ApplySplineNumMap)
{
    PrepareFillNPCDataByNum(Pair.Value);
}
```

如果上层是多次调用，单帧实际 N 可能是：

```text
N_frame = Σ N_each_call
```

不要只看单次调用的 N。

#### 问题 2：谁限制 N？容量还是预算？

在源码里找：

- `Clamp`
- `Min`
- 配置项
- `MaxNum`
- `Limit`
- `Budget`
- `PerFrame`
- `Queue`

必须区分：

```text
容量限制：最多能有多少
单帧预算：一帧最多处理多少
```

常见错误：

> `Clamp(0, SplineMaxNum)` 限制的是 spline 上 NPC 总数，不是一帧最多 spawn 多少。

把容量当预算用，是典型炸帧风险。

#### 问题 3：如果 N 翻 2× / 5× 会怎样？

用健康帧斜率外推。

示例：

```text
N = 31 实测 3.4ms
单个成本 ≈ 110us
```

外推：

| N | 预测耗时 | 60 FPS 帧预算占比 |
|---:|---:|---:|
| 31 | 3.4ms | 20% |
| 100 | ~11ms | 66% |
| 200 | ~22ms | 超预算 |
| 300 | ~33ms | 严重炸帧 |

如果上限配置允许达到这些 N，即使当前样本“看起来还行”，也要报为架构风险。

### 5.3 无法读源码时怎么办

如果暂时无法读源码，不要默认 trace 中 max(N) 是上限。

结论中明确写：

```text
当前 trace 观测最大 N = xx；
系统真实上界未知；
需要代码确认 N 来源、Clamp、配置项、上层调用循环；
因此 O(N) 外推风险为待验证。
```

---

## Step 6 — 单点异常 / 混合型：拆飙升帧子树

先跑摘要脚本，不要手工 Read 巨大 JSON：

```bash
python <SKILL_DIR>/scripts/summarize_frames.py "<output>.json" \
    --top-frames 5 \
    --top-subs 15
```

摘要通常看三块：

1. Top 慢帧子调用聚合：每帧内 sub-timer 的 count + sum_us。
2. 跨帧全局子 timer 热点：所有捕获帧打通后的 sum_us 排序。
3. Log 频次摘要：按 Category + 消息模板聚合。

---

### 6.1 Inclusive time 读法

摘要里的 `sum_us` 是 inclusive 时要特别小心：

不能这样加：

```text
SpawnNPC.sum + RequestAppearanceAsync.sum + FMsgLogf.sum
```

因为它们可能在同一条调用链上，会重复计数。

正确读法：

```text
Prepare 11.3ms
  -> 几乎全在 SpawnNPC
    -> 几乎全在 RequestAppearanceAsync
      -> 主要由 FMsgLogf 覆盖
```

这是一条主链路，不是并列加法。

如果需要 self-time / exclusive-time，需要逐节点：

```text
self_time = node_time - sum(children_time)
```

脚本若未提供 exclusive-time，就不要假装有。

---

### 6.2 同 N 对照是异常诊断金标准

要判断“异常帧是不是算法退化”，必须找一对 N 完全相同或接近的帧。

推荐：

```text
N 完全相同最好；
N 差异在 ±10% 内可以接受；
找不到就明确标注“无法做同 N 对照”。
```

示例：

```text
异常帧: N=27, 总耗时=11.3ms, 单次均值=410us
健康帧: N=27, 总耗时= 2.4ms, 单次均值= 86us
```

控制变量后，看每个 sub-timer 的单次放大倍数：

| 现象 | 解释 |
|---|---|
| 所有 sub-timer 单次同步放大 5~7× | 全局抖动 / 外部放大器 |
| 只有单一 sub-timer 飙升，其他正常 | 该子调用内部有病 |
| 单次成本差不多但 N 差很多 | 结构性 O(N)，不是异常 |
| mean 高但 median 正常 | 少数 item 长尾 |

---

### 6.3 零假设证伪：占比高不等于根因

看到某子项占比高，先做减法：

> 如果它归零，剩余耗时还能解释整体差异吗？

判断：

| 减法结果 | 解释 |
|---|---|
| 归零后差异基本消失 | 该子项是主要根因 |
| 归零后仍有明显差异 | 该子项是放大器之一，还有其他因素 |
| 归零后只解释少量差异 | 它主要是症状或高频小项 |

示例：

```text
异常帧 11.30ms
健康帧  2.40ms
异常帧日志 9.18ms
健康帧日志 1.45ms
```

日志额外贡献：

```text
9.18 - 1.45 = 7.73ms
```

异常额外总差异：

```text
11.30 - 2.40 = 8.90ms
```

说明日志解释了大部分差异，但如果扣掉日志后仍有差距，也要继续查其他放大器。

### 零假设减法的前提

- 分子和分母必须来自同一时间窗；
- 不跨层级重复计数；
- 同名 timer 如果出现在多个分支，要确认统计范围；
- 如果无法确认，标注为“近似估算”。

---

### 6.4 Log 与 timer 时间窗对齐

如果 JSON 里有 `Log[]`，可以把 `STAT_FMsgLogf` 和具体日志对齐。

不要要求 Time 完全相等。建议：

1. 以某个 `STAT_FMsgLogf` 的时间窗为准：

```text
[Time, Time + useTime]
```

2. 查找 `Log[]` 里落在该窗口附近的记录。
3. 允许微小偏差，例如 ±50us，具体取决于 trace 精度。
4. 多条 Log 落在同一窗口时，只能说“高度相关”，不要强行一一对应。

对齐结果用于增强证据，不应单独作为唯一根因证据。

---

### 6.5 FMsgLogf 慢的分层解释

看到 `STAT_FMsgLogf` 慢，只能先说：

```text
日志路径放大
```

不要直接跳到：

```text
磁盘 IO 卡
```

日志慢可能来自多层：

#### 第 1 层：调用侧开销

- 格式化字符串；
- 参数 `ToString`；
- `FString::Printf`；
- 临时内存分配；
- 动态 trace scope 名称。

#### 第 2 层：UE 日志系统

- `FOutputDeviceRedirector`;
- 日志锁；
- `FlushBufferedItems`;
- Category / verbosity；
- OutputDevice 分发。

#### 第 3 层：日志后端

- 文件写入；
- 控制台输出；
- Session Frontend；
- 远端日志转发；
- stdout/stderr 重定向。

#### 第 4 层：外部环境

- 磁盘 IO；
- 杀毒软件；
- 远端采集；
- 系统调度；
- 其他线程锁竞争。

只有看到 `FlushBufferedItems`、OutputDevice、文件写入、IO 等证据后，才能进一步指向具体日志后端。

---

### 6.6 埋点本身也可能有开销

检查热路径里是否存在：

- `FString::Printf`;
- 动态 trace scope 名称；
- `SCOPED_NAMED_EVENT_FSTRING`;
- 每个元素循环里构造调试字符串。

原则：

- 高频路径优先使用静态 scope 名；
- 动态字符串放在宏开关或 trace channel enabled 判断之后；
- 不要在每个 item 循环中构造调试字符串；
- trace 关闭时不应仍然构造字符串。

---

## Step 7 — 回查完整帧和跨线程上下文

裁剪后的函数 JSON 只能说明：

```text
目标函数内部看到什么变慢
```

不能证明：

```text
真正根因一定在目标函数内部
```

异常帧必须回完整 utrace，看同一时间窗。

### 7.1 GameThread

查：

- GC；
- `FlushAsyncLoading`;
- `Wait`;
- 锁等待；
- Tick 群组；
- 资源创建；
- 大量日志 flush；
- TaskGraph 等待。

### 7.2 AsyncLoadingThread / IO

查：

- 包加载；
- 资源反序列化；
- StaticMesh / SkeletalMesh / Texture 加载；
- IO Dispatcher；
- 同步读；
- BulkData 加载。

### 7.3 RenderThread / RHIThread

查：

- 同步栅栏；
- 资源创建；
- shader / PSO；
- render command flush；
- GameThread 等 RenderThread。

### 7.4 Log / OutputDevice

查：

- `FOutputDeviceRedirector::FlushBufferedItems`;
- OutputDevice 锁；
- 文件日志；
- 远端日志通道；
- 控制台输出。

### 7.5 判断规则

| 现象 | 倾向 |
|---|---|
| 多线程同一时间窗一起尖峰 | 全局抖动 / 外部因素 |
| 只有目标函数子树尖峰 | 函数内部路径问题 |
| 日志节点和 OutputDevice/Flush 同时尖峰 | 日志后端放大 |
| AsyncLoading 与目标函数资源请求重叠 | 资源加载放大 |
| GameThread 等待其他线程 | 同步屏障或锁问题 |

---

## Step 8 — 输出双层诊断

最终输出建议按这个结构：

```markdown
## 结论
一句话说明形状：O(N) / 单点异常 / 混合 / 外部干扰。

## 证据
- meta 分布
- N 回归表
- 同 N 对照
- 子树主链路
- 零假设减法
- 整帧上下文

## 双层诊断
### 第 1 层：结构性问题
...
### 第 2 层：异常放大器
...

## 证据强度
- 确定
- 高概率
- 待验证

## 优化建议
按层分组。

## 验证方式
A/B trace 指标。
```

---

### 8.1 证据强度分级

性能诊断里很多结论不是 100% 确定，建议显式标注。

#### 确定

JSON 直接可见。

例：

```text
frame 6368 中 PrepareFillNPCDataByNum = 11.3ms。
final num = 27。
FMsgLogf 单条耗时明显高于健康帧。
```

#### 高概率

由同 N 对照、时间窗相关性、调用链强支持。

例：

```text
日志路径是主要放大器之一。
RequestAppearanceAsync cache 命中时可能同步回调。
```

#### 待验证

需要完整 utrace、源码或 A/B 实验确认。

例：

```text
具体是否磁盘 IO、远端日志转发、GC、AsyncLoading 造成。
```

---

### 8.2 改进建议按层分组

#### 第 1 层：结构性 O(N) 问题

常见建议：

- 限制单帧 N；
- 分帧 drain 队列；
- 时间预算 + 数量预算双约束；
- 减少调用源；
- 批量化 API；
- 预计算 / 预热；
- 避免 cache 命中同步连锁回调。

示例：

```text
每帧最多处理 3~5 个；
或每帧最多 0.75~1.5ms；
剩余进入 Pending Queue。
```

#### 第 2 层：异常放大器问题

常见建议：

- hot path 日志降级到 `Verbose` / `VeryVerbose`;
- 性能包默认关闭 Routine 日志；
- 每 NPC 日志改批次摘要；
- 避免日志后端同步 flush；
- 排查 OutputDevice 锁；
- 排查 GC / IO / AsyncLoading；
- cache 命中回调也 defer；
- 资源提前预热。

批次摘要日志示例：

```text
PrepareFillNPCDataByNum Summary:
  SplineID=183
  FinalNum=27
  Spawned=27
  SyncAvatarReady=27
  AsyncLoadRequest=0
  NewPartRef=8
  CostMs=11.3
```

---

## Step 9 — A/B 验证闭环

每个优化建议都要能通过重抓 trace 验证。

没有 A/B 验证，只能说：

```text
预期收益
```

不能说：

```text
已解决
```

### 9.1 关闭 hot path 日志前后

对比：

- `FMsgLogf` count / sum_us；
- 目标函数 p50 / p90 / max；
- 同 N 下 per-item 成本；
- 异常帧是否消失；
- 日志后端 flush 是否下降。

### 9.2 分帧预算前后

对比：

- 单帧最大 N 是否下降；
- 单帧目标函数 max 是否下降；
- Pending 队列长度；
- Pending 最大延迟；
- NPC 出现是否有可见 pop-in；
- 总吞吐是否满足需求。

### 9.3 资源预热前后

对比：

- `AsyncLoadAsset_Internal` count / sum_us；
- 首次进入区域 spike；
- cache miss 次数；
- 资源加载是否前移到安全时间窗。

### 9.4 Defer callback 前后

对比：

- 同一帧同步 `OnXxxReady` 次数；
- 回调队列 drain 数量；
- 单帧 callback max；
- 业务延迟是否可接受。

---

## 常用结论模板

### O(N) 累积型

```text
该函数当前表现为 O(N) 累积型热点。单次成本相对稳定，总耗时主要由 N 决定。
当前 trace 观测最大 N=xx 时耗时 yy ms；但 trace 中的 max(N) 不是系统上限。
需要确认 N 的来源、配置上限、上层是否多次调用，以及是否存在单帧预算。
如果 N 扩大到 2×/5×，预计耗时将线性增长，存在炸帧风险。
```

### 单点异常型

```text
该函数大部分帧正常，但 frame X 出现单点异常。
同 N 对照显示，异常帧中单次成本从 xx us 放大到 yy us。
子树显示主要放大点在 A -> B -> C。
当前证据支持 C 是主要放大器；具体是否由 GC/IO/锁/日志后端造成，需要回完整 utrace 验证。
```

### 混合型

```text
这是混合型问题。

第 1 层：结构性 O(N)。
健康帧中耗时随 N 线性增长，说明该函数是一帧批量处理的放大器。N 大时即使没有异常也会产生数 ms 成本。

第 2 层：异常帧放大器。
异常帧中，在相同/接近 N 下，单次成本被 K 倍放大，最终被 N 次循环乘起来形成尖峰。

因此不能二选一地说“是 N 问题”或“是日志/IO/GC 问题”。两层都要修：
- 限制 N / 分帧，解决结构性放大；
- 移除 hot path 高频小成本 / 查异常放大器，解决长尾。
```

### 外部干扰型

```text
当前目标函数耗时与 N 无明显相关，且异常帧整帧也明显变慢。
这倾向于外部干扰或全局抖动，而不是函数内部算法退化。
但需要先确认 N 是否选对；若 N 无误，应回完整 utrace 查看 GC、IO、AsyncLoading、锁、Render/RHI 同步、Log flush 等上下文。
```

---

## 禁止事项

- 原样贴大段 JSON。
- 只说“max 帧很卡”，不解释形状。
- 只看占比，不做同 N 对照和零假设证伪。
- 把观测 max(N) 当系统上限。
- 把 `总耗时 / N` 叫“中位数”。
- 把 inclusive 的跨层 `sum_us` 相加。
- 被用户问句诱导成单一根因。
- 从 `FMsgLogf` 慢直接跳到“磁盘 IO 卡”，没有中间证据。
- 用裁剪函数 JSON 直接证明完整帧根因。
- 编造 JSON 里没有的数字。
- 不标注待验证假设。
- 没有 A/B trace 就宣称优化已解决。

---

## 决策树速查

```text
Step 0：样本可信度
  │
  ├─ 样本太少 / top_n 不覆盖全部
  │    └─ 标注低置信度，只做案例分析
  │
  └─ 样本可用
       │
       ▼
Step 1：读 meta
  │
  ├─ 稳定但绝对耗时高
  │    └─ 稳定热点，直接给优化方向
  │
  ├─ 跨度小且绝对耗时低
  │    └─ 可判无明显问题
  │
  └─ 跨度 ≥ 2~3× 或 mean >> p50
       │
       ▼
Step 2：抽 N，并校验 N
       │
       ▼
Step 3：按 N 升序建回归表
       │
       ▼
Step 4：判形状
  │
  ├─ O(N) 累积型
  │    └─ Step 5：追问 N 上界 + 外推
  │
  ├─ 单点异常型
  │    └─ Step 6：同 N 对照 + 子树 + 零假设
  │
  ├─ 混合型
  │    └─ Step 5 + Step 6：结构性风险 AND 异常放大器
  │
  └─ 无相关
       └─ 先确认 N 是否选错，再查整帧上下文
       │
       ▼
Step 7：回完整 utrace 看跨线程 / 整帧上下文
       │
       ▼
Step 8：双层输出 + 证据强度
       │
       ▼
Step 9：A/B trace 验证闭环
```