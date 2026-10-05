# 乐曲速度图换算 API（Tempo Map Converter）

用 **Python + FastAPI** 实现的乐曲速度图换算服务：在乐谱位置
`小节:拍:tick` 与时间（秒）之间双向换算，支持 **PPQ**、**只在小节首拍
生效的拍号表**，以及 **阶梯（step）/ 线性（linear）BPM** 速度点。

在此基础上还提供**带录音锚点的谱例修订发布**：创建谱例（速度图 +
锚点）后，可预览/发布整份替换速度点表，锚点秒数随版本演进，锁定锚点
受“一 tick 时长”漂移约束保护；谱例持久化到 **SQLite**，重启后仍可读取。

- 位置中的“拍”按**当前拍号分母**对应的音符计（6/8 中一拍 = 八分音符）；
- 内部统一展开为“距乐谱起点的 **tick 坐标**”（PPQ：每四分音符的 tick 数）；
- 线性速度段按解析积分换算，反向换算在**同一速度段内解析求逆**；
- 正反向边界归属一致（边界 tick 归后一个速度点，右连续）；
- 往返误差 ≤ **1 tick**（响应中直接给出残差与判定）；
- 非法拍号、速度点非递增、BPM 非正等情况返回 **422**，错误项带有
  出错的**数组索引** `index`；
- 谱例发布采用**乐观并发**：预期修订号不一致返回 **409**；锁定锚点
  偏移超过录入时一 tick 时长返回带锚点索引的 **422**，数据不变。

---

## 目录

