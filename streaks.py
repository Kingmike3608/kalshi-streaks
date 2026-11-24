"""Daily 100% streaks page for Kalshi lines (NBA, NHL, NFL, EPL, La Liga, Ligue 1, MLS).

Takes every open Kalshi player-prop market for today's games (the next 7 days for the NFL),
checks each player's ESPN game log, and keeps the highest Kalshi line the player has hit
in every one of their last N games. It also checks every game against today's opponent
over the last three seasons (H2H). Soccer streaks use Kalshi's team-goal and match-goal
lines over the next two weeks. A Games view adds each team's last-10 scoring ranges next
to Kalshi's game total. Writes streaks.html next to this file.

  python3 streaks.py             # rebuild today's page
  python3 streaks.py --out PATH  # write it somewhere else (the app's daily job does this)
  python3 streaks.py --if-due     # rebuild only if a RUN_HOURS slot passed since the last good build
  python3 streaks.py --selftest  # check the streak math
"""
import json
import math
import re
import sys
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_LOG = "https://site.web.api.espn.com/apis/common/v3/sports"
OUT = Path(__file__).with_name("streaks.html")
MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}


def first(*names):
    return lambda g: g.get(names[0])


def total(*names):
    return lambda g: sum(g.get(n, 0) for n in names) if any(n in g for n in names) else None


# series -> (label, stat from one game log row); a stat of None means the player has no such column
LEAGUES = {
    "NBA": dict(path="basketball/nba", code="NBA", days=1, min_games=5, min_h2h=3, series={
        "KXNBAPTS": ("Points", first("points")),
        "KXNBAREB": ("Rebounds", first("totalRebounds")),
        "KXNBAAST": ("Assists", first("assists")),
        "KXNBA3PT": ("3PTM", first("threePointFieldGoalsMade")),
    }),
    "NHL": dict(path="hockey/nhl", code="NHL", days=1, min_games=5, min_h2h=3, series={
        "KXNHLGOAL": ("Goals", first("goals")),
        "KXNHLAST": ("Assists", first("assists")),
        "KXNHLPTS": ("Points", first("points")),
    }),
    "NFL": dict(path="football/nfl", code="NFL", days=7, min_games=4, min_h2h=2, series={
        "KXNFLPASSYDS": ("Pass Yds", first("passingYards")),
        "KXNFLRSHYDS": ("Rush Yds", first("rushingYards")),
        "KXNFLRECYDS": ("Rec Yds", first("receivingYards")),
        "KXNFLREC": ("Receptions", first("receptions")),
        "KXNFLTD": ("TDs", total("rushingTouchdowns", "receivingTouchdowns")),
    }),
    # Soccer is team goals only; Kalshi lists those lines a few days before kickoff, hence the 2-week window.
    "EPL": dict(path="soccer/eng.1", code="EPL", days=14, min_games=4, soccer=True, series={}),
    "La Liga": dict(path="soccer/esp.1", code="LALIGA", days=14, min_games=4, soccer=True, series={}),
    "Ligue 1": dict(path="soccer/fra.1", code="LIGUE1", days=14, min_games=4, soccer=True, series={}),
    "MLS": dict(path="soccer/usa.1", code="MLS", days=14, min_games=4, soccer=True, series={}),
}
NFL_TABS = ("QB", "RB", "WR", "TE")
SOCCER_TABS = ("Team Goals", "Match Goals")
RUN_HOURS = (10, 16)  # local (Mac clock) times the page is refreshed; a missed or failed slot is retried
INSIGHT_GAMES = 10  # the Games view summarizes each team's last 10 completed games
H2H_SEASONS = 3  # head-to-head looks at this season and the two before it, playoffs included


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "streaks/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def norm(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    name = re.sub(r"[^a-z ]", "", name)
    return " ".join(w for w in name.split() if w not in ("jr", "sr", "ii", "iii", "iv"))


def event_date(event_ticker):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    return datetime(2000 + int(m[1]), MONTHS[m[2]], int(m[3])).date() if m else None


def markets(series, today, days):
    out, cursor = [], ""
    while True:
        d = get(f"{KALSHI}/markets?series_ticker={series}&status=open&limit=1000&cursor={cursor}")
        for m in d.get("markets", []):
            day = event_date(m["event_ticker"])
            if day and today <= day < today + timedelta(days=days):
                out.append(m)
        cursor = d.get("cursor")
        if not cursor:
            return out


