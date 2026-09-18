# factor-forge · 研报因子简述 → JSON → 可执行代码 / 公司因子仓库

> **公开脱敏版说明**：本仓库为对外发布版本，所有内部主机名、GitLab/CI 标识、数据库与库表名、字段名均已替换为通用占位符（如 `legacy_db`、`DailyQuote`、`datadict.example.com`、`registry.example.com`），**功能逻辑与原版一致**，测试全绿。若要在自己的环境使用，把占位符换成你的数据字典服务与库表即可。

把**券商研报里的因子简述**（自然语言 + 公式）自动转化为**结构化 JSON**，再
编译成**可执行的 Python 因子代码**，并在样本数据上自动验证。这是从"读懂一个
因子"到"能跑出这个因子"的完整流水线。输入既可以是本地 `.txt`、**研报 PDF**、
**网页 URL**，也可以是**登记在册的固定研报来源**（增量同步）；除了独立模块，
还能一键生成**公司因子仓库（signal library）的 `core.py` 及配套脚手架**，其中
数据列名对齐公司数据字典。

```
研报因子简述 (txt / PDF / URL / 固定来源)
        │
        ▼
  ┌─ extract ─┐     ┌─ codegen ─┐     ┌─ validate ─┐
  │  Factor   │ ──► │  Python / │ ──► │  样本数据  │
  │  (JSON)   │     │  core.py  │     │  跑通验证  │
  └───────────┘     └───────────┘     └────────────┘
```

> 定位：研究/工程演示。生成的代码需人工复核后再用于生产。

---

## 1. 为什么要这条流水线

研报里的因子是**给人看的自然语言**：公式混在文字里、变量靠"其中①②"描述、
收益方向藏在"说明"段落。要让它变成能回测、能入库、能喂给模型的资产，必须先
**结构化**，再**代码化**：

| 阶段 | 输入 | 输出 | 模块 |
|---|---|---|---|
| 输入 | 本地文件 / 网页 URL | 因子简述文本 | `webinput.py` |
| 输入 | **研报 PDF** | 因子简述文本 | `pdfinput.py` |
| 输入 | **固定来源**（增量同步） | 研报条目 + 下载 | `sources.py` |
| 提取 | 因子简述文本 | `Factor` JSON | `extractor.py` |
| 生成 | `Factor` JSON | Python 模块 | `codegen.py` |
| 生成 | `Factor` JSON + 数据字典 | **公司 `core.py` + 仓库** | `corepy.py` |
| 绑定 | 变量中文名 → 公司列名 | 数据字典查询 | `datadict.py` |
| 验证 | Python 模块 + 样本数据 | 是否跑通 + 统计 | `validator.py` |

## 2. 中间表示：Factor JSON（`schema.py`）

`Factor` 是流水线的枢纽，字段覆盖"理解 + 复现"所需的一切：

```jsonc
{
  "name": "CGO",                       // 因子代码（取自公式左侧）
  "name_cn": "资本利得突出量",
  "category": "行为金融因子-处置效应",
  "description": "处置效应指...",       // 经济学逻辑（说明段）
  "formula": "CGO_{i,t} = (Close - RP_t)/Close ...",
  "variables": [                        // 每个公式变量 -> 规范数据字段
    {"symbol": "V_t",   "name_cn": "换手率",   "data_field": "turnover"},
    {"symbol": "Close", "name_cn": "收盘价",   "data_field": "close"},
    {"symbol": "P_t",   "name_cn": "VWAP均价", "data_field": "vwap"}
  ],
  "direction": -1,                      // 与未来收益的方向(+1/-1/0)
  "direction_note": "负相关",
  "params": {"lookback": 120},
  "operator": "turnover_weighted_price" // 代码生成算子(实现模板)
}
```

**变量 → 数据字段映射**是代码生成的关键：提取器把"换手率/收盘价/成交额…"
映射到规范字段（`DATA_FIELDS`：close/turnover/amount/vwap/…），代码生成器
据此知道函数该消费哪些输入序列。

## 3. 提取器（`extractor.py`）：文本 → JSON

针对研报简述的半结构化格式做启发式解析（宽容、不因脏文本抛错）：

- **名称/类别**：取公式出现前的短行；含"因子"的行归为类别。
- **公式**：收集所有含 `=` 的行。
- **变量**：解析"其中/①②"段。支持
  `V_t、Close、P_t 分别为换手率、收盘价、VWAP均价`（多变量↔多描述对齐）与
  `k 为权重系数`（单变量）。**只按中文顿号切分**，保护 `r_{i,s}` 里的逗号下标。
