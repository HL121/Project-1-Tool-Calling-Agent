# NYC Rental Agent Tool Design

## Agent 目标

这个 agent 的目标是帮助用户在 NYC 租房数据里快速筛选房源，并进一步回答几个租房时常见的问题：价格是否合理、到目标地点通勤是否方便、楼宇是否有公开违规/投诉记录，以及一个娱乐性质的风水解读。

整体流程是：先用 `search_listings` 从 CSV 数据里找出候选房源，并返回每套房的 `listing_id`；后续工具再基于这个 `listing_id` 做更具体的分析。

## 共用数据层

部署到 Google Cloud Run 时，CSV 文件会随代码一起打包进 container image。用户或 TA 只需要访问部署后的 URL，不需要自己本地有 CSV 文件。

数据可以使用 2026 年 1 月到 8 月的租房 CSV。应用启动时读取所有月份文件，合并成一个内存中的 DataFrame，后面的本地工具都直接使用这份数据。

基础清洗逻辑：

- 给每个月的数据加一列 `month`，表示数据来源月份。
- 按 `street + unit` 去重，保留最新月份的记录。
- 清理明显异常的 `available_date`。
- 去掉明显异常的低价房源，例如低于 `$1,000` 的记录。
- 生成 `effective_rent`：如果 `net_effective_price > 0`，就用 `net_effective_price`；否则用 `price`。后续价格比较统一使用 `effective_rent`。

## Tool 1: `search_listings`

作用：根据用户条件搜索房源，是整个 agent 的入口工具。

可选参数可以包括：

- `borough`
- `neighborhood`
- `min_bedrooms`
- `max_bedrooms`
- `max_price`
- `furnished`
- `no_fee`
- `new_development_only`
- `sort_by`
- `limit`

CSV 中必要字段：

- `id`：房源唯一标识，后续工具都基于它继续分析。
- `street`：地址。
- `unit`：房号，也可用于和 `street` 一起去重。
- `neighborhood`：街区筛选。
- `borough`：行政区筛选。
- `bedrooms`：卧室数筛选。
- `bathrooms`：展示给用户。
- `price`：挂牌月租。
- `net_effective_price`：优惠后有效月租；如果大于 0，用于计算 `effective_rent`。
- `months_free`：免租月数，展示优惠信息。
- `furnished`：是否 furnished。
- `no_fee`：是否 no fee。
- `is_new_development`：是否新楼。
- `created_at_utc`：用于 `sort_by="newest"`。
- `url`：原始房源链接。

基本逻辑：

1. 根据用户自然语言提取筛选条件。
2. 用户没有提到的条件不筛选。
3. 价格条件使用 `effective_rent`，避免有免租优惠的房源被误判。
4. 按价格或发布时间排序。
5. 返回前几条关键结果，不返回 `photo_ids` 这类长字段。

## Tool 2: `assess_price_fairness`

作用：判断某套房的价格相对同类房源是偏低、合理还是偏高。

参数：

- `listing_id`：必填。

基本逻辑：

1. 根据 `listing_id` 找到目标房源。
2. 使用目标房源的 `effective_rent` 作为比较价格。
3. 找可比房源：优先选择同一 `neighborhood`、同样 `bedrooms` 的房源，并排除目标房源本身。
4. 如果同街区样本太少，可以退到同一 `borough`、同样卧室数。
5. 计算可比房源的中位数、25% 分位数、75% 分位数，以及目标房源在可比样本中的百分位。
6. 根据百分位给出结论，例如明显低于市场、略低、合理、略高、明显偏高。
7. 如果使用 1 月到 8 月多个月数据，可以顺便返回该区域和户型的月度中位数趋势。

## Tool 3: `commute_to` / Google Maps Routes API

作用：查询某套房到用户指定目的地的通勤路线和时间。

参数：

- `listing_id`：必填。
- `destination`：必填，例如 `Columbia University, New York, NY`。
- `transit_mode`：可选，例如 `any`、`bus`、`subway`。

API key 获取方式：

1. 在 Google Cloud Console 中选择项目。
2. 确认项目已经连接 billing account。
3. Enable `Routes API`。
4. 创建 API key。
5. 建议限制这个 key 只能调用 `Routes API`。
6. 部署到 Cloud Run 时，把 key 放入环境变量 `GOOGLE_MAPS_API_KEY`，不要写进代码。

调用逻辑：

1. 通过 `listing_id` 找到房源，读取 `latitude` 和 `longitude` 作为起点。
2. 目的地使用用户输入的文本，例如 `Columbia University, New York, NY`。
3. 调用 Google Routes API 的 `computeRoutes` endpoint。
4. 设置 `travelMode` 为 `TRANSIT`。
5. 可以设置 `computeAlternativeRoutes=True`，让 API 返回多条路线，再从中选择通勤时间最短的一条。
6. 如果用户指定只看 bus 或 subway，可以通过 `transitPreferences.allowedTravelModes` 控制偏好的交通方式。

headers 示例说明：

