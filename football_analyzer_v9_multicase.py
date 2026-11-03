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


BASE = 'https://api.football-data.org/v4'
try:
    KEY = os.environ.get('FOOTBALL_DATA_KEY') or st.secrets.get('FOOTBALL_DATA_KEY', '')
except Exception:
    KEY = ''

# PropLine: autentificarea se face doar pe server, prin Streamlit Secrets.
try:
    PROPLINE_KEY = os.environ.get('PROPLINE_API_KEY') or st.secrets.get('PROPLINE_API_KEY', '')
except Exception:
    PROPLINE_KEY = ''
PROP_BASE = 'https://api.prop-line.com/v1'

@st.cache_data(ttl=300, show_spinner=False)
def prop_get(path, params=None):
    response = requests.get(PROP_BASE + path, headers={'X-API-Key': PROPLINE_KEY},
                            params=params or {}, timeout=30)
    if response.status_code in (401, 403):
        raise RuntimeError('PropLine: cheia nu este acceptată sau planul nu permite această cerere.')
    if response.status_code == 429:
        raise RuntimeError('PropLine: limită de cereri atinsă. Reîncearcă mai târziu.')
    response.raise_for_status()
    return response.json()

def norm_team(value):
    value = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().lower()
    value = re.sub(r'\b(fc|cf|ac|sc|afc|calcio|football|club|deportivo|the)\b', ' ', value)
    return ' '.join(re.findall(r'[a-z0-9]+', value))

# Aliasuri explicite, pentru a nu confunda echipe diferite cu nume similare.
TEAM_ALIASES = {
    'us sassuolo': 'sassuolo', 'sassuolo': 'sassuolo',
    'sport lisboa e benfica': 'benfica', 'sl benfica': 'benfica', 'benfica': 'benfica',
    'newcastle united': 'newcastle', 'newcastle': 'newcastle',
    'everton': 'everton', 'atalanta bc': 'atalanta', 'atalanta': 'atalanta',
    'parma': 'parma', 'fiorentina': 'fiorentina', 'alverca': 'alverca',
}

def canonical_team(value):
    name = norm_team(value)
    return TEAM_ALIASES.get(name, name)

def safe_event_match(home, away, kickoff, event):
    """Potrivire pe ambele echipe, ordinea terenului și ora exactă."""
    try:
        start = pd.to_datetime(event['commence_time'], utc=True)
        target = pd.to_datetime(kickoff, utc=True)
        minutes = abs((start - target).total_seconds()) / 60
        if minutes > 15:
            return None
        h1, h2 = canonical_team(home), canonical_team(event.get('home_team'))
        a1, a2 = canonical_team(away), canonical_team(event.get('away_team'))
        if not h1 or not a1 or not h2 or not a2:
            return None
        # Acceptăm identitate canonică ori nume aproape identice; nu doar un scor global.
        hs = 1.0 if h1 == h2 else SequenceMatcher(None, h1, h2).ratio()
        aws = 1.0 if a1 == a2 else SequenceMatcher(None, a1, a2).ratio()
        if min(hs, aws) < 0.88:
            return None
        return (min(hs, aws), minutes)
    except (KeyError, ValueError, TypeError):
        return None

def team_similarity(a, b):
    x, y = norm_team(a), norm_team(b)
    if not x or not y:
        return 0
    return 1.0 if x == y else SequenceMatcher(None, x, y).ratio()

def decimal_price(value):
    """Convertește cote americane sau zecimale, fără a ghici formatul ambiguu."""
    try:
        p = float(value)
        if p >= 100:
            return round(1 + p / 100, 3)
        if p <= -100:
            return round(1 + 100 / abs(p), 3)
        if 1 < p < 100:
            return round(p, 3)
    except (ValueError, TypeError):
        pass
    return None


