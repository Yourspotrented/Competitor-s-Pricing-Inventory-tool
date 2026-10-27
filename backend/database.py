from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, create_engine, event
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

_DB_PATH = os.getenv("DATABASE_PATH", "./scarcity.db")
_ENGINE = create_engine(
    f"sqlite:///{_DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30.0},
)

@event.listens_for(_ENGINE, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

_SessionFactory = sessionmaker(bind=_ENGINE, autocommit=False, autoflush=False)

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScarcityCheck(Base):
    """One competitor-platform scarcity reading for one of our events."""
    __tablename__ = "scarcity_checks"

    id = Column(Integer, primary_key=True)

    # Our side (ReachPro)
    reachpro_listing_id = Column(String(100), nullable=False, index=True)
    reachpro_event_id = Column(String(100), nullable=False, index=True)
    event_name = Column(String(500), nullable=True)
    event_venue = Column(String(500), nullable=True)
    event_date = Column(String(100), nullable=True)
    section = Column(String(500), nullable=True)
    our_price = Column(Float, nullable=True)
    region = Column(String(50), nullable=True)

    # Competitor side
    platform = Column(String(30), nullable=False, index=True)   # "spothero" | "parkwhiz"
    is_available = Column(Boolean, nullable=True)                # None = check failed
    price = Column(Float, nullable=True)
    spots_left = Column(Integer, nullable=True)                  # exact count (SpotHero only)
    capacity = Column(Integer, nullable=True)                    # total lot capacity (SpotHero only)
    percent_remaining = Column(Float, nullable=True)             # spots_left / capacity (SpotHero only)
    availability_status = Column(String(20), nullable=True)      # "available"|"limited"|"sold_out" (ParkWhiz)
    scarcity_level = Column(String(20), nullable=True, index=True)  # normalized: "ok"|"limited"|"sold_out"|"unknown"
    error = Column(String(300), nullable=True)

    # Which listing source produced the row: "reachpro" (API) or "lysted" (CSV
    # export). Added after the table existed, so create_tables() back-fills the
    # column with ALTER TABLE — create_all alone never adds columns.
    source = Column(String(20), nullable=True, index=True)

    checked_at = Column(DateTime, default=utcnow, index=True)


class LotCheck(Base):
    """One competing lot's scarcity reading for one of our events (multi-lot breakdown history)."""
    __tablename__ = "lot_checks"

    id = Column(Integer, primary_key=True)

    reachpro_event_id = Column(String(100), nullable=False, index=True)
    event_name = Column(String(500), nullable=True)
    event_date = Column(String(100), nullable=True)

    platform = Column(String(30), nullable=False, index=True)   # "spothero" | "parkwhiz"
    # The platform's own id for the lot, where it exposes one (SpotHero does,
    # ParkWhiz does not). Name+address alone are not unique — see the comment
    # in list_spothero_lots — so price history needs this to pair rows up.
    lot_id = Column(String(50), nullable=True, index=True)
    lot_name = Column(String(500), nullable=True)
    lot_address = Column(String(500), nullable=True)
    price = Column(Float, nullable=True)
    spots_left = Column(Integer, nullable=True)                  # exact count (SpotHero only)
    capacity = Column(Integer, nullable=True)                    # total lot capacity (SpotHero only)
    percent_remaining = Column(Float, nullable=True)             # SpotHero only
    availability_status = Column(String(20), nullable=True)      # ParkWhiz only
    scarcity_level = Column(String(20), nullable=True, index=True)
    is_our_lot = Column(Boolean, nullable=True)

    checked_at = Column(DateTime, default=utcnow, index=True)


class PriceSpike(Base):
    """A detected significant price increase for one lot vs. its previous reading."""
    __tablename__ = "price_spikes"

    id = Column(Integer, primary_key=True)

    reachpro_event_id = Column(String(100), nullable=False, index=True)
    event_name = Column(String(500), nullable=True)
    event_date = Column(String(100), nullable=True)
    region = Column(String(50), nullable=True)

    platform = Column(String(30), nullable=False, index=True)
    lot_name = Column(String(500), nullable=True)
    lot_address = Column(String(500), nullable=True)

    previous_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=False)
    percent_increase = Column(Float, nullable=False, index=True)
    is_our_lot = Column(Boolean, nullable=True, index=True)      # the lot behind our own listing

    previous_checked_at = Column(DateTime, nullable=True)
    detected_at = Column(DateTime, default=utcnow, index=True)