def espn_games(path, today, days):
    """ESPN games in the window: [{id, date, teams: [{id, abbr, name, short, logo}, ...]}]."""
    games = []
    for i in range(days):
        day = (today + timedelta(days=i)).strftime("%Y%m%d")
        for e in get(f"{ESPN}/{path}/scoreboard?dates={day}").get("events", []):
            teams = [dict(id=c["team"]["id"], abbr=c["team"].get("abbreviation", ""), name=c["team"].get("displayName", ""),
                          short=c["team"].get("shortDisplayName", ""), logo=c["team"].get("logo", ""))
                     for c in e["competitions"][0]["competitors"]]
            if len(teams) == 2:
                games.append(dict(id=e["id"], date=e["date"], teams=teams))
    return games


def roster_index(path, games):
    """Normalized name -> player info (incl. this window's opponent), for every team playing in the window."""
    opp = {}  # team id -> (opponent id, opponent abbreviation)
    for g in games:
        a, b = g["teams"]
        opp.setdefault(a["id"], (b["id"], b["abbr"]))
        opp.setdefault(b["id"], (a["id"], a["abbr"]))
    index = {}
    for d in ThreadPoolExecutor(8).map(lambda t: get(f"{ESPN}/{path}/teams/{t}/roster"), opp):
        team = d.get("team", {})
        logo = (team.get("logo") or (team.get("logos") or [{}])[0].get("href", ""))
        groups = d.get("athletes", [])
        people = [p for g in groups for p in g.get("items", [])] if groups and "items" in groups[0] else groups
        opp_id, opp_abbr = opp.get(team.get("id"), ("", ""))
        for p in people:
            index[norm(p["displayName"])] = dict(id=p["id"], name=p["displayName"], team=team.get("abbreviation", ""),
                                                logo=logo, pos=(p.get("position") or {}).get("abbreviation", ""),
                                                opp_id=opp_id, opp=opp_abbr, season=d.get("season", {}).get("year"))
    return index


def game_log(path, athlete_id, season=None):
    """Regular-season and playoff games, most recent first, as {stat name: number, "_opp": id, "_type": name}."""
    d = get(f"{ESPN_LOG}/{path}/athletes/{athlete_id}/gamelog" + (f"?season={season}" if season else ""))
    names, meta, games = d.get("names", []), d.get("events", {}), []
    for st in d.get("seasonTypes", []):
        if "Preseason" in st.get("displayName", ""):
            continue
        for cat in st.get("categories", []):
            for e in cat.get("events", []):
                info = meta.get(e["eventId"], {})
                row = {"_opp": info.get("opponent", {}).get("id"), "_type": st.get("displayName", "")}
                for n, v in zip(names, e["stats"]):
                    try:
                        row[n.split("-")[0]] = float(v.split("-")[0])  # "made-attempted": "3-7" -> made = 3
                    except ValueError:
                        pass
                games.append((info.get("gameDate", ""), row))
    return [g for _, g in sorted(games, key=lambda x: x[0], reverse=True)]


def score(competitor):
    sc = competitor.get("score")
    try:
        return int(float(sc["value"] if isinstance(sc, dict) else sc))
    except (TypeError, ValueError, KeyError):
        return None


def team_games(path, team_id):
    """This season's completed games, newest first: {scored, allowed, total, opp}."""
    out = []
    for e in get(f"{ESPN}/{path}/teams/{team_id}/schedule").get("events", []):
        c = e["competitions"][0]
        if not c.get("status", {}).get("type", {}).get("completed"):
            continue
        me = [x for x in c["competitors"] if x["team"]["id"] == team_id]
        them = [x for x in c["competitors"] if x["team"]["id"] != team_id]
        if me and them and score(me[0]) is not None and score(them[0]) is not None:
            s, a = score(me[0]), score(them[0])
            out.append((e["date"], dict(scored=s, allowed=a, total=s + a, opp=them[0]["team"]["id"])))
    return [g for _, g in sorted(out, key=lambda x: x[0], reverse=True)]


def kalshi_teams(code, today, days):
    """Kalshi team code -> team name, from the league's game-winner markets."""
    return {m["ticker"].rsplit("-", 1)[1]: m.get("yes_sub_title") or ""
            for m in markets(f"KX{code}GAME", today, days) if not m["ticker"].endswith("-TIE")}