def bookmaker_prices(event, selection):
    """Toate cotele reale pentru piața cerută, cu casa și data actualizării."""
    found = []
    for book in event.get('bookmakers', []):
        if not isinstance(book, dict):
            continue
        bookmaker = str(book.get('title') or book.get('key') or '').strip()
        if not bookmaker:
            continue
        for market in book.get('markets', []):
            if not isinstance(market, dict):
                continue
            key = market.get('key')
            for outcome in market.get('outcomes', []):
                if not isinstance(outcome, dict):
                    continue
                name = str(outcome.get('name', '')).strip().lower()
                valid = False
                if selection == 'GG – DA' and key in ('both_teams_to_score', 'btts'):
                    valid = name in ('yes', 'da')
                elif selection == 'Peste 2,5' and key == 'totals' and name in ('over', 'peste'):
                    try:
                        valid = abs(float(outcome.get('point')) - 2.5) < 0.001
                    except (TypeError, ValueError):
                        pass
                if valid:
                    odd = decimal_price(outcome.get('price'))
                    if odd is not None:
                        found.append((odd, bookmaker, market.get('last_update') or book.get('last_update') or ''))
    return sorted(found, key=lambda x: x[0], reverse=True)


@st.cache_data(ttl=300, show_spinner=False)
def prop_soccer_odds():
    sports = prop_get('/sports')
    if isinstance(sports, dict):
        sports = sports.get('sports', sports.get('data', []))
    if not isinstance(sports, list):
        raise RuntimeError('PropLine: lista sporturilor are un format neașteptat.')
    keys = [sp['key'] for sp in sports if isinstance(sp, dict) and
            str(sp.get('key', '')).startswith('soccer_') and sp.get('active', True)]
    events, errors = [], []
    for key in keys:
        try:
            # Fără filtru de bookmaker: API poate returna toate casele disponibile în abonament.
            payload = prop_get(f'/sports/{key}/odds',
                               {'markets': 'both_teams_to_score,totals'})
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


def prop_diagnostics(df):
    """Diagnostic agregat fără afișarea cheilor API."""
    events, errors, sports_count = prop_soccer_odds()
    report = []
    for _, row in df[['Meci', 'Start UTC']].drop_duplicates().iterrows():
        home, away = row['Meci'].split(' – ', 1)
        matches = [event for event in events
                   if safe_event_match(home, away, row['Start UTC'], event) is not None]
        books = sorted({str(book.get('title') or book.get('key'))
                        for event in matches for book in event.get('bookmakers', [])
                        if isinstance(book, dict) and (book.get('title') or book.get('key'))})
        report.append({'Meci analizat': row['Meci'],
                       'Evenimente potrivite': len(matches),
                       'Case disponibile': ', '.join(books) or '(niciuna)',
                       'GG disponibil': any(bookmaker_prices(e, 'GG – DA') for e in matches),
                       'Peste 2,5 disponibil': any(bookmaker_prices(e, 'Peste 2,5') for e in matches)})
    return pd.DataFrame(report), len(events), sports_count, errors


def apply_prop_odds(df):
    events, errors, count = prop_soccer_odds()
    updated = df.copy()
    for col in ('Sursă cotă', 'Actualizare cotă UTC', 'Alte cote disponibile'):
        if col not in updated.columns:
            updated[col] = ''
    matched = 0
    for i, row in updated.iterrows():
        try:
            home, away = row['Meci'].split(' – ', 1)
            kickoff = pd.Timestamp(row['Start UTC'])
        except (ValueError, KeyError, TypeError):
            continue
        candidates = [event for event in events if safe_event_match(home, away, kickoff, event) is not None]
        # Nu asociem cote unui meci dacă există evenimente ambigue.
        if len(candidates) != 1:
            continue
        # Peste 2,5: doar cotă introdusă manual până la verificarea sursei.
        if row['Piață'] == 'Peste 2,5':
            continue
        prices = bookmaker_prices(candidates[0], row['Piață'])
        if prices:
            odd, bookmaker, updated_at = prices[0]
            updated.at[i, 'Cotă'] = odd
            updated.at[i, 'Sursă cotă'] = bookmaker
            updated.at[i, 'Actualizare cotă UTC'] = updated_at
            updated.at[i, 'Alte cote disponibile'] = '; '.join(
                f'{name}: {price:.2f}' for price, name, _ in prices[1:])
            matched += 1
    return updated, matched, len(events), count, errors

@st.cache_resource
def api_session():
    return {'session': requests.Session(), 'last': 0.0}

def api(path, params=None):
    state = api_session()
    for attempt in range(4):
        elapsed = time.monotonic() - state['last']
        if elapsed < 7:
            time.sleep(7 - elapsed)
        state['last'] = time.monotonic()
        response = state['session'].get(BASE + path, headers={'X-Auth-Token': KEY}, params=params or {}, timeout=35)
        if response.status_code == 429:
            if attempt == 3:
                raise RuntimeError('Limită API 429. Reîncearcă mai târziu.')
            retry = response.headers.get('Retry-After', '')
            try:
                delay = float(retry)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = 65 * (attempt + 1)
            time.sleep(max(7, min(120, delay)))
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError('API indisponibil')

