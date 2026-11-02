#!/usr/bin/env python3
"""
Sync your Last.fm scrobbles + loved tracks into a local SQLite file, then write an
interactive stats page (index.html + data.json). Only uses the Python standard library.

First run downloads everything (a few minutes for 40k+ scrobbles).
After that, each run only fetches scrobbles newer than the last one saved.
If the database has scrobbles without album info (saved by an older version of this
script), the next run re-downloads everything once to fill the albums in.

Usage:
    python3 lastfm_sync.py --user YOURNAME --key YOUR_API_KEY \
        --db /opt/lastfm/lastfm.db --out /var/www/my_webapp/www/lastfm/index.html --tz America/Costa_Rica

data.json is written next to index.html. --user and --key can also come from the
LASTFM_USER / LASTFM_KEY environment variables. Use --no-sync to rebuild the page
from the database without calling Last.fm, or --full to force a full re-download.

Deleted scrobbles: every sync compares your saved count with Last.fm's. If you have more,
the script re-downloads everything once and removes whatever Last.fm no longer has. A full
re-download (--full) always does this check too, and also picks up corrected track names.
To run ONLY the cleanup: --prune (add --dry-run first to preview what it would remove).
To clean up just one stretch of time (much faster): --prune 2026-09, --prune 09/2026, --prune 2026,
--prune 15/09/2026, --prune 2026-09-15, or a range like --prune 2026-09-01..2026-10-15.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

VERSION = "17"  # matches the number in the downloaded file name (lastfm_sync_v17.py)
# Last.fm API key used by the "Now playing" widget on the landing page. It runs in the visitor's browser, so
# it is visible in the page source (same as on your message board). Read-only use only.
WIDGET_API_KEY = "7c066b6ca577fbecbe59555b66b6c249"
API = "https://ws.audioscrobbler.com/2.0/"
TEMPORARY_ERRORS = {8, 11, 16, 29}  # operation failed, service offline, temporarily unavailable, rate limit


# ----------------------------------------------------------------------------
# Last.fm API
# ----------------------------------------------------------------------------
def api_get(params, retries=5):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "lastfm-stats-sync/1.0"})
    for attempt in range(retries):
        data = None
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                data = json.load(e)
            except Exception:
                data = None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            data = None
        if data and "error" in data and data["error"] not in TEMPORARY_ERRORS:
            sys.exit(f"Last.fm API error {data['error']}: {data.get('message')}")
        if data and "error" not in data:
            return data
        time.sleep(2 ** attempt)
    sys.exit("Last.fm kept failing. Try again later (progress so far is saved).")


def as_list(x):
    if x is None:
        return []
    return [x] if isinstance(x, dict) else x


def count_scrobbles(db):
    return db.execute("SELECT COUNT(*) FROM scrobbles").fetchone()[0]


def download(db, user, key, since=0, seen=None, until=None):
    """Fetch scrobbles newer than `since` (0 = everything), and older than `until` if given, into the database.
    If `seen` is a set, every scrobble Last.fm returns is added to it."""
    params = dict(method="user.getrecenttracks", user=user, api_key=key, format="json", limit=200)
    if since:
        params["from"] = since  # duplicates are handled by the primary key, so inclusive/exclusive doesn't matter
    if until:
        params["to"] = until
    before_count = count_scrobbles(db)
    page, total_pages = 1, 1
    while page <= total_pages:
        data = api_get({**params, "page": page})["recenttracks"]
        total_pages = int(data["@attr"]["totalPages"])
        rows = []
        for t in as_list(data.get("track")):
            if t.get("@attr", {}).get("nowplaying") == "true" or not t.get("date"):
                continue
            album = (t.get("album") or {}).get("#text") or ""
            row = (int(t["date"]["uts"]), t["artist"]["#text"], t["name"], album)
            rows.append(row)
            if seen is not None:
                seen.add(row[:3])
        db.executemany(
            """INSERT INTO scrobbles (uts, artist, track, album) VALUES (?,?,?,?)
               ON CONFLICT (uts, artist, track) DO UPDATE SET album = excluded.album""", rows)
        db.commit()  # save progress every page
        if page % 10 == 0 or page == total_pages:
            print(f"  scrobbles: page {page}/{total_pages}", flush=True)
        page += 1
        time.sleep(0.25)
    print(f"Added {count_scrobbles(db) - before_count:,} new scrobbles.")


def remote_playcount(user, key):
    data = api_get(dict(method="user.getinfo", user=user, api_key=key, format="json"))
    return int(data["user"]["playcount"])


def reconcile(db, seen, allow_big_delete=False, dry_run=False, window=None):
    """Delete local scrobbles that Last.fm no longer has. Only call after a COMPLETE download.
    window = (first_ts, last_ts): only scrobbles inside this stretch of time are checked."""
    lo, hi = window if window else (0, 2 ** 62)
    mine = db.execute("SELECT uts, artist, track FROM scrobbles WHERE uts BETWEEN ? AND ?", (lo, hi)).fetchall()
    local = len(mine)
    gone = [r for r in mine if r not in seen]
    if not gone:
        print("No deleted scrobbles found.")
        return
    if dry_run:
        print(f"Dry run: {len(gone):,} scrobbles no longer exist on Last.fm and would be removed, e.g.:")
        for uts, artist, track in sorted(gone)[:10]:
            print(f"  {datetime.fromtimestamp(uts).strftime('%Y-%m-%d %H:%M')}  {artist} - {track}")
        return
    if not allow_big_delete and (not seen or len(gone) > max(50, 0.2 * local)):
        print(f"Found {len(gone):,} scrobbles missing from Last.fm, which is a lot ({len(gone) / local:.0%} of "
              f"{'that period' if window else 'your history'}), so nothing was deleted. "
              f"If that's really right, rerun with --allow-big-delete.")
        return
    db.executemany("DELETE FROM scrobbles WHERE uts=? AND artist=? AND track=?", gone)
    db.commit()
    print(f"Removed {len(gone):,} scrobbles that no longer exist on Last.fm, e.g.:")
    for uts, artist, track in gone[:5]:
        print(f"  {datetime.fromtimestamp(uts).strftime('%Y-%m-%d %H:%M')}  {artist} - {track}")


def full_pass(db, user, key, allow_big_delete=False, dry_run=False, window=None):
    seen = set()
    if window:
        # Ask Last.fm for one second more on each side, so it doesn't matter whether it treats the
        # range ends as inclusive or exclusive (a scrobble right on the edge must not look "deleted").
        download(db, user, key, since=max(window[0] - 1, 1), until=window[1] + 1, seen=seen)
    else:
        download(db, user, key, since=0, seen=seen)  # exits on failure, so we only get here if it finished
    reconcile(db, seen, allow_big_delete, dry_run, window)


def parse_period(text, tz):
    """Turn a --prune period into (first_ts, last_ts), both inclusive, using day boundaries in `tz`.
    Accepts 2026 | 09/2026 | 2026-09 | 15/09/2026 | 2026-09-15 | or a range 'A..B' of any of those."""
    def one(t):
        t = t.strip()
        m = (re.fullmatch(r"(\d{4})", t) and ("y", None, None, int(t))
             or re.fullmatch(r"(\d{1,2})/(\d{4})", t) and ("m", None, int(t.split("/")[0]), int(t.split("/")[1]))
             or re.fullmatch(r"(\d{4})-(\d{1,2})", t) and ("m", None, int(t.split("-")[1]), int(t.split("-")[0]))
             or re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", t) and ("d", int(t.split("/")[0]), int(t.split("/")[1]), int(t.split("/")[2]))
             or re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t) and ("d", int(t.split("-")[2]), int(t.split("-")[1]), int(t.split("-")[0])))
        if not m:
            sys.exit(f"Can't read the period '{t}'. Use 2026, 09/2026, 2026-09, 15/09/2026, 2026-09-15, "
                     f"or a range like 2026-09-01..2026-10-15 (slashes mean day/month/year).")
        kind, day, month, year = m
        try:
            if kind == "y":
                a, b = datetime(year, 1, 1), datetime(year + 1, 1, 1)
            elif kind == "m":
                a, b = datetime(year, month, 1), datetime(year + (month == 12), month % 12 + 1, 1)
            else:
                a = datetime(year, month, day)
                b = a + timedelta(days=1)
        except ValueError:
            sys.exit(f"'{t}' isn't a real date.")
        return a.replace(tzinfo=tz), b.replace(tzinfo=tz)

    parts = text.split("..")
    if len(parts) > 2:
        sys.exit("Use at most one '..' in a range, like 2026-09-01..2026-10-15.")
    first = one(parts[0])[0]
    after_last = one(parts[-1])[1]
    if after_last <= first:
        sys.exit("The end of the range is before its start.")
    return int(first.timestamp()), int(after_last.timestamp()) - 1


def sync_scrobbles(db, user, key, full=False, allow_big_delete=False):
    missing_albums = db.execute("SELECT 1 FROM scrobbles WHERE album IS NULL LIMIT 1").fetchone()
    if full or missing_albums:
        if missing_albums:
            print("Filling in album names for existing scrobbles (one-time, takes a few minutes)...")
        full_pass(db, user, key, allow_big_delete)
        return

    since = db.execute("SELECT MAX(uts) FROM scrobbles").fetchone()[0] or 0
    download(db, user, key, since=since)

    # Deleted scrobbles can't be seen in an incremental sync, but the totals give them away.
    local, remote = count_scrobbles(db), remote_playcount(user, key)
    if local > remote:
        print(f"Database has {local - remote:,} more scrobbles than Last.fm ({local:,} vs {remote:,}); checking for deletions...")
        full_pass(db, user, key, allow_big_delete)


def sync_loved(db, user, key):
    loved, page, total_pages = [], 1, 1
    while page <= total_pages:
        data = api_get(dict(method="user.getlovedtracks", user=user, api_key=key,
                            format="json", limit=1000, page=page))["lovedtracks"]
        total_pages = int(data["@attr"]["totalPages"])
        for t in as_list(data.get("track")):
            loved.append((t["artist"]["name"], t["name"]))
        page += 1
        time.sleep(0.25)
    with db:  # replace the whole list so un-loved tracks disappear too
        db.execute("DELETE FROM loved")
        db.executemany("INSERT OR IGNORE INTO loved VALUES (?,?)", loved)
    print(f"Loved tracks: {len(loved):,}.")


# ----------------------------------------------------------------------------
# data.json  (the page does all the sorting/filtering in the browser)
# ----------------------------------------------------------------------------
def norm(s):
    return " ".join(str(s).casefold().split())


def build_data(db, user, tz_name, updated):
    artists, a_idx = [], {}
    tracks, t_idx = [], {}   # [artistIdx, title]
    albums, al_idx = [], {}  # [artistIdx, title]
    ts_deltas, tr_seq, al_seq = [], [], []
    last_ts = 0

    def artist_id(name):
        k = norm(name)
        if k not in a_idx:
            a_idx[k] = len(artists)
            artists.append(name)
        return a_idx[k]

    for uts, artist, track, album in db.execute(
            "SELECT uts, artist, track, album FROM scrobbles ORDER BY uts"):
        a = artist_id(artist)
        tk = (a, norm(track))
        if tk not in t_idx:
            t_idx[tk] = len(tracks)
            tracks.append([a, track])
        al = -1
        if album and norm(album):
            ak = (a, norm(album))
            if ak not in al_idx:
                al_idx[ak] = len(albums)
                albums.append([a, album])
            al = al_idx[ak]
        ts_deltas.append(uts - last_ts)  # first value is absolute, the rest are gaps
        last_ts = uts
        tr_seq.append(t_idx[tk])
        al_seq.append(al)

    loved_rows = db.execute("SELECT artist, track FROM loved").fetchall()
    loved_idx, seen = [], set()
    for artist, track in loved_rows:
        i = t_idx.get((a_idx.get(norm(artist), -1), norm(track)))
        if i is not None and i not in seen:
            seen.add(i)
            loved_idx.append(i)

    return {
        "user": user, "updated": updated, "tz": tz_name, "total": len(ts_deltas),
        "loved_total": len(loved_rows), "artists": artists, "tracks": tracks, "albums": albums,
        "s": {"t": ts_deltas, "k": tr_seq, "a": al_seq}, "loved": loved_idx,
    }


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)  # atomic, so visitors never see a half-written file
    os.chmod(path, 0o644)


# ----------------------------------------------------------------------------
# The page: plain HTML (teal background, white text). Reads data.json and does
# all the calculating in the browser, only for the stat you open.
# ----------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Last.fm stats</title>
<style>
/* The only styling on the page: the "Now playing" card. Everything else stays plain. */
#lastfm-widget {
  max-width: 540px; margin: 1.2rem 0; padding: 8px;
  border: 1px solid #000; overflow: auto;
}
#lastfm-widget[hidden], #lastfm-widget [hidden] { display: none !important; }
#lastfm-cover { float: left; width: 200px; height: 200px; object-fit: cover; margin: 0 12px 0 0; }
#lastfm-info { overflow: hidden; padding-top: 2px; }
#lastfm-track { overflow-wrap: anywhere; }
#lastfm-playcounts { margin-top: 10px; }
#lastfm-playcounts div { margin-top: 2px; }
@media (max-width: 520px) { #lastfm-cover { float: none; display: block; margin: 0 auto 14px; } }
</style>
</head>
<body bgcolor="teal" text="white" link="white" vlink="white" alink="white">
<h1 id="title">Last.fm stats</h1>
<p id="meta">Loading…</p>

<p>
<a href="#unplayed">Most played, not heard in a month</a> |
<a href="#span">Most plays in a span of time</a> |
<a href="#streaks">Streaks</a> |
<a href="#patterns">Listening patterns</a> |
<a href="#loved">Loved tracks: days per play</a>
<br>
By month:
<a href="#firstlast">First and last scrobble</a> |
<a href="#unique">Unique tracks, albums and artists</a> |
<a href="#permonth">Scrobbles per month</a>
<br>
Busiest:
<a href="#days">Days</a> |
<a href="#spans">Spans of time</a>
<br>
Charts:
<a href="#top">Top artists, albums and tracks</a>
<br>
Fun stats:
<a href="#sessions">Listening sessions</a> |
<a href="#rhythm">When you listen</a> |
<a href="#discovery">Discovery</a> |
<a href="#records">Records</a> |
<a href="#comebacks">Comebacks</a> |
<a href="#burst">Binge or steady?</a> |
<a href="#diversity">Diversity &amp; power laws</a> |
<a href="#taste">Taste drift</a> |
<a href="#charts">Charts</a>
<br>
<a href="#now">Now playing</a>
</p>
<hr>
<p id="pick">Pick a stat above.</p>

<div id="lastfm-widget" hidden>
<img id="lastfm-cover" alt="Album cover" width="200" hidden>
<div id="lastfm-info">
<div id="lastfm-status">Loading...</div>
<div id="lastfm-track"></div>
<div id="lastfm-artist"></div>
<div id="lastfm-album"></div>
<div id="lastfm-playcounts">
<div id="lastfm-artist-plays"></div>
<div id="lastfm-album-plays"></div>
<div id="lastfm-track-plays"></div>
<div id="lastfm-last-played"></div>
</div>
</div>
</div>

<p id="common" hidden>
Rows <select id="top"><option>10</option><option>25</option><option selected>50</option><option>100</option><option>250</option><option value="0">All</option></select>
Search <input id="q" type="search" placeholder="artist, album or track">
<br>
Only scrobbles from <input id="d_from" type="date"> to <input id="d_to" type="date">
<button type="button" id="d_clear">All time</button>
<span id="d_note"></span>
<br>
Tip: click a column header to sort (click again to reverse). Click # to go back to the default order.
</p>

<div id="s_unplayed" hidden>
<h2>Most played, not heard in a month</h2>
<p>
Type <select id="u_type"><option value="track">Track</option><option value="artist">Artist</option><option value="album">Album</option></select>
Month <select id="month"></select>
Min total plays <input id="u_min" type="number" min="1" value="1">
</p>
<div id="t_unplayed"></div>
</div>

<div id="s_span" hidden>
<h2>Most plays in a span of time</h2>
<p>
Type <select id="b_type"><option value="track">Track</option><option value="artist">Artist</option><option value="album">Album</option></select>
Span (days) <input id="b_days" type="number" min="0.5" step="0.5" value="7">
Show <select id="b_show"><option value="best">Best span per item</option><option value="all">Every span (no overlaps)</option></select>
Min plays in the span <input id="b_min" type="number" min="1" value="2">
&nbsp;|&nbsp; When sorting "Plays" ↑ (smallest first), hide rows ≤ <input id="b_floor" type="number" min="1" value="2">
</p>
<p id="b_note"></p>
<div id="t_span"></div>
</div>

<div id="s_streaks" hidden>
<h2>Streaks</h2>
<p>
Type <select id="k_type"><option value="track">Track</option><option value="artist">Artist</option><option value="album">Album</option></select>
Kind <select id="k_kind"><option value="row">In a row (back-to-back plays)</option><option value="days">Consecutive days</option></select>
Show <select id="k_show"><option value="best">Best streak per item</option><option value="all">Every streak</option></select>
Min streak <input id="k_min" type="number" min="2" value="2">
&nbsp;|&nbsp; When sorting the streak length ↑ (smallest first), hide rows ≤ <input id="k_floor" type="number" min="2" value="2">
</p>
<p id="k_note"></p>
<div id="k_sel" hidden>
<p>
<b id="k_count">0 selected</b>
<button type="button" id="k_all">Select all shown</button>
<button type="button" id="k_none">Clear</button>
<button type="button" id="k_csv">Download CSV</button>
<a id="k_lib" href="https://www.last.fm" target="_blank" rel="noopener">Open my Last.fm library</a>
</p>
<ol id="k_links"></ol>
</div>
<div id="t_streaks"></div>
</div>

<div id="s_patterns" hidden>
<h2>Listening patterns</h2>
<p>Totals can hide whether you replay something on a few days or return to it consistently over a long stretch. These measures describe the selected date range: active span includes both the first and last calendar day played; days heard counts distinct calendar days.</p>
<p>
Type <select id="p_type"><option value="track">Track</option><option value="artist">Artist</option><option value="album">Album</option></select>
Min plays <input id="p_min" type="number" min="1" value="5">
</p>
<div id="t_patterns"></div>
</div>

<div id="s_loved" hidden>
<h2>Loved tracks: days per play</h2>
<p id="l_note"></p>
<p>
Order <select id="l_order"><option value="low">Fewest days per play</option><option value="high">Most days per play (neglected)</option></select>
Min days since first play <input id="l_days" type="number" min="0" step="1" value="1">
Min plays <input id="l_min" type="number" min="1" value="1">
<br>
<label><input type="checkbox" id="l_x" checked> Extra columns</label>
Days/play counting from play # <input id="l_from" type="number" min="2" value="3">
</p>
<p>The extra columns help when a track grew on you after you first played it: days/play from play # starts the clock at your Nth play instead of your first, the median gap ignores long quiet stretches, and the peak columns show your hottest 30 days with it and when that began.</p>
<div id="t_loved"></div>
</div>

<div id="s_firstlast" hidden>
<h2>First and last scrobble of each month</h2>
<p>The search box matches either song.</p>
<div id="t_firstlast"></div>
</div>

<div id="s_unique" hidden>
<h2>Unique tracks, albums and artists per month</h2>
<p id="n_best"></p>
<p>Unique means different ones played at least once that month. The All-time row is your entire Last.fm history, even when a date filter is active. “Scrobbles / …” is the average number of plays per unique item.</p>
<div id="t_unique"></div>
</div>

<div id="s_permonth" hidden>
<h2>Scrobbles per month</h2>
<p id="m_note"></p>
<div id="t_permonth"></div>
</div>

<div id="s_days" hidden>
<h2>Busiest days</h2>
<p id="d_best"></p>
<p>Every day with scrobbles. Unique means different ones played at least once that day. Click a column header to rank days by that. The search box doesn't apply here.</p>
<div id="t_days"></div>
</div>

<div id="s_spans" hidden>
<h2>Busiest spans of time</h2>
<p>
Span (days) <input id="w_days" type="number" min="0.5" step="0.5" value="7">
Measure <select id="w_measure"><option value="n">Scrobbles</option><option value="track">Unique tracks</option><option value="album">Unique albums</option><option value="artist">Unique artists</option></select>
Show <select id="w_show"><option value="best">Best span</option><option value="all">Every span (no overlaps)</option></select>
Min value <input id="w_min" type="number" min="1" value="1">
&nbsp;|&nbsp; When sorting the chosen measure ↑ (smallest first), hide rows ≤ <input id="w_floor" type="number" min="1" value="2">
</p>
<p id="w_best"></p>
<p id="w_note"></p>
<div id="t_spans"></div>
</div>

<div id="s_top" hidden>
<h2>Top artists, albums and tracks</h2>
<p>
Show <select id="c_type"><option value="artist">Artists</option><option value="album">Albums</option><option value="track">Tracks</option></select>
<label><input type="checkbox" id="c_bb"> Billboard mode (compare with the previous period)</label>
<label><input type="checkbox" id="c_cal"> Calendar month or year</label>
<span id="c_calbox" hidden>
<select id="c_unit"><option value="month">Month</option><option value="year">Year</option></select>
<select id="c_period"></select>
</span>
</p>
<p id="c_note"></p>
<div id="t_top"></div>
</div>

<div id="s_sessions" hidden>
<h2>Listening sessions</h2>
<p>A session is a run of scrobbles with no gap longer than <input id="se_gap" type="number" min="1" value="30"> minutes between them. Duration is first scrobble to last (the last track's own length isn't known). The search box matches the top artist.</p>
<p id="se_note"></p>
<div id="t_sessions"></div>
</div>

<div id="s_rhythm" hidden>
<h2>When you listen</h2>
<p>Scrobbles by hour of the day and day of the week, in the page's timezone.</p>
<p id="r_note"></p>
<div id="t_rhythm"></div>
</div>

<div id="s_discovery" hidden>
<h2>Discovery: new music vs. old favorites</h2>
<p>"New" means the first time you ever scrobbled that artist, album or track. Your whole history is checked, not just the date range, so something you heard years ago isn't counted as new.</p>
<p id="v_note"></p>
<div id="t_discovery"></div>
</div>

<div id="s_records" hidden>
<h2>Records and fun facts</h2>
<p>Calculated for the selected date range.</p>
<div id="t_records"></div>
</div>

<div id="s_comebacks" hidden>
<h2>Comebacks and sleeper hits</h2>
<p>Things you stopped playing for a long stretch and then came back to. "Plays after" shows whether the comeback turned into a real obsession, and days/play since coming back measures it from the day you returned instead of the first time you ever heard it.</p>
<p>
Type <select id="m_type"><option value="track">Track</option><option value="artist">Artist</option><option value="album">Album</option></select>
Min gap (days) <input id="m_gap" type="number" min="1" value="180">
Min plays after coming back <input id="m_after" type="number" min="1" value="5">
</p>
<div id="t_comebacks"></div>
</div>

<div id="s_burst" hidden>
<h2>Binge or steady?</h2>
<p>Burstiness looks at the gaps between plays. -1 means perfectly regular, 0 means random, and +1 means extreme binges (Goh-Barabasi burstiness, B = (SD - mean) / (SD + mean) of the gaps). Verdict: +0.25 or more is binge-y, -0.25 or less is steady, in between is mixed. The binge share is how much of its plays fell inside its single busiest window.</p>
<p>
Type <select id="g_type"><option value="track">Track</option><option value="artist">Artist</option><option value="album">Album</option></select>
Min plays <input id="g_min" type="number" min="3" value="10">
Peak window (days) <input id="g_days" type="number" min="1" value="30">
&nbsp;|&nbsp; When sorting "Plays" ↑ (smallest first), hide rows ≤ <input id="g_floor" type="number" min="1" value="3">
</p>
<div id="t_burst"></div>
</div>

<div id="s_diversity" hidden>
<h2>Diversity and power laws</h2>
<p>These measures describe how evenly your plays are spread across artists, albums and tracks. High diversity means many items each played a similar amount; low diversity means a few obsessions dominate.</p>
<p>
Type <select id="dv_type"><option value="artist">Artist</option><option value="album">Album</option><option value="track">Track</option></select>
Min plays per item <input id="dv_min" type="number" min="1" value="1">
</p>
<div id="dv_summary"></div>
<div id="t_diversity"></div>
</div>

<div id="s_taste" hidden>
<h2>Taste drift: how your top items change month to month</h2>
<p>For each pair of consecutive months, this measures how similar your top-N lists are (rank correlation) and how much of this month's listening was to items you'd never played before this month.</p>
<p>
Type <select id="td_type"><option value="artist">Artist</option><option value="album">Album</option><option value="track">Track</option></select>
Top N for overlap <input id="td_n" type="number" min="1" max="100" value="20">
</p>
<div id="td_note"></div>
<div id="t_taste"></div>
</div>

<div id="s_charts" hidden>
<h2>Charts</h2>
<p>Pick what to plot on the bars, how to group scrobbles into bars, and how to measure each bar. Bars use the same <code>█</code> characters as “When you listen”, so the page stays plain HTML — nothing is loaded from anywhere else.</p>

<p><b>What</b><br>
Bars are <select id="ch_dim">
  <option value="hour">Hour of day (0–23)</option>
  <option value="weekday">Day of week (Mon–Sun)</option>
  <option value="day">Day</option>
  <option value="week">Week (Mon start)</option>
  <option value="month" selected>Month</option>
  <option value="year">Year</option>
  <option value="artist">Artist</option>
  <option value="album">Album</option>
  <option value="track">Track</option>
</select>
Measure <select id="ch_measure">
  <option value="n" selected>Scrobbles</option>
  <option value="utrack">Unique tracks</option>
  <option value="ualbum">Unique albums</option>
  <option value="uartist">Unique artists</option>
  <option value="minutes">Minutes (see note)</option>
</select>
</p>

<p><b>When</b> (this range is <i>independent</i> of the From/To at the top of every page)<br>
Range <select id="ch_range">
  <option value="all" selected>All time</option>
  <option value="global">Same as the page's From/To</option>
  <option value="last7">Last 7 days</option>
  <option value="last30">Last 30 days</option>
  <option value="last90">Last 90 days</option>
  <option value="last365">Last 365 days</option>
  <option value="year">A calendar year…</option>
  <option value="custom">Custom from–to…</option>
</select>
<span id="ch_range_year" hidden>Year <input id="ch_year" type="number" min="2000" max="2100" value="2025"></span>
<span id="ch_range_custom" hidden>
  From <input id="ch_from" type="date"> to <input id="ch_to" type="date">
</span>
</p>

<p><b>Filter</b> (matches artist / album / track names, or time-bucket labels)<br>
Search <input id="ch_q" type="search" placeholder="artist, album or track">
</p>

<p><b>Order and size</b><br>
<label><input type="checkbox" id="ch_sort" checked> Sort by value</label>
<select id="ch_dir">
  <option value="desc" selected>Biggest first</option>
  <option value="asc">Smallest first</option>
</select>
<label><input type="checkbox" id="ch_cum"> Cumulative (running total)</label>
<label><input type="checkbox" id="ch_log"> Log scale (bars = log(value + 1))</label>
<br>
Show at most <input id="ch_top" type="number" min="1" max="500" value="24"> bars
&nbsp;|&nbsp; Bar width <input id="ch_width" type="number" min="4" max="200" value="40"> characters
&nbsp;|&nbsp; Scale
<select id="ch_scale_mode">
  <option value="auto" selected>Auto (biggest bar = full width)</option>
  <option value="manual">Manual max…</option>
</select>
<span id="ch_scale_manual" hidden>Max <input id="ch_scale_max" type="number" min="1" step="1" value="100"></span>
</p>

<p><b>Layout</b><br>
<label><input type="checkbox" id="ch_wide"> Wide bars (label above the bar, one full-width row each)</label>
</p>

<p id="ch_note"></p>
<div id="t_charts"></div>
</div>

<script>
(async function () {
  const $ = id => document.getElementById(id);
  let D;
  try {
    const r = await fetch('data.json', { cache: 'no-cache' });
    D = await r.json();
  } catch (e) { $('meta').textContent = 'Could not load data.json — has the sync script run yet?'; return; }

  // ---------- time helpers ----------
  const tz = D.tz || Intl.DateTimeFormat().resolvedOptions().timeZone;
  const monthFmt = new Intl.DateTimeFormat('en-US', { timeZone: tz, year: 'numeric', month: '2-digit' });
  const dtFmt = new Intl.DateTimeFormat('sv-SE', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const monthKey = ts => {
    let y = '', m = '';
    for (const p of monthFmt.formatToParts(new Date(ts * 1000))) { if (p.type === 'year') y = p.value; else if (p.type === 'month') m = p.value; }
    return y + '-' + m;
  };
  const fmtDT = ts => dtFmt.format(new Date(ts * 1000));
  const fmtD = ts => fmtDT(ts).slice(0, 10);
  const fmtDay = idx => new Date(idx * 86400000).toISOString().slice(0, 10);
  const monthName = k => { const [y, m] = k.split('-'); return new Date(Date.UTC(+y, +m - 1, 1)).toLocaleString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' }); };
  const dayIndex = ts => { const s = dtFmt.format(new Date(ts * 1000)); return Math.round(Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) / 86400000); };

  // ---------- build artist / album / track lists ----------
  const N = D.s.t.length;
  const ts = new Array(N);
  { let acc = 0; for (let i = 0; i < N; i++) { acc += D.s.t[i]; ts[i] = acc; } }
  const seqTrack = D.s.k, seqAlbum = D.s.a;
  const seqArtist = seqTrack.map(k => D.tracks[k][0]);
  const seq = { artist: seqArtist, track: seqTrack, album: seqAlbum };
  const mk = ts.map(monthKey);

  const mkEnt = (name, sub) => ({ name, sub, ts: [], months: new Set() });
  const dayCache = {};
  const patternCache = {};
  let E = null;
  let lo = 0, hi = N;      // only scrobbles lo..hi-1 are analyzed (the chosen date range)
  let rangeEnd = null;     // end of the chosen range as a timestamp (null = no end date)
  function build() {
    E = {
      artist: D.artists.map(n => mkEnt(n, '')),
      track: D.tracks.map(([a, t]) => mkEnt(t, D.artists[a])),
      album: D.albums.map(([a, t]) => mkEnt(t, D.artists[a])),
    };
    for (let i = lo; i < hi; i++) {
      for (const type of ['artist', 'track', 'album']) {
        const id = seq[type][i];
        if (id < 0) continue;
        const e = E[type][id];
        e.ts.push(ts[i]);
        e.months.add(mk[i]);
      }
    }
    for (const k in dayCache) delete dayCache[k];
    for (const k in patternCache) delete patternCache[k];
  }

  // ---------- date range ----------
  let dayIdx = null;
  const getDayIdx = () => dayIdx || (dayIdx = ts.map(dayIndex));
  const dateToIdx = str => Date.UTC(+str.slice(0, 4), +str.slice(5, 7) - 1, +str.slice(8, 10)) / 86400000;
  function lowerBound(arr, x) { let a = 0, b = arr.length; while (a < b) { const m = (a + b) >> 1; if (arr[m] < x) a = m + 1; else b = m; } return a; }
  function tzMidnight(idx) {   // timestamp of 00:00 on that day in the chosen timezone
    const g = idx * 86400;
    const s = dtFmt.format(new Date(g * 1000));
    const local = Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10), +s.slice(11, 13), +s.slice(14, 16)) / 1000;
    return g - (local - g);
  }
  function applyRange() {
    let f = $('d_from').value, t = $('d_to').value;     // 'YYYY-MM-DD' or ''
    if (f && t && f > t) [f, t] = [t, f];                // dates the wrong way round: swap them
    const di = getDayIdx();
    const fi = f ? dateToIdx(f) : -Infinity, ti = t ? dateToIdx(t) : Infinity;
    lo = lowerBound(di, fi);
    hi = Math.max(lo, lowerBound(di, ti + 1));
    rangeEnd = t ? Math.min(Date.now() / 1000, tzMidnight(ti + 1)) : null;
    build();
    $('d_note').textContent = (f || t)
      ? (hi - lo).toLocaleString() + ' of ' + N.toLocaleString() + ' scrobbles in this range'
      : '';
  }

  const LABEL = { artist: 'Artist', album: 'Album', track: 'Track' };

  // ---------- the stats ----------
  function unplayed(type, month, minPlays) {
    return E[type].filter(e => e.ts.length >= minPlays && !e.months.has(month))
      .sort((x, y) => y.ts.length - x.ts.length).map(e => ({ e }));
  }

  function bestWindow(list, span) {
    let best = [0, 0, 0], j = 0; const n = list.length;
    for (let i = 0; i < n; i++) {
      if (j < i) j = i;
      while (j < n && list[j] < list[i] + span) j++;
      if (j - i > best[0]) best = [j - i, list[i], list[j - 1]];
    }
    return best;
  }
  // Every separate busy span for ONE item: take its busiest window, then look for more windows wholly
  // before it and wholly after it (and so on), so spans for the same item never overlap or share a play.
  function spansAll(list, sp, minPlays) {
    const found = [], stack = [[0, list.length]];
    while (stack.length) {
      const [a, b] = stack.pop();
      if (b - a < minPlays) continue;
      let bestN = 0, bestI = a, bestJ = a, j = a;
      for (let i = a; i < b; i++) {
        if (j < i) j = i;
        while (j < b && list[j] < list[i] + sp) j++;
        if (j - i > bestN) { bestN = j - i; bestI = i; bestJ = j; }
      }
      if (bestN < minPlays) continue;
      found.push([bestN, list[bestI], list[bestJ - 1]]);
      stack.push([a, bestI], [bestJ, b]);
    }
    return found;
  }
  function span(type, days, minPlays, all) {
    const sp = days * 86400, out = [];
    for (const e of E[type]) {
      if (e.ts.length < minPlays) continue;
      if (all) {
        for (const [c, s, t] of spansAll(e.ts, sp, minPlays)) out.push({ e, c, s, t });
      } else {
        const [c, s, t] = bestWindow(e.ts, sp);
        if (c >= minPlays) out.push({ e, c, s, t });
      }
    }
    return out.sort((x, y) => y.c - x.c || y.e.ts.length - x.e.ts.length || x.s - y.s);
  }

  // longest run of back-to-back scrobbles of the same thing (one best run per artist/album/track)
  function streaksInRow(type, minLen, all) {
    const s = seq[type], best = new Map(), out = [];
    let i = lo;
    while (i < hi) {
      const id = s[i];
      let j = i + 1;
      if (id >= 0) {
        while (j < hi && s[j] === id) j++;
        const len = j - i;
        if (len >= minLen) {
          // second = index of the 2nd scrobble of the run (the one you'd delete as a duplicate)
          const item = { e: E[type][id], len, from: fmtDT(ts[i]), to: fmtDT(ts[j - 1]), fts: ts[i], tts: ts[j - 1], second: i + 1 };
          if (all) out.push(item);
          else { const b = best.get(id); if (!b || len > b.len) best.set(id, item); }
        }
      }
      i = j;
    }
    return (all ? out : [...best.values()]).sort((x, y) => y.len - x.len);
  }

  // longest run of consecutive calendar days with at least one play
  function dayLists(type) {
    if (dayCache[type]) return dayCache[type];
    const di = getDayIdx();
    const L = E[type].map(() => []), s = seq[type];
    for (let i = lo; i < hi; i++) {
      const id = s[i];
      if (id < 0) continue;
      const l = L[id];
      if (l[l.length - 1] !== di[i]) l.push(di[i]);
    }
    return (dayCache[type] = L);
  }
  // Find every separate run of consecutive calendar days for each item.
  // Each run is emitted once; runs for the same item are disjoint by construction.
  function streaksDays(type, minLen, all) {
    const L = dayLists(type), out = [];
    L.forEach((days, id) => {
      let best = null, start = 0;
      for (let i = 1; i <= days.length; i++) {
        if (i === days.length || days[i] !== days[i - 1] + 1) {
          const len = i - start;
          if (len >= minLen) {
            const item = {
              e: E[type][id], len,
              from: fmtDay(days[start]), to: fmtDay(days[i - 1]),
              fts: days[start], tts: days[i - 1]
            };
            if (all) out.push(item);
            else if (!best || len > best.len) best = item;
          }
          start = i;
        }
      }
      if (!all && best) out.push(best);
    });
    return out.sort((x, y) => y.len - x.len || x.fts - y.fts || x.e.name.localeCompare(y.e.name));
  }

  function loved(order, minDays, minPlays) {
    const now = rangeEnd === null ? Date.now() / 1000 : rangeEnd, out = [];
    for (const i of D.loved) {
      const e = E.track[i], n = e.ts.length;
      if (n < minPlays) continue;
      const days = Math.max((now - e.ts[0]) / 86400, minDays);
      out.push({ e, n, days, ratio: days / n, end: now, minDays });
    }
    return out.sort(order === 'low' ? (x, y) => x.ratio - y.ratio || y.n - x.n : (x, y) => y.ratio - x.ratio || x.n - y.n);
  }

  function listeningPatterns(type, minPlays) {
    if (!patternCache[type]) {
      const now = rangeEnd === null ? Date.now() / 1000 : rangeEnd;
      patternCache[type] = E[type].map(e => {
        const plays = e.ts.length;
        if (!plays) return null;
        let firstDay = Infinity, lastDay = -Infinity, daysHeard = 0, prevDay = null;
        for (const t of e.ts) {
          const d = dayIndex(t);
          firstDay = Math.min(firstDay, d);
          lastDay = Math.max(lastDay, d);
          if (d !== prevDay) { daysHeard++; prevDay = d; }
        }
        const activeDays = lastDay - firstDay + 1;
        return {
          e, plays, daysHeard, activeDays,
          playsPerHeardDay: plays / daysHeard,
          coverage: daysHeard / activeDays,
          daysPerPlay: activeDays / plays,
          lastPlayed: e.ts[plays - 1],
          daysSinceLast: Math.max(0, (now - e.ts[plays - 1]) / 86400),
        };
      });
    }
    return patternCache[type].filter(x => x && x.plays >= minPlays);
  }

  // ---------- rendering (tables with click-to-sort headers) ----------
  const num = (id, fallback, min) => { const v = parseFloat($(id).value); return Number.isFinite(v) ? Math.max(v, min) : fallback; };
  const sortBy = {};   // table name -> { col: column index, dir: 1 (ascending) or -1 (descending) }

  // Sorts the WHOLE list (before the Rows limit is applied), so "newest first" really shows the newest.
  function sortedList(key, cols, list) {
    const st = sortBy[key], c = st && cols[st.col];
    if (!c || !c.v) return list;
    return [...list].sort((x, y) => {
      const a = c.v(x), b = c.v(y);
      const r = typeof a === 'number' && typeof b === 'number' ? a - b
        : String(a).localeCompare(String(b), undefined, { sensitivity: 'base', numeric: true });  // numeric: "Track 2" before "Track 10"
      return r * st.dir;
    });
  }

  // cols: [{ h: header, v: value to sort by, s: optional text to show, dir0: -1 if the first click should go biggest/newest first }]
  // opts.before / opts.after: { h, make(tr, item, rowNumber) } adds an extra cell at the start / end of every row.
  function renderTable(el, key, cols, list, opts) {
    opts = opts || {};
    const t = document.createElement('table');
    t.border = 1; t.cellSpacing = 0; t.cellPadding = 4;
    const hr = t.createTHead().insertRow();
    const th0 = (text) => { const th = document.createElement('th'); th.textContent = text; hr.appendChild(th); return th; };
    if (opts.before) th0(opts.before.h || '');
    cols.forEach((c, idx) => {
      const st = sortBy[key];
      const th = th0(c.h + (st && st.col === idx ? (st.dir === 1 ? ' ▲' : ' ▼') : ''));
      if (c.v) {
        th.title = 'Click to sort';
        th.addEventListener('click', () => {
          const cur = sortBy[key];
          sortBy[key] = cur && cur.col === idx ? { col: idx, dir: -cur.dir } : { col: idx, dir: c.dir0 || 1 };
          refresh();
        });
      } else if (c.h === '#') {
        th.title = 'Click to go back to the default order';
        th.addEventListener('click', () => { delete sortBy[key]; refresh(); });
      }
    });
    if (opts.after) th0(opts.after.h || '');
    const tb = t.createTBody();
    list.forEach((x, i) => {
      const tr = tb.insertRow();
      if (opts.before) opts.before.make(tr, x, i);
      cols.forEach(c => { tr.insertCell().textContent = c.h === '#' ? i + 1 : (c.s ? c.s(x) : c.v(x)); });
      if (opts.after) opts.after.make(tr, x, i);
    });
    el.replaceChildren(t);
    return t;
  }

  const entCols = type => type === 'artist'
    ? [{ h: 'Artist', v: x => x.e.name }]
    : [{ h: 'Artist', v: x => x.e.sub }, { h: LABEL[type], v: x => x.e.name }];
  const unplayedCols = type => [{ h: '#' }, { h: 'Plays', v: x => x.e.ts.length, dir0: -1 }, ...entCols(type)];
  const spanCols = type => [{ h: '#' }, { h: 'Plays', v: x => x.c, dir0: -1 },
    { h: 'First → last play in span', v: x => x.s, s: x => fmtDT(x.s) + ' → ' + fmtDT(x.t), dir0: -1 }, ...entCols(type)];
  const streakCols = (kind, type) => [{ h: '#' }, { h: kind === 'row' ? 'Plays in a row' : 'Days in a row', v: x => x.len, dir0: -1 },
    { h: 'From', v: x => x.fts, s: x => x.from, dir0: -1 }, { h: 'To', v: x => x.tts, s: x => x.to, dir0: -1 }, ...entCols(type)];
  const lovedCols = () => [{ h: '#' }, { h: 'Days/play', v: x => x.ratio, s: x => x.ratio.toFixed(2) },
    { h: 'Plays', v: x => x.n, dir0: -1 }, { h: 'Days', v: x => x.days, s: x => Math.round(x.days), dir0: -1 },
    { h: 'First played', v: x => x.e.ts[0], s: x => fmtD(x.e.ts[0]), dir0: -1 },
    { h: 'Artist', v: x => x.e.sub }, { h: 'Track', v: x => x.e.name },
    ...($('l_x').checked ? lovedExtra() : [])];
  const patternCols = type => [{ h: '#' }, { h: 'Plays', v: x => x.plays, dir0: -1 },
    { h: 'Days heard', v: x => x.daysHeard, dir0: -1 },
    { h: 'Active span (days)', v: x => x.activeDays, dir0: -1 },
    { h: 'Plays / day heard', v: x => x.playsPerHeardDay, s: x => x.playsPerHeardDay.toFixed(2), dir0: -1 },
    { h: 'Days heard / active span', v: x => x.coverage, s: x => (100 * x.coverage).toFixed(1) + '%', dir0: -1 },
    { h: 'Active-span days / play', v: x => x.daysPerPlay, s: x => x.daysPerPlay.toFixed(2) },
    { h: 'Last played', v: x => x.lastPlayed, s: x => fmtD(x.lastPlayed), dir0: -1 },
    ...entCols(type)];

  // ---------- streak selection (click rows, shift-click for a range) ----------
  const picked = new Set();   // scrobble indexes of the 2nd scrobble of each selected streak
  let shownRows = [];
  let lastIdx = null;

  // Last.fm's own page for a track on one day, e.g.
  // https://www.last.fm/user/NAME/library/music/Stereolab/_/Italian+Shoes+Continuum?from=2026-10-10&rangetype=1day
  // (spaces become +, date is YYYY-MM-DD). You can delete the scrobble from that page.
  const lfmEnc = s => encodeURIComponent(s).replace(/%20/g, '+').replace(/[!'()*]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
  const lfmUrl = i => 'https://www.last.fm/user/' + encodeURIComponent(D.user) + '/library/music/' +
    lfmEnc(D.artists[seqArtist[i]]) + '/_/' + lfmEnc(D.tracks[seqTrack[i]][1]) + '?from=' + fmtD(ts[i]) + '&rangetype=1day';

  const pickedInfo = () => [...picked].sort((a, b) => a - b).map(i => ({
    when: fmtDT(ts[i]), unix: ts[i], artist: D.artists[seqArtist[i]], track: D.tracks[seqTrack[i]][1],
    album: seqAlbum[i] >= 0 ? D.albums[seqAlbum[i]][1] : '', url: lfmUrl(i),
  }));
  function csvText() {
    const q = v => '"' + String(v).replace(/"/g, '""') + '"';
    return ['when,unix,artist,track,album,lastfm_url', ...pickedInfo().map(r => [r.when, r.unix, r.artist, r.track, r.album, r.url].map(q).join(','))].join('\n') + '\n';
  }
  function updateSel() {
    $('k_count').textContent = picked.size + ' selected';
    $('k_links').replaceChildren(...pickedInfo().map(r => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = r.url; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = r.when + ' - ' + r.artist + ' - ' + r.track;
      li.appendChild(a);
      return li;
    }));
  }
  function renderStreaks(key, cols, list, kind) {
    const selectable = kind === 'row';
    $('k_sel').hidden = !selectable;
    shownRows = []; lastIdx = null;
    const opts = selectable ? {
      before: {
        h: '',
        make: (tr, x, idx) => {
          const cb = document.createElement('input');
          cb.type = 'checkbox'; cb.checked = picked.has(x.second);
          tr.insertCell().appendChild(cb);
          tr.cb = cb; tr.key = x.second;
          shownRows[idx] = tr;
          tr.addEventListener('click', ev => {
            if (ev.target !== cb) cb.checked = !cb.checked;  // a click on the checkbox itself already toggled it
            const on = cb.checked;
            const from = ev.shiftKey && lastIdx !== null ? Math.min(lastIdx, idx) : idx;
            const to = ev.shiftKey && lastIdx !== null ? Math.max(lastIdx, idx) : idx;
            for (let k = from; k <= to; k++) {
              shownRows[k].cb.checked = on;
              if (on) picked.add(shownRows[k].key); else picked.delete(shownRows[k].key);
            }
            lastIdx = idx;
            updateSel();
          });
        },
      },
      after: {
        h: 'Last.fm',
        make: (tr, x) => {   // link to the track's page on Last.fm for that day, where the scrobble can be deleted
          const a = document.createElement('a');
          a.href = lfmUrl(x.second); a.textContent = 'open'; a.target = '_blank'; a.rel = 'noopener';
          a.addEventListener('click', ev => ev.stopPropagation());  // don't toggle the row's selection
          tr.insertCell().appendChild(a);
        },
      },
    } : {};
    const t = renderTable($('t_streaks'), key, cols, list, opts);
    t.addEventListener('mousedown', ev => { if (ev.shiftKey) ev.preventDefault(); });  // no text-selecting while shift-clicking
    updateSel();
  }

  // ---------- per-month stats ----------
  const songText = i => D.artists[seqArtist[i]] + ' - ' + D.tracks[seqTrack[i]][1];
  const ymKey = ym => Math.floor(ym / 12) + '-' + String(ym % 12 + 1).padStart(2, '0');

  // one entry per month that has scrobbles in the chosen date range, oldest first
  function months() {
    const byKey = new Map();
    for (let i = lo; i < hi; i++) {
      const k = mk[i];
      let m = byKey.get(k);
      if (!m) {
        const [y, mo] = k.split('-');
        m = { key: k, ym: +y * 12 + (+mo - 1), n: 0, albumPlays: 0, first: i, last: i, tracks: new Set(), albums: new Set(), artists: new Set() };
        byKey.set(k, m);
      }
      m.n++; m.last = i;
      m.tracks.add(seqTrack[i]); m.artists.add(seqArtist[i]);
      if (seqAlbum[i] >= 0) { m.albums.add(seqAlbum[i]); m.albumPlays++; }
    }
    return [...byKey.values()];
  }
  // same, but months with no scrobbles at all are included (as 0) between the first and last month
  function monthsFilled() {
    const ms = months();
    if (!ms.length) return [];
    const have = new Map(ms.map(m => [m.ym, m])), out = [];
    for (let ym = ms[0].ym; ym <= ms[ms.length - 1].ym; ym++) out.push(have.get(ym) || { key: ymKey(ym), ym, n: 0 });
    return out;
  }
  const monthCol = { h: 'Month', v: x => x.ym, s: x => monthName(x.key), dir0: -1 };
  const firstLastCols = () => [{ h: '#' }, monthCol,
    { h: 'First scrobble', v: x => ts[x.first], s: x => fmtDT(ts[x.first]), dir0: -1 }, { h: 'First song', v: x => songText(x.first) },
    { h: 'Last scrobble', v: x => ts[x.last], s: x => fmtDT(ts[x.last]), dir0: -1 }, { h: 'Last song', v: x => songText(x.last) }];
  const avg = (plays, unique) => unique ? plays / unique : 0;
  const allAlbumPlays = D.s.a.reduce((n, id) => n + (id >= 0 ? 1 : 0), 0);
  const uniqueCols = () => [{ h: '#' }, monthCol,
    { h: 'Unique tracks', v: x => x.tracks.size, dir0: -1 },
    { h: 'Unique albums', v: x => x.albums.size, dir0: -1 },
    { h: 'Unique artists', v: x => x.artists.size, dir0: -1 },
    { h: 'Scrobbles', v: x => x.n, dir0: -1 },
    { h: 'Scrobbles / artist', v: x => avg(x.n, x.artists.size) },
    { h: 'Scrobbles / album', v: x => avg(x.albumPlays, x.albums.size) },
    { h: 'Scrobbles / track', v: x => avg(x.n, x.tracks.size) }];
  const perMonthCols = () => [{ h: '#' }, monthCol, { h: 'Scrobbles', v: x => x.n, dir0: -1 }];

  // ---------- busiest days ----------
  function days() {
    const di = getDayIdx(), byDay = new Map();
    for (let i = lo; i < hi; i++) {
      const d = di[i];
      let m = byDay.get(d);
      if (!m) { m = { day: d, n: 0, tracks: new Set(), albums: new Set(), artists: new Set() }; byDay.set(d, m); }
      m.n++; m.tracks.add(seqTrack[i]); m.artists.add(seqArtist[i]);
      if (seqAlbum[i] >= 0) m.albums.add(seqAlbum[i]);
    }
    return [...byDay.values()].sort((x, y) => y.n - x.n || y.day - x.day);
  }
  const daysCols = () => [{ h: '#' }, { h: 'Day', v: x => x.day, s: x => fmtDay(x.day), dir0: -1 },
    { h: 'Scrobbles', v: x => x.n, dir0: -1 }, { h: 'Unique tracks', v: x => x.tracks.size, dir0: -1 },
    { h: 'Unique albums', v: x => x.albums.size, dir0: -1 }, { h: 'Unique artists', v: x => x.artists.size, dir0: -1 }];

  // ---------- busiest spans of time (any stretch of N days, starting at any scrobble) ----------
  const cntBuf = { track: new Int32Array(D.tracks.length), album: new Int32Array(D.albums.length), artist: new Int32Array(D.artists.length) };
  // busiest window among scrobbles a..b-1, by number of scrobbles ('n') or of different tracks/albums/artists; earliest wins ties
  function bestSpan(a, b, sp, measure) {
    const arr = measure === 'n' ? null : seq[measure], cnt = arr && cntBuf[measure];
    let best = [0, a, a], j = a, distinct = 0;
    for (let i = a; i < b; i++) {
      while (j < b && ts[j] < ts[i] + sp) {
        if (arr) { const id = arr[j]; if (id >= 0 && cnt[id]++ === 0) distinct++; }
        j++;
      }
      const val = arr ? distinct : j - i;
      if (val > best[0]) best = [val, i, j];
      if (arr) { const id = arr[i]; if (id >= 0 && --cnt[id] === 0) distinct--; }
    }
    return best;
  }
  function windowStats(i, j) {
    const t = new Set(), al = new Set(), ar = new Set();
    for (let k = i; k < j; k++) { t.add(seqTrack[k]); ar.add(seqArtist[k]); if (seqAlbum[k] >= 0) al.add(seqAlbum[k]); }
    return { n: j - i, tracks: t.size, albums: al.size, artists: ar.size };
  }
  // all = false: just the busiest span. all = true: the busiest span, then the busiest spans wholly before and
  // wholly after it, and so on, so no two spans overlap or share a scrobble.
  function spans(measure, dayLen, minVal, all) {
    const sp = dayLen * 86400, out = [], stack = [[lo, hi]];
    while (stack.length) {
      const [a, b] = stack.pop();
      if (a >= b) continue;
      const [val, i, j] = bestSpan(a, b, sp, measure);
      if (val < minVal) continue;
      out.push({ val, s: ts[i], t: ts[j - 1], ...windowStats(i, j) });
      if (all) stack.push([a, i], [j, b]);
    }
    return out.sort((x, y) => y.val - x.val || x.s - y.s);
  }
  const spansCols = () => [{ h: '#' }, { h: 'From', v: x => x.s, s: x => fmtDT(x.s), dir0: -1 }, { h: 'To', v: x => x.t, s: x => fmtDT(x.t), dir0: -1 },
    { h: 'Scrobbles', v: x => x.n, dir0: -1 }, { h: 'Unique tracks', v: x => x.tracks, dir0: -1 },
    { h: 'Unique albums', v: x => x.albums, dir0: -1 }, { h: 'Unique artists', v: x => x.artists, dir0: -1 }];
  const MEASURE_NAME = { n: 'scrobbles', track: 'unique tracks', album: 'unique albums', artist: 'unique artists' };

  // ---------- top charts (with optional Billboard-style comparison) ----------
  const SIZE = { artist: D.artists.length, track: D.tracks.length, album: D.albums.length };
  const firstIdxCache = {};
  function firstIdx(type) {   // index of each item's first-ever scrobble (-1 = never)
    if (firstIdxCache[type]) return firstIdxCache[type];
    const a = new Int32Array(SIZE[type]).fill(-1), s = seq[type];
    for (let i = 0; i < N; i++) { const id = s[i]; if (id >= 0 && a[id] < 0) a[id] = i; }
    return (firstIdxCache[type] = a);
  }
  // Ranked chart for scrobbles i0..i1-1. Most plays first; on a tie, whoever reached that many plays first ranks higher.
  function chartFor(type, i0, i1) {
    const s = seq[type], cnt = new Int32Array(SIZE[type]), last = new Int32Array(SIZE[type]), rank = new Int32Array(SIZE[type]);
    for (let i = i0; i < i1; i++) { const id = s[i]; if (id < 0) continue; cnt[id]++; last[id] = i; }
    const ids = [];
    for (let id = 0; id < cnt.length; id++) if (cnt[id]) ids.push(id);
    ids.sort((a, b) => cnt[b] - cnt[a] || last[a] - last[b] || a - b);
    ids.forEach((id, k) => { rank[id] = k + 1; });
    return { ids, cnt, rank };
  }
  const idxRange = (fromDay, toDay) => { const di = getDayIdx(); return [lowerBound(di, fromDay), lowerBound(di, toDay + 1)]; };
  const monthRange = ym => [Date.UTC(Math.floor(ym / 12), ym % 12, 1) / 86400000, Date.UTC(Math.floor((ym + 1) / 12), (ym + 1) % 12, 1) / 86400000 - 1];
  const yearRange = y => [Date.UTC(y, 0, 1) / 86400000, Date.UTC(y + 1, 0, 1) / 86400000 - 1];
  const entName = (type, id) => type === 'artist' ? { name: D.artists[id], sub: '' }
    : { name: D[type + 's'][id][1], sub: D.artists[D[type + 's'][id][0]] };
  const chartEnt = type => type === 'artist'
    ? [{ h: 'Artist', v: x => x.name }]
    : [{ h: 'Artist', v: x => x.sub }, { h: LABEL[type], v: x => x.name }];
  function fillPeriods() {
    const unit = $('c_unit').value, sel = $('c_period');
    const keys = [...monthSet].sort().reverse();   // 'YYYY-MM', newest first
    const items = unit === 'year' ? [...new Set(keys.map(k => k.slice(0, 4)))] : keys;
    sel.replaceChildren(...items.map(k => {
      const o = document.createElement('option'); o.value = k; o.textContent = unit === 'year' ? k : monthName(k); return o;
    }));
    sel.value = items[0];
  }

  // ---------- "Now playing" widget (landing page) ----------
  const LASTFM_USER = D.user;
  const LASTFM_API_KEY = "__WIDGET_KEY__";

  async function getUserPlaycount(method, extraParams) {
    const params = new URLSearchParams({
      method: method,
      username: LASTFM_USER,
      api_key: LASTFM_API_KEY,
      format: "json",
      ...extraParams
    });

    const response = await fetch("https://ws.audioscrobbler.com/2.0/?" + params.toString());
    const data = await response.json();

    if (!response.ok || data.error) {
      throw new Error(data.message || "Last.fm API error");
    }

    const result = data[method.split(".")[0]];
    if (!result) return null;

    const playcount = method === "artist.getInfo" ? result.stats?.userplaycount : result.userplaycount;
    if (playcount == null) return null;

    return Number(playcount).toLocaleString();
  }

  async function updateLastFM() {
    const statusEl = $("lastfm-status");
    const trackEl = $("lastfm-track");
    const artistEl = $("lastfm-artist");
    const albumEl = $("lastfm-album");
    const coverEl = $("lastfm-cover");
    const artistPlaysEl = $("lastfm-artist-plays");
    const albumPlaysEl = $("lastfm-album-plays");
    const trackPlaysEl = $("lastfm-track-plays");
    const lastPlayedEl = $("lastfm-last-played");

    try {
      const params = new URLSearchParams();
      params.append("method", "user.getRecentTracks");
      params.append("user", LASTFM_USER);
      params.append("api_key", LASTFM_API_KEY);
      params.append("format", "json");
      params.append("limit", "1");

      const response = await fetch("https://ws.audioscrobbler.com/2.0/?" + params.toString());
      const data = await response.json();

      if (data && data.error) {
        throw new Error(data.message || "Last.fm API error");
      }
      if (!data || !data.recenttracks) {
        throw new Error("No recent tracks data");
      }

      let track = data.recenttracks.track;
      if (Array.isArray(track)) {
        track = track[0];
      }
      if (!track) {
        throw new Error("No recent tracks found");
      }

      if (track["@attr"] && track["@attr"].nowplaying === "true") {
        statusEl.textContent = "Now playing:";
        statusEl.className = "";
      } else {
        statusEl.className = "";
        const timestamp = Number(track.date && track.date.uts);
        if (timestamp && Number.isFinite(timestamp)) {
          statusEl.textContent = "Last played " + timeAgo(timestamp) + ":";
        } else {
          statusEl.textContent = "Last played:";
        }
      }

      trackEl.replaceChildren();
      const trackName = track.name || "Unknown track";
      if (track.url) {
        const link = document.createElement("a");
        link.href = track.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = trackName;
        trackEl.appendChild(link);
      } else {
        trackEl.textContent = trackName;
      }

      const artistName = track.artist && track.artist["#text"] ? track.artist["#text"] : "Unknown artist";
      artistEl.textContent = "by " + artistName;

      const albumName = track.album && track.album["#text"] ? track.album["#text"] : "";
      albumEl.textContent = albumName;

      // This user's play counts for the current artist, album, and track.
      artistPlaysEl.textContent = "";
      albumPlaysEl.textContent = "";
      trackPlaysEl.textContent = "";

      const playcountRequests = [
        getUserPlaycount("artist.getInfo", { artist: artistName }),
        albumName
          ? getUserPlaycount("album.getInfo", { artist: artistName, album: albumName })
          : Promise.resolve(null),
        getUserPlaycount("track.getInfo", { artist: artistName, track: trackName })
      ];

      const [artistPlays, albumPlays, trackPlays] = await Promise.allSettled(playcountRequests);

      if (artistPlays.status === "fulfilled") {
        artistPlaysEl.textContent = artistPlays.value + " artist plays";
      }
      if (albumPlays.status === "fulfilled" && albumPlays.value !== null) {
        albumPlaysEl.textContent = albumPlays.value + " album plays";
      }
      if (trackPlays.status === "fulfilled") {
        trackPlaysEl.textContent = trackPlays.value + " track plays";
      }

      lastPlayedEl.textContent = "";
      try {
        const currentUts = track.date && track.date.uts;
        const lastUts = await getLastPlayed(artistName, trackName, currentUts);
        if (lastUts) {
          lastPlayedEl.textContent = "Previously played " + formatFullDate(lastUts);
        }
      } catch (e) {
        // leave it blank if it fails
      }

      let imageUrl = "";
      if (track.image && Array.isArray(track.image)) {
        let chosenImage = null;
        for (let i = 0; i < track.image.length; i++) {
          const currentImage = track.image[i];
          if (currentImage.size === "extralarge") {
            chosenImage = currentImage;
            break;
          }
          if (!chosenImage && currentImage.size === "large") {
            chosenImage = currentImage;
          }
        }
        if (!chosenImage && track.image.length > 0) {
          chosenImage = track.image[track.image.length - 1];
        }
        if (chosenImage && chosenImage["#text"]) {
          imageUrl = chosenImage["#text"];
        }
      }

      coverEl.src = imageUrl;
      coverEl.hidden = !imageUrl;
      coverEl.alt = "Album cover for " + trackName;
    } catch (error) {
      statusEl.textContent = "Last.fm unavailable";
      statusEl.className = "";
      trackEl.textContent = "";
      artistEl.textContent = "";
      albumEl.textContent = "";
      artistPlaysEl.textContent = "";
      albumPlaysEl.textContent = "";
      trackPlaysEl.textContent = "";
      lastPlayedEl.textContent = "";
      coverEl.hidden = true;
    }
  }

  function timeAgo(timestamp) {
    if (!Number.isFinite(timestamp) || timestamp <= 0) {
      return "previously";
    }
    const nowSeconds = Math.floor(Date.now() / 1000);
    const diffSeconds = nowSeconds - timestamp;
    if (diffSeconds < 60) {
      return "just now";
    }
    if (diffSeconds < 3600) {
      return Math.floor(diffSeconds / 60) + " minutes ago";
    }
    if (diffSeconds < 86400) {
      return Math.floor(diffSeconds / 3600) + " hours ago";
    }
    return Math.floor(diffSeconds / 86400) + " days ago";
  }

  function formatFullDate(timestamp) {
    const d = new Date(timestamp * 1000);

    const datePart = d.toLocaleDateString("en-US", {
      weekday: "long",
      year: "numeric",
      month: "long",
      day: "numeric"
    });

    const timePart = d.toLocaleTimeString("en-US", {
      hour: "numeric",
      minute: "2-digit"
    }).replace(/\u202f/g, " ");

    // Calendar-day difference, so 11 PM yesterday counts as "1 day ago"
    const startOfDay = x => new Date(x.getFullYear(), x.getMonth(), x.getDate());
    const dayDiff = Math.round((startOfDay(new Date()) - startOfDay(d)) / 86400000);

    let ago;
    if (dayDiff <= 0) ago = "today";
    else if (dayDiff === 1) ago = "1 day ago";
    else ago = dayDiff + " days ago";

    return datePart + " at " + timePart + " (" + ago + ")";
  }

  async function getLastPlayed(artistName, trackName, currentUts) {
    const params = new URLSearchParams({
      method: "user.getTrackScrobbles",
      user: LASTFM_USER,
      api_key: LASTFM_API_KEY,
      format: "json",
      artist: artistName,
      track: trackName,
      limit: "5"
    });

    const response = await fetch("https://ws.audioscrobbler.com/2.0/?" + params.toString());
    const data = await response.json();

    if (!response.ok || data.error) {
      throw new Error(data.message || "Last.fm API error");
    }

    let scrobbles = data.trackscrobbles && data.trackscrobbles.track;
    if (!scrobbles) return null;
    if (!Array.isArray(scrobbles)) scrobbles = [scrobbles];

    // Skip the scrobble that's the one currently shown at the top of the widget
    const previous = scrobbles.find(s => s.date && String(s.date.uts) !== String(currentUts || ""));
    return previous ? Number(previous.date.uts) : null;
  }

  // Only talk to Last.fm while the widget is on screen (every 30 seconds), not while you're looking at stats.
  let widgetTimer = null;
  function widgetOn(on) {
    $("lastfm-widget").hidden = !on;
    if (on && !widgetTimer) { updateLastFM(); widgetTimer = setInterval(updateLastFM, 30000); }
    if (!on && widgetTimer) { clearInterval(widgetTimer); widgetTimer = null; }
  }

  // ---------- fun stats ----------
  const fmtDur = sec => {
    sec = Math.round(sec);
    if (sec < 60) return sec + 's';
    const m = Math.floor(sec / 60);
    if (m < 60) return m + 'm';
    const h = Math.floor(m / 60);
    if (h < 24) return h + 'h ' + (m % 60) + 'm';
    return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
  };
  const median = a => { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y), m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
  const meanSd = a => { const n = a.length; if (!n) return [0, 0]; const m = a.reduce((x, y) => x + y, 0) / n; return [m, Math.sqrt(a.reduce((x, y) => x + (y - m) * (y - m), 0) / n)]; };   // population SD
  const f1 = v => v.toFixed(1), pct = v => v.toFixed(1) + '%';
  const nf = v => v.toLocaleString();
  let hourCache = null;
  const getHours = () => hourCache || (hourCache = ts.map(t => +dtFmt.format(new Date(t * 1000)).slice(11, 13) % 24));
  const weekdayOf = day => (((day + 3) % 7) + 7) % 7;   // 0 = Monday ... 6 = Sunday
  const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const gini = counts => {   // 0 = everything played equally, 1 = all plays on one thing
    const x = [...counts].sort((a, b) => a - b), m = x.length, sum = x.reduce((a, b) => a + b, 0);
    if (!m || !sum) return 0;
    return 2 * x.reduce((a, v, i) => a + (i + 1) * v, 0) / (m * sum) - (m + 1) / m;
  };
  const effective = counts => {   // "as if you listened equally to this many": exp of the Shannon entropy
    const sum = counts.reduce((a, b) => a + b, 0);
    return sum ? Math.exp(-counts.reduce((a, c) => a + (c ? (c / sum) * Math.log(c / sum) : 0), 0)) : 0;
  };

  // --- sessions ---
  const sesArt = new Int32Array(D.artists.length), sesTrk = new Int32Array(D.tracks.length);
  function sessionStats(a, b) {
    const arts = [], trks = [];
    for (let k = a; k < b; k++) {
      const ar = seqArtist[k], tr = seqTrack[k];
      if (sesArt[ar]++ === 0) arts.push(ar);
      if (sesTrk[tr]++ === 0) trks.push(tr);
    }
    let top = 0, topId = -1;
    for (const ar of arts) if (sesArt[ar] > top) { top = sesArt[ar]; topId = ar; }   // ties: whoever appeared first
    arts.forEach(ar => { sesArt[ar] = 0; });
    trks.forEach(tr => { sesTrk[tr] = 0; });
    return { a, b, n: b - a, start: ts[a], end: ts[b - 1], dur: ts[b - 1] - ts[a], artists: arts.length, tracks: trks.length,
      topArtist: D.artists[topId], topShare: top / (b - a) * 100 };
  }
  function sessions(gapMin) {
    const gap = gapMin * 60, out = [];
    let a = lo;
    for (let i = lo + 1; i <= hi; i++) {
      if (i === hi || ts[i] - ts[i - 1] > gap) { if (i > a) out.push(sessionStats(a, i)); a = i; }
    }
    return out;
  }
  const sessionCols = () => [{ h: '#' },
    { h: 'Start', v: x => x.start, s: x => fmtDT(x.start), dir0: -1 }, { h: 'End', v: x => x.end, s: x => fmtDT(x.end), dir0: -1 },
    { h: 'Duration', v: x => x.dur, s: x => fmtDur(x.dur), dir0: -1 }, { h: 'Scrobbles', v: x => x.n, dir0: -1 },
    { h: 'Unique artists', v: x => x.artists, dir0: -1 }, { h: 'Unique tracks', v: x => x.tracks, dir0: -1 },
    { h: 'Top artist', v: x => x.topArtist, s: x => x.topArtist + ' (' + x.topShare.toFixed(0) + '%)' }];

  // --- when you listen ---
  function rhythm() {
    const hrs = getHours(), di = getDayIdx(), rows = [];
    for (let h = 0; h < 24; h++) rows.push({ hour: h, d: new Array(7).fill(0), total: 0 });
    for (let i = lo; i < hi; i++) { const r = rows[hrs[i]]; r.d[weekdayOf(di[i])]++; r.total++; }
    return rows;
  }
  const rhythmCols = (maxTotal, n) => [{ h: 'Hour', v: x => x.hour, s: x => String(x.hour).padStart(2, '0') + ':00' },
    ...WEEKDAYS.map((w, k) => ({ h: w, v: x => x.d[k], dir0: -1 })),
    { h: 'Total', v: x => x.total, dir0: -1 }, { h: 'Share', v: x => x.total / n * 100, s: x => pct(x.total / n * 100), dir0: -1 },
    { h: 'Chart', v: x => x.total, s: x => '█'.repeat(Math.round(x.total / maxTotal * 24)), dir0: -1 }];

  // --- discovery (first-ever plays; looks at the whole history, not just the date range) ---
  let discCache = null;
  function discovery() {
    if (discCache) return discCache;
    const flag = type => {
      const f = firstIdx(type), s = seq[type], pre = new Int32Array(N + 1);
      for (let i = 0; i < N; i++) pre[i + 1] = pre[i] + (s[i] >= 0 && f[s[i]] === i ? 1 : 0);
      return pre;
    };
    return (discCache = { artist: flag('artist'), track: flag('track'), album: flag('album') });
  }
  function discoveryRows() {
    const dc = discovery(), ms = months();
    for (const m of ms) {
      m.newA = dc.artist[m.last + 1] - dc.artist[m.first]; m.newAl = dc.album[m.last + 1] - dc.album[m.first]; m.newT = dc.track[m.last + 1] - dc.track[m.first];
      m.cumA = dc.artist[m.last + 1]; m.cumT = dc.track[m.last + 1];
    }
    return ms.reverse();
  }
  const discoveryCols = () => [{ h: '#' }, monthCol,
    { h: 'New artists', v: x => x.newA, dir0: -1 }, { h: 'New albums', v: x => x.newAl, dir0: -1 }, { h: 'New tracks', v: x => x.newT, dir0: -1 },
    { h: 'Scrobbles', v: x => x.n, dir0: -1 },
    { h: 'First-time plays', v: x => x.newT / x.n * 100, s: x => pct(x.newT / x.n * 100), dir0: -1 },
    { h: 'Artists so far', v: x => x.cumA, dir0: -1 }, { h: 'Tracks so far', v: x => x.cumT, dir0: -1 }];

  // --- records ---
  function records() {
    const n = hi - lo;
    if (!n) return [];
    const di = getDayIdx(), hrs = getHours(), out = [];
    const add = (name, value, detail) => out.push({ name, value, detail: detail || '' });
    const dayN = new Map(), hourN = new Map(), weekN = new Map(), artDay = new Map(), trkDay = new Map();
    const A = D.artists.length, T = D.tracks.length;
    let night = 0, weekend = 0, replay = 0;
    const hourSlot = new Array(24).fill(0), wkSlot = new Array(7).fill(0);
    for (let i = lo; i < hi; i++) {
      const d = di[i], w = weekdayOf(d);
      dayN.set(d, (dayN.get(d) || 0) + 1);
      const hk = d * 24 + hrs[i]; hourN.set(hk, (hourN.get(hk) || 0) + 1);
      const wk = Math.floor((d + 3) / 7); weekN.set(wk, (weekN.get(wk) || 0) + 1);
      const ak = d * A + seqArtist[i]; artDay.set(ak, (artDay.get(ak) || 0) + 1);
      const tk = d * T + seqTrack[i]; trkDay.set(tk, (trkDay.get(tk) || 0) + 1);
      hourSlot[hrs[i]]++; wkSlot[w]++;
      if (hrs[i] < 6) night++;
      if (w >= 5) weekend++;
      if (i > lo && seqTrack[i] === seqTrack[i - 1]) replay++;
    }
    const maxOf = (map) => { let best = null; for (const [k, v] of map) if (!best || v > best[1]) best = [k, v]; return best; };   // first one wins ties
    const days = [...dayN.keys()];
    const calDays = days[days.length - 1] - days[0] + 1;
    add('Scrobbles', nf(n), fmtD(ts[lo]) + ' to ' + fmtD(ts[hi - 1]));
    add('Days with scrobbles', nf(days.length) + ' of ' + nf(calDays), pct(days.length / calDays * 100) + ' of the days in that stretch');
    const perDay = [...dayN.values()], [mean] = meanSd(perDay);
    add('A typical active day', 'median ' + f1(median(perDay)) + ', mean ' + f1(mean), 'scrobbles per day, counting only days you scrobbled');
    // longest streak of days with a scrobble / longest silence between two active days
    let bestS = [0, 0, 0], bestB = [0, 0, 0], runStart = days[0];
    for (let k = 1; k <= days.length; k++) {
      if (k === days.length || days[k] !== days[k - 1] + 1) {
        const len = days[k - 1] - runStart + 1;
        if (len > bestS[0]) bestS = [len, runStart, days[k - 1]];
        if (k < days.length) { const g = days[k] - days[k - 1] - 1; if (g > bestB[0]) bestB = [g, days[k - 1] + 1, days[k] - 1]; runStart = days[k]; }
      }
    }
    add('Longest listening streak', nf(bestS[0]) + ' days in a row', fmtDay(bestS[1]) + ' to ' + fmtDay(bestS[2]));
    add('Longest break', bestB[0] ? nf(bestB[0]) + ' days with no scrobbles' : 'none', bestB[0] ? fmtDay(bestB[1]) + ' to ' + fmtDay(bestB[2]) : '');
    const bd = maxOf(dayN); add('Busiest day', nf(bd[1]) + ' scrobbles', fmtDay(bd[0]));
    const bh = maxOf(hourN); add('Busiest hour', nf(bh[1]) + ' scrobbles', fmtDay(Math.floor(bh[0] / 24)) + ' at ' + String(bh[0] % 24).padStart(2, '0') + ':00');
    const bw = maxOf(weekN); add('Busiest week (Mon to Sun)', nf(bw[1]) + ' scrobbles', 'week of ' + fmtDay(bw[0] * 7 - 3));
    const ms = months(), bm = ms.reduce((b, m) => (!b || m.n > b.n ? m : b), null);
    add('Busiest month', nf(bm.n) + ' scrobbles', monthName(bm.key));
    const ses = sessions(30), bs = ses.reduce((b, x) => (!b || x.n > b.n ? x : b), null);
    add('Longest session (30 min gaps)', nf(bs.n) + ' scrobbles in ' + fmtDur(bs.dur), fmtDT(bs.start) + ' to ' + fmtDT(bs.end));
    const ba = maxOf(artDay); add('Most of one artist in a day', nf(ba[1]) + ' scrobbles', D.artists[ba[0] % A] + ', ' + fmtDay(Math.floor(ba[0] / A)));
    const bt = maxOf(trkDay); add('Most of one track in a day', nf(bt[1]) + ' plays', D.tracks[bt[0] % T][1] + ' by ' + D.artists[D.tracks[bt[0] % T][0]] + ', ' + fmtDay(Math.floor(bt[0] / T)));
    const tc = E.track.map(e => e.ts.length).filter(c => c > 0), ac = E.artist.map(e => e.ts.length).filter(c => c > 0);
    const once = (c) => c.filter(v => v === 1).length;
    add('Tracks played exactly once', nf(once(tc)) + ' of ' + nf(tc.length), pct(once(tc) / tc.length * 100) + ' of the different tracks you played');
    add('Artists played exactly once', nf(once(ac)) + ' of ' + nf(ac.length), pct(once(ac) / ac.length * 100) + ' of the different artists you played');
    const sortedA = [...ac].sort((a, b) => b - a), sortedT = [...tc].sort((a, b) => b - a);
    const share = (arr, k) => arr.slice(0, k).reduce((a, b) => a + b, 0) / n * 100;
    add('Top artists\' share of scrobbles', 'top 1: ' + pct(share(sortedA, 1)) + ', top 10: ' + pct(share(sortedA, 10)) + ', top 50: ' + pct(share(sortedA, 50)), 'how concentrated your listening is');
    add('Top 10 tracks\' share', pct(share(sortedT, 10)), 'of all scrobbles');
    add('Inequality (Gini) across artists', gini(ac).toFixed(3), '0 = every artist played equally, 1 = everything on one artist');
    add('Effective number of artists', f1(effective(ac)), 'as if you had listened equally to this many artists');
    add('Effective number of tracks', f1(effective(tc)), 'same idea for tracks');
    add('Replay rate', pct(n > 1 ? replay / (n - 1) * 100 : 0), 'scrobbles that are the same track as the one right before');
    add('Night owl share', pct(night / n * 100), 'scrobbled between 00:00 and 05:59');
    add('Weekend share', pct(weekend / n * 100), 'Saturday and Sunday (a flat 28.6% would be an even week)');
    const bhr = hourSlot.indexOf(Math.max(...hourSlot)), bwd = wkSlot.indexOf(Math.max(...wkSlot));
    add('Favorite time of day', String(bhr).padStart(2, '0') + ':00 to ' + String(bhr).padStart(2, '0') + ':59', nf(hourSlot[bhr]) + ' scrobbles in that hour of the day');
    add('Favorite day of the week', WEEKDAYS[bwd], nf(wkSlot[bwd]) + ' scrobbles');
    let loyal = null; E.artist.forEach(e => { if (e.ts.length && (!loyal || e.months.size > loyal.months.size)) loyal = e; });
    add('Most loyal artist', loyal.name, 'played in ' + loyal.months.size + ' of the ' + ms.length + ' months with scrobbles');
    const ds = days.length ? (() => { let sum = 0; const dd = (() => { const m = new Map(); for (let i = lo; i < hi; i++) { const d = di[i]; if (!m.has(d)) m.set(d, new Set()); m.get(d).add(seqArtist[i]); } return m; })(); for (const st of dd.values()) sum += st.size; return sum / dd.size; })() : 0;
    add('Different artists per active day', f1(ds), 'on average');
    return out;
  }

  // --- comebacks ---
  function comebacks(type, minGapDays, minAfter) {
    const out = [], gapSec = minGapDays * 86400, now = rangeEnd === null ? Date.now() / 1000 : rangeEnd;
    for (const e of E[type]) {
      const t = e.ts, n = t.length;
      if (n < 2) continue;
      let bi = -1, bg = 0;
      for (let i = 1; i < n; i++) { const g = t[i] - t[i - 1]; if (g > bg) { bg = g; bi = i; } }   // longest gap; earliest wins ties
      if (bg < gapSec) continue;
      const after = n - bi;
      if (after < minAfter) continue;
      out.push({ e, n, gap: bg / 86400, leftAt: t[bi - 1], returnAt: t[bi], before: bi, after, perDay: Math.max((now - t[bi]) / 86400, 1) / after });
    }
    return out.sort((x, y) => y.after - x.after || y.gap - x.gap);
  }
  const comebackCols = type => [{ h: '#' },
    { h: 'Longest gap (days)', v: x => x.gap, s: x => Math.round(x.gap), dir0: -1 },
    { h: 'Last play before', v: x => x.leftAt, s: x => fmtD(x.leftAt), dir0: -1 }, { h: 'Came back', v: x => x.returnAt, s: x => fmtD(x.returnAt), dir0: -1 },
    { h: 'Plays before', v: x => x.before, dir0: -1 }, { h: 'Plays after', v: x => x.after, dir0: -1 },
    { h: 'Days/play since coming back', v: x => x.perDay, s: x => x.perDay.toFixed(2) },
    { h: 'All plays', v: x => x.n, dir0: -1 }, ...entCols(type)];

  // --- binge or steady? ---
  function burstiness(type, minPlays, winDays) {
    const out = [], sp = winDays * 86400;
    for (const e of E[type]) {
      const t = e.ts, n = t.length;
      if (n < Math.max(3, minPlays)) continue;
      const g = []; for (let i = 1; i < n; i++) g.push(t[i] - t[i - 1]);
      const [m, sd] = meanSd(g), B = (sd + m) ? (sd - m) / (sd + m) : 0;
      const [pk, pkStart] = bestWindow(t, sp);
      out.push({ e, n, B, share: pk / n * 100, peakStart: pkStart, half: t[Math.ceil(n / 2) - 1], first: t[0], last: t[n - 1] });
    }
    return out.sort((x, y) => y.B - x.B || y.n - x.n);
  }
  const verdict = B => B >= 0.25 ? 'Binge-y' : B <= -0.25 ? 'Steady' : 'Mixed';
  const burstCols = type => [{ h: '#' }, { h: 'Plays', v: x => x.n, dir0: -1 },
    { h: 'Burstiness', v: x => x.B, s: x => x.B.toFixed(3), dir0: -1 }, { h: 'Verdict', v: x => x.B, s: x => verdict(x.B), dir0: -1 },
    { h: 'Share in peak window', v: x => x.share, s: x => pct(x.share), dir0: -1 }, { h: 'Peak began', v: x => x.peakStart, s: x => fmtD(x.peakStart), dir0: -1 },
    { h: 'Half its plays done by', v: x => x.half, s: x => fmtD(x.half), dir0: -1 },
    { h: 'First played', v: x => x.first, s: x => fmtD(x.first), dir0: -1 }, { h: 'Last played', v: x => x.last, s: x => fmtD(x.last), dir0: -1 },
    ...entCols(type)];

  // --- diversity & power laws ---
  // Shannon entropy H = -Σ p log2 p (bits). Perplexity = 2^H, the "effective number" of equally-likely items.
  // Simpson's D = Σ p², the probability two random plays are the same item. Gini as in economics.
  // HHI = 10000 · Σ p² (economists' concentration index). Zipf fit: OLS of log(rank) vs log(plays);
  // a pure Zipf law gives slope -1. R² says how well the power law actually fits.
  function diversityStats(type, minPlays) {
    minPlays = minPlays || 1;
    const counts = E[type].map(e => e.ts.length).filter(c => c >= minPlays).sort((a, b) => b - a);
    const m = counts.length, total = counts.reduce((a, b) => a + b, 0);
    if (!m || !total) return null;
    const p = counts.map(c => c / total);
    const H = -p.reduce((a, q) => a + (q > 0 ? q * Math.log2(q) : 0), 0);
    const perplexity = 2 ** H;
    const simpson = p.reduce((a, q) => a + q * q, 0);
    const simpsonDiv = 1 - simpson;
    const hhi = simpson * 10000;
    const asc = [...counts].sort((a, b) => a - b);
    let g = 0;
    for (let i = 0; i < m; i++) g += (2 * (i + 1) - m - 1) * asc[i];
    const gini = g / (m * total);
    const nFit = Math.min(m, 500);
    const xs = [], ys = [];
    for (let i = 0; i < nFit; i++) { xs.push(Math.log(i + 1)); ys.push(Math.log(Math.max(counts[i], 1))); }
    const mx = xs.reduce((a, b) => a + b, 0) / nFit, my = ys.reduce((a, b) => a + b, 0) / nFit;
    let sxy = 0, sxx = 0, syy = 0;
    for (let i = 0; i < nFit; i++) { sxy += (xs[i] - mx) * (ys[i] - my); sxx += (xs[i] - mx) ** 2; syy += (ys[i] - my) ** 2; }
    const slope = sxx ? sxy / sxx : 0, r2 = sxx && syy ? (sxy * sxy) / (sxx * syy) : 0;
    const once = counts.filter(c => c === 1).length;
    return { type, m, total, H, perplexity, simpson, simpsonDiv, hhi, gini, slope, r2, once,
             top1: counts[0] / total, top10: counts.slice(0, 10).reduce((a, b) => a + b, 0) / total };
  }

  function pearson(a, b) {
    const n = a.length;
    if (n < 2) return null;
    const ma = a.reduce((x, y) => x + y, 0) / n, mb = b.reduce((x, y) => x + y, 0) / n;
    let num = 0, sa = 0, sb = 0;
    for (let i = 0; i < n; i++) { num += (a[i] - ma) * (b[i] - mb); sa += (a[i] - ma) ** 2; sb += (b[i] - mb) ** 2; }
    return sa && sb ? num / Math.sqrt(sa * sb) : null;
  }
  function tasteDrift(type, topN) {
    const ms = months().reverse();
    if (ms.length < 2) return [];
    const SIZE = { artist: D.artists.length, album: D.albums.length, track: D.tracks.length }[type];
    const first = firstIdx(type);
    const byMonth = ms.map(mo => {
      const cnt = new Int32Array(SIZE);
      for (let i = mo.first; i <= mo.last; i++) { const id = seq[type][i]; if (id >= 0) cnt[id]++; }
      const ids = [];
      for (let id = 0; id < cnt.length; id++) if (cnt[id]) ids.push(id);
      ids.sort((a, b) => cnt[b] - cnt[a]);
      const rank = new Int32Array(SIZE).fill(-1);
      ids.forEach((id, k) => { rank[id] = k + 1; });
      return { mo, rank, top: ids.slice(0, topN) };
    });
    const out = [];
    for (let i = 1; i < byMonth.length; i++) {
      const prev = byMonth[i - 1], cur = byMonth[i];
      const union = [...new Set([...prev.top, ...cur.top])];
      const ra = union.map(id => prev.rank[id] || topN + 1);
      const rb = union.map(id => cur.rank[id] || topN + 1);
      const rho = pearson(ra, rb);
      const setA = new Set(prev.top), setB = new Set(cur.top);
      const inter = [...setB].filter(id => setA.has(id)).length;
      const jaccard = setA.size && setB.size ? inter / (setA.size + setB.size - inter) : 0;
      let novel = 0;
      for (let j = cur.mo.first; j <= cur.mo.last; j++) {
        const id = seq[type][j];
        if (id >= 0 && first[id] >= cur.mo.first && first[id] <= cur.mo.last) novel++;
      }
      out.push({ mo: cur.mo, rho, jaccard, inter, novel, novelty: novel / cur.mo.n });
    }
    return out;
  }
  const diversitySummary = () => {
    const rows = ['artist', 'album', 'track'].map(t => diversityStats(t, Math.max(1, Math.round(num('dv_min', 1, 1))))).filter(Boolean);
    const el = $('dv_summary');
    if (!rows.length) { el.replaceChildren(); return rows; }
    el.replaceChildren(...rows.map(r => {
      const d = document.createElement('div');
      d.style.margin = '6px 0';
      d.textContent = LABEL[r.type] + 's: ' + r.m.toLocaleString() + ' with plays · ' +
        'Shannon H = ' + r.H.toFixed(2) + ' bits (perplexity ' + r.perplexity.toFixed(1) + ') · ' +
        'Simpson = ' + r.simpson.toFixed(3) + ' (diversity ' + r.simpsonDiv.toFixed(3) + ') · ' +
        'Gini = ' + r.gini.toFixed(3) + ' · HHI = ' + r.hhi.toFixed(0) + ' · ' +
        'Zipf slope = ' + r.slope.toFixed(2) + ' (R² ' + r.r2.toFixed(2) + ')';
      return d;
    }));
    return rows;
  };
  const diversityCols = () => [
    { h: 'Type', v: x => x.type, s: x => LABEL[x.type] + 's' },
    { h: 'Played ≥ once', v: x => x.m, dir0: -1 },
    { h: 'Total plays', v: x => x.total, dir0: -1 },
    { h: 'H (bits)', v: x => x.H, s: x => x.H.toFixed(2), dir0: -1 },
    { h: 'Perplexity', v: x => x.perplexity, s: x => x.perplexity.toFixed(1), dir0: -1 },
    { h: 'Simpson (Σp²)', v: x => x.simpson, s: x => x.simpson.toFixed(3) },
    { h: 'Simpson diversity', v: x => x.simpsonDiv, s: x => x.simpsonDiv.toFixed(3), dir0: -1 },
    { h: 'Gini', v: x => x.gini, s: x => x.gini.toFixed(3) },
    { h: 'HHI (0–10000)', v: x => x.hhi, s: x => x.hhi.toFixed(0) },
    { h: 'Zipf slope', v: x => x.slope, s: x => x.slope.toFixed(2) },
    { h: 'Zipf R²', v: x => x.r2, s: x => x.r2.toFixed(2), dir0: -1 },
    { h: 'Played once', v: x => x.once, dir0: -1 },
    { h: 'Top 1 share', v: x => x.top1, s: x => pct(x.top1 * 100), dir0: -1 },
    { h: 'Top 10 share', v: x => x.top10, s: x => pct(x.top10 * 100), dir0: -1 },
  ];
  const tasteCols = () => [
    { h: 'Month', v: x => x.mo.ym, s: x => monthName(x.mo.key), dir0: -1 },
    { h: 'Plays', v: x => x.mo.n, dir0: -1 },
    { h: 'Spearman ρ vs previous', v: x => x.rho ?? 0, s: x => x.rho === null ? '-' : x.rho.toFixed(3), dir0: -1 },
    { h: 'Jaccard (top N)', v: x => x.jaccard, s: x => x.jaccard.toFixed(3), dir0: -1 },
    { h: 'In both top N', v: x => x.inter, dir0: -1 },
    { h: 'First-time plays', v: x => x.novel, dir0: -1 },
    { h: 'Novelty share', v: x => x.novelty, s: x => pct(x.novelty * 100), dir0: -1 },
  ];

  // --- charts: any measure over any dimension, rendered as text bars ---
  //
  // The charts section is self-contained: it has its OWN date range
  // (ch_range / ch_year / ch_from / ch_to), independent of the page's From/To.
  // That way "plays of Track X over the last 90 days" is possible regardless of what
  // the other sections are filtered to.
  //
  // Measures:
  //   n        -> number of scrobbles in the bucket
  //   utrack   -> number of distinct tracks
  //   ualbum   -> number of distinct albums
  //   uartist  -> number of distinct artists
  //   minutes  -> falls back to scrobbles (durations aren't in data.json)
  //
  // Ordering: ch_dir picks desc/asc; ch_cum turns the value into a running total in the
  // current order; ch_log turns the drawn bar length into log(value+1).
  //
  // Scale: ch_scale_mode = 'auto' makes the biggest bar fill ch_width; 'manual' pins the
  // max to ch_scale_max so different charts can be compared side by side.
  //
  // Layout: ch_wide draws a full-width bar under the label instead of a narrow column of blocks.
  const CH_DIM_LABEL = { hour: 'Hour', weekday: 'Weekday', day: 'Day', week: 'Week', month: 'Month', year: 'Year',
                         artist: 'Artist', album: 'Album', track: 'Track' };
  const CH_MEASURE_LABEL = { n: 'scrobbles', utrack: 'unique tracks', ualbum: 'unique albums', uartist: 'unique artists', minutes: 'minutes' };

  // Resolve the chart's own date range into [a, b) scrobble indexes.
  function chartRangeIndexes() {
    const mode = $('ch_range').value;
    if (mode === 'global') return [lo, hi];
    if (mode === 'all') return [0, N];
    const di = getDayIdx();
    const today = dayIndex(Date.now() / 1000);
    let fromDay, toDay;
    if (mode === 'last7' || mode === 'last30' || mode === 'last90' || mode === 'last365') {
      const days = { last7: 7, last30: 30, last90: 90, last365: 365 }[mode];
      fromDay = today - days + 1;
      toDay = today;
    } else if (mode === 'year') {
      const y = Math.max(2000, Math.min(2100, Math.round(num('ch_year', new Date().getFullYear(), 2000))));
      fromDay = Date.UTC(y, 0, 1) / 86400000;
      toDay = Date.UTC(y + 1, 0, 1) / 86400000 - 1;
    } else if (mode === 'custom') {
      const f = $('ch_from').value, t = $('ch_to').value;
      if (!f && !t) return [0, N];
      fromDay = f ? dateToIdx(f) : di[0];
      toDay = t ? dateToIdx(t) : today;
      if (fromDay > toDay) [fromDay, toDay] = [toDay, fromDay];
    } else {
      return [0, N];
    }
    const a = lowerBound(di, fromDay);
    const b = lowerBound(di, toDay + 1);
    return [a, Math.max(a, b)];
  }

  function bucketValue(measure, plays, tracks, albums, artists) {
    if (measure === 'n') return plays;
    if (measure === 'utrack') return tracks.size;
    if (measure === 'ualbum') return albums.size;
    if (measure === 'uartist') return artists.size;
    if (measure === 'minutes') return null;
    return plays;
  }

  // Render a bar chart. opts: width, log, scaleMax (null = auto), wide.
  function renderBars(el, rows, opts) {
    opts = opts || {};
    const width = opts.width || 40;
    const logged = !!opts.log;
    const valuesForScale = rows.map(r => logged ? Math.log((r.value || 0) + 1) : (r.value || 0));
    const autoMax = valuesForScale.reduce((m, v) => Math.max(m, v), 0) || 1;
    const maxVal = opts.scaleMax != null ? opts.scaleMax : autoMax;
    const scaleFor = v => logged ? Math.log((v || 0) + 1) : (v || 0);
    const barLen = v => {
      const scaled = scaleFor(v);
      const frac = maxVal ? scaled / maxVal : 0;
      return Math.round(Math.max(0, Math.min(1, frac)) * width);
    };

    const t = document.createElement('table');
    t.border = 1; t.cellSpacing = 0; t.cellPadding = 4;
    const hr = t.createTHead().insertRow();

    if (opts.wide) {
      ['#', 'Bar', 'Value'].forEach(h => { const th = document.createElement('th'); th.textContent = h; hr.appendChild(th); });
      const tb = t.createTBody();
      rows.forEach((r, i) => {
        const tr = tb.insertRow();
        tr.insertCell().textContent = i + 1;
        tr.insertCell().textContent = r.label;
        tr.insertCell().textContent = r.fmt || String(r.value);
        const barRow = tb.insertRow();
        const barCell = barRow.insertCell();
        barCell.colSpan = 3;
        barCell.textContent = '█'.repeat(barLen(r.value));
      });
    } else {
      ['#', 'Bar', 'Value', 'Chart'].forEach(h => { const th = document.createElement('th'); th.textContent = h; hr.appendChild(th); });
      const tb = t.createTBody();
      rows.forEach((r, i) => {
        const tr = tb.insertRow();
        tr.insertCell().textContent = i + 1;
        tr.insertCell().textContent = r.label;
        tr.insertCell().textContent = r.fmt || String(r.value);
        tr.insertCell().textContent = '█'.repeat(barLen(r.value));
      });
    }
    el.replaceChildren(t);
  }

  function timeBuckets(a, b, dim, measure) {
    const di = getDayIdx(), hrs = getHours();
    const buckets = new Map();
    const touch = key => {
      let x = buckets.get(key);
      if (!x) { x = { key, n: 0, tracks: new Set(), albums: new Set(), artists: new Set(), _sort: 0 }; buckets.set(key, x); }
      return x;
    };
    if (dim === 'hour') for (let h = 0; h < 24; h++) touch(h);
    if (dim === 'weekday') for (let w = 0; w < 7; w++) touch(w);

    let minSort = Infinity, maxSort = -Infinity;
    for (let i = a; i < b; i++) {
      let key, sortKey;
      if (dim === 'hour') { key = hrs[i]; sortKey = key; }
      else if (dim === 'weekday') { key = weekdayOf(di[i]); sortKey = key; }
      else if (dim === 'day') { key = fmtDay(di[i]); sortKey = di[i]; }
      else if (dim === 'week') {
        const monday = di[i] - (((di[i] + 3) % 7 + 7) % 7);
        key = fmtDay(monday); sortKey = monday;
      } else if (dim === 'month') {
        const [yy, mm] = mk[i].split('-');
        key = mk[i]; sortKey = +yy * 12 + (+mm - 1);
      } else if (dim === 'year') {
        key = mk[i].slice(0, 4); sortKey = +key;
      }
      const x = touch(key);
      x.n++;
      x.tracks.add(seqTrack[i]); x.artists.add(seqArtist[i]);
      if (seqAlbum[i] >= 0) x.albums.add(seqAlbum[i]);
      x._sort = sortKey;
      if (sortKey < minSort) minSort = sortKey;
      if (sortKey > maxSort) maxSort = sortKey;
    }

    if (dim === 'day' || dim === 'week' || dim === 'month' || dim === 'year') {
      for (let s = minSort; s <= maxSort; s++) {
        if (!Number.isFinite(s)) continue;
        let key;
        if (dim === 'day' || dim === 'week') key = fmtDay(s);
        else if (dim === 'month') key = ymKey(s);
        else key = String(s);
        if (!buckets.has(key)) { const x = touch(key); x._sort = s; }
      }
    }

    return [...buckets.values()].map(x => {
      const value = bucketValue(measure, x.n, x.tracks, x.albums, x.artists) ?? x.n;
      let label;
      if (dim === 'hour') label = String(x.key).padStart(2, '0') + ':00';
      else if (dim === 'weekday') label = WEEKDAYS[x.key];
      else if (dim === 'month') label = monthName(x.key);
      else label = x.key;
      return { label, value, fmt: nf(value), _sort: x._sort };
    }).sort((p, q) => p._sort - q._sort);
  }

  function entityBuckets(a, b, type, measure) {
    const SIZE = { artist: D.artists.length, album: D.albums.length, track: D.tracks.length }[type];
    const cnt = new Int32Array(SIZE),
          trks = Array.from({ length: SIZE }, () => new Set()),
          albs = Array.from({ length: SIZE }, () => new Set()),
          arts = Array.from({ length: SIZE }, () => new Set());
    for (let i = a; i < b; i++) {
      const id = seq[type][i];
      if (id < 0) continue;
      cnt[id]++;
      trks[id].add(seqTrack[i]); arts[id].add(seqArtist[i]);
      if (seqAlbum[i] >= 0) albs[id].add(seqAlbum[i]);
    }
    const rows = [];
    for (let id = 0; id < cnt.length; id++) {
      if (!cnt[id]) continue;
      const value = measure === 'minutes' ? cnt[id] : bucketValue(measure, cnt[id], trks[id], albs[id], arts[id]);
      if (value == null) continue;
      const name = type === 'artist' ? D.artists[id] : D[type + 's'][id][1];
      const sub = type === 'artist' ? '' : D.artists[D[type + 's'][id][0]];
      rows.push({ label: sub ? sub + ' – ' + name : name, value, fmt: nf(value), search: (sub + ' ' + name).toLowerCase() });
    }
    return rows;
  }

  const chartsNote = () => {
    const dim = $('ch_dim').value, measure = $('ch_measure').value;
    const el = $('ch_note');
    const base = 'Plotting ' + CH_MEASURE_LABEL[measure] + ' per ' + CH_DIM_LABEL[dim].toLowerCase() +
      ' within the chart range, independent of the page’s From/To.';
    if (measure === 'minutes') {
      el.textContent = base + ' Track durations aren’t in data.json (Last.fm doesn’t return them), so this falls back to scrobbles.';
    } else {
      el.textContent = base;
    }
  };

  const renderCharts = () => {
    chartsNote();
    const dim = $('ch_dim').value, measure = $('ch_measure').value;
    const capN = Math.max(1, Math.round(num('ch_top', 24, 1)));
    const width = Math.max(4, Math.round(num('ch_width', 40, 4)));
    const doSort = $('ch_sort').checked, dir = $('ch_dir').value === 'asc' ? 1 : -1;
    const doCum = $('ch_cum').checked, doLog = $('ch_log').checked;
    const wide = $('ch_wide').checked;
    const scaleMode = $('ch_scale_mode').value;
    const scaleMax = scaleMode === 'manual' ? Math.max(1, num('ch_scale_max', 100, 1)) : null;
    const q = $('ch_q').value.trim().toLowerCase();

    $('ch_range_year').hidden = $('ch_range').value !== 'year';
    $('ch_range_custom').hidden = $('ch_range').value !== 'custom';
    $('ch_scale_manual').hidden = scaleMode !== 'manual';

    const [a, b] = chartRangeIndexes();
    let rows;
    if (dim === 'artist' || dim === 'album' || dim === 'track') {
      rows = entityBuckets(a, b, dim, measure);
      if (q) rows = rows.filter(r => r.search.includes(q));
    } else {
      rows = timeBuckets(a, b, dim, measure);
      if (q) rows = rows.filter(r => r.label.toLowerCase().includes(q));
    }

    if (doSort) rows = [...rows].sort((p, q2) => dir * (p.value - q2.value) || p.label.localeCompare(q2.label));
    rows = rows.slice(0, capN);

    if (doCum) {
      let acc = 0;
      rows = rows.map(r => { acc += r.value; return { ...r, value: acc, fmt: nf(acc) }; });
    }

    renderBars($('t_charts'), rows, { width, log: doLog, scaleMax, wide });
  };

  // --- loved tracks: extra columns ---
  function lovedStats(x, N) {
    if (x.st && x.stN === N) return x.st;
    const t = x.e.ts, n = t.length, g = [];
    for (let i = 1; i < n; i++) g.push((t[i] - t[i - 1]) / 86400);
    const [peak, peakStart] = bestWindow(t, 30 * 86400);
    const ymOf = sec => { const k = monthKey(sec); return +k.slice(0, 4) * 12 + (+k.slice(5, 7) - 1); };
    const totalMonths = Math.max(1, ymOf(x.end) - ymOf(t[0]) + 1);
    x.stN = N;
    return (x.st = {
      median: n >= 2 ? median(g) : -1,
      fromN: n >= N ? Math.max((x.end - t[N - 1]) / 86400, x.minDays) / (n - N + 1) : -1,
      peak, peakStart,
      recent: n - lowerBound(t, x.end - 90 * 86400),
      active: x.e.months.size, totalMonths, activeRatio: x.e.months.size / totalMonths,
    });
  }
  const lovedExtra = () => {
    const N = Math.round(num('l_from', 3, 2)), st = x => lovedStats(x, N);
    return [
      { h: 'Median gap (days)', v: x => st(x).median, s: x => st(x).median < 0 ? '-' : f1(st(x).median) },
      { h: 'Days/play from play #' + N, v: x => st(x).fromN < 0 ? 1e9 : st(x).fromN, s: x => st(x).fromN < 0 ? '-' : st(x).fromN.toFixed(2) },
      { h: 'Most plays in 30 days', v: x => st(x).peak, dir0: -1 },
      { h: 'That peak began', v: x => st(x).peakStart, s: x => fmtD(st(x).peakStart), dir0: -1 },
      { h: 'Plays last 90 days', v: x => st(x).recent, dir0: -1 },
      { h: 'Months played', v: x => st(x).activeRatio, s: x => st(x).active + ' of ' + st(x).totalMonths, dir0: -1 }];
  };

  let current = '';
  const SECTIONS = ['unplayed', 'span', 'streaks', 'patterns', 'loved', 'firstlast', 'unique', 'permonth', 'days', 'spans', 'top', 'sessions', 'rhythm', 'discovery', 'records', 'comebacks', 'burst', 'diversity', 'taste', 'charts'];

  function refresh() {
    if (!current) return;
    const lim = +$('top').value || Infinity;
    const q = $('q').value.trim().toLowerCase();
    // sort the whole list, then apply the search box and the Rows limit
    const view = (key, cols, list) => sortedList(key, cols, list)
      .filter(x => !q || (x.e.sub + ' ' + x.e.name).toLowerCase().includes(q)).slice(0, lim);

    if (current === 'unplayed') {
      const type = $('u_type').value, key = 'unplayed-' + type, cols = unplayedCols(type);
      renderTable($('t_unplayed'), key, cols, view(key, cols, unplayed(type, $('month').value, num('u_min', 1, 1))));
    } else if (current === 'span') {
      const type = $('b_type').value, key = 'span-' + type, cols = spanCols(type);
      const all = $('b_show').value === 'all';
      $('b_note').textContent = all
        ? 'Every separate busy span for each ' + type + ': its busiest span first, then the busiest spans before and after that, and so on. Spans for the same item never overlap or share a play.'
        : 'The single busiest span for each ' + type + '.';
      const floorB = num('b_floor', 2, 1);
      const spanList = span(type, num('b_days', 7, 0.01), num('b_min', 2, 1), all);
      const spanSorted = sortedList(key, cols, spanList);
      const stSpan = sortBy[key];
      const ascSpan = stSpan && cols[stSpan.col] && cols[stSpan.col].h === 'Plays' && stSpan.dir === 1;
      const spanFinal = ascSpan ? spanSorted.filter(x => x.c > floorB) : spanSorted;
      renderTable($('t_span'), key, cols, spanFinal.filter(x => !q || (x.e.sub + ' ' + x.e.name).toLowerCase().includes(q)).slice(0, lim));
    } else if (current === 'streaks') {
      const type = $('k_type').value, kind = $('k_kind').value;
      const minLen = num('k_min', 2, 2);
      const all = $('k_show').value === 'all';
      const list = kind === 'row' ? streaksInRow(type, minLen, all) : streaksDays(type, minLen, all);
      $('k_note').textContent = kind === 'row'
        ? 'Runs of the same ' + type + ' with nothing else scrobbled in between (' + (all ? 'every separate run' : 'best run per ' + type) +
          '). Click a row to select it, shift-click to select a range. A selected row stands for its 2nd scrobble; use the open link (or the list below) to find it on Last.fm and delete it there.'
        : (all ? 'Every separate run of consecutive calendar days for each ' + type + '. Runs for the same item never overlap or repeat the same days.'
               : 'Longest run of consecutive calendar days with at least one play (best run per ' + type + ').');
      const key = 'streaks-' + kind + '-' + type, cols = streakCols(kind, type);
      const floorK = num('k_floor', 2, 2);
      const sortedK = sortedList(key, cols, list);
      const stK = sortBy[key];
      const ascLen = stK && cols[stK.col] && (cols[stK.col].h === 'Plays in a row' || cols[stK.col].h === 'Days in a row') && stK.dir === 1;
      const listFinal = ascLen ? sortedK.filter(x => x.len > floorK) : sortedK;
      renderStreaks(key, cols, listFinal.filter(x => !q || (x.e.sub + ' ' + x.e.name).toLowerCase().includes(q)).slice(0, lim), kind);
    } else if (current === 'patterns') {
      const type = $('p_type').value, key = 'patterns-' + type, cols = patternCols(type);
      renderTable($('t_patterns'), key, cols, view(key, cols, listeningPatterns(type, num('p_min', 5, 1))));
    } else if (current === 'firstlast') {
      const cols = firstLastCols();
      const hit = x => !q || (songText(x.first) + ' ' + songText(x.last)).toLowerCase().includes(q);
      renderTable($('t_firstlast'), 'firstlast', cols, sortedList('firstlast', cols, months().reverse()).filter(hit).slice(0, lim));
    } else if (current === 'unique') {
      const ms = months(), cols = uniqueCols();
      const best = f => {
        const max = Math.max(...ms.map(f));
        const w = ms.filter(m => f(m) === max).map(m => monthName(m.key));
        return max.toLocaleString() + ' in ' + w.slice(0, 3).join(', ') + (w.length > 3 ? ' and ' + (w.length - 3) + ' more' : '');
      };
      $('n_best').textContent = ms.length
        ? 'Most unique tracks in a month: ' + best(m => m.tracks.size) + '. Albums: ' + best(m => m.albums.size) +
          '. Artists: ' + best(m => m.artists.size) + '.'
        : 'No scrobbles in this range.';
      const table = renderTable($('t_unique'), 'unique', cols, sortedList('unique', cols, ms.reverse()).slice(0, lim));
      // Keep this row independent of the date filter: it represents the full Last.fm history.
      const allTime = {
        key: 'All-time', ym: -Infinity, tracks: { size: D.tracks.length },
        albums: { size: D.albums.length }, artists: { size: D.artists.length },
        n: D.total, albumPlays: allAlbumPlays
      };
      const row = table.tBodies[0].insertRow(0);
      row.insertCell().textContent = '—';
      row.insertCell().textContent = 'All-time total';
      [
        allTime.tracks.size, allTime.albums.size, allTime.artists.size, allTime.n,
        avg(allTime.n, allTime.artists.size).toFixed(2),
        avg(allTime.albumPlays, allTime.albums.size).toFixed(2),
        avg(allTime.n, allTime.tracks.size).toFixed(2)
      ].forEach(value => { row.insertCell().textContent = Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 }); });
      row.style.fontWeight = 'bold';
    } else if (current === 'permonth') {
      const ms = monthsFilled(), cols = perMonthCols();
      const total = ms.reduce((a, m) => a + m.n, 0), empty = ms.filter(m => m.n === 0).length;
      $('m_note').textContent = ms.length
        ? total.toLocaleString() + ' scrobbles across ' + ms.length.toLocaleString() + ' months' + (empty ? ' (' + empty + ' with none)' : '') + '.'
        : 'No scrobbles in this range.';
      renderTable($('t_permonth'), 'permonth', cols, sortedList('permonth', cols, ms.reverse()).slice(0, lim));
    } else if (current === 'days') {
      const ds = days(), cols = daysCols();
      const best = f => {
        const max = ds.reduce((m, d) => Math.max(m, f(d)), 0);
        const w = ds.filter(d => f(d) === max).sort((x, y) => y.day - x.day).map(d => fmtDay(d.day));
        return max.toLocaleString() + ' on ' + w.slice(0, 3).join(', ') + (w.length > 3 ? ' and ' + (w.length - 3) + ' more' : '');
      };
      $('d_best').textContent = ds.length
        ? 'Most scrobbles in a day: ' + best(d => d.n) + '. Unique tracks: ' + best(d => d.tracks.size) +
          '. Albums: ' + best(d => d.albums.size) + '. Artists: ' + best(d => d.artists.size) + '.'
        : 'No scrobbles in this range.';
      renderTable($('t_days'), 'days', cols, sortedList('days', cols, ds).slice(0, lim));
    } else if (current === 'spans') {
      const measure = $('w_measure').value, all = $('w_show').value === 'all';
      const dayLen = num('w_days', 7, 0.01), sp = dayLen * 86400, minVal = num('w_min', 1, 1);
      const bests = ['n', 'track', 'album', 'artist'].map(m => {
        const [val, i, j] = bestSpan(lo, hi, sp, m);
        return val ? MEASURE_NAME[m] + ': ' + val.toLocaleString() + ' (' + fmtD(ts[i]) + ' to ' + fmtD(ts[j - 1]) + ')' : null;
      });
      $('w_best').textContent = bests[0]
        ? 'Best ' + dayLen + '-day span for ' + bests.join('; ') + '.'
        : 'No scrobbles in this range.';
      $('w_note').textContent = all
        ? 'Every separate span, ranked by ' + MEASURE_NAME[measure] + ': the busiest first, then the busiest before and after it, and so on. Spans never overlap or share a scrobble.'
        : 'The single busiest span, by ' + MEASURE_NAME[measure] + '.';
      const cols = spansCols();
      const floorW = num('w_floor', 2, 1);
      const sortedW = sortedList('spans', cols, spans(measure, dayLen, minVal, all));
      const stW = sortBy['spans'];
      const MEASURE_HEADS = ['Scrobbles', 'Unique tracks', 'Unique albums', 'Unique artists'];
      const ascVal = stW && stW.dir === 1 && cols[stW.col] && MEASURE_HEADS.includes(cols[stW.col].h);
      const finalW = ascVal ? sortedW.filter(x => x.val > floorW) : sortedW;
      renderTable($('t_spans'), 'spans', cols, finalW.slice(0, lim));
    } else if (current === 'top') {
      const type = $('c_type').value, cal = $('c_cal').checked;
      let bb = $('c_bb').checked;
      $('c_calbox').hidden = !cal;
      if (!N) { $('c_note').textContent = 'No scrobbles yet.'; $('t_top').replaceChildren(); return; }
      const di = getDayIdx();
      let cur, prev = null, curLabel, prevLabel;   // cur / prev = [first day, last day] as day numbers
      if (cal) {
        const unit = $('c_unit').value, v = $('c_period').value;
        if (unit === 'year') {
          cur = yearRange(+v); prev = yearRange(+v - 1); curLabel = v; prevLabel = String(+v - 1);
        } else {
          const ym = +v.slice(0, 4) * 12 + (+v.slice(5, 7) - 1);
          cur = monthRange(ym); prev = monthRange(ym - 1); curLabel = monthName(v); prevLabel = monthName(ymKey(ym - 1));
        }
      } else {
        const f = $('d_from').value, t = $('d_to').value;
        if (f || t) {   // the From/To dates; a missing end means "the first scrobble" / "today"
          let a = f ? dateToIdx(f) : di[0], b = t ? dateToIdx(t) : dayIndex(Date.now() / 1000);
          if (a > b) [a, b] = [b, a];
          const len = b - a + 1;
          cur = [a, b]; prev = [a - len, a - 1];
          curLabel = fmtDay(a) + ' to ' + fmtDay(b) + ' (' + len + ' days)';
          prevLabel = fmtDay(a - len) + ' to ' + fmtDay(a - 1) + ' (' + len + ' days)';
        } else { cur = [di[0], di[N - 1]]; curLabel = 'all time'; }
      }
      const noRange = bb && !prev;
      if (noRange) bb = false;
      const [i0, i1] = idxRange(cur[0], cur[1]);
      const now = chartFor(type, i0, i1);
      const before = bb ? chartFor(type, ...idxRange(prev[0], prev[1])) : null;
      const first = firstIdx(type);
      const list = now.ids.map((id, k) => {
        const e = entName(type, id), rank = k + 1;
        const it = { name: e.name, sub: e.sub, plays: now.cnt[id], rank, prevRank: 0, prevPlays: 0, move: '', moveKey: 0 };
        if (before) {
          it.prevRank = before.rank[id]; it.prevPlays = before.cnt[id];
          if (it.prevRank) { const d = it.prevRank - rank; it.move = d > 0 ? '+' + d : d < 0 ? String(d) : '='; it.moveKey = d; }
          else if (first[id] >= i0) { it.move = 'NEW'; it.moveKey = 1e9; }        // first ever play is in this period
          else { it.move = 'RE'; it.moveKey = 1e9 - 1; }                          // played before, but not in the previous period
        }
        return it;
      });
      $('c_note').textContent = (noRange ? 'Billboard mode needs a period: set the From and To dates above, or tick Calendar. ' : '') +
        'Chart for ' + curLabel + (bb ? ', compared with ' + prevLabel + '. Move: +n up n places, -n down n places, = same place, NEW = first ever play was in this period, RE = played before but not in the previous period. ' : '. ') +
        'Ties: whoever reached that many plays first ranks higher.' + (list.length ? '' : ' No scrobbles in this period.');
      const cols = bb ? [
        { h: 'Rank', v: x => x.rank }, { h: 'Move', v: x => x.moveKey, s: x => x.move, dir0: -1 },
        { h: 'Last period', v: x => x.prevRank || 1e9, s: x => x.prevRank || '-' },
        { h: 'Plays', v: x => x.plays, dir0: -1 }, { h: 'Last period plays', v: x => x.prevPlays, dir0: -1 }, ...chartEnt(type)]
        : [{ h: 'Rank', v: x => x.rank }, { h: 'Plays', v: x => x.plays, dir0: -1 }, ...chartEnt(type)];
      const key = 'top-' + type + (bb ? '-bb' : '');
      const hit = x => !q || (x.sub + ' ' + x.name).toLowerCase().includes(q);
      renderTable($('t_top'), key, cols, sortedList(key, cols, list).filter(hit).slice(0, lim));
    } else if (current === 'sessions') {
      const gapMin = num('se_gap', 30, 1), list = sessions(gapMin), cols = sessionCols();
      if (list.length) {
        const byDur = list.reduce((b, x) => (x.dur > b.dur ? x : b), list[0]), byN = list.reduce((b, x) => (x.n > b.n ? x : b), list[0]);
        $('se_note').textContent = nf(list.length) + ' sessions, ' + f1(list.reduce((a, x) => a + x.n, 0) / list.length) + ' scrobbles on average (median ' + f1(median(list.map(x => x.n))) +
          '). Longest by time: ' + fmtDur(byDur.dur) + ' on ' + fmtD(byDur.start) + '. Most scrobbles: ' + nf(byN.n) + ' on ' + fmtD(byN.start) + '.';
      } else { $('se_note').textContent = 'No scrobbles in this range.'; }
      const key = 'sessions', byTop = x => !q || x.topArtist.toLowerCase().includes(q);
      const ordered = sortedList(key, cols, list.sort((x, y) => y.n - x.n || y.dur - x.dur || x.start - y.start));
      renderTable($('t_sessions'), key, cols, ordered.filter(byTop).slice(0, lim));
    } else if (current === 'rhythm') {
      const rows = rhythm(), n = hi - lo, maxTotal = Math.max(1, ...rows.map(r => r.total));
      if (n) {
        const bh = rows.reduce((b, r) => (r.total > b.total ? r : b), rows[0]);
        const wd = WEEKDAYS.map((w, k) => [w, rows.reduce((a, r) => a + r.d[k], 0)]), bw = wd.reduce((b, x) => (x[1] > b[1] ? x : b), wd[0]);
        const part = (a, b) => rows.slice(a, b).reduce((s, r) => s + r.total, 0) / n * 100;
        $('r_note').textContent = 'Busiest hour of the day: ' + String(bh.hour).padStart(2, '0') + ':00 (' + pct(bh.total / n * 100) + ' of scrobbles). Busiest weekday: ' + bw[0] + ' (' + nf(bw[1]) + ').' +
          ' Night owl (00-05): ' + pct(part(0, 6)) + ', morning (06-11): ' + pct(part(6, 12)) + ', afternoon (12-17): ' + pct(part(12, 18)) + ', evening (18-23): ' + pct(part(18, 24)) +
          '. Weekend: ' + pct((wd[5][1] + wd[6][1]) / n * 100) + '. By weekday: ' + wd.map(x => x[0] + ' ' + nf(x[1])).join(', ') + '.';
      } else { $('r_note').textContent = 'No scrobbles in this range.'; }
      const cols = rhythmCols(maxTotal, Math.max(1, n));
      renderTable($('t_rhythm'), 'rhythm', cols, sortedList('rhythm', cols, rows).slice(0, lim));
    } else if (current === 'discovery') {
      const rows = discoveryRows(), cols = discoveryCols();
      const sum = f => rows.reduce((a, m) => a + f(m), 0), n = sum(m => m.n);
      const bestM = f => { const m = rows.reduce((b, x) => (!b || f(x) > f(b) ? x : b), null); return m ? f(m) + ' in ' + monthName(m.key) : 'none'; };
      $('v_note').textContent = rows.length
        ? 'In this range: ' + nf(sum(m => m.newA)) + ' new artists, ' + nf(sum(m => m.newAl)) + ' new albums and ' + nf(sum(m => m.newT)) + ' new tracks. ' + pct(sum(m => m.newT) / n * 100) +
          ' of your scrobbles were the first-ever play of that track. Most new artists in a month: ' + bestM(m => m.newA) + '. Most new tracks: ' + bestM(m => m.newT) + '.'
        : 'No scrobbles in this range.';
      renderTable($('t_discovery'), 'discovery', cols, sortedList('discovery', cols, rows).slice(0, lim));
    } else if (current === 'records') {
      const cols = [{ h: 'Record', v: x => x.name }, { h: 'Value', v: x => x.value }, { h: 'Details', v: x => x.detail }];
      renderTable($('t_records'), 'records', cols, sortedList('records', cols, records()));
    } else if (current === 'comebacks') {
      const type = $('m_type').value, key = 'comebacks-' + type, cols = comebackCols(type);
      renderTable($('t_comebacks'), key, cols, view(key, cols, comebacks(type, num('m_gap', 180, 1), num('m_after', 5, 1))));
    } else if (current === 'burst') {
      const type = $('g_type').value, key = 'burst-' + type, cols = burstCols(type);
      const floorG = num('g_floor', 3, 1);
      const sortedG = sortedList(key, cols, burstiness(type, num('g_min', 10, 3), num('g_days', 30, 1)));
      const stG = sortBy[key];
      const ascPlaysG = stG && cols[stG.col] && cols[stG.col].h === 'Plays' && stG.dir === 1;
      const finalG = ascPlaysG ? sortedG.filter(x => x.n > floorG) : sortedG;
      renderTable($('t_burst'), key, cols, finalG.filter(x => !q || (x.e.sub + ' ' + x.e.name).toLowerCase().includes(q)).slice(0, lim));
    } else if (current === 'diversity') {
      const rows = diversitySummary() || [];
      const cols = diversityCols();
      renderTable($('t_diversity'), 'diversity', cols, sortedList('diversity', cols, rows).slice(0, lim));
    } else if (current === 'taste') {
      const type = $('td_type').value, N = Math.min(Math.max(Math.round(num('td_n', 20, 1)), 1), 100);
      const rows = tasteDrift(type, N);
      if (!rows.length) {
        $('td_note').textContent = 'Need at least two months with scrobbles in this range.';
        $('t_taste').replaceChildren();
      } else {
        const rhos = rows.map(r => r.rho).filter(x => x !== null);
        const avgRho = rhos.length ? rhos.reduce((a, b) => a + b, 0) / rhos.length : 0;
        const avgJ = rows.reduce((a, r) => a + r.jaccard, 0) / rows.length;
        const avgNov = rows.reduce((a, r) => a + r.novelty, 0) / rows.length;
        $('td_note').textContent = 'Across ' + rows.length + ' month-to-month steps: average Spearman ρ = ' +
          avgRho.toFixed(3) + ' (1 = identical ordering, 0 = unrelated, -1 = reversed), average Jaccard top-' + N +
          ' overlap = ' + (avgJ * 100).toFixed(1) + '%, average novelty share = ' + (avgNov * 100).toFixed(1) + '%.';
        const cols = tasteCols();
        const key = 'taste-' + type + '-' + N;
        renderTable($('t_taste'), key, cols, sortedList(key, cols, rows).slice(0, lim));
      }
    } else if (current === 'charts') {
      renderCharts();
    } else if (current === 'loved') {
      const key = 'loved' + ($('l_x').checked ? '+x' : ''), cols = lovedCols();
      renderTable($('t_loved'), key, cols, view(key, cols, loved($('l_order').value, num('l_days', 1, 0), num('l_min', 1, 1))));
    }
  }

  function route() {
    const h = location.hash.slice(1);
    current = SECTIONS.includes(h) ? h : '';
    SECTIONS.forEach(s => { $('s_' + s).hidden = s !== current; });
    $('common').hidden = !current;
    $('pick').hidden = !!current;
    widgetOn(!current);   // landing page (or #now) shows the widget; opening any stat hides it
    refresh();
  }

  // ---------- setup ----------
  $('title').textContent = D.user + '’s Last.fm stats';
  document.title = D.user + ' – Last.fm stats';
  $('meta').textContent = D.total.toLocaleString() + ' scrobbles · ' + D.artists.length.toLocaleString() + ' artists · ' +
    D.albums.length.toLocaleString() + ' albums · ' + D.tracks.length.toLocaleString() + ' tracks · ' +
    D.loved_total.toLocaleString() + ' loved · updated ' + D.updated + ' · dates in ' + tz;
  $('l_note').textContent = D.loved.length.toLocaleString() + ' of ' + D.loved_total.toLocaleString() +
    ' loved tracks have scrobbles. Days since first play ÷ total plays.';

  const nowKey = monthKey(Date.now() / 1000);
  const monthSet = new Set(mk);
  monthSet.add(nowKey);
  [...monthSet].sort().reverse().forEach(m => {
    const o = document.createElement('option'); o.value = m; o.textContent = monthName(m); $('month').appendChild(o);
  });
  $('month').value = nowKey;
  fillPeriods();
  build();

  ['top', 'q', 'u_type', 'month', 'u_min', 'b_type', 'b_days', 'b_show', 'b_min', 'c_type', 'c_bb', 'c_cal', 'c_period', 'w_days', 'w_measure', 'w_show', 'w_min', 'k_type', 'k_kind', 'k_show', 'k_min', 'p_type', 'p_min', 'l_order', 'l_days', 'l_min', 'l_x', 'l_from', 'se_gap', 'm_type', 'm_gap', 'm_after', 'g_type', 'g_min', 'g_days', 'dv_type', 'dv_min', 'td_type', 'td_n',
   'ch_dim', 'ch_measure', 'ch_sort', 'ch_cum', 'ch_top', 'ch_width',
   'ch_range', 'ch_year', 'ch_from', 'ch_to', 'ch_q', 'ch_dir', 'ch_log', 'ch_scale_mode', 'ch_scale_max', 'ch_wide',
   'b_floor', 'k_floor', 'w_floor', 'g_floor']
    .forEach(id => $(id).addEventListener('input', refresh));
  $('k_lib').href = 'https://www.last.fm/user/' + encodeURIComponent(D.user) + '/library';
  $('k_all').addEventListener('click', () => { shownRows.forEach(r => { r.cb.checked = true; picked.add(r.key); }); updateSel(); });
  $('k_none').addEventListener('click', () => { picked.clear(); shownRows.forEach(r => { r.cb.checked = false; }); updateSel(); });
  $('k_csv').addEventListener('click', () => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csvText()], { type: 'text/csv' }));
    a.download = 'scrobbles-to-delete.csv';
    a.click();
  });
  ['d_from', 'd_to'].forEach(id => $(id).addEventListener('input', () => { applyRange(); refresh(); }));
  $('d_clear').addEventListener('click', () => { $('d_from').value = ''; $('d_to').value = ''; applyRange(); refresh(); });
  $('c_unit').addEventListener('input', () => { fillPeriods(); refresh(); });
  window.addEventListener('hashchange', route);
  route();
})();
</script>
</body></html>
"""


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"lastfm_sync v{VERSION}")
    ap.add_argument("--user", default=os.environ.get("LASTFM_USER"))
    ap.add_argument("--key", default=os.environ.get("LASTFM_KEY"))
    ap.add_argument("--db", default="lastfm.db", help="SQLite file (keep it OUTSIDE your web folder)")
    ap.add_argument("--out", default="index.html", help="where to write the page; data.json goes next to it")
    ap.add_argument("--tz", default=None, help="e.g. America/Costa_Rica (default: each visitor's browser timezone)")
    ap.add_argument("--no-sync", action="store_true", help="skip Last.fm, just rebuild the page")
    ap.add_argument("--full", action="store_true",
                    help="re-download your whole history and remove scrobbles deleted on Last.fm")
    ap.add_argument("--prune", nargs="?", const="all", default=None, metavar="PERIOD",
                    help="ONLY remove scrobbles deleted on Last.fm, then rebuild the page. Optionally give a period to "
                         "check just that time: 2026, 09/2026, 2026-09, 15/09/2026, 2026-09-15 or 2026-09-01..2026-10-15")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --prune: list what would be removed, but delete nothing")
    ap.add_argument("--allow-big-delete", action="store_true",
                    help="allow removing more than 20%% of your saved scrobbles in one go")
    args = ap.parse_args()

    if not args.no_sync and not (args.user and args.key):
        sys.exit("Need --user and --key (or LASTFM_USER / LASTFM_KEY), or use --no-sync.")
    if not args.user:
        args.user = "me"

    tz = ZoneInfo(args.tz) if args.tz else datetime.now().astimezone().tzinfo

    db = sqlite3.connect(args.db)
    db.execute("""CREATE TABLE IF NOT EXISTS scrobbles
                  (uts INTEGER NOT NULL, artist TEXT NOT NULL, track TEXT NOT NULL, album TEXT,
                   PRIMARY KEY (uts, artist, track))""")
    if "album" not in [r[1] for r in db.execute("PRAGMA table_info(scrobbles)")]:
        db.execute("ALTER TABLE scrobbles ADD COLUMN album TEXT")  # database from an older version
        db.commit()
    db.execute("""CREATE TABLE IF NOT EXISTS loved
                  (artist TEXT NOT NULL, track TEXT NOT NULL, PRIMARY KEY (artist, track))""")

    if args.dry_run and not args.prune:
        sys.exit("--dry-run only works together with --prune.")

    if args.prune:
        if args.no_sync:
            sys.exit("--prune needs to talk to Last.fm, so it can't be combined with --no-sync.")
        window = None
        if args.prune != "all":
            window = parse_period(args.prune, tz)
            fmt = "%Y-%m-%d %H:%M"
            print(f"Checking only {datetime.fromtimestamp(window[0], tz).strftime(fmt)} to "
                  f"{datetime.fromtimestamp(window[1], tz).strftime(fmt)} ({args.tz or 'server time'}).")
        full_pass(db, args.user, args.key, args.allow_big_delete, args.dry_run, window)
        if args.dry_run:
            return  # nothing changed, so no need to rebuild the page
    elif not args.no_sync:
        sync_scrobbles(db, args.user, args.key, full=args.full, allow_big_delete=args.allow_big_delete)
        sync_loved(db, args.user, args.key)

    updated = datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")
    data = build_data(db, args.user, args.tz, updated)
    out_dir = os.path.dirname(os.path.abspath(args.out))
    write_atomic(os.path.join(out_dir, "data.json"), json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    write_atomic(args.out, PAGE.replace("__WIDGET_KEY__", WIDGET_API_KEY))
    print(f"Wrote {args.out} and data.json ({data['total']:,} scrobbles, {len(data['artists']):,} artists, "
          f"{len(data['albums']):,} albums, {len(data['tracks']):,} tracks)")


if __name__ == "__main__":
    main()