def match_team(code, name, teams):
    """The ESPN team for a Kalshi code: same abbreviation, else the one sharing the most name words (no ties)."""
    for t in teams:
        if t["abbr"] == code:
            return t
    words = set(norm(name).split())
    ranked = sorted(((len(words & set(norm(t["name"] + " " + t["short"]).split())), i) for i, t in enumerate(teams)),
                    reverse=True)
    if ranked and ranked[0][0] and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        return teams[ranked[0][1]]
    return None


def game_codes(event_ticker, codes):
    """'KXEPLTOTAL-26NOV23BREEVE' -> ('BRE', 'EVE') using the league's known Kalshi codes."""
    m = re.search(r"-\d{2}[A-Z]{3}\d{2}([A-Z0-9]+)$", event_ticker)
    key = m[1] if m else ""
    for i in range(2, len(key) - 1):
        if key[:i] in codes and key[i:] in codes:
            return key[:i], key[i:]
    return None


def over_line(m):
    """Kalshi 'Over 248.5' market -> 249 (the score needed), from its strike or ticker suffix."""
    strike = m.get("floor_strike")
    return math.floor(float(strike)) + 1 if strike is not None else int(re.sub(r"^[A-Z]+", "", m["ticker"].rsplit("-", 1)[1]))


def streak(values, line):
    """Consecutive most recent games at or above line."""
    n = 0
    for v in values:
        if v is None or v < line:
            break
        n += 1
    return n


def best_line(values, lines, min_games):
    """Highest line hit in the last min_games+ games in a row: (line, streak) or None."""
    for line in sorted(lines, reverse=True):
        s = streak(values, line)
        if s >= min_games:
            return line, s
    return None


def build(today):
    rows, games, failed = [], [], 0
    for league, cfg in LEAGUES.items():
        try:
            r, g = build_league(league, cfg, today)
        except Exception as e:  # one league's outage shouldn't blank the page
            print(f"{league}: skipped ({type(e).__name__}: {e})", file=sys.stderr)
            failed += 1
            continue
        rows += r
        games += g
    if failed == len(LEAGUES):  # offline: keep the last good page instead of writing an empty one
        raise RuntimeError("every league failed; page left unchanged")
    rows.sort(key=lambda r: (-r["streak"], -r["line"]))
    games.sort(key=lambda g: g["date"])
    return rows, games


def build_league(league, cfg, today):
    games = espn_games(cfg["path"], today, cfg["days"])
    if not games:
        return [], []
    rows = player_rows(league, cfg, today, games) if cfg["series"] else []
    teams = [t for g in games for t in g["teams"]]

    def history(t):
        try:
            return t["id"], team_games(cfg["path"], t["id"])
        except Exception as e:
            print(f"  no schedule for {t['name']}: {e}", file=sys.stderr)
            return t["id"], []
    hist = dict(ThreadPoolExecutor(8).map(history, teams))
    codes = kalshi_teams(cfg["code"], today, cfg["days"])

    def find_game(event_ticker):
        pair = game_codes(event_ticker, codes)
        ids = {t["id"] for c in pair or () if (t := match_team(c, codes[c], teams))}
        options = [g for g in games if len(ids) == 2 and {t["id"] for t in g["teams"]} == ids]
        day = str(event_date(event_ticker))
        return next((g for g in options if local_day(g["date"]) == day), options[0] if options else None)

    totals = {}  # ESPN game id -> {goals/points needed: market}
    for m in markets(f"KX{cfg['code']}TOTAL", today, cfg["days"]):
        g = find_game(m["event_ticker"])
        if g:
            totals.setdefault(g["id"], {})[over_line(m)] = m
    by_id = {g["id"]: g for g in games}

    def team_row(tab, label, g, t, line, n, m):
        other = next(x for x in g["teams"] if x["id"] != t["id"])
        return dict(kind="form", league=league, tab=tab, name=t["name"], team=t["abbr"], opp=other["abbr"],
                    logo=t["logo"], label=label, line=line, streak=n, ticker=m["ticker"], is_team=True,
                    ask=float(m.get("yes_ask_dollars") or 0), day=local_day(g["date"]))
    if cfg.get("soccer"):
        team_lines = {}  # (game id, team id) -> {goals needed: market}
        for m in markets(f"KX{cfg['code']}TEAMTOTAL", today, cfg["days"]):
            g = find_game(m["event_ticker"])
            code = re.match(r"[A-Z]+", m["ticker"].rsplit("-", 1)[1])
            t = g and code and code[0] in codes and match_team(code[0], codes[code[0]], g["teams"])
            if t:
                team_lines.setdefault((g["id"], t["id"]), {})[over_line(m)] = m
        for (gid, tid), by_line in team_lines.items():
            hit = best_line([x["scored"] for x in hist.get(tid, [])], by_line, cfg["min_games"])
            if hit:
                t = next(x for x in by_id[gid]["teams"] if x["id"] == tid)
                rows.append(team_row("Team Goals", "Goals", by_id[gid], t, hit[0], hit[1], by_line[hit[0]]))
        for gid, by_line in totals.items():
            for t in by_id[gid]["teams"]:
                hit = best_line([x["total"] for x in hist.get(t["id"], [])], by_line, cfg["min_games"])
                if hit:
                    rows.append(team_row("Match Goals", "Match Goals", by_id[gid], t, hit[0], hit[1], by_line[hit[0]]))
    print(f"{league}: {len(games)} games, {len(totals)} with a Kalshi total", file=sys.stderr)
    return rows, [insight(league, g, hist, totals.get(g["id"], {})) for g in games]