- **方向**：从全文识别 `正相关/负相关/反转效应` 等 → +1/-1。
- **算子推断**：**优先看因子名/类别**（避免"动量因子的正文提到反转"被误判），
  再看公式。映射到代码模板：`turnover_weighted_price / momentum / reversal /
  volatility / generic`。

## 4. 代码生成（`codegen.py`）：JSON → Python

按 `operator` 分派到**经过校验的实现模板**；未匹配的算子生成**带公式与变量映射
的骨架**（而非瞎猜），把实现留给人：

| 算子 | 因子例 | 生成实现 |
|---|---|---|
| `turnover_weighted_price` | CGO 资本利得突出量 | 换手率加权持仓成本参考价 |
| `momentum` | 12-1 动量 | `close.shift(skip)/close.shift(lag)-1` |
| `reversal` | 短期反转 | `-close.pct_change(period)` |
| `volatility` | 已实现波动率 | 滚动 `std * sqrt(252)` |
| `generic` | Amihud 非流动性 | 文档化骨架 + `NotImplementedError` |

每个生成模块自带**溯源头**（公式、变量映射、方向、来源）+ 单一切入函数。

## 5. 验证器（`validator.py`）：代码必须跑通

生成不是终点——验证器**动态加载生成的模块**，按其声明的数据字段合成合理样本
（价格随机游走、换手率 Gamma 分布等），调用因子函数并检查：输出非全 NaN、
方差非退化、覆盖率正常。**闭环：文本 → JSON → 代码 → 确实能算。**

## 6. 扩展输入：网页 URL（`webinput.py`）

`forge.py` 的 `path` 参数除了本地文件/目录，还接受 `http(s)://` URL（例如
`https://fund.eastmoney.com/006195.html`）。流程：抓取页面 → 纯标准库把 HTML
转成**按行的纯文本**（标题置顶，保留段落/标题/表格行，剥离 script、style、
导航等噪声）→ 交给同一个提取器。只用 `urllib` + `html.parser`，不引入新依赖。

```bash
python scripts/forge.py https://fund.eastmoney.com/006195.html --name 006195
```

## 7. 研报 PDF 输入（`pdfinput.py`）

券商研报以 PDF 发布。`forge.py` 直接接受 `.pdf` 路径：

```
PDF --PyMuPDF--> 全文 --find_factor_briefs--> 因子简述候选(打分) --> extract
```

因子段落定位是**确定性启发式**（无 LLM）：扫描"因子定义/因子构造/计算公式/
指标说明"等锚点，对其后窗口按公式行（`=`）、`其中`、方向词打分，跳过目录点线
与"敬请参阅/图表/风险提示"等噪声，返回得分最高的候选。PDF 支持依赖
`pymupdf`（可选依赖）。

```bash
python scripts/forge.py path/to/研报.pdf
```

## 8. 固定来源与增量同步（`sources.py` + `sources/`）

因子研报来自**一组固定来源**——例如券商研报中心持续发布 PDF。来源在
`sources/sources.json` 中声明式登记，`sync` 增量拉取、下载、解析、入库，
**已处理的条目不重复处理**（`sources/state.json` 记录），保证整套流程可重复、
可审计、一致地产出同样的结构。

```
sources.json (registry)
    │  forge.py sources sync
    ▼
fetch_entries(source)      # 按 kind 拉取：
    │                      #   eastmoney —— 研报中心列表 API（行业/策略）
    │                      #   folder   —— 本地收件箱新增 PDF
    │                      #   pdf_urls —— 静态 PDF 直链清单
    ▼
download_entry -> cache/   # 下载 PDF（含详情页解析真实直链）
    ▼
pdfinput -> extract -> json/code/company   # 复用同一流水线
    ▼
state.json + digest.md     # 每条目状态：factor / no_factor / *_error
```

内置东方财富研报中心来源：列表来自 `reportapi.eastmoney.com`（行业 qType=1 /
策略 jg），PDF 直链 `pdf.dfcfw.com/pdf/H3_<infoCode>_1.pdf`；策略类报告的真实
直链通过详情页（`zw_strategy.jshtml?encodeUrl=...`）解析。`keywords` 过滤只保留
因子/量化相关标题。

