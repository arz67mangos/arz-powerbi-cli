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


# Desktop windows are found by title: Desktop titles its window with the file name without extension
# ("Sales" for Sales.pbip), optionally followed by " - ..." / " • Last saved ...".
def _user32():
    if os.name != "nt":
        raise PowerBIError("Power BI Desktop control needs Windows")
    import ctypes
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # real pixel sizes for screenshots
    except Exception:
        pass
    return ctypes, wintypes, u, k


def _exe_of(pid: int) -> str:
    ctypes, wintypes, u, k = _user32()
    h = k.OpenProcess(0x1000, False, pid)                  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        return buf.value if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
    finally:
        k.CloseHandle(h)


def desktop_windows(stem: str | None = None) -> list[dict]:
    """Visible top-level Power BI Desktop windows: [{hwnd, pid, title}], optionally for one file stem."""
    ctypes, wintypes, u, k = _user32()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if u.IsWindowVisible(hwnd) and not u.GetWindow(hwnd, 4):       # no owner = main window
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                pid = wintypes.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append({"hwnd": hwnd, "pid": pid.value, "title": buf.value})
        return True

    u.EnumWindows(cb, 0)
    out = [w for w in found if Path(_exe_of(w["pid"])).stem.lower().startswith("pbidesktop")]
    if stem:
        s = stem.lower()
        out = [w for w in out if w["title"].lower() == s or w["title"].lower().startswith((s + " ", s + " "))]
    return out


def _pid_alive(pid: int) -> bool:
    ctypes, wintypes, u, k = _user32()
    h = k.OpenProcess(0x00100000, False, pid)              # SYNCHRONIZE
    if not h:
        return False
    try:
        return k.WaitForSingleObject(h, 0) == 0x102        # WAIT_TIMEOUT = still running
    finally:
        k.CloseHandle(h)


def close_desktop(stem: str, timeout: float = 30) -> list[int]:
    """Ask Desktop windows showing STEM to close (like clicking X) and wait for them to exit.

    Never answers dialogs: if Desktop asks to save (or shows auto-recovery) it stays open and this
    raises, because 'Save' would overwrite edits made on disk and 'Don't save' drops GUI edits."""
    ctypes, wintypes, u, k = _user32()
    wins = desktop_windows(stem)
    for w in wins:
        u.PostMessageW(w["hwnd"], 0x0010, 0, 0)            # WM_CLOSE
    pids = sorted({w["pid"] for w in wins})
    end = time.time() + timeout
    while time.time() < end and any(_pid_alive(p) for p in pids):
        time.sleep(0.5)
    left = [p for p in pids if _pid_alive(p)]
    if left:
        raise PowerBIError(f"Power BI Desktop ({stem}) is still open after {timeout:g}s: it is probably asking "
                           "to save changes. In Desktop choose 'Don't save' to keep the CLI's edits on disk "
                           "('Save' would overwrite them), then run the command again.")
    return pids


def wait_for_desktop(stem: str, timeout: float = 120, settle: float = 15) -> dict:
    """Wait until a Desktop window for STEM appears, then SETTLE seconds for visuals to render."""
    end = time.time() + timeout
    while time.time() < end:
        wins = desktop_windows(stem)
        if wins:
            time.sleep(settle)
            return desktop_windows(stem)[0] if desktop_windows(stem) else wins[0]
        time.sleep(1)
    raise PowerBIError(f"No Power BI Desktop window for '{stem}' appeared within {timeout:g}s")


def screenshot_desktop(stem: str, out: str) -> dict:
    """Save a PNG of the Desktop window for STEM (works while it is covered by other windows)."""
    import struct
    import zlib
    ctypes, wintypes, u, k = _user32()
    wins = desktop_windows(stem)
    if not wins:
        raise PowerBIError(f"No Power BI Desktop window is showing '{stem}'; run 'desktop open' or 'desktop reload'")
    hwnd = wins[0]["hwnd"]
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)                              # SW_RESTORE: minimised windows render nothing
        time.sleep(2)
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    g = ctypes.windll.gdi32
    hdc = u.GetWindowDC(hwnd)
    mdc = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    g.SelectObject(mdc, bmp)
    ok = u.PrintWindow(hwnd, mdc, 2)                       # PW_RENDERFULLCONTENT (WebView/GPU content)
    hdr = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)   # top-down 32-bit BITMAPINFOHEADER
    buf = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.c_char_p(hdr), 0)
    g.DeleteObject(bmp)
    g.DeleteDC(mdc)
    u.ReleaseDC(hwnd, hdc)
    if not ok:
        raise PowerBIError("Windows couldn't capture the Desktop window (PrintWindow failed)")
    raw = buf.raw
    rgb = bytearray()                                      # BGRA rows -> filter byte + RGB
    for y in range(h):
        row = raw[y * w * 4:(y + 1) * w * 4]
        line = bytearray(w * 3)
        line[0::3], line[1::3], line[2::3] = row[2::4], row[1::4], row[0::4]
        rgb += b"\x00" + line

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(bytes(rgb), 6)) + chunk(b"IEND", b""))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_bytes(png)
    return {"screenshot": str(Path(out).resolve()), "width": w, "height": h, "title": wins[0]["title"]}
