# NYC Rental Agent Tool Design

## Agent 目标

这个 agent 的目标是帮助用户在 NYC 租房数据里快速筛选房源，并进一步回答几个租房时常见的问题：价格是否合理、到目标地点通勤是否方便、楼宇是否有公开违规/投诉记录，以及一个娱乐性质的风水解读。

整体流程是：先用 `search_listings` 从 CSV 数据里找出候选房源，并返回每套房的 `listing_id`；后续工具再基于这个 `listing_id` 做更具体的分析。

## 共用数据层

当前已经实现的数据层由 `scripts/prepare_data.py` 和 `data/nyc_rental_listings_clean.csv` 组成。清洗脚本负责从原始月度文件生成统一候选池；后续工具实现时应只读取这份清洗后的 CSV，不再各自读取或清洗原始文件。CSV 中的 `id` 是跨工具传递房源的主键，后续 `search_listings`、价格评估、通勤、楼宇检查和房源状态检查都应使用同一份数据。

候选池来自 2026 年 6、7、8 月三个快照，不限制 NYC borough，同时包含通过位置验证的 New Jersey 房源。当前生成结果有 70,599 条记录、26 个字段。项目部署到 Google Cloud Run 时，应将清洗后的 CSV、官方位置边界和地址验证缓存随 container image 一起打包；这属于部署要求，当前仓库尚未实现应用启动和 Cloud Run 服务。

这份 CSV 是静态候选索引，不是实时库存接口。`created_at_utc` 和 `last_publish_month` 表示房源最近一次出现在三个数据快照中的时间，不能证明房源此刻仍然可租。后续实现推荐流程时，应只对即将返回的少量候选按需检查 `url`，不能把 CSV 中存在 URL 当作仍可租的证明。图片、视频、优惠和 open house 也需要通过详情页获取。读取 CSV 时必须把 `zip_code` 指定为字符串，避免 `07302` 等 ZIP 丢失前导零，并显式解析日期字段。

```python
df = pd.read_csv(
    "data/nyc_rental_listings_clean.csv",
    dtype={"zip_code": "string"},
    parse_dates=["created_at_utc", "available_date"],
)
```

### Data preparation

运行以下命令重新生成候选池：

```bash
python3 scripts/prepare_data.py
```

脚本读取 `data/firstmover-nyc-rental-listings/` 中 2026-06、2026-07 和 2026-08 三个月的快照。处理顺序如下：

1. 合并三个月数据，并记录每条记录的来源月份。
2. 清洗 `created_at_utc`、`available_date`、地址和房号。
3. 清理 URL 首尾空白、尾部 `/`，并将 URL 中未编码的 `#` 转为 `%23`。先按相同非空 URL 去重；再对完整的 `state + zip_code + normalized_address + normalized_unit` 地址键，把旧记录中的有效 URL 回填到最新但无 URL 的记录后去重；最后按 `id` 去重。每一步都保留来源月份最新、其次发布时间最新的记录；缺少任一地址键字段时不执行地址去重。
4. 将保留记录的来源月份写入 `last_publish_month`，可用于判断数据新鲜度，但不代表当前仍可租。
5. 删除无法形成有效租金或 `effective_rent <= 500` 的记录；随后删除 `COMMERCIAL` 和 `LAND`，并应用房间数量异常规则。
6. 删除经过同房源 URL 回填后仍缺失或空白 `url` 的记录。
7. 将 `zip_code` 统一成五位文本并强制校验 `^\d{5}$`；输入不符合要求时中止生成，避免把无效 ZIP 写入候选池。用有效经纬度和官方边界生成、验证 `state`、`borough`、`neighborhood`；空值、非数字或越界坐标先尝试使用地址精确地理编码缓存修复，仍无法验证的记录直接删除。
8. 删除不进入推荐索引的字段，输出清洗后的 CSV。