@st.cache_data(ttl=86400)
def competitions():
    return api('/competitions').get('competitions', [])

@st.cache_data(ttl=3600)
def matches_for_competition(code, start, end):
    return api(f'/competitions/{code}/matches', {'dateFrom': start.isoformat(), 'dateTo': end.isoformat()}).get('matches', [])

def valid_finished(matches):
    return sorted([m for m in matches if m.get('status') == 'FINISHED' and m.get('utcDate') and
                   m.get('score', {}).get('fullTime', {}).get('home') is not None and
                   m.get('score', {}).get('fullTime', {}).get('away') is not None and
                   m.get('homeTeam', {}).get('id') and m.get('awayTeam', {}).get('id')], key=lambda x: x['utcDate'])

def team_stats_v4(matches, team_id, n=12, minimum=8):
    chosen = [m for m in matches if team_id in (m['homeTeam']['id'], m['awayTeam']['id'])][-n:]
    if len(chosen) < minimum:
        return None
    scored, conceded = [], []
    for m in chosen:
        h, a = m['score']['fullTime']['home'], m['score']['fullTime']['away']
        home = m['homeTeam']['id'] == team_id
        scored.append(h if home else a)
        conceded.append(a if home else h)
    return sum(scored)/len(chosen), sum(conceded)/len(chosen)

def poisson(lh, la):
    lh, la = max(.15, min(4., lh)), max(.15, min(4., la))
    gg = (1 - math.exp(-lh)) * (1 - math.exp(-la))
    total = lh + la
    over = 1 - math.exp(-total) * (1 + total + total * total / 2)
    return gg, over

def predict_v4(history, home_id, away_id, minimum):
    h = team_stats_v4(history, home_id, minimum=minimum)
    a = team_stats_v4(history, away_id, minimum=minimum)
    if h is None or a is None:
        return None
    return poisson((h[0] + a[1])/2, (a[0] + h[1])/2)

def weighted_mean(values):
    if not values:
        return None
    weights = [0.90 ** (len(values)-1-i) for i in range(len(values))]
    return sum(x*w for x, w in zip(values, weights)) / sum(weights)

def predict_v5(history, home_id, away_id, minimum):
    # League-wide prior calculated strictly from earlier matches.
    if len(history) < 30:
        return None
    league_home = sum(m['score']['fullTime']['home'] for m in history)/len(history)
    league_away = sum(m['score']['fullTime']['away'] for m in history)/len(history)
    league_home = max(.3, league_home)
    league_away = max(.3, league_away)
    home_all = [m for m in history if home_id in (m['homeTeam']['id'], m['awayTeam']['id'])][-12:]
    away_all = [m for m in history if away_id in (m['homeTeam']['id'], m['awayTeam']['id'])][-12:]
    if min(len(home_all), len(away_all)) < minimum:
        return None
    home_games = [m for m in home_all if m['homeTeam']['id'] == home_id]
    away_games = [m for m in away_all if m['awayTeam']['id'] == away_id]
    # Shrink limited home/away samples toward league rates to avoid unstable estimates.
    def shrink(vals, prior, strength=5):
        if not vals:
            return prior
        return (len(vals)*weighted_mean(vals) + strength*prior)/(len(vals)+strength)
    h_sc = shrink([m['score']['fullTime']['home'] for m in home_games], league_home)
    h_co = shrink([m['score']['fullTime']['away'] for m in home_games], league_away)
    a_sc = shrink([m['score']['fullTime']['away'] for m in away_games], league_away)
    a_co = shrink([m['score']['fullTime']['home'] for m in away_games], league_home)
    lh = league_home * (h_sc/league_home) * (a_co/league_home)
    la = league_away * (a_sc/league_away) * (h_co/league_away)
    return poisson(lh, la)

def accessible(include_cups=False):
    kinds = ('LEAGUE', 'CUP') if include_cups else ('LEAGUE',)
    return [c for c in competitions() if c.get('type') in kinds and c.get('code')]


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
    return '; '.join(reasons) if reasons else 'Candidat pentru verificare, nu pariu sigur'