```python
headers = {
    "Content-Type": "application/json",
    "X-Goog-Api-Key": API_KEY,
    "X-Goog-FieldMask": (
        "routes.duration,"
        "routes.staticDuration,"
        "routes.distanceMeters,"
        "routes.description,"
        "routes.legs.steps.travelMode,"
        "routes.legs.steps.distanceMeters,"
        "routes.legs.steps.staticDuration,"
        "routes.legs.steps.transitDetails.stopDetails,"
        "routes.legs.steps.transitDetails.transitLine.name,"
        "routes.legs.steps.transitDetails.transitLine.nameShort,"
        "routes.legs.steps.transitDetails.transitLine.vehicle.type"
    ),
}
```

- `Content-Type`：说明请求体是 JSON。
- `X-Goog-Api-Key`：传入 Google Routes API key。
- `X-Goog-FieldMask`：告诉 Google 只返回需要的字段，减少无用响应内容。
- `routes.duration`：整条路线耗时。
- `routes.distanceMeters`：整条路线距离。
- `routes.description`：路线描述。
- `routes.legs.steps.travelMode`：每一步是步行、公交、地铁等。
- `routes.legs.steps.transitDetails.stopDetails`：上车和下车站点。
- `transitLine.name` / `nameShort`：线路名称，例如 `1 Train`。
- `vehicle.type`：交通工具类型，例如 `BUS` 或 `SUBWAY`。

错误处理：

- 如果没有 API key，返回错误并提示设置 `GOOGLE_MAPS_API_KEY`。
- 如果 API 请求失败，返回错误和建议，不编造通勤时间。
- 如果目的地无法解析，提示用户提供更具体的地址。

## Tool 4: `check_building_violations` / NYC Open Data

作用：可选工具，用来查询房源所在建筑是否有公开的 HPD 房屋维护违规记录，例如噪音、虫害、漏水、供暖/热水等相关问题。

API：

```text
https://data.cityofnewyork.us/resource/wvxf-dwi5.json
```

这个 API 来自 NYC Open Data 的 HPD Housing Maintenance Code Violations 数据集，不需要 API key。

参数：

- `listing_id`：必填。

基本逻辑：

1. 根据 `listing_id` 找到房源地址和 zip code。
2. 把 CSV 里的地址转换成 HPD API 更容易匹配的格式。例如 `327 East 83rd Street` 转成门牌号 `327` 和街道名 `EAST 83 STREET`。
3. 用 `housenumber`、`streetname`、`zip` 查询 NYC Open Data。
4. 统计违规总数、不同等级数量、状态数量。
5. 从违规描述里做简单关键词统计，例如 `heat`、`hot water`、`roach`、`mice`、`bedbugs`、`mold`、`leak`。
6. 返回最近几条违规描述。

注意：如果查不到记录，不能直接说这栋楼完全没有问题；只能说没有匹配到公开记录，也可能是地址格式没有完全匹配。

## Tool 5: `feng_shui_reading`

作用：娱乐性质的风水解读工具，内部会调用 LLM。

模型：

```text
vertex_ai/gemini-3.5-flash-lite
```

参数：

- `listing_id`：必填。
- `user_birth_year`：可选。

基本逻辑：

1. 根据 `listing_id` 找到房源。
2. 先用代码提取结构化特征，例如：
   - `unit` 里能否解析出楼层。
   - 门牌号和楼层数字里是否包含 4 或 8。
   - 街道名是 Street 还是 Avenue。
   - 卧室数、卫浴数、是否新楼。
3. 把这些结构化特征传给 Gemini。
4. prompt 里明确要求模型只能基于已提供的信息分析，不能编造朝向、采光等数据里没有的信息。
5. 让模型返回 JSON，例如：

```json
{
  "score": 7,
  "auspicious": ["..."],
  "concerns": ["..."],
  "remedies": ["..."],
  "disclaimer": "仅供娱乐参考"
}
```

如果 LLM 调用失败或 JSON 解析失败，工具返回已提取的结构化特征和错误说明，而不是直接抛异常。

## Tool 调用方式

这些工具之间有一个常见链路，但不是强制的 sequential call。也就是说，agent 不一定每次都必须先调用 `search_listings`。

常见流程是：

```text
search_listings -> listing_id
listing_id -> assess_price_fairness
listing_id -> commute_to
listing_id -> check_building_violations
listing_id -> feng_shui_reading
```

但如果用户已经给出了明确的 `listing_id`，或者对话历史里已经有某套房的 `listing_id`，agent 可以直接调用对应工具。例如用户直接问：

```text
How long is listing 5119369 to Columbia University?
```

这时不需要先调用 `search_listings`，可以直接调用：

```text
commute_to(listing_id=5119369, destination="Columbia University")
```

再比如用户在 `search_listings` 之后追问：

```text
第二套到 Columbia 多久？
```

agent 应该从上一轮搜索结果中找到“第二套”对应的 `listing_id`，然后直接调用 `commute_to`。

因此 system prompt 里可以说明：当用户说“第二套”“那个 Williamsburg 的房子”时，agent 应该从对话历史中找到对应的 `listing_id`；只有在用户没有给出具体房源、也没有可引用的历史房源时，才需要先用 `search_listings` 找候选房源。
