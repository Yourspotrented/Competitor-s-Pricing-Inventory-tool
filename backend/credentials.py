"""
Where the platforms' hand-refreshed logins live.

Lysted gives us a 24-hour browser token, ReachPro a session cookie, and
neither issues an API key for this account. Rather than a developer editing
.env and redeploying, the team pastes a fresh one on the dashboard and it is
stored here (2026-09-22 call: "for them to be able to update the tokens for
now. Pending [until] we have a better API").

Environment variables still work and act as the fallback, so local runs and
the day a real key arrives both keep working.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def stored(source: str) -> str:
    """The most recently pasted credential for this platform, or ""."""
    from database import PlatformCredential, get_session
    db = get_session()
    try:
        row = (db.query(PlatformCredential)
               .filter(PlatformCredential.source == source)
               .order_by(PlatformCredential.saved_at.desc(), PlatformCredential.id.desc())
               .first())
        return (row.token or "").strip() if row else ""
    except Exception as exc:           # table missing on an older database
        logger.debug("No stored %s credential (%s)", source, exc)
        return ""
    finally:
        db.close()


def current(source: str, env_var: str) -> str:
    """What to authenticate with: the dashboard paste first, else the env var."""
    return stored(source) or os.getenv(env_var, "").strip()


def save(source: str, token: str, expires_at: Optional[datetime] = None,
         saved_by: str = "") -> Dict[str, Any]:
    """Store a pasted credential. Validation belongs to the caller."""
    from database import PlatformCredential, create_tables, get_session
    create_tables()
    db = get_session()
    try:
        row = PlatformCredential(source=source, token=token.strip(),
                                 expires_at=expires_at, saved_by=saved_by or None)
        db.add(row)
        db.commit()
        saved_at = row.saved_at
    finally:
        db.close()
    logger.info("%s credential saved%s", source,
                f", valid until {expires_at.isoformat()}" if expires_at else "")
    return {"status": "saved", "source": source,
            "saved_at": saved_at.isoformat() if saved_at else None,
            "expires_at": expires_at.isoformat() if expires_at else None}


def last_saved(source: str) -> Optional[Dict[str, Any]]:
    """When this platform's credential was last pasted, and by whom."""
    from database import PlatformCredential, get_session
    db = get_session()
    try:
        row = (db.query(PlatformCredential)
               .filter(PlatformCredential.source == source)
               .order_by(PlatformCredential.saved_at.desc(), PlatformCredential.id.desc())
               .first())
        if row is None:
            return None
        return {"saved_at": row.saved_at.isoformat() if row.saved_at else None,
                "saved_by": row.saved_by,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None}
    except Exception:
        return None
    finally:
        db.close()