本次处理从 72,542 条原始记录开始，去重后剩余 70,767 条，再删除 11 条 `effective_rent <= 500` 的记录、13 条非住宅、121 条房间数量异常、22 条经回填后仍无 URL 的记录和 1 条无法验证的位置异常，最终得到 70,599 条候选房源。

脚本输出：

- `data/nyc_rental_listings_clean.csv`：供后续推荐搜索使用的三个月去重候选池。

脚本依赖以下已存在的地理资源，但不会生成或更新它们：

- `data/geography/nyc_nta_2020.geojson.gz`：NYC NTA 官方边界。
- `data/geography/nj_municipalities.geojson.gz`：New Jersey municipality 官方边界。
- `data/geography/address_geocode_verification.csv`：Census 地址精确匹配验证缓存。

位置清洗使用经纬度执行 point-in-polygon。NYC 边界来自 [NYC Open Data 2020 Neighborhood Tabulation Areas](https://data.cityofnewyork.us/resource/9nt8-h7nd.geojson)，New Jersey 边界来自 [NJGIN Municipal Boundaries of NJ](https://maps.nj.gov/arcgis/rest/services/Framework/Government_Boundaries/MapServer/2)。NYC 的 `neighborhood` 是官方 NTA 名称；New Jersey 的 `neighborhood` 是 municipality 名称，`borough` 统一写为 `New Jersey`。坐标结果与原 `state` 或 `borough` 冲突时，脚本使用 Census Geocoder Exact Match 缓存验证地址，再重新进行空间连接。

#### 清洗后字段说明

| 字段 | 类型 / 格式 | 含义与用途 | 使用限制 |
| --- | --- | --- | --- |
| `created_at_utc` | UTC datetime，ISO 8601 | 房源发布时间；用于“最新发布”排序，并作为同一房源去重时的第二优先级。 | 只表示发布或刷新时间，不代表当前仍可租。 |
| `available_date` | date，可为空 | 数据源提供的预计可入住日期；用于向用户展示辅助信息。 | 无法解析、缺失或与发布时间相差超过一年时设为空；不能用于判断实时可租状态。 |
| `id` | integer | 房源唯一标识；所有后续工具接收的 `listing_id` 都对应此字段。 | 是数据源标识，不应由地址重新生成。 |
| `street` | text | 原始街道地址，用于展示、地理编码和详情查询。 | 保留数据源写法，不适合作为唯一去重键。 |
| `unit` | text，可为空 | 原始房号，用于展示。 | 空白及没有实际房号信息的字面值 `UNIT` 会设为空；缺失时不会仅凭街道地址把多套房合并。 |
| `neighborhood` | text | 位置筛选字段；NYC 为官方 NTA，NJ 为 municipality。 | NYC NTA 与房源网站常用街区名称可能不同；跨州比较时需注意分类粒度。 |
| `borough` | text | 大区域筛选字段；NYC 使用五区名称，NJ 统一为 `New Jersey`。 | 对 NJ 不代表 county 或 municipality，细分位置应使用 `neighborhood`。 |
| `zip_code` | 五位 text | ZIP 筛选、展示，并作为地址验证缓存的匹配键；生成时强制满足 `^\d{5}$`。 | 当前只验证格式并补齐前导零，没有使用 ZIP 边界验证其与坐标、街道的语义一致性；加载时必须保留为字符串。 |
| `state` | `NY` / `NJ` | 州级位置筛选；由坐标和官方边界验证。 | 当前数据范围只包含 NY 和 NJ。 |
| `latitude` | decimal degrees | 房源纬度；用于通勤计算和位置空间连接。 | 空值、非数字或越界坐标仅在地址精确验证成功时修复，否则记录被删除；坐标不等同于具体入口点。 |
| `longitude` | decimal degrees | 房源经度；用于通勤计算和位置空间连接。 | 空值、非数字或越界坐标仅在地址精确验证成功时修复，否则记录被删除；坐标不等同于具体入口点。 |
| `building_type` | category | 房屋类型，可用于住宅类型筛选或结果解释。 | `UNKNOWN` 表示来源未提供可靠类型|
| `bedrooms` | number | 卧室数；用于候选筛选和可比房源匹配。 | 允许 `0` 表示 studio |
| `bathrooms` | number | 完整卫生间数量；用于展示和可比房源匹配。 | `0`，以及 `>= 7` 且大于 `bedrooms + 2` 的异常记录已删除。 |
| `half_baths` | number | 半卫生间数量。 | `>= 5` 的异常记录已删除。 |
| `furnished` | boolean | 是否标记为 furnished；可作为搜索筛选条件。 | `False` 只表示数据源未标记为 furnished，不保证绝对无家具。 |
| `is_new_development` | boolean | 是否标记为新开发项目；可作为搜索筛选条件。 | 依赖数据源标签，不表示楼龄或首次入住日期。 |
| `price` | number，USD / month | 挂牌月租。 | 未直接反映可能存在的租金优惠。 |
| `net_effective_price` | number，USD / month | 数据源提供的 net effective rent；大于 0 时用于计算 `effective_rent`。 | `0` 表示没有可用的净有效租金值；优惠条款仍需打开详情页确认。 |
| `source_group` | text，可为空 | 上游发布方、经纪公司或数据来源组，用于数据溯源。 | 来源命名不统一，目前不作为推荐筛选或质量评分。 |
| `source_type` | category | 数据接入类型，目前包括 `PARTNER`、`FEED`、`OWNER`。 | 反映接入渠道，不代表房源质量、真实性等级或收费方式。 |
| `url` | URL | StreetEasy 原始详情页；用于确认实时状态和获取动态详情。 | 候选池要求非空，路径中的字面 `#` 已编码为 `%23`；外部页面仍可能下架、跳转或更新。 |
| `normalized_address` | text | 标准化街道地址；统一大小写、空格、标点、方向词、街道后缀和序数词，用于去重。 | 内部匹配字段，不建议直接展示；不能单独识别同楼不同单元。 |
| `normalized_unit` | text，可为空 | 标准化房号；移除 Apt/Apartment/Unit/# 前缀和标点，用于与标准化地址联合去重。 | 缺失时不参与地址房号去重。 |
| `last_publish_month` | `YYYY-MM` | 最终保留记录所在的最近来源月份。 | 只表示最近一次出现在三个月快照中的月份，不是下架时间或可租截止日期。 |
| `effective_rent` | number，USD / month | 推荐统一价格：`net_effective_price > 0` 时取该值，否则取 `price`；用于价格筛选和价格公平性比较。 | 不重新推导免租优惠；准确性依赖数据源提供的 `net_effective_price`，且 `<= 500` 的记录已删除。 |

- 租金筛选、排序、展示主价格和市场比较统一使用 `effective_rent`。`price` 仅表示挂牌租金，`net_effective_price` 仅作为 `effective_rent` 的来源值；工具不应绕过 `effective_rent` 自行选择二者。
- 房源匹配和去重统一使用 `state + zip_code + normalized_address + normalized_unit`，避免把不同地区的同名地址误判为同一房源。`street` 和 `unit` 保留原始写法，只用于用户展示、地理编码或打开详情时提供上下文，不再作为标准匹配键。
- 房源身份统一使用 `id`，所有跨工具调用都传递 `listing_id=id`；需要核对实时状态或动态详情时使用 `url`。
- 最新发布时间排序使用 `created_at_utc`，判断记录来自哪个最新快照使用 `last_publish_month`，`available_date` 为最早可move in时间。

#### 删除字段说明

| 字段 | 原始含义 | 删除原因 / 不可用方式 | 当前替代方案 |
| --- | --- | --- | --- |
| `sqft` | 房源面积。 | 72,542 条原始记录中有 54,464 条为 `0`；`0` 混合了未知值与真实值，不能可靠用于面积筛选或计算每平方英尺租金。 | 暂不提供面积筛选；需要时从 `url` 获取并重新验证。 |
| `lease_months` | 租期月数。 | 64,902 条原始记录缺失，覆盖率过低，无法稳定筛选租期，也无法和优惠月数配对计算。 | 通过 `url` 查询最新租期条款。 |
| `months_free` | 免租月数。 | 64,902 条记录为 `0`，且恰好与 `lease_months` 缺失数量一致，无法区分“没有优惠”和“来源未提供”；单独使用会误算实际租金。 | 价格使用数据源已有的 `net_effective_price`；优惠细节通过 `url` 确认。 |
| `no_fee` | 是否免经纪费。 | 三个月 72,542 条原始记录全部为 `False`，没有区分度，也可能把未提供误当成明确收费。 | 不提供 no-fee 筛选；通过 `url` 确认最新费用。 |
| `has_videos` | 是否有视频。 | 只是快照布尔标记，不包含视频地址，媒体内容可能随详情页更新，不能由 agent 直接消费。 | 通过 `url` 动态检查。 |
| `has_3d_tour` | 是否有 3D tour。 | 只是快照布尔标记，不包含 tour 地址，无法独立打开或验证可用性。 | 通过 `url` 动态检查。 |
| `media_asset_count` | 媒体资源数量。 | 只提供数量，不反映图片类型、质量、重复或当前是否仍存在，对推荐价值有限。 | 打开 `url` 查看当前媒体内容。 |
| `lead_photo_id` | 首图的内部资源 ID。 | 不是可访问 URL，缺少媒体服务接口时无法直接展示或分析。 | 从详情页获取可访问图片。 |
| `photo_ids` | 图片内部资源 ID 列表。 | 是逗号分隔的内部标识，不包含图片 URL，也不适合作为搜索或推荐特征。 | 从详情页或未来的媒体工具获取图片。 |
| `open_house_start_utc` | Open house 开始时间。 | 61,541 条原始记录缺失，且活动时间高度时效化；静态快照中的时间很快过期。 | 通过 `url` 实时查询。 |
| `open_house_end_utc` | Open house 结束时间。 | 与开始时间相同，缺失率高且容易过期，不能作为长期候选池字段。 | 通过 `url` 实时查询。 |
| `open_house_appointment_only` | Open house 是否仅限预约。 | 仅在存在 open house 时有意义，61,541 条记录缺失，并会随活动安排变化。 | 通过 `url` 实时查询。 |

## Tool 1: `search_listings`

作用：根据用户条件搜索房源，是整个 agent 的入口工具。

可选参数可以包括：

- `borough`
- `neighborhood`
- `min_bedrooms`
- `max_bedrooms`
- `max_price`
- `furnished`
- `new_development_only`
- `sort_by`
- `limit`

CSV 中必要字段：

- `id`：房源唯一标识，后续工具都基于它继续分析。
- `street`：用于结果展示的原始地址。
- `unit`：用于结果展示的原始房号。
- `normalized_address`：与 `state`、`zip_code`、`normalized_unit` 联合进行地址匹配和去重。
- `normalized_unit`：与 `state`、`zip_code`、`normalized_address` 联合使用的标准房号。
- `neighborhood`：街区筛选。
- `borough`：行政区筛选。
- `bedrooms`：卧室数筛选。
- `bathrooms`：展示给用户。
- `effective_rent`：价格筛选、排序和比较统一使用的最终租金。
- `price`：挂牌月租，只用于补充展示。
- `net_effective_price`：数据源提供的优惠后租金，只作为 `effective_rent` 的来源值。
- `furnished`：是否 furnished。
- `is_new_development`：是否新楼。
- `created_at_utc`：用于 `sort_by="newest"`。
- `url`：原始房源链接。

基本逻辑：

1. 根据用户自然语言提取筛选条件。
2. 用户没有提到的条件不筛选。
3. 价格条件使用 `effective_rent`，避免有免租优惠的房源被误判。
4. 按价格或发布时间排序。
5. 返回前几条关键结果；媒体及 open house 详情通过 `url` 按需查询。

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