def journal_export(out, stake):
    # Fișierul este descărcat de utilizator; Streamlit nu garantează stocarea permanentă.
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
            summary.append({'Perioada': period, 'Model': model, 'Piață': market,
                            'Prag (%)': threshold, 'Meciuri': len(part), 'Selecții': len(chosen),
                            'Reușită (%)': round(100*chosen['Real'].mean(), 1) if len(chosen) else None,
                            'Brier': round(((part['Probabilitate']-part['Real'])**2).mean(), 4),
                            'Brier de bază': round(((part['Real'].mean()-part['Real'])**2).mean(), 4)})
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
    response = requests.request(method, url + '/rest/v1/' + table, headers=headers,
                                params=params, json=payload, timeout=35)
    response.raise_for_status()
    return response.json() if response.content else []

def save_predictions(records):
    if not records:
        return 0
    # Nu suprascrie niciodată o predicție existentă: prima estimare rămâne fixă.
    for start in range(0, len(records), 100):
        db_request('POST', 'predictions', {'on_conflict': 'match_id,market'},
                   records[start:start+100], 'resolution=ignore-duplicates,return=minimal')
    return len(records)

def update_final_scores():
    pending = db_request('GET', 'predictions', {'select':'match_id,competition,match_date', 'result':'is.null', 'limit':'1000'})
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
                won = (h > 0 and a > 0) if market == 'GG – DA' else (h+a >= 3)
                db_request('PATCH', 'predictions', {'match_id': 'eq.' + str(m['id']), 'market': 'eq.' + market},
                           {'home_goals': h, 'away_goals': a, 'result': 'WIN' if won else 'LOSS'}, 'return=minimal')
                count += 1
    return count

def scheduled_analysis(day=None):
    day = day or datetime.now(ZoneInfo('Europe/Bucharest')).date()
    records, errors = [], []
    for comp in accessible(include_cups=True):
        code = comp['code']
        try:
            matches = matches_for_competition(code, day, day)
            upcoming = [m for m in matches if m.get('status') in ('SCHEDULED','TIMED')]
            if not upcoming:
                continue
            hist = valid_finished(matches_for_competition(code, day-timedelta(days=365), day-timedelta(days=1)))
            for m in upcoming:
                h, a = m.get('homeTeam',{}).get('id'), m.get('awayTeam',{}).get('id')
                if not h or not a:
                    continue
                v4, v5 = predict_v4(hist,h,a,8), predict_v5(hist,h,a,8)
                if not v4 or not v5:
                    continue
                for market, prob, comparator in zip(('GG – DA','Peste 2,5'),v4,v5):
                    records.append({'match_id': m['id'], 'competition': code,
                                    'match_date': day.isoformat(), 'kickoff_utc': m['utcDate'],
                                    'home_team': m['homeTeam'].get('name','?'),
                                    'away_team': m['awayTeam'].get('name','?'),
                                    'market': market, 'v4_probability': round(prob*100,2),
                                    'v5_probability': round(comparator*100,2),
                                    'recorded_at': datetime.now(timezone.utc).isoformat()})
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
            errors.append(f'{code}: {str(exc)[:120]}')
    save_predictions(records)
    return len(records), errors

def show_automatic_journal():
    st.subheader('📚 Jurnal automat permanent')
    st.caption('Predicțiile sunt salvate înainte de meci; rezultatele se completează automat din API după meci. Necesită Supabase și programarea GitHub Actions.')
    if not config_value('SUPABASE_URL') or not config_value('SUPABASE_SERVICE_ROLE_KEY'):
        st.warning('Mai întâi configurează baza de date Supabase conform instrucțiunilor V9.')
        return
    try:
        data = db_request('GET','predictions',{'select':'match_date,competition,home_team,away_team,market,v4_probability,v5_probability,odds,recorded_at,home_goals,away_goals,result', 'order':'recorded_at.desc','limit':'2000'})
        if not data:
            st.info('Nu există încă predicții salvate automat.')
            return
        df = pd.DataFrame(data)
        st.metric('Predicții salvate', len(df))
        resolved = df[df['result'].isin(['WIN','LOSS'])]
        if len(resolved):
            st.metric('Rată de reușită (selecții soluționate)', f"{100*(resolved['result']=='WIN').mean():.1f}%")
            st.caption('Rata de reușită nu reprezintă profit. ROI necesită cote reale înregistrate înainte de meci.')
        st.dataframe(df, hide_index=True, use_container_width=True)
        st.download_button('Exportă jurnalul (opțional)',df.to_csv(index=False).encode('utf-8-sig'),'v9_jurnal.csv','text/csv')
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        st.error(f'Jurnal indisponibil: {exc}')