class FacilityCompetitorCheck(Base):
    """
    One radius-based competitor-inventory reading for one of our facilities
    (static portfolio, not event-tied). Powers the "Competitor total inv
    left" signal the team wants alongside "Our total inv left" in the sheet.
    """
    __tablename__ = "facility_competitor_checks"

    id = Column(Integer, primary_key=True)

    facility_id = Column(String(100), nullable=False, index=True)
    facility_name = Column(String(500), nullable=True)
    facility_address = Column(String(500), nullable=True)
    cluster_label = Column(String(200), nullable=True)  # the sheet's own "Cluster" column — display grouping only
    radius_group_id = Column(String(300), nullable=True, index=True)  # facilities sharing this id got one shared SpotHero search
    radius_group_size = Column(Integer, nullable=True)

    radius_miles = Column(Float, nullable=False)
    start_hour = Column(Integer, nullable=True)  # SpotHero booking window used for this search, e.g. 10 = 10am
    end_hour = Column(Integer, nullable=True)
    competitor_count = Column(Integer, nullable=True)
    competitor_total_inv_left = Column(Integer, nullable=True)  # sum of spots_left across matched SpotHero lots; NULL if none had a known count
    competitor_total_capacity = Column(Integer, nullable=True)  # sum of capacity across lots with both spots_left and capacity known
    competitor_percent_remaining = Column(Float, nullable=True)  # competitor_total_inv_left / competitor_total_capacity * 100
    lots_with_known_inventory = Column(Integer, nullable=True)
    any_sold_out = Column(Boolean, nullable=True)
    any_limited = Column(Boolean, nullable=True)
    error = Column(String(300), nullable=True)

    checked_at = Column(DateTime, default=utcnow, index=True)


class FacilityLotCheck(Base):
    """
    One competing lot's price/scarcity reading for one of our facility
    radius-groups (facility-flow parallel to LotCheck, which is event-keyed).
    Kept per radius_group_id + platform + lot identity so each lot's own
    price history can be compared reading-to-reading, powering
    FacilityPriceSpike detection.
    """
    __tablename__ = "facility_lot_checks"

    id = Column(Integer, primary_key=True)

    radius_group_id = Column(String(300), nullable=False, index=True)
    cluster_label = Column(String(200), nullable=True)  # for display only — see FacilityCompetitorCheck
    radius_miles = Column(Float, nullable=True)

    platform = Column(String(30), nullable=False, index=True)  # "spothero" | "parkwhiz"
    lot_name = Column(String(500), nullable=True)
    lot_address = Column(String(500), nullable=True)
    price = Column(Float, nullable=True)
    spots_left = Column(Integer, nullable=True)          # SpotHero only
    capacity = Column(Integer, nullable=True)             # SpotHero only
    percent_remaining = Column(Float, nullable=True)      # SpotHero only
    availability_status = Column(String(20), nullable=True)  # ParkWhiz only
    scarcity_level = Column(String(20), nullable=True)

    checked_at = Column(DateTime, default=utcnow, index=True)


class FacilityPriceSpike(Base):
    """
    A detected significant price increase for one competitor lot near one
    of our facility radius-groups, vs. that lot's previous reading —
    facility-flow parallel to PriceSpike (which is event-keyed).
    """
    __tablename__ = "facility_price_spikes"

    id = Column(Integer, primary_key=True)

    radius_group_id = Column(String(300), nullable=False, index=True)
    cluster_label = Column(String(200), nullable=True)
    radius_miles = Column(Float, nullable=True)
    # One representative facility for display (a radius group can have many);
    # first member of the group at detection time.
    sample_facility_id = Column(String(100), nullable=True)
    sample_facility_name = Column(String(500), nullable=True)

    platform = Column(String(30), nullable=False, index=True)
    lot_name = Column(String(500), nullable=True)
    lot_address = Column(String(500), nullable=True)

    previous_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=False)
    percent_increase = Column(Float, nullable=False, index=True)

    previous_checked_at = Column(DateTime, nullable=True)
    detected_at = Column(DateTime, default=utcnow, index=True)
    notified = Column(Boolean, default=False)  # whether a Teams notification was sent for this spike


