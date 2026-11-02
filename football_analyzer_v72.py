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

st.set_page_config(page_title='Football Analyzer V7.2', page_icon='⚽', layout='wide')
st.title('⚽ Football Analyzer V7.2 — GG & Peste 2,5')
st.caption('V7.2: V4 este modelul principal, V5 comparator. Cote Superbet prin PropLine când sunt disponibile; completare manuală ca rezervă. Fără garanții de profit.')
BASE = 'https://api.football-data.org/v4'
try:
    KEY = st.secrets.get('FOOTBALL_DATA_KEY', '')
except Exception:
    KEY = ''

# PropLine: autentificarea se face doar pe server, prin Streamlit Secrets.
try:
    PROPLINE_KEY = st.secrets.get('PROPLINE_API_KEY', '')
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

def decimal_price(american):
    # PropLine folosește cote americane: +110 -> 2.10; -125 -> 1.80.
    try:
        p = float(american)
        if p >= 100:
            return round(1 + p / 100, 3)
        if p <= -100:
            return round(1 + 100 / abs(p), 3)
    except (ValueError, TypeError):
        pass
    return None

def market_price(event, selection):
    for book in event.get('bookmakers', []):
        if book.get('key') != 'superbet_ro':
            continue
        for market in book.get('markets', []):
            key = market.get('key')
            if selection == 'GG – DA' and key == 'both_teams_to_score':
                for outcome in market.get('outcomes', []):
                    if str(outcome.get('name', '')).strip().lower() in ('yes', 'da'):
                        return decimal_price(outcome.get('price')), market.get('last_update')
            if selection == 'Peste 2,5' and key == 'totals':
                for outcome in market.get('outcomes', []):
                    try:
                        correct_line = abs(float(outcome.get('point')) - 2.5) < 0.001
                    except (TypeError, ValueError):
                        correct_line = False
                    if correct_line and str(outcome.get('name', '')).lower() == 'over':
                        return decimal_price(outcome.get('price')), market.get('last_update')
    return None, None

@st.cache_data(ttl=300, show_spinner=False)
def prop_soccer_odds():
    sports = prop_get('/sports')
    if isinstance(sports, dict):
        sports = sports.get('sports', sports.get('data', []))
    keys = [sp['key'] for sp in sports if isinstance(sp, dict) and
            str(sp.get('key', '')).startswith('soccer_') and sp.get('active', True)]
    events, errors = [], []
    for key in keys:
        try:
            payload = prop_get(f'/sports/{key}/odds',
                               {'markets': 'both_teams_to_score,totals', 'bookmakers': 'superbet_ro'})
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
    """Diagnostic fără chei API sau răspunsuri brute sensibile."""
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
            books = [b for b in event.get('bookmakers', []) if isinstance(b, dict) and b.get('key') == 'superbet_ro']
            markets = sorted({m.get('key', '?') for b in books for m in b.get('markets', []) if isinstance(m, dict)})
            report.append({'Meci analizat': row['Meci'], 'Loc': rank,
                           'Eveniment PropLine': f"{event.get('home_team', '?')} – {event.get('away_team', '?')}",
                           'Diferență ore': round(hours, 2), 'Potrivire nume (%)': round(score*100, 1),
                           'Potrivire sigură V7.2': safe_event_match(home, away, kickoff, event) is not None,
                           'Superbet prezent': bool(books), 'Piețe Superbet': ', '.join(markets) or '(niciuna)',
                           'GG disponibil': market_price(event, 'GG – DA')[0] is not None,
                           'Peste 2,5 disponibil': market_price(event, 'Peste 2,5')[0] is not None})
        if not near:
            report.append({'Meci analizat': row['Meci'], 'Loc': 0,
                           'Eveniment PropLine': '(niciun eveniment în intervalul ±3 ore)',
                           'Diferență ore': None, 'Potrivire nume (%)': None, 'Potrivire sigură V7.2': False,
                           'Superbet prezent': False, 'Piețe Superbet': '(niciuna)',
                           'GG disponibil': False, 'Peste 2,5 disponibil': False})
    return pd.DataFrame(report), len(events), sports_count, errors

