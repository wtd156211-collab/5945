# 枢纽与产线节拍评估仿真

我们给客户的枢纽和产线做节拍评估。以前是一份 Excel 加一堆宏，后来同事写了个 Python 脚本，跑一次要一个通宵，而且同一份输入跑两遍数字还对不上，跟同事核对的时候谁也说不清是谁算错了。这次重做，目标很明确：把场景文件喂进去，出来一份能对得上的结果。

时间必须是虚拟的：跑一年的仿真不许真的等一年，也不许靠 sleep 或者系统时钟推进，事件按时间先后从堆里弹出来。最看重的是可复现：同一个场景加同一个种子，跑两遍结果逐字节一致，换台机器也一致，所有随机都走同一个可以注入的随机源，不许有地方偷偷用全局随机；时间相同的多个事件谁先谁后要有明确规则，并且写进文档，不能靠字典序或者碰运气。另一条是断点续跑：大场景一次跑不完，要能在某个时刻导出快照、之后从快照接着跑，最后的统计结果必须和一口气跑完完全一致——以前那套就是在这一步对不上。规模上，一个场景几十万个事件要能跑完，不能每一步都对整个事件表排序；跑完把耗时和事件数打出来，方便看瓶颈在哪。

输出两份东西：一份统计报告，含每个资源的使用率、等待时长的分位数和积压峰值；一份完整的事件日志，能拿去跟别人对拍。两份的格式由实现决定，定下来之后写在 README 里。

仓库现在只有这份说明和 samples/ 下的两个场景，代码从零写。实现只用标准库，测试用 unittest；快照和事件日志的格式要有测试保证，续跑一致这条要有真的测试用例，不能只是 dump 出来看看。

## 场景文件

场景是 JSON，字段含义如下（结构以 samples/ 下的两个文件为准）：

| 字段 | 含义 |
|---|---|
| `name` | 场景名，只用于输出 |
| `seed` | 场景自带的随机种子；运行时允许用参数覆盖，随机源必须可注入 |
| `horizon_seconds` | 仿真时长（虚拟秒），到点结束 |
| `arrival.process` | 目前只有 `poisson`：按 `rate_per_hour`（小时到达率）生成到达 |
| `arrival.start_seconds` / `arrival.stop_seconds` | 到达窗口，窗口之外不再产生新作业 |
| `stations[].id` | 工位/资源名 |
| `stations[].servers` | 该工位的并行台数 |
| `stations[].service` | 单件加工时长分布：`constant`（`seconds`）、`uniform`（`min_seconds`~`max_seconds`）、`exponential`（`mean_seconds`） |
| `stations[].next` | 加工完流向的下一个工位，`null` 表示离开系统 |
| `stations[].rework` | 可选，加工完成后按 `probability` 回到 `target` 工位重新排队 |
| `downtime[]` | 计划停机：`station` 在 `start_seconds` 起停止派工 `duration_seconds`；停机期间不开始新的加工，已经开工的那一件按原时长做完 |

两个场景的分工：`samples/hub.json` 是小型枢纽（卸货、分拣、装车三级、约 6300 件到达），用来快速验证正确性和续跑一致性；`samples/line.json` 是 24 小时产线（六个工位、含返修与两段计划停机、约 5.7 万件到达），用来验证几十万事件规模下的耗时和稳定性。实际事件数取决于实现里事件的划分方式。

## 用法

```bash
# 跑完整仿真：输出统计报告和完整事件日志
python3 -m sim run samples/hub.json --report report.json --log events.jsonl

# 覆盖场景自带种子（随机源可注入，代码里也可直接传 random.Random 实例）
python3 -m sim run samples/hub.json --seed 42 --report report.json

# 断点：处理完所有 time <= T 的事件后导出快照并停止
python3 -m sim run samples/hub.json --log events.jsonl --snapshot-at 14400 --snapshot-out snap.json

# 续跑：从快照恢复，日志追加到同一份文件，最终报告与一口气跑完逐字节一致
python3 -m sim resume snap.json --log events.jsonl --report report.json
```

每次运行结束在 stdout 打印 `events=<处理的事件数> wall_time=<真实耗时>`，用于定位性能瓶颈。这两个值不进报告文件——报告内容必须可复现，而耗时每次都不一样。

## 可复现约定

