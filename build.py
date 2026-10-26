import re, urllib.request, datetime, html as htmlmod, os, json, statistics

KITE_12M = (13, 20)
KITE_9M = (17, 30)
DAY_START = 8
DAY_END = 18
N_DAYS = 5  # marge par rapport aux 3 jours de cadence : la GitHub Action tourne avec ~4-5h de retard,
            # une fenêtre de 3 jours pile laissait le dashboard sans "aujourd'hui" les matins de refresh

# (windguru id, nom, trajet, lat, lon, slug windfinder)
SPOTS = [
    (31, "Guincho", "25 min", 38.730, -9.473, "guincho"),
    (317, "Fonte da Telha (Costa da Caparica)", "30 min", 38.565, -9.195, "fonte_da_telha"),
    (185, "Lagoa de Albufeira", "1h00", 38.508, -9.178, "lagoa_de_albufeira"),
    (829734, "Lagoa de Óbidos", "1h15", 39.418, -9.218, "foz_do_arelho"),
    (39830, "Baleal - Peniche", "1h20", 39.373, -9.338, "baleal_bay"),
]

# Sources agrégées : chaque modèle donne un vent moyen par heure, on garde la médiane comme consensus.
# Windguru (modèles régionaux haute résolution) + Open-Meteo (grands modèles nationaux) + Windfinder Superforecast.
WG_MODELS = [(3, "GFS 13km"), (21, "WRF 9km"), (37, "WRF 3km"), (64, "Zephr-HD 2.6km")]
OM_MODELS = [("icon_seamless", "ICON"), ("ecmwf_ifs025", "ECMWF"),
             ("meteofrance_seamless", "Météo-France AROME/ARPEGE"), ("ukmo_seamless", "UK Met Office")]
MAYBE_SHARE = 1 / 3  # "possible" si au moins 1/3 des modèles voient le 12m ridable alors que la médiane non
# Temps de trajet voiture, sans trafic, depuis Jardim da Estrela (Lisboa) — calculés via OSRM le 2026-09-04

STATE_LABEL = {"maybe": "possible", "k12": "12m", "k9": "9m", "mix": "12m ou 9m", "danger": "trop fort", "none": "pas de vent"}

ROOT = os.path.dirname(os.path.abspath(__file__))


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")


def fetch(sc, m=3):
    return get(f"http://wap2.windguru.cz/view.php?sc={sc}&m={m}&n=&start=0&full=1")


DIRS16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def deg_to_dir(deg):
    return None if deg is None else DIRS16[round(deg / 22.5) % 16]


# Chaque source renvoie {(day_index, heure): (vent, rafale, direction)}, day_index 0 = aujourd'hui.
def wg_source(sc, m):
    return {(d["day_index"], d["hour"]): (d["speed"], d["gust"], d["dir"])
            for d in parse(fetch(sc, m)) if d["speed"] is not None and d["day_index"] >= 0}


def om_source(lat, lon, model):
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           f"&hourly=wind_speed_10m,wind_gusts_10m,wind_direction_10m&wind_speed_unit=kn"
           f"&forecast_days={N_DAYS}&timezone=Europe/Lisbon&models={model}")
    h = json.loads(get(url))["hourly"]
    today = datetime.date.today()
    out = {}
    for i, ts in enumerate(h["time"]):
        s = h["wind_speed_10m"][i]
        if s is None:
            continue
        d = datetime.datetime.fromisoformat(ts)
        g = h["wind_gusts_10m"][i]
        out[((d.date() - today).days, d.hour)] = (round(s), None if g is None else round(g),
                                                  deg_to_dir(h["wind_direction_10m"][i]))
    return out


def wf_source(slug):
    # Seul le jour courant est rendu côté serveur sur la page Superforecast
    src = get(f"https://www.windfinder.com/weatherforecast/{slug}")
    out = {}
    for row in re.findall(r'class="fc-table-horizon">(.*?)class="cell-cl"', src, re.S):
        h = re.search(r'cell-ts">(\d+)h', row)
        ws = re.search(r'cell-ws">.*?unit">(\d+)', row, re.S)
        wg = re.search(r'cell-wg[^"]*">.*?unit">(\d+)', row, re.S)
        wd = re.search(r'alt="([\d.]+)°"', row)
        if h and ws:
            out[(0, int(h.group(1)))] = (int(ws.group(1)), int(wg.group(1)) if wg else None,
                                         deg_to_dir(float(wd.group(1))) if wd else None)
    return out


def merge(sources):
    """sources: {nom: {(jour, heure): (vent, rafale, dir)}} -> par jour, liste des heures en consensus."""
    per_day = [[] for _ in range(N_DAYS)]
    for day in range(N_DAYS):
        for hour in range(DAY_START, DAY_END + 1):
            vals = [(name, s[(day, hour)]) for name, s in sources.items() if (day, hour) in s]
            if len(vals) < 2:
                continue
            speeds = sorted(v[0] for _, v in vals)
            gusts = sorted(v[1] for _, v in vals if v[1] is not None)
            dirs = [v[2] for _, v in vals if v[2]]
            per_day[day].append({
                "hour": hour, "speed": round(statistics.median(speeds)),
                "gust": round(statistics.median(gusts)) if gusts else None,
                "dir": max(set(dirs), key=dirs.count) if dirs else None,
                "lo": speeds[0], "hi": speeds[-1], "n": len(vals),
                "share12": sum(1 for s in speeds if s >= KITE_12M[0]) / len(speeds),
                "detail": [(name, v[0]) for name, v in vals],
            })
    return per_day


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
    # Le run du modèle peut démarrer la veille (ex. dimanche 18h quand on build à 0h17) :
    # on recale day_index pour que 0 = le bloc dont le jour correspond à aujourd'hui.
    today_abbr = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"][datetime.date.today().weekday()]
    offset = next((d["day_index"] for d in data if d["day"] == today_abbr), 0)
    for d in data:
        d["day_index"] -= offset
    return data


