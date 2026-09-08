"""
Upload a file to SharePoint via Microsoft Graph, using the same
client-credentials app the Repricing Tool already uses successfully
against this tenant (see ~/Desktop/reprising_tool/Repricing-tool-/Utility.py
— that app is already admin-consented, unlike the newer "Stubhub Scraper"
app registered for this project, which is still blocked on tenant admin
consent). Credentials live in backend/.env, not hardcoded here.

Usage:
    from sharepoint_upload import upload_file_to_sharepoint
    result = upload_file_to_sharepoint("Clusters_20260820.xlsx", workbook_bytes)
    print(result["web_url"])
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _get_access_token() -> str:
    tenant_id = os.getenv("SHAREPOINT_TENANT_ID", "").strip()
    client_id = os.getenv("SHAREPOINT_CLIENT_ID", "").strip()
    client_secret = os.getenv("SHAREPOINT_CLIENT_SECRET", "").strip()
    if not (tenant_id and client_id and client_secret):
        raise RuntimeError(
            "SHAREPOINT_TENANT_ID / SHAREPOINT_CLIENT_ID / SHAREPOINT_CLIENT_SECRET "
            "are not all set in backend/.env"
        )

    resp = requests.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def upload_file_to_sharepoint(file_name: str, content_bytes: bytes,
                               content_type: str = _XLSX_CONTENT_TYPE) -> Dict[str, Any]:
    """
    Upload content_bytes as file_name into the configured SharePoint folder
    (SHAREPOINT_REPORT_SITE_HOST / _SITE_PATH / _FOLDER_PATH in .env).

    Returns { file_name, web_url } — web_url is the real SharePoint link,
    usable directly in a Teams card or anywhere else.
    """
    token = _get_access_token()
    host = os.getenv("SHAREPOINT_REPORT_SITE_HOST", "yourspotrented.sharepoint.com").strip()
    site_path = os.getenv("SHAREPOINT_REPORT_SITE_PATH", "/sites/MaxParkArbLLC").strip()
    folder_path = os.getenv("SHAREPOINT_REPORT_FOLDER_PATH", "Stubhub Team/Pricing_Alerts").strip().strip("/")
    if not site_path.startswith("/"):
        site_path = f"/{site_path}"

    site_resp = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{host}:{site_path}?$select=id",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    site_resp.raise_for_status()
    site_id = site_resp.json()["id"]

    drive_resp = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive?$select=id,name",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    drive_resp.raise_for_status()
    drive_id = drive_resp.json()["id"]

    target_path = f"{folder_path}/{file_name}" if folder_path else file_name
    encoded_target_path = quote(target_path, safe="/")

    upload_resp = requests.put(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives/{drive_id}/root:/{encoded_target_path}:/content",
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        data=content_bytes, timeout=60,
    )
    upload_resp.raise_for_status()
    upload_data = upload_resp.json()

    logger.info("Uploaded %s to SharePoint: %s", file_name, upload_data.get("webUrl"))
    return {"file_name": file_name, "web_url": upload_data.get("webUrl", "")}
