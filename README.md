# 医药规划指标证据库

支撑医药工业五年规划的跨部门指标监测。规划把**首创新药全球占比**与**全球年销售额
超过十亿美元的品种数**列为关键指标后，难点不在加总，而在：同一化合物在不同国家的
别名、多家公司共同开发、分地区许可、按不同财年披露收入，以及来源相互冲突。本库把
这些事实**分别建档、带来源链、带版本**，所有发布结论都可逐字复现，且**不接受手填
汇总数**。

## 七类证据分别建档

| 档案文件 | 实体 | 说明 |
|---|---|---|
| `compounds.json` | 化合物 | 各国别名统一收在 `aliases` |
| `candidates.json` | 候选药 | 各法域商品名 `names_by_jurisdiction`、研发代号、首创方 |
| `indications.json` | 适应证 | 一个候选药可对应多个适应证 |
| `discoveries.json` | 首次关键发现 | 首创的科学锚点（新靶点/新机制/新结构类型） |
| `approvals.json` | 各法域批准 | 批准日期被更正时新增版本，不覆盖历史 |
| `relations.json` | 许可关系 | 共同开发 / 分地区许可 / 授权转手 / 并购，均带**生效区间与地域** |
| `sales.json` | 销售披露 | 按公司×地域×财年；区分产品销售、权利金、代收份额、内部销售 |

另含 `parties.json`（公司/集团）、`sources.json`（来源）、`opinions.json`
（竞争性归属意见）、`reviews.json`（科学/统计复核）、`restrictions.json`（访问控制）、
`rulebooks.json`（有版本的币种/财年/地域换算规则）、`periods.json`（统计期）、
`events.json`（带时间戳的迟报/重述/更正事件回放）。

## 关键规则如何落地

- **谁算首创**：由首次关键发现 + 各法域首批 + 首创方组成依据；来源冲突时**并列登记
  竞争性意见**，不强行合并。复核择一"采纳"，且**科学复核与统计复核两类都通过**后，
  结论才具备冻结资格。证据不足或存在未采纳对立意见时显式标注缺口、暂不计入。
- **哪部分销售归入同一品种**：
  - 按带生效区间的许可/转手关系确定权利持有人；让渡方在让渡区间只保留**权利金**，
    权利金不重复计入全球产品销售；
  - **集团内部交易整条抵销**（静态同集团，或并购生效日后并入同一集团；生效日前不抵销）；
  - 代收的合作方份额从产品销售中扣除；
  - 同一地域/期间出现权利链无法拆成独占权利的多方产品销售（如共同开发双计），
    **挂"待确认证据"，在采纳归属意见前不计入**（宁可留缺口，不臆造拆分）。
- **币种、财年、地域归一**：全部走**有版本的换算规则**（`rulebooks.json`）。快照冻结时
  钉住规则版本；旧快照用旧版本逐字复现，事后更新汇率不改变历史数字。缺换算规则时
  按证据缺口处理，不猜测。
- **生效区间**：并购、授权转手只影响其覆盖区间；区间外不受影响。

## 版本、局部重算与复现（双时间）

每条证据按 id 保存一串**仅追加**版本；业务时间在实体自身（批准日、生效区间、披露
期间），系统时间是版本入库时刻 `recorded_at`。

- 企业**重述年报**、**批准日期更正**、**后到的海外证据**都写成新版本（见
  `events.json`），按时间回放。
- 重算只针对受新版本影响的品种（记录→品种的依赖映射），未受影响品种的结论
  **字节级沿用**；已发布快照永不改写。
- 冻结快照记录 `as_of`、规则版本、采纳意见与全部证据的版本指纹，并计算 SHA-256
  内容指纹。事后用快照坐标重建并比对，已发布年度数字仍能复现。

## 访问控制

未公开合同/内部材料按**密级 + 授权名单**限制（`restrictions.json`、来源的
`confidential` 标记）。发布计算以系统主体进行（合同能进入合并与抵销口径），对外只
输出脱敏后的聚合结论；个人通过 `X-Actor-Id` / `X-Clearance` 头访问，未授权取不到
受限原文（HTTP 403）。

## 反手填

结论中的全球销售恒为 `totals_provenance: "derived-from-source-chain"`，由销售披露经
去重、抵销、换算派生；引擎的手填入口 `submit_manual_total(...)` 直接抛
`OverrideGuardError`，监测人员无法绕过来源链。

## 运行

```bash
python3 service.py --check                 # 基础入口检查
python3 -m unittest -v                     # 或 python3 -m pytest -q
python3 service.py --data fixtures/dataset --bootstrap --port 8000
```

健康地址 `/health`。只读 API（示例）：

- `GET /api/periods`
- `GET /api/snapshots`
- `GET /api/snapshots/{id}/metrics` —— 队列规模、首创数、首创全球占比、十亿美元品种数
- `GET /api/snapshots/{id}/candidates/{cid}/determination`
  —— 每个品种列明：**是否计入及理由、首创依据、去重后全球销售、换算规则版本、
  逐条销售处置（纳入/抵销/权利金/冲突）、仍待确认的证据、证据版本指纹**
- `GET /api/snapshots/{id}/reproduce` —— 用当前库重建并与冻结指纹比对
- `GET /api/evidence/{recordId}` —— 原始证据（受限记录按密级返回或 403）

## 库 API 速览

```python
from evidence_base import load_dataset_dir
from evidence_base.app import Service

svc = Service(load_dataset_dir("fixtures/dataset"))
svc.freeze("FY2025", "2026-07-31T00:00:00Z", "SNAP-V1",
           frozen_at="2026-07-31T00:00:00Z")
# …此后重述/更正/海外证据入库（recorded_at 更晚）…
svc.recompute("SNAP-V1", "FY2025", "2027-03-01T00:00:00Z", "SNAP-V2",
              frozen_at="2027-03-01T00:00:00Z")
svc.reproduce("SNAP-V1")   # 旧快照仍逐字复现
```

`fixtures/sample.json` 仍只用于说明 2030 年指标口径，不含企业经营数据；
`fixtures/dataset/` 是覆盖上述全部规则的虚构演示数据集。
