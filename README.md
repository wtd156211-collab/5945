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

## 统计报告与事件日志

两份输出的格式由实现决定，定下来后补在这里：字段清单、单位、排序方式，以及事件日志里每条记录的字段与时间戳语义。

## 运行方式

```bash
# 一口气跑完
python3 -m sim run samples/hub.json -o out/hub

# 断点续跑：先在虚拟时刻 3600s 停住并导出快照，再从快照接着跑
python3 -m sim run samples/hub.json -o out/hub --stop-at 3600 --snapshot-at 3600
python3 -m sim run --resume out/hub/snapshot.json -o out/hub

# 覆盖场景自带种子（仅全新运行可用，续跑的种子来自快照）
python3 -m sim run samples/hub.json -o out/hub --seed 42

# 测试
python3 -m unittest discover -s tests
```

每次运行在输出目录写 `events.csv` 和 `report.json`，加 `--snapshot-at` 时写 `snapshot.json`。续跑时 `events.csv` 以追加方式接着写，因此分段跑完的文件与一口气跑完逐字节一致。运行结束在标准输出打印 `events=<事件数> wall=<耗时> rate=<事件/秒>`，用于观察瓶颈（仅标准库实现，本机约 90 万事件/秒）。

## 确定性规则

- **虚拟时间**：内部时间全部是整数微秒（`1 秒 = 1_000_000 us`）。所有从分布采样得到的时长（到达间隔、加工时长）在采样后立即四舍五入量化到微秒，事件堆只做整数比较，不依赖浮点边界行为。
- **随机源**：全引擎只有一个可注入的 `random.Random(seed)`，到达间隔、加工时长、返修判定都从这里取数；任何地方不碰全局 `random` 状态（有测试保证）。取数顺序由事件处理顺序唯一确定。
- **同刻事件顺序**：堆元素为 `(time_us, event_kind, seq, payload)`。时间相同先按 `event_kind` 的固定优先级，再按入堆序号 `seq` 先进先出：
  1. `service_end`（先释放台位）
  2. `arrival`
  3. `downtime_start`
  4. `downtime_end`

  `payload` 不参与比较，规则与字典序、哈希序无关。
- **快照语义**：`--snapshot-at T` 表示"处理完所有时间戳 ≤ T 的事件之后"的状态；快照里的事件堆只含时间戳 > T 的事件。快照完整保存 RNG 状态、事件堆、各工位队列、计数器（含日志行号、事件计数）和统计累加器，因此续跑与一口气跑完的事件序列、随机数流、统计结果完全一致（有逐字节对比的测试）。

## 输出格式

### `events.csv`（事件日志）

CSV，首行为表头，之后每行一条日志记录，`seq` 从 0 连续递增，`time_us` 单调不减。全部字段为整数或固定词表，不含浮点数，可直接 diff 对拍。

| 列 | 含义 |
|---|---|
| `seq` | 日志行序号，从 0 连续递增（续跑时接着编号） |
| `time_us` | 事件发生的虚拟时间，整数微秒 |
| `event` | `arrival` / `service_start` / `service_end` / `exit` / `downtime_start` / `downtime_end` |
| `station` | 工位 id；`exit` 事件为空 |
| `job_id` | 作业编号（按到达顺序从 1 开始）；停机事件为空 |
| `detail` | `key=value` 对，以 `;` 分隔：`service_start` 带 `wait_us`、`dur_us`；`service_end` 带 `to=<下一站>`（`to=rework:<id>` 表示返修，`to=exit` 表示离开）；`exit` 带 `tis_us`（系统内停留时长）；`downtime_start` 带 `duration_us` |

### `report.json`（统计报告）

JSON，键序固定，浮点数保留 6 位小数。顶层字段：

| 字段 | 含义 |
|---|---|
| `scenario` / `seed` | 场景名与实际使用的种子 |
| `simulated_seconds` | 本次仿真的虚拟时长（秒） |
| `events_processed` | 处理的事件总数（续跑时累计，与一口气跑完相同） |
| `jobs` | `arrived` / `completed` / `in_system_at_end`，以及已完成作业的 `time_in_system_seconds`（`mean`、`max`，秒） |
| `stations[]` | 按场景文件中工位顺序排列，每个工位：`servers`、`utilization`（= 累计加工时长 ÷ (`servers` × 仿真时长)）、`busy_seconds`、`jobs_started`、`jobs_completed`、`backlog_peak`（排队等待件数峰值，不含在制）、`wait_seconds`（每次上工的排队等待时长：`count`、`mean`、`p50`、`p90`、`p95`、`p99`、`max`，单位秒；分位数用排序后线性插值法） |

### `snapshot.json`（快照）

JSON，`format="gsb-sim-snapshot"`、`version=1`。字段：`scenario`（完整场景，自包含）、`seed`、`sim_time_us`、`counters`（`event_seq`/`job_seq`/`log_seq`/`events_processed`）、`rng_state`（`random.Random.getstate()` 的 JSON 形式）、`heap`（未来事件）、`stations`（各工位等待队列、在制台数、停机标记）、`jobs`（在系统内作业的到达时间）、`stats`（统计累加器）。结构有测试锁定。