```bash
python scripts/forge.py sources list                    # 查看登记的来源
python scripts/forge.py sources sync --limit 3          # 增量同步全部启用来源
python scripts/forge.py sources sync --source local_inbox --company
python scripts/forge.py sources status                  # 已处理条目与状态
```

产出统一放在 `examples/from_sources/<source_id>/<日期_标题>/`：`brief.txt`、
`entry.json`、因子 `json/`、`code/`、（`--company` 时）`company/Signal_XXX/`。
`from_sources/digest.md` 是所有来源的汇总（哪份研报有因子、哪份没有）。

## 9. 公司因子仓库代码生成（`corepy.py`）

`--company` 模式按公司 signal library（Gaea runner）的真实仓库结构产出整套
脚手架，核心是 **`core.py`**：

```
Signal_XXX/
├── core.py          # read_daily_data(SQL, 公司列名) / calc_factor_data /
│                    # build_signal_data / get_signal / main(date, output_dir)
├── function.py      # 公司标准数据层（read_sql/read_tradingdays/save_signal…）
├── main.py          # click CLI（-d date -o output -r rawdata）
├── manifest.json    # runner_id/runner_name、依赖表、checks、DAG
├── rawdata.json     # 数据库连接（占位符，不含真实凭据）
├── .gitlab-ci.yml   # ci-template Auto-Trigger
├── materials/spec.json  # 公司 spec 格式（signal_name/formula/variables…）
└── README.md
```

要点：

- `SIGNAL_FILE = runner_value_<runner_id>.json`、`SIGNAL_CHECK_FILE`、
  `MIN_VALID_VALUE`、`function.save_signal(...)` 均按公司惯例生成。
- 已实现算子（`turnover_weighted_price / momentum / reversal / volatility`）
  生成完整可跑的计算逻辑；`generic` 生成公司同款待实现骨架。
- 换手率按公司口径派生：`TradeVolume / FloatShares`；
  VWAP = `TradeValue / TradeVolume`。
- `validate_corepy` 用合成的日频面板数据实跑 `calc_factor_data` +
  `build_signal_data`（stub 掉 `function`/`duckdb`），检查 `runner_value`
  非退化、输出列为 `runner_code/runner_value`。

```bash
python scripts/forge.py examples/input/cgo.txt --company \
    --runner-id 30180 --runner-name Signal_CGO
```

## 10. 数据字典绑定（`datadict.py`）

公司代码里的**数据名称来自公司数据字典**（`http://datadict.example.com`，RAG 检索，
`POST /query`）。对每个因子变量的中文名（换手率/收盘价…），查询字典并把返回的
表/列解析为绑定（`DailyQuote.ClosePx` 等），写入 `core.py` 的 SELECT 与
文档头。查询结果落盘缓存（`.datadict_cache.json`）。字典不可达或用 `--no-dict`
时，回退到已对字典核验过的内置绑定（`BUILTIN_BINDINGS`）。

## 11. 使用

```bash
pip install -r requirements.txt          # numpy, pandas, pytest (+pymupdf 可选)

# 单个因子 / 整个目录，端到端
python scripts/forge.py examples/input/cgo.txt
python scripts/forge.py examples/input/

# 网页 URL 输入
python scripts/forge.py https://fund.eastmoney.com/006195.html

# 研报 PDF 输入（自动定位因子简述段）
python scripts/forge.py path/to/研报.pdf

# 只到 JSON（不生成代码）
python scripts/forge.py examples/input/ --json-only

# 公司因子仓库（core.py + 脚手架），数据名对齐 datadict.example.com
python scripts/forge.py examples/input/cgo.txt --company \
    --runner-id 30180 --runner-name Signal_CGO

# 离线（不查字典，用内置绑定）
python scripts/forge.py examples/input/ --company --no-dict

# 固定来源：登记 / 增量同步 / 状态
python scripts/forge.py sources list
python scripts/forge.py sources sync --limit 3
python scripts/forge.py sources sync --source local_inbox --company
python scripts/forge.py sources status

# 测试
python -m pytest tests/ -q
```

输出（以 `cgo.txt` 为例）：
```
=== cgo ===
  name      : CGO  (资本利得突出量)
  operator  : turnover_weighted_price
  direction : -1  (负相关)
  variables : [('V_t','turnover'), ('Close','close'), ('P_t','vwap'), ('k','')]
  JSON      : examples/json/cgo.json
  code      : examples/code/cgo.py
  validate  : PASS — OK — 180/300 finite (mean 0.0468, std 0.0614)
  dict      : turnover -> DailyQuote: TradeVolume / NULLIF(FloatShares, 0)
              close    -> DailyQuote.ClosePx  [dict]
  repo      : examples/company/Signal_CGO
  core.py   : PASS — OK — 8/8 stocks (mean 0.0713, std 0.0847)
```