def kite_state(speed, share12=0):
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
    if share12 >= MAYBE_SHARE:
        return "maybe"
    return "none"


def day_labels(n_days=N_DAYS):
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
        d["state"] = kite_state(d["speed"], d.get("share12", 0))
    windows, cur, cur_state = [], [], None
    for d in day_data:
        if d["state"] in ("k12", "k9", "mix", "maybe"):
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
        detail = " / ".join(f"{n} {s}" for n, s in d.get("detail", []))
        title = (f"{d['hour']}h · consensus {d['speed']}kt (raf. {d['gust']}kt) {d['dir'] or ''} · "
                 f"{d.get('n', 1)} modèles {d.get('lo')}–{d.get('hi')}kt\n{detail}")
        bars.append(f'<div class="bar" style="height:{pct}%" data-state="{d["state"]}" '
                     f'title="{htmlmod.escape(title)}"></div>')
    return "".join(bars)


def collect():
    labels = day_labels(N_DAYS)
    per_spot = []
    any_fetch_ok = False
    for sc, name, drive, lat, lon, wf in SPOTS:
        sources = {}
        jobs = ([(f"Windguru {label}", wg_source, (sc, m)) for m, label in WG_MODELS]
                + [(label, om_source, (lat, lon, model)) for model, label in OM_MODELS]
                + [("Windfinder Superforecast", wf_source, (wf,))])
        for label, fn, args in jobs:
            try:
                data = fn(*args)
                if data:
                    sources[label] = data
            except Exception:
                pass  # modèle absent pour ce spot (ex. Baleal n'a pas les modèles régionaux Windguru)
        any_fetch_ok = any_fetch_ok or bool(sources)
        per_day_data = merge(sources)
        per_day_windows = [windows_for_day(dd) if dd else [] for dd in per_day_data]
        per_spot.append({"sc": sc, "name": name, "drive": drive, "wf": wf, "lat": lat, "lon": lon,
                         "sources": sorted(sources), "per_day_data": per_day_data,
                         "per_day_windows": per_day_windows})
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
                    "hmax": max(h.get("hi", h["speed"]) for h in hrs),
                    "gmax": max((h["gust"] for h in hrs if h["gust"] is not None), default=None),
                    "dir": max({h["dir"] for h in hrs if h["dir"]},
                               key=[h["dir"] for h in hrs].count) if any(h["dir"] for h in hrs) else "?",
                })
            if wins:
                pills = "".join(
                    f'<span class="pill" data-state="{state}">{STATE_LABEL[state]} · {hrs[0]["hour"]}h–{hrs[-1]["hour"]}h · '
                    f'{min(h["speed"] for h in hrs)}–{max(h["speed"] for h in hrs)}kt'
                    + (f' (jusqu\'à {max(h["hi"] for h in hrs)}kt selon modèles)' if state == "maybe" else "") + '</span>'
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
              <a class="wg-link" href="https://www.windguru.cz/{spot["sc"]}" target="_blank" rel="noopener">Windguru ↗</a>
              <a class="wg-link" href="https://www.windfinder.com/weatherforecast/{spot["wf"]}" target="_blank" rel="noopener">Windfinder ↗</a>
              <a class="wg-link" href="https://www.windy.com/{spot["lat"]}/{spot["lon"]}/wind?{spot["lat"]},{spot["lon"]},11" target="_blank" rel="noopener">Windy ↗</a>
            </div>
          </header>
          <p class="sources">{len(spot["sources"])} sources : {htmlmod.escape(", ".join(spot["sources"]))}</p>
          {''.join(day_rows)}
        </article>''')

    order = {"k9": 0, "mix": 1, "k12": 2, "maybe": 3}
    all_highlights.sort(key=lambda h: (labels.index(h["day_label"]), order.get(h["state"], 9), -h["smax"]))
    if all_highlights:
        chips = "".join(f'''
          <div class="chip" data-state="{h['state']}">
            <div class="chip-top"><span class="chip-day">{h['day_label']}</span><span class="pill" data-state="{h['state']}">{STATE_LABEL[h['state']]}</span></div>
            <div class="chip-spot">{htmlmod.escape(h['spot'])}</div>
            <div class="chip-meta">{h['h0']}h–{h['h1']}h &nbsp;·&nbsp; consensus {h['smin']}–{h['smax']}kt {f" (max {h['hmax']}kt)" if h['state'] == 'maybe' else ""} &nbsp;·&nbsp; raf. {h['gmax']}kt &nbsp;·&nbsp; {h['dir']}</div>
          </div>''' for h in all_highlights)
    else:
        chips = f'<p class="muted">Aucune fenêtre ridable détectée sur les {N_DAYS} prochains jours pour un 12m ou un 9m.</p>'

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%d/%m/%Y à %H:%M UTC")
    warn = "" if any_fetch_ok else '<p class="warn">⚠ Aucune source de prévision joignable au moment de la génération — données possiblement absentes.</p>'

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
