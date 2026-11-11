"""
M2M - Manabox to Moxfield sync (Windows GUI)

Manabox is the source of truth; Moxfield becomes a shareable copy.

  * Collection binders: one Moxfield trade binder per Manabox binder / registered deck
  * Decks:              one Private/Unlisted/Public Moxfield decklist per registered Manabox deck
  * Lists:              per Manabox list -> Moxfield wish list (add missing cards),
                        a decklist that is NOT in your collection, or skip

Build the .exe:   pip install pyinstaller pandas selenium
                  pyinstaller --onefile --windowed --name M2M m2m_app.py
"""

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import urllib.request
import webbrowser
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

APP_NAME = "M2M"
VERSION = "1.1"
CONTACT = "twyst@twyst.click"
TIP_URL = "https://ko-fi.com/twyst"
WHATS_NEW = (
    "- Decks: renamed decks can be matched to their Moxfield deck, and you can set a format, folder (also "
    "folders inside folders) or name for decks already on Moxfield.\n"
    "- Commanders: pick a missing commander from a list of the deck's legendary creatures.\n"
    "- Schemes, attractions and other special cards go to their own boards on Moxfield.\n"
    "- Wish list: new cards are separated from extra copies of cards already wanted; you choose what to add.\n"
    "- A review window at the end lists cards on Moxfield but not in Manabox, so you can send them to a list.\n"
    "- Clearer buttons and options, and a check that you are really logged in to Moxfield.")
def _data_dir():
    """Keep settings, saved state and the Moxfield login in an 'M2M_data' folder next to the program, so
    cleaning up is just deleting that folder. Falls back to %LOCALAPPDATA%\\M2M if that place isn't writable."""
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    try:
        d = here / "M2M_data"
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_test"
        probe.write_text("x")
        probe.unlink()
        return d
    except Exception:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME


APP_DIR = _data_dir()
SETTINGS_FILE = APP_DIR / "settings.json"
STATE_FILE = APP_DIR / "state.json"

DEBUG_PORT = 9222
BINDERS_URL = "https://www.moxfield.com/binders"
COLLECTION_URL = "https://www.moxfield.com/collection"
DECKS_URL = "https://www.moxfield.com/decks/personal"
WISHLIST_URL = "https://www.moxfield.com/wishlist"

LIST_SKIP = "Skip"
LIST_WISH = "Wish list (add missing cards)"
LIST_DECK = "Decklist (not in collection)"
LIST_CHOICES = [LIST_SKIP, LIST_WISH, LIST_DECK]

DECK_NEW = "Create new decks only"
DECK_SYNC = "Also add missing cards to existing decks"
DECK_CHOICES = [DECK_NEW, DECK_SYNC]

FORMATS = {   # label shown in the app -> value of Moxfield's Format dropdown
    "None": "none", "Commander / EDH": "commander", "Brawl": "historicBrawl", "Standard Brawl": "brawl",
    "Competitive Brawl": "competitiveBrawl", "Duel Commander": "duelCommander", "Oathbreaker": "oathbreaker",
    "Pauper EDH": "pauperEdh", "Tiny Leaders": "tinyLeaders", "Standard": "standard", "Pioneer": "pioneer",
    "Modern": "modern", "Legacy": "legacy", "Vintage": "vintage", "Pauper": "pauper", "Historic": "historic",
    "Alchemy": "alchemy", "Timeless": "timeless", "Premodern": "premodern", "Old School": "oldSchool",
    "Penny Dreadful": "pennyDreadful", "Dandan": "dandan", "Gladiator": "gladiator", "PreDH": "predh",
    "Centurion": "centurion", "Archon": "archon", "Conquest": "conquest", "Value Vintage": "valueVintage",
    "Alpha 40": "alpha40", "Pendragon": "pendragon", "Primordial": "primordial",
    "Highlander Gauntlet": "highlanderGauntlet", "Australian Highlander": "highlanderAustralian",
    "Canadian Highlander": "highlanderCanadian", "European Highlander": "highlanderEuropean",
    "Russian Duel Commander": "duelCommanderRussian",
}


BRAWL_FORMATS = {"Brawl", "Standard Brawl", "Competitive Brawl"}      # planeswalkers can lead these
COMMANDER_FORMATS = BRAWL_FORMATS | {"Commander / EDH", "Duel Commander", "Pauper EDH", "Tiny Leaders",
                                     "PreDH", "Russian Duel Commander"}


def format_label(text):
    """Map the badge text on a deck page ('Commander') to the matching label in FORMATS."""
    t = str(text).strip().lower()
    for lab in FORMATS:
        if lab.lower() == t:
            return lab
    for lab in FORMATS:
        if lab.lower().startswith(t) and t:
            return lab
    return "None"


def guess_format(name, n_cards):
    """A best guess only - the user confirms it."""
    if "dandan" in re.sub(r"[^a-z]", "", name.lower().replace("â", "a")):
        return "Dandan"
    if 98 <= n_cards <= 101:
        return "Commander / EDH"
    return "None"


def guess_folders(names):
    """Folder per deck from a shared name prefix ('J22 - Cats' + 'J22 - Holy' -> 'J22'); '' if none."""
    prefixes = {n: n.split(" - ")[0].strip() for n in names if " - " in n}
    counts = {}
    for p in prefixes.values():
        counts[p] = counts.get(p, 0) + 1
    return {n: (prefixes[n] if n in prefixes and counts[prefixes[n]] >= 2 else "") for n in names}


DEFAULTS = {
    "csv": "",
    "chrome": "",
    "profile": str(APP_DIR / "ChromeProfile"),
    "visibility": "Private",
    "deck_mode": DECK_NEW,
    "wish_remove_owned": False,
    "set_commanders": False,
    "deck_binders": False,
    "welcome_seen": False,
    "seen_version": "",
    "do_binders": True,
    "do_decks": True,
    "do_lists": True,
    "lists": {},
}


class Stopped(Exception):
    pass


# ── Pure helpers (no browser) ────────────────────────────────────────────────

def find_chrome():
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if p.exists():
                return str(p)
    return ""


