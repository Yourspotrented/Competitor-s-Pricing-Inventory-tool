"""
Radius-based competitor clustering for our facility portfolio.

For each of our facilities (static garages/lots, not event-tied), find
nearby competitor facilities on SpotHero within a radius. Facilities we
own that are close to each other are grouped into a cluster first, so a
shared competitor search covers all of them at once instead of repeating
near-identical SpotHero calls per facility — this mirrors what Tatsav
described on the call ("find a centre point... and using that").

Usage:
    from facilities import load_facilities
    from clustering import build_clusters, find_cluster_competitors

    facilities = load_facilities()
    clusters = build_clusters(facilities, radius_miles=0.7)
    for cluster in clusters:
        competitors, err = find_cluster_competitors(cluster, facilities)
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from checkers.scarcity_check import (
    _normalize_addr,
    _spot_addr,
    _spot_name,
    _spot_facility_id,
    _spot_scarcity,
    _spot_price,
    _spot_coords,
    fetch_spothero_lots_near,
    fetch_parkwhiz_lots_near,
)
from facilities import load_our_facility_ids

logger = logging.getLogger(__name__)

DEFAULT_RADIUS_MILES = 0.5
_EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def build_clusters(facilities: List[Dict[str, Any]], radius_miles: float = DEFAULT_RADIUS_MILES,
                    group: bool = True) -> List[Dict[str, Any]]:
    """
    Group our facilities into clusters no wider than radius_miles across
    (every member within radius_miles of the cluster's own centroid, not
    just of its nearest neighbor). Facilities with no geocoded coordinates
    form their own singleton cluster with no centroid, so they're still
    tracked but can't be radius-searched.

    Greedy, seed-anchored clustering, not single-linkage union-find:
    single-linkage would chain "A near B, B near C" into one cluster even
    when A and C are miles apart, since each link only checks neighbor-to-
    neighbor distance — that let one cluster balloon to 120+ of 162
    facilities in practice. A moving-centroid approach (re-averaging as
    members join) still drifts the same way, just more slowly, and can even
    admit a member that ends up farther than radius_miles from the
    cluster's *final* centroid once more members join afterward. Anchoring
    every membership check to the fixed seed location avoids both: no
    member is ever farther than radius_miles from the seed, and the seed
    never moves once picked, so distances only ever get checked against a
    single fixed point.

    group=False skips the grouping entirely and gives every facility its own
    single-member "cluster" centred on itself — see the comment in the body.

    Returns a list of dicts:
      { facilities: [...], center_lat, center_lon, group_id }
    center_lat/lon are None if no member has coordinates. Note center_lat/
    lon (used to anchor the SpotHero search) is the simple average of all
    final members, which can differ slightly from the seed — but every
    member is still within radius_miles of the seed, which bounds the
    cluster's true diameter to at most 2 * radius_miles, not unbounded.
    """
    geocoded = [f for f in facilities if f["lat"] is not None and f["lon"] is not None]
    geocoded_ids = {f["id"] for f in geocoded}
    ungeocoded = [f for f in facilities if f["id"] not in geocoded_ids]

    group_list: List[List[Dict[str, Any]]] = []

    if not group:
        # One search per facility, centred on the facility itself. Clustering
        # trades accuracy for API calls: a member sitting at the edge of a
        # cluster has its competitors measured from a centroid up to
        # radius_miles away, so its numbers describe a circle that isn't
        # actually centred on it. At a radius as tight as the Events team's
        # 0.1mi that distortion is the same size as the radius, so every
        # facility gets its own search instead.
        group_list = [[f] for f in geocoded]
    else:
        # Greedy, seed-anchored: pick an unassigned facility as the seed, pull
        # in every remaining unassigned facility within radius_miles of THAT
        # seed's fixed location (not a moving average), then move to the next
        # unassigned seed. Each pass is a single fixed-point check, no drift.
        unassigned = list(geocoded)
        while unassigned:
            seed = unassigned.pop(0)
            members = [seed]
            still_unassigned = []
            for f in unassigned:
                if haversine_miles(seed["lat"], seed["lon"], f["lat"], f["lon"]) <= radius_miles:
                    members.append(f)
                else:
                    still_unassigned.append(f)
            unassigned = still_unassigned
            group_list.append(members)

    def _group_id(members: List[Dict[str, Any]]) -> str:
        # Deterministic id for "these facilities shared one SpotHero search"
        # — the sorted member ids joined together, so the same radius group
        # always gets the same id across scans (no separate counter/state).
        return "g-" + "-".join(sorted(m["id"] for m in members))

    clusters: List[Dict[str, Any]] = []
    for members in group_list:
        center_lat = sum(m["lat"] for m in members) / len(members)
        center_lon = sum(m["lon"] for m in members) / len(members)
        clusters.append({"facilities": members, "center_lat": center_lat, "center_lon": center_lon,
                          "group_id": _group_id(members)})

    for f in ungeocoded:
        clusters.append({"facilities": [f], "center_lat": None, "center_lon": None,
                          "group_id": _group_id([f])})

    logger.info("Built %d %s from %d facilities (%d ungeocoded)",
                len(clusters), "cluster(s)" if group else "per-facility search(es)",
                len(facilities), len(ungeocoded))
    return clusters


def _is_ours(lot_address: str, lot_name: str, our_facilities: List[Dict[str, Any]],
              lot_id: str = "") -> bool:
    """
    Is this search result one of our own facilities rather than a competitor?

    When the lot carries a SpotHero facility id, that id is the answer: our
    sheet ids are SpotHero facility ids (verified against live results —
    titles match character-for-character), so membership in
    load_our_facility_ids() is exact, and a lot with an id we don't own is
    definitively a competitor.

    Address matching is kept only for results with no id (ParkWhiz exposes
    none). It was the original approach and is measurably wrong both ways:
    SpotHero often lists one of our lots at a different street number than
    our sheet name implies, so ours leaked in and inflated "Competitor total
    inv left" (e.g. our "76 Gainsborough St." spot is listed at "87
    Gainsborough Street"); and a genuine competitor garage on a street where
    we also hold a spot was dropped as ours, silently deleting the largest
    competitors from the count (Copley Place, Westin Copley, Renaissance
    Park). ParkWhiz still carries both failure modes, but it contributes
    status only — never a number — to the inventory totals.
    """
    if lot_id:
        return lot_id in load_our_facility_ids()

    norm_lot_addr = _normalize_addr(lot_address) if lot_address else ""
    if not norm_lot_addr:
        return False
    for f in our_facilities:
        norm_our_addr = _normalize_addr(f["address"])
        if norm_our_addr and (norm_our_addr in norm_lot_addr or norm_lot_addr in norm_our_addr):
            return True
    return False


def find_cluster_lots(cluster: Dict[str, Any], all_our_facilities: List[Dict[str, Any]],
                       start_hour: int = 10, end_hour: int = 22,
                       radius_miles: float = DEFAULT_RADIUS_MILES
                       ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    """
    Split one cluster's SpotHero search into (competitors, our_own_lots, error).

    One search answers both questions, so our own live inventory costs no
    extra API calls: the lots this scan already had to fetch and discard as
    "ours" are exactly the facilities whose remaining inventory the team
    tracks by hand in the "Our total inv left" column.

    our_own_lots entries carry the facility_id they matched, so a caller can
    attribute each reading back to a row in the sheet. Only lots identified
    by SpotHero facility id are returned — an address-matched guess isn't
    good enough to write into our own inventory numbers.

    See find_cluster_competitors for the competitor-side filtering rules;
    our own lots skip the radius cut, since a facility being ours doesn't
    depend on how far it sits from its cluster's centroid.
    """
    if cluster["center_lat"] is None:
        return [], [], "cluster has no geocoded facility"

    lat, lon = cluster["center_lat"], cluster["center_lon"]
    results, err = fetch_spothero_lots_near(lat, lon, start_hour=start_hour, end_hour=end_hour)
    if err:
        return [], [], err

    our_ids = load_our_facility_ids()
    seen_addrs: set = set()
    seen_own: set = set()
    competitors: List[Dict[str, Any]] = []
    our_lots: List[Dict[str, Any]] = []

    for r in results:
        addr = _spot_addr(r)
        name = _spot_name(r)
        lot_id = _spot_facility_id(r)

        if lot_id and lot_id in our_ids:
            if lot_id in seen_own:
                continue
            seen_own.add(lot_id)
            our_lots.append({
                "facility_id": lot_id,
                "facility_name": name,
                "facility_address": addr,
                "price": _spot_price(r),
                **_spot_scarcity(r),
            })
            continue
        if _is_ours(addr, name, all_our_facilities, lot_id=lot_id):
            continue

        coords = _spot_coords(r)
        if coords is None:
            continue  # can't verify distance — exclude rather than assume it's within radius
        if haversine_miles(lat, lon, coords[0], coords[1]) > radius_miles:
            continue

        norm_addr = _normalize_addr(addr) if addr else _normalize_addr(name)
        if not norm_addr or norm_addr in seen_addrs:
            continue

        seen_addrs.add(norm_addr)
        competitors.append({
            "lot_name": name,
            "lot_address": addr,
            "price": _spot_price(r),
            **_spot_scarcity(r),
        })

    return competitors, our_lots, None


def find_cluster_competitors(cluster: Dict[str, Any], all_our_facilities: List[Dict[str, Any]],
                              start_hour: int = 10, end_hour: int = 22,
                              radius_miles: float = DEFAULT_RADIUS_MILES) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Find unique competitor lots near a cluster's center, excluding anything
    that matches one of our own facility addresses (checked against the
    full portfolio, not just this cluster, since a competitor search can
    surface any of our other facilities too).

    start_hour/end_hour set the booking window used for the SpotHero search
    (today, rolling to tomorrow if already past end_hour) — price and
    spots_left both depend on this window, so a 3-hour vs 24-hour search
    over the same lot returns different numbers. Defaults to 10am-10pm,
    the team's own standard window.

    radius_miles enforces the actual distance cut client-side: SpotHero's
    own geo search returns lots well beyond the requested radius in
    practice (spot-checked results up to ~1.3mi out for a 0.5mi search), so
    trusting its results as-is silently returns "nearby" competitors that
    aren't nearby. Lots with no usable coordinates are dropped rather than
    kept — an unverifiable distance shouldn't count as "within radius".

    Returns (competitors, error). competitors is a list of dicts:
      { lot_name, lot_address, price, spots_left, capacity, percent_remaining, scarcity_level }
    deduped by normalized address. Callers that also want our own facilities'
    live inventory out of the same search should use find_cluster_lots.
    """
    competitors, _our_lots, err = find_cluster_lots(
        cluster, all_our_facilities, start_hour=start_hour, end_hour=end_hour, radius_miles=radius_miles,
    )
    return competitors, err