class FacilityLowInventoryAlert(Base):
    """
    A competitor lot's remaining inventory dropped below the low-inventory
    threshold (see LOW_INVENTORY_THRESHOLD_PERCENT in facility_scan_runner.py)
    — SpotHero only, since ParkWhiz doesn't expose an exact percent_remaining.
    Fired once per crossing (previous reading was at/above threshold, or had
    no prior reading), not on every scan while a lot stays low — otherwise
    an hourly scheduler would repeat the same alert all day.
    """
    __tablename__ = "facility_low_inventory_alerts"

    id = Column(Integer, primary_key=True)

    radius_group_id = Column(String(300), nullable=False, index=True)
    cluster_label = Column(String(200), nullable=True)
    radius_miles = Column(Float, nullable=True)
    sample_facility_id = Column(String(100), nullable=True)
    sample_facility_name = Column(String(500), nullable=True)

    platform = Column(String(30), nullable=False, index=True)
    lot_name = Column(String(500), nullable=True)
    lot_address = Column(String(500), nullable=True)

    spots_left = Column(Integer, nullable=True)
    capacity = Column(Integer, nullable=True)
    percent_remaining = Column(Float, nullable=False, index=True)
    previous_percent_remaining = Column(Float, nullable=True)  # NULL if this is the first reading ever seen for this lot

    detected_at = Column(DateTime, default=utcnow, index=True)
    notified = Column(Boolean, default=False)


# ---------------------------------------------------------------------------
# Events-team flow — fully separate from the pricing-team tables above.
# Same shape, own tables, own scheduler, own Teams channel (see
# events_facilities.py / events_scan_runner.py / events_scheduler.py).
# Not reused even for the 103 facilities that overlap with the pricing
# portfolio — the two teams' scans and alerts are kept independent by
# design (per team decision), not merged/deduped against each other.
# ---------------------------------------------------------------------------

class EventsCompetitorCheck(Base):
    """Events-flow parallel to FacilityCompetitorCheck."""
    __tablename__ = "events_competitor_checks"

    id = Column(Integer, primary_key=True)

    facility_id = Column(String(100), nullable=False, index=True)
    facility_name = Column(String(500), nullable=True)
    facility_address = Column(String(500), nullable=True)
    cluster_label = Column(String(200), nullable=True)
    radius_group_id = Column(String(300), nullable=True, index=True)
    radius_group_size = Column(Integer, nullable=True)

    radius_miles = Column(Float, nullable=False)
    start_hour = Column(Integer, nullable=True)
    end_hour = Column(Integer, nullable=True)
    competitor_count = Column(Integer, nullable=True)
    competitor_total_inv_left = Column(Integer, nullable=True)
    competitor_total_capacity = Column(Integer, nullable=True)
    competitor_percent_remaining = Column(Float, nullable=True)
    lots_with_known_inventory = Column(Integer, nullable=True)
    any_sold_out = Column(Boolean, nullable=True)
    any_limited = Column(Boolean, nullable=True)
    error = Column(String(300), nullable=True)

    checked_at = Column(DateTime, default=utcnow, index=True)


class EventsLotCheck(Base):
    """Events-flow parallel to FacilityLotCheck."""
    __tablename__ = "events_lot_checks"

    id = Column(Integer, primary_key=True)

    radius_group_id = Column(String(300), nullable=False, index=True)
    cluster_label = Column(String(200), nullable=True)
    radius_miles = Column(Float, nullable=True)

    platform = Column(String(30), nullable=False, index=True)
    lot_name = Column(String(500), nullable=True)
    lot_address = Column(String(500), nullable=True)
    price = Column(Float, nullable=True)
    spots_left = Column(Integer, nullable=True)
    capacity = Column(Integer, nullable=True)
    percent_remaining = Column(Float, nullable=True)
    availability_status = Column(String(20), nullable=True)
    scarcity_level = Column(String(20), nullable=True)

    checked_at = Column(DateTime, default=utcnow, index=True)


