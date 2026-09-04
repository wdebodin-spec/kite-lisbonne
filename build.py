import re, urllib.request, datetime, html as htmlmod, os, json

KITE_12M = (15, 20)
KITE_9M = (17, 30)
DAY_START = 8
DAY_END = 18

SPOTS = [
    (31, "Guincho", "25 min"),
    (39830, "Baleal - Peniche", "1h20"),
    (829734, "Lagoa de Óbidos", "1h15"),
    (317, "Fonte da Telha (Costa da Caparica)", "30 min"),
    (185, "Lagoa de Albufeira", "1h00"),
]
# Temps de trajet voiture, sans trafic, depuis Jardim da Estrela (Lisboa) — calculés via OSRM le 2026-09-04

STATE_LABEL = {"k12": "12m", "k9": "9m", "mix": "12m ou 9m", "danger": "trop fort", "none": "pas de vent"}

ROOT = os.path.dirname(os.path.abspath(__file__))


def fetch(sc):
    url = f"http://wap2.windguru.cz/view.php?sc={sc}&m=3&n=&start=0&full=1"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")


def parse(html_src):
    table = re.search(r"<table>(.*?)</table>", html_src, re.S).group(1)
    rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)

    def cells(row):
        return re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)

    header_cells = cells(rows[0])
    headers = []
    for c in header_cells:
        m = re.search(r"([A-Za-z]{2})<br/>(\d+)h", c)
        if m:
            headers.append((m.group(1), int(m.group(2))))
    n = len(headers)

    def find_row(label_substr):
        for r in rows[1:]:
            c = cells(r)
            if c and label_substr.lower() in re.sub("<[^>]+>", "", c[0]).lower():
                return c[-n:]
        return [""] * n

    speed_row = find_row("wind speed")
    gust_row = find_row("wind gusts")
    dir_row = find_row("wind direction")

    def num(c):
        m = re.search(r"(-?\d+)", c)
        return int(m.group(1)) if m else None

    def direction(c):
        m = re.search(r'alt="([A-Z]+)"', c)
        return m.group(1) if m else None

    data = []
    day_index = -1
    prev_day = None
    for i, (day, hour) in enumerate(headers):
        if day != prev_day:
            day_index += 1
            prev_day = day
        data.append({
            "day": day, "day_index": day_index, "hour": hour,
            "speed": num(speed_row[i]), "gust": num(gust_row[i]),
            "dir": direction(dir_row[i]),
        })
    return data


def kite_state(speed):
    if speed is None:
        return "none"
    if speed > KITE_9M[1]:
        return "danger"
    in12 = KITE_12M[0] <= speed <= KITE_12M[1]
    in9 = KITE_9M[0] <= speed <= KITE_9M[1]
    if in12 and in9:
        return "mix"
    if in12:
        return "k12"
    if in9:
        return "k9"
    return "none"


def day_labels(n_days=3):
    today = datetime.date.today()
    names = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    out = []
    for i in range(n_days):
        d = today + datetime.timedelta(days=i)
        prefix = "Aujourd'hui" if i == 0 else ("Demain" if i == 1 else names[d.weekday()].capitalize())
        out.append(f"{prefix} {d.strftime('%d/%m')}")
    return out


def windows_for_day(day_data):
    for d in day_data:
        d["state"] = kite_state(d["speed"])
    windows, cur, cur_state = [], [], None
    for d in day_data:
        if d["state"] in ("k12", "k9", "mix"):
            if d["state"] == cur_state:
                cur.append(d)
            else:
                if cur:
                    windows.append((cur_state, cur))
                cur, cur_state = [d], d["state"]
        else:
            if cur:
                windows.append((cur_state, cur))
            cur, cur_state = [], None
    if cur:
        windows.append((cur_state, cur))
    return [w for w in windows if len(w[1]) >= 2]


def bars_html(day_data):
    bars = []
    for d in day_data:
        speed = d["speed"] or 0
        pct = max(4, min(100, round(speed / 35 * 100)))
        title = f"{d['hour']}h · {d['speed']}kt (raf. {d['gust']}kt) {d['dir'] or ''}"
        bars.append(f'<div class="bar" style="height:{pct}%" data-state="{d["state"]}" '
                     f'title="{htmlmod.escape(title)}"></div>')
    return "".join(bars)


def collect():
    labels = day_labels(3)
    per_spot = []
    any_fetch_ok = False
    for sc, name, drive in SPOTS:
        per_day_data = []
        try:
            data = parse(fetch(sc))
            any_fetch_ok = True
            for idx in range(3):
                per_day_data.append([d for d in data if d["day_index"] == idx and d["speed"] is not None
                                      and DAY_START <= d["hour"] <= DAY_END])
        except Exception:
            per_day_data = [[], [], []]
        per_day_windows = [windows_for_day(dd) if dd else [] for dd in per_day_data]
        per_spot.append({"sc": sc, "name": name, "drive": drive, "per_day_data": per_day_data, "per_day_windows": per_day_windows})
    return labels, per_spot, any_fetch_ok


