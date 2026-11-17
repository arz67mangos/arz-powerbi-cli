"""Backend: the real Power BI service (REST API via MSAL auth) and Power BI Desktop."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import requests

from cli_anything.powerbi.core.session import state_dir

SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]
# Azure CLI's public client id — preauthorised for the Power BI API in every tenant.
DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"


class PowerBIError(Exception):
    pass


def api_base() -> str:
    return os.environ.get("POWERBI_API_BASE", "https://api.powerbi.com/v1.0/myorg").rstrip("/")


# ── Auth ────────────────────────────────────────────────────────────────

def _cache():
    import msal
    cache = msal.SerializableTokenCache()
    p = state_dir() / "token_cache.json"
    if p.exists():
        cache.deserialize(p.read_text(encoding="utf-8"))
    return cache, p


def _save_cache(cache, p):
    if cache.has_state_changed:
        p.write_text(cache.serialize(), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def _public_app(tenant=None):
    import msal
    cache, p = _cache()
    app = msal.PublicClientApplication(
        os.environ.get("POWERBI_CLIENT_ID", DEFAULT_CLIENT_ID),
        authority=f"https://login.microsoftonline.com/{tenant or os.environ.get('POWERBI_TENANT_ID', 'organizations')}",
        token_cache=cache)
    return app, cache, p


def _service_principal() -> bool:
    return all(os.environ.get(k) for k in ("POWERBI_TENANT_ID", "POWERBI_CLIENT_ID", "POWERBI_CLIENT_SECRET"))


def auth_mode() -> str:
    if os.environ.get("POWERBI_ACCESS_TOKEN"):
        return "env-token"
    if _service_principal():
        return "service-principal"
    return "user"


def login(interactive: bool = False, tenant: str | None = None, echo=print) -> dict:
    """Sign in a user (device code by default, browser with interactive=True)."""
    if auth_mode() == "service-principal":
        get_token()
        return status()
    app, cache, p = _public_app(tenant)
    if interactive:
        res = app.acquire_token_interactive(SCOPE)
    else:
        flow = app.initiate_device_flow(scopes=SCOPE)
        if "user_code" not in flow:
            raise PowerBIError(f"Device flow failed: {flow.get('error_description', flow)}")
        echo(flow["message"])
        res = app.acquire_token_by_device_flow(flow)
    _save_cache(cache, p)
    if "access_token" not in res:
        raise PowerBIError(f"Login failed: {res.get('error_description', res.get('error'))}")
    return status()


def logout() -> dict:
    p = state_dir() / "token_cache.json"
    existed = p.exists()
    if existed:
        p.unlink()
    return {"logged_out": existed}


def get_token() -> str:
    tok = os.environ.get("POWERBI_ACCESS_TOKEN")
    if tok:
        return tok
    import msal
    if _service_principal():
        app = msal.ConfidentialClientApplication(
            os.environ["POWERBI_CLIENT_ID"],
            authority=f"https://login.microsoftonline.com/{os.environ['POWERBI_TENANT_ID']}",
            client_credential=os.environ["POWERBI_CLIENT_SECRET"])
        res = app.acquire_token_for_client(scopes=SCOPE)
    else:
        app, cache, p = _public_app()
        accounts = app.get_accounts()
        res = app.acquire_token_silent(SCOPE, account=accounts[0]) if accounts else None
        _save_cache(cache, p)
    if not res or "access_token" not in res:
        raise PowerBIError("Not signed in to Power BI. Run: cli-anything-powerbi auth login "
                           "(or set POWERBI_ACCESS_TOKEN, or POWERBI_TENANT_ID/CLIENT_ID/CLIENT_SECRET)")
    return res["access_token"]


def status() -> dict:
    mode = auth_mode()
    out = {"mode": mode, "api_base": api_base()}
    if mode == "user":
        app, _, _ = _public_app()
        accts = app.get_accounts()
        out["signed_in"] = bool(accts)
        out["account"] = accts[0].get("username") if accts else None
    else:
        out["signed_in"] = True
    return out


# ── REST ────────────────────────────────────────────────────────────────

def call(method: str, path: str, body=None, params=None, raw=False):
    url = api_base() + path
    try:
        r = requests.request(method, url, json=body, params=params, timeout=120,
                             headers={"Authorization": f"Bearer {get_token()}"})
    except requests.RequestException as e:
        raise PowerBIError(f"{method} {url} failed: {e}") from e
    if r.status_code >= 400:
        try:
            detail = r.json().get("error", r.json())
        except ValueError:
            detail = r.text[:500]
        raise PowerBIError(f"HTTP {r.status_code} on {method} {path}: {json.dumps(detail)}")
    if raw:
        return r
    return r.json() if r.content else {}


def _grp(ws: str | None) -> str:
    return f"/groups/{ws}" if ws else ""


def list_workspaces(filter_name: str | None = None) -> list[dict]:
    params = {"$filter": f"contains(name,'{filter_name}')"} if filter_name else None
    return call("GET", "/groups", params=params).get("value", [])


def resolve_workspace(ref: str | None) -> str | None:
    """id or name -> id. None / 'me' / 'my' -> My workspace."""
    if not ref or ref.lower() in ("me", "my", "myworkspace"):
        return None
    if len(ref) == 36 and ref.count("-") == 4:
        return ref
    hits = [w for w in list_workspaces() if w["name"].lower() == ref.lower()]
    if not hits:
        raise PowerBIError(f"Workspace '{ref}' not found")
    return hits[0]["id"]


def list_datasets(ws=None) -> list[dict]:
    return call("GET", f"{_grp(ws)}/datasets").get("value", [])


def resolve_dataset(ref: str, ws=None) -> str:
    if len(ref) == 36 and ref.count("-") == 4:
        return ref
    hits = [d for d in list_datasets(ws) if d["name"].lower() == ref.lower()]
    if not hits:
        raise PowerBIError(f"Dataset '{ref}' not found in {'workspace ' + ws if ws else 'My workspace'}")
    return hits[0]["id"]


def get_dataset(ds, ws=None) -> dict:
    return call("GET", f"{_grp(ws)}/datasets/{ds}")


def list_reports(ws=None) -> list[dict]:
    return call("GET", f"{_grp(ws)}/reports").get("value", [])


def refresh(ds, ws=None, refresh_type: str | None = None) -> dict:
    body = {"notifyOption": "NoNotification"}
    if refresh_type:  # enhanced refresh (Premium/Fabric capacity)
        body = {"type": refresh_type}
    r = call("POST", f"{_grp(ws)}/datasets/{ds}/refreshes", body=body, raw=True)
    return {"dataset_id": ds, "accepted": r.status_code == 202,
            "request_id": r.headers.get("RequestId") or r.headers.get("x-ms-request-id")}


def refresh_history(ds, ws=None, top=10) -> list[dict]:
    return call("GET", f"{_grp(ws)}/datasets/{ds}/refreshes", params={"$top": top}).get("value", [])


def wait_refresh(ds, ws=None, timeout=1800, interval=10, sleep=time.sleep) -> dict:
    """Poll until the latest refresh leaves 'Unknown' (= in progress)."""
    deadline = time.monotonic() + timeout
    while True:
        hist = refresh_history(ds, ws, top=1)
        if hist and hist[0].get("status") != "Unknown":
            return hist[0]
        if time.monotonic() > deadline:
            raise PowerBIError(f"Refresh still running after {timeout}s")
        sleep(interval)


def execute_dax(ds, query: str, ws=None) -> dict:
    """POST executeQueries; returns {"columns": [...], "rows": [...]} for the first table."""
    body = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}
    res = call("POST", f"{_grp(ws)}/datasets/{ds}/executeQueries", body=body)
    result = (res.get("results") or [{}])[0]
    if result.get("error"):
        raise PowerBIError(f"DAX error: {json.dumps(result['error'])}")
    tables = result.get("tables") or [{"rows": []}]
    rows = tables[0].get("rows", [])
    cols = list(rows[0].keys()) if rows else []
    return {"columns": cols, "rows": rows, "row_count": len(rows)}


# ── Power BI Desktop ────────────────────────────────────────────────────

def find_desktop() -> str:
    for name in ("PBIDesktop", "PBIDesktopStore"):
        p = shutil.which(name)
        if p:
            return p
    for p in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft Power BI Desktop/bin/PBIDesktop.exe",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WindowsApps/PBIDesktopStore.exe"):
        if p.exists():
            return str(p)
    raise PowerBIError("Power BI Desktop not found. Install it from the Microsoft Store "
                       "(winget install Microsoft.PowerBI) or https://aka.ms/pbidesktop")


def open_in_desktop(path: str) -> dict:
    exe = find_desktop()
    proc = subprocess.Popen([exe, str(Path(path).resolve())])
    return {"desktop": exe, "opened": str(Path(path).resolve()), "pid": proc.pid}