def apply_prop_odds(df):
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
        # Acceptăm doar un singur eveniment verificat, fără asociere ambiguă.
        if len(candidates) != 1:
            continue
        odds, updated_at = market_price(candidates[0][2], row['Piață'])
        if odds and odds > 1:
            updated.at[i, 'Cotă'] = odds
            updated.at[i, 'Sursă cotă'] = 'PropLine / Superbet RO'
            updated.at[i, 'Actualizare cotă UTC'] = updated_at or ''
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

def accessible():
    return [c for c in competitions() if c.get('type') == 'LEAGUE' and c.get('code')]

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

mode = st.radio('Alege analiza', ['Meciuri viitoare', 'Testare istorică V5'], horizontal=True)
if not KEY:
    st.warning('Adaugă FOOTBALL_DATA_KEY în Streamlit → Settings → Secrets.')
elif mode == 'Meciuri viitoare':
    day = st.date_input('Data meciurilor', date.today())
    threshold = st.slider('Estimare minimă (%)', 30, 90, 55)
    minimum = st.slider('Minimum meciuri istorice per echipă', 5, 12, 8)
    min_odds = st.number_input('Cotă minimă de urmărit', min_value=1.01, max_value=20.0, value=1.80, step=0.05)
    edge_min = st.slider('Avantaj estimat minim față de cotă (puncte procentuale)', 0, 20, 5)
    max_disagreement = st.slider('Diferență maximă V4–V5 (puncte procentuale)', 0, 30, 12)
    st.caption('V4 este estimarea principală. V5 este doar comparator. După analiză apasă «Preia cotele Superbet».')
    if st.button('Analizează meciurile', type='primary'):
        rows, skipped = [], []
        comps = accessible()
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
                            continue
                        result = predict_v4(hist, h, a, minimum)
                        comparator = predict_v5(hist, h, a, minimum)
                        if result is None or comparator is None:
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
        if skipped:
            with st.expander(f'Competiții omise ({len(skipped)})'):
                st.write(skipped)
    if 'v7_selections' in st.session_state:
        st.subheader('Selecții și cote — Superbet automat sau completare manuală')
        if not PROPLINE_KEY:
            st.info('Adaugă PROPLINE_API_KEY în Streamlit Secrets pentru preluarea cotelor.')
        elif st.button('🔄 Preia cotele Superbet (PropLine)', type='secondary'):
            with st.spinner('Caut cote Superbet în campionatele disponibile...'):
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
        st.caption('Cotele pot fi indisponibile, întârziate sau diferite de cele din aplicația Superbet. Verifică înainte de orice decizie.')
        st.caption('Nu introduce cote inventate. Poți completa manual cotele lipsă; dacă modifici una, verifică sursa și ora.')
        edited = st.data_editor(st.session_state['v7_selections'], hide_index=True, use_container_width=True,
                                disabled=[c for c in st.session_state['v7_selections'].columns if c != 'Cotă'],
                                column_config={'Cotă': st.column_config.NumberColumn('Cotă', min_value=1.01, max_value=100.0, step=0.01)},
                                key='v7_odds_editor')
        out = edited.copy()
        out['Probabilitate cotă (%)'] = out['Cotă'].apply(lambda o: round(100/o, 1) if pd.notna(o) and o > 1 else None)
        out['Avantaj estimat (pp)'] = out.apply(lambda r: round(r['V4 (%)']-r['Probabilitate cotă (%)'], 1) if pd.notna(r['Probabilitate cotă (%)']) else None, axis=1)
        out['EV estimat (%)'] = out.apply(lambda r: round(r['V4 (%)']*r['Cotă']-100, 1) if pd.notna(r['Cotă']) and r['Cotă'] > 1 else None, axis=1)
        def flag(r):
            if pd.isna(r['Cotă']):
                return 'Fără cotă'
            if r['Cotă'] < min_odds or r['Avantaj estimat (pp)'] < edge_min:
                return 'Nu trece filtrele'
            if r['Diferență modele (pp)'] > max_disagreement:
                return 'Modelele nu sunt de acord'
            return 'Candidat pentru verificare, nu pariu sigur'
        out['Evaluare'] = out.apply(flag, axis=1)
        st.dataframe(out, use_container_width=True, hide_index=True)
        st.download_button('Descarcă evaluarea V7 CSV', out.to_csv(index=False).encode('utf-8-sig'), 'football_v7_evaluare.csv', 'text/csv')
        st.warning('EV este o estimare dependentă de precizia modelului. Nu este profit demonstrat. Nu mări mizele pe baza acestor rezultate.')
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