def find_cluster_parkwhiz_competitors(cluster: Dict[str, Any], all_our_facilities: List[Dict[str, Any]],
                                       start_hour: int = 10, end_hour: int = 22,
                                       radius_miles: float = DEFAULT_RADIUS_MILES) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """
    ParkWhiz equivalent of find_cluster_competitors. Reported separately —
    not merged into the SpotHero-based competitor list or inventory sums —
    because ParkWhiz only exposes a 3-state status (available/limited/
    sold_out) per lot, not an exact spots_left/capacity count, so it can't
    contribute a real number to "Competitor total inv left".

    radius_miles enforces the actual distance cut client-side, same reason
    as find_cluster_competitors: ParkWhiz's own /quotes/ call takes a fixed
    ~1.4mi bounding box (see fetch_parkwhiz_lots_near), which is wider than
    our facility-clustering radius, so results need re-filtering. Lots with
    no usable coordinates are dropped rather than kept.

    Returns (competitors, error). competitors is a list of dicts:
      { lot_name, lot_address, price, availability_status }
    deduped by normalized address, our own facilities excluded.
    """
    if cluster["center_lat"] is None:
        return [], "cluster has no geocoded facility"

    lat, lon = cluster["center_lat"], cluster["center_lon"]
    lots, err = fetch_parkwhiz_lots_near(lat, lon, start_hour=start_hour, end_hour=end_hour)
    if err:
        return [], err

    seen_addrs: set = set()
    competitors: List[Dict[str, Any]] = []
    for lot in lots:
        name = lot.get("name", "")
        addr = lot.get("address", "")
        if _is_ours(addr, name, all_our_facilities):
            continue

        coords = lot.get("coords")
        if coords is None:
            continue  # can't verify distance — exclude rather than assume it's within radius
        if haversine_miles(lat, lon, coords[0], coords[1]) > radius_miles:
            continue

        norm_addr = _normalize_addr(addr) if addr else _normalize_addr(name)
        if not norm_addr or norm_addr in seen_addrs:
            continue

        seen_addrs.add(norm_addr)
        competitors.append({
            "lot_name": name,
            "lot_address": addr,
            "price": lot.get("price"),
            "availability_status": lot.get("status"),
        })

    return competitors, None