def build_dashboard_html(labels, per_spot, any_fetch_ok):
    all_highlights = []
    spot_blocks = []
    for spot in per_spot:
        day_rows = []
        for i, day_data in enumerate(spot["per_day_data"]):
            wins = spot["per_day_windows"][i]
            for state, hrs in wins:
                all_highlights.append({
                    "day_label": labels[i], "spot": spot["name"], "state": state,
                    "h0": hrs[0]["hour"], "h1": hrs[-1]["hour"],
                    "smin": min(h["speed"] for h in hrs), "smax": max(h["speed"] for h in hrs),
                    "gmax": max((h["gust"] for h in hrs if h["gust"] is not None), default=None),
                    "dir": max({h["dir"] for h in hrs if h["dir"]},
                               key=[h["dir"] for h in hrs].count) if any(h["dir"] for h in hrs) else "?",
                })
            if wins:
                pills = "".join(
                    f'<span class="pill" data-state="{state}">{STATE_LABEL[state]} · {hrs[0]["hour"]}h–{hrs[-1]["hour"]}h · '
                    f'{min(h["speed"] for h in hrs)}–{max(h["speed"] for h in hrs)}kt</span>'
                    for state, hrs in wins
                )
            else:
                pills = '<span class="pill" data-state="none">pas de fenêtre ridable</span>'
            ribbon = bars_html(day_data) if day_data else '<div class="ribbon-empty">données indisponibles</div>'
            day_rows.append(f'''
            <div class="dayrow">
              <div class="daylabel">{labels[i]}</div>
              <div class="ribbon">{ribbon}</div>
              <div class="pills">{pills}</div>
            </div>''')
        spot_blocks.append(f'''
        <article class="spot-card">
          <header class="spot-head">
            <h3>{htmlmod.escape(spot["name"])}</h3>
            <div class="spot-meta">
              <span class="drive-time">🚗 {spot["drive"]}</span>
              <a class="wg-link" href="https://www.windguru.cz/{spot["sc"]}" target="_blank" rel="noopener">windguru.cz/{spot["sc"]} ↗</a>
            </div>
          </header>
          {''.join(day_rows)}
        </article>''')

    order = {"k9": 0, "mix": 1, "k12": 2}
    all_highlights.sort(key=lambda h: (h["day_label"], order.get(h["state"], 9), -h["smax"]))
    if all_highlights:
        chips = "".join(f'''
          <div class="chip" data-state="{h['state']}">
            <div class="chip-top"><span class="chip-day">{h['day_label']}</span><span class="pill" data-state="{h['state']}">{STATE_LABEL[h['state']]}</span></div>
            <div class="chip-spot">{htmlmod.escape(h['spot'])}</div>
            <div class="chip-meta">{h['h0']}h–{h['h1']}h &nbsp;·&nbsp; {h['smin']}–{h['smax']}kt &nbsp;·&nbsp; raf. {h['gmax']}kt &nbsp;·&nbsp; {h['dir']}</div>
          </div>''' for h in all_highlights)
    else:
        chips = '<p class="muted">Aucune fenêtre ridable détectée sur les 3 prochains jours pour un 12m ou un 9m.</p>'

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%d/%m/%Y à %H:%M UTC")
    warn = "" if any_fetch_ok else '<p class="warn">⚠ Windguru injoignable au moment de la génération — données possiblement absentes.</p>'

    with open(os.path.join(ROOT, "template.html"), encoding="utf-8") as f:
        template = f.read()
    return (template.replace("__NOW__", now).replace("__WARN__", warn)
            .replace("__CHIPS__", chips).replace("__SPOTS__", "".join(spot_blocks))
            .replace("__HISTORY__", build_history_html()))


def build_history_html():
    path = os.path.join(ROOT, "sessions.json")
    try:
        with open(path, encoding="utf-8") as f:
            sessions = json.load(f)
    except FileNotFoundError:
        sessions = []

    if not sessions:
        return '<p class="muted">Aucune session enregistrée pour l\'instant.</p>'

    sessions = sorted(sessions, key=lambda s: s["date"], reverse=True)
    items = []
    for s in sessions:
        d = datetime.date.fromisoformat(s["date"])
        date_label = d.strftime("%d/%m/%Y")
        note = s.get("note") or ""
        note_html = f'<span class="history-note">— {htmlmod.escape(note)}</span>' if note else ""
        items.append(f'''
          <div class="history-item">
            <span class="history-date">{date_label}</span>
            <span class="history-spot">{htmlmod.escape(s["spot"])}</span>
            {note_html}
          </div>''')
    return f'<div class="history">{"".join(items)}</div>'


if __name__ == "__main__":
    labels, per_spot, ok = collect()
    out = build_dashboard_html(labels, per_spot, ok)
    with open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8") as f:
        f.write(out)
    print("written index.html", len(out), "bytes, fetch_ok =", ok)
