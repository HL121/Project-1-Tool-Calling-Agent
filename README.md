# NYC Rental Agent：`commute_to` 和 `check_neighborhood_fit`

两个 tool 都在 `tools.py` 里，共用环境变量 `GOOGLE_MAPS_API_KEY`。Google Cloud 项目需要启用 **Routes API** 和 **Places API (New)**。本地运行：在 `.env` 里写 `GOOGLE_MAPS_API_KEY=...`，然后执行 `uv run --env-file .env python app.py`。

## Tool 3: `commute_to`

查询从一个地址到目的地的通勤方式和时间。数据来自 Google Routes API。

### 参数

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `origin` | 是 | 房源的完整地址，例如 `610 West 150th Street, New York, NY` |
| `destination` | 是 | 目的地，地名或地址都可以，例如 `Columbia University, New York, NY` |
| `modes` | 否 | `transit` / `walk` / `bicycle` / `drive` 的列表；不传就四种都查 |
| `transit_preference` | 否 | `subway` / `bus` / `less_walking` / `fewer_transfers`，只对 transit 有效 |
| `departure_time` | 否 | 出发时间，RFC3339 格式，例如 `2026-10-05T08:30:00-04:00`；不传就按现在出发 |
| `arrive_by` | 否 | 最晚到达时间，格式同上，只对 transit 有效；同时传两个时间时以它为准 |

### 逻辑

1. 如果填了 `modes` 或 `transit_preference`，检查是否合法，不合法就返回 error，并列出合法值。没填就跳过检查：`modes` 默认查全部四种，`transit_preference` 默认不加偏好。
2. 每种出行方式各请求一次 Routes API，几种方式同时请求。
3. transit 的每条路线压缩成：时长、距离、换乘次数、步行分钟、上车时间、**实际到达时间**，以及一句路线描述。连续的步行合并成一段。
4. transit 按**到达时间**排序，最早到的排第一。Google 的 `duration` 不包括等车时间，所以不按时长排。传了 `arrive_by` 时反过来，排最晚到的，让用户尽量晚出门。同一条线路只是班次不同的只保留一班，最多再给 2 条备选。
5. 有两种以上方式查到路线时，返回 `fastest_mode`。

错误处理：参数不合法；所有方式都找不到路线（请用户补全地址，不编造时间）；API 请求失败。

### 输出示例

`commute_to("610 West 150th Street, New York, NY", "Columbia University, New York, NY", modes=["transit", "bicycle"])`

```json
{
  "origin": "610 West 150th Street, New York, NY",
  "destination": "Columbia University, New York, NY",
  "options": [
    {
      "mode": "transit",
      "duration_min": 19,
      "distance_mi": 1.7,
      "transfers": 0,
      "walking_min": 4,
      "first_boarding": "4:12 PM",
      "route": "Walk 2 min -> M4 bus (Broadway/W 150 St to Broadway/W 120 St, 12 stops) -> Walk 2 min",
      "arrive_at": "4:30 PM",
      "alternatives": [
        {
          "duration_min": 26, "transfers": 1, "walking_min": 11, "first_boarding": "4:14 PM", "arrive_at": "4:33 PM",
          "route": "Walk 7 min -> 1 Line subway (145 St to 96 St, 2 stops) -> Walk 1 min -> 1 Line subway (96 St to 116 St - Columbia University, 3 stops) -> Walk 2 min"
        }
      ]
    },
    {"mode": "bicycle", "duration_min": 14, "distance_mi": 1.8, "alternatives": []}
  ],
  "fastest_mode": "bicycle"
}
```

## Tool 5: `check_neighborhood_fit`

检查一套房的周边是否符合用户的生活习惯：想要附近有的（wants）和不想挨着的雷区（avoids）。房源坐标来自 `data/nyc_rental_listings_clean.csv`，周边地点来自 Google Places Text Search。

### 参数

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `listing_id` | 是 | CSV 里的 `id`，来自 search 结果 |
| `wants` | 否 | 想要附近有的地点，自由文本，例如 `["fitness gym", "laundromat", "dog run", "Trader Joe's"]` |
| `avoids` | 否 | 雷区，自由文本，例如 `["nightclub", "fire station"]` |

`wants` 和 `avoids` 都是可选的，但至少要有一个。搜索词用具体的英文效果最好，例如用 `fitness gym` 而不是 `gym`，用 `nightclub` 而不是 `bar`（安静的酒吧并不吵）。

### 逻辑

1. `wants` 和 `avoids` 都为空时，返回 error，提示先问用户的生活习惯。
2. 用 `listing_id` 在 CSV 里查经纬度，找不到就返回 error。
3. 每一项在房源周围做一次文字搜索，所有项同时请求，自己用经纬度计算距离，取最近的一个。
4. 判断：want 在 **800 米**（步行约 10 分钟）内算满足；avoid 在 **150 米**（大约同一个街区）内才算踩雷。超出范围也会返回最近的地点，方便 agent 说明"最近的要走多远"。
5. 返回每一项的结果，以及给 ranking 用的 `wants_met`、`wants_total`、`dealbreakers_hit`。

错误处理：没有偏好；`listing_id` 不存在；API 请求失败。

### 输出示例

`check_neighborhood_fit(5096445, wants=["gym", "laundromat", "dog park"], avoids=["bar", "fire station"])`

```json
{
  "listing": "610 West 150th Street #2H, Manhattan",
  "wants": [
    {"want": "gym", "met": true, "nearest": {"name": "Parco Harlem Gym", "distance": "295 meters"}},
    {"want": "laundromat", "met": true, "nearest": {"name": "Miss Bubble Laundromat", "distance": "70 meters"}},
    {"want": "dog park", "met": true, "nearest": {"name": "142nd Street Dog Run 🐕", "distance": "633 meters"}}
  ],
  "avoids": [
    {"avoid": "bar", "hit": true, "nearest": {"name": "Uptown Bourbon", "distance": "54 meters"}},
    {"avoid": "fire station", "hit": false, "nearest": {"name": "FDNY Engine 80/Ladder 23", "distance": "888 meters"}}
  ],
  "wants_met": 3,
  "wants_total": 3,
  "dealbreakers_hit": 1
}
```

## `app.py` 的 agent prompt 设计思路

tool 本身的用法写在 `tools.py` 的 `TOOLS` 描述里。system prompt可以考虑包含以下几点：

- **search 结果要带完整地址和 `id`**：`commute_to` 用地址，`check_neighborhood_fit` 用 `id`。用户说"第二套"时，模型从对话历史里取对应的值。
- **把纽约当前时间放进 prompt**：用户说"明天 9 点要到"时，模型才能填对 `arrive_by` 的日期和时区。
- **先问生活习惯**：用户找房时没提到生活习惯，就问一次；说过就不再问。用户没有偏好时，不调用 `check_neighborhood_fit`，ranking 也不考虑周边。`wants` 和 `avoids` 只能来自用户说过的话，不能编造，并在整个对话中沿用。
- **ranking 使用的字段**：通勤时间，以及 `wants_met`、`dealbreakers_hit`。踩雷的房源排在后面。
- **解释结果用自然语言**：说出线路、到达时间、地点名字和距离，并联系用户说过的习惯；开车要提醒不包括停车时间；不展示原始计数，也不编造 tool 没有返回的时间或地点。