class EventsPriceSpike(Base):
    """Events-flow parallel to FacilityPriceSpike."""
    __tablename__ = "events_price_spikes"

    id = Column(Integer, primary_key=True)

    radius_group_id = Column(String(300), nullable=False, index=True)
    cluster_label = Column(String(200), nullable=True)
    radius_miles = Column(Float, nullable=True)
    sample_facility_id = Column(String(100), nullable=True)
    sample_facility_name = Column(String(500), nullable=True)

    platform = Column(String(30), nullable=False, index=True)
    lot_name = Column(String(500), nullable=True)
    lot_address = Column(String(500), nullable=True)

    previous_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=False)
    percent_increase = Column(Float, nullable=False, index=True)

    previous_checked_at = Column(DateTime, nullable=True)
    detected_at = Column(DateTime, default=utcnow, index=True)
    notified = Column(Boolean, default=False)


class EventsLowInventoryAlert(Base):
    """Events-flow parallel to FacilityLowInventoryAlert."""
    __tablename__ = "events_low_inventory_alerts"

    id = Column(Integer, primary_key=True)

    radius_group_id = Column(String(300), nullable=False, index=True)
    cluster_label = Column(String(200), nullable=True)
    radius_miles = Column(Float, nullable=True)
    sample_facility_id = Column(String(100), nullable=True)
    sample_facility_name = Column(String(500), nullable=True)

    platform = Column(String(30), nullable=False, index=True)
    lot_name = Column(String(500), nullable=True)
    lot_address = Column(String(500), nullable=True)

    spots_left = Column(Integer, nullable=True)
    capacity = Column(Integer, nullable=True)
    percent_remaining = Column(Float, nullable=False, index=True)
    previous_percent_remaining = Column(Float, nullable=True)

    detected_at = Column(DateTime, default=utcnow, index=True)
    notified = Column(Boolean, default=False)


class OurInventorySnapshot(Base):
    """
    One pull of our own facility data from the team's Notion "Dashboard"
    database (lease/finance tracker — see our_inventory.py). Column names
    mirror Notion's own field names rather than "Our total inv left",
    since what "Inventory" actually represents (capacity vs. currently
    available) hasn't been confirmed with the team yet.
    """
    __tablename__ = "our_inventory_snapshots"

    id = Column(Integer, primary_key=True)

    facility_id = Column(String(100), nullable=False, index=True)
    facility_name = Column(String(500), nullable=True)
    inventory = Column(Integer, nullable=True)  # Notion's "Inventory" field, as-is
    status = Column(String(100), nullable=True)  # Notion's "Status & Availability"
    facility_status = Column(String(100), nullable=True)  # Notion's "Facility Status"

    checked_at = Column(DateTime, default=utcnow, index=True)


class OurFacilityDatabaseSnapshot(Base):
    """
    One import of our own facility master data from the team's SharePoint
    "FACILITY DATABASE" export (manually downloaded CSV — see
    our_facility_database.py for why this isn't pulled live via Graph API
    yet). Column names mirror the source sheet's own headers, kept separate
    from OurInventorySnapshot (Notion) since it hasn't been confirmed which
    source is authoritative for "our inventory".
    """
    __tablename__ = "our_facility_database_snapshots"

    id = Column(Integer, primary_key=True)

    facility_id = Column(String(100), nullable=False, index=True)
    facility_name = Column(String(500), nullable=True)
    facility_address = Column(String(500), nullable=True)
    facility_status = Column(String(100), nullable=True)
    facility_category = Column(String(100), nullable=True)
    no_of_stalls = Column(Integer, nullable=True)
    monthly_inventory = Column(String(50), nullable=True)  # kept as raw text — source mixes "N/A" and numbers
    parkwhiz_status = Column(String(50), nullable=True)
    parkwhiz_stalls = Column(Integer, nullable=True)
    way_status = Column(String(50), nullable=True)
    way_stalls = Column(Integer, nullable=True)
    neighbor_status = Column(String(50), nullable=True)
    neighbor_stalls = Column(Integer, nullable=True)
    spacer_status = Column(String(50), nullable=True)
    spacer_stalls = Column(Integer, nullable=True)

    checked_at = Column(DateTime, default=utcnow, index=True)


