"""Repeated scans that alert when a new contract clears the minimum ROI.

`tradeup watch` loops in the terminal; `tradeup watch --once` runs a single pass, for Windows Task Scheduler.
Contracts already alerted on (same set of listings) are remembered in data/alerts_seen.json, so a restart
doesn't repeat them. Every alert is also appended to data/alerts.log with its buy links.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .actions import run_scan
from .catalog import Catalog
from .config import DATA_DIR, Config
from .display import console, contract_kind, money, pct, print_result, signed_money
from .evaluate import Result

SEEN_FILE = DATA_DIR / "alerts_seen.json"
LOG_FILE = DATA_DIR / "alerts.log"
SEEN_LIMIT = 1000


def result_key(res: Result) -> str:
    return "|".join(sorted(f"{l.market}:{l.listing_id}" for l in res.listings))


def load_seen(path: Path | None = None) -> list[str]:
    path = path or SEEN_FILE
    try:
        return list(json.loads(path.read_text(encoding="utf8")))
    except (OSError, ValueError):
        return []


def save_seen(seen: list[str], path: Path | None = None) -> None:
    path = path or SEEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seen[-SEEN_LIMIT:]), encoding="utf8")


def new_results(results: list[Result], seen: list[str]) -> list[Result]:
    known = set(seen)
    out = []
    for r in results:
        k = result_key(r)
        if k not in known:
            known.add(k)
            out.append(r)
    return out


def log_alert(res: Result, path: Path | None = None) -> None:
    path = path or LOG_FILE
    lines = [f"[{datetime.now():%Y-%m-%d %H:%M}] {res.label}  {contract_kind(res)}",
             f"  cost {money(res.cost)}, expected return {money(res.ev)}, profit {signed_money(res.profit)} "
             f"({pct(res.roi)}), win chance {res.profit_chance * 100:.0f}%"]
    lines += [f"  {i.source}: {i.skin.name} float {i.float_value:.6f} {money(i.price)}  {i.link}"
              for i in res.contract.inputs]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf8") as fh:
        fh.write("\n".join(lines) + "\n\n")


def notify(title: str, text: str) -> None:
    """Windows toast notification through PowerShell (no extra packages); a beep anywhere else or on failure."""
    if sys.platform == "win32":
        esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
            "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
            f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{esc(title)}</text>"
            f"<text>{esc(text)}</text></binding></visual></toast>');"
            "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
            "[Windows.UI.Notifications.ToastNotification]::new($x))")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    print("\a", end="", flush=True)


def watch(cfg_loader, tiers, modes, targets, souvenir: bool, top: int, min_roi: float, every_minutes: float,
          once: bool = False, budget: float | None = None) -> None:
    cat = Catalog()
    seen = load_seen()
    while True:
        cfg: Config = cfg_loader()   # re-read each pass so config.toml edits apply without a restart
        if budget is not None:
            cfg["scan"]["max_contract_cost"] = budget
        started = datetime.now()
        console.rule(f"[bold]Scan {started:%H:%M}[/]")
        try:
            out = run_scan(cfg, cat, tiers, modes, targets, souvenir, top, min_roi, show_coarse=False)
        except Exception as e:   # a failed pass (network, API) must not end an overnight watch
            console.print(f"[red]Scan failed:[/] {e}")
            out = None
        if out is not None:
            if not out.deep_ran:
                console.print("[red]No market API keys are set, so watch can't check real listings.[/]")
                return
            fresh = new_results(out.results, seen)
            for r in fresh:
                print_result(r)
                log_alert(r)
                notify(f"Trade-up: {pct(r.roi)} expected",
                       f"{r.label} - cost {money(r.cost)}, profit {signed_money(r.profit)}, "
                       f"win chance {r.profit_chance * 100:.0f}%")
            seen += [result_key(r) for r in fresh]
            save_seen(seen)
            console.print(f"{len(out.results)} contract(s) at or above {min_roi * 100:.1f}% ROI, "
                          f"{len(fresh)} new." + (f" Details saved to {LOG_FILE}." if fresh else ""))
        if once:
            return
        wake = time.time() + every_minutes * 60
        console.print(f"[dim]Next scan at {datetime.fromtimestamp(wake):%H:%M}. Press Ctrl+C to stop.[/]")
        time.sleep(max(0.0, wake - time.time()))