def sanitise_binder_name(name):
    """Moxfield binder names: letters, numbers, spaces and hyphens only."""
    name = str(name).replace("'", "").replace("/", "-").replace("£", "")
    name = re.sub(r"[^a-zA-Z0-9 \-]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def norm_name(name):
    """Moxfield shows binder names with hyphens for spaces; compare in that form."""
    return sanitise_binder_name(name).lower().replace(" ", "-")


def sanitise_deck_name(name):
    name = str(name).replace("'", "").replace("/", "-").replace("£", "")
    name = re.sub(r"[^a-zA-Z0-9 \-!,()]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', "", str(name).strip())
    return re.sub(r"\s+", " ", name)


def read_csv_str(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")


def load_manabox(path):
    df = read_csv_str(path)
    needed = {"Binder Name", "Binder Type", "Name", "Set code", "Collector number", "Foil", "Quantity"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"This doesn't look like a Manabox collection export. Missing columns: {sorted(missing)}")
    return df


def list_names(df):
    return sorted(df[df["Binder Type"] == "list"]["Binder Name"].unique())


def decklist_lines(group):
    """[(card name, '1 Name (SET) 123 *F*')] with duplicate rows summed."""
    counts = {}
    for _, r in group.iterrows():
        foil = {"foil": " *F*", "etched": " *E*"}.get(str(r["Foil"]).lower(), "")
        key = (r["Name"], str(r["Set code"]).upper(), str(r["Collector number"]), foil)
        counts[key] = counts.get(key, 0) + int(r["Quantity"])
    return [(n, f"{q} {n} ({s}) {c}{f}") for (n, s, c, f), q in sorted(counts.items())]


def mx_key(set_code, number, foil):
    f = str(foil).strip().lower()
    return (str(set_code).strip().lower(), str(number).strip().lower(),
            "" if f in ("", "normal", "false", "no") else f)


def collection_diff(mana, mox):
    """Compare Manabox rows (no lists) with a Moxfield collection export.

    Returns (rows to add, extras). Missing cards are assigned to the Manabox binders they were most
    recently added to. extras = [(card, set, number, qty, foil)] that are on Moxfield but not in Manabox."""
    mq, names = {}, {}
    for _, r in mox.iterrows():
        k = mx_key(r["Edition"], r["Collector Number"], r["Foil"])
        mq[k] = mq.get(k, 0) + int(r["Count"])
        names[k] = r["Name"]
    m = mana.copy()
    m["_k"] = [mx_key(x, y, z) for x, y, z in zip(m["Set code"], m["Collector number"], m["Foil"])]
    m["Quantity"] = m["Quantity"].astype(int)
    totals = m.groupby("_k")["Quantity"].sum().to_dict()
    out = []
    for k, g in m.groupby("_k"):
        short = totals[k] - mq.get(k, 0)
        if short <= 0:
            continue
        if "Added" in g.columns:
            g = g.sort_values("Added", ascending=False)
        for _, r in g.iterrows():
            if short <= 0:
                break
            take = min(int(r["Quantity"]), short)
            row = r.drop("_k").copy()
            row["Quantity"] = str(take)
            out.append(row)
            short -= take
    added = pd.DataFrame(out)[list(mana.columns)] if out else pd.DataFrame(columns=list(mana.columns))
    extras = [(names[k], k[0], k[1], q - totals.get(k, 0), k[2]) for k, q in mq.items() if q > totals.get(k, 0)]
    return added, sorted(extras)


def parse_line(line):
    """'1 Sol Ring (C19) 37 *F*' -> ('sol ring', 'c19', '37')"""
    m = re.match(r"^\s*\d+x?\s+(.*?)(?:\s+\((\w+)\)\s*(\S+))?(?:\s+\*\w\*)?\s*$", line)
    if not m:
        return (line.strip().lower(), "", "")
    return (m.group(1).strip().lower(), (m.group(2) or "").lower(), (m.group(3) or "").lower())


def line_qty(line):
    m = re.match(r"^\s*(\d+)", line)
    return int(m.group(1)) if m else 1


def front_name(name):
    """Lowercase front face of a card name ('A // B' and 'A / B' both -> 'a')."""
    return re.split(r"\s+//?\s+", str(name).strip().lower())[0]


def deck_missing(lines, text, any_copy=False):
    """Manabox lines not yet covered by a deck's current text.

    Moxfield keeps ONE printing per card per deck and adds up the quantities, so cards are compared by
    name and total quantity. Returns lines whose quantity is what is still short."""
    have = {}
    for l in text.splitlines():
        if l.strip():
            k = front_name(parse_line(l)[0])
            have[k] = have.get(k, 0) + line_qty(l)
    need = {}
    for _, l in lines:
        k = front_name(parse_line(l)[0])
        need[k] = need.get(k, 0) + line_qty(l)
    if any_copy:                      # only care that the card is there at all (e.g. a wish list)
        need = {k: 1 for k in need}
    short = {k: need[k] - have.get(k, 0) for k in need if need[k] > have.get(k, 0)}
    missing = []
    for _, l in lines:
        k = front_name(parse_line(l)[0])
        if short.get(k, 0) > 0:
            take = min(line_qty(l), short[k])
            short[k] -= take
            missing.append(re.sub(r"^\s*\d+x?", str(take), l, count=1))
    return missing


# ── Moxfield automation ──────────────────────────────────────────────────────

NOBINDER = "(no binder)"   # pseudo binder name: import with no binder selected
TOP = "-top-"          # folder value meaning 'move to the top level'


def xp_lit(text):
    """XPath string literal that copes with apostrophes."""
    if "'" not in text:
        return "'" + text + "'"
    return "concat(" + ", \"'\", ".join("'" + part + "'" for part in text.split("'")) + ")"


def scryfall_lookup(ids, log=None):
    """{scryfall id: (front-face name, type line, oracle text)} via Scryfall's public API (75 cards per request)."""
    out, ids = {}, sorted(set(i for i in ids if isinstance(i, str) and len(i) > 20))
    for k in range(0, len(ids), 75):
        body = json.dumps({"identifiers": [{"id": i} for i in ids[k:k + 75]]}).encode()
        req = urllib.request.Request(
            "https://api.scryfall.com/cards/collection", data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "M2M-collection-sync/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        for c in data.get("data", []):
            faces = c.get("card_faces") or []
            name = (faces[0].get("name") if faces else None) or c.get("name", "")
            type_line = c.get("type_line") or " // ".join(f.get("type_line", "") for f in faces)
            text = c.get("oracle_text") or " ".join(f.get("oracle_text", "") for f in faces)
            out[c["id"]] = (name, type_line, text)
        time.sleep(0.15)
    return out


def special_board(type_line):
    """Moxfield keeps these card types in their own boards (Bulk Edit > Other), not the Main Deck."""
    t = type_line.lower()
    if "scheme" in t:
        return "Schemes"
    if "attraction" in t:
        return "Attraction Deck"
    if "contraption" in t:
        return "Contraption Deck"
    if re.search(r"\bplane\b", t.split("—")[0]) or "phenomenon" in t:
        return "Planar Deck"
    if "stickers" in t:
        return "Sticker Sheets"
    return ""


def commander_candidates(type_line, text, walkers=False):
    tl = type_line.lower()
    return (("legendary" in tl and ("creature" in tl or (walkers and "planeswalker" in tl)))
            or "can be your commander" in text.lower())


def commander_choices(group, info, walkers=False):
    """(candidate names, pre-selected name) for one deck. Foil candidates first: precon commanders are usually foil."""
    names, foil = [], set()
    for sid, f in zip(group["Scryfall ID"].astype(str), group["Foil"].astype(str)):
        if sid in info and commander_candidates(info[sid][1], info[sid][2], walkers):
            nm = info[sid][0]
            if nm not in names:
                names.append(nm)
            if f.lower() in ("foil", "etched"):
                foil.add(nm)
    names = sorted(names, key=lambda n: (n not in foil, n))
    return names, (names[0] if len(names) == 1 or len(foil) == 1 else "")


class Syncer:
    def __init__(self, cfg, log, confirm, pause, pick_file, choose_opts, choose_tidy, stop_event):
        self.cfg = cfg
        self.log = log
        self.confirm = confirm      # confirm(message) -> bool
        self.pause = pause          # pause(message) -> None, blocks until the user clicks OK
        self.pick_file = pick_file  # pick_file(message) -> path or ""
        self.choose_opts = choose_opts  # choose_opts(rows) -> {deck: (format value, folder)} or None
        self.choose_wish_add = None     # choose_wish_add(new lines, [(line, already wanted)]) -> lines to add
        self.choose_remove = None       # choose_remove(rows) -> [lines to remove] or None
        self._owned = {}
        self._extras = []               # cards on Moxfield but not in Manabox: (name, set, number, qty, foil)
        self.choose_actions = None      # choose_actions(extras, list names, default) -> {'groups': {list name: [lines]}} or None
        self._cmdr = {}                 # deck name -> {'std': (names, pre), 'walk': (names, pre)}
        self._boards = {}               # front card name -> special board (Schemes, ...)
        self.choose_cmdr = None         # choose_cmdr(rows) -> {deck: commander name} or None
        self.choose_tidy = choose_tidy  # choose_tidy(rows) -> {deck: {'format':..,'folder':..}} or None
        self.stop = stop_event
        self.driver = None
        self.main_handle = None
        self._deck_index = None
        self._deck_folder = {}
        self._deck_display = {}
        self._deck_fmt_seen = {}
        self._folder_paths = set()
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
        if not isinstance(self.state.get("decks"), dict):
            self.state["decks"] = {}

    # -- plumbing
    def check_stop(self):
        if self.stop.is_set():
            raise Stopped()

    def save_state(self):
        STATE_FILE.write_text(json.dumps(self.state, indent=1), encoding="utf-8")

    def wait(self, timeout=20):
        return WebDriverWait(self.driver, timeout)

    def js_click(self, el):
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", el)

    def step(self, label, fn):
        try:
            return fn()
        except Exception as e:
            raise RuntimeError(f"step '{label}' failed ({type(e).__name__})") from e

    def connect(self):
        chrome = self.cfg["chrome"]
        if not chrome or not Path(chrome).exists():
            raise RuntimeError("Chrome was not found. Set the Chrome location in the settings.")
        Path(self.cfg["profile"]).mkdir(parents=True, exist_ok=True)
        self.log("Starting Chrome...")
        subprocess.Popen([chrome, f"--remote-debugging-port={DEBUG_PORT}",
                          f"--user-data-dir={self.cfg['profile']}", "https://www.moxfield.com/"])
        time.sleep(4)
        opts = Options()
        opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{DEBUG_PORT}")
        try:
            self.driver = webdriver.Chrome(service=Service(), options=opts)
        except Exception as e:
            raise RuntimeError(
                "Couldn't connect to Chrome. Close every Chrome window (also check Task Manager) and try again.\n"
                f"({type(e).__name__})") from e
        self.pause("Log in to Moxfield in the Chrome window that opened, then click OK here.")
        self.main_handle = self.driver.window_handles[0]
        self.driver.switch_to.window(self.main_handle)
        for attempt in range(3):
            if self.logged_in():
                break
            self.log("  Moxfield doesn't look logged in yet.")
            self.pause("It looks like you're not logged in to Moxfield yet. Log in in the Chrome window "
                       "(use the Log In button at the top right), then click OK here.")
            self.driver.switch_to.window(self.main_handle)
        else:
            raise RuntimeError("Still not logged in to Moxfield. Log in in the Chrome window and start again.")
        self.log("Connected.")

    def logged_in(self):
        """False only when Moxfield clearly shows a Log In / Sign In control or sends us to a login page."""
        d = self.driver
        try:
            d.get(DECKS_URL)
            self.wait(30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(3)
            if re.search(r"log-?in|sign-?in", d.current_url.lower()):
                return False
            return not d.execute_script(
                "var n=['log in','login','sign in'];"
                "return Array.from(document.querySelectorAll('a,button')).some(function(e){"
                "var t=(e.textContent||'').trim().toLowerCase();"
                "return n.indexOf(t)>=0&&e.offsetParent!==null;});")
        except Exception:
            return True                   # can't tell: don't block the user

    def restore_tab(self):
        """Close stray tabs (the export download opens a blank one) and bring the Moxfield tab to the front.
        Background tabs don't render, which would make the page look empty."""
        d = self.driver
        try:
            for h in list(d.window_handles):
                if h != self.main_handle:
                    d.switch_to.window(h)
                    d.close()
            d.switch_to.window(self.main_handle)
            d.execute_cdp_cmd("Page.bringToFront", {})
        except Exception:
            pass

    def open_collection_menu(self, item_text):
        d = self.driver
        d.get(COLLECTION_URL)
        self.wait(30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)
        self.wait().until(EC.element_to_be_clickable(
            (By.XPATH, "//a[contains(.,'More') and contains(@class,'cursor-pointer')]"))).click()
        time.sleep(0.5)
        self.wait().until(EC.element_to_be_clickable(
            (By.XPATH, f"//*[contains(text(),'{item_text}')]"))).click()
        time.sleep(1)

    # -- reading what is on Moxfield
    def export_moxfield_collection(self):
        """Download Moxfield's own collection export (Collection > More > Export CSV) and read it."""
        dl = Path(tempfile.mkdtemp(prefix="m2m_dl_"))
        for method in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
            try:
                self.driver.execute_cdp_cmd(method, {"behavior": "allow", "downloadPath": str(dl)})
                break
            except Exception:
                continue
        path = None
        try:
            self.restore_tab()
            self.open_collection_menu("Export CSV")
            for _ in range(60):
                files = [f for f in dl.glob("*.csv")]
                if files and not list(dl.glob("*.crdownload")):
                    path = files[0]
                    break
                time.sleep(1)
        except Exception:
            pass
        if path is None:
            chosen = self.pick_file("Couldn't download the Moxfield export automatically.\n\n"
                                    "In Moxfield open Collection > More > Export CSV, then choose the "
                                    "downloaded file in the next window.")
            if not chosen:
                raise RuntimeError("No Moxfield export available.")
            path = Path(chosen)
        self.restore_tab()
        mox = read_csv_str(path)
        need = {"Count", "Name", "Edition", "Collector Number", "Foil"}
        if need - set(mox.columns):
            raise RuntimeError(f"That doesn't look like a Moxfield collection export (missing {sorted(need - set(mox.columns))}). "
                               "The usual cause is not being logged in to Moxfield: log in in the Chrome window and run again.")
        return mox

    def collect_deck_links(self):
        """Deck links on the page currently shown (waits for them, scrolls to load lazy rows)."""
        d = self.driver
        xp = "//a[contains(@href,'/decks/')]"
        pat = r"/decks/[\w-]{10,}"
        try:
            self.wait(15).until(lambda x: any(re.search(pat, a.get_attribute("href") or "")
                                              for a in x.find_elements(By.XPATH, xp)))
        except Exception:
            pass
        found, last = {}, -1
        for _ in range(20):
            for a in d.find_elements(By.XPATH, xp):
                href = a.get_attribute("href") or ""
                shown = (a.get_attribute("textContent") or "").strip()
                if shown and "/decks/personal" not in href and re.search(pat, href):
                    texts = d.execute_script(
                        "var a=arguments[0],r=a;"
                        "var K='svg[data-icon=\"ellipsis-vertical\"]';"
                        "while(r&&!r.querySelector(K))r=r.parentElement;"
                        "if(!r||r.querySelectorAll(K).length!==1)return null;var out=[];"
                        "r.querySelectorAll('*').forEach(function(e){if(a.contains(e))return;"
                        "var t='';e.childNodes.forEach(function(n){if(n.nodeType===3)t+=n.textContent;});"
                        "t=t.trim();if(t)out.push(t);});return out;", a)
                    if texts is None:
                        continue          # not a deck row (e.g. the 'Most Recent Deck' card)
                    found.setdefault(shown.lower(), (href, shown))
                    labs = [t for t in texts if t.lower() == "none" or format_label(t) != "None"]
                    if labs:
                        self._deck_fmt_seen.setdefault(shown.lower(), labs[0])
            if len(found) == last:
                break
            last = len(found)
            d.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
        # one deck can have several links (e.g. name + a format label); keep the shortest text per URL
        by_href = {}
        for low, (href, shown) in found.items():
            key = href.split("?")[0].rstrip("/")
            if key not in by_href or len(shown) < len(by_href[key][1]):
                by_href[key] = (href, shown)
        return {shown.lower(): (href, shown) for href, shown in by_href.values()}

    def folder_names(self):
        return [(a.get_attribute("textContent") or "").strip()
                for a in self.driver.find_elements(By.XPATH, "//a[contains(@class,'decklist-folder-name')]")]

    def go_root(self):
        """Moxfield remembers the last folder you opened; climb back to the top level."""
        d = self.driver
        for _ in range(6):
            ups = d.find_elements(By.XPATH, "//*[normalize-space(text())='Up a level']")
            ups = [u for u in ups if u.is_displayed()]
            if not ups:
                return
            self.js_click(ups[0])
            time.sleep(2)

    def open_folder_path(self, path):
        d = self.driver
        d.get(DECKS_URL)
        self.wait(30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)
        self.go_root()
        for label in path:
            for a in d.find_elements(By.XPATH, "//a[contains(@class,'decklist-folder-name')]"):
                if re.sub(r"\s*\(\d+\)$", "", (a.get_attribute("textContent") or "").strip()) == \
                        re.sub(r"\s*\(\d+\)$", "", label):
                    self.js_click(a)
                    break
            else:
                return False
            self.wait(15).until(EC.presence_of_element_located((By.XPATH, "//*[contains(normalize-space(.),'Up a level')]")))
            time.sleep(2)
        return True

    def deck_index(self):
        """{lowercase deck name: url}, including decks inside folders (each folder is opened in turn)."""
        if self._deck_index is None:
            self.restore_tab()
            idx, folders_of = {}, {}
            queue_ = [[]]                       # folder paths still to scan; [] = the top level
            while queue_:
                path = queue_.pop(0)
                if not self.open_folder_path(path):
                    continue
                for name, (url, shown) in self.collect_deck_links().items():
                    idx.setdefault(name, url)
                    self._deck_display.setdefault(name, shown)
                    folders_of.setdefault(name, "/".join(re.sub(r"\s*\(\d+\)$", "", x) for x in path))
                for f in self.folder_names():
                    fp = path + [re.sub(r"\s*\(\d+\)$", "", f)]
                    self._folder_paths.add("/".join(fp))
                    queue_.append(fp)
            self._deck_index = idx
            self._deck_folder = folders_of
            n_folders = sum(1 for v in folders_of.values() if v)
            self.log(f"  found {len(idx)} decks on Moxfield ({n_folders} inside folders)")
        return self._deck_index

    def deck_url(self, name):
        return self.deck_index().get(sanitise_deck_name(name).lower()) or self.state["decks"].get(name)

    def open_bulk_edit(self, page_url):
        """Open a deck's (or the wish list's) Bulk Edit page and return its text box."""
        d = self.driver
        d.get(page_url)
        self.wait(30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)
        try:                                     # cards shown on the deck page (all boards, incl. commander)
            self._page_names = {front_name((e.get_attribute("textContent") or "").strip())
                                for e in d.find_elements(By.CSS_SELECTOR, "[data-testid='deck-view-table-row-name']")}
        except Exception:
            self._page_names = set()
        # The link text is split across spans ("Bulk" + "Edit"), so anchor on the "Bulk" span
        xp = ("//span[normalize-space(.)='Bulk']/ancestor::*[self::a or self::button][1]"
              " | //a[contains(@href,'/edit')][.//span[contains(.,'Edit')]]")
        link = self.wait(30).until(EC.presence_of_element_located((By.XPATH, xp)))
        self.step("open Bulk Edit", lambda: self.js_click(link))
        ta = self.wait(30).until(EC.visibility_of_element_located((By.TAG_NAME, "textarea")))
        time.sleep(1)
        return ta

    def switch_board(self, board):
        """In Bulk Edit: Other > <board>. Returns that board's text box."""
        d = self.driver

        def shown(xp):
            return [e for e in d.find_elements(By.XPATH, xp) if e.is_displayed()]
        tab = self.step("open 'Other' tab", lambda: self.wait(10).until(
            lambda x: next(iter(shown("//*[normalize-space(text())='Other']")), None)))
        self.js_click(tab)
        time.sleep(1)
        item = self.step(f"pick {board}", lambda: self.wait(8).until(
            lambda x: next(iter(shown(f"//*[normalize-space(text())='{board}']")), None)))
        self.js_click(item)
        time.sleep(1.5)
        return self.wait(10).until(EC.visibility_of_element_located((By.TAG_NAME, "textarea")))

    def board_text(self, url, board):
        self.open_bulk_edit(url)
        return self.switch_board(board).get_attribute("value") or ""

    def add_to_boards(self, url, main_lines, board_lines, remove_main=()):
        """Type missing lines into Main and/or the other boards of a deck (removing lines that belong in another
        board from Main), then Save once."""
        ta = self.open_bulk_edit(url)
        existing = ta.get_attribute("value") or ""
        if remove_main:
            gone = set(remove_main)
            kept = [l for l in existing.splitlines() if l.strip() and l not in gone]
            ta.click()
            ta.send_keys(Keys.CONTROL, "a")
            ta.send_keys(Keys.DELETE)
            ta.send_keys("\n".join(kept + list(main_lines)))
        elif main_lines:
            ta.click()
            ta.send_keys(Keys.CONTROL, Keys.END)
            ta.send_keys(("" if (existing.endswith("\n") or not existing) else "\n") + "\n".join(main_lines))
        for board, lines in board_lines.items():
            if not lines:
                continue
            t2 = self.switch_board(board)
            existing = t2.get_attribute("value") or ""
            t2.click()
            t2.send_keys(Keys.CONTROL, Keys.END)
            t2.send_keys(("" if (existing.endswith("\n") or not existing) else "\n") + "\n".join(lines))
        time.sleep(0.5)
        self.step("Save", lambda: self.js_click(
            self.driver.find_element(By.XPATH, "//button[normalize-space(.)='Save']")))
        time.sleep(3)

    def append_and_save(self, ta, lines):
        existing = ta.get_attribute("value") or ""
        ta.click()
        ta.send_keys(Keys.CONTROL, Keys.END)
        prefix = "" if (existing.endswith("\n") or not existing) else "\n"
        ta.send_keys(prefix + "\n".join(lines))
        time.sleep(0.5)
        self.step("Save", lambda: self.js_click(
            self.driver.find_element(By.XPATH, "//button[normalize-space(.)='Save']")))
        time.sleep(3)

    # -- collection binders
    def create_binder(self, name):
        d = self.driver
        d.get(BINDERS_URL)
        self.wait(30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)
        self.wait().until(EC.element_to_be_clickable((By.XPATH, "//button[contains(.,'Create Binder')]"))).click()
        box = self.wait().until(EC.presence_of_element_located((By.ID, "name")))
        time.sleep(0.5)
        box.click()
        box.clear()
        box.send_keys(sanitise_binder_name(name))
        time.sleep(0.3)
        self.wait().until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, ".modal-footer button[type='submit']"))).click()
        self.wait(10).until(EC.invisibility_of_element_located((By.ID, "name")))
        time.sleep(1)

    def open_import_modal(self):
        self.open_collection_menu("Import CSV")
        self.wait().until(EC.presence_of_element_located((By.ID, "csvFormat")))

    def binder_option(self, name):
        """The binder's entry in the import dialog's dropdown, or None."""
        sel = Select(self.driver.find_element(By.ID, "tradeBinderId"))
        target = norm_name(name)
        for opt in sel.options:
            if norm_name(opt.text) == target:
                return sel, opt
        return None

    def do_import(self, csv_path, binder_name):
        d = self.driver
        Select(d.find_element(By.ID, "csvFormat")).select_by_value("manaBox")
        fi = d.find_element(By.ID, "filename")
        d.execute_script("arguments[0].style.display='block'; arguments[0].style.visibility='visible';", fi)
        fi.send_keys(str(Path(csv_path).resolve()))
        time.sleep(1)
        if binder_name != NOBINDER:
            found = self.binder_option(binder_name)
            if not found:
                raise RuntimeError(f"binder '{binder_name}' not found in the import dialog")
            found[0].select_by_visible_text(found[1].text)
        Select(d.find_element(By.ID, "game")).select_by_value("paper")
        btn = self.wait(15).until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, ".modal-footer button[type='submit']")))
        try:
            btn.click()
        except Exception:
            self.js_click(btn)
        time.sleep(3)
        self.wait(15).until(EC.invisibility_of_element_located((By.ID, "csvFormat")))
        time.sleep(1)

    def sync_binders(self, df):
        mana = df[df["Binder Type"] != "list"].copy()
        if mana.empty:
            return
        self.log("== Collection binders ==")
        deck_binders = bool(self.cfg.get("deck_binders", False))
        deck_names = set(mana[mana["Binder Type"] == "deck"]["Binder Name"])
        names = sorted(n for n in mana["Binder Name"].unique() if deck_binders or n not in deck_names)
        seen = {}
        for n in names:
            seen.setdefault(norm_name(n), []).append(n)
        clashes = [v for v in seen.values() if len(v) > 1]
        if clashes:
            raise RuntimeError(f"These binders would get the same Moxfield name: {clashes}. Rename one in Manabox.")
        renamed = [n for n in names if sanitise_binder_name(n) != n.strip()]
        if renamed:
            self.log("Moxfield only allows letters, numbers, spaces and hyphens, so these get renamed:")
            for n in renamed:
                self.log(f"   {n}  ->  {sanitise_binder_name(n)}")

        self.log("Reading your Moxfield collection...")
        mox = self.export_moxfield_collection()
        added, extras = collection_diff(mana, mox)
        self._extras = extras
        if not deck_binders and not added.empty:        # deck cards join the collection without a binder
            added = added.copy()
            added["Binder Name"] = added["Binder Name"].where(~added["Binder Name"].isin(deck_names), NOBINDER)
        have = int(mox["Count"].astype(int).sum())
        want = int(mana["Quantity"].astype(int).sum())
        self.log(f"  Moxfield has {have} cards, Manabox has {want}.")
        if added.empty:
            self.log("  Collection already matches - nothing to add.")
        else:
            todo = sorted(added["Binder Name"].unique())
            n_cards = int(added["Quantity"].astype(int).sum())
            preview = "\n".join(f"{r['Binder Name'] if r['Binder Name'] != NOBINDER else '(collection, no binder)'}: {r['Quantity']} x {r['Name']} ({str(r['Set code']).upper()}) {r['Collector number']}"
                                for _, r in added.head(25).iterrows())
            more = f"\n... and {len(added) - 25} more lines" if len(added) > 25 else ""
            if not self.confirm(f"Add {n_cards} cards to {len(todo)} Moxfield binders?\n\n{preview}{more}"):
                self.log("  skipped by you")
                todo = []
            with tempfile.TemporaryDirectory() as tmp:
                for n in todo:
                    self.check_stop()
                    g = added[added["Binder Name"] == n]
                    path = Path(tmp) / (safe_filename(n) + ".csv")
                    g.to_csv(path, index=False, encoding="utf-8")
                    try:
                        self.open_import_modal()
                        if n != NOBINDER and not self.binder_option(n):
                            self.create_binder(n)
                            self.log(f"  created binder: {sanitise_binder_name(n)}")
                            self.open_import_modal()
                        self.do_import(path, n)
                        self.log(f"  added {int(g['Quantity'].astype(int).sum())} cards to "
                                 + ("the collection (no binder)" if n == NOBINDER else n))
                    except Stopped:
                        raise
                    except Exception as e:
                        self.log(f"  FAILED '{n}': {e}")
        if extras:
            self.log(f"-- {len(extras)} cards are on Moxfield but not in Manabox. Review them by hand:")
            for name, st, num, q, _f in extras[:60]:
                self.log(f"   {q} x {name} ({st.upper()}) {num}")
            if len(extras) > 60:
                self.log(f"   ... and {len(extras) - 60} more")

    # -- decks and list-decklists
    def close_modal(self):
        try:
            self.driver.find_element(By.CSS_SELECTOR, ".btn-close").click()
            time.sleep(1)
        except Exception:
            pass

    def read_folders(self):
        """Names of your existing deck folders (read from the New Deck pop-up)."""
        d = self.driver
        d.get(DECKS_URL)
        self.wait(30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)
        self.go_root()
        self.step("open New Deck", lambda: self.js_click(self.wait().until(EC.presence_of_element_located(
            (By.XPATH, "//*[self::a or self::button][contains(.,'New Deck') or contains(.,'Create new deck') or contains(.,'Create New Deck')]")))))
        modal = self.wait().until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".modal-dialog")))
        time.sleep(1)
        self.step("expand Advanced options", lambda: self.js_click(
            modal.find_element(By.XPATH, ".//*[normalize-space(text())='Advanced options']")))
        time.sleep(1)
        sel = Select(modal.find_element(By.ID, "deckFolderId"))
        names = [re.sub(r"\s*\(\d+\)\s*$", "", o.text.strip()) for o in sel.options
                 if o.get_attribute("value") not in ("", "new")]
        self.log(f"  existing folders: {names}")
        self.close_modal()
        return names

    def ensure_folder_path(self, full):
        """Create 'A/B/C' level by level (folders can hold folders)."""
        parts = [x.strip() for x in full.split("/") if x.strip()]
        known = {p.lower() for p in self._folder_paths}
        for i in range(len(parts)):
            sub = "/".join(parts[:i + 1])
            if sub.lower() in known:
                continue
            self.create_folder(parts[i], parts[:i])
            self._folder_paths.add(sub)
            known.add(sub.lower())
            self.log(f"  created folder: {sub}")

    def create_folder(self, name, parent=()):
        d = self.driver
        if not self.open_folder_path(list(parent)):
            raise RuntimeError(f"couldn't open folder {'/'.join(parent)}")
        self.step("open New Folder", lambda: self.js_click(self.wait().until(EC.presence_of_element_located(
            (By.XPATH, "//*[self::a or self::button][contains(.,'New Folder') or contains(.,'New folder')]")))))
        modal = self.wait().until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".modal-dialog")))
        time.sleep(0.5)
        box = modal.find_element(By.ID, "name")
        box.click()
        box.send_keys(name)
        time.sleep(0.3)
        self.step("create folder", lambda: self.js_click(modal.find_element(
            By.XPATH, ".//button[@type='submit' or contains(.,'Create')]")))
        self.wait(10).until(EC.invisibility_of_element_located((By.CSS_SELECTOR, ".modal-dialog")))
        time.sleep(1)

    def create_deck(self, name, text, fmt="none", folder=""):
        d = self.driver
        d.get(DECKS_URL)
        self.wait(30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)
        self.step("open New Deck", lambda: self.js_click(self.wait().until(EC.presence_of_element_located(
            (By.XPATH, "//*[self::a or self::button][contains(.,'New Deck') or contains(.,'Create new deck') or contains(.,'Create New Deck')]")))))
        modal = self.wait().until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".modal-dialog")))
        time.sleep(1)

        def fill_name():
            inputs = [i for i in modal.find_elements(By.TAG_NAME, "input")
                      if i.get_attribute("type") in (None, "", "text") and i.is_displayed()]
            inputs[0].send_keys(sanitise_deck_name(name))
        self.step("type name", fill_name)
        # always set it: Moxfield's pop-up otherwise keeps its own default (Commander)
        self.step("format", lambda: Select(modal.find_element(By.ID, "format")).select_by_value(fmt or "none"))

        def expand():
            for _ in range(4):
                labels = modal.find_elements(By.XPATH, ".//label[contains(.,'Paste in List')]")
                if labels and labels[0].is_displayed():
                    return
                self.js_click(modal.find_element(By.XPATH, ".//*[normalize-space(text())='Advanced options']"))
                time.sleep(1)
            raise RuntimeError("Advanced options did not expand")
        self.step("expand Advanced options", expand)
        time.sleep(0.5)
        if folder:
            def pick_folder():
                sel = Select(modal.find_element(By.ID, "deckFolderId"))
                for o in sel.options:
                    if o.text.strip().lower() == folder.strip().lower():
                        sel.select_by_visible_text(o.text)
                        return
                raise RuntimeError(f"folder '{folder}' not found")
            self.step(f"folder {folder}", pick_folder)
        self.step("visibility", lambda: self.js_click(modal.find_element(
            By.XPATH, f".//label[contains(.,'{self.cfg['visibility']}')]")))
        self.step("Paste in List", lambda: self.js_click(modal.find_element(
            By.XPATH, ".//label[contains(.,'Paste in List')]")))
        time.sleep(0.5)

        def paste():
            ta = self.wait().until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".modal-dialog textarea")))
            ta.send_keys(text)
        self.step("paste list", paste)

        def untick_override():
            boxes = modal.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
            if not boxes:
                raise RuntimeError("no checkbox found")
            if boxes[-1].is_selected():
                self.js_click(boxes[-1])
                time.sleep(0.3)
            if boxes[-1].is_selected():
                raise RuntimeError("override checkbox did not change")
        # Unticked = keep the exact printings listed (ticked would swap in your preferred printings)
        self.step("untick 'Override with preferred printings'", untick_override)
        time.sleep(0.5)
        self.step("Create", lambda: self.js_click(modal.find_element(By.XPATH, ".//button[contains(.,'Create')]")))
        self.step("wait for deck page", lambda: self.wait(30).until(
            lambda x: "/decks/" in x.current_url and "/decks/personal" not in x.current_url))
        time.sleep(2)

    def sync_decklists(self, items, title):
        """items = {name: [(card, line), ...]}. Creates missing decks and adds missing cards to existing ones."""
        if not items:
            return
        self.log(f"== {title} ==")
        create, update = {}, {}
        for name, lines in items.items():
            self.check_stop()
            url = self.deck_url(name)
            if not url:
                create[name] = lines
                continue
            if self.cfg.get("deck_mode", DECK_NEW) == DECK_NEW:
                continue                      # already on Moxfield; leave it alone
            main_l = [(c, l) for c, l in lines if front_name(c) not in self._boards]
            spec = {}
            for c, l in lines:
                b = self._boards.get(front_name(c))
                if b:
                    spec.setdefault(b, []).append((c, l))
            try:
                text = self.open_bulk_edit(url).get_attribute("value") or ""
                missing = deck_missing(main_l, text)
                if missing:
                    # A card that is on the deck page but not in Bulk Edit sits in another board (usually the
                    # commander, which Bulk Edit never lists): it is not missing.
                    on_page = getattr(self, "_page_names", set())
                    missing = [l for l in missing
                               if not (line_qty(l) == 1 and front_name(parse_line(l)[0]) in on_page)]
                # Bulk Edit never lists the commander (Moxfield keeps it in its own board), so a Commander deck
                # looks exactly one card short: that single missing card is the commander, not a real gap.
                if (len(missing) == 1 and line_qty(missing[0]) == 1
                        and format_label(self._deck_fmt_seen.get(sanitise_deck_name(name).lower(), "")) == "Commander / EDH"):
                    have_n = sum(line_qty(l) for l in text.splitlines() if l.strip() and not l.lstrip().startswith("-"))
                    need_n = sum(line_qty(l) for _, l in main_l)
                    if have_n == need_n - 1:
                        missing = []
                stray = [l for l in text.splitlines()
                         if l.strip() and not l.lstrip().startswith("-")
                         and front_name(parse_line(l)[0]) in self._boards]
                board_missing = {}
                for b, bl in spec.items():
                    bm = deck_missing(bl, self.board_text(url, b))
                    if bm:
                        board_missing[b] = bm
            except Stopped:
                raise
            except Exception as e:
                self.log(f"  FAILED reading '{name}': {e}")
                continue
            if missing or board_missing or stray:
                update[name] = (url, missing, board_missing, stray)
            n_add = len(missing) + sum(len(v) for v in board_missing.values())
            self.log(f"  checked {name}" + (f" - {n_add} lines to add" if n_add else "")
                     + (f" - {len(stray)} special cards to move out of Main" if stray else ""))
        opts = {}
        if create:
            rows = []
            folders = guess_folders(list(create))
            for n, lines in create.items():
                total = sum(line_qty(l) for _, l in lines)
                rows.append((n, total, guess_format(n, total), folders[n], self._cmdr.get(n)))
            idx = self.deck_index()
            taken = {sanitise_deck_name(n).lower() for n in items} | set(self.state["decks"].values())
            unmatched = sorted((self._deck_display[l] for l in idx if l not in taken and idx[l] not in taken),
                               key=str.lower)
            opts = self.choose_opts(rows, sorted(self._folder_paths, key=str.lower), unmatched)
            if opts is None:
                self.log("  new decks skipped by you")
                create = {}
            else:
                for n in list(create):                      # "already on Moxfield as ..." (e.g. you renamed it)
                    m = (opts.get(n) or ("", "", "", ""))[3]
                    if m and m.lower() in idx:
                        self.state["decks"][n] = idx[m.lower()]
                        self.save_state()
                        del create[n]
                        self.log(f"  '{n}' matched to your existing Moxfield deck '{m}' (remembered)")
        if update:
            def flat(m, bm, st=()):
                return (m + [f"{l}   [{b}]" for b, ls in bm.items() for l in ls]
                        + [f"{l}   [move out of Main]" for l in st])
            msg = "Add cards to these existing decks:\n" + "\n".join(
                f"  {n}: {len(flat(m, bm, st))} changes\n      " + "\n      ".join(flat(m, bm, st)[:6])
                + ("\n      ..." if len(flat(m, bm, st)) > 6 else "")
                for n, (_, m, bm, st) in update.items())
            for n, (_, m, bm, st) in update.items():
                self.log(f"  {n}: would change -> " + " | ".join(flat(m, bm, st)))
            if not self.confirm(msg + "\n\nGo ahead?"):
                self.log("  existing decks left alone")
                update = {}
        if not create and not update:
            self.log("  nothing to create" if self.cfg.get("deck_mode", DECK_NEW) == DECK_NEW
                     else "  everything already matches")
            return
        needed = {opts[n][1] for n in create if opts.get(n) and opts[n][1]}
        if needed:
            have = {f.lower() for f in self.read_folders()}
            for f in sorted(needed):
                if f.lower() not in have:
                    try:
                        self.create_folder(f)
                        self.log(f"  created folder: {f}")
                    except Stopped:
                        raise
                    except Exception as e:
                        self.log(f"  FAILED creating folder '{f}': {e}")
        for name, lines in create.items():
            self.check_stop()
            fmt, folder, cmdr = (opts.get(name) or ("none", "", "", ""))[:3]
            try:
                main_l = [(c, l) for c, l in lines if front_name(c) not in self._boards]
                spec = {}
                for c, l in lines:
                    b = self._boards.get(front_name(c))
                    if b:
                        spec.setdefault(b, []).append(l)
                self.create_deck(name, "\n".join(l for _, l in main_l), fmt, folder)
                self.state["decks"][name] = self.driver.current_url
                self.save_state()
                self.log(f"  created: {name} ({len(main_l)} lines)")
                if spec:
                    self.add_to_boards(self.driver.current_url, [], spec)
                    self.log("    " + ", ".join(f"{len(v)} to {b}" for b, v in spec.items()))
                if cmdr:
                    try:
                        self.set_commander(self.state["decks"][name], cmdr)
                        self.log(f"    commander -> {cmdr}")
                    except Stopped:
                        raise
                    except Exception as e:
                        self.log(f"    FAILED setting commander '{cmdr}': {e}")
            except Stopped:
                raise
            except Exception as e:
                self.log(f"  FAILED creating '{name}': {e}")
        for name, (url, missing, bmissing, stray) in update.items():
            self.check_stop()
            try:
                self.add_to_boards(url, missing, bmissing, stray)
                self.log(f"  updated: {name} (+{len(missing) + sum(len(v) for v in bmissing.values())} lines"
                         + (f", {len(stray)} moved out of Main)" if stray else ")"))
            except Stopped:
                raise
            except Exception as e:
                self.log(f"  FAILED updating '{name}': {e}")
                self.log("   add by hand: " + "; ".join(missing + [l for v in bmissing.values() for l in v]))

    def sync_decks(self, df):
        decks = df[df["Binder Type"] == "deck"]
        items = {n: decklist_lines(g) for n, g in decks.groupby("Binder Name")}
        self._boards = {}
        try:
            self.log("  checking for schemes, attractions and other special cards (Scryfall)...")
            info = scryfall_lookup(list(decks["Scryfall ID"].dropna().astype(str)))
            for nm, tl, _ in info.values():
                b = special_board(tl)
                if b:
                    self._boards[front_name(nm)] = b
            if self._boards:
                self.log(f"  {len(self._boards)} special cards will go to their own boards")
            self._cmdr = {n: {"std": commander_choices(g, info, False), "walk": commander_choices(g, info, True)}
                          for n, g in decks.groupby("Binder Name")}
        except Exception as e:
            self.log(f"  Scryfall lookup failed ({type(e).__name__}); special cards stay in the main deck")
        self.sync_decklists(items, "Decks")

    # -- lists
    def sync_wishlist(self, lines):
        ta = self.open_bulk_edit(WISHLIST_URL)
        existing = ta.get_attribute("value") or ""
        missing = deck_missing(lines, existing)
        self.log(f"  wish list has {len([l for l in existing.splitlines() if l.strip()])} lines")
        add = []
        if missing:
            have = {}
            for l in existing.splitlines():
                if l.strip() and not l.lstrip().startswith("-"):
                    k = front_name(parse_line(l)[0])
                    have[k] = have.get(k, 0) + line_qty(l)
            new_l = [l for l in missing if have.get(front_name(parse_line(l)[0]), 0) == 0]
            more_l = [(l, have[front_name(parse_line(l)[0])]) for l in missing
                      if have.get(front_name(parse_line(l)[0]), 0) > 0]
            if self.choose_wish_add:
                add = self.choose_wish_add(new_l, more_l) or []
            elif self.confirm(f"Add these {len(missing)} lines to your Moxfield wish list?\n\n"
                              + "\n".join("    " + l for l in missing)):
                add = missing
            if not add:
                self.log("  adding skipped by you")
        else:
            self.log("  nothing to add")
        remove = []
        if self.cfg.get("wish_remove_owned") and self.choose_remove:
            cand = []
            for l in existing.splitlines():
                if l.strip():
                    n = front_name(parse_line(l)[0])
                    if n in self._owned:
                        cand.append((l, self._owned[n]))
            if cand:
                self.log(f"  {len(cand)} wish list lines are cards you own")
                remove = self.choose_remove(cand) or []
            else:
                self.log("  no wish list cards that you own")
        if not add and not remove:
            return
        if remove:
            gone = set(remove)
            kept = [l for l in existing.splitlines() if l.strip() and l not in gone]
            new_text = "\n".join(kept + add)
            ta.click()
            ta.send_keys(Keys.CONTROL, "a")
            ta.send_keys(Keys.DELETE)
            ta.send_keys(new_text)
            time.sleep(0.5)
            self.step("Save", lambda: self.js_click(
                self.driver.find_element(By.XPATH, "//button[normalize-space(.)='Save']")))
            time.sleep(3)
        else:
            self.append_and_save(ta, add)
        if add:
            self.log(f"  added {len(add)} lines to the wish list")
        if remove:
            self.log(f"  removed {len(remove)} lines from the wish list")

    def sync_lists(self, df):
        handling = {n: c for n, c in self.cfg["lists"].items() if c != LIST_SKIP}
        if not handling:
            return
        owned = {}
        for n, q in zip(df[df["Binder Type"] != "list"]["Name"], df[df["Binder Type"] != "list"]["Quantity"]):
            k = front_name(n)
            owned[k] = owned.get(k, 0) + int(q)
        self._owned = owned
        deck_items = {}
        for name, how in handling.items():
            self.check_stop()
            g = df[(df["Binder Type"] == "list") & (df["Binder Name"] == name)]
            if g.empty:
                continue
            lines = decklist_lines(g)
            if how == LIST_WISH:
                self.log(f"== Wish list <- '{name}' ==")
                try:
                    self.sync_wishlist(lines)
                except Stopped:
                    raise
                except Exception as e:
                    self.log(f"  FAILED wish list: {e}")
            elif how == LIST_DECK:
                deck_items[name] = lines
        self.sync_decklists(deck_items, "Lists as decklists")

    # -- tidy existing decks (format + folder)
    BADGE_XP = ("//a[contains(@class,'badge-header')]"
                "[.//*[local-name()='svg'][@data-icon='chevron-down']]")
    KEBAB_XP = ".//a[.//*[local-name()='svg'][@data-icon='ellipsis-vertical']]"

    def read_format(self, url):
        self.driver.get(url)
        badge = self.wait(25).until(EC.presence_of_element_located((By.XPATH, self.BADGE_XP)))
        time.sleep(0.5)
        return (badge.get_attribute("textContent") or "").strip()

    def set_format(self, url, label):
        d = self.driver
        d.get(url)
        badge = self.wait(25).until(EC.presence_of_element_located((By.XPATH, self.BADGE_XP)))
        before = (badge.get_attribute("textContent") or "").strip()
        self.step("open format menu", lambda: self.js_click(badge))
        time.sleep(1)
        xp = (f"(//*[normalize-space(text())='{label}']"
              f"[not(ancestor-or-self::a[contains(@class,'badge-header')])])[last()]")
        item = self.step("pick format", lambda: self.wait(10).until(EC.presence_of_element_located((By.XPATH, xp))))
        self.js_click(item)
        time.sleep(2)
        after = (self.wait(25).until(EC.presence_of_element_located((By.XPATH, self.BADGE_XP)))
                 .get_attribute("textContent") or "").strip()
        if after.lower() == before.lower():
            raise RuntimeError(f"format still '{after}'")
        return after

    def move_deck(self, shown_name, folder, from_path=()):
        d = self.driver
        if not self.open_folder_path(list(from_path)):
            raise RuntimeError("couldn't open the deck's current folder")
        link = None
        for a in d.find_elements(By.XPATH, "//a[contains(@href,'/decks/')]"):
            if (a.get_attribute("textContent") or "").strip().lower() == shown_name.lower():
                link = a
                break
        if link is None:
            raise RuntimeError("deck not found in its folder")
        kebab = link.find_element(
            By.XPATH, "./ancestor::*[.//*[local-name()='svg'][@data-icon='ellipsis-vertical']][1]" + self.KEBAB_XP[1:])
        self.step("open row menu", lambda: self.js_click(kebab))
        time.sleep(1)
        leaf = "Top Level" if not folder else folder.split("/")[-1].strip()
        folder = leaf
        xp = (f"//*[contains(normalize-space(.),'Move to {folder}')]"
              f"[not(*[contains(normalize-space(.),'Move to {folder}')])]")
        try:
            item = self.wait(6).until(EC.presence_of_element_located((By.XPATH, xp)))
        except Exception:
            menus = d.find_elements(By.CSS_SELECTOR, ".dropdown-menu, [role='menu']")
            seen = [(m.get_attribute("textContent") or "").strip()[:160] for m in menus]
            seen = [x for x in seen if x]
            raise RuntimeError(f"no 'Move to {folder}' item; menus on page: {seen[-2:]}")
        self.js_click(item)
        time.sleep(2)

    def rename_deck(self, url, new_name):
        """On the deck page, click the name, type the new one and press Enter."""
        d = self.driver
        d.get(url)
        span = self.wait(25).until(EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='deck-name']")))
        cur = (span.get_attribute("textContent") or "").strip()
        time.sleep(1)
        try:
            span.click()
        except Exception:
            self.js_click(span)
        time.sleep(1)

        def edit_box(x):
            for e in x.find_elements(By.CSS_SELECTOR, "input, textarea"):
                try:
                    if e.is_displayed() and (e.get_attribute("value") or "").strip() == cur:
                        return e
                except Exception:
                    pass
            return None
        box = self.step("find the name box", lambda: self.wait(8).until(edit_box))
        box.send_keys(Keys.CONTROL, "a")
        box.send_keys(new_name)
        box.send_keys(Keys.TAB)                   # clicking away (blur) is what saves on Moxfield
        time.sleep(2)

        def shown_name():
            try:
                return (d.find_element(By.CSS_SELECTOR, "[data-testid='deck-name']").get_attribute("textContent") or "").strip()
            except Exception:
                return ""
        if shown_name() != new_name:
            try:                                  # fall back to Enter
                box.send_keys(Keys.ENTER)
            except Exception:
                pass
            time.sleep(2)
        d.get(url)                                # reload to be sure it really saved
        self.wait(25).until(EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='deck-name']")))
        time.sleep(1)
        if shown_name() != new_name:
            raise RuntimeError(f"name is still '{shown_name()}'")

    def tidy(self, df):
        self.log("== Fixing existing decks (format, folder, name) ==")
        idx = self.deck_index()
        mana, mana_orig = {}, {}
        if df is not None:
            for n, g in df[df["Binder Type"] == "deck"].groupby("Binder Name"):
                mana[sanitise_deck_name(n).lower()] = int(g["Quantity"].astype(int).sum())
                mana_orig[sanitise_deck_name(n).lower()] = n
        self.log(f"Reading the format of {len(idx)} decks (this takes a few minutes)...")
        info = {}
        for low, url in idx.items():
            self.check_stop()
            if low in self._deck_fmt_seen:               # already shown on the decks list
                info[low] = self._deck_fmt_seen[low]
                continue
            try:
                info[low] = self.read_format(url)
            except Stopped:
                raise
            except Exception as e:
                self.log(f"  couldn't read {self._deck_display.get(low, low)}: {type(e).__name__}")
        no_folder = [self._deck_display[l] for l in info if not self._deck_folder.get(l)]
        guesses = guess_folders(no_folder)
        rows = []
        for low, fmt_text in info.items():
            shown = self._deck_display[low]
            cur_folder = self._deck_folder.get(low, "")
            cur_fmt = format_label(fmt_text)
            if cur_fmt == "None" and fmt_text.strip().lower() != "none":
                cur_fmt = fmt_text.strip()                # a format we don't list: leave it as it is
            needs = not (cur_fmt != "None" and cur_folder)
            cards = mana.get(low, 0)
            if not cards:                                    # renamed deck: follow the remembered link to Manabox
                for mname, murl in self.state["decks"].items():
                    if murl == idx.get(low):
                        cards = mana.get(sanitise_deck_name(mname).lower(), 0)
                        break
            new_fmt = cur_fmt if cur_fmt != "None" else guess_format(shown, cards)
            new_folder = cur_folder or guesses.get(shown, "")
            rows.append((shown, cards, cur_fmt, cur_folder, new_fmt, new_folder, needs))
        rows.sort(key=lambda r: (not r[6], r[0].lower()))
        if not any(r[6] for r in rows):
            self.log("  every deck already has a format and a folder")
        if not rows:
            self.log("  every deck already has a format and a folder")
            return
        changes = self.choose_tidy(rows, sorted(self._folder_paths, key=str.lower))
        if not changes:
            self.log("  nothing changed")
            return
        folders_needed = {c["folder"] for c in changes.values() if c.get("folder") and c["folder"] != TOP}
        for f in sorted(folders_needed):
            try:
                self.ensure_folder_path(f)
            except Stopped:
                raise
            except Exception as e:
                self.log(f"  FAILED creating folder '{f}': {e}")
        for shown, c in changes.items():
            self.check_stop()
            url = idx.get(shown.lower())
            if c.get("format"):
                try:
                    now = self.set_format(url, c["format"])
                    self.log(f"  {shown}: format -> {now}")
                except Stopped:
                    raise
                except Exception as e:
                    self.log(f"  FAILED format for '{shown}': {e}")
        for shown, c in changes.items():
            self.check_stop()
            if c.get("folder"):
                try:
                    cur = [x for x in self._deck_folder.get(shown.lower(), "").split("/") if x]
                    self.move_deck(shown, "" if c["folder"] == TOP else c["folder"], cur)
                    self.log(f"  {shown}: moved to {c['folder']}")
                except Stopped:
                    raise
                except Exception as e:
                    self.log(f"  FAILED moving '{shown}': {e}")
        for shown, c in changes.items():                   # renames last, so the other steps find decks by old name
            self.check_stop()
            if c.get("rename"):
                try:
                    url = idx.get(shown.lower())
                    self.rename_deck(url, c["rename"])
                    if shown.lower() in mana_orig:         # keep the link to the Manabox deck
                        self.state["decks"][mana_orig[shown.lower()]] = url
                        self.save_state()
                    self.log(f"  {shown}: renamed to '{c['rename']}'")
                except Stopped:
                    raise
                except Exception as e:
                    self.log(f"  FAILED renaming '{shown}': {e}")

    # -- commanders
    def alert_showing(self):
        return bool(self.driver.execute_script(
            "var t=(document.body&&document.body.textContent)||'';"
            "return t.indexOf('without a Commander')>=0||t.indexOf('Pick a Commander')>=0;"))

    def has_commander_alert(self, url):
        self.driver.get(url)
        try:
            self.wait(20).until(EC.presence_of_element_located((By.XPATH, "//li[@data-testid='deck-view-table-row']")))
        except Exception:
            pass
        for _ in range(6):                       # the banner can appear a moment after the card list
            time.sleep(1)
            if self.alert_showing():
                return True
        return False

    def set_commander(self, url, name):
        try:
            self.set_commander_via_row(url, name)
        except Stopped:
            raise
        except Exception as e:
            self.log(f"    row menu didn't work ({e}); trying the search popup")
            self.set_commander_via_popup(url, name)

    def set_commander_via_row(self, url, name):
        """Card row > caret-down menu > 'Set as Commander'."""
        d = self.driver
        d.get(url)
        self.wait(25).until(EC.presence_of_element_located((By.XPATH, "//li[@data-testid='deck-view-table-row']")))
        time.sleep(1.5)
        want = name.lower()
        row = None
        for li in d.find_elements(By.XPATH, "//li[@data-testid='deck-view-table-row']"):
            try:
                t = (li.find_element(By.XPATH, ".//*[@data-testid='deck-view-table-row-name']")
                     .get_attribute("textContent") or "").strip().lower()
            except Exception:
                continue
            if t == want or t.startswith(want + " /") or want.startswith(t + " /"):
                row = li
                break
        if row is None:
            raise RuntimeError(f"'{name}' not found in the deck list")
        caret = row.find_element(By.XPATH, ".//a[@role='button'][.//*[contains(@class,'fa-caret-down')]]")
        self.step("open card menu", lambda: self.js_click(caret))
        time.sleep(1)
        item = self.step("Set as Commander", lambda: self.wait(8).until(EC.presence_of_element_located(
            (By.XPATH, "//*[normalize-space(text())='Set as Commander'] | //*[normalize-space(text())='Set as commander']"))))
        self.js_click(item)
        time.sleep(2.5)
        time.sleep(1)
        if self.alert_showing():
            raise RuntimeError("commander alert still showing")

    def set_commander_via_popup(self, url, name):
        d = self.driver
        d.get(url)
        pc = "//*[contains(normalize-space(.),'Pick a Commander')][not(*[contains(normalize-space(.),'Pick a Commander')])]"
        alert = self.step("find 'Pick a Commander'", lambda: self.wait(20).until(EC.presence_of_element_located(
            (By.XPATH, pc))))
        self.step("open commander picker", lambda: self.js_click(alert))
        modal = self.step("wait for picker", lambda: self.wait(10).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, ".modal-dialog, [role='dialog']"))))
        time.sleep(1)
        boxes = [b for b in modal.find_elements(By.CSS_SELECTOR, "input")
                 if b.is_displayed() and (b.get_attribute("type") or "text") in ("text", "search", "")]
        if not boxes:
            raise RuntimeError("no search box in the commander picker")
        boxes[0].click()
        boxes[0].send_keys(name)
        time.sleep(2.5)
        lit = xp_lit(name)
        xp = (f".//*[contains(normalize-space(.),{lit})]"
              f"[not(*[contains(normalize-space(.),{lit})])][not(self::input)]")
        try:
            item = self.wait(8).until(lambda x: next((e for e in modal.find_elements(By.XPATH, xp) if e.is_displayed()), None))
        except Exception:
            raise RuntimeError(f"no search result for '{name}'; picker text: {(modal.get_attribute('textContent') or '').strip()[:200]!r}")
        self.js_click(item)
        time.sleep(2.5)
        if self.alert_showing():
            m = d.find_elements(By.CSS_SELECTOR, ".modal-dialog, [role='dialog']")
            raise RuntimeError("commander not set; picker still shows: "
                               + (((m[0].get_attribute('textContent') or '').strip()[:200]) if m else "(closed)"))

    def commanders(self, df):
        self.log("== Set commanders (EDH decks) ==")
        idx = self.deck_index()
        mana = {sanitise_deck_name(n).lower(): g for n, g in df[df["Binder Type"] == "deck"].groupby("Binder Name")}
        edh = [l for l in idx if format_label(self._deck_fmt_seen.get(l, "")) in COMMANDER_FORMATS]
        self.log(f"  {len(edh)} decks in commander-style formats; checking which have no commander set...")
        missing = []
        for low in edh:
            self.check_stop()
            try:
                if self.has_commander_alert(idx[low]):
                    missing.append(low)
            except Stopped:
                raise
            except Exception as e:
                self.log(f"  couldn't check {self._deck_display.get(low, low)}: {type(e).__name__}")
        if not missing:
            self.log("  every Commander deck already has a commander")
            return
        ids = []
        for low in missing:
            if low in mana:
                ids += list(mana[low]["Scryfall ID"].dropna().astype(str))
        self.log(f"  looking up {len(set(ids))} cards on Scryfall...")
        try:
            info = scryfall_lookup(ids)
        except Exception as e:
            self.log(f"  Scryfall lookup failed ({type(e).__name__}); you can still type names yourself")
            info = {}
        rows = []
        for low in missing:
            walk = format_label(self._deck_fmt_seen.get(low, "")) in BRAWL_FORMATS
            names, pre = commander_choices(mana[low], info, walk) if low in mana else ([], "")
            rows.append((self._deck_display[low], names, pre))
        rows.sort(key=lambda r: r[0].lower())
        chosen = self.choose_cmdr(rows)
        if not chosen:
            self.log("  nothing changed")
            return
        for shown, name in chosen.items():
            self.check_stop()
            try:
                self.set_commander(idx[shown.lower()], name)
                self.log(f"  {shown}: commander -> {name}")
            except Stopped:
                raise
            except Exception as e:
                self.log(f"  FAILED commander for '{shown}': {e}")

    def run_commanders(self, df):
        try:
            self.connect()
            self.commanders(df)
            self.log("All done.")
        except Stopped:
            self.log("Stopped.")
        except Exception as e:
            self.log(f"ERROR: {e}")
            self.log(traceback.format_exc(limit=-4))
        finally:
            try:
                if self.driver:
                    self.driver.quit()
            except Exception:
                pass

    def run_tidy(self, df):
        try:
            self.connect()
            self.tidy(df)
            self.log("All done.")
        except Stopped:
            self.log("Stopped.")
        except Exception as e:
            self.log(f"ERROR: {e}")
            self.log(traceback.format_exc(limit=-4))
        finally:
            try:
                if self.driver:
                    self.driver.quit()
            except Exception:
                pass

    # -- run
    def run_actions(self):
        """End-of-run follow-ups. Today: cards that are on Moxfield but not in Manabox (sold, given away...) can be
        added to a decklist of your choice per card (an existing list or a new name)."""
        names = sorted(self.cfg.get("lists", {}), key=str.lower)
        for pref in ("Sold", "IOU MTG"):
            if pref not in names:
                names.append(pref)
        try:
            idx = self.deck_index()
            names += [self._deck_display[l] for l in idx if self._deck_display[l] not in names]
        except Stopped:
            raise
        except Exception:
            pass
        default = next((n for n, c in self.cfg.get("lists", {}).items() if c == LIST_DECK), "Sold")
        res = self.choose_actions(self._extras, names, default)
        groups = (res or {}).get("groups") or {}
        if not groups:
            self.log("  no follow-up actions chosen")
            return
        for deck, lines in groups.items():
            self.check_stop()
            name = sanitise_deck_name(deck)
            self.log(f"== Add {len(lines)} cards to decklist '{name}' ==")
            try:
                pairs = [(parse_line(l)[0], l) for l in lines]
                url = self.deck_url(name)
                if url:
                    ta = self.open_bulk_edit(url)
                    missing = deck_missing(pairs, ta.get_attribute("value") or "")
                    if not missing:
                        self.log("  those cards are already on that decklist")
                        continue
                    self.append_and_save(ta, missing)
                    self.log(f"  added {len(missing)} lines to '{name}'")
                else:
                    self.create_deck(name, "\n".join(lines), "none", "")
                    self.state["decks"][name] = self.driver.current_url
                    self.save_state()
                    self.log(f"  created decklist '{name}' with {len(lines)} lines")
            except Stopped:
                raise
            except Exception as e:
                self.log(f"  FAILED adding to '{name}': {e}")
        self.log("  These cards are still in your Moxfield collection: remove them there by hand "
                 "(the app can't remove cards).")

    def run(self, df):
        try:
            self.connect()
            if self.cfg["do_binders"]:
                self.sync_binders(df)
            if self.cfg["do_decks"]:
                self.sync_decks(df)
            if self.cfg["do_lists"]:
                self.sync_lists(df)
            if self.cfg.get("set_commanders") and self.choose_cmdr:
                self.commanders(df)
            if self._extras and self.choose_actions:
                self.run_actions()
            self.log("All done.")
        except Stopped:
            self.log("Stopped.")
        except Exception as e:
            self.log(f"ERROR: {e}")
            self.log(traceback.format_exc(limit=-4))
        finally:
            try:
                if self.driver:
                    self.driver.quit()
            except Exception:
                pass