## 12. 示例因子（`examples/`）

| 因子 | 类别 | 算子 | 方向 | 结果 |
|---|---|---|---|---|
| CGO 资本利得突出量 | 行为金融-处置效应 | turnover_weighted_price | −1 | ✅ 可执行 |
| MOM 动量 | 技术面-动量 | momentum | +1 | ✅ 可执行 |
| REV 短期反转 | 技术面-反转 | reversal | +1 | ✅ 可执行 |
| VOL 已实现波动率 | 风险-波动率 | volatility | −1 | ✅ 可执行 |
| ILLIQ Amihud 非流动性 | 流动性 | generic（骨架） | +1 | ⬜ 待实现 |

## 13. 项目结构

```
factor_forge/
  schema.py       # Factor/FactorVariable + JSON 序列化 + 数据字段字典
  extractor.py    # 文本 -> Factor（名称/公式/变量/方向/算子 启发式）
  codegen.py      # Factor -> Python（算子模板库 + 通用骨架）
  corepy.py       # Factor -> 公司 core.py + signal library 仓库脚手架
  webinput.py     # URL 输入：抓取 + HTML -> 按行纯文本
  pdfinput.py     # PDF 输入：PyMuPDF 提取 + 因子简述段落定位
  sources.py      # 固定来源：注册表/拉取器/下载/增量状态
  datadict.py     # datadict.example.com 客户端 + 公司列名绑定
  validator.py    # 生成代码在样本数据上跑通验证（含 core.py 验证）
  templates/      # 公司脚手架模板（function.py/main.py/ci/rawdata 等）
sources/
  sources.json    # 固定来源注册表（eastmoney / folder / pdf_urls）
  inbox/          # 本地研报收件箱（放入 PDF 即被 folder 来源处理）
  state.json      # 增量状态（自动生成，勿手改）
  cache/          # 下载的研报缓存（自动生成）
examples/
  input/          # 研报因子简述（文本）
  json/           # 中间结构化 JSON
  code/           # 生成的因子代码
  company/        # 生成的公司因子仓库（Signal_XXX/）
  from_sources/   # 固定来源同步产出 + digest.md（自动生成）
scripts/
  forge.py        # 端到端流水线（本地/目录/URL/PDF + sources 子命令）
tests/            # schema/提取/生成/验证/URL/PDF/字典/来源/端到端
```

## 14. 设计取舍与扩展

- **启发式提取，容错优先**：研报格式千差万别，提取器尽量填、不抛错；提取不全时
  落到 `generic` 骨架，交给人补全。
- **算子模板是核心资产**：每新增一类公式范式，加一个模板即可扩展覆盖面。
- **可接 LLM 增强**：`extract` 的"公式理解/变量对齐/方向判断"可替换或增强为
  LLM 调用（输出同样的 `Factor` JSON），本仓库的结构化表示即为对接契约。
- **方向与 IC 校验**：`direction` 目前来自文本；接入真实数据后可用 IC 复核，
  与因子库自进化（Atlas）打通。
- **公司脚手架来自真实仓库**：`templates/` 与 `corepy.py` 的结构对齐现网
  signal library（如 `Signal_AccumulationDistributionIndex`）；`rawdata.json`
  只含占位符，不含真实凭据。
- **数据名可溯源**：每个绑定的 `evidence` 记录字典命中来源；字典不可达自动
  回退内置绑定，保证离线可用。
- **URL 输入是"尽力而为"**：网页正文质量参差，提取器容错优先；对基金页等
  非研报页面，公式/方向可能抽取不全，落入 `generic` 骨架由人补全。
- **固定来源强调一致与幂等**：来源声明式登记、状态落盘，`sync` 只处理新增
  条目；无因子段落的研报记为 `no_factor` 而非报错，`digest.md` 提供可审计的
  "哪些报告产出了因子"汇总。新增来源 = 在 `sources.json` 加一条（或加一个
  kind 拉取器），无需改动主流程。

## 参考

- Amihud, Y. (2002). *Illiquidity and Stock Returns*. JFE.
- Jegadeesh & Titman (1993). *Returns to Buying Winners and Selling Losers*.
- Moskowitz, Ooi & Pedersen (2012). *Time Series Momentum*. JFE.
