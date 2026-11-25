# 公共自行车调拨装笼采录台

给公共自行车站点调度员与仓门交接员用的离线命令行账本。它保存车桩退租回执、车型与可借认定、空位与预约、共享笼位清单和两端扫描。仓门签收会结束对应车的调拨保管，公共停车需要独立的车桩退租证据。全部样例均为合成记录。

## 运行

在项目根目录使用 Python 3.10+ 和内置 SQLite。把项目目录作为模块搜索入口，无需安装依赖。

```powershell
python -B -m bike_rebalance demo --db shift.sqlite
python -B -m bike_rebalance audit --db shift.sqlite
python -B -m unittest discover -s tests -v
```

`demo` 要求空数据库。它把 `fixtures/shift.json` 和 `fixtures/late.json` 依次提交到同一个数据库，两次都观察第 40 分钟。演示输出显示两个版本，SQLite 留存两批原文、各实体当前状态和版本差异。

```powershell
python -B -m bike_rebalance fleet --db shift.sqlite
python -B -m bike_rebalance supply --db shift.sqlite
python -B -m bike_rebalance cage --db shift.sqlite
python -B -m bike_rebalance history --db shift.sqlite manifests M-cargo
python -B -m bike_rebalance rebuild --db shift.sqlite --at 24
python -B -m bike_rebalance rebuild --db shift.sqlite --at 40
python -B -m bike_rebalance audit --db shift.sqlite
```

还可用 `init --db FILE` 建空库，`ingest --db FILE INPUT [--at MINUTE]` 采录混合批，`status --db FILE` 查看完整账。三个业务命令也能独立采录，各自接受 `--input INPUT [--at MINUTE]` 与所属事件类型。`audit` 为只读，发现证据或账本差异返回 1。输入、文件或数据库错误返回 2。

## 采录格式

UTF-8 JSON 非空数组。每条记录有唯一的 `id`、`kind` 和非负整数 `at`。`at` 是合成班次内的有效分钟，导入先后由 SQLite 版本记录。

| `kind` | 数据 | 命令 |
| --- | --- | --- |
| `return` | `vehicle station outcome`，结果为 `docked` 或 `uncertain` | `fleet` |
| `check` | `vehicle type cleared`，车型为 `city`、`electric` 或 `cargo`，认定为布尔值 | `fleet` |
| `space` | `station free`，班次分钟起点观测到的物理空位 | `supply` |
| `reservation` | `station mode begin end type`，模式为 `bike` 或 `dock`，后者车型为 `null` | `supply` |
| `cancel` | `reservation` | `supply` |
| `redeem` | `reservation proof`，取车时 `proof` 为采录车号，停车时为相应车桩回执编号 | `supply` |
| `cage` | `slots`，注册事件的 `id` 即笼号，容量 1–24 | `cage` |
| `manifest` | `cage origin gate depot depart due items`，站点、来源仓门、目的仓门各自独立，`items` 为 1–12 项有序车型数组 | `cage` |
| `scan` | `manifest vehicle stage channel site`，阶段为 `load` 或 `unload`，通道为 `gate` 或 `public` | `cage` |
| `redirect` | `manifest vehicles from_depot to_depot`，改卸收据圈定清单、车号数组、原接收方与新接收方，双方必须不同 | `cage` |
| `void` | `manifest` | `cage` |

同分钟依次处理笼注册、空位、车型认定、退租、装笼、改卸收据、卸笼、预约、取消、核销、作废、清单申请，同类按事件编号。同一分钟的同车认定或退租地点冲突会冻结该类证据结论，待下一条有效证据解除。相同站点的同分钟空位冲突会停止新空位预约。预约、清单、重算的历史以提交顺序保留。未给 `--at` 时取已有观察分钟和已采录事件分钟的最大值。给定时点后可向前或向后观察，证据原件保留。

### 改卸交接的可重放裁决

