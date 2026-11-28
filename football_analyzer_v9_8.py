import os
import json
import math
import re
import unicodedata
from difflib import SequenceMatcher
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st


BASE = 'https://football-data.org'
try:
    KEY = os.environ.get('FOOTBALL_DATA_KEY') or st.secrets.get('FOOTBALL_DATA_KEY', '')
except Exception:
    KEY = ''

try:
    PROPLINE_KEY = os.environ.get('PROPLINE_API_KEY') or st.secrets.get('PROPLINE_API_KEY', '')
except Exception:
    PROPLINE_KEY = ''
PROP_BASE = 'https://prop-line.com'

API_FOOTBALL_BASE = 'https://api-sports.io'
API_FOOTBALL_KEY = ''
try:
    API_FOOTBALL_KEY = os.environ.get('API_FOOTBALL_KEY') or st.secrets.get('API_FOOTBALL_KEY', '')
except Exception:
    pass

# V10.0: Campionate validate statistic din backtesting (Brier < 0.246, reușită > 68% la prag >= 63%)
PROVEN_LEAGUE_CODES = {'DED', 'BL1', 'FL1', 'PD', 'SA'}


@st.cache_resource
def api_football_session():
    return {'session': requests.Session(), 'last': 0.0}


def api_football_get(path, params=None):
    if not API_FOOTBALL_KEY:
        raise RuntimeError('Lipsește API_FOOTBALL_KEY din Streamlit Secrets.')
    state = api_football_session()
    elapsed = time.monotonic() - state['last']
    if elapsed < 0.45:
        time.sleep(0.45 - elapsed)
    state['last'] = time.monotonic()
    try:
        response = state['session'].get(
            API_FOOTBALL_BASE + path,
            headers={'x-apisports-key': API_FOOTBALL_KEY},
            params=params or {},
            timeout=(5, 22),
        )
    except requests.Timeout as exc:
        raise RuntimeError('API-Football nu a răspuns la timp.') from exc
    if response.status_code == 429:
        raise RuntimeError('API-Football: limita de cereri a fost atinsă (429).')
    if response.status_code in (401, 403):
        raise RuntimeError('API-Football: verifică cheia și abonamentul (401/403).')
    response.raise_for_status()
    payload = response.json()
    errors = payload.get('errors') or {}
    if errors:
        raise RuntimeError('API-Football: ' + str(errors)[:220])
    return payload.get('response', [])


def football_season(day):
    return day.year if day.month >= 7 else day.year - 1


@st.cache_data(ttl=86400, show_spinner=False)
def api_football_competitions():
    data = api_football_get('/leagues', {'current': 'true'})
    found = {}
    for item in data:
        league = item.get('league') or {}
        lid = league.get('id')
        if not lid:
            continue
        seasons = item.get('seasons') or []
        current = next((x for x in seasons if x.get('current')), None)
        if not current:
            continue
        kind = str(league.get('type', 'League')).upper()
        found[f'AF:{lid}'] = {
            'code': f'AF:{lid}',
            'name': f"{league.get('name', 'Competiție')} — {(item.get('country') or {}).get('name', '?')}",
            'type': 'CUP' if kind == 'CUP' else 'LEAGUE',
            'season': int(current['year']),
        }
    return sorted(found.values(), key=lambda c: c['name'])


def adapt_football_fixture(item):
    fixture = item.get('fixture') or {}
    teams = item.get('teams') or {}
    goals = item.get('goals') or {}
    home, away = teams.get('home') or {}, teams.get('away') or {}
    if not fixture.get('id') or not fixture.get('date') or not home.get('id') or not away.get('id'):
        return None
    short = str((fixture.get('status') or {}).get('short') or '')
    finished = short in ('FT', 'AET', 'PEN')
    scheduled = short in ('NS', 'TBD')
    status = 'FINISHED' if finished else ('SCHEDULED' if scheduled else 'IN_PLAY')
    if short in ('AET', 'PEN'):
        status = 'EXTRA_TIME_FINISHED'
    return {
        'id': int(fixture['id']),
        'utcDate': pd.to_datetime(fixture['date'], utc=True).isoformat(),
        'status': status,
        'homeTeam': {'id': int(home['id']), 'name': home.get('name', '?')},
        'awayTeam': {'id': int(away['id']), 'name': away.get('name', '?')},
        'score': {'fullTime': {'home': goals.get('home'), 'away': goals.get('away')}},
    }


@st.cache_data(ttl=21600, show_spinner=False)
def api_football_matches(code, start, end):
    league_id = int(code.split(':', 1)[1])
    all_matches = {}
    for year in sorted({football_season(start), football_season(end), end.year}):
        if year > datetime.now(timezone.utc).year:
            continue
        batch = api_football_get(
            '/fixtures',
            {'league': league_id, 'season': year, 'from': start.isoformat(), 'to': end.isoformat()},
        )
        for item in batch:
            m = adapt_football_fixture(item)
            if m is not None:
                all_matches[m['id']] = m
    return sorted(all_matches.values(), key=lambda x: x['utcDate'])


@st.cache_data(ttl=300, show_spinner=False)
def prop_get(path, params=None):
    response = requests.get(
        PROP_BASE + path, headers={'X-API-Key': PROPLINE_KEY}, params=params or {}, timeout=30
    )
    if response.status_code in (401, 403):
        raise RuntimeError('PropLine: cheia nu este acceptată sau planul nu permite această cerere.')
    if response.status_code == 429:
        raise RuntimeError('PropLine: limită de cereri atinsă. Reîncearcă mai târziu.')
    response.raise_for_status()
    return response.json()


TEAM_STOPWORDS = {
    'fc', 'cf', 'afc', 'sc', 'ac', 'club', 'football', 'futebol',
    'soccer', 'fk', 'sk', 'calcio', 'deportivo', 'the'
}