def local_day(iso):
    """ESPN UTC time -> US Eastern date string, the calendar Kalshi tickers use."""
    return str(datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).date())


def spread(values):
    return dict(avg=round(sum(values) / len(values), 1), min=min(values), max=max(values)) if values else None


def insight(league, g, hist, by_line):
    """Last-10 scoring ranges for both teams, next to Kalshi's main game total (the line priced nearest 50¢)."""
    main = min(by_line.items(), key=lambda kv: abs(float(kv[1].get("yes_ask_dollars") or 0) - 0.5)) if by_line else None
    sides = []
    for t in g["teams"]:
        last = hist.get(t["id"], [])[:INSIGHT_GAMES]
        sides.append(dict(abbr=t["abbr"], name=t["name"], logo=t["logo"], n=len(last),
                          scored=spread([x["scored"] for x in last]), allowed=spread([x["allowed"] for x in last]),
                          total=spread([x["total"] for x in last]),
                          over=sum(x["total"] >= main[0] for x in last) if main else None))
    a, b = sides
    proj = (round((a["scored"]["avg"] + b["allowed"]["avg"] + b["scored"]["avg"] + a["allowed"]["avg"]) / 2, 1)
            if all(s["scored"] for s in sides) else None)
    return dict(league=league, date=g["date"], teams=sides, proj=proj, line=main[0] if main else None,
                ask=float(main[1].get("yes_ask_dollars") or 0) if main else None, ticker=main[1]["ticker"] if main else None)


def player_rows(league, cfg, today, games):
    rows = []
    by_player = {}
    for series, (label, stat) in cfg["series"].items():
        for m in markets(series, today, cfg["days"]):
            name = m["title"].split(":")[0].strip()
            line = int(m["ticker"].rsplit("-", 1)[1])
            by_player.setdefault(norm(name), {}).setdefault(series, {})[line] = m
    if not by_player:
        return rows
    index = roster_index(cfg["path"], games)
    found = {k: index[k] for k in by_player if k in index}
    print(f"{league}: {len(by_player)} players with props, {len(found)} matched to ESPN", file=sys.stderr)

    def logs(item):
        key, p = item
        try:
            current = game_log(cfg["path"], p["id"])
        except Exception as e:  # one bad log shouldn't sink the page
            print(f"  skip {p['name']}: {e}", file=sys.stderr)
            return key, [], []
        past = []
        for back in range(1, H2H_SEASONS if p["season"] else 1):
            try:
                past += game_log(cfg["path"], p["id"], p["season"] - back)
            except Exception:  # rookies have no older seasons (ESPN 404s)
                break
        return key, current, current + past  # both lists are newest first
    for key, current, everything in ThreadPoolExecutor(12).map(logs, found.items()):
        p = found[key]
        form = [g for g in current if "Regular Season" in g["_type"]]
        h2h = [g for g in everything if p["opp_id"] and g["_opp"] == p["opp_id"]]
        for series, by_line in by_player[key].items():
            label, stat = cfg["series"][series]
            tab = label if league != "NFL" else (p["pos"] if p["pos"] in NFL_TABS else "")
            for kind, played, min_games in (("form", form, cfg["min_games"]), ("h2h", h2h, cfg["min_h2h"])):
                hit = best_line([stat(g) for g in played], by_line, min_games)
                if not hit:
                    continue
                line, n = hit
                m = by_line[line]
                rows.append(dict(kind=kind, league=league, tab=tab, name=p["name"], team=p["team"], opp=p["opp"],
                                 logo=p["logo"], label=label, line=line, streak=n, ticker=m["ticker"],
                                 ask=float(m.get("yes_ask_dollars") or 0), day=str(event_date(m["event_ticker"]))))
    return rows


