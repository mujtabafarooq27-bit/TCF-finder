#!/usr/bin/env python3
"""
TCF exam availability watcher for Alliance Française Vancouver, Calgary,
and Edmonton.

Vancouver & Edmonton: both use a table listing (Exam date / Registration
dates / Location / Spots left / Price / Bookings). We parse that table
and flag any row that currently has spots left and isn't "Sold Out" /
"Opens in ...".

Calgary: the registration-process page lists months that are either
"SOLD OUT" or have a "Registrations" link. We follow every such link and
check the linked page too, since a month can show a live "Registrations"
button while the page behind it is actually sold out.

In all cases we ALSO keep a full-text hash per page as a safety net, so
if the structured parsing ever misses something (site redesign, unusual
wording), you still get notified that the page changed even if we can't
say exactly what changed.

This only reads pages and emails you - it never registers or pays for
anything on your behalf.
"""

import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).parent / "state.json"
TIMEOUT_SECONDS = 20
USER_AGENT = (
    "Mozilla/5.0 (compatible; TCF-Slot-Watcher/1.0; "
    "personal monitoring script, low frequency, not a bulk scraper)"
)

NOT_OPEN_PHRASES = ("sold out", "closed", "full")

# ---------------------------------------------------------------------
# EDIT THIS: your three pages.
# ---------------------------------------------------------------------
VANCOUVER_URL = "https://www.alliancefrancaise.ca/en/language/exams/tcf-canada/?s8-datatable1_rows=75"
CALGARY_URL = "https://www.afcalgary.ca/exams/tcf/registration-process/"
EDMONTON_URL = "https://www.afedmonton.com/en/exams/tcf/?s8-datatable1_rows=75"


def get_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def page_text(soup: BeautifulSoup) -> str:
    scope = BeautifulSoup(str(soup), "html.parser")
    for tag in scope(["script", "style"]):
        tag.decompose()
    text = scope.get_text(separator="\n", strip=True)
    return "\n".join(line for line in text.splitlines() if line.strip())


# ---------------------------------------------------------------------
# Vancouver / Edmonton: table-style pages
# ---------------------------------------------------------------------

REGISTER_WINDOW_RE = re.compile(
    r"Register within\s+(.+?)\s*[-\u2013\u2014]\s*(.+?)(?:\n|Have an Account|$)",
    re.IGNORECASE,
)


def find_register_window(text: str) -> str | None:
    """Look for an exact 'Register within <start> - <end>' phrase, which
    some session pages render as plain static text (not JS-computed)."""
    match = REGISTER_WINDOW_RE.search(text)
    if match:
        return f"{match.group(1).strip()} to {match.group(2).strip()}"
    return None


