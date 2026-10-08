"""Explainable screening of normalized evidence, not proof of common ownership.

Input completeness is an upstream attestation. The raw-chain evidence builder is a
separate, unfinished integration; never manufacture completeness from partial data.
"""
from collections import defaultdict

from .model import D, ZERO, decimal as dec


def audit(evidence, now):
    if not isinstance(evidence, dict):
        return {"decision": "SKIP", "reasons": ["BUNDLE_EVIDENCE_MISSING"]}
    reasons = []
    as_of = evidence.get("as_of")
    if type(as_of) is not int or not 0 <= now - as_of <= 120:
        return {"decision": "SKIP", "reasons": ["BUNDLE_EVIDENCE_STALE"]}
    for key in ("launch_history_complete", "funding_history_complete"):
        if evidence.get(key) is not True:
            reasons.append(key.upper() + "_MISSING")
    coverage = dec(evidence.get("holder_coverage_pct", 0))
    if not 0 <= coverage <= 100:
        raise ValueError("invalid holder coverage")
    if coverage < 95:
        reasons.append("HOLDER_COVERAGE_LOW")
    holders = {}
    parent = {}

    def root(a):
        parent.setdefault(a, a)
        if parent[a] != a:
            parent[a] = root(parent[a])
        return parent[a]

    def join(a, b):
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for h in evidence.get("holders", []):
        wallet = h["wallet"]
        if not isinstance(wallet, str) or not wallet or wallet in holders:
            raise ValueError("invalid/duplicate holder wallet")
        pct = dec(h["pct"])
        if not 0 <= pct <= 100:
            raise ValueError("invalid holder percentage")
        holders[wallet] = pct
        root(wallet)
    if sum(holders.values(), ZERO) > 100:
        raise ValueError("holder supply exceeds 100%")
    if sum(holders.values(), ZERO) < coverage:
        reasons.append("HOLDER_COVERAGE_INCONSISTENT")
    buys = evidence.get("early_buys", [])
    launch_slot = evidence.get("launch_slot")
    if type(launch_slot) is not int or launch_slot < 0:
        reasons.append("LAUNCH_SLOT_MISSING")
        launch_slot = -100
    early = set()
    first_buy = {}
    for b in buys:
        if type(b["ts"]) is not int or b["ts"] > as_of or b["ts"] < 0:
            raise ValueError("buy evidence timestamp invalid")
        if type(b["slot"]) is not int or b["slot"] < 0:
            raise ValueError("buy slot invalid")
        first_buy[b["wallet"]] = min(first_buy.get(b["wallet"], b["ts"]), b["ts"])
        if 0 <= b["slot"] - launch_slot <= 3:
            early.add(b["wallet"])
    fundees = defaultdict(set)
    links = []
    excluded_hubs = 0
    unresolved_wallets = set()
    for f in evidence.get("funding", []):
        if type(f["ts"]) is not int or not 0 <= f["ts"] <= as_of:
            raise ValueError("funding evidence timestamp invalid")
        if f.get("source_kind") != "private_verified":
            excluded_hubs += 1
            if f.get("source_kind") not in ("exchange", "bridge", "service"):
                unresolved_wallets.add(f["destination"])
            continue
        bought = first_buy.get(f["destination"])
        if bought is not None and 0 <= bought - f["ts"] <= 3600:
            fundees[f["source"]].add(f["destination"])
    for source, wallets in sorted(fundees.items()):
        ordered = sorted(wallets)
        if len(ordered) >= 2:
            for wallet in ordered[1:]:
                join(ordered[0], wallet)
            links.append({"type": "SHARED_PRIVATE_FUNDER", "source": source, "wallets": ordered})
    # Aggregate split transfers before applying the materiality floor. Trace only
    # classified private owners; pool/exchange/service edges cannot prove ownership.
    edges = defaultdict(lambda: ZERO)
    classifications = {}
    edge_records = defaultdict(list)
    for t in evidence.get("transfers", []):
        if type(t["ts"]) is not int or not 0 <= t["ts"] <= as_of:
            raise ValueError("transfer evidence timestamp invalid")
        amount = dec(t["supply_pct"])
        if not 0 <= amount <= 100:
            raise ValueError("invalid transfer percentage")
        edge = (t["source"], t["destination"])
        edges[edge] += amount
        edge_records[edge].append((t["ts"],amount))
        kinds = (t.get("source_kind", "unknown"), t.get("destination_kind", "unknown"))
        if edge in classifications and classifications[edge] != kinds:
            classifications[edge] = ("unknown", "unknown")
        else:
            classifications[edge] = kinds
    reached_at = {wallet:first_buy[wallet] for wallet in early}
    frontier, visited = dict(reached_at), set(early)
    for depth in range(3):
        next_frontier = {}
        for (source, destination), amount in sorted(edges.items()):
            if source not in frontier or amount < D(".5"):
                continue
            eligible = [(ts,n) for ts,n in edge_records[(source,destination)] if ts >= frontier[source]]
            if sum((n for _,n in eligible),ZERO) < D(".5"):
                continue
            kinds = classifications[(source, destination)]
            if any(k in ("pool", "exchange", "bridge", "service") for k in kinds):
                continue
            if kinds != ("private_verified", "private_verified"):
                reasons.append("UNCLASSIFIED_MATERIAL_TRANSFER")
                continue
            join(source, destination)
            links.append({"type":"EARLY_DISTRIBUTION_PATH", "source":source,
                          "destination":destination, "depth":depth+1})
            cumulative=ZERO
            for ts,n in sorted(eligible):
                cumulative+=n
                if cumulative>=D(".5"):
                    arrival=ts
                    break
            if destination not in reached_at or arrival<reached_at[destination]:
                reached_at[destination]=arrival
                next_frontier[destination]=min(next_frontier.get(destination,arrival),arrival)
        visited.update(next_frontier)
        frontier = next_frontier
    if any(source in frontier and destination not in visited and amount >= D(".5")
           for (source,destination),amount in edges.items()):
        reasons.append("DISTRIBUTION_TRACE_DEPTH_LIMIT")
    fragments=defaultdict(list)
    for (source,destination),records in edge_records.items():
        if source not in reached_at:continue
        if any(k in ("pool","exchange","bridge","service") for k in classifications[(source,destination)]):continue
        amount=sum((n for ts,n in records if ts>=reached_at[source]),ZERO)
        if ZERO<amount<D(".5"):fragments[source].append((destination,amount))
    fragmented_outflows=[]
    for source,recipients in sorted(fragments.items()):
        amount=sum((n for _,n in recipients),ZERO)
        if len(recipients)>=2 and amount>=D(".5"):
            reasons.append("FRAGMENTED_DISTRIBUTION_REQUIRES_REVIEW")
            fragmented_outflows.append({"source":source,"recipients":[w for w,_ in recipients],"gross_supply_pct":str(amount)})
    groups = defaultdict(list)
    for wallet in sorted(parent):
        groups[root(wallet)].append(wallet)
    clusters = []
    for wallets in groups.values():
        if len(wallets) < 2:
            continue
        pct = sum((holders.get(w, ZERO) for w in wallets), ZERO)
        clusters.append({"wallets": wallets, "current_supply_pct": str(pct),
                         "early_linked": any(w in early for w in wallets)})
    largest = max((dec(c["current_supply_pct"]) for c in clusters), default=ZERO)
    linked_early = max((dec(c["current_supply_pct"]) for c in clusters if c["early_linked"]), default=ZERO)
    early_pct = sum((holders.get(w, ZERO) for w in early), ZERO)
    unresolved_pct = sum((holders.get(w, ZERO) for w in unresolved_wallets), ZERO)
    if unresolved_pct >= 5:
        reasons.append("UNRESOLVED_FUNDING_GE_5PCT")
    if largest >= 10:
        reasons.append("LINKED_SUPPLY_GE_10PCT")
    if linked_early >= 8:
        reasons.append("EARLY_LINKED_SUPPLY_GE_8PCT")
    if early_pct >= 15:
        reasons.append("EARLY_COHORT_GE_15PCT")
    bad = set(evidence.get("known_bad_wallets", []))
    if any(wallet in bad and pct > 0 for wallet, pct in holders.items()):
        reasons.append("KNOWN_BAD_HOLDER")
    return {"decision": "SKIP" if reasons else "PASS_SCREEN", "reasons": sorted(set(reasons)),
            "largest_linked_supply_pct": str(largest), "early_current_supply_pct": str(early_pct),
            "unresolved_funding_supply_pct": str(unresolved_pct),
            "clusters": clusters, "links": links, "fragmented_outflows":fragmented_outflows, "excluded_hub_or_unknown_funding_edges": excluded_hubs,
            "notice": "Heuristic screen; PASS_SCREEN is not a safety guarantee."}