TEAM_ALIASES = {
    'paris saint germain': 'paris saint germain', 'paris sg': 'paris saint germain',
    'psg': 'paris saint germain',
    'birmingham city': 'birmingham', 'birmingham': 'birmingham',
    'west bromwich albion': 'west bromwich', 'west brom': 'west bromwich',
    'west bromwich': 'west bromwich',
    'athletico paranaense': 'athletico paranaense',
    'atletico paranaense': 'athletico paranaense',
    'ca paranaense': 'athletico paranaense',
    'club athletico paranaense': 'athletico paranaense',
    'chapecoense': 'chapecoense',
    'associacao chapecoense de futebol': 'chapecoense',
    'sport lisboa e benfica': 'benfica', 'sl benfica': 'benfica',
    'newcastle united': 'newcastle', 'newcastle': 'newcastle',
    'us sassuolo': 'sassuolo', 'atalanta bc': 'atalanta',
    'internacional porto alegre': 'internacional',
    'sc internacional': 'internacional',
    'club brugge kv': 'club brugge', 'club brugge': 'club brugge',
    'psv eindhoven': 'psv', 'psv': 'psv',
    'cardiff city': 'cardiff', 'cardiff': 'cardiff',
    'west ham united': 'west ham', 'west ham': 'west ham',
    'middlesbrough': 'middlesbrough', 'wrexham afc': 'wrexham',
    'cr vasco da gama': 'vasco da gama', 'vasco da gama rj': 'vasco da gama',
    'cambuur leeuwarden': 'cambuur', 'cambuur': 'cambuur',
    'feyenoord rotterdam': 'feyenoord', 'feyenoord': 'feyenoord',
    'sbv excelsior': 'excelsior', 'excelsior rotterdam': 'excelsior',
    'nec nijmegen': 'nec', 'nec': 'nec',
    'eintracht frankfurt': 'eintracht frankfurt',
    'tsg 1899 hoffenheim': 'hoffenheim', 'hoffenheim': 'hoffenheim',
    'rc strasbourg alsace': 'strasbourg', 'rc strasbourg': 'strasbourg',
    'stade brestois 29': 'brest', 'stade brestois': 'brest',
    'telstar 1963': 'telstar', 'sc telstar': 'telstar',
    'chapecoense af': 'chapecoense', 'chapecoense sc': 'chapecoense',
    'cruzeiro ec': 'cruzeiro', 'cruzeiro mg': 'cruzeiro',
    'bayer 04 leverkusen': 'bayer leverkusen', 'bayer leverkusen': 'bayer leverkusen',
    'sc paderborn 07': 'paderborn', 'paderborn': 'paderborn',
    'real betis balompie': 'real betis', 'real betis': 'real betis',
    'bolton wanderers': 'bolton', 'bolton': 'bolton',
    'ss lazio': 'lazio', 'lazio': 'lazio',
    'us lecce': 'lecce', 'lecce': 'lecce',
    'ssc napoli': 'napoli', 'napoli': 'napoli',
    'ec bahia': 'bahia', 'bahia ba': 'bahia',
    'coritiba fbc': 'coritiba', 'coritiba pr': 'coritiba',
    'tottenham hotspur': 'tottenham', 'tottenham': 'tottenham',
    'ipswich town': 'ipswich', 'ipswich': 'ipswich',
    'rayo vallecano de madrid': 'rayo vallecano', 'vallecano': 'rayo vallecano',
    'se palmeiras': 'palmeiras', 'palmeiras sp': 'palmeiras',
    'cr flamengo': 'flamengo', 'flamengo rj': 'flamengo',
    'parma calcio 1913': 'parma', 'parma calcio': 'parma',
    'ogc nice': 'nice', 'nice': 'nice',
    'paris saint germain fc': 'paris saint germain',
}


def norm_team(value):
    value = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().lower()
    value = value.replace('&', ' and ')
    words = re.findall(r'[a-z0-9]+', value)
    return ' '.join(w for w in words if w not in TEAM_STOPWORDS)


def canonical_team(value):
    name = norm_team(value)
    aliases = {norm_team(k): norm_team(v) for k, v in TEAM_ALIASES.items()}
    return aliases.get(name, name)


REGION_SUFFIXES = {'rj', 'sp', 'mg', 'rs', 'pr', 'sc', 'ba', 'ce', 'pe', 'go', 'df'}


def catalog_team_name(value):
    name = canonical_team(value)
    parts = name.split()
    if len(parts) >= 3 and parts[-1] in REGION_SUFFIXES:
        return ' '.join(parts[:-1])
    return name


def safe_team_name_match(a, b):
    x, y = catalog_team_name(a), catalog_team_name(b)
    return bool(x and y and x == y)


