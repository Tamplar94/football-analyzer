import math
from datetime import date, timedelta

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Football Analyzer", page_icon="⚽", layout="wide")
st.title("⚽ Football Analyzer — GG & Peste 2,5")
st.caption("Estimări experimentale bazate pe rezultate istorice. Nu reprezintă garanții de câștig.")

BASE = "https://api.football-data.org/v4"
try:
    KEY = st.secrets.get("FOOTBALL_DATA_KEY", "")
except Exception:
    KEY = ""


def api(path, params=None):
    response = requests.get(
        BASE + path,
        headers={"X-Auth-Token": KEY},
        params=params or {},
        timeout=25,
    )
    if response.status_code == 429:
        raise RuntimeError("Prea multe cereri către API. Așteaptă un minut și încearcă din nou.")
    if response.status_code in (401, 403):
        raise RuntimeError("Cheia API nu este validă sau competiția nu este inclusă în abonament.")
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=86400)
def competitions():
    return api("/competitions").get("competitions", [])


@st.cache_data(ttl=3600)
def matches_for_competition(code, start, end):
    return api(
        f"/competitions/{code}/matches",
        {"dateFrom": start.isoformat(), "dateTo": end.isoformat()},
    ).get("matches", [])


@st.cache_data(ttl=3600)
def recent_matches(code, before):
    # O singură interogare per competiție; rezultatele sunt reutilizate pentru toate echipele.
    start = before - timedelta(days=365)
    end = before - timedelta(days=1)
    matches = matches_for_competition(code, start, end)
    return [m for m in matches if m.get("status") == "FINISHED" and
            m.get("score", {}).get("fullTime", {}).get("home") is not None and
            m.get("score", {}).get("fullTime", {}).get("away") is not None]


def team_stats(matches, team_id, before, n=12):
    selected = [m for m in matches if m.get("utcDate", "")[:10] < before.isoformat()
                and team_id in (m.get("homeTeam", {}).get("id"), m.get("awayTeam", {}).get("id"))]
    selected.sort(key=lambda m: m.get("utcDate", ""), reverse=True)
    selected = selected[:n]
    if len(selected) < 8:
        return None
    scored, conceded = [], []
    for m in selected:
        home = m["homeTeam"]["id"] == team_id
        h = m["score"]["fullTime"]["home"]
        a = m["score"]["fullTime"]["away"]
        scored.append(h if home else a)
        conceded.append(a if home else h)
    return sum(scored) / len(scored), sum(conceded) / len(conceded)


def estimate(home, away):
    lam_home = max(0.15, min(4.0, (home[0] + away[1]) / 2))
    lam_away = max(0.15, min(4.0, (away[0] + home[1]) / 2))
    gg = (1 - math.exp(-lam_home)) * (1 - math.exp(-lam_away))
    total = lam_home + lam_away
    over25 = 1 - math.exp(-total) * (1 + total + total**2 / 2)
    return gg, over25


match_date = st.date_input("Data meciurilor", value=date.today())
min_probability = st.slider("Probabilitatea minimă estimată (%)", 40, 90, 55)
max_matches = st.slider("Număr maxim de selecții", 5, 50, 20)
st.info("Model Poisson simplu, fără xG, accidentări, cote sau calibrare istorică. Nu folosi estimările ca probabilități validate.")

if not KEY:
    st.warning("Adaugă FOOTBALL_DATA_KEY în Streamlit → Manage app → Settings → Secrets.")
elif st.button("Analizează meciurile", type="primary"):
    try:
        available = competitions()
        # API-ul returnează doar competițiile la care contul are acces.
        selected = [c for c in available if c.get("type") == "LEAGUE" and c.get("code")]
        rows = []
        checked = 0
        progress = st.progress(0)
        for i, comp in enumerate(selected):
            code = comp["code"]
            try:
                today_matches = matches_for_competition(code, match_date, match_date)
                upcoming = [m for m in today_matches if m.get("status") in ("SCHEDULED", "TIMED")]
                if upcoming:
                    history = recent_matches(code, match_date)
                    for match in upcoming:
                        checked += 1
                        home_team = match.get("homeTeam", {})
                        away_team = match.get("awayTeam", {})
                        if not home_team.get("id") or not away_team.get("id"):
                            continue
                        h = team_stats(history, home_team["id"], match_date)
                        a = team_stats(history, away_team["id"], match_date)
                        if h is None or a is None:
                            continue
                        gg, over25 = estimate(h, a)
                        for market, probability in (("GG – DA", gg), ("Peste 2,5", over25)):
                            if probability * 100 >= min_probability:
                                rows.append({
                                    "Ora (UTC)": match.get("utcDate", "")[11:16],
                                    "Campionat": comp.get("name", code),
                                    "Meci": f"{home_team.get('name', '?')} – {away_team.get('name', '?')}",
                                    "Piață": market,
                                    "Estimare (%)": round(probability * 100, 1),
                                })
            except requests.HTTPError as exc:
                # O competiție poate să nu fie accesibilă în planul curent.
                if exc.response is not None and exc.response.status_code in (400, 403, 404):
                    continue
                raise
            progress.progress((i + 1) / max(1, len(selected)))
        if rows:
            df = pd.DataFrame(rows).sort_values("Estimare (%)", ascending=False).head(max_matches)
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button("Descarcă analiza CSV", df.to_csv(index=False).encode("utf-8-sig"), "analiza_meciuri.csv", "text/csv")
        else:
            st.warning("Nu au fost găsite selecții pentru această dată și acest prag. Încearcă altă dată sau un prag mai mic.")
        st.caption(f"Meciuri programate examinate: {checked}. Orele sunt afișate în UTC.")
    except Exception as exc:
        st.error(f"Nu s-au putut prelua datele: {exc}")