改卸收据只能由当时对每辆被列车持有接收责任的一方（`from_depot`）发出，且每辆车都必须已有有效装笼凭证、尚未被任何仓签收。同一分钟的全部收据先按改卸前的责任快照统一授权：快照里没有在途保管状态（含本分钟才装笼）的车一律 fail-closed 拒绝。批量收据中任一辆车不满足前置条件，整单记为 `REDIRECT_REJECTED`，其中任何车的责任都不转移；逐车原因写入 `failures`。

同一分钟内多份各自合法、车号交叠的收据，先各自对照同一份改卸前责任状态取得授权，再按事件编号顺序确定唯一命中：第一个声明某车的收据赢得该车，后到的交叠收据整单记为 `REDIRECT_CONFLICT` 并注明命中收据，保证任一车始终至多有一个有权签收的仓。裁决在同分钟装笼之后、卸笼扫描之前生效，因此同分钟的仓门扫描按裁决后的责任判定。

每次仓门卸笼扫描都必须落在该车当时可追溯的接收责任上：扫描仓门与 `receiver` 一致才可能有效，改卸后原仓再扫记 `GATE_NOT_RESPONSIBLE`（未改卸过的车沿用 `WRONG_DESTINATION_GATE`），扫描行同时回写 `responsible_depot` 说明命中仓或等待原因。有效签收经 `Fleet.begin_receipt` 核对在途保管后，才把车放到该仓并释放笼位；未签收、无效或待裁定的扫描不释放笼位。在途明细行保留完整 `redirects` 收据链，调度员在同一份清单里即可查出每辆车当前由哪边负责接收。迟到但有效分钟更早的证据重新导入或重算时，按有效分钟重放并自动改判；没有改卸收据的旧输入保持原有扫描与迟到口径。

## 现场判定

- **车辆与退租**。同站重确认位置不会额外扣空位。同站重复回执保留为 `ALREADY_PARKED`。不确定回执撤回当前公共停车认定，笼内车的公共停车回执标记 `IN_CUSTODY`。只有具备当前公共停车位置和有效车型认定、且未被占用的车进入可预约供给。
- **空位与预约**。开点空位快照在第 10 分钟内可用，之后需要新快照。其后确认的到站、骑出和装笼更新空位，无法确认的移动等待新采录。空位预约按半开窗口最大同时占用量核定。取车预约自受理起保留指定车到取消、核销或到期。后续现场失格时标记 `AT_RISK`，仍保留车号以便处理。核销须在预约窗口内，并核对可借车辆或此前未核销的独立退租回执。
- **装笼和归库**。同笼可由同一车次的多份单共享，车次以起点、来源仓门、目的仓门和出发分钟标识。货运车占两位，其余车型占一位。清单优先完整覆盖，再依次最少替代、最低替代顺位总和、最少物理位置、车号字典序。未配齐时整单不占位。已承诺位置在各车卸笼或全单未开装时人工撤销后释放。超期后继续保留物理占用。有效卸笼须晚于同车来源装笼，实际卸笼晚于到期分钟才是归库迟到。

## 样例与重算

| 本地样例 | 可观察结果 |
| --- | --- |
| `M-cargo`，`M-flex`，`M-full` | 货运占两位、电助力替代占一位，第三单因笼位不足拒绝，随后 R-flex 无电助力车可约。 |
| `G-cargo-receive` 与第二批来源装笼 | 缺来源的卸笼在补证后认定按时归库。`M-flex` 补录的卸笼实际发生在到期后。 |
| `R-check`、`M-check` | 晚录的停借认定令预约改绑另一辆车，后续清单因此无可用车。 |
| `M-reuse`、`R-mid` | 正式归库释放共享笼，下一车次先占用最后一辆空闲车，于是暂时无法受理取车预约。 |

每批 SQLite 事务写入原文件与 SHA-256、规范事件、文件归属、业务实体及版本差异。同一事件编号可复用相同内容，冲突内容会回滚整批。`rebuild` 从规范事件重放。`audit` 从保留的原文件再次解析，按历次可见文件和时点重放，并校对文件索引、当前实体以及每次变化。