def summarize_competitor_inventory(competitors: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Aggregate a competitor list into the "Competitor total inv left" signal.
    Only SpotHero lots expose an exact spots_left count, so the sums only
    include those; lots with unknown counts still count toward
    any_sold_out/any_limited so the signal isn't silently dropped.
    """
    known = [c for c in competitors if c.get("spots_left") is not None]
    known_capacity = [c for c in known if c.get("capacity") is not None]
    total_inv_left = sum(c["spots_left"] for c in known) if known else None
    total_capacity = sum(c["capacity"] for c in known_capacity) if known_capacity else None
    percent_remaining = (
        round(total_inv_left / total_capacity * 100, 1)
        if total_inv_left is not None and total_capacity else None
    )
    return {
        "competitor_count": len(competitors),
        "competitor_total_inv_left": total_inv_left,
        "competitor_total_capacity": total_capacity,
        "competitor_percent_remaining": percent_remaining,
        "lots_with_known_inventory": len(known),
        "any_sold_out": any(c.get("scarcity_level") == "sold_out" for c in competitors),
        "any_limited": any(c.get("scarcity_level") == "limited" for c in competitors),
    }


def run_facility_scan(radius_miles: float = DEFAULT_RADIUS_MILES,
                       csv_path: Optional[str] = None,
                       start_hour: int = 10, end_hour: int = 22,
                       _cluster_lots_out: Optional[List[Dict[str, Any]]] = None,
                       load_facilities_fn=None,
                       group_facilities: bool = True) -> List[Dict[str, Any]]:
    """
    Full pipeline: load our facilities, cluster them, find competitors per
    cluster (one SpotHero search per cluster, shared across every facility
    in it), and produce one competitor-inventory summary per facility.

    start_hour/end_hour set the SpotHero booking window (see
    find_cluster_competitors) — defaults to 10am-10pm.

    load_facilities_fn: which facility loader to use — defaults to the
    pricing team's facilities.load_facilities. Pass
    events_facilities.load_events_facilities to run this same pipeline
    against the Events team's portfolio instead; the clustering/search/
    self-exclusion logic itself is identical for both, only the input
    facility list differs.

    _cluster_lots_out: if a list is passed, one dict per cluster is appended
    to it with the raw per-lot competitor data (SpotHero + ParkWhiz) behind
    that cluster's summary — { radius_group_id, cluster_label, radius_miles,
    sample_facility_id, sample_facility_name, spothero_lots, parkwhiz_lots }.
    Internal hook for run_facility_scan_with_lots(); most callers should use
    that function instead of passing this directly.

    Returns a list of dicts, one per facility:
      { facility_id, facility_name, facility_address, cluster_label,
        radius_group_id, radius_group_size, radius_miles, error,
        **summarize_competitor_inventory(...) }
    cluster_label is the sheet's own "Cluster" column (display grouping
    only). radius_group_id identifies the actual radius group whose shared
    SpotHero search produced this facility's numbers — every facility with
    the same radius_group_id got the exact same search, so their numbers
    are a shared subtotal, not independent per-facility results.
    """
    if load_facilities_fn is None:
        from facilities import load_facilities as load_facilities_fn

    facilities = load_facilities_fn(csv_path=csv_path)
    clusters = build_clusters(facilities, radius_miles=radius_miles, group=group_facilities)

    results: List[Dict[str, Any]] = []
    own_by_id: Dict[str, Dict[str, Any]] = {}
    for cluster in clusters:
        competitors, our_lots, err = find_cluster_lots(cluster, facilities, start_hour=start_hour,
                                                        end_hour=end_hour, radius_miles=radius_miles)
        summary = summarize_competitor_inventory(competitors) if not err else {}
        for lot in our_lots:
            # A facility can surface in more than one cluster's search; the
            # reading is the same either way, so first one wins.
            own_by_id.setdefault(lot["facility_id"], lot)

        if _cluster_lots_out is not None:
            parkwhiz_lots, pw_err = find_cluster_parkwhiz_competitors(
                cluster, facilities, start_hour=start_hour, end_hour=end_hour, radius_miles=radius_miles,
            )
            seed = cluster["facilities"][0]
            _cluster_lots_out.append({
                "radius_group_id": cluster["group_id"],
                "cluster_label": seed.get("cluster_label", ""),
                "radius_miles": radius_miles,
                "sample_facility_id": seed["id"],
                "sample_facility_name": seed["name"],
                "spothero_lots": competitors if not err else [],
                "parkwhiz_lots": parkwhiz_lots if not pw_err else [],
            })

        for facility in cluster["facilities"]:
            own = own_by_id.get(facility["id"], {})
            results.append({
                "our_spots_left": own.get("spots_left"),
                "our_capacity": own.get("capacity"),
                "our_percent_remaining": own.get("percent_remaining"),
                "our_price": own.get("price"),
                "our_scarcity_level": own.get("scarcity_level"),
                "facility_id": facility["id"],
                "facility_name": facility["name"],
                "facility_address": facility["address"],
                "cluster_label": facility.get("cluster_label", ""),
                "radius_group_id": cluster["group_id"],
                "radius_group_size": len(cluster["facilities"]),
                "radius_miles": radius_miles,
                "error": err,
                "competitor_count": summary.get("competitor_count"),
                "competitor_total_inv_left": summary.get("competitor_total_inv_left"),
                "competitor_total_capacity": summary.get("competitor_total_capacity"),
                "competitor_percent_remaining": summary.get("competitor_percent_remaining"),
                "lots_with_known_inventory": summary.get("lots_with_known_inventory"),
                "any_sold_out": summary.get("any_sold_out"),
                "any_limited": summary.get("any_limited"),
            })

    # Backfill: a facility can turn up in a neighbouring cluster's search
    # rather than its own (or in one processed later in the loop), so attach
    # any own-lot readings the first pass hadn't seen yet.
    for row in results:
        if row["our_spots_left"] is None:
            own = own_by_id.get(row["facility_id"])
            if own:
                row.update({
                    "our_spots_left": own.get("spots_left"),
                    "our_capacity": own.get("capacity"),
                    "our_percent_remaining": own.get("percent_remaining"),
                    "our_price": own.get("price"),
                    "our_scarcity_level": own.get("scarcity_level"),
                })

    logger.info("Facility scan complete: %d facilities across %d clusters (%d with own SpotHero inventory)",
                len(results), len(clusters), sum(1 for r in results if r["our_spots_left"] is not None))
    return results


def run_facility_scan_with_lots(radius_miles: float = DEFAULT_RADIUS_MILES,
                                 csv_path: Optional[str] = None,
                                 start_hour: int = 10, end_hour: int = 22,
                                 load_facilities_fn=None,
                                 group_facilities: bool = True) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Same as run_facility_scan, but also returns the raw per-cluster
    competitor lot lists (SpotHero + ParkWhiz) needed to persist price
    history and detect spikes — powers the scheduled scan / spike detection
    path. Costs one extra ParkWhiz call per cluster vs. the summary-only
    path; the UI's on-demand "view lots" click still uses the cheaper
    find_competitors_for_facility instead of this.

    load_facilities_fn: see run_facility_scan.

    Returns (facility_results, cluster_lots) — facility_results has the
    same shape as run_facility_scan's return value; cluster_lots is a list
    of per-cluster dicts, see run_facility_scan's _cluster_lots_out docs.
    """
    cluster_lots: List[Dict[str, Any]] = []
    facility_results = run_facility_scan(
        radius_miles=radius_miles, csv_path=csv_path, start_hour=start_hour, end_hour=end_hour,
        _cluster_lots_out=cluster_lots, load_facilities_fn=load_facilities_fn,
        group_facilities=group_facilities,
    )
    return facility_results, cluster_lots


