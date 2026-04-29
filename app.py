"""
NageurStats - Tracker de performances FFN
Backend Flask qui scrape ffn.extranat.fr
"""
import os
import re
import unicodedata
import webbrowser
import threading
from collections import defaultdict


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

FFN_BASE = "https://ffn.extranat.fr/webffn"
HEADERS = {"User-Agent": "Mozilla/5.0 (NageurStats)"}
TIMEOUT = 15


def time_to_seconds(t):
    if not t:
        return None
    m = re.match(r"(\d+):(\d+)\.(\d+)", t.strip())
    if not m:
        return None
    h, s, c = m.groups()
    return int(h) * 60 + int(s) + int(c) / 100


def parse_swimmer_entry(ind, club_fallback=""):
    """ 'MARCHAND Adrien (2002) H FRA - CN YVETOT' -> dict """
    m = re.match(r"(.+?)\s+\((\d{4})\)\s+([HF])\s+(\w+)\s+-\s+(.*)", ind)
    if m:
        return {
            "name": m.group(1),
            "year": m.group(2),
            "sex": m.group(3),
            "nationality": m.group(4),
            "club": m.group(5),
        }
    return {"name": ind, "year": "", "sex": "", "nationality": "", "club": club_fallback}


def score_match(swimmer, tokens):
    """Score un nageur par rapport à des tokens de recherche."""
    name_lower = strip_accents(swimmer["name"].lower())
    parts = name_lower.split()
    score = 0
    matched_tokens = 0
    for tok in tokens:
        tl = strip_accents(tok.lower())
        token_matched = False
        if tl in parts:
            score += 30; token_matched = True
        elif any(p.startswith(tl) for p in parts):
            score += 15; token_matched = True
        elif tl in name_lower:
            score += 5; token_matched = True
        if token_matched:
            matched_tokens += 1
    # Bonus énorme si tous les tokens matchent
    if matched_tokens == len(tokens) and len(tokens) > 1:
        score += 200
    elif matched_tokens == len(tokens):
        score += 50
    return score


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify([])

    tokens = [t for t in re.split(r"\s+", q) if len(t) >= 2]
    if not tokens:
        return jsonify([])

    # Lance plusieurs requêtes : query complète, ordres inversés, et "tok1 tok2" pour chaque paire
    queries = set()
    if len(q) >= 4:
        queries.add(q)
    if len(tokens) >= 2:
        queries.add(" ".join(tokens))
        queries.add(" ".join(reversed(tokens)))
        # paires NOM Prénom et Prénom NOM avec 2 premiers tokens
        queries.add(f"{tokens[0]} {tokens[-1]}")
        queries.add(f"{tokens[-1]} {tokens[0]}")
    for t in tokens:
        if len(t) >= 4:
            queries.add(t)
    if not queries:
        # fallback: pad le token le plus long pour atteindre 4 chars
        longest = max(tokens, key=len)
        if len(longest) >= 4:
            queries.add(longest)
        else:
            return jsonify([])

    seen = {}
    for query in queries:
        try:
            r = requests.get(
                f"{FFN_BASE}/_recherche.php",
                params={"go": "ind", "idrch": query},
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            data = r.json()
        except Exception:
            continue
        for s in data:
            iuf = s.get("iuf")
            if iuf in seen:
                continue
            parsed = parse_swimmer_entry(s.get("ind", ""), s.get("clb", ""))
            parsed["iuf"] = iuf
            seen[iuf] = parsed

    # Trier par score décroissant
    swimmers = list(seen.values())
    for s in swimmers:
        s["_score"] = score_match(s, tokens)
    swimmers.sort(key=lambda x: -x["_score"])
    swimmers = [s for s in swimmers if s["_score"] > 0][:25]
    for s in swimmers:
        s.pop("_score", None)
    return jsonify(swimmers)


def parse_mpp_table(table):
    """Parse une table MPP qui peut contenir 25m ET 50m, séparés par des <thead>.
       Retourne {'25': [perfs], '50': [perfs]}."""
    out = {"25": [], "50": []}
    current_pool = None
    # Itère sur tous les enfants directs / descendants de la table dans l'ordre du document
    for el in table.find_all(["thead", "tr"]):
        if el.name == "thead":
            txt = el.get_text(" ", strip=True)
            if "Bassin : 25" in txt:
                current_pool = "25"
            elif "Bassin : 50" in txt:
                current_pool = "50"
            continue
        # tr
        if current_pool is None:
            continue
        tds = el.find_all("td")
        th = el.find("th", scope="row")
        if not th or len(tds) < 6:
            continue
        event = th.get_text(strip=True)
        time_raw = tds[0].get_text(strip=True)
        tm = re.search(r"(\d+:\d+\.\d+)", time_raw)
        time_str = tm.group(1) if tm else time_raw
        # Parse splits depuis data-tippy-content
        splits = []
        btn = tds[0].find("button", attrs={"data-tippy-content": True})
        if btn:
            tooltip_html = btn["data-tippy-content"]
            # Format: "50 m : ... 00:30.63 ... (00:30.63)"
            for m in re.finditer(r"(\d+)\s*m\s*:.*?(\d{2}:\d{2}\.\d{2}).*?\((\d{2}:\d{2}\.\d{2})\)", tooltip_html, re.DOTALL):
                splits.append({
                    "distance": int(m.group(1)),
                    "cumul": m.group(2),
                    "lap": m.group(3),
                })
        age = tds[1].get_text(strip=True).strip("()")
        points_raw = tds[2].get_text(strip=True).replace(" pts", "")
        location = re.sub(r"\s+", " ", tds[3].get_text(" ", strip=True)).split("(")[0].strip()
        date = tds[4].get_text(strip=True)
        level = tds[5].get_text(strip=True).strip("[]")
        club = tds[7].get_text(strip=True) if len(tds) > 7 else ""
        out[current_pool].append({
            "event": event,
            "time": time_str,
            "seconds": time_to_seconds(time_str),
            "age": age,
            "points": int(re.sub(r"\D", "", points_raw) or 0),
            "location": location,
            "date": date,
            "level": level,
            "club": club,
            "splits": splits,
        })
    return out


@app.route("/api/swimmer/<iuf>")
def api_swimmer(iuf):
    if not iuf.isdigit():
        return jsonify({"error": "invalid IUF"}), 400
    try:
        r = requests.get(
            f"{FFN_BASE}/nat_recherche.php",
            params={"idact": "nat", "idrch_id": iuf, "idbas": 25},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    soup = BeautifulSoup(r.text, "lxml")

    # La table MPP (parfois unique) peut contenir 25m ET 50m, séparés par des <thead>
    mpp_25, mpp_50 = [], []
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True)
        if "Meilleures Performances Personnelles" not in text:
            continue
        parsed = parse_mpp_table(table)
        if parsed["25"]:
            mpp_25 = parsed["25"]
        if parsed["50"]:
            mpp_50 = parsed["50"]

    # Total perfs depuis le texte de la page
    total = 0
    m = re.search(r"possède\s*<b>\s*(\d+)\s*</b>\s*performances", r.text)
    if m:
        total = int(m.group(1))

    # Analyse globale
    all_mpp = mpp_25 + mpp_50
    analysis = None
    if all_mpp:
        best = max(all_mpp, key=lambda p: p["points"])
        # Récents (dernière année calendaire)
        recents = sorted(all_mpp, key=lambda p: p["date"].split("/")[::-1], reverse=True)[:5]
        analysis = {
            "total_events": len(all_mpp),
            "total_perfs_db": total,
            "best_points": best["points"],
            "best_perf": best,
            "avg_points": round(sum(p["points"] for p in all_mpp) / len(all_mpp)),
            "clubs": sorted(set(p["club"] for p in all_mpp if p["club"])),
            "recent": recents,
        }

    return jsonify({
        "iuf": iuf,
        "pools": {"25": {"mpp": mpp_25}, "50": {"mpp": mpp_50}},
        "analysis": analysis,
    })


def open_browser():
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    is_local = os.environ.get("PORT") is None
    if is_local:
        threading.Timer(1.2, open_browser).start()
        app.run(host="127.0.0.1", port=port, debug=False)
    else:
        app.run(host="0.0.0.0", port=port, debug=False)