class OurSpotheroInventorySnapshot(Base):
    """
    Our own facility's live remaining inventory, read from SpotHero during a
    facility/events scan.

    This is the direct answer to the sheet's "Our total inv left" column —
    live spots remaining out of capacity, from the platform actually selling
    the spot — where OurInventorySnapshot (Notion) and
    OurFacilityDatabaseSnapshot (SharePoint CSV) both carry static
    configured numbers instead. Kept as its own table alongside them rather
    than replacing either, since which source the team treats as
    authoritative hasn't been settled.

    Costs no extra API calls: these readings fall out of the same cluster
    searches the competitor scan already runs, which previously discarded
    our own lots (see clustering.find_cluster_lots).
    """
    __tablename__ = "our_spothero_inventory_snapshots"

    id = Column(Integer, primary_key=True)

    facility_id = Column(String(100), nullable=False, index=True)
    facility_name = Column(String(500), nullable=True)

    spots_left = Column(Integer, nullable=True)
    capacity = Column(Integer, nullable=True)
    percent_remaining = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    scarcity_level = Column(String(20), nullable=True)  # ok / limited / sold_out / unknown

    start_hour = Column(Integer, nullable=True)
    end_hour = Column(Integer, nullable=True)

    checked_at = Column(DateTime, default=utcnow, index=True)


class LystedUpload(Base):
    """
    One upload of the listing team's Lysted inventory export (CSV). The
    latest upload is the current active set; older ones stay for history.
    """
    __tablename__ = "lysted_uploads"

    id = Column(Integer, primary_key=True)
    filename = Column(String(300), nullable=True)
    row_count = Column(Integer, nullable=False, default=0)
    active_count = Column(Integer, nullable=False, default=0)    # Status ACTIVE + Broadcast Y
    skipped_count = Column(Integer, nullable=False, default=0)   # rows stored but not checked
    warnings = Column(String(2000), nullable=True)               # parse warnings, newline-joined
    uploaded_at = Column(DateTime, default=utcnow, index=True)


class LystedListing(Base):
    """
    One row of a Lysted export. Column names mirror the export's own headers
    (see lysted_listings.py for the parse rules). listing_key is our synthetic
    identity — the export has no listing id.
    """
    __tablename__ = "lysted_listings"

    id = Column(Integer, primary_key=True)
    upload_id = Column(Integer, nullable=False, index=True)
    listing_key = Column(String(400), nullable=False, index=True)
    event_key = Column(String(400), nullable=False, index=True)
    # Lysted's own id for this listing, resolved after an upload
    # (lysted_api.resolve_listing_ids). The export has no id column, so
    # without this nothing can be deactivated: Lysted addresses a listing
    # only by id.
    lysted_listing_id = Column(String(30), nullable=True, index=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)

    username = Column(String(200), nullable=True)
    quantity = Column(Integer, nullable=True)
    status = Column(String(50), nullable=True)                   # ACTIVE | READY
    broadcast = Column(String(5), nullable=True)                 # Y | N
    event_name = Column(String(500), nullable=True)              # cleaned, used for matching
    event_name_raw = Column(String(500), nullable=True)          # as exported
    event_date = Column(String(50), nullable=True)               # ISO, e.g. 2026-09-12T19:01:00
    event_date_raw = Column(String(100), nullable=True)          # as exported, e.g. "2026-09-12 7:01pm"
    venue = Column(String(500), nullable=True)
    city = Column(String(200), nullable=True)
    state = Column(String(50), nullable=True)
    section = Column(String(500), nullable=True)                 # the lot: "500 LEE ST. E.- 0.5 MILES"
    row = Column(String(100), nullable=True)
    seats = Column(String(100), nullable=True)
    public_notes = Column(String(1000), nullable=True)
    total_cost = Column(Float, nullable=True)
    list_price = Column(Float, nullable=True)
    inhand_date = Column(String(100), nullable=True)

    uploaded_at = Column(DateTime, default=utcnow, index=True)