- 时间完全是虚拟的：事件按 `(time, 类型优先级, 序号)` 从最小堆弹出，不用 sleep、不读系统时钟。
- 所有随机（到达间隔、加工时长、返修判定）都走同一个可注入的 `random.Random` 实例；任何地方不碰全局随机状态。快照会完整保存/恢复 RNG 状态。
- 同一时刻多个事件的先后顺序是固定规则：**downtime_start < service_end < downtime_end < arrival**，同类事件按入堆序号先进先出。不靠字典序、不靠碰运气。含义：停机先生效（停机边界那一瞬不再派工），完工释放的台位先于新到达处理。
- 到达窗口为闭区间 `[start_seconds, stop_seconds]`；首个到达时刻 = `start_seconds + Exp(rate)`。作业从 `stations` 列表的第一个工位进入系统。
- 停机语义：`[start, start+duration)` 内不开始新加工；已开工的件按原时长做完；停机结束那一刻恢复派工。
- 浮点经 JSON 序列化用最短往返表示（`repr`），写出去再读回来 bit 级不变，这是快照无损的前提。

## 统计报告（JSON，`format: "sim-report"`）

| 字段 | 含义 |
|---|---|
| `scenario` / `seed` / `horizon_seconds` | 场景名、实际使用的种子、仿真时长（虚拟秒） |
| `events_processed` | 处理的事件总数 |
| `jobs.arrived / departed / in_system` | 到达数、离开系统数、结束时仍在系统内数 |
| `stations.<id>.servers` | 并行台数 |
| `stations.<id>.jobs_enqueued / started / completed` | 进入队列 / 开工 / 完工件数 |
| `stations.<id>.utilization` | 使用率 = 台位占用时长 ÷ (`servers` × `horizon_seconds`)，范围 [0, 1] |
| `stations.<id>.downtime_seconds` | 计划停机总时长 |
| `stations.<id>.queue_peak` | 积压峰值（队列中等待件数的最大值） |
| `stations.<id>.wait_seconds` | 排队等待时长统计：`count / mean / p50 / p90 / p95 / p99 / max`，单位秒；只统计已开工的件，分位数用线性插值法（位置 = `(n-1) * p`） |

`stations` 按场景文件里的工位顺序排列。报告里不含任何真实时钟信息，同场景同种子重跑逐字节一致。

## 事件日志（JSON Lines，每行一条记录）

公共字段：`seq`（从 0 连续的日志序号）、`time`（虚拟秒，单调不减）、`event`（事件名）。其余字段按事件类型：

| `event` | 额外字段 | 含义 |
|---|---|---|
| `arrival` | `job` | 作业进入系统 |
| `enqueue` | `job`, `station`, `queue` | 进入某工位队列，`queue` 为入队后的队列长度 |
| `service_start` | `job`, `station`, `wait`, `duration` | 开工；`wait` 为本次排队时长，`duration` 为抽到的加工时长 |
| `service_end` | `job`, `station` | 完工 |
| `rework` | `job`, `station`, `target` | 判定返修，流向 `target` |
| `depart` | `job`, `station` | 离开系统 |
| `downtime_start` / `downtime_end` | `station` | 计划停机开始 / 结束 |

键按字典序输出（`json.dumps(sort_keys=True)`），浮点用最短往返表示。日志可整份 diff 对拍；续跑时追加写入，拼接结果与一次跑完逐字节一致。

## 快照（JSON，`format: "sim-snapshot"`）

`--snapshot-at T` 表示：处理完所有 `time <= T` 的事件后停机并导出。快照是自包含的，字段：`scenario`（内嵌完整场景）、`seed`、`clock`、`seq`、`log_seq`、`next_job_id`、`events_processed`、`rng_state`（完整 RNG 状态）、`event_heap`（待处理事件堆，规范化排序）、`stations`（每台位的 `busy / down / down_since / queue`）、`jobs`（系统内作业）、`stats`（全部统计累加器，含等待时长明细）。续跑不需要原始场景文件，也不能换种子。恢复后重新导出的快照与原快照逐字节一致（有无损往返测试保证）。

## 测试

```bash
python3 -m unittest discover -s tests
```

覆盖：同种子逐字节复现、全局随机不泄漏、种子覆盖、注入 RNG、hub/line 两个场景的续跑一致（含快照点落在停机区间内的情况）、快照 schema 与无损往返、事件日志 schema 与单调性、同刻事件优先级、停机/返修语义、报告字段与分位数。