def main():
    st.set_page_config(page_title='Football Analyzer V9', page_icon='⚽', layout='wide')
    st.title('⚽ Football Analyzer V9 — GG & Peste 2,5')
    st.caption('Toate competițiile disponibile; jurnal automat opțional. Predicțiile nu garantează câștiguri.')
    mode = st.radio('Alege analiza', ['Meciuri viitoare', 'Jurnal automat', 'Testare istorică V5'], horizontal=True)
    if not KEY:
        st.warning('Adaugă FOOTBALL_DATA_KEY în Streamlit → Settings → Secrets.')
    elif mode == 'Meciuri viitoare':
        day = st.date_input('Data meciurilor', date.today())
        include_cups = st.checkbox('Include și cupele (Champions League, Europa League, dacă API permite)', value=True)
        try:
            available_comps = accessible(include_cups=include_cups)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            st.error(f'Nu pot încărca lista competițiilor: {exc}')
            available_comps = []
        comp_names = {c['code']: f"{c.get('name', c['code'])} ({c['code']})" for c in available_comps}
        chosen_codes = st.multiselect('Campionate / competiții analizate', options=list(comp_names),
                                      default=list(comp_names), format_func=lambda c: comp_names[c],
                                      help='Mai multe competiții înseamnă mai multe cereri API. Cupele pot avea istoric insuficient.')
        threshold = st.slider('Estimare minimă (%)', 30, 90, 55)
        minimum = st.slider('Minimum meciuri istorice per echipă', 5, 12, 8)
        min_odds = st.number_input('Cotă minimă de urmărit', min_value=1.01, max_value=20.0, value=1.60, step=0.05)
        edge_min = st.slider('Avantaj estimat minim față de cotă (puncte procentuale)', 0, 20, 5)
        max_disagreement = st.slider('Diferență maximă V4–V5 (puncte procentuale)', 0, 30, 12)
        st.caption('V4 este estimarea principală. V5 este comparator. După analiză preia cotele disponibile.')
        if st.button('Analizează meciurile', type='primary'):
            rows, skipped, excluded = [], [], []
            comps = [c for c in available_comps if c['code'] in chosen_codes]
            if not comps:
                st.warning('Selectează cel puțin o competiție.')
            bar = st.progress(0)
            for i, comp in enumerate(comps):
                code = comp['code']
                try:
                    matches = matches_for_competition(code, day, day)
                    upcoming = [m for m in matches if m.get('status') in ('SCHEDULED', 'TIMED')]
                    if upcoming:
                        hist = valid_finished(matches_for_competition(code, day-timedelta(days=365), day-timedelta(days=1)))
                        for m in upcoming:
                            h, a = m.get('homeTeam', {}).get('id'), m.get('awayTeam', {}).get('id')
                            if not h or not a:
                                excluded.append({'Competiție': code, 'Meci': str(m.get('id')), 'Motiv': 'Lipsesc ID-urile echipelor'})
                                continue
                            result = predict_v4(hist, h, a, minimum)
                            comparator = predict_v5(hist, h, a, minimum)
                            if result is None or comparator is None:
                                excluded.append({'Competiție': code, 'Meci': f"{m['homeTeam'].get('name', '?')} – {m['awayTeam'].get('name', '?')}", 'Motiv': 'Istoric insuficient pentru V4 sau V5'})
                                continue
                            for market, p, p5 in zip(('GG – DA', 'Peste 2,5'), result, comparator):
                                if p*100 >= threshold:
                                    try:
                                        clock = pd.Timestamp(m['utcDate']).tz_convert(ZoneInfo('Europe/Bucharest')).strftime('%H:%M')
                                    except (KeyError, ValueError, TypeError):
                                        clock = '?'
                                    rows.append({'Ora România': clock, 'Campionat': comp.get('name', code),
                                                 'Meci': f"{m['homeTeam'].get('name', '?')} – {m['awayTeam'].get('name', '?')}",
                                                 'Start UTC': m['utcDate'], 'Piață': market, 'V4 (%)': round(p*100, 1), 'V5 (%)': round(p5*100, 1),
                                                 'Diferență modele (pp)': round(abs(p-p5)*100, 1), 'Cotă': None})
                except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                    skipped.append(f'{code}: {str(exc)[:80]}')
                bar.progress((i+1)/max(1, len(comps)))
            if rows:
                df = pd.DataFrame(rows).sort_values('V4 (%)', ascending=False).reset_index(drop=True)
                st.session_state['v7_selections'] = df
            else:
                st.session_state.pop('v7_selections', None)
                st.info('Nicio selecție pentru filtrele actuale.')
            if excluded:
                with st.expander(f'Meciuri fără predicție ({len(excluded)})'):
                    st.dataframe(pd.DataFrame(excluded), hide_index=True, use_container_width=True)
            if skipped:
                with st.expander(f'Competiții omise ({len(skipped)})'):
                    st.write(skipped)
        if 'v7_selections' in st.session_state:
            st.subheader('Selecții și cote — mai multe case sau completare manuală')
            st.info('Pentru Peste 2,5, preluarea automată a cotelor este dezactivată temporar. Introdu manual cota verificată la casa de pariuri.')
            if not PROPLINE_KEY:
                st.info('Adaugă PROPLINE_API_KEY în Streamlit Secrets pentru preluarea cotelor.')
            elif st.button('🔄 Preia cotele de la casele disponibile (PropLine)', type='secondary'):
                with st.spinner('Caut cote în campionatele disponibile...'):
                    try:
                        found, matched, events_count, sports_count, errors = apply_prop_odds(st.session_state['v7_selections'])
                        st.session_state['v7_selections'] = found
                        st.session_state.pop('v7_odds_editor', None)
                        st.success(f'Cote găsite pentru {matched} selecții; {events_count} evenimente verificate din {sports_count} competiții PropLine.')
                        if errors:
                            with st.expander(f'Erori la {len(errors)} competiții'):
                                st.write(errors)
                    except (requests.RequestException, RuntimeError, ValueError) as exc:
                        st.error(f'Nu am putut prelua cotele: {exc}')
            if PROPLINE_KEY and st.button('🔍 Diagnostic PropLine — de ce lipsesc cotele?'):
                with st.spinner('Verific evenimentele și piețele disponibile...'):
                    try:
                        diagnostic, total_events, total_sports, diag_errors = prop_diagnostics(st.session_state['v7_selections'])
                        st.info(f'Diagnostic: {total_events} evenimente din {total_sports} competiții. Nu sunt afișate chei API.')
                        st.dataframe(diagnostic, hide_index=True, use_container_width=True)
                        st.download_button('Descarcă diagnostic CSV', diagnostic.to_csv(index=False).encode('utf-8-sig'),
                                           'football_v71_diagnostic.csv', 'text/csv')
                        if diag_errors:
                            with st.expander(f'Erori API ({len(diag_errors)})'):
                                st.write(diag_errors)
                    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                        st.error(f'Diagnostic indisponibil: {str(exc)[:200]}')
            st.caption('Sunt afișate numai casele și piețele returnate de furnizor. Verifică disponibilitatea și cota la casa aleasă înainte de orice decizie.')
            st.caption('Nu introduce cote inventate. Poți completa manual cotele lipsă; dacă modifici una, verifică sursa și ora.')
            edited = st.data_editor(st.session_state['v7_selections'], hide_index=True, use_container_width=True,
                                    disabled=[c for c in st.session_state['v7_selections'].columns if c != 'Cotă'],
                                    column_config={'Cotă': st.column_config.NumberColumn('Cotă', min_value=1.01, max_value=100.0, step=0.01)},
                                    key='v7_odds_editor')
            out = edited.copy()
            out['Probabilitate cotă (%)'] = out['Cotă'].apply(lambda o: round(100/o, 1) if pd.notna(o) and o > 1 else None)
            out['Avantaj estimat (pp)'] = out.apply(lambda r: round(r['V4 (%)']-r['Probabilitate cotă (%)'], 1) if pd.notna(r['Probabilitate cotă (%)']) else None, axis=1)
            out['EV estimat (%)'] = out.apply(lambda r: round(r['V4 (%)']*r['Cotă']-100, 1) if pd.notna(r['Cotă']) and r['Cotă'] > 1 else None, axis=1)
            out['Evaluare'] = out.apply(lambda r: evaluation_reason(r, min_odds, edge_min, max_disagreement), axis=1)
            candidates = out[out['Evaluare'] == 'Candidat pentru verificare, nu pariu sigur']
            st.subheader(f'Candidați care trec filtrele: {len(candidates)}')
            if len(candidates):
                st.dataframe(candidates, use_container_width=True, hide_index=True)
            else:
                st.info('Nicio selecție nu trece toate filtrele. Nu le relaxa doar pentru a obține pariuri.')
            st.subheader('Toate selecțiile și motivele respingerii')
            st.dataframe(out, use_container_width=True, hide_index=True)
            st.download_button('Descarcă evaluarea V9 CSV', out.to_csv(index=False).encode('utf-8-sig'), 'football_v9_evaluare.csv', 'text/csv')
            st.subheader('Jurnal pentru verificarea rezultatelor')
            st.caption('Descarcă jurnalul înainte de meci și păstrează fișierul. Nu se salvează permanent pe server.')
            stake = st.number_input('Miză de simulare per selecție (lei)', min_value=1.0, max_value=10000.0, value=20.0, step=1.0)
            journal = journal_export(out, stake)
            st.download_button('Descarcă jurnalul înainte de meci (CSV)',
                               journal.to_csv(index=False).encode('utf-8-sig'),
                               f'football_v8_jurnal_{day.isoformat()}.csv', 'text/csv')
            with st.expander('Verifică rezultatele unui jurnal salvat'):
                st.caption('În CSV, completează Pariu plasat? cu DA numai pentru pariurile efectiv plasate, apoi golurile finale. Nu modifica predicțiile sau cotele inițiale.')
                uploaded = st.file_uploader('Încarcă jurnalul CSV completat', type=['csv'], key='v8_journal_upload')
                if uploaded is not None:
                    try:
                        past = pd.read_csv(uploaded)
                        needed = {'Pariu plasat? (DA/NU)', 'Goluri gazde', 'Goluri oaspeți', 'Cotă', 'Piață', 'Miză planificată (lei)'}
                        if not needed.issubset(past.columns):
                            st.error('Jurnal incomplet: lipsesc coloane obligatorii.')
                        else:
                            settled = settle_journal(past)
                            actual = settled[(settled['Pariu plasat? (DA/NU)'].astype(str).str.upper() == 'DA') &
                                             (settled['Rezultat (CÂȘTIG/PIERDERE/NEJUCAT)'].isin(['CÂȘTIG','PIERDERE']))]
                            st.dataframe(settled, hide_index=True, use_container_width=True)
                            if len(actual):
                                profit = pd.to_numeric(actual['Profit net (lei)'], errors='coerce').sum()
                                stakes = pd.to_numeric(actual['Miză planificată (lei)'], errors='coerce').sum()
                                st.metric('Profit net înregistrat (lei)', f'{profit:.2f}')
                                st.metric('ROI pe pariurile soluționate', f'{100*profit/stakes:.1f}%' if stakes else '—')
                            st.download_button('Descarcă jurnalul verificat', settled.to_csv(index=False).encode('utf-8-sig'),
                                               'football_v8_jurnal_verificat.csv', 'text/csv')
                    except (ValueError, pd.errors.ParserError, UnicodeError) as exc:
                        st.error(f'CSV invalid: {exc}')
            st.warning('EV este o estimare dependentă de precizia modelului. Nu este profit demonstrat. Nu mări mizele pe baza acestor rezultate.')
    elif mode == 'Jurnal automat':
        show_automatic_journal()
    else:
        st.subheader('📊 Testare istorică: V4 vs V5')
        st.caption('Fiecare predicție folosește doar meciurile încheiate înainte de startul ei. Ultimele 60 de zile sunt evaluate dintr-o fereastră de 365 de zile.')
        end = st.date_input('Ultima zi inclusă în test', date.today()-timedelta(days=1), max_value=date.today()-timedelta(days=1))
        minimum = st.slider('Minimum meciuri istorice per echipă', 5, 12, 8)
        compare = st.checkbox('Compară și cu o perioadă anterioară (mai multe cereri API)')
        earlier = st.date_input('Ultima zi a perioadei anterioare', end-timedelta(days=120), max_value=end-timedelta(days=60)) if compare else None
        st.warning('Comparația este exploratorie. Pentru o evaluare independentă trebuie testate date viitoare, fără reglarea modelului pe rezultate.')
        if st.button('Testează V4 și V5', type='primary'):
            periods = [('Perioada principală', end)] + ([('Perioada anterioară', earlier)] if compare else [])
            comps = accessible()
            rows, skipped = [], []
            bar = st.progress(0)
            status = st.empty()
            total = len(periods)*len(comps)
            for pi, (period, finish) in enumerate(periods):
                for ci, comp in enumerate(comps):
                    code = comp['code']
                    status.caption(f'{period}: {comp.get("name", code)} ({pi*len(comps)+ci+1}/{total})')
                    try:
                        finished = valid_finished(matches_for_competition(code, finish-timedelta(days=364), finish))
                        for index, m in enumerate(finished):
                            match_day = date.fromisoformat(m['utcDate'][:10])
                            if not (finish-timedelta(days=59) <= match_day <= finish):
                                continue
                            # Strictly earlier kickoff; do not include the target or simultaneous matches.
                            past = [x for x in finished[:index] if x['utcDate'] < m['utcDate']]
                            h, a = m['homeTeam']['id'], m['awayTeam']['id']
                            hg, ag = m['score']['fullTime']['home'], m['score']['fullTime']['away']
                            for name, fn in [('V4', predict_v4), ('V5', predict_v5)]:
                                pred = fn(past, h, a, minimum)
                                if pred is None:
                                    continue
                                for market, p, actual in [('GG – DA', pred[0], int(hg>0 and ag>0)),
                                                          ('Peste 2,5', pred[1], int(hg+ag>=3))]:
                                    rows.append({'Perioada': period, 'Data': match_day.isoformat(),
                                                 'Campionat': comp.get('name', code), 'Meci': f"{m['homeTeam'].get('name','?')} – {m['awayTeam'].get('name','?')}",
                                                 'Model': name, 'Piață': market, 'Estimat (%)': round(p*100, 1),
                                                 'Rezultat real': 'DA' if actual else 'NU', 'Probabilitate': p, 'Real': actual})
                    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                        skipped.append(f'{period} / {code}: {str(exc)[:100]}')
                    bar.progress((pi*len(comps)+ci+1)/max(1, total))
            status.empty()
            if not rows:
                st.warning('Nu există suficiente date pentru testare.')
            else:
                df, summary = evaluate(rows)
                st.subheader('Rezultate generale — V4 vs V5')
                st.dataframe(summary, use_container_width=True, hide_index=True)
                st.caption('Brier mai mic este mai bun. Brier de bază folosește frecvența reală a perioadei ca reper descriptiv, nu ca predicție disponibilă înaintea meciului.')
                st.subheader('Rezultate pe campionate')
                by_league = []
                for (period, league, model, market), part in df.groupby(['Perioada','Campionat','Model','Piață']):
                    for threshold in (55, 60, 65, 70):
                        chosen = part[part['Estimat (%)'] >= threshold]
                        by_league.append({'Perioada':period, 'Campionat':league, 'Model':model, 'Piață':market,
                                          'Prag (%)':threshold, 'Meciuri':len(part), 'Selecții':len(chosen),
                                          'Reușită (%)':round(100*chosen['Real'].mean(),1) if len(chosen) else None,
                                          'Brier':round(((part['Probabilitate']-part['Real'])**2).mean(),4)})
                leagues = pd.DataFrame(by_league)
                st.dataframe(leagues, use_container_width=True, hide_index=True)
                st.download_button('Descarcă comparația generală CSV', summary.to_csv(index=False).encode('utf-8-sig'), 'football_v5_comparatie.csv', 'text/csv')
                st.download_button('Descarcă raportul pe campionate CSV', leagues.to_csv(index=False).encode('utf-8-sig'), 'football_v5_campionate.csv', 'text/csv')
                visible = df.drop(columns=['Probabilitate','Real'])
                st.download_button('Descarcă meciurile testate CSV', visible.to_csv(index=False).encode('utf-8-sig'), 'football_v6_meciuri.csv', 'text/csv')
                with st.expander('Vezi meciurile testate'):
                    st.dataframe(visible, use_container_width=True, hide_index=True)
            if skipped:
                with st.expander(f'Competiții omise ({len(skipped)})'):
                    st.write(skipped)

if __name__ == '__main__':
    main()