def parse_spot_table(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Best-effort parse of a spots-left table into row dicts.

    Falls back to an empty list (never raises) if no table is found -
    the caller still has the full-page text hash as a safety net.

    For each row, if a link is present, we follow it and try to pull an
    exact "Register within <start> - <end>" phrase from that page, since
    the summary table's own "Opens in ..." cell may be filled in by
    JavaScript rather than being present in the static HTML.
    """
    rows_out = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if not cells:
                continue

            spots_left = None
            status_bits = []
            has_price = any(c.startswith("$") for c in cells)
            for cell in cells:
                if spots_left is None and re.fullmatch(r"\d{1,3}", cell):
                    spots_left = int(cell)
                lower = cell.lower()
                if any(phrase in lower for phrase in NOT_OPEN_PHRASES):
                    status_bits.append(cell)

            # Skip rows that don't look like real session rows at all
            # (headers, labels) - a real row always has a spot count, a
            # price, or a recognized "not open" phrase.
            if spots_left is None and not status_bits and not has_price:
                continue

            # Anything that ISN'T explicitly "Sold Out" / "Closed" / "Full"
            # counts as worth telling you about - including "Opens in ...",
            # "Book now", or wording we haven't seen before.
            is_open = not status_bits

            # A stable identifier for this session that ignores the
            # spots/price/status columns, so we can tell "still open"
            # apart from "just opened" across runs.
            identity_cells = [
                c
                for c in cells
                if not re.fullmatch(r"\d{1,3}", c)
                and not c.startswith("$")
                and not any(p in c.lower() for p in NOT_OPEN_PHRASES)
            ]
            session_key = " | ".join(identity_cells) or " | ".join(cells)

            register_window = None
            link = tr.find("a", href=True)
            if link:
                sub_url = urljoin(base_url, link["href"])
                try:
                    sub_text = page_text(get_soup(sub_url))
                    register_window = find_register_window(sub_text)
                except Exception:  # noqa: BLE001 - don't let a bad link break the whole check
                    pass

            rows_out.append(
                {
                    "raw": " | ".join(cells),
                    "session_key": session_key,
                    "spots_left": spots_left,
                    "status": ", ".join(status_bits) if status_bits else None,
                    "is_open": is_open,
                    "register_window": register_window,
                }
            )
    return rows_out


def check_simple_table(name: str, url: str) -> dict:
    soup = get_soup(url)
    text = page_text(soup)
    rows = parse_spot_table(soup, url)
    open_rows = [r for r in rows if r["is_open"]]
    return {
        "name": name,
        "url": url,
        "text_hash": hash_text(text),
        "text": text,
        "rows": rows,
        "open_rows": open_rows,
        "kind": "table",
    }


# ---------------------------------------------------------------------
# Calgary: month list + per-month registration link check
# ---------------------------------------------------------------------

def check_calgary(name: str, url: str) -> dict:
    soup = get_soup(url)
    text = page_text(soup)

    reg_links = {}
    for a in soup.find_all("a", href=True):
        label = a.get_text(strip=True).lower()
        if label in ("registrations", "registration", "register", "register now"):
            reg_links[urljoin(url, a["href"])] = a.get_text(strip=True)

    sublinks = {}
    for link_url in reg_links:
        try:
            sub_soup = get_soup(link_url)
            sub_text = page_text(sub_soup)
            lower = sub_text.lower()
            actually_sold_out = "sold out" in lower or "missed the registration" in lower
            sublinks[link_url] = {
                "text_hash": hash_text(sub_text),
                "text": sub_text,
                "sold_out": actually_sold_out,
            }
        except Exception as exc:  # noqa: BLE001
            sublinks[link_url] = {"error": str(exc)}

    return {
        "name": name,
        "url": url,
        "text_hash": hash_text(text),
        "text": text,
        "sublinks": sublinks,
        "kind": "calgary",
    }


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------

def hash_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def send_email(subject: str, body: str) -> None:
    email_from = os.environ["EMAIL_ADDRESS"]
    email_password = os.environ["EMAIL_PASSWORD"]
    email_to = os.environ.get("EMAIL_TO", email_from)
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(email_from, email_password)
        server.sendmail(email_from, [email_to], msg.as_string())


def summarize_table_result(result: dict, prev: dict | None) -> tuple[bool, list[str]]:
    """Returns (should_alert, message_lines). Only alerts for sessions that
    are NEWLY open (weren't open on the previous run), so you don't get a
    fresh email every 30 minutes for the same still-open row."""
    lines = []
    should_alert = False
    prev_sessions = (prev or {}).get("sessions", {})

    newly_open = [
        r for r in result["open_rows"]
        if not prev_sessions.get(r["session_key"], {}).get("is_open")
    ]
    still_open = [
        r for r in result["open_rows"]
        if prev_sessions.get(r["session_key"], {}).get("is_open")
    ]

    if newly_open:
        should_alert = True
        lines.append(
            f"WORTH CHECKING ({len(newly_open)}) - not marked Sold Out / Closed / Full:"
        )
        for row in newly_open:
            lines.append(f"  - {row['raw']}")
            if row.get("register_window"):
                lines.append(f"    Registration window: {row['register_window']}")
        lines.append("")

    if still_open:
        lines.append(f"Still in that state from before ({len(still_open)}), FYI:")
        for row in still_open:
            lines.append(f"  - {row['raw']}")
        lines.append("")

    # Even rows that aren't open yet are worth surfacing if we found an
    # exact opening time for them - that's the "when to be ready" info.
    upcoming = [r for r in result["rows"] if not r["is_open"] and r.get("register_window")]
    if upcoming:
        lines.append("Known upcoming registration windows (not open yet):")
        for row in upcoming:
            lines.append(f"  - {row['raw']}")
            lines.append(f"    Registration window: {row['register_window']}")
        lines.append("")

    if prev is not None and prev.get("text_hash") != result["text_hash"] and not newly_open:
        # page changed but our structured parser didn't find a newly-open
        # row - tell the user anyway so nothing gets missed.
        should_alert = True
        lines.append(
            "Page content changed but the automatic parser didn't clearly "
            "identify a newly open slot. Check the page directly:"
        )
        lines.append(result["url"])
        lines.append("")

    return should_alert, lines


def summarize_calgary_result(result: dict, prev: dict | None) -> tuple[bool, list[str]]:
    lines = []
    should_alert = False
    prev_sublinks = (prev or {}).get("sublinks", {})

    for link_url, info in result["sublinks"].items():
        if "error" in info:
            continue
        was_known = link_url in prev_sublinks
        was_sold_out = prev_sublinks.get(link_url, {}).get("sold_out")

        if not was_known:
            should_alert = True
            status = "SOLD OUT" if info["sold_out"] else "NOT marked sold out - check now"
            lines.append(f"New registration link appeared: {link_url}\n  Status: {status}")
        elif was_sold_out and not info["sold_out"]:
            should_alert = True
            lines.append(f"Now open (was sold out before): {link_url}")

    if prev is not None and prev.get("text_hash") != result["text_hash"] and not should_alert:
        should_alert = True
        lines.append(
            "Main registration page changed in some other way. Check it directly:"
        )
        lines.append(result["url"])

    return should_alert, lines


def main() -> int:
    state = load_state()
    new_state = {}
    report_sections = []
    errors = []

    checks = [
        ("Vancouver", VANCOUVER_URL, check_simple_table),
        ("Edmonton", EDMONTON_URL, check_simple_table),
    ]

    for name, url, fn in checks:
        try:
            result = fn(name, url)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            print(f"[error] {name}: {exc}")
            continue

        prev = state.get(name)
        should_alert, lines = summarize_table_result(result, prev)
        new_state[name] = {
            "text_hash": result["text_hash"],
            "sessions": {
                row["session_key"]: {"is_open": row["is_open"], "spots_left": row["spots_left"]}
                for row in result["rows"]
            },
        }
        # Always print every row to the run log (not email) so you can check
        # the Actions log any time to see current spots/registration windows
        # without waiting for an alert.
        print(f"[{name}] {len(result['rows'])} row(s) found:")
        for row in result["rows"]:
            print(f"    {row['raw']}")
            if row.get("register_window"):
                print(f"        Registration window: {row['register_window']}")
        if prev is None:
            print(f"[init] {name}: baseline recorded ({len(result['rows'])} rows found)")
        elif should_alert:
            print(f"[alert] {name}")
            report_sections.append((name, lines))
        else:
            print(f"[no change] {name}")

    try:
        result = check_calgary("Calgary", CALGARY_URL)
        prev = state.get("Calgary")
        should_alert, lines = summarize_calgary_result(result, prev)
        new_state["Calgary"] = {
            "text_hash": result["text_hash"],
            "sublinks": {u: {"sold_out": i.get("sold_out")} for u, i in result["sublinks"].items() if "error" not in i},
        }
        # Always print what was found, same as Vancouver/Edmonton above.
        print(f"[Calgary] {len(result['sublinks'])} registration link(s) found on the main page:")
        if not result["sublinks"]:
            print(
                "    (none - every month is probably showing plain 'SOLD OUT' "
                "text right now, with no clickable Registrations link)"
            )
        for link_url, info in result["sublinks"].items():
            if "error" in info:
                print(f"    {link_url}  -> ERROR fetching this link: {info['error']}")
                continue
            status = "SOLD OUT (inside)" if info["sold_out"] else "NOT marked sold out - worth checking"
            print(f"    {link_url}")
            print(f"        Status: {status}")
        if prev is None:
            print(f"[init] Calgary: baseline recorded ({len(result['sublinks'])} registration links found)")
        elif should_alert:
            print("[alert] Calgary")
            report_sections.append(("Calgary", lines))
        else:
            print("[no change] Calgary")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Calgary: {exc}")
        print(f"[error] Calgary: {exc}")

    if errors:
        print("Errors encountered:\n" + "\n".join(errors), file=sys.stderr)

    if report_sections:
        body_lines = ["TCF availability watcher found updates:\n"]
        for name, lines in report_sections:
            body_lines.append(f"=== {name} ===")
            body_lines.extend(lines)
            body_lines.append("")
        body = "\n".join(body_lines)
        subject = "TCF slot alert: " + ", ".join(name for name, _ in report_sections)
        try:
            send_email(subject, body)
            print("Email sent.")
        except Exception as exc:  # noqa: BLE001
            print(f"[error] failed to send email: {exc}", file=sys.stderr)
            errors.append(f"email send failed: {exc}")

    save_state(new_state)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