class LystedSoldOutAlert(Base):
    """
    A Lysted listing whose parking has just gone sold-out on a buying
    platform — i.e. we can no longer fulfil it, so the listing team needs to
    deactivate it. One row per listing per crossing (not per scan).
    """
    __tablename__ = "lysted_sold_out_alerts"

    id = Column(Integer, primary_key=True)
    listing_key = Column(String(400), nullable=False, index=True)
    event_key = Column(String(400), nullable=False, index=True)
    source = Column(String(20), nullable=True, index=True)       # "lysted" | "reachpro" | "stagefront"
    event_name = Column(String(500), nullable=True)
    event_date = Column(String(50), nullable=True)
    venue = Column(String(500), nullable=True)
    city = Column(String(200), nullable=True)
    state = Column(String(50), nullable=True)
    section = Column(String(500), nullable=True)
    platforms = Column(String(100), nullable=True)               # "spothero" / "parkwhiz" / both, comma-joined
    reason = Column(String(30), nullable=True, index=True)       # "sold_out" | "day_before_event"
    quantity = Column(Integer, nullable=True)
    list_price = Column(Float, nullable=True)
    previous_levels = Column(String(100), nullable=True)         # what each platform showed last scan
    passes_secured = Column(Integer, nullable=True)              # passes bought for this listing (spare spots sheet)
    passes_left = Column(Integer, nullable=True)                 # of those, not yet used or cancelled
    # The decision computed at detection time (listing_actions.decide),
    # stored rather than recomputed later — Stagefront's rule needs
    # platform_detail (not stored) to recompute, unlike Lysted/ReachPro's
    # passes_left-only rule, which /api/actions/pending still recomputes
    # live when this is NULL (older rows, and every Lysted/ReachPro row).
    action = Column(String(20), nullable=True)                   # "keep" | "deactivate" | "reduce"
    detected_at = Column(DateTime, default=utcnow, index=True)
    notified = Column(Boolean, default=False)


class StagefrontSync(Base):
    """
    One pull of our Stagefront inventory (QueryEvents + QueryEventInventoryAll
    — see stagefront_api.py / stagefront_listings.py). Lysted-upload parallel,
    but API-only: there is no CSV step, so this is a sync, not an upload. The
    latest sync is the active set; older ones stay for history.
    """
    __tablename__ = "stagefront_syncs"

    id = Column(Integer, primary_key=True)
    events_seen = Column(Integer, nullable=False, default=0)    # events QueryEvents returned, before the parking/open filter
    listing_count = Column(Integer, nullable=False, default=0)
    active_count = Column(Integer, nullable=False, default=0)   # listings with at least one open (unsold, unheld) pass
    synced_at = Column(DateTime, default=utcnow, index=True)


class StagefrontListing(Base):
    """
    One listing from a Stagefront sync — one row per (event, Listing_ID),
    grouped from QueryEventInventoryAll's per-item rows (see
    stagefront_listings.flatten). Unlike LystedListing, both ids are
    Stagefront's own real, stable ids — nothing here is synthesized.
    """
    __tablename__ = "stagefront_listings"

    id = Column(Integer, primary_key=True)
    sync_id = Column(Integer, nullable=False, index=True)
    stagefront_listing_id = Column(String(50), nullable=False, index=True)
    stagefront_event_id = Column(String(50), nullable=False, index=True)
    # At least one pass still open (not sold/completed, not on hold) — see
    # stagefront_listings._is_open. Stagefront's own "is this broadcasting"
    # flag hasn't been identified yet (no confirmed Item_Status_ID legend),
    # so this is the defensive stand-in, same role as Lysted's is_active.
    is_active = Column(Boolean, nullable=False, default=True, index=True)

    quantity = Column(Integer, nullable=True)         # open passes (not sold/completed, not on hold)
    quantity_total = Column(Integer, nullable=True)    # open + sold/held
    # Passes we already physically hold (Item.InHand == true) among the open
    # ones — Stagefront's native equivalent of ReachPro's unsold_qty, used the
    # same way: it overrides the Lysted flow's spare-spots lookup, which has
    # nothing to find for a Stagefront event.
    passes_in_hand = Column(Integer, nullable=True)

    event_name = Column(String(500), nullable=True)       # cleaned, used for matching
    event_name_raw = Column(String(500), nullable=True)
    event_date = Column(String(50), nullable=True)         # ISO, date + actual time combined — see _combine_date_time
    event_time = Column(String(50), nullable=True)         # Stagefront's own "6:00 PM" text, kept for display
    venue = Column(String(500), nullable=True)
    city = Column(String(200), nullable=True)
    state = Column(String(50), nullable=True)
    cat_desc = Column(String(100), nullable=True)

    section = Column(String(500), nullable=True)      # cleaned lot/address, used for matching
    section_raw = Column(String(500), nullable=True)
    row = Column(String(100), nullable=True)
    item_status_id = Column(Integer, nullable=True)   # raw — no confirmed legend yet, kept for when one is captured
    price = Column(Float, nullable=True)

    synced_at = Column(DateTime, default=utcnow, index=True)


