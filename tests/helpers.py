"""测试用内存证据库构造助手。"""

from __future__ import annotations

from evidence_base.model import (
    Approval,
    Candidate,
    Compound,
    KeyDiscovery,
    Opinion,
    Party,
    Relation,
    Review,
    SalesDisclosure,
    Source,
)
from evidence_base.rules import FiscalPeriod, FXRate, RuleBook, RuleBookVersion
from evidence_base.store import EvidenceStore

T0 = "2026-01-01T00:00:00Z"


def source(store, sid="SRC", confidential=False, clearance=0):
    s = Source(id=sid, title=f"来源 {sid}", doc_type="annual-report",
               confidential=confidential, clearance_required=clearance)
    store.put(s, T0)
    return s


def party(store, pid, group=None, **kw):
    p = Party(id=pid, name=pid, group_id=group or pid, **kw)
    store.put(p, T0)
    return p


def candidate(store, cid="C1", compound_id="CP1", novelty="first-in-class",
              originators=("PA",)):
    c = Candidate(id=cid, compound_id=compound_id, code=cid,
                  novelty_class=novelty, originator_party_ids=list(originators))
    store.put(c, T0)
    return c


def compound(store, cpid="CP1"):
    store.put(Compound(id=cpid, name=cpid), T0)


def discovery(store, cid="C1", cpid="CP1", party_id="PA", date="2019-01-01",
              novelty="new-target", did="KD1", sid="SRC"):
    store.put(KeyDiscovery(id=did, candidate_id=cid, compound_id=cpid,
                           title="首次发现", discovered_on=date, party_id=party_id,
                           novelty_claim=novelty, source_id=sid), T0)


def approval(store, cid="C1", juris="US", date="2025-01-01", aid=None,
             first=True, sid="SRC", recorded_at=T0, source_id=None):
    aid = aid or f"AP-{cid}-{juris}"
    store.put(Approval(id=aid, candidate_id=cid, jurisdiction=juris, agency=juris,
                       decision_date=date, first_in_jurisdiction_flag=first,
                       source_id=source_id or sid), recorded_at)
    return aid


def sale(store, cid, party_id, region, amount, ccy="USD", *, period="FY2025",
         label="FY2025", start="2025-01-01", end="2025-12-31",
         kind="booked", sid="SRC", sid_id=None, **extra):
    sid_id = sid_id or f"SL-{cid}-{party_id}-{region}-{period}"
    store.put(SalesDisclosure(
        id=sid_id, candidate_id=cid, party_id=party_id, region=region,
        period_key=period, fiscal_year_label=label,
        period_start=start, period_end=end, currency=ccy, amount=amount,
        revenue_kind=kind, source_id=sid, **extra), T0)
    return sid_id


def relation(store, cid, rtype, grantor, grantee, regions=None, *,
             start="2020-01-01", end=None, confidential=False, sid="SRC",
             rid=None, **extra):
    rid = rid or f"R-{cid}-{grantor}-{grantee}-{rtype}"
    from evidence_base.temporal import Interval
    store.put(Relation(
        id=rid, candidate_id=cid, relation_type=rtype,
        grantor_party_id=grantor, grantee_party_id=grantee,
        regions=[] if regions is None else regions,
        valid=Interval(start, end), confidential=confidential,
        source_id=sid, **extra), T0)
    return rid


def opinion(store, cid, oid, fic=True, proponent="X", sid="SRC", **proposed):
    store.put(Opinion(id=oid, subject="fic-attribution", candidate_id=cid,
                      proponent=proponent, claim=oid,
                      proposed={"is_first_in_class": fic, **proposed},
                      source_id=sid), T0)


def review(store, cid, rtype, rid, rulebook="RB1", opinion_id=None, sid="SRC"):
    store.put(Review(id=rid, candidate_id=cid, review_type=rtype, reviewer=rtype,
                     decision="approved", opinion_id=opinion_id,
                     rulebook_version=rulebook, source_id=sid), T0)


def rulebook(version="RB1", usd=7.1, recorded=T0, calendars=None, regions=None):
    fx = [FXRate("2025-06-30", "USD", "CNY", usd), FXRate("2025-12-31", "USD", "CNY", usd)]
    fx.append(FXRate("2025-12-31", "EUR", "CNY", 7.8))
    fx.append(FXRate("2025-12-31", "JPY", "CNY", 0.048))
    cals = {}
    for pid, mapping in (calendars or {}).items():
        cals[pid] = {
            label: FiscalPeriod(key, s, e) for label, (key, s, e) in mapping.items()
        }
    book = RuleBook()
    book.add_version(RuleBookVersion(
        version=version, recorded_at=recorded, anchor_ccy="CNY",
        fx_rates=fx, fiscal_calendars=cals,
        region_aliases=regions or {"US": ["USA", "United States"],
                                   "CN": ["China"], "EU": ["Europe"]}))
    return book


def base_store():
    store = EvidenceStore()
    source(store)
    return store
