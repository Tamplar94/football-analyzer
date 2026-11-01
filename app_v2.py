import math
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='Football Analyzer V2', page_icon='⚽', layout='wide')
st.title('⚽ Football Analyzer V2 — GG & Peste 2,5')
st.caption('Model Poisson experimental. Procentele nu sunt calibrate sau validate și nu garantează câștiguri.')
BASE = 'https://api.football-data.org/v4'
try:
    KEY = st.secrets.get('FOOTBALL_DATA_KEY', '')
except Exception:
    KEY = ''

session = requests.Session()

def api(path, params=None):
    response = session.get(BASE + path, headers={'X-Auth-Token': KEY}, params=params or {}, timeout=25)
    if response.status_code == 429:
        raise RuntimeError('Limita API a fost atinsă. Așteaptă și reîncearcă.')
    if response.status_code in (401, 403):
        raise RuntimeError('Cheia API sau accesul la competiție nu este valid.')
    response.raise_for_status()
    return response.json()

@st.cache_data(ttl=86400)
def competitions():
    return api('/competitions').get('competitions', [])

@st.cache_data(ttl=3600)
def matches_for_competition(code, start, end):
    return api(f'/competitions/{code}/matches', {'dateFrom': start.isoformat(), 'dateTo': end.isoformat()}).get('matches', [])

@st.cache_data(ttl=3600)
def recent_matches(code, before):
    # A single 365-day request per league, reused for every team.
    matches = matches_for_competition(code, before - timedelta(days=365), before - timedelta(days=1))
    return [m for m in matches if m.get('status') == 'FINISHED' and
            m.get('score', {}).get('fullTime', {}).get('home') is not None and
            m.get('score', {}).get('fullTime', {}).get('away') is not None]

def team_stats(matches, team_id, before, n=12, min_games=8):
    selected = [m for m in matches if m.get('utcDate', '')[:10] < before.isoformat() and
                team_id in (m.get('homeTeam', {}).get('id'), m.get('awayTeam', {}).get('id'))]
    selected.sort(key=lambda m: m.get('utcDate', ''), reverse=True)
    selected = selected[:n]
    if len(selected) < min_games:
        return None
    scored, conceded = [], []
    for m in selected:
        is_home = m['homeTeam']['id'] == team_id
        h, a = m['score']['fullTime']['home'], m['score']['fullTime']['away']
        scored.append(h if is_home else a)
        conceded.append(a if is_home else h)
    return sum(scored) / len(scored), sum(conceded) / len(conceded), len(selected)

def estimate(home, away):
    lam_home = max(.15, min(4., (home[0] + away[1]) / 2))
    lam_away = max(.15, min(4., (away[0] + home[1]) / 2))
    gg = (1 - math.exp(-lam_home)) * (1 - math.exp(-lam_away))
    total = lam_home + lam_away
    over25 = 1 - math.exp(-total) * (1 + total + total**2 / 2)
    return gg, over25

match_date = st.date_input('Data meciurilor', value=date.today())
min_probability = st.slider('Estimare minimă (%)', 30, 90, 55)
max_matches = st.slider('Număr maxim de selecții', 5, 100, 30)
min_games = st.slider('Minimum meciuri istorice per echipă', 5, 12, 8)
st.info('Estimările sunt exploratorii: fără xG, cote, accidentări, avantaj teren propriu calibrat sau backtesting.')

if not KEY:
    st.warning('Configurează FOOTBALL_DATA_KEY în Streamlit → Manage app → Settings → Secrets.')
elif st.button('Analizează meciurile', type='primary'):
    try:
        available = competitions()
        selected = [c for c in available if c.get('type') == 'LEAGUE' and c.get('code')]
        if not selected:
            st.warning('Contul API nu returnează campionate accesibile.')
        rows, checked, skipped = [], 0, []
        progress = st.progress(0)
        for i, comp in enumerate(selected):
            code = comp['code']
            try:
                today_matches = matches_for_competition(code, match_date, match_date)
                upcoming = [m for m in today_matches if m.get('status') in ('SCHEDULED', 'TIMED')]
                checked += len(upcoming)
                if upcoming:
                    history = recent_matches(code, match_date)
                    for match in upcoming:
                        home_team, away_team = match.get('homeTeam', {}), match.get('awayTeam', {})
                        if not home_team.get('id') or not away_team.get('id'):
                            continue
                        h = team_stats(history, home_team['id'], match_date, min_games=min_games)
                        a = team_stats(history, away_team['id'], match_date, min_games=min_games)
                        if h is None or a is None:
                            continue
                        gg, over25 = estimate(h, a)
                        utc_time = match.get('utcDate', '')
                        try:
                            local_time = pd.Timestamp(utc_time).tz_convert(ZoneInfo('Europe/Bucharest')).strftime('%H:%M')
                        except (ValueError, TypeError):
                            local_time = utc_time[11:16]
                        for market, probability in [('GG – DA', gg), ('Peste 2,5', over25)]:
                            if probability * 100 >= min_probability:
                                rows.append({'Ora (România)': local_time,
                                             'Campionat': comp.get('name', code),
                                             'Meci': f"{home_team.get('name', '?')} – {away_team.get('name', '?')}",
                                             'Piață': market,
                                             'Estimare (%)': round(probability * 100, 1),
                                             'Meciuri istorice (gazde/oaspeți)': f'{h[2]}/{a[2]}'})
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (400, 403, 404):
                    skipped.append(f'{code}: HTTP {status}')
                else:
                    raise
            progress.progress((i + 1) / max(1, len(selected)))
        if rows:
            df = pd.DataFrame(rows).sort_values('Estimare (%)', ascending=False).head(max_matches)
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button('Descarcă analiza CSV', df.to_csv(index=False).encode('utf-8-sig'), 'analiza_meciuri_v2.csv', 'text/csv')
        else:
            st.warning('Nu există selecții care să îndeplinească filtrele. Încearcă altă dată sau un prag mai mic.')
        st.caption(f'Competiții accesibile: {len(selected)}. Meciuri programate examinate: {checked}. Orele sunt în fusul României.')
        if skipped:
            with st.expander(f'Competiții omise ({len(skipped)})'):
                st.write(', '.join(skipped))
    except Exception as exc:
        st.error(f'Nu s-au putut prelua datele: {exc}')