def _resolve_facility_cluster(facility_id: str, radius_miles: float,
                               csv_path: Optional[str],
                               load_facilities_fn=None,
                               group_facilities: bool = True) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    """Shared lookup for the per-facility live-competitor endpoints below."""
    if load_facilities_fn is None:
        from facilities import load_facilities as load_facilities_fn

    facilities = load_facilities_fn(csv_path=csv_path)
    target = next((f for f in facilities if f["id"] == facility_id), None)
    if target is None:
        return None, facilities, "facility not found"

    clusters = build_clusters(facilities, radius_miles=radius_miles, group=group_facilities)
    cluster = next((c for c in clusters if any(m["id"] == facility_id for m in c["facilities"])), None)
    if cluster is None:
        return None, facilities, "facility not found in any cluster"

    return cluster, facilities, None


def find_competitors_for_facility(facility_id: str, radius_miles: float = DEFAULT_RADIUS_MILES,
                                   csv_path: Optional[str] = None,
                                   start_hour: int = 10, end_hour: int = 22,
                                   load_facilities_fn=None,
                                   group_facilities: bool = True) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Live lookup of the individual SpotHero competitor lots (name, address,
    price, spots_left) behind one facility's shared radius-group number —
    powers the "show me the actual lots, not just the total" expand view.
    Rebuilds the same clustering as run_facility_scan and re-searches live
    rather than reading a cached summary, so the individual prices/lots
    shown are fresh, not just today's total re-displayed.

    start_hour/end_hour set the SpotHero booking window (see
    find_cluster_competitors) — defaults to 10am-10pm.

    Returns (competitors, error). error is set if the facility id isn't
    found or its cluster has no geocoded coordinates.
    """
    cluster, facilities, err = _resolve_facility_cluster(facility_id, radius_miles, csv_path,
                                                          load_facilities_fn=load_facilities_fn,
                                                          group_facilities=group_facilities)
    if err:
        return [], err
    return find_cluster_competitors(cluster, facilities, start_hour=start_hour, end_hour=end_hour,
                                     radius_miles=radius_miles)


def find_parkwhiz_competitors_for_facility(facility_id: str, radius_miles: float = DEFAULT_RADIUS_MILES,
                                            csv_path: Optional[str] = None,
                                            start_hour: int = 10, end_hour: int = 22,
                                            load_facilities_fn=None,
                                            group_facilities: bool = True) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """ParkWhiz equivalent of find_competitors_for_facility — see find_cluster_parkwhiz_competitors."""
    cluster, facilities, err = _resolve_facility_cluster(facility_id, radius_miles, csv_path,
                                                          load_facilities_fn=load_facilities_fn,
                                                          group_facilities=group_facilities)
    if err:
        return [], err
    return find_cluster_parkwhiz_competitors(cluster, facilities, start_hour=start_hour, end_hour=end_hour,
                                              radius_miles=radius_miles)