class PlatformCredential(Base):
    """
    A selling platform's short-lived login, pasted by the team.

    Neither platform issues us an API key: Lysted's is a browser token that
    expires after 24 hours, ReachPro's is a session cookie. The 2026-09-22
    call settled on someone refreshing them by hand "for now, pending a better
    API" — still far less work than exporting and uploading a CSV.

    Kept in the database rather than .env so it survives a redeploy and can be
    updated from the dashboard without a developer. One row per paste; the
    newest for a source is the one in use, and the history says who pasted it
    and when it lapsed.
    """
    __tablename__ = "platform_credentials"

    id = Column(Integer, primary_key=True)
    source = Column(String(20), nullable=False, index=True)   # "lysted" | "reachpro" | "stagefront"
    token = Column(String(8000), nullable=False)
    expires_at = Column(DateTime, nullable=True)     # from the token itself; None for a cookie
    saved_at = Column(DateTime, default=utcnow, index=True)
    saved_by = Column(String(120), nullable=True)


class GeocodeCache(Base):
    """
    Cached lat/lon for a geocode query string. Venue locations never change,
    so once we've resolved "The Norva, near ..." once, we never need to ask
    Nominatim again for that same query — this is what actually eliminates
    almost all geocode API calls on repeat scans, not just spaces them out.
    """
    __tablename__ = "geocode_cache"

    query = Column(String(500), primary_key=True)
    lat = Column(Float, nullable=True)   # NULL = confirmed "not found", cached too (avoids re-asking)
    lon = Column(Float, nullable=True)
    cached_at = Column(DateTime, default=utcnow)


