# 医药规划指标证据库

支撑医药工业五年规划的跨部门指标监测，重点服务两个关键指标：

* **首创新药（First-in-Class, FIC）全球占比**
* **全球年销售额超过十亿美元的品种数**

系统不直接维护“汇总数”，而是维护一条**可回溯的来源链**：以化合物、候选药、
适应证、首次关键发现、各法域批准、许可关系和销售披露分别建档，再由证据按
版本化规则推导每个品种的结论。监测人员无法用手填汇总数绕过来源链——没有
披露来源腿的金额根本无法计入或冻结。

## 核心机制

| 需求 | 落地方式 |
| --- | --- |
| 同一化合物跨国别名 | `Compound` 持别名表，全部品种结论归一到同一 `compound_id` |
| 多家共同开发 / 跨区许可 | `LicenseRelation` 带生效区间、地域、分成；转手只新增区间相接记录 |
| 谁算首创各执一词 | `AttributionClaim` 并存竞争性意见；未决首创异议**阻塞计入**，裁决留痕 |
| FIC 结论冻结 | 必须通过**科学复核 + 统计复核**两类复核，才冻结到指定统计期 |
| 集团内部交易抵销 | `GroupRelation` 区间化；按**披露期间**双方是否同集团判定，收购前不抵销 |
| 币种 / 财年 / 地域归一 | `RuleBookVersion` 版本化：年均汇率、财年结日、地域映射；按**披露日**选版 |
| 重述 / 更正 / 后到证据 | 一律写入新版本（`supersedes` 指向上一版），触发**局部重算**，只波及关联品种 |
| 已发布年度数字可复现 | 冻结快照记录事件序号 + 全部输入/规则/复核指纹，可按序号重建视图逐位比对 |
| 未公开合同保密 | 来源标 `confidential` + 授权标识；未授权人员只见脱敏结论，经济数字不变 |

### 销售去重口径

* 按 `stream_key` 标识同一终端销售流；同一规范地域的多条终端腿只保留**披露日最新**一条。
* 特许权使用费与终端销售属同一销售流，有终端腿时**不重复加总**。
* 里程碑、合作利润分成不计入年度产品销售额；内部销售按性质或集团关系抵销。
* 财年与日历年不一致时（如日本 3 月结日），金额按天分摊回日历年统计期，
  各日历年段用对应年均汇率折算基准币（USD）。

## 代码结构

```
evidence_db/
  timeline.py   生效区间 [start,end)、统计期间、按天分摊、财年切片
  evidence.py   七类证据档案 + 竞争主张 + 证据缺口（只追加、不可变）
  rules.py      版本化汇率 / 财年 / 地域 / 指标口径
  registry.py   只追加事件序列、版本链、血缘、局部重算、冻结快照与复现校验
  engine.py     FIC 判定、双重复核、去重/抵销/归一、结论装配、访问投影
fixtures/
  sample.json   2030 规划指标定义（口径示例，无企业数据）
  scenario.py   端到端虚构场景：别名/共同开发/跨区许可/收购/重述/更正/保密合同
service.py      只读 HTTP 服务（健康检查、结论、指标、已发布快照与复现校验）
```

## 使用

自检（含一次冻结→逐位复现）：

```bash
python3 service.py --check
```

测试：

```bash
python3 -m unittest -v        # 28 项：含去重、抵销、重述、复现、ACL、HTTP
```

启动只读服务（健康地址 `/health`）：

```bash
python3 service.py --port 8000
```

主要路由（未公开合同通过请求头授权）：

```
GET /health
GET /periods/2025/indicators
GET /periods/2025/candidates/V-ALPHACIN
GET /periods/2025/published/latest
GET /periods/2025/published/{freeze_id}
GET /periods/2025/published/{freeze_id}/verify
```

授权头：`X-Viewer`、`X-Clearance: public|restricted|confidential`、
`X-Grants: grant:contract-AB-2021`。

每个品种结论都列明：`included`（是否计入）、`fic_basis`（首创依据）、
`global_sales.deduped_total`（去重后全球销售）、`rule_versions`（换算规则）、
`sales_legs` 中每条腿的计入理由，以及 `gaps`（仍待确认的证据）。

## 端到端场景说明

`fixtures/scenario.py` 为完全虚构数据。2025 期冻结后再调用
`add_post_freeze_events()`：Beta 重述年报（700m→720m，新版本）、FDA 更正
批准日（新版本）、后到的欧洲证据（披露日落在新版汇率 `rules-v2` 之后）。
已发布快照仍按旧事件序号与旧汇率逐位复现，当前结论则反映全部更新。