def team_similarity(a, b):
    x, y = canonical_team(a), canonical_team(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    xw, yw = set(x.split()), set(y.split())
    overlap = len(xw & yw) / max(len(xw), len(yw))
    string_score = SequenceMatcher(None, x, y).ratio()
    return max(string_score, overlap * 0.96) if overlap >= 0.75 else string_score


def safe_event_match(home, away, kickoff, event):
    try:
        start = pd.to_datetime(event['commence_time'], utc=True)
        target = pd.to_datetime(kickoff, utc=True)
        minutes = abs((start - target).total_seconds()) / 60
Folosește codul cu precauție.
if minutes > 15:
return None
h1, h2 = canonical_team(home), canonical_team(event.get('home_team'))
a1, a2 = canonical_team(away), canonical_team(event.get('away_team'))
if not all((h1, h2, a1, a2)) or h1 == a1 or h2 == a2:
return None
hs = team_similarity(h1, h2)
aws = team_similarity(a1, a2)
exact_both = safe_team_name_match(home, event.get('home_team')) and safe_team_name_match(
away, event.get('away_team')
)
if not exact_both and min(hs, aws) < 0.90:
return None
return min(hs, aws), minutes
except (KeyError, ValueError, TypeError, OverflowError):
return None
def decimal_price(american):
try:
p = float(american)
if p >= 100:
return round(1 + p / 100, 3)
if p <= -100:
return round(1 + 100 / abs(p), 3)
except (ValueError, TypeError):
pass
return None
def market_price(event, selection, with_source=False, superbet_only=False):
"""Superbet first; optional strict filter so foreign bookmaker odds are excluded."""
books = [b for b in event.get('bookmakers', []) if isinstance(b, dict)]
if superbet_only:
books = [b for b in books if b.get('key') == 'superbet_ro']
else:
books.sort(key=lambda b: (b.get('key') != 'superbet_ro', str(b.get('key', ''))))
for book in books:
for market in book.get('markets', []):
if not isinstance(market, dict):
continue
key = market.get('key')
for outcome in market.get('outcomes', []):
if not isinstance(outcome, dict):
continue
name = str(outcome.get('name', '')).strip().lower()
selected = selection == 'GG – DA' and key == 'both_teams_to_score' and name in ('yes', 'da')
if selection == 'Peste 2,5' and key == 'totals' and name == 'over':
try:
selected = abs(float(outcome.get('point')) - 2.5) < 0.001
except (TypeError, ValueError):
selected = False
if selected:
price = decimal_price(outcome.get('price'))
if price is not None and 1.01 <= price <= 100:
result = (
price,
market.get('last_update'),
book.get('title') or book.get('key') or 'Necunoscut',
)
return result if with_source else result[:2]
return (None, None, None) if with_source else (None, None)
@st.cache_data(ttl=300, show_spinner=False)
def prop_soccer_odds():
sports = prop_get('/sports')
if isinstance(sports, dict):
sports = sports.get('sports', sports.get('data', []))
keys = [
sp['key']
for sp in sports
if isinstance(sp, dict) and str(sp.get('key', '')).startswith('soccer_') and sp.get('active', True)
]
events, errors = [], []
for key in keys:
try:
payload = prop_get(f'/sports/{key}/odds', {'markets': 'both_teams_to_score,totals'})
if isinstance(payload, list):
batch = payload
elif isinstance(payload, dict):
batch = payload.get('events', payload.get('data', []))
else:
batch = []
if isinstance(batch, list):
events.extend(e for e in batch if isinstance(e, dict))
except (requests.RequestException, RuntimeError, ValueError) as exc:
errors.append(f'{key}: {str(exc)[:100]}')
return events, errors, len(keys)
def prop_catalog_diagnostics(df):
events, errors, sports_count = prop_soccer_odds()
parsed = []
for event in events:
try:
when = pd.to_datetime(event.get('commence_time'), utc=True, errors='coerce')
if pd.isna(when):
continue
parsed.append({
'Start UTC PropLine': when.strftime('%Y-%m-%d %H:%M'),
'Data UTC': when.strftime('%Y-%m-%d'),
'Eveniment PropLine': f"{event.get('home_team', '?')} – {event.get('away_team', '?')}",
'Superbet prezent': any(
isinstance(b, dict) and b.get('key') == 'superbet_ro'
for b in event.get('bookmakers', [])
),
})
except (TypeError, ValueError, OverflowError):
continue
catalog = pd.DataFrame(parsed)
summary = (
catalog.groupby('Data UTC')
.agg(Evenimente=('Eveniment PropLine', 'size'), Cu_Superbet=('Superbet prezent', 'sum'))
.reset_index()
if not catalog.empty
else pd.DataFrame(columns=['Data UTC', 'Evenimente', 'Cu_Superbet'])
)
sample = catalog.sort_values('Start UTC PropLine').head(40) if not catalog.empty else catalog
requested = df[['Meci', 'Start UTC']].drop_duplicates().copy()
requested['Start UTC'] = pd.to_datetime(requested['Start UTC'], utc=True, errors='coerce').dt.strftime('%Y-%m-%d %H:%M')
return summary, sample, requested, len(events), sports_count, errors
def prop_diagnostics(df):
events, errors, sports_count = prop_soccer_odds()
report = []
unique_matches = df[['Meci', 'Start UTC']].drop_duplicates()
for _, row in unique_matches.iterrows():
home, away = row['Meci'].split(' – ', 1)
kickoff = pd.to_datetime(row['Start UTC'], utc=True)
near = []
for event in events:
try:
start = pd.to_datetime(event['commence_time'], utc=True)
hours = abs((start - kickoff).total_seconds()) / 3600
if hours > 3 or start.date() != kickoff.date():
continue
hs = team_similarity(home, event.get('home_team'))
aws = team_similarity(away, event.get('away_team'))
near.append((min(hs, aws), hours, event))
except (KeyError, TypeError, ValueError):
continue
near.sort(key=lambda x: (-x[0], x[1]))
for rank, (score, hours, event) in enumerate(near[:3], 1):
books = [b for b in event.get('bookmakers', []) if isinstance(b, dict)]
superbet_books = [b for b in books if b.get('key') == 'superbet_ro']
markets = sorted({
m.get('key', '?')
for b in superbet_books
for m in b.get('markets', [])
if isinstance(m, dict)
})
report.append({
'Meci analizat': row['Meci'],
'Loc': rank,
'Eveniment PropLine': f"{event.get('home_team', '?')} – {event.get('away_team', '?')}",
'Diferență ore': round(hours, 2),
'Potrivire nume (%)': round(score * 100, 1),
'Potrivire sigură V9.3': safe_event_match(home, away, kickoff, event) is not None,
'Superbet prezent': bool(superbet_books),
'Case disponibile': ', '.join(str(b.get('title') or b.get('key')) for b in books),
'Piețe Superbet': ', '.join(markets) or '(niciuna)',
'GG disponibil': market_price(event, 'GG – DA')[0] is not None,
'Peste 2,5 disponibil': market_price(event, 'Peste 2,5')[0] is not None,
'Motiv': 'Potrivire sigură' if safe_event_match(home, away, kickoff, event) else 'Nume sau oră incompatibile',
})
if not near:
report.append({
'Meci analizat': row['Meci'],
'Loc': 0,
'Eveniment PropLine': '(niciun eveniment în intervalul ±3 ore)',
'Diferență ore': None,
'Potrivire nume (%)': None,
'Potrivire sigură V9.3': False,
'Superbet prezent': False,
'Case disponibile': '',
'Piețe Superbet': '(niciuna)',
'GG disponibil': False,
'Peste 2,5 disponibil': False,
'Motiv': 'Fără eveniment apropiat',
})
return pd.DataFrame(report), len(events), sports_count, errors
def apply_prop_odds(df, superbet_only=False):
events, errors, count = prop_soccer_odds()
updated = df.copy()
if 'Sursă cotă' not in updated.columns:
updated['Sursă cotă'] = ''
if 'Actualizare cotă UTC' not in updated.columns:
updated['Actualizare cotă UTC'] = ''
matched = 0
for i, row in updated.iterrows():
try:
home, away = row['Meci'].split(' – ', 1)
kickoff = pd.Timestamp(row['Start UTC'])
except (ValueError, KeyError, TypeError):
continue
candidates = []
for event in events:
score = safe_event_match(home, away, kickoff, event)
if score is not None:
candidates.append((score[0], score[1], event))
candidates.sort(key=lambda x: (-x[0], x[1]))
if len(candidates) != 1:
continue
odds, updated_at, bookmaker = market_price(
candidates[0][2], row['Piață'], with_source=True, superbet_only=superbet_only
)
if odds and odds > 1:
updated.at[i, 'Cotă'] = odds
updated.at[i, 'Sursă cotă'] = 'PropLine / ' + str(bookmaker)
updated.at[i, 'Actualizare cotă UTC'] = updated_at or ''
matched += 1
return updated, matched, len(events), count, errors
@st.cache_resource
def api_session():
return {'session': requests.Session(), 'last': 0.0}
def api(path, params=None):
state = api_session()
elapsed = time.monotonic() - state['last']
if elapsed < 7:
time.sleep(7 - elapsed)
state['last'] = time.monotonic()
try:
response = state['session'].get(
BASE + path, headers={'X-Auth-Token': KEY}, params=params or {}, timeout=(5, 12)
)
except requests.Timeout as exc:
raise RuntimeError('API a depășit timpul de răspuns pentru această competiție.') from exc
if response.status_code == 429:
raise RuntimeError('Limită API 429: competiție omisă temporar; reîncearcă mai târziu.')
response.raise_for_status()
return response.json()
@st.cache_data(ttl=86400, show_spinner=False)
def football_data_competitions():
return api('/competitions').get('competitions', [])
def selected_provider():
default = 'football-data.org' if KEY else 'API-Football'
return st.session_state.get('match_provider_v98', default)
def competitions():
provider = selected_provider()
if provider == 'football-data.org':
if not KEY:
raise RuntimeError('Lipsește FOOTBALL_DATA_KEY din Streamlit Secrets.')
return football_data_competitions()
if not API_FOOTBALL_KEY:
raise RuntimeError('Lipsește API_FOOTBALL_KEY din Streamlit Secrets.')
return api_football_competitions()
@st.cache_data(ttl=21600, show_spinner=False)
def matches_for_competition(code, start, end):
if str(code).startswith('AF:'):
return api_football_matches(code, start, end)
return api(
f'/competitions/{code}/matches', {'dateFrom': start.isoformat(), 'dateTo': end.isoformat()}
).get('matches', [])
def valid_finished(matches):
return sorted(
[
m
for m in matches
if m.get('status') == 'FINISHED'
and m.get('utcDate')
and m.get('score', {}).get('fullTime', {}).get('home') is not None
and m.get('score', {}).get('fullTime', {}).get('away') is not None
and m.get('homeTeam', {}).get('id')
and m.get('awayTeam', {}).get('id')
],
key=lambda x: x['utcDate'],
)

--- MOTOR MATEMATIC CALIBRAT V10.0 ---

def dixon_coles_poisson(lh, la, rho=-0.11, shrinkage=0.12):
"""
Calculează probabilitățile GG și Peste 2.5 folosind matricea de scoruri exacte (0-8 goluri)
cu ajustare Dixon-Coles pentru scorurile mici (0-0, 1-0, 0-1, 1-1) și temperare (shrinkage)
pentru a preveni supraestimările extreme (>80%).
"""
lh = max(0.25, min(3.4, lh))
la = max(0.25, min(3.4, la))
def pmf(k, lam):
return (lam ** k) * math.exp(-lam) / math.factorial(k)
def tau(x, y):
if x == 0 and y == 0:
return 1.0 - lh * la * rho
if x == 0 and y == 1:
return 1.0 + lh * rho
if x == 1 and y == 0:
return 1.0 + la * rho
if x == 1 and y == 1:
return 1.0 - rho
return 1.0
prob_matrix = [[0.0] * 9 for _ in range(9)]
total_mass = 0.0
for i in range(9):
pi = pmf(i, lh)
for j in range(9):
p = pi * pmf(j, la) * max(0.01, tau(i, j))
prob_matrix[i][j] = p
total_mass += p
gg_raw = 0.0
over_raw = 0.0
for i in range(9):
for j in range(9):
norm_p = prob_matrix[i][j] / total_mass
if i >= 1 and j >= 1:
gg_raw += norm_p
if i + j >= 3:
over_raw += norm_p
# Temperare conservatoare către media neutră (0.50) pentru eliminarea optimismului exagerat
gg_cal = (1.0 - shrinkage) * gg_raw + shrinkage * 0.50
over_cal = (1.0 - shrinkage) * over_raw + shrinkage * 0.50
return gg_cal, over_cal
def team_stats_v4(matches, team_id, n=16, minimum=10):
chosen = [m for m in matches if team_id in (m['homeTeam']['id'], m['awayTeam']['id'])][-n:]
if len(chosen) < minimum:
return None
scored, conceded = [], []
for m in chosen:
h, a = m['score']['fullTime']['home'], m['score']['fullTime']['away']
home = m['homeTeam']['id'] == team_id
scored.append(h if home else a)
conceded.append(a if home else h)
return sum(scored) / len(chosen), sum(conceded) / len(chosen)
def predict_v4(history, home_id, away_id, minimum):
h = team_stats_v4(history, home_id, n=16, minimum=minimum)
a = team_stats_v4(history, away_id, n=16, minimum=minimum)
if h is None or a is None:
return None
return dixon_coles_poisson((h[0] + a[1]) / 2, (a[0] + h[1]) / 2, rho=-0.11, shrinkage=0.10)
def weighted_mean(values, decay=0.92):
if not values:
return None
weights = [decay ** (len(values) - 1 - i) for i in range(len(values))]
return sum(x * w for x, w in zip(values, weights)) / sum(weights)
def predict_v5(history, home_id, away_id, minimum):
"""
V5 Recalibrat (V10.0): Formă ponderată pe ultimele 18 meciuri cu regresie puternică
către media ligii (strength=8) pentru a elimina instabilitatea pe eșantioane mici.
"""
if len(history) < 40:
return None
league_home = max(0.35, sum(m['score']['fullTime']['home'] for m in history) / len(history))
league_away = max(0.35, sum(m['score']['fullTime']['away'] for m in history) / len(history))
home_all = [m for m in history if home_id in (m['homeTeam']['id'], m['awayTeam']['id'])][-18:]
away_all = [m for m in history if away_id in (m['homeTeam']['id'], m['awayTeam']['id'])][-18:]
if min(len(home_all), len(away_all)) < minimum:
return None
home_games = [m for m in home_all if m['homeTeam']['id'] == home_id]
away_games = [m for m in away_all if m['awayTeam']['id'] == away_id]
def shrink(vals, prior, strength=8):
if not vals:
return prior
return (len(vals) * weighted_mean(vals) + strength * prior) / (len(vals) + strength)
h_sc = shrink([m['score']['fullTime']['home'] for m in home_games], league_home)
h_co = shrink([m['score']['fullTime']['away'] for m in home_games], league_away)
a_sc = shrink([m['score']['fullTime']['away'] for m in away_games], league_away)
a_co = shrink([m['score']['fullTime']['home'] for m in away_games], league_home)
# Medie geometrică stabilizată în loc de înmulțire brută
lh = math.sqrt(h_sc * a_co)
la = math.sqrt(a_sc * h_co)
return dixon_coles_poisson(lh, la, rho=-0.12, shrinkage=0.14)
def accessible(include_cups=False):
kinds = ('LEAGUE', 'CUP') if include_cups else ('LEAGUE',)
return [c for c in competitions() if c.get('type') in kinds and c.get('code')]
def kelly_fraction_pct(prob_pct, odds, fraction=0.25):
"""Calculează miza recomandată (% din bancă) folosind Fractional Kelly (1/4) cu plafon de 3%."""
if pd.isna(odds) or odds <= 1.01 or pd.isna(prob_pct):
return 0.0
p = prob_pct / 100.0
b = odds - 1.0
q = 1.0 - p
full_kelly = (b * p - q) / b
if full_kelly <= 0:
return 0.0
return round(min(3.0, full_kelly * fraction * 100.0), 2)
def evaluation_reason(row, min_odds, edge_min, max_disagreement):
if pd.isna(row['Cotă']):
return 'Fără cotă verificată'
reasons = []
if row['Cotă'] < min_odds:
reasons.append(f'Cotă sub {min_odds:.2f}')
if row['Avantaj estimat (pp)'] < edge_min:
reasons.append(f'Avantaj sub {edge_min} pp')
if row['Diferență modele (pp)'] > max_disagreement:
reasons.append(f'Diferență V4–V5 peste {max_disagreement} pp')
return '; '.join(reasons) if reasons else 'Candidat validat EV+ (verifică echipa de start)'
def journal_export(out, stake):
journal = out.copy()
journal.insert(0, 'Înregistrat UTC', datetime.now(timezone.utc).isoformat(timespec='seconds'))
journal['Miză planificată (lei)'] = stake
journal['Pariu plasat? (DA/NU)'] = 'NU'
journal['Goluri gazde'] = ''
journal['Goluri oaspeți'] = ''
journal['Rezultat (CÂȘTIG/PIERDERE/NEJUCAT)'] = ''
journal['Profit net (lei)'] = ''
return journal
def settle_journal(df):
settled = df.copy()
for i, row in settled.iterrows():
if str(row.get('Pariu plasat? (DA/NU)', '')).strip().upper() != 'DA':
settled.at[i, 'Rezultat (CÂȘTIG/PIERDERE/NEJUCAT)'] = 'NEJUCAT'
settled.at[i, 'Profit net (lei)'] = 0.0
continue
try:
h, a = int(row['Goluri gazde']), int(row['Goluri oaspeți'])
odd, stake = float(row['Cotă']), float(row['Miză planificată (lei)'])
if min(h, a) < 0 or odd <= 1 or stake <= 0:
continue
win = (h > 0 and a > 0) if row['Piață'] == 'GG – DA' else (h + a >= 3)
settled.at[i, 'Rezultat (CÂȘTIG/PIERDERE/NEJUCAT)'] = 'CÂȘTIG' if win else 'PIERDERE'
settled.at[i, 'Profit net (lei)'] = round(stake * (odd - 1) if win else -stake, 2)
except (TypeError, ValueError, KeyError):
continue
return settled
def evaluate(rows):
df = pd.DataFrame(rows)
summary = []
for (period, model, market), part in df.groupby(['Perioada', 'Model', 'Piață']):
for threshold in (55, 60, 65, 70):
chosen = part[part['Estimat (%)'] >= threshold]
summary.append({
'Perioada': period,
'Model': model,
'Piață': market,
'Prag (%)': threshold,
'Meciuri': len(part),
'Selecții': len(chosen),
'Reușită (%)': round(100 * chosen['Real'].mean(), 1) if len(chosen) else None,
'Brier': round(((part['Probabilitate'] - part['Real']) ** 2).mean(), 4),
'Brier de bază': round(((part['Real'].mean() - part['Real']) ** 2).mean(), 4),
})
return df, pd.DataFrame(summary)
def config_value(name):
if os.environ.get(name):
return os.environ[name]
try:
return st.secrets.get(name, '')
except Exception:
return ''
def db_request(method, table, params=None, payload=None, prefer=None):
url, key = config_value('SUPABASE_URL').rstrip('/'), config_value('SUPABASE_SERVICE_ROLE_KEY')
if not url or not key:
raise RuntimeError('Configurează SUPABASE_URL și SUPABASE_SERVICE_ROLE_KEY în Secrets.')
headers = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
if prefer:
headers['Prefer'] = prefer
response = requests.request(
method, url + '/rest/v1/' + table, headers=headers, params=params, json=payload, timeout=35
)
if not response.ok:
raise RuntimeError(response.text)
return response.json() if response.content else []
def save_predictions(records):
if not records:
return 0
for start in range(0, len(records), 100):
db_request(
'POST',
'predictions',
{'on_conflict': 'match_id,market'},
records[start : start + 100],
'resolution=ignore-duplicates,return=minimal',
)
return len(records)
def fill_missing_prematch_odds(events):
if not events:
return 0
now = datetime.now(timezone.utc).isoformat()
pending = db_request(
'GET',
'predictions',
{
'select': 'match_id,market,home_team,away_team,kickoff_utc',
'odds': 'is.null',
'kickoff_utc': 'gt.' + now,
'order': 'kickoff_utc.asc',
'limit': '1000',
},
)
filled = 0
for row in pending:
price = verified_pre_match_odds(
row['home_team'], row['away_team'], row['kickoff_utc'], row['market'], events
)
if price is None:
continue
db_request(
'PATCH',
'predictions',
{
'match_id': 'eq.' + str(row['match_id']),
'market': 'eq.' + row['market'],
'odds': 'is.null',
'kickoff_utc': 'gt.' + datetime.now(timezone.utc).isoformat(),
},
{'odds': price},
'return=minimal',
)
filled += 1
return filled
def update_final_scores():
pending = db_request(
'GET', 'predictions', {'select': 'match_id,competition,match_date', 'result': 'is.null', 'limit': '1000'}
)
grouped = {}
for row in pending:
grouped.setdefault((row['competition'], row['match_date']), set()).add(row['match_id'])
count = 0
for (comp, day), ids in grouped.items():
matches = matches_for_competition(comp, date.fromisoformat(day), date.fromisoformat(day))
for m in matches:
if m.get('id') not in ids or m.get('status') != 'FINISHED':
continue
score = m.get('score', {}).get('fullTime', {})
h, a = score.get('home'), score.get('away')
if h is None or a is None:
continue
for market in ('GG – DA', 'Peste 2,5'):
won = (h > 0 and a > 0) if market == 'GG – DA' else (h + a >= 3)
db_request(
'PATCH',
'predictions',
{'match_id': 'eq.' + str(m['id']), 'market': 'eq.' + market},
{'home_goals': h, 'away_goals': a, 'result': 'WIN' if won else 'LOSS'},
'return=minimal',
)
count += 1
return count
def verified_pre_match_odds(home, away, kickoff, market, events):
start = pd.to_datetime(kickoff, utc=True, errors='coerce')
now = pd.Timestamp.now(tz='UTC')
if pd.isna(start) or now >= start:
return None
matches = [event for event in events if safe_event_match(home, away, kickoff, event) is not None]
if len(matches) != 1:
return None
price, updated = market_price(matches[0], market)
if price is None or not 1.01 <= price <= 100:
return None
stamp = pd.to_datetime(updated, utc=True, errors='coerce')
if pd.isna(stamp) or stamp > now or stamp >= start:
return None
return price
def scheduled_analysis(day=None):
day = day or datetime.now(ZoneInfo('Europe/Bucharest')).date()
records, errors = [], []
events = []
if PROPLINE_KEY:
try:
events, odds_errors, _ = prop_soccer_odds()
errors.extend('PropLine: ' + err for err in odds_errors)
except (requests.RequestException, RuntimeError, ValueError, TypeError) as exc:
errors.append(f'PropLine indisponibil: {str(exc)[:120]}')
for comp in accessible(include_cups=False):
code = comp['code']
if code not in PROVEN_LEAGUE_CODES:
continue
try:
matches = matches_for_competition(code, day, day)
upcoming = [m for m in matches if m.get('status') in ('SCHEDULED', 'TIMED')]
if not upcoming:
continue
hist = valid_finished(
matches_for_competition(code, day - timedelta(days=365), day - timedelta(days=1))
)
for m in upcoming:
h, a = m.get('homeTeam', {}).get('id'), m.get('awayTeam', {}).get('id')
if not h or not a:
continue
v4, v5 = predict_v4(hist, h, a, 10), predict_v5(hist, h, a, 10)
if not v4 or not v5:
continue
for market, prob, comparator in zip(('GG – DA', 'Peste 2,5'), v4, v5):
home_name = m['homeTeam'].get('name', '?')
away_name = m['awayTeam'].get('name', '?')
record = {
'match_id': m['id'],
'competition': code,
'match_date': day.isoformat(),
'kickoff_utc': m['utcDate'],
'home_team': home_name,
'away_team': away_name,
'market': market,
'v4_probability': round(prob * 100, 2),
'v5_probability': round(comparator * 100, 2),
'recorded_at': datetime.now(timezone.utc).isoformat(),
'odds': None,
}
if events:
odd = verified_pre_match_odds(home_name, away_name, m['utcDate'], market, events)
if odd is not None:
record['odds'] = odd
records.append(record)
except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
errors.append(f'{code}: {str(exc)[:120]}')
save_predictions(records)
if events:
try:
filled = fill_missing_prematch_odds(events)
errors.append(f'Informare: {filled} cote lipsă completate înainte de meci.')
except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
errors.append('Completarea cotelor lipsă a eșuat: ' + str(exc)[:120])
return len(records), errors
def show_automatic_journal():
st.subheader('📚 Jurnal automat permanent')
st.caption('Predicțiile sunt salvate înainte de meci; rezultatele se completează automat din API după meci.')
if not config_value('SUPABASE_URL') or not config_value('SUPABASE_SERVICE_ROLE_KEY'):
st.warning('Mai întâi configurează baza de date Supabase conform instrucțiunilor V9.')
return
try:
data = db_request(
'GET',
'predictions',
{
'select': 'match_date,competition,home_team,away_team,market,v4_probability,v5_probability,odds,recorded_at,home_goals,away_goals,result',
'order': 'recorded_at.desc',
'limit': '2000',
},
)
if not data:
st.info('Nu există încă predicții salvate automat.')
return
df = pd.DataFrame(data)
st.metric('Predicții salvate', len(df))
resolved = df[df['result'].isin(['WIN', 'LOSS'])]
if len(resolved):
st.metric('Rată de reușită (selecții soluționate)', f"{100 * (resolved['result'] == 'WIN').mean():.1f}%")
priced = resolved.copy()
priced['odds'] = pd.to_numeric(priced['odds'], errors='coerce')
priced = priced[priced['odds'] > 1]
if not priced.empty:
priced['Profit simulat (unități)'] = priced.apply(
lambda row: row['odds'] - 1 if row['result'] == 'WIN' else -1, axis=1
)
st.metric(
'ROI teoretic (selecții cu cote salvate)',
f"{100 * priced['Profit simulat (unități)'].mean():.1f}%",
)
st.subheader('📈 Validare pe cote pre-meci — simulare')
sim = df.copy()
sim['odds'] = pd.to_numeric(sim['odds'], errors='coerce')
sim['v4_probability'] = pd.to_numeric(sim['v4_probability'], errors='coerce')
sim['v5_probability'] = pd.to_numeric(sim['v5_probability'], errors='coerce')
sim = sim[sim['result'].isin(['WIN', 'LOSS']) & (sim['odds'] > 1)]
min_odd = st.number_input('Cotă minimă (simulare)', min_value=1.01, max_value=10.0, value=1.65, step=0.05, key='validation_min_odd')
min_prob = st.slider('Probabilitate minimă V4 (%) — simulare', 0, 95, 63, key='validation_min_prob')
max_gap = st.slider('Diferență maximă V4–V5 (pp) — simulare', 0, 40, 10, key='validation_gap')
sim = sim[
(sim['odds'] >= min_odd)
& (sim['v4_probability'] >= min_prob)
& ((sim['v4_probability'] - sim['v5_probability']).abs() <= max_gap)
].copy()
if sim.empty:
st.info('Nu există încă selecții finalizate cu cote care respectă aceste filtre.')
else:
sim['Profit simulat (unități)'] = sim.apply(
lambda r: r['odds'] - 1 if r['result'] == 'WIN' else -1, axis=1
)
c1, c2, c3, c4 = st.columns(4)
c1.metric('Selecții evaluate', len(sim))
c2.metric('Profit (unități)', f"{sim['Profit simulat (unități)'].sum():+.2f}")
c3.metric('ROI simulat', f"{100 * sim['Profit simulat (unități)'].mean():+.1f}%")
c4.metric('Rată câștig', f"{100 * (sim['result'] == 'WIN').mean():.1f}%")
by_market = sim.groupby('market').agg(
Selectii=('result', 'size'),
Castigate=('result', lambda x: int((x == 'WIN').sum())),
Profit_unitati=('Profit simulat (unități)', 'sum'),
ROI_pct=('Profit simulat (unități)', lambda x: round(100 * x.mean(), 1)),
)
st.dataframe(by_market.reset_index(), hide_index=True, use_container_width=True)
st.dataframe(df, hide_index=True, use_container_width=True)
st.download_button('Exportă jurnalul (opțional)', df.to_csv(index=False).encode('utf-8-sig'), 'v10_jurnal.csv', 'text/csv')
except (requests.RequestException, RuntimeError, ValueError) as exc:
st.error(f'Jurnal indisponibil: {exc}')
def main():
st.set_page_config(page_title='Football Analyzer V10.0 — Calibrat', page_icon='⚽', layout='wide')
st.title('⚽ Football Analyzer V10.0 — Dixon-Coles & Filtru EV+')
st.caption(
'Model calibrat pe date istorice (corecție Dixon-Coles + selecție campionate cu scor Brier optim). '
'Niciun model matematic nu garantează profit.'
)
providers = []
if KEY:
providers.append('football-data.org')
if API_FOOTBALL_KEY:
providers.append('API-Football')
if providers:
st.selectbox(
'Sursa meciurilor',
providers,
key='match_provider_v98',
help='Selectează football-data.org cât timp API-Football este suspendat.',
)
else:
st.warning('Nu există nicio cheie de furnizor configurată.')
mode = st.radio('Alege analiza', ['Meciuri viitoare', 'Jurnal automat', 'Testare istorică V10'], horizontal=True)
if not KEY and not API_FOOTBALL_KEY:
st.warning('Adaugă API_FOOTBALL_KEY sau FOOTBALL_DATA_KEY în Streamlit → Settings → Secrets.')
elif mode == 'Meciuri viitoare':
day = st.date_input('Data meciurilor', date.today())
# V10.0: Cupele sunt dezactivate implicit (istoric insuficient și imprevizibilitate mare)
include_cups = st.checkbox('Include și cupele (nerecomandat — istoric instabil)', value=False)
try:
available_comps = accessible(include_cups=include_cups)
except (requests.RequestException, RuntimeError, ValueError) as exc:
st.error(f'Nu pot încărca lista competițiilor: {exc}')
available_comps = []
comp_names = {c['code']: f"{c.get('name', c['code'])} ({c['code']})" for c in available_comps}
# Selectăm implicit doar campionatele dovedite profitabile în backtest
default_proven = [c for c in comp_names if c in PROVEN_LEAGUE_CODES] or list(comp_names)
chosen_codes = st.multiselect(
'Campionate / competiții analizate (implicit: doar ligile cu acuratețe dovedită)',
options=list(comp_names),
default=default_proven,
format_func=lambda c: comp_names[c],
)
col1, col2, col3 = st.columns(3)
with col1:
threshold = st.slider('Estimare minimă calibrată (%)', 45, 85, 62)
minimum = st.slider('Minimum meciuri istorice per echipă', 8, 20, 10)
with col2:
min_odds = st.number_input('Cotă minimă de urmărit', min_value=1.20, max_value=10.0, value=1.65, step=0.05)
edge_min = st.slider('Avantaj minim față de cotă (pp)', 0, 20, 4)
with col3:
max_disagreement = st.slider('Diferență maximă V4–V5 (pp)', 0, 25, 10)
superbet_only = st.checkbox('Doar cote Superbet RO (evită case străine)', value=True)
if st.button('Analizează meciurile', type='primary'):
rows, skipped, excluded, coverage = [], [], [], []
comps = [c for c in available_comps if c['code'] in chosen_codes]
if not comps:
st.warning('Selectează cel puțin o competiție.')
progress_slot = st.empty()
status_slot = st.empty()
total_upcoming = 0
total_with_prediction = 0
progress_slot.progress(0, text='Pregătesc analiza...')
for i, comp in enumerate(comps):
status_slot.caption(f"Verific {i + 1}/{len(comps)}: {comp.get('name', comp['code'])}")
code = comp['code']
try:
matches = matches_for_competition(code, day, day)
upcoming = [m for m in matches if m.get('status') in ('SCHEDULED', 'TIMED')]
stats = {
'Competiție': comp.get('name', code),
'Cod': code,
'Meciuri returnate de API': len(matches),
'Meciuri viitoare': len(upcoming),
'Cu predicție': 0,
'Fără predicție': 0,
'Selecții peste prag': 0,
'Observații': '',
}
coverage.append(stats)
total_upcoming += len(upcoming)
if upcoming:
hist = valid_finished(
matches_for_competition(code, day - timedelta(days=365), day - timedelta(days=1))
)
for m in upcoming:
h, a = m.get('homeTeam', {}).get('id'), m.get('awayTeam', {}).get('id')
if not h or not a:
stats['Fără predicție'] += 1
excluded.append({'Competiție': code, 'Meci': str(m.get('id')), 'Motiv': 'Lipsesc ID-urile echipelor'})
continue
result = predict_v4(hist, h, a, minimum)
comparator = predict_v5(hist, h, a, minimum)
if result is None or comparator is None:
stats['Fără predicție'] += 1
excluded.append({
'Competiție': code,
'Meci': f"{m['homeTeam'].get('name', '?')} – {m['awayTeam'].get('name', '?')}",
'Motiv': f'Istoric insuficient (minim {minimum} meciuri)',
})
continue
total_with_prediction += 1
stats['Cu predicție'] += 1
for market, p, p5 in zip(('GG – DA', 'Peste 2,5'), result, comparator):
# Scor combinat calibrat: 65% V4 + 35% V5
p_blend = 0.65 * p + 0.35 * p5
if p_blend * 100 >= threshold:
stats['Selecții peste prag'] += 1
try:
clock = (
pd.Timestamp(m['utcDate'])
.tz_convert(ZoneInfo('Europe/Bucharest'))
.strftime('%H:%M')
)
except (KeyError, ValueError, TypeError):
clock = '?'
rows.append({
'Ora România': clock,
'Campionat': comp.get('name', code),
'Meci': f"{m['homeTeam'].get('name', '?')} – {m['awayTeam'].get('name', '?')}",
'Start UTC': m['utcDate'],
'Piață': market,
'Probabilitate V10 (%)': round(p_blend * 100, 1),
'V4 (%)': round(p * 100, 1),
'V5 (%)': round(p5 * 100, 1),
'Diferență modele (pp)': round(abs(p - p5) * 100, 1),
'Cotă': None,
})
except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
skipped.append(f'{code}: {str(exc)[:80]}')
finally:
progress_slot.progress((i + 1) / len(comps), text=f'Competiții verificate: {i + 1}/{len(comps)}')
progress_slot.empty()
status_slot.success(f'Analiză încheiată: {len(comps)} competiții verificate, {len(skipped)} omise.')
st.info(
f'Meciuri viitoare: {total_upcoming} | Analizate: {total_with_prediction} | '
f'Selecții peste pragul de {threshold}%: {len(rows)}'
)
if rows:
df = pd.DataFrame(rows).sort_values('Probabilitate V10 (%)', ascending=False).reset_index(drop=True)
st.session_state['v7_selections'] = df
else:
st.session_state.pop('v7_selections', None)
st.info('Nicio selecție nu atinge pragul de probabilitate configurat.')
if excluded:
with st.expander(f'Meciuri excluse ({len(excluded)})'):
st.dataframe(pd.DataFrame(excluded), hide_index=True, use_container_width=True)
if skipped:
with st.expander(f'Competiții omise ({len(skipped)})'):
st.write(skipped)
if 'v7_selections' in st.session_state:
st.subheader('Selecții și cote — Superbet / Completare manuală')
if not PROPLINE_KEY:
st.info('Adaugă PROPLINE_API_KEY în Streamlit Secrets pentru preluarea cotelor.')
elif st.button('🔄 Preia cotele disponibile (PropLine)', type='secondary'):
with st.spinner('Caut cote...'):
try:
found, matched, events_count, sports_count, errors = apply_prop_odds(
st.session_state['v7_selections'], superbet_only=superbet_only
)
st.session_state['v7_selections'] = found
st.session_state.pop('v7_odds_editor', None)
st.success(f'Cote găsite pentru {matched} selecții din {events_count} evenimente.')
except (requests.RequestException, RuntimeError, ValueError) as exc:
st.error(f'Nu am putut prelua cotele: {exc}')
st.caption('Poți introduce manual cotele reale din aplicația Superbet în coloana „Cotă”.')
edited = st.data_editor(
st.session_state['v7_selections'],
hide_index=True,
use_container_width=True,
disabled=[c for c in st.session_state['v7_selections'].columns if c != 'Cotă'],
column_config={'Cotă': st.column_config.NumberColumn('Cotă', min_value=1.01, max_value=100.0, step=0.01)},
key='v7_odds_editor',
)
out = edited.copy()
out['Probabilitate cotă (%)'] = out['Cotă'].apply(
lambda o: round(100 / o, 1) if pd.notna(o) and o > 1 else None
)
out['Avantaj estimat (pp)'] = out.apply(
lambda r: round(r['Probabilitate V10 (%)'] - r['Probabilitate cotă (%)'], 1)
if pd.notna(r['Probabilitate cotă (%)'])
else None,
axis=1,
)
out['EV estimat (%)'] = out.apply(
lambda r: round(r['Probabilitate V10 (%)'] * r['Cotă'] - 100, 1)
if pd.notna(r['Cotă']) and r['Cotă'] > 1
else None,
axis=1,
)
out['Miză Kelly 1/4 (% bancă)'] = out.apply(
lambda r: kelly_fraction_pct(r['Probabilitate V10 (%)'], r['Cotă']), axis=1
)
out['Evaluare'] = out.apply(lambda r: evaluation_reason(r, min_odds, edge_min, max_disagreement), axis=1)
candidates = out[out['Evaluare'] == 'Candidat validat EV+ (verifică echipa de start)']
st.subheader(f'✅ Candidați care trec toate filtrele de siguranță: {len(candidates)}')
if len(candidates):
st.dataframe(candidates, use_container_width=True, hide_index=True)
else:
st.info('
Nicio selecție nu trece toate filtrele. Nu forța pariuri fără avantaj matematic.')
st.subheader('Toate selecțiile și motivele respingerii')
st.dataframe(out, use_container_width=True, hide_index=True)
st.download_button(
'Descarcă evaluarea V10 CSV',
out.to_csv(index=False).encode('utf-8-sig'),
'football_v10_evaluare.csv',
'text/csv',
)
st.subheader('Jurnal pentru verificarea rezultatelor')
stake = st.number_input('Miză de simulare per selecție (lei)', min_value=1.0, max_value=10000.0, value=20.0, step=1.0)
journal = journal_export(out, stake)
st.download_button(
'Descarcă jurnalul înainte de meci (CSV)',
journal.to_csv(index=False).encode('utf-8-sig'),
f'football_v10_jurnal_{day.isoformat()}.csv',
'text/csv',
)
with st.expander('Verifică rezultatele unui jurnal salvat'):
uploaded = st.file_uploader('Încarcă jurnalul CSV completat', type=['csv'], key='v8_journal_upload')
if uploaded is not None:
try:
past = pd.read_csv(uploaded)
needed = {'Pariu plasat? (DA/NU)', 'Goluri gazde', 'Goluri oaspeți', 'Cotă', 'Piață', 'Miză planificată (lei)'}
if not needed.issubset(past.columns):
st.error('Jurnal incomplet: lipsesc coloane obligatorii.')
else:
settled = settle_journal(past)
actual = settled[
(settled['Pariu plasat? (DA/NU)'].astype(str).str.upper() == 'DA')
& (settled['Rezultat (CÂȘTIG/PIERDERE/NEJUCAT)'].isin(['CÂȘTIG', 'PIERDERE']))
]
st.dataframe(settled, hide_index=True, use_container_width=True)
if len(actual):
profit = pd.to_numeric(actual['Profit net (lei)'], errors='coerce').sum()
stakes = pd.to_numeric(actual['Miză planificată (lei)'], errors='coerce').sum()
st.metric('Profit net înregistrat (lei)', f'{profit:.2f}')
st.metric('ROI pe pariurile soluționate', f'{100 * profit / stakes:.1f}%' if stakes else '—')
st.download_button(
'Descarcă jurnalul verificat',
settled.to_csv(index=False).encode('utf-8-sig'),
'football_v10_jurnal_verificat.csv',
'text/csv',
)
except (ValueError, pd.errors.ParserError, UnicodeError) as exc:
st.error(f'CSV invalid: {exc}')
elif mode == 'Jurnal automat':
show_automatic_journal()
else:
st.subheader('📊 Testare istorică: V4 vs V5 (Calibrat V10) vs V10 Combinat')
st.caption('Fiecare predicție folosește doar meciurile încheiate înainte de startul ei. Ultimele 60 de zile sunt evaluate dintr-o fereastră de 365 de zile.')
end = st.date_input('Ultima zi inclusă în test', date.today() - timedelta(days=1), max_value=date.today() - timedelta(days=1))
minimum = st.slider('Minimum meciuri istorice per echipă', 8, 20, 10)
compare = st.checkbox('Compară și cu o perioadă anterioară (mai multe cereri API)')
earlier = (
st.date_input('Ultima zi a perioadei anterioare', end - timedelta(days=120), max_value=end - timedelta(days=60))
if compare
else None
)
if st.button('Testează modelele V10', type='primary'):
periods = [('Perioada principală', end)] + ([('Perioada anterioară', earlier)] if compare else [])
comps = [c for c in accessible() if c['code'] in PROVEN_LEAGUE_CODES] or accessible()
rows, skipped = [], []
status = st.empty()
total = len(periods) * len(comps)
for pi, (period, finish) in enumerate(periods):
for ci, comp in enumerate(comps):
code = comp['code']
status.caption(f'{period}: {comp.get("name", code)} ({pi * len(comps) + ci + 1}/{total})')
try:
finished = valid_finished(matches_for_competition(code, finish - timedelta(days=364), finish))
for index, m in enumerate(finished):
match_day = date.fromisoformat(m['utcDate'][:10])
if not (finish - timedelta(days=59) <= match_day <= finish):
continue
past = [x for x in finished[:index] if x['utcDate'] < m['utcDate']]
h, a = m['homeTeam']['id'], m['awayTeam']['id']
hg, ag = m['score']['fullTime']['home'], m['score']['fullTime']['away']
p4 = predict_v4(past, h, a, minimum)
p5 = predict_v5(past, h, a, minimum)
if p4 is None or p5 is None:
continue
p10 = (0.65 * p4[0] + 0.35 * p5[0], 0.65 * p4[1] + 0.35 * p5[1])
for name, pred in [('V4', p4), ('V5', p5), ('V10 Combinat', p10)]:
for market, p, actual in [
('GG – DA', pred[0], int(hg > 0 and ag > 0)),
('Peste 2,5', pred[1], int(hg + ag >= 3)),
]:
rows.append({
'Perioada': period,
'Data': match_day.isoformat(),
'Campionat': comp.get('name', code),
'Meci': f"{m['homeTeam'].get('name', '?')} – {m['awayTeam'].get('name', '?')}",
'Model': name,
'Piață': market,
'Estimat (%)': round(p * 100, 1),
'Rezultat real': 'DA' if actual else 'NU',
'Probabilitate': p,
'Real': actual,
})
except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
skipped.append(f'{period} / {code}: {str(exc)[:100]}')
status.empty()
if not rows:
st.warning('Nu există suficiente date pentru testare.')
else:
df, summary = evaluate(rows)
st.subheader('Rezultate generale — V4 vs V5 vs V10 Combinat')
st.dataframe(summary, use_container_width=True, hide_index=True)
st.subheader('Rezultate pe campionate')
by_league = []
for (period, league, model, market), part in df.groupby(['Perioada', 'Campionat', 'Model', 'Piață']):
for threshold in (55, 60, 65, 70):
chosen = part[part['Estimat (%)'] >= threshold]
by_league.append({
'Perioada': period,
'Campionat': league,
'Model': model,
'Piață': market,
'Prag (%)': threshold,
'Meciuri': len(part),
'Selecții': len(chosen),
'Reușită (%)': round(100 * chosen['Real'].mean(), 1) if len(chosen) else None,
'Brier': round(((part['Probabilitate'] - part['Real']) ** 2).mean(), 4),
})
leagues = pd.DataFrame(by_league)
st.dataframe(leagues, use_container_width=True, hide_index=True)
st.download_button(
'Descarcă comparația generală CSV',
summary.to_csv(index=False).encode('utf-8-sig'),
'football_v10_comparatie.csv',
'text/csv',
)
if skipped:
with st.expander(f'Competiții omise ({len(skipped)})'):
st.write(skipped)
if name == 'main':
main()
