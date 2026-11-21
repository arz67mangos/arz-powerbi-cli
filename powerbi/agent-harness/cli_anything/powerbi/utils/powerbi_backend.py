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


# ── UI Automation (Desktop's UI is WebView2; elements are found by exact accessible name) ──
_UIA_PS = r'''
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
function Find-All($root, $name, $type) {
  $out = New-Object System.Collections.ArrayList
  $stack = New-Object System.Collections.Stack; $stack.Push($root); $n = 0
  while ($stack.Count -gt 0 -and $n -lt 40000) {
    $e = $stack.Pop(); $c = $walker.GetFirstChild($e)
    while ($c -ne $null) { $n++
      if ($c.Current.Name -ceq $name -and ($type -eq "" -or $c.Current.ControlType.ProgrammaticName -eq "ControlType.$type")) { [void]$out.Add($c) }
      $stack.Push($c); $c = $walker.GetNextSibling($c) }
  }
  return ,$out
}
function In-View($e, $view) {
  if ($view -eq "") { return $true }
  $p = $walker.GetParent($e); $k = 0
  while ($p -ne $null -and $k -lt 80) { if ($p.Current.Name -like "*$view.html - Web content") { return $true }; $p = $walker.GetParent($p); $k++ }
  return $false
}
$pc = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, [int]__PID__)
$tops = $AE::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $pc)
$action = '__ACTION__'; $name = '__NAME__'; $type = '__TYPE__'; $view = '__VIEW__'; $title = '__TITLE__'
$hits = @()
foreach ($t in $tops) {
  if ($title -ne "" -and -not ($t.Current.Name -like "*$title*")) { continue }
  foreach ($h in (Find-All $t $name $type)) { if (In-View $h $view) { $hits += ,@($t, $h) } }
}
if ($action -eq "windows") { foreach ($t in $tops) { "WIN`t" + $t.Current.Name + "`t" + $t.Current.ClassName }; exit 0 }
if ($hits.Count -eq 0) { "NOTFOUND"; exit 0 }
$el = $hits[0][1]
if ($action -eq "exists") { "FOUND`t" + $hits[0][0].Current.Name; exit 0 }
if ($action -eq "invoke") { $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); "OK`t" + $hits[0][0].Current.Name; exit 0 }
if ($action -eq "select") { $el.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select(); "OK`t" + $hits[0][0].Current.Name; exit 0 }
'''


def _wake_accessibility(hwnd: int):
    """Chromium builds its accessibility tree only once a client asks; poke each render host via MSAA."""
    ctypes, wintypes, u, k = _user32()
    hosts = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        buf = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(h, buf, 64)
        if buf.value in ("Chrome_RenderWidgetHostHWND", "Internet Explorer_Server"):  # WebView2 UI; IE-hosted dialogs
            hosts.append(h)
        return True
    u.EnumChildWindows(hwnd, cb, 0)

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
    iid = GUID(0x618736E0, 0x3C3D, 0x11CF, (ctypes.c_ubyte * 8)(0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71))
    for h in hosts:                                         # IID_IAccessible, OBJID_CLIENT
        p = ctypes.c_void_p()                               # ponytail: object not released; CLI process is short-lived
        ctypes.windll.oleacc.AccessibleObjectFromWindow(h, ctypes.c_long(-4), ctypes.byref(iid), ctypes.byref(p))
    return len(hosts)


def uia(pid: int, action: str, name: str = "", ctype: str = "", view: str = "", title: str = "") -> str:
    """Run one UI Automation action in the Desktop process PID. Names match exactly (case-sensitive).
    action: exists | invoke | select | windows. Returns the script's first output line."""
    vals = {"__PID__": str(int(pid)), "__ACTION__": action, "__NAME__": name, "__TYPE__": ctype,
            "__VIEW__": view, "__TITLE__": title}
    script = _UIA_PS
    for k_, v in vals.items():
        script = script.replace(k_, v.replace("'", "''"))
    return _run_ps(script)


def _main_window(stem: str) -> dict:
    wins = desktop_windows(stem)
    if not wins:
        raise PowerBIError(f"No Power BI Desktop window is showing '{stem}'")
    return wins[0]