- [一键启动（Docker Compose）](#一键启动docker-compose)
- [本地直接运行](#本地直接运行)
- [接口地址与交互式文档](#接口地址与交互式文档)
- [约定与规则](#约定与规则)
- [请求字段](#请求字段)
- [响应字段](#响应字段)
- [示例](#示例)
- [谱例：带录音锚点的修订发布](#谱例带录音锚点的修订发布)
- [错误响应（422，含索引）](#错误响应422含索引)
- [运行测试](#运行测试)
- [项目结构](#项目结构)

---

## 一键启动（Docker Compose）

需要 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

启动后服务监听在：

- 本机地址：<http://localhost:8000>
- 健康检查：<http://localhost:8000/health>
- 接口：`POST http://localhost:8000/convert`
- 谱例：`POST/GET http://localhost:8000/scores`（详见
  [谱例：带录音锚点的修订发布](#谱例带录音锚点的修订发布)）
- Swagger 交互文档：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- OpenAPI Schema：<http://localhost:8000/openapi.json>

谱例数据保存在 SQLite（容器内 `/app/data/scores.db`，挂载命名卷
`tempo-data`）；`docker compose restart` 或 `down` 后重新 `up` 数据仍在。
删除数据：`docker compose down -v`。

停止：`docker compose down`（后台运行：`docker compose up --build -d`）。

## 本地直接运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

地址同上。本地运行时 SQLite 默认写到 `./data/scores.db`，可用环境变量
`TEMPO_MAP_DB_PATH` 指定其他路径。

---

## 约定与规则

### 1. PPQ 与位置 `小节:拍:tick`

- `ppq`：每个**四分音符**的 tick 数（如 480）。
- 位置字符串形如 `"1:1:0"`，三个分量均为整数：
  - **小节** `measure`：从 1 开始；
  - **拍** `beat`：从 1 开始，按**该小节生效拍号的分母**计——分母为 `d`
    时一拍是 `1/d` 音符，长度 `4*PPQ/d` tick（4/4 一拍 = 四分音符 = PPQ
    tick；6/8 一拍 = 八分音符 = PPQ/2 tick）。合法范围
    `1 ≤ beat ≤ 拍号分子`，超过即 422；
  - **tick**：`0 ≤ tick < 4*PPQ/d`（单拍范围），超出即 422。
- 内部横坐标为距乐谱起点的 tick 总数（PPQ 坐标）：

  ```
  beat_ticks  = 4 * PPQ / d          # d = 该小节生效拍号的分母
  total_ticks = 该小节首拍 tick + (beat - 1) * beat_ticks + tick
  ```

- 正向展开（位置→tick）、速度点定位、反向规范化（tick→位置）与边界
  归属共用同一坐标规则：tick 恰好落在拍号区域/小节/拍边界时归后一段，
  正反互为逆运算。
- 每个速度点/换算结果都会被规范为该坐标系下的整数 tick。

### 2. 拍号表（`time_signatures`）

- 每项形如 `{"measure": 5, "numerator": 3, "denominator": 4}`，
  表示 3/4 从**第 5 小节首拍**起生效；拍号**只能在小节首拍生效**。
- 拍号表按小节号**严格递增**（不得乱序、不得同一小节重复）。
- 拍号分母必须是 **2 的幂**（2/4/8/16/32…）。
- 单拍长度与小节长度：

  ```
  beat_ticks = 4 * PPQ / denominator   # 一拍（1/denominator 音符）的 tick 数
  bar_ticks  = numerator * beat_ticks
  ```

  `4*PPQ/denominator` 必须是**整数 tick**；若在当前 PPQ 下无法整除
  （例如 PPQ=100 时的 1/32，每拍 12.5 tick），返回 422，因为该拍号
  无法在本坐标下精确表示。
- 若拍号表为空，或第一项不在第 1 小节，则开头隐含 **4/4**。
- 复合拍号按分母计拍：6/8 = 6 个八分音符拍/小节（PPQ=480 时每拍 240
  tick、每小节 1440 tick），7/8 = 7 个八分音符拍/小节（每拍 240 tick、
  每小节 1680 tick）。

### 3. 速度点（`tempo_points`）

- 每项：`{"position": "1:1:0", "bpm": 120, "curve": "step"}`。
- **首个速度点必须位于 `1:1:0`**（tick 0）。
- 位置必须**严格递增**（相等或倒序返回 422）。
- `bpm` 必须为**正数**（有限值；0、负数、NaN、Infinity 返回 422）。
- `curve`：
  - `"step"`（默认）：从该点到下一点**保持该 BPM**（阶梯速度）；
  - `"linear"`：从该点到下一点，BPM 在两点之间**线性变化**。
- 速度点 `i` 的曲线决定区间 `[tick_i, tick_{i+1})` 内的速度；
  **末个速度点之后保持其 BPM**（恒定）。
- **边界归属一致**：边界 tick `tick_{i+1}` 归属于后一个点（右连续），
  正向（位置→秒）与反向（秒→位置）使用同一边界约定。

### 4. 线性段的积分与求逆

段内速度随 tick 线性变化：

```
B(t) = b0 + (b1 - b0) * (t - t0) / span
```

- 正向（对“秒/tick”积分）：

  ```
  ΔT = 60·span / ((b1-b0)·PPQ) · ln(B(t) / b0)
  ```

- 反向（在**同一段**内解析求逆）：

  ```
  B(t) = b0 · exp(ΔT / A)，  A = 60·span / ((b1-b0)·PPQ)
  t = t0 + span · (B(t) - b0) / (b1 - b0)
  ```

  阶梯段直接按恒定 BPM 线性换算；两端 BPM 几乎相等时自动退化为恒定
  速度的等价极限。

- 秒 → 位置先得到**未量化的浮点 tick**（`raw_total_ticks`），再四舍五入
  到整数 tick；往返残差以未量化 tick 与量化 tick 之差衡量，保证
  **≤ 1 tick**。

---

## 请求字段

`POST /convert`，`Content-Type: application/json`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ppq` | int | 是 | 每四分音符 tick 数，≥ 1 |
| `time_signatures` | array | 否 | 拍号表，按小节首拍生效；空时整体 4/4 |
| `tempo_points` | array | 是 | 速度点表；首点须在 `1:1:0`，严格递增 |
| `position` | string | 二选一 | 待换算位置 `"小节:拍:tick"`（拍按当前拍号分母计） |
| `seconds` | number | 二选一 | 待换算时间（秒，非负） |

`time_signatures[]`：`measure`（int，≥1）、`numerator`（int，≥1）、
`denominator`（int，2 的幂）。

`tempo_points[]`：`position`（string）、`bpm`（number，>0）、
`curve`（`"step"` 或 `"linear"`，默认 `"step"`）。

> `position` 与 `seconds` **必须且只能提供一个**，否则返回 422。

## 响应字段

| 字段 | 说明 |
|---|---|
| `position` / `measure` / `beat` / `tick` | 规范后的乐谱位置及其分量 |
| `total_ticks` | 距起点的 tick 总数（PPQ 坐标，整数） |
| `seconds` | 距起点的时间（秒） |
| `input_position` / `input_seconds` | 回显本次输入（另一个为 `null`） |
| `roundtrip.raw_total_ticks` | 秒→tick 反解的未量化浮点 tick |
| `roundtrip.residual_ticks` | 往返 tick 残差（量化前后） |
| `roundtrip.residual_seconds` | 往返秒残差 |
| `roundtrip.position_roundtrip` | 往返后重新规范化的位置 |
| `roundtrip.within_one_tick` | 往返误差是否 ≤ 1 tick |

---

## 示例

### 示例 1：位置 → 秒（拍号变化 + 阶梯/线性混合）

```bash
curl -s -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "ppq": 480,
    "time_signatures": [
      {"measure": 1, "numerator": 4, "denominator": 4},
      {"measure": 5, "numerator": 3, "denominator": 4}
    ],
    "tempo_points": [
      {"position": "1:1:0", "bpm": 120, "curve": "step"},
      {"position": "2:1:0", "bpm": 60,  "curve": "linear"},
      {"position": "3:1:0", "bpm": 180, "curve": "step"},
      {"position": "5:1:0", "bpm": 90,  "curve": "step"}
    ],
    "position": "6:1:0"
  }'
```

结果（节选）：第 1 小节 120 BPM=2.0s；第 2 小节 60→180 线性=
`2·ln3`≈2.1972s；第 3–4 小节 180 BPM 共 8 拍=2.6667s；第 5 小节
90 BPM=2.0s。

```json
{
  "position": "6:1:0",
  "total_ticks": 9120,
  "seconds": 8.863891244002886,
  "roundtrip": { "residual_ticks": 0.0, "within_one_tick": true }
}
```

### 示例 2：秒 → 位置（用上一步的秒数往返）

```bash
curl -s -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "ppq": 480,
    "time_signatures": [
      {"measure": 1, "numerator": 4, "denominator": 4},
      {"measure": 5, "numerator": 3, "denominator": 4}
    ],
    "tempo_points": [
      {"position": "1:1:0", "bpm": 120, "curve": "step"},
      {"position": "2:1:0", "bpm": 60,  "curve": "linear"},
      {"position": "3:1:0", "bpm": 180, "curve": "step"},
      {"position": "5:1:0", "bpm": 90,  "curve": "step"}
    ],
    "seconds": 8.863891244002886
  }'
```

回到 `"position": "6:1:0"`，`"total_ticks": 9120`，`within_one_tick: true`。

### 示例 3：复合拍号 6/8 的位置语义

6/8 中“拍”是八分音符：PPQ=480 时每拍 `4*480/8 = 240` tick。`1:4:0`
即第一小节第 4 拍 = `3*240 = 720` tick；120 BPM 下每四分音符 0.5 s，
720 tick = 1.5 个四分音符 = **0.75 s**：

```bash
curl -s -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "ppq": 480,
    "time_signatures": [{"measure": 1, "numerator": 6, "denominator": 8}],
    "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
    "position": "1:4:0"
  }'
```

```json
{
  "position": "1:4:0",
  "total_ticks": 720,
  "seconds": 0.75,
  "roundtrip": { "residual_ticks": 0.0, "within_one_tick": true }
}
```

同一拍号下，`1:7:0`（超过第 6 拍）或 `1:1:240`（tick 超出单拍范围
240）都会返回 **422**。

### 示例 4：纯线性 60→120 渐变一拍

60 BPM 线性升到 120 BPM（跨一拍/480 tick），该拍时长为 `ln 2` 秒：

```bash
curl -s -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "ppq": 480,
    "time_signatures": [],
    "tempo_points": [
      {"position": "1:1:0", "bpm": 60,  "curve": "linear"},
      {"position": "1:2:0", "bpm": 120, "curve": "step"}
    ],
    "seconds": 0.6931471805599453
  }'
# -> "position": "1:2:0", "total_ticks": 480
```

也可在 <http://localhost:8000/docs> 页面上直接点 **Try it out** 调用。

---

## 谱例：带录音锚点的修订发布

谱例（score）把一份速度图（PPQ + 拍号表 + 速度点表）和一组**录音锚点**
（乐谱位置 + 锁定标志）保存为一个可修订的对象，持久化在 SQLite 中：

| 接口 | 说明 |
|---|---|
| `POST /scores` | 创建谱例，返回谱例 ID 与修订号 `0` |
| `GET /scores/{score_id}` | 查询谱例：返回同一修订版本的速度图与锚点 |
| `POST /scores/{score_id}/preview` | 预览整份替换速度点表（不修改已存数据） |
| `POST /scores/{score_id}/publish` | 发布整份替换速度点表（修订号 +1） |

规则：

- **创建**：提交 PPQ、拍号、速度点及锚点（位置 + `locked` 标志，**至少
  一个锁定**）；服务按原图计算并保存各锚点秒数，同时记录**录入时该位置
  一 tick 的时长**（`60 / (BPM(位置) * PPQ)`）作为锁定漂移容忍度。
- **预览**：凭谱例 ID 和**预期修订号**提交整份替换速度点表；返回各锚点的
  原秒数、新秒数、偏移及锁定冲突标志，**不修改已存数据**。
- **发布**：按**当前修订**重新计算——锁定锚点相对**创建时保存的秒数**
  若偏移超过录入时该位置一 tick 的时长，返回**带锚点索引的 422** 且数据
  不变；成功则在**一个事务**内更新速度图、未锁定锚点秒数和修订号，
  **锁定锚点秒数不变**。
- **乐观并发**：预览/发布的 `expected_revision` 与当前修订不一致时返回
  **409**（响应带 `current_revision`）。预览后若有其他发布成功，旧预期
  修订号再提交仍返回 409，不会覆盖新版本。

### 创建谱例

```bash
curl -s -X POST http://localhost:8000/scores \
  -H 'Content-Type: application/json' \
  -d '{
    "ppq": 480,
    "time_signatures": [{"measure": 1, "numerator": 4, "denominator": 4}],
    "tempo_points": [
      {"position": "1:1:0", "bpm": 120, "curve": "step"},
      {"position": "3:1:0", "bpm": 60,  "curve": "step"}
    ],
    "anchors": [
      {"position": "2:1:0", "locked": true},
      {"position": "4:1:0", "locked": false}
    ]
  }'
```

响应（201）：锚点秒数已按原图计算保存（2:1:0 → 2.0s，4:1:0 → 8.0s）。

```json
{
  "score_id": "0be4745afeda4b2f8f8d4bc1eb238cf3",
  "revision": 0,
  "ppq": 480,
  "time_signatures": [{"measure": 1, "numerator": 4, "denominator": 4}],
  "tempo_points": [
    {"position": "1:1:0", "bpm": 120.0, "curve": "step"},
    {"position": "3:1:0", "bpm": 60.0, "curve": "step"}
  ],
  "anchors": [
    {"index": 0, "position": "2:1:0", "locked": true,  "seconds": 2.0},
    {"index": 1, "position": "4:1:0", "locked": false, "seconds": 8.0}
  ]
}
```

### 预览替换速度点表

```bash
curl -s -X POST http://localhost:8000/scores/<score_id>/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 0,
    "tempo_points": [
      {"position": "1:1:0", "bpm": 120, "curve": "step"},
      {"position": "3:1:0", "bpm": 240, "curve": "step"}
    ]
  }'
```

响应（200）：各锚点原秒数 / 新秒数 / 偏移 / 锁定冲突一目了然
（`tolerance_seconds` 即录入时该位置一 tick 的时长），已存数据不变。

```json
{
  "score_id": "0be4745afeda4b2f8f8d4bc1eb238cf3",
  "revision": 0,
  "has_lock_conflicts": false,
  "anchors": [
    {"index": 0, "position": "2:1:0", "locked": true,
     "original_seconds": 2.0, "new_seconds": 2.0, "offset_seconds": 0.0,
     "tolerance_seconds": 0.0010416666666666667, "lock_conflict": false},
    {"index": 1, "position": "4:1:0", "locked": false,
     "original_seconds": 8.0, "new_seconds": 5.0, "offset_seconds": -3.0,
     "tolerance_seconds": 0.0020833333333333333, "lock_conflict": false}
  ]
}
```

### 发布替换速度点表

请求体与预览相同：

```bash
curl -s -X POST http://localhost:8000/scores/<score_id>/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 0,
    "tempo_points": [
      {"position": "1:1:0", "bpm": 120, "curve": "step"},
      {"position": "3:1:0", "bpm": 240, "curve": "step"}
    ]
  }'
```

成功响应（200）：`revision` 变为 `1`，速度图与未锁定锚点秒数已更新，
锁定锚点秒数保持创建时的保存值（2.0s）。

```json
{
  "score_id": "0be4745afeda4b2f8f8d4bc1eb238cf3",
  "revision": 1,
  "anchors": [
    {"index": 0, "position": "2:1:0", "locked": true,  "seconds": 2.0},
    {"index": 1, "position": "4:1:0", "locked": false, "seconds": 5.0}
  ]
}
```

失败情形：

- **锁定漂移超限 → 422**（带锚点索引，数据不变）。例如把全图提速到
  240 BPM，锁定锚点 `2:1:0` 的秒数将从 2.0 变为 1.0，偏移 −1.0s 远超
  一 tick 时长 0.00104s：

  ```json
  {"detail": [{"loc": ["body", "anchors", 0],
    "msg": "锁定锚点 2:1:0 的秒数相对创建时保存值 2.000000 偏移 -1.000000 秒，超过录入时该位置一 tick 的时长 0.001042 秒",
    "index": 0}]}
  ```

- **修订号冲突 → 409**。预览之后若有其他发布成功，再用旧的
  `expected_revision` 提交预览或发布都会返回 409，新版本不会被覆盖：

  ```json
  {"detail": [{"loc": ["body", "expected_revision"],
    "msg": "预期修订号 0 与谱例当前修订 1 不一致：期间已有其他发布，请重新查询并预览后再提交",
    "index": null, "current_revision": 1}]}
  ```

### 查询谱例

```bash
curl -s http://localhost:8000/scores/<score_id>
```

返回同一修订版本的速度图与锚点（速度点表、锚点秒数与 `revision` 来自
同一次提交，永不混杂）。谱例不存在时返回 404。

---

## 错误响应（422，含索引）

所有请求体级别的语义/结构错误统一返回 HTTP **422**，`detail` 为错误
数组；每项包含：

- `loc`：字段定位（形如
  `["body","tempo_points",1,"bpm"]`，天然带数组下标）；
- `index`：出错的**数组元素索引**（顶层字段错误时为 `null`）；
- `msg`：中文错误说明。

多个错误会**一次性聚合返回**。例如首点不在 1:1:0、第 2 个速度点
BPM=0、速度点位置重复、拍号分母不是 2 的幂，会在同一个 422 中分别
带上索引 0/1/2/0：

```json
{
  "detail": [
    {"loc": ["body", "time_signatures", 0, "denominator"],
     "msg": "拍号分母必须是 2 的幂（2/4/8/16/...），收到 6", "index": 0},
    {"loc": ["body", "tempo_points", 1, "bpm"],
     "msg": "BPM 必须是正数，收到 0.0", "index": 1},
    {"loc": ["body", "tempo_points", 0, "position"],
     "msg": "首个速度点必须位于 1:1:0（tick 0），收到 '1:2:0'", "index": 0},
    {"loc": ["body", "tempo_points", 2, "position"],
     "msg": "速度点位置必须严格递增：第 2 点 2:1:0 不晚于第 1 点 2:1:0",
     "index": 2}
  ]
}
```

会触发 422 的典型情况：PPQ < 1；拍号分母非 2 的幂；拍号小节号非严格
递增；拍号在当前 PPQ 下单拍长度非整数 tick（`4*PPQ/分母` 不可整除）；
首速度点不在 `1:1:0`；速度点位置非严格递增；BPM ≤ 0（或非有限值）；
`curve` 非法；位置格式错误、拍超出当前拍号分子或 tick 超出单拍范围；
`position`/`seconds` 同时提供或都不提供；`seconds` 为负或非有限值。

---

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试覆盖：分母计拍坐标展开（含 6/8 复合拍号）、阶梯/线性速度（解析式与
Simpson 数值积分交叉校验，误差 ~1e-14）、同段反求与边界归属、拍号切换、
密集/随机点上的 ≤1 tick 往返、各类带索引的 422，以及谱例的创建/查询/
预览/发布、锁定漂移 422、修订冲突 409 与 SQLite 重开持久化。

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由、请求/响应模型、422/409 错误处理
│   ├── tempo.py       # 核心引擎：拍号展开、积分换算、同段求逆、校验
│   └── db.py          # 谱例/锚点的 SQLite 持久化（事务化发布）
├── tests/
│   ├── conftest.py    # 测试用临时 SQLite 路径
│   ├── test_tempo.py  # 引擎数值/往返/校验测试
│   ├── test_api.py    # /convert 端到端测试
│   └── test_scores.py # 谱例创建/预览/发布/持久化测试
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```