def prune_old_data() -> dict:
    """
    Delete old rows from high-volume scan tables and VACUUM the database to
    reclaim disk space back to the OS.

    Called on startup (in a background thread) and then every 24 hours so
    scarcity.db never grows unboundedly on the Render persistent disk.

    Retention policy:
       7 days  — all lot-level and aggregate scan readings, inventory snapshots
                  (facility_lot_checks, events_lot_checks, scarcity_checks,
                  lot_checks, our_spothero_inventory_snapshots,
                  facility_competitor_checks, events_competitor_checks,
                  our_inventory_snapshots, our_facility_database_snapshots)
      14 days  — price spikes and sold-out alerts (smaller volume; kept a bit
                  longer as a short audit trail for the team)
      5 most recent uploads/syncs for Lysted and Stagefront; the rest are removed
      geocode_cache and platform_credentials are never pruned (tiny, permanent value)
    """
    import logging
    _log = logging.getLogger(__name__)

    # SQLite stores datetimes as naive UTC strings — strip tzinfo before comparing.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff_7  = now - timedelta(days=7)
    cutoff_14 = now - timedelta(days=14)

    _PRUNE_RULES = [
        # (table, timestamp_column, cutoff) — highest-volume tables first
        ("facility_lot_checks",              "checked_at",  cutoff_7),
        ("events_lot_checks",                "checked_at",  cutoff_7),
        ("scarcity_checks",                  "checked_at",  cutoff_7),
        ("lot_checks",                       "checked_at",  cutoff_7),
        ("our_spothero_inventory_snapshots", "checked_at",  cutoff_7),
        ("facility_competitor_checks",       "checked_at",  cutoff_7),
        ("events_competitor_checks",         "checked_at",  cutoff_7),
        ("our_inventory_snapshots",          "checked_at",  cutoff_7),
        ("our_facility_database_snapshots",  "checked_at",  cutoff_7),
        ("price_spikes",                     "detected_at", cutoff_14),
        ("facility_price_spikes",            "detected_at", cutoff_14),
        ("events_price_spikes",              "detected_at", cutoff_14),
        ("lysted_sold_out_alerts",           "detected_at", cutoff_14),
    ]

    summary: dict = {}

    with _ENGINE.begin() as conn:
        for table, ts_col, cutoff in _PRUNE_RULES:
            result = conn.exec_driver_sql(
                f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,)
            )
            deleted = result.rowcount
            if deleted:
                _log.info("prune_old_data: %s — removed %d rows", table, deleted)
            summary[table] = deleted

        # Keep only the 5 most recent Lysted uploads + their listing rows.
        old_upload_ids = [
            row[0] for row in conn.exec_driver_sql(
                "SELECT id FROM lysted_uploads "
                "WHERE id NOT IN "
                "(SELECT id FROM lysted_uploads ORDER BY uploaded_at DESC LIMIT 5)"
            ).fetchall()
        ]
        if old_upload_ids:
            ph = ",".join("?" * len(old_upload_ids))
            conn.exec_driver_sql(
                f"DELETE FROM lysted_listings WHERE upload_id IN ({ph})",
                old_upload_ids,
            )
            conn.exec_driver_sql(
                f"DELETE FROM lysted_uploads WHERE id IN ({ph})",
                old_upload_ids,
            )
            _log.info("prune_old_data: removed %d old Lysted upload(s)", len(old_upload_ids))
            summary["lysted_uploads_removed"] = len(old_upload_ids)

        # Keep only the 5 most recent Stagefront syncs + their listing rows.
        old_sync_ids = [
            row[0] for row in conn.exec_driver_sql(
                "SELECT id FROM stagefront_syncs "
                "WHERE id NOT IN "
                "(SELECT id FROM stagefront_syncs ORDER BY synced_at DESC LIMIT 5)"
            ).fetchall()
        ]
        if old_sync_ids:
            ph = ",".join("?" * len(old_sync_ids))
            conn.exec_driver_sql(
                f"DELETE FROM stagefront_listings WHERE sync_id IN ({ph})",
                old_sync_ids,
            )
            conn.exec_driver_sql(
                f"DELETE FROM stagefront_syncs WHERE id IN ({ph})",
                old_sync_ids,
            )
            _log.info("prune_old_data: removed %d old Stagefront sync(s)", len(old_sync_ids))
            summary["stagefront_syncs_removed"] = len(old_sync_ids)

    # VACUUM must run outside any transaction in SQLite — use a raw connection
    # in autocommit mode so freed pages are returned to the OS immediately.
    raw = _ENGINE.raw_connection()
    try:
        raw.isolation_level = None   # sqlite3 autocommit
        raw.execute("VACUUM")
        _log.info("prune_old_data: VACUUM complete")
    finally:
        raw.close()

    total = sum(v for v in summary.values() if isinstance(v, int))
    _log.info("prune_old_data: finished — %d total rows removed", total)
    return summary


def _ensure_column(table: str, column: str, ddl: str) -> None:
    """
    Additive SQLite migration. create_all only creates *missing tables*; it
    never adds a column to a table that already exists, so a model column
    added after first deploy would silently not exist in the live database.
    """
    with _ENGINE.begin() as conn:
        existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table}({column})")


def create_tables() -> None:
    Base.metadata.create_all(bind=_ENGINE)
    _ensure_column("scarcity_checks", "source", "VARCHAR(20)")
    _ensure_column("lot_checks", "lot_id", "VARCHAR(50)")
    _ensure_column("price_spikes", "is_our_lot", "BOOLEAN")
    _ensure_column("lysted_sold_out_alerts", "passes_secured", "INTEGER")
    _ensure_column("lysted_sold_out_alerts", "passes_left", "INTEGER")
    _ensure_column("lysted_sold_out_alerts", "source", "VARCHAR(20)")
    _ensure_column("lysted_sold_out_alerts", "reason", "VARCHAR(30)")
    _ensure_column("lysted_sold_out_alerts", "action", "VARCHAR(20)")
    _ensure_column("lysted_listings", "lysted_listing_id", "VARCHAR(30)")


def get_db():
    db: Session = _SessionFactory()
    try:
        yield db
    finally:
        db.close()


def get_session() -> Session:
    return _SessionFactory()