def render(rows, games, today, built):
    sub = {lg: list(NFL_TABS if lg == "NFL" else SOCCER_TABS if c.get("soccer") else (v[0] for v in c["series"].values()))
           for lg, c in LEAGUES.items()}
    data = json.dumps(dict(rows=rows, games=games, sub=sub, leagues=list(LEAGUES),
                           today=today.strftime("%A · %b %-d").upper(), built=built))
    return TEMPLATE.replace("__DATA__", data.replace("</", "<\\/"))


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>100% Streaks</title>
<style>
:root{--bg:#141414;--card:#1c1c1c;--line:#2a2a2a;--text:#f2f2f2;--mut:#8a8a8a;--green:#4ade80}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.4 -apple-system,BlinkMacSystemFont,"Inter",system-ui,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px 48px}
.date{color:var(--mut);letter-spacing:.12em;font-size:13px;font-weight:600}
h1{font-size:clamp(40px,9vw,72px);line-height:1;margin:10px 0 6px;font-weight:800;letter-spacing:-.02em}
.kick{font-size:13px;letter-spacing:.12em;font-weight:600;color:var(--mut)}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:22px 0 10px}
.tabs button{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:999px;padding:7px 14px;font:inherit;font-size:14px;cursor:pointer}
.tabs button[aria-pressed=true]{border-color:var(--green);color:var(--green)}
.sub button{font-size:13px;padding:5px 12px}
.card{background:var(--card);border-radius:20px;padding:6px 20px;margin-top:10px}
.row{display:flex;align-items:center;gap:14px;padding:12px 0;border-bottom:1px solid var(--line);cursor:pointer}
.row:last-child{border-bottom:0}
.row img{width:40px;height:40px;object-fit:contain;flex:none}
.who{font-weight:600;font-size:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.who .sep{color:var(--mut);font-weight:400}.who .prop{color:var(--green)}
.meta{font-size:12px;color:var(--mut)}
.dots{flex:1;border-bottom:2px dotted var(--line);min-width:10px;align-self:center}
.n{font-weight:800;font-size:22px;white-space:nowrap}.n small{font-size:11px;letter-spacing:.12em;color:var(--mut);font-weight:600;margin-left:4px}
.ask{font-size:13px;color:var(--mut);white-space:nowrap;width:58px;text-align:right}
.dot{width:12px;height:12px;border-radius:50%;background:var(--green);flex:none}
.empty{color:var(--mut);padding:24px 0;text-align:center}
.qp{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:14px 20px;margin-top:10px}
.qp ol{margin:10px 0;padding-left:20px}.qp li{margin:4px 0}.qp a{color:var(--green)}
.qp select,.qp .btn{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:999px;padding:5px 12px;font:inherit;font-size:14px;cursor:pointer;text-decoration:none;display:inline-block}
.qp .btn.go{border-color:var(--green);color:var(--green)}
.foot{color:var(--mut);font-size:12px;margin-top:16px}
.game{padding:14px 0;border-bottom:1px solid var(--line)}.game:last-child{border-bottom:0}
.gh{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.gh b{font-size:17px}.gh .line{color:var(--green);font-size:14px;font-weight:600}
.scroll{overflow-x:auto}
.game table{border-collapse:collapse;width:100%;font-size:14px;min-width:480px}
.game th{color:var(--mut);font-size:11px;letter-spacing:.08em;font-weight:600;text-align:right;padding:4px 6px}
.game th:first-child,.game td:first-child{text-align:left}
.game td{text-align:right;padding:6px;white-space:nowrap;font-variant-numeric:tabular-nums}
.game td small{color:var(--mut)}.game td img{width:22px;height:22px;object-fit:contain;vertical-align:middle;margin-right:6px}
@media (max-width:560px){.dots,.ask{display:none}.who{font-size:15px}.n{font-size:18px}}
</style></head><body><div class="wrap">
<div class="date" id="date"></div>
<h1><span id="pre">100%</span><br><span id="title">Streaks</span></h1>
<div class="kick" id="kick"></div>
<div class="tabs" id="kind" role="group" aria-label="Streak type"></div>
<div class="tabs" id="tabs" role="group" aria-label="League"></div>
<div class="tabs sub" id="sub" role="group" aria-label="Stat"></div>
<div class="qp" id="qp"></div>
<div class="card" id="list"></div>
<div class="foot" id="foot"></div>
</div>
<script>
const D=__DATA__;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let kind='form',league='All',tab='',qpN=4;
const TOKEN=new URLSearchParams(location.search).get('token');
const KINDS={form:'Recent form',h2h:'H2H vs opponent',games:'Games'};
document.getElementById('date').textContent=D.today;
function nm(r){return r.is_team?r.name:short(r.name)}
function short(n){const p=n.split(' ');return p.length>1?p[0][0]+'. '+p.slice(1).join(' '):n}
function btns(el,items,cur,fn){el.innerHTML=items.map(t=>`<button aria-pressed="${t===cur}">${esc(t||'All')}</button>`).join('');
  [...el.children].forEach((b,i)=>b.onclick=()=>fn(items[i]))}
// shortcut: the middle URL segment is Kalshi's cosmetic slug (not in the API); if links 404, open the event from Kalshi search
function kalshiUrl(t){const p=t.toLowerCase().split('-');return `https://kalshi.com/markets/${p[0]}/${p[0]}/${p[0]}-${p[1]}`}
function quick(rows){
  const seen=new Set(),legs=[];
  for(const r of rows){if(legs.length>=qpN)break;if(!r.ask||seen.has(r.name))continue;seen.add(r.name);legs.push(r)}
  const el=document.getElementById('qp');
  const pick=`<select id="qpn" aria-label="Number of legs">${[2,3,4,5,6].map(n=>`<option${n===qpN?' selected':''}>${n}</option>`).join('')}</select>`;
  if(legs.length<2){el.innerHTML=`<b>Quick parlay</b> · top ${pick} legs · not enough legs with a YES price in this view.`}
  else{
    const price=legs.reduce((p,r)=>p*r.ask,1),tickers=legs.map(r=>r.ticker).join(',');
    el.innerHTML=`<b>Quick parlay</b> · top ${pick} legs · about ${(price*100).toFixed(1)}¢ · pays ${(1/price).toFixed(2)}×
    <ol>${legs.map(r=>`<li>${esc(nm(r))} ${r.line}+ ${esc(r.label)} · YES ${Math.round(r.ask*100)}¢ · <a href="${kalshiUrl(r.ticker)}" target="_blank" rel="noopener">Kalshi ↗</a></li>`).join('')}</ol>
    ${TOKEN?`<a class="btn go" href="/?token=${encodeURIComponent(TOKEN)}#parlay=${tickers}" target="_blank">Open as parlay in Kalshi Helper</a> `:''}<button class="btn" id="qpcopy">Copy tickers</button>
    <div class="meta" style="margin-top:8px">Price multiplies the legs' YES prices as if independent. The real combo price is Kalshi's quote, and Kalshi only combines legs from supported collections.</div>`;
    document.getElementById('qpcopy').onclick=e=>{navigator.clipboard&&navigator.clipboard.writeText(tickers.split(',').join(' '));e.target.textContent='Copied'};
  }
  document.getElementById('qpn').onchange=e=>{qpN=+e.target.value;draw()};
}
function draw(){
  btns(document.getElementById('kind'),Object.values(KINDS),KINDS[kind],v=>{kind=Object.keys(KINDS).find(k=>KINDS[k]===v);draw()});
  document.getElementById('kick').textContent={h2h:'↑ KALSHI LINES + HEAD-TO-HEAD (LAST 3 SEASONS)',form:'↑ KALSHI LINES + RECENT FORM',games:'↑ LAST 10 GAMES + KALSHI GAME TOTALS'}[kind];
  btns(document.getElementById('tabs'),['All',...D.leagues],league,v=>{league=v;tab='';draw()});
  btns(document.getElementById('sub'),league==='All'||kind==='games'?[]:['',...D.sub[league]],tab,v=>{tab=v;draw()});
  document.getElementById('pre').textContent=kind==='games'?'Today\u2019s':'100%';
  document.getElementById('title').textContent=(league==='All'?'':league+' ')+{h2h:'H2H Streaks',form:'Streaks',games:'Games'}[kind];
  document.getElementById('qp').hidden=kind==='games';
  if(kind==='games')return drawGames();
  const rows=D.rows.filter(r=>r.kind===kind&&(league==='All'||r.league===league)&&(!tab||r.tab===tab));
  quick(rows);
  const list=document.getElementById('list');
  list.innerHTML=rows.length?rows.map(r=>`<div class="row" title="Click to copy ${esc(r.ticker)}" data-t="${esc(r.ticker)}">
    <img src="${esc(r.logo)}" alt="${esc(r.team)}" loading="lazy">
    <div style="min-width:0"><div class="who">${esc(nm(r))} <span class="sep">:</span> <span class="prop">${r.line}+ ${esc(r.label)}</span></div>
    <div class="meta">${esc(r.league)} · ${esc(r.team)} vs ${esc(r.opp)}${r.league==='NFL'?' · '+esc(r.day):''}</div></div>
    <div class="dots"></div><div class="n">${r.streak}/${r.streak}<small>GAMES</small></div>
    <div class="ask">${r.ask?'YES '+Math.round(r.ask*100)+'¢':'no ask'}</div><span class="dot" aria-hidden="true"></span></div>`).join('')
    :`<div class="empty">${league!=='All'&&D.sub[league].includes('Team Goals')&&kind==='form'?'No soccer streaks yet. Kalshi lists team-goal and match-goal lines a few days before kickoff.':'No 100% streaks on Kalshi lines for this slate.'}</div>`;
  list.querySelectorAll('.row').forEach(el=>el.onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText(el.dataset.t);el.querySelector('.meta').textContent='Copied '+el.dataset.t});
}
const n=v=>Number.isInteger(v)?v:v.toFixed(1);
const rng=x=>x?`${n(x.avg)} <small>(${x.min}–${x.max})</small>`:'–';
function drawGames(){
  const games=D.games.filter(g=>league==='All'||g.league===league);
  const list=document.getElementById('list');
  list.innerHTML=games.length?games.map(g=>`<div class="game">
    <div class="gh"><div><b>${g.teams.map(t=>esc(t.abbr)).join(' vs ')}</b> <span class="meta">${esc(g.league)} · ${new Date(g.date).toLocaleString([],{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}</span></div>
    <div class="line">${g.line!=null?`Kalshi total: over ${g.line-0.5} · YES ${Math.round(g.ask*100)}¢`:'No open Kalshi total'}${g.proj!=null?` · projected ${n(g.proj)}`:''}</div></div>
    <div class="scroll"><table><thead><tr><th>LAST ${g.teams[0].n||''} GAMES</th><th>SCORED</th><th>ALLOWED</th><th>GAME TOTAL</th>${g.line!=null?`<th>OVER ${g.line-0.5}</th>`:''}</tr></thead><tbody>
    ${g.teams.map(t=>`<tr><td><img src="${esc(t.logo)}" alt="" loading="lazy">${esc(t.name)}</td><td>${rng(t.scored)}</td><td>${rng(t.allowed)}</td><td>${rng(t.total)}</td>${g.line!=null?`<td>${t.over}/${t.n}</td>`:''}</tr>`).join('')}
    </tbody></table></div></div>`).join('')
    :'<div class="empty">No games in this window.</div>';
}
document.getElementById('foot').textContent=`Updated ${D.built}. Recent form = this season's most recent games in a row at or above the Kalshi line. H2H = most recent games in a row vs today's opponent over the last 3 seasons, playoffs included. Highest qualifying line shown. Soccer streaks use Kalshi team-goal and match-goal lines, which Kalshi lists a few days before kickoff. Games shows average (min–max) over each team's last 10 games; projected total blends each side's scoring with the other's allowed. Click a row to copy its Kalshi ticker. Not a prediction.`;
draw();
</script></body></html>
"""


def selftest():
    assert streak([5, 6, 4, 9], 4) == 4
    assert streak([5, 3, 9], 4) == 1
    assert streak([None, 9], 1) == 0
    assert best_line([12, 15, 11, 14, 13, 2], [10, 15, 20], 5) == (10, 5)
    assert best_line([12, 15, 11], [10], 5) is None
    assert total("a", "b")({"a": 1}) == 1 and total("a", "b")({}) is None
    assert event_date("KXNBAPTS-26NOV11ORLUTA").isoformat() == "2026-11-11"
    assert norm("Luka Dončić Jr.") == "luka doncic"
    codes = {"BRE": "Brentford", "EVE": "Everton", "MCI": "Manchester City", "MUN": "Manchester United"}
    assert game_codes("KXEPLTOTAL-26NOV23BREEVE", codes) == ("BRE", "EVE")
    assert game_codes("KXEPLTOTAL-26NOV23MCIXXX", codes) is None
    espn = [dict(id="1", abbr="MNC", name="Manchester City", short="Man City"),
            dict(id="2", abbr="MAN", name="Manchester United", short="Man United")]
    assert match_team("MCI", codes["MCI"], espn)["id"] == "1" and match_team("MUN", codes["MUN"], espn)["id"] == "2"
    assert match_team("ZZZ", "Nowhere", espn) is None
    assert over_line({"floor_strike": 248.5, "ticker": "X-249"}) == 249 and over_line({"ticker": "X-MUN6"}) == 6
    assert score({"score": {"value": 3.0}}) == 3 and score({"score": "101"}) == 101 and score({}) is None
    assert spread([1, 3, 2]) == dict(avg=2.0, min=1, max=3)
    import os, tempfile
    with tempfile.NamedTemporaryFile() as f:
        page, day = Path(f.name), datetime(2026, 11, 14)
        os.utime(page, (0, day.replace(hour=16, minute=5).timestamp()))  # built yesterday 16:05
        assert due(page, day.replace(hour=9) + timedelta(days=1)) is False  # before 10:00: yesterday's 16:00 is covered
        assert due(page, day.replace(hour=10, minute=1) + timedelta(days=1)) is True
        assert due(page, day.replace(hour=15)) is False and due(page, day.replace(hour=16, minute=30)) is False
        os.utime(page, (0, day.replace(hour=11).timestamp()))
        assert due(page, day.replace(hour=16, minute=1)) is True
    assert due(Path("/nonexistent/streaks.html")) is True
    print("selftest OK")


def built_today(out=OUT):
    """True if the page at out was written today (US Eastern), so it doesn't need a rebuild."""
    tz = ZoneInfo("America/New_York")
    return out.exists() and datetime.fromtimestamp(out.stat().st_mtime, tz).date() == datetime.now(tz).date()


def due(out=OUT, now=None):
    """True if the latest RUN_HOURS slot has passed since the page was last written (failed runs don't write)."""
    now = now or datetime.now()
    slots = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in RUN_HOURS]
    passed = [t for t in slots if t <= now] or [slots[-1] - timedelta(days=1)]
    return not out.exists() or datetime.fromtimestamp(out.stat().st_mtime) < max(passed)


def wait_for_network(tries=10, pause=30):
    """Right after the Mac wakes, DNS can be down for a while; give it up to ~5 minutes."""
    for i in range(tries):
        try:
            return get(f"{KALSHI}/exchange/status")
        except OSError as e:
            if i == tries - 1:
                raise RuntimeError(f"no network ({e}); page left unchanged")
            time.sleep(pause)


def write(out=OUT):
    wait_for_network()
    now = datetime.now(ZoneInfo("America/New_York"))
    rows, games = build(now.date())
    tmp = out.with_suffix(".tmp")
    tmp.write_text(render(rows, games, now.date(), now.strftime("%b %-d, %-I:%M %p ET")))
    tmp.replace(out)  # swap in whole, so a reader never sees a half-written page
    return len(rows)


def main():
    if "--selftest" in sys.argv:
        return selftest()
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else OUT
    if "--if-due" in sys.argv and not due(out):
        return
    try:
        print(f"{datetime.now():%Y-%m-%d %H:%M} wrote {out} ({write(out)} streaks)")
    except RuntimeError as e:  # offline: the next check retries
        print(f"{datetime.now():%Y-%m-%d %H:%M} not rebuilt: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
