import math
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='Football Analyzer V5', page_icon='⚽', layout='wide')
st.title('⚽ Football Analyzer V5 — GG & Peste 2,5')
st.caption('V5 compară două modele pe aceleași meciuri. Estimările nu garantează profit.')
BASE = 'https://api.football-data.org/v4'
try:
    KEY = st.secrets.get('FOOTBALL_DATA_KEY', '')
except Exception:
    KEY = ''

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
    st.caption('Se afișează estimările V5; modelul poate omite meciuri cu istoric insuficient.')
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
                        result = predict_v5(hist, h, a, minimum)
                        if result is None:
                            continue
                        for market, p in zip(('GG – DA', 'Peste 2,5'), result):
                            if p*100 >= threshold:
                                try:
                                    clock = pd.Timestamp(m['utcDate']).tz_convert(ZoneInfo('Europe/Bucharest')).strftime('%H:%M')
                                except (KeyError, ValueError, TypeError):
                                    clock = '?'
                                rows.append({'Ora România': clock, 'Campionat': comp.get('name', code),
                                             'Meci': f"{m['homeTeam'].get('name', '?')} – {m['awayTeam'].get('name', '?')}",
                                             'Piață': market, 'Estimare (%)': round(p*100, 1)})
            except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                skipped.append(f'{code}: {str(exc)[:80]}')
            bar.progress((i+1)/max(1, len(comps)))
        if rows:
            df = pd.DataFrame(rows).sort_values('Estimare (%)', ascending=False)
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button('Descarcă selecțiile CSV', df.to_csv(index=False).encode('utf-8-sig'), 'football_v5_selectii.csv', 'text/csv')
        else:
            st.info('Nicio selecție pentru filtrele actuale.')
        if skipped:
            with st.expander(f'Competiții omise ({len(skipped)})'):
                st.write(skipped)
else:
    st.subheader('📊 Comparație V4 vs V5 pe aceleași meciuri')
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
            st.download_button('Descarcă meciurile testate CSV', visible.to_csv(index=False).encode('utf-8-sig'), 'football_v5_meciuri.csv', 'text/csv')
            with st.expander('Vezi meciurile testate'):
                st.dataframe(visible, use_container_width=True, hide_index=True)
        if skipped:
            with st.expander(f'Competiții omise ({len(skipped)})'):
                st.write(skipped)