# ── GUI ──────────────────────────────────────────────────────────────────────

class App:
    def __init__(self, root):
        self.root = root
        root.title("M2M - Manabox to Moxfield")
        root.geometry("760x720")
        self.cfg = dict(DEFAULTS)
        try:
            self.cfg.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except Exception:
            pass
        if not self.cfg["chrome"]:
            self.cfg["chrome"] = find_chrome()

        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.df = None
        self.list_vars = {}

        pad = {"padx": 8, "pady": 4}
        f = ttk.Frame(root)
        f.pack(fill="x", **pad)
        f.columnconfigure(1, weight=1)

        self.csv_var = tk.StringVar(value=self.cfg["csv"])
        self.chrome_var = tk.StringVar(value=self.cfg["chrome"])
        self.profile_var = tk.StringVar(value=self.cfg["profile"])
        self.vis_var = tk.StringVar(value=self.cfg["visibility"])
        self.binders_var = tk.BooleanVar(value=self.cfg["do_binders"])
        self.decks_var = tk.BooleanVar(value=self.cfg["do_decks"])
        self.lists_var = tk.BooleanVar(value=self.cfg["do_lists"])
        self.remove_owned_var = tk.BooleanVar(value=self.cfg.get("wish_remove_owned", False))
        self.set_cmdr_var = tk.BooleanVar(value=self.cfg.get("set_commanders", False))
        self.deck_binders_var = tk.BooleanVar(value=self.cfg.get("deck_binders", False))

        ttk.Label(f, text="Manabox export (CSV)").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.csv_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(f, text="Browse...", command=self.pick_csv).grid(row=0, column=2, **pad)
        ttk.Label(f, text="Chrome location").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.chrome_var).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(f, text="Browse...", command=lambda: self.pick_file(self.chrome_var)).grid(row=1, column=2, **pad)
        ttk.Label(f, text="Chrome profile folder").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.profile_var).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(f, text="Browse...", command=lambda: self.pick_dir(self.profile_var)).grid(row=2, column=2, **pad)

        o = ttk.LabelFrame(root, text="1. What to copy from Manabox to Moxfield")
        o.pack(fill="x", **pad)
        ttk.Checkbutton(o, text="Collection: copy my Manabox binders to Moxfield binders",
                        variable=self.binders_var).pack(anchor="w", padx=8)
        ttk.Checkbutton(o, text="Also make a binder for each registered deck (off = those cards still count as owned, just without a binder)",
                        variable=self.deck_binders_var).pack(anchor="w", padx=28)
        ttk.Checkbutton(o, text="Decks: copy my Manabox decks to Moxfield as decklists", variable=self.decks_var).pack(anchor="w", padx=8)
        r = ttk.Frame(o)
        r.pack(anchor="w", padx=8, pady=2)
        ttk.Label(r, text="Visibility of new decks:").pack(side="left")
        ttk.Combobox(r, textvariable=self.vis_var, values=["Private", "Unlisted", "Public"],
                     width=10, state="readonly").pack(side="left", padx=6)
        r2 = ttk.Frame(o)
        r2.pack(anchor="w", padx=8, pady=2)
        ttk.Label(r2, text="Decks already on Moxfield:").pack(side="left")
        self.deck_mode_var = tk.StringVar(value=self.cfg.get("deck_mode", DECK_NEW))
        ttk.Combobox(r2, textvariable=self.deck_mode_var, values=DECK_CHOICES,
                     width=40, state="readonly").pack(side="left", padx=6)
        ttk.Checkbutton(o, text="After copying decks, offer to set a commander on Commander decks that have none",
                        variable=self.set_cmdr_var).pack(anchor="w", padx=8)
        ttk.Checkbutton(o, text="Lists: copy my Manabox lists (choose what happens to each one below)", variable=self.lists_var).pack(anchor="w", padx=8)
        ttk.Checkbutton(o, text="Wish list: offer to remove cards I now own (you review them first)",
                        variable=self.remove_owned_var).pack(anchor="w", padx=28)
        ttk.Label(o, text="Moxfield has only ONE wish list, so every list set to 'Wish list' is merged into it. 'Decklist not in collection' makes a separate deck.",
                  foreground="#555555").pack(anchor="w", padx=28)
        lf = ttk.Frame(o)                      # scrollable, so many lists can't push the window off screen
        lf.pack(fill="x", padx=28, pady=(0, 6))
        self.lists_canvas = tk.Canvas(lf, height=110, highlightthickness=0)
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.lists_canvas.yview)
        self.lists_canvas.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.lists_canvas.pack(side="left", fill="x", expand=True)
        self.lists_frame = ttk.Frame(self.lists_canvas)
        self.lists_canvas.create_window((0, 0), window=self.lists_frame, anchor="nw")
        self.lists_frame.bind("<Configure>", lambda e: self.lists_canvas.configure(
            scrollregion=self.lists_canvas.bbox("all")))
        self.lists_canvas.bind("<Enter>", lambda e: self.lists_canvas.bind_all(
            "<MouseWheel>", lambda ev: self.lists_canvas.yview_scroll(int(-ev.delta / 120), "units")))
        self.lists_canvas.bind("<Leave>", lambda e: self.lists_canvas.unbind_all("<MouseWheel>"))

        warn = ("Sync only ADDS things on Moxfield; it never deletes. You are asked before anything changes. "
                "Cards on Moxfield but not in Manabox are shown in a review window at the end.")
        ttk.Label(root, text=warn, wraplength=720, foreground="#a05000").pack(anchor="w", **pad)

        b = ttk.Frame(root)
        b.pack(fill="x", **pad)
        self.start_btn = ttk.Button(b, text="Start sync (copy Manabox to Moxfield)", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(b, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(b, text="Help", command=lambda: self.show_welcome(False)).pack(side="right")
        fx = ttk.LabelFrame(root, text="2. Fix decks already on Moxfield (separate from the sync above)")
        fx.pack(fill="x", **pad)
        self.tidy_btn = ttk.Button(fx, text="Set format, folder or name...", command=self.start_tidy)
        self.tidy_btn.pack(side="left", padx=8, pady=4)
        self.cmdr_btn = ttk.Button(fx, text="Pick missing commanders...", command=self.start_cmdr)
        self.cmdr_btn.pack(side="left", padx=8, pady=4)

        self.logbox = tk.Text(root, height=18, state="disabled", wrap="word")
        self.logbox.pack(fill="both", expand=True, **pad)

        if self.cfg["csv"] and Path(self.cfg["csv"]).exists():
            self.load_csv(self.cfg["csv"], quiet=True)
        root.after(100, self.poll)
        if self.cfg.get("seen_version") != VERSION:
            root.after(400, lambda: self.show_welcome(True))

    WELCOME = (
        "M2M copies your Manabox collection to Moxfield: binders, decklists (with formats, folders and "
        "commanders) and wish lists. It only goes one way, Manabox -> Moxfield.\n\n"
        "How to use it\n"
        "1. In Manabox, export your collection as a CSV, then choose it at the top of this window.\n"
        "2. Tick what to sync and click Start sync.\n"
        "3. A Chrome window opens. Log in to Moxfield there once (the app remembers it).\n"
        "4. The app controls that Chrome window like a person clicking through Moxfield. Don't close it, "
        "click in it or minimise it (keep it at least partly visible). You can use other windows meanwhile.\n"
        "5. Syncing takes time: a first sync of a big collection can take from several minutes to over an hour. "
        "Later syncs are much quicker because only the differences are added.\n"
        "6. The app shows what it is about to change and asks first.\n\n"
        "Good to know\n"
        "- It only adds things on Moxfield. It never deletes anything.\n"
        "- Moxfield has only one wish list. Manabox lists set to 'Wish list' are all merged into it; lists set to "
        "'Decklist not in collection' become separate decklists.\n"
        "- 'Set format, folder or name...' and 'Pick missing commanders...' fix decks that are already on Moxfield, "
        "without running a sync.\n"
        "- Moxfield only allows letters, numbers, spaces and hyphens in binder names, so some names are "
        "renamed (the app lists them first).\n"
        "- Settings and your Moxfield login are stored in the M2M_data folder next to this program. Delete that folder to reset.\n"
        "- Windows may warn about an unknown publisher the first time: More info -> Run anyway.\n\n"
        "You can reopen this any time with the Help button.")

    def show_welcome(self, first):
        top = tk.Toplevel(self.root)
        top.title(f"M2M {VERSION}")
        top.geometry("600x760")
        top.transient(self.root)
        top.grab_set()
        row = ttk.Frame(top)
        row.pack(side="bottom", fill="x", padx=16, pady=12)
        ttk.Label(top, text=f"What's new in {VERSION}", font=("TkDefaultFont", 10, "bold")).pack(
            anchor="w", padx=16, pady=(14, 2))
        ttk.Label(top, text=WHATS_NEW, wraplength=550, justify="left").pack(anchor="w", padx=16)
        ttk.Label(top, text=self.WELCOME, wraplength=550, justify="left").pack(anchor="w", padx=16, pady=12)
        mail = ttk.Label(top, text=f"Feedback or problems: {CONTACT}", foreground="#0b57d0",
                         cursor="hand2", font=("TkDefaultFont", 9, "underline"))
        mail.pack(anchor="w", padx=16)
        mail.bind("<Button-1>", lambda e: webbrowser.open(f"mailto:{CONTACT}?subject=M2M%20feedback"))
        if TIP_URL:
            tip = ttk.Label(top, text="Find M2M useful? Tip me on Ko-fi", foreground="#0b57d0",
                            cursor="hand2", font=("TkDefaultFont", 9, "underline"))
            tip.pack(anchor="w", padx=16, pady=(4, 0))
            tip.bind("<Button-1>", lambda e: webbrowser.open(TIP_URL))
        hide = tk.BooleanVar(value=True)
        ttk.Button(row, text="Send feedback", command=lambda: webbrowser.open(
            f"mailto:{CONTACT}?subject=M2M%20feedback")).pack(side="left", padx=(0, 8))
        if first:
            ttk.Checkbutton(row, text="Don't show this again", variable=hide).pack(side="left")

        def close():
            if first and hide.get():
                self.cfg["welcome_seen"] = True
                self.cfg["seen_version"] = VERSION
                try:
                    APP_DIR.mkdir(parents=True, exist_ok=True)
                    SETTINGS_FILE.write_text(json.dumps(self.cfg, indent=1), encoding="utf-8")
                except Exception:
                    pass
            top.destroy()
        ttk.Button(row, text="Got it", command=close).pack(side="right")
        top.protocol("WM_DELETE_WINDOW", close)

    # -- file pickers
    def pick_csv(self):
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if p:
            self.csv_var.set(p)
            self.load_csv(p)

    def pick_file(self, var):
        p = filedialog.askopenfilename()
        if p:
            var.set(p)

    def pick_dir(self, var):
        p = filedialog.askdirectory()
        if p:
            var.set(p)

    def load_csv(self, path, quiet=False):
        try:
            self.df = load_manabox(path)
        except Exception as e:
            self.df = None
            if not quiet:
                messagebox.showerror(APP_NAME, str(e))
            return
        for w in self.lists_frame.winfo_children():
            w.destroy()
        self.list_vars = {}
        saved = self.cfg.get("lists", {})
        names = list_names(self.df)
        if not names:
            ttk.Label(self.lists_frame, text="(no Manabox lists found in this export)").pack(anchor="w")
        for n in names:
            default = LIST_WISH if re.search(r"want|wish", n, re.I) else LIST_SKIP
            v = tk.StringVar(value=saved.get(n, default))
            row = ttk.Frame(self.lists_frame)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=n, width=24).pack(side="left")
            ttk.Combobox(row, textvariable=v, values=LIST_CHOICES, width=32, state="readonly").pack(side="left")
            self.list_vars[n] = v
        d = self.df
        self.log(f"Loaded {len(d)} rows: {d[d['Binder Type']!='list']['Binder Name'].nunique()} binders/decks, "
                 f"{d[d['Binder Type']=='deck']['Binder Name'].nunique()} registered decks, {len(names)} lists.")

    # -- logging / prompts (called from the worker thread via the queue)
    def log(self, msg):
        self.q.put(("log", msg))

    def confirm(self, msg):
        ev, box = threading.Event(), {}
        self.q.put(("confirm", msg, ev, box))
        ev.wait()
        return box.get("ok", False)

    def pause(self, msg):
        ev = threading.Event()
        self.q.put(("pause", msg, ev, None))
        ev.wait()

    def ask_yes_no(self, msg):
        """Scrollable Yes/No dialog whose buttons always stay visible (messages can be long)."""
        top = tk.Toplevel(self.root)
        top.title(APP_NAME)
        top.geometry("680x560")
        top.transient(self.root)
        top.grab_set()
        result = {"ok": False}

        def yes():
            result["ok"] = True
            top.destroy()
        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", pady=8)
        ttk.Button(btns, text="Yes", command=yes).pack(side="right", padx=10)
        ttk.Button(btns, text="No", command=top.destroy).pack(side="right")
        sb = ttk.Scrollbar(top)
        sb.pack(side="right", fill="y")
        txt = tk.Text(top, wrap="word", yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.configure(command=txt.yview)
        txt.insert("1.0", msg)
        txt.configure(state="disabled")
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        self.root.wait_window(top)
        return result["ok"]

    def choose_opts(self, rows, folders=(), unmatched=()):
        ev, box = threading.Event(), {}
        self.q.put(("deckopts", rows, ev, box, list(folders), list(unmatched)))
        ev.wait()
        return box.get("opts")

    def deck_options_dialog(self, rows, folders=(), unmatched=()):
        """Review window for new decks: pick a format and folder for each (guesses pre-filled)."""
        top = tk.Toplevel(self.root)
        top.title("New decks")
        top.geometry("1180x560")
        top.transient(self.root)
        top.grab_set()
        result = {"opts": None}
        ttk.Label(top, text="These decks are not on Moxfield yet. Check the suggested format and folder "
                            "(guesses - change anything), then click Create.", wraplength=720).pack(anchor="w", padx=10, pady=8)
        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", pady=8)
        canvas = tk.Canvas(top, highlightthickness=0)
        sb = ttk.Scrollbar(top, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0))
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        for i, h in enumerate(("Deck", "Cards", "Format", "Commander", "Folder (blank = none)",
                               "Already on Moxfield as (if renamed)")):
            ttk.Label(inner, text=h, font=("TkDefaultFont", 9, "bold")).grid(row=0, column=i, sticky="w", padx=4, pady=2)
        vars_ = {}

        def wire(fv, cv, cb, ch):
            """Commander box follows the chosen format (only commander-style formats have one)."""
            def refresh(*_):
                label = fv.get()
                if label in COMMANDER_FORMATS:
                    names, pre = (ch or {}).get("walk" if label in BRAWL_FORMATS else "std", ([], ""))
                    cb.configure(values=names, state="normal")
                    if not cv.get() or cv.get() not in names:
                        cv.set(pre)
                else:
                    cb.configure(values=[], state="disabled")
                    cv.set("")
            fv.trace_add("write", refresh)
            refresh()

        for r, row in enumerate(rows, start=1):
            name, cards, fmt, folder = row[:4]
            ch = row[4] if len(row) > 4 else None
            fv, ov, cv = tk.StringVar(value=fmt), tk.StringVar(value=folder), tk.StringVar()
            ttk.Label(inner, text=name, width=30).grid(row=r, column=0, sticky="w", padx=4, pady=1)
            ttk.Label(inner, text=str(cards)).grid(row=r, column=1, padx=4)
            ttk.Combobox(inner, textvariable=fv, values=list(FORMATS), width=20, state="readonly").grid(row=r, column=2, padx=4)
            cb = ttk.Combobox(inner, textvariable=cv, values=[], width=30)
            cb.grid(row=r, column=3, padx=4)
            ttk.Combobox(inner, textvariable=ov, values=[""] + list(folders), width=22).grid(row=r, column=4, padx=4)
            mv = tk.StringVar()
            ttk.Combobox(inner, textvariable=mv, values=[""] + list(unmatched), width=26,
                         state="readonly").grid(row=r, column=5, padx=4)
            vars_[name] = (fv, ov, cv, mv)
            wire(fv, cv, cb, ch)

        def create():
            result["opts"] = {n: (FORMATS.get(f.get(), "none"), o.get().strip(),
                                  c.get().strip() if f.get() in COMMANDER_FORMATS else "", m.get().strip())
                              for n, (f, o, c, m) in vars_.items()}
            top.destroy()
        ttk.Button(btns, text="Create decks", command=create).pack(side="right", padx=10)
        ttk.Button(btns, text="Skip new decks", command=top.destroy).pack(side="right")
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        self.root.wait_window(top)
        return result["opts"]

    def choose_wish_add(self, new_lines, more_lines):
        ev, box = threading.Event(), {}
        self.q.put(("wishadd", new_lines, ev, box, more_lines))
        ev.wait()
        return box.get("lines")

    def wishadd_dialog(self, new_lines, more_lines):
        top = tk.Toplevel(self.root)
        top.title("Add to wish list")
        top.geometry("680x600")
        top.transient(self.root)
        top.grab_set()
        result = {"lines": None}
        ttk.Label(top, text="Moxfield keeps one printing per card on the wish list, so the number shows how many "
                            "you want. New cards are ticked. Cards already on your wish list are not ticked: tick "
                            "them only if you want extra copies (for example for several decks).",
                  wraplength=640).pack(anchor="w", padx=10, pady=8)
        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", pady=8)
        canvas = tk.Canvas(top, highlightthickness=0)
        sb = ttk.Scrollbar(top, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0))
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        items, r = [], 0
        if new_lines:
            ttk.Label(inner, text=f"New cards ({len(new_lines)})", font=("TkDefaultFont", 9, "bold")).grid(
                row=r, column=0, sticky="w", pady=(2, 4))
            r += 1
            for l in new_lines:
                v = tk.BooleanVar(value=True)
                ttk.Checkbutton(inner, text=l, variable=v).grid(row=r, column=0, sticky="w", padx=4)
                items.append((l, v))
                r += 1
        if more_lines:
            ttk.Label(inner, text=f"Already on your wish list - extra copies ({len(more_lines)})",
                      font=("TkDefaultFont", 9, "bold")).grid(row=r, column=0, sticky="w", pady=(10, 4))
            r += 1
            for l, have in more_lines:
                v = tk.BooleanVar(value=False)
                ttk.Checkbutton(inner, text=f"{l}   (already wanted: {have})", variable=v).grid(
                    row=r, column=0, sticky="w", padx=4)
                items.append((l, v))
                r += 1

        def apply():
            result["lines"] = [l for l, v in items if v.get()]
            top.destroy()
        ttk.Button(btns, text="Add ticked", command=apply).pack(side="right", padx=10)
        ttk.Button(btns, text="Skip", command=top.destroy).pack(side="right")
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        self.root.wait_window(top)
        return result["lines"]

    def choose_remove(self, rows):
        ev, box = threading.Event(), {}
        self.q.put(("remove", rows, ev, box))
        ev.wait()
        return box.get("lines")

    def remove_dialog(self, rows):
        top = tk.Toplevel(self.root)
        top.title("Remove from wish list")
        top.geometry("620x520")
        top.transient(self.root)
        top.grab_set()
        result = {"lines": None}
        ttk.Label(top, text="These wish list cards are in your collection. Ticked lines will be removed from your "
                            "Moxfield wish list. Untick any you still want (for example a different printing).",
                  wraplength=580).pack(anchor="w", padx=10, pady=8)
        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", pady=8)
        canvas = tk.Canvas(top, highlightthickness=0)
        sb = ttk.Scrollbar(top, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0))
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        vars_ = []
        for r, (line, owned) in enumerate(rows):
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(inner, text=line, variable=v).grid(row=r, column=0, sticky="w", padx=4)
            ttk.Label(inner, text=f"you own {owned}").grid(row=r, column=1, sticky="w", padx=12)
            vars_.append((line, v))

        def apply():
            result["lines"] = [l for l, v in vars_ if v.get()]
            top.destroy()
        ttk.Button(btns, text="Remove ticked", command=apply).pack(side="right", padx=10)
        ttk.Button(btns, text="Keep all", command=top.destroy).pack(side="right")
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        self.root.wait_window(top)
        return result["lines"]

    def choose_actions(self, extras, list_names, default=""):
        ev, box = threading.Event(), {}
        self.q.put(("actions", extras, ev, box, list(list_names), default))
        ev.wait()
        return box.get("res")

    def actions_dialog(self, extras, list_names, default=""):
        top = tk.Toplevel(self.root)
        top.title("Actions")
        top.geometry("760x600")
        top.transient(self.root)
        top.grab_set()
        result = {"res": None}
        ttk.Label(top, text=f"{len(extras)} cards are on Moxfield but not in Manabox (sold, given away...). "
                            "Tick the ones to put on a Moxfield decklist and choose which list each goes to: "
                            "pick an existing list or type a new name.", wraplength=720).pack(anchor="w", padx=10, pady=8)
        allrow = ttk.Frame(top)
        allrow.pack(anchor="w", padx=10)
        ttk.Label(allrow, text="Set every ticked card to:").pack(side="left")
        all_var = tk.StringVar(value=default or (list_names[0] if list_names else ""))
        ttk.Combobox(allrow, textvariable=all_var, values=list_names, width=28).pack(side="left", padx=6)
        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", pady=8)
        ttk.Label(top, text="The cards stay in your Moxfield collection: remove them there by hand.",
                  foreground="#a05000").pack(side="bottom", anchor="w", padx=10)
        canvas = tk.Canvas(top, highlightthickness=0)
        sb = ttk.Scrollbar(top, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=6)
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        items = []
        for r, (name, st, num, q, foil) in enumerate(extras):
            f = {"foil": " *F*", "etched": " *E*"}.get(foil, "")
            line = f"{q} {name} ({st.upper()}) {num}{f}"
            v, t = tk.BooleanVar(value=True), tk.StringVar(value=all_var.get())
            ttk.Checkbutton(inner, text=line, variable=v, width=44).grid(row=r, column=0, sticky="w", padx=4)
            ttk.Combobox(inner, textvariable=t, values=list_names, width=26).grid(row=r, column=1, padx=4, pady=1)
            items.append((line, v, t))

        def set_all():
            for _, v, t in items:
                if v.get():
                    t.set(all_var.get())
        ttk.Button(allrow, text="Apply", command=set_all).pack(side="left")

        def apply():
            groups = {}
            for line, v, t in items:
                if v.get() and t.get().strip():
                    groups.setdefault(t.get().strip(), []).append(line)
            result["res"] = {"groups": groups}
            top.destroy()
        ttk.Button(btns, text="Add ticked", command=apply).pack(side="right", padx=10)
        ttk.Button(btns, text="Skip", command=top.destroy).pack(side="right")
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        self.root.wait_window(top)
        return result["res"]

    def choose_cmdr(self, rows):
        ev, box = threading.Event(), {}
        self.q.put(("cmdr", rows, ev, box))
        ev.wait()
        return box.get("chosen")

    def cmdr_dialog(self, rows):
        top = tk.Toplevel(self.root)
        top.title("Pick missing commanders")
        top.geometry("620x500")
        top.transient(self.root)
        top.grab_set()
        result = {"chosen": None}
        ttk.Label(top, text="Commander decks with no commander. Pick one (legendary creatures from the deck are listed), "
                            "or type a card name. Leave blank to skip.", wraplength=580).pack(anchor="w", padx=10, pady=8)
        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", pady=8)
        canvas = tk.Canvas(top, highlightthickness=0)
        sb = ttk.Scrollbar(top, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0))
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        vars_ = {}
        for r, (deck, names, pre) in enumerate(rows):
            v = tk.StringVar(value=pre)
            ttk.Label(inner, text=deck, width=28).grid(row=r, column=0, sticky="w", padx=4, pady=2)
            ttk.Combobox(inner, textvariable=v, values=names, width=38).grid(row=r, column=1, padx=4)
            vars_[deck] = v

        def apply():
            result["chosen"] = {d: v.get().strip() for d, v in vars_.items() if v.get().strip()}
            top.destroy()
        ttk.Button(btns, text="Apply", command=apply).pack(side="right", padx=10)
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right")
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        self.root.wait_window(top)
        return result["chosen"]

    def choose_tidy(self, rows, folders=()):
        ev, box = threading.Event(), {}
        self.q.put(("tidy", rows, ev, box, list(folders)))
        ev.wait()
        return box.get("changes")

    def tidy_dialog(self, rows, folders=()):
        """Decks missing a format or folder: edit the suggestions, then apply."""
        top = tk.Toplevel(self.root)
        top.title("Set format, folder or name")
        top.geometry("1120x600")
        top.transient(self.root)
        top.grab_set()
        result = {"changes": None}
        ttk.Label(top, text="Decks with no format or no folder. Suggestions are guesses - change anything. "
                            "Folder: type a name, use A/B for a folder inside a folder (created if missing), or - to move a deck to the top level.",
                  wraplength=860).pack(anchor="w", padx=10, pady=8)
        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", pady=8)
        canvas = tk.Canvas(top, highlightthickness=0)
        sb = ttk.Scrollbar(top, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0))
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        for i, h in enumerate(("Deck", "Cards", "Now: format", "Now: folder", "New format", "New folder", "Rename to")):
            ttk.Label(inner, text=h, font=("TkDefaultFont", 9, "bold")).grid(row=0, column=i, sticky="w", padx=4, pady=2)
        vars_ = {}
        extra = []                                    # widgets of decks that need nothing
        show_all = tk.BooleanVar(value=False)

        def toggle():
            for ws in extra:
                for w in ws:
                    (w.grid if show_all.get() else w.grid_remove)()
        ttk.Checkbutton(btns, text="Show all decks", variable=show_all, command=toggle).pack(side="left", padx=10)
        for r, (name, cards, cur_fmt, cur_folder, new_fmt, new_folder, needs) in enumerate(rows, start=1):
            fv, ov, rv = tk.StringVar(value=new_fmt), tk.StringVar(value=new_folder), tk.StringVar()
            ws = [ttk.Label(inner, text=name, width=30), ttk.Label(inner, text=str(cards)),
                  ttk.Label(inner, text=cur_fmt), ttk.Label(inner, text=cur_folder or "-"),
                  ttk.Combobox(inner, textvariable=fv, values=list(FORMATS), width=20, state="readonly"),
                  ttk.Combobox(inner, textvariable=ov, values=["", "-"] + list(folders), width=22),
                  ttk.Entry(inner, textvariable=rv, width=24)]
            for c, w in enumerate(ws):
                w.grid(row=r, column=c, sticky="w", padx=4, pady=1)
            if not needs:
                extra.append(ws)
                for w in ws:
                    w.grid_remove()
            vars_[name] = (cur_fmt, cur_folder, fv, ov, rv)

        def apply():
            changes = {}
            for n, (cur_fmt, cur_folder, fv, ov, rv) in vars_.items():
                c = {}
                if rv.get().strip() and rv.get().strip() != n:
                    c["rename"] = rv.get().strip()
                if fv.get() != cur_fmt and fv.get() != "None":
                    c["format"] = fv.get()
                nf = ov.get().strip()
                if nf in ("-", "top", "Top Level") and cur_folder:
                    c["folder"] = TOP
                elif nf and nf not in ("-", "top", "Top Level") and nf != cur_folder:
                    c["folder"] = nf
                if c:
                    changes[n] = c
            result["changes"] = changes
            top.destroy()
        ttk.Button(btns, text="Apply changes", command=apply).pack(side="right", padx=10)
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right")
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        self.root.wait_window(top)
        return result["changes"]

    def ask_file(self, msg):
        ev, box = threading.Event(), {}
        self.q.put(("file", msg, ev, box))
        ev.wait()
        return box.get("path", "")

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", item[1] + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "confirm":
                    item[3]["ok"] = self.ask_yes_no(item[1])
                    item[2].set()
                elif kind == "actions":
                    item[3]["res"] = self.actions_dialog(item[1], item[4], item[5] if len(item) > 5 else "")
                    item[2].set()
                elif kind == "wishadd":
                    item[3]["lines"] = self.wishadd_dialog(item[1], item[4])
                    item[2].set()
                elif kind == "remove":
                    item[3]["lines"] = self.remove_dialog(item[1])
                    item[2].set()
                elif kind == "cmdr":
                    item[3]["chosen"] = self.cmdr_dialog(item[1])
                    item[2].set()
                elif kind == "tidy":
                    item[3]["changes"] = self.tidy_dialog(item[1], item[4] if len(item) > 4 else [])
                    item[2].set()
                elif kind == "deckopts":
                    item[3]["opts"] = self.deck_options_dialog(item[1], item[4], item[5])
                    item[2].set()
                elif kind == "file":
                    messagebox.showinfo(APP_NAME, item[1])
                    item[3]["path"] = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
                    item[2].set()
                elif kind == "pause":
                    messagebox.showinfo(APP_NAME, item[1])
                    item[2].set()
                elif kind == "done":
                    self.tidy_btn.configure(state="normal")
                    self.cmdr_btn.configure(state="normal")
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    # -- run control
    def collect_cfg(self):
        self.cfg.update({
            "csv": self.csv_var.get().strip(), "chrome": self.chrome_var.get().strip(),
            "profile": self.profile_var.get().strip(), "visibility": self.vis_var.get(),
            "deck_mode": self.deck_mode_var.get(),
            "do_binders": self.binders_var.get(), "do_decks": self.decks_var.get(),
            "do_lists": self.lists_var.get(),
            "wish_remove_owned": self.remove_owned_var.get(),
            "set_commanders": self.set_cmdr_var.get(),
            "deck_binders": self.deck_binders_var.get(),
            "lists": {n: v.get() for n, v in self.list_vars.items()},
        })
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(self.cfg, indent=1), encoding="utf-8")

    def start(self):
        if self.csv_var.get().strip() and (self.df is None):
            self.load_csv(self.csv_var.get().strip())
        if self.df is None:
            messagebox.showerror(APP_NAME, "Choose your Manabox export CSV first.")
            return
        self.collect_cfg()
        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.tidy_btn.configure(state="disabled")
        self.cmdr_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        s = Syncer(self.cfg, self.log, self.confirm, self.pause, self.ask_file, self.choose_opts, self.choose_tidy, self.stop_event)
        s.choose_remove = self.choose_remove
        s.choose_wish_add = self.choose_wish_add
        s.choose_actions = self.choose_actions
        s.choose_cmdr = self.choose_cmdr

        def work():
            s.run(self.df)
            self.q.put(("done",))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def start_tidy(self):
        if self.csv_var.get().strip() and self.df is None:
            self.load_csv(self.csv_var.get().strip(), quiet=True)
        self.collect_cfg()
        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.tidy_btn.configure(state="disabled")
        self.cmdr_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        s = Syncer(self.cfg, self.log, self.confirm, self.pause, self.ask_file,
                   self.choose_opts, self.choose_tidy, self.stop_event)

        def work():
            s.run_tidy(self.df)
            self.q.put(("done",))
        threading.Thread(target=work, daemon=True).start()

    def start_cmdr(self):
        if self.csv_var.get().strip() and self.df is None:
            self.load_csv(self.csv_var.get().strip(), quiet=True)
        if self.df is None:
            messagebox.showerror(APP_NAME, "Choose your Manabox export CSV first.")
            return
        self.collect_cfg()
        self.stop_event.clear()
        for b in (self.start_btn, self.tidy_btn, self.cmdr_btn):
            b.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        s = Syncer(self.cfg, self.log, self.confirm, self.pause, self.ask_file,
                   self.choose_opts, self.choose_tidy, self.stop_event)
        s.choose_cmdr = self.choose_cmdr

        def work():
            s.run_commanders(self.df)
            self.q.put(("done",))
        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.log("Stopping after the current step...")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
