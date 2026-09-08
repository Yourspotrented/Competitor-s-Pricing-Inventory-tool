from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, create_engine
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

_DB_PATH = os.getenv("DATABASE_PATH", "./scarcity.db")
_ENGINE = create_engine(f"sqlite:///{_DB_PATH}", connect_args={"check_same_thread": False})
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
    event_name = Column(String(500), nullable=True)
    event_date = Column(String(50), nullable=True)
    venue = Column(String(500), nullable=True)
    city = Column(String(200), nullable=True)
    state = Column(String(50), nullable=True)
    section = Column(String(500), nullable=True)
    platforms = Column(String(100), nullable=True)               # "spothero" / "parkwhiz" / both, comma-joined
    quantity = Column(Integer, nullable=True)
    list_price = Column(Float, nullable=True)
    previous_levels = Column(String(100), nullable=True)         # what each platform showed last scan
    detected_at = Column(DateTime, default=utcnow, index=True)
    notified = Column(Boolean, default=False)


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


def get_db():
    db: Session = _SessionFactory()
    try:
        yield db
    finally:
        db.close()


def get_session() -> Session:
    return _SessionFactory()