def refresh_desktop(stem: str, timeout: float = 600) -> dict:
    """Click the report's 'Refresh now' button (the 'incomplete or no data' / 'calculated tables need to be
    refreshed' bar) and wait until the bar and the refresh dialog are gone. Exact name match only."""
    w = _main_window(stem)
    _wake_accessibility(w["hwnd"])
    time.sleep(1)
    if uia(w["pid"], "invoke", "Refresh now", "Button", "reportView").startswith("NOTFOUND"):
        return {"refreshed": False, "note": "no 'Refresh now' button (data already loaded)"}
    start = time.time()
    time.sleep(3)
    while time.time() - start < timeout:
        bar = uia(w["pid"], "exists", "Refresh now", "Button", "reportView").startswith("FOUND")
        extra = [l for l in uia(w["pid"], "windows").splitlines() if l.startswith("WIN\t")]
        if not bar and len(extra) <= 1:             # only the main window: refresh dialog closed
            return {"refreshed": True, "seconds": round(time.time() - start, 1)}
        time.sleep(2)
    raise PowerBIError(f"Refresh still running after {timeout:g}s")


def show_page(stem: str, page: str) -> bool:
    """Switch the open report to PAGE by selecting its page tab (exact display name)."""
    w = _main_window(stem)
    _wake_accessibility(w["hwnd"])
    return uia(w["pid"], "select", page, "TabItem", "reportView").startswith("OK")


# The save prompt is an IE-hosted page that UIA can't see into; MSAA can. Role 43 = push button.
_MSAA_PS = r'''
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName Accessibility
Add-Type -ReferencedAssemblies Accessibility -TypeDefinition @"
using System; using System.Collections.Generic; using System.Runtime.InteropServices; using Accessibility;
public static class Msaa {
  [DllImport("oleacc.dll")] static extern int AccessibleObjectFromWindow(IntPtr h, uint id, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out object o);
  [DllImport("oleacc.dll")] static extern int AccessibleChildren(IAccessible p, int start, int n, [Out] object[] kids, out int got);
  static string S(Func<object> f) { try { var r = f(); return r == null ? "" : r.ToString(); } catch { return ""; } }
  public static string Run(long hwnd, string prompt, string button, bool click) {
    Guid g = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71"); object o;
    AccessibleObjectFromWindow(new IntPtr(hwnd), 0xFFFFFFFC, ref g, out o);
    var names = new List<string>(); IAccessible hit = null; Walk((IAccessible)o, 0, prompt, button, names, ref hit);
    if (!names.Contains(prompt)) return "NOPROMPT";
    if (hit == null) return "NOTFOUND";
    if (click) hit.accDoDefaultAction(0);
    return "OK";
  }
  static void Walk(IAccessible a, int d, string prompt, string button, List<string> names, ref IAccessible hit) {
    int n = 0; try { n = a.accChildCount; } catch { }
    if (n <= 0 || d > 30) return;
    object[] k = new object[n]; int got; AccessibleChildren(a, 0, n, k, out got);
    for (int i = 0; i < got; i++) {
      var c = k[i] as IAccessible;
      if (c == null) { object id = k[i]; names.Add(S(() => a.get_accName(id))); continue; }
      string name = S(() => c.get_accName(0)); names.Add(name);
      if (hit == null && name == button && S(() => c.get_accRole(0)) == "43") hit = c;
      Walk(c, d + 1, prompt, button, names, ref hit);
    }
  }
}
"@
[Msaa]::Run(__HWND__, 'Do you want to save your changes?', "Don't save", $__CLICK__)
'''


def _run_ps(script: str) -> str:
    import base64
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", enc], capture_output=True, text=True, timeout=120)
    out = (r.stdout or "").strip()
    if r.returncode != 0 and not out:
        raise PowerBIError(f"UI Automation failed: {(r.stderr or '').strip()[:300]}")
    return out


def _save_prompts(pid: int) -> list[int]:
    """IE-server hwnds inside visible 'Microsoft Power BI Desktop' windows of PID (the save prompt)."""
    ctypes, wintypes, u, k = _user32()
    tops, hosts = [], []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def top(h, _):
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        buf = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(h, buf, 256)
        if p.value == pid and u.IsWindowVisible(h) and buf.value == "Microsoft Power BI Desktop":
            tops.append(h)
        return True
    u.EnumWindows(top, 0)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def child(h, _):
        buf = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(h, buf, 64)
        if buf.value == "Internet Explorer_Server":
            hosts.append(h)
        return True
    for t in tops:
        u.EnumChildWindows(t, child, 0)
    return hosts


def discard_and_close(stem: str, pid: int, click: bool = True) -> bool:
    """If Desktop process PID shows 'Do you want to save your changes?', invoke ONLY the push button
    named exactly "Don't save" (never Save/Cancel). Returns True if it did (or, click=False, would)."""
    for h in _save_prompts(pid):
        script = _MSAA_PS.replace("__HWND__", str(int(h))).replace("$__CLICK__", "$true" if click else "$false")
        if _run_ps(script).startswith("OK"):
            return True
    return False


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
