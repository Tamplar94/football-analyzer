import math
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='Football Analyzer V4', page_icon='⚽', layout='wide')
st.title('⚽ Football Analyzer V4 — GG & Peste 2,5')
st.caption('Model Poisson experimental. Testarea istorică nu garantează rezultate viitoare sau profit.')
BASE = 'https://api.football-data.org/v4'
try:
    KEY = st.secrets.get('FOOTBALL_DATA_KEY', '')
except Exception:
    KEY = ''

session = requests.Session()
# Rate limiter shared between cached calls in this app process.
# Conservative for football-data.org free plan (10 requests/minute).
MIN_INTERVAL = 7.0
_last_request = [0.0]


def wait_for_slot():
    elapsed = time.monotonic() - _last_request[0]
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_request[0] = time.monotonic()


def retry_delay(response, attempt):
    raw = response.headers.get('Retry-After', '')
    try:
        return min(120.0, max(7.0, float(raw)))
    except ValueError:
        try:
            delta = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
            return min(120.0, max(7.0, delta))
        except (TypeError, ValueError, OverflowError):
            return min(120.0, 65.0 * (attempt + 1))


def api(path, params=None):
    for attempt in range(4):
        wait_for_slot()
        response = session.get(BASE + path, headers={'X-Auth-Token': KEY},
                               params=params or {}, timeout=30)
        if response.status_code == 429:
            if attempt == 3:
                raise RuntimeError('Limita API persistă după 4 încercări. Reîncearcă mai târziu.')
            delay = retry_delay(response, attempt)
            time.sleep(delay)
            continue
        if response.status_code in (401, 403):
            # 403 may be league-specific; preserve it for the caller.
            response.raise_for_status()
        response.raise_for_status()
        return response.json()
    raise RuntimeError('API indisponibil temporar.')

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

mode = st.radio('Alege analiza', ['Meciuri viitoare', 'Testare istorică V4'], horizontal=True)

if mode == 'Meciuri viitoare':
    match_date = st.date_input('Data meciurilor', value=date.today())
    min_probability = st.slider('Estimare minimă (%)', 30, 90, 55)
    max_matches = st.slider('Număr maxim de selecții', 5, 100, 30)
    min_games = st.slider('Minimum meciuri istorice per echipă', 5, 12, 8)
    st.info('Estimările sunt exploratorii: fără xG, cote, accidentări, avantaj teren propriu calibrat sau backtesting.')
    st.caption('Analiza tuturor campionatelor poate dura câteva minute. Nu apăsa repetat pe buton.')

    if not KEY:
        st.warning('Configurează FOOTBALL_DATA_KEY în Streamlit → Manage app → Settings → Secrets.')
    elif st.button('Analizează meciurile', type='primary'):
        try:
            available = competitions()
            selected = [c for c in available if c.get('type') == 'LEAGUE' and c.get('code')]
            if not selected:
                st.warning('Contul API nu returnează campionate accesibile.')
            rows, checked, skipped = [], 0, []
            status_box = st.empty()
            progress = st.progress(0)
            for i, comp in enumerate(selected):
                code = comp['code']
                status_box.caption(f'Analizez campionatul {i + 1}/{len(selected)}: {comp.get("name", code)}')
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
                        skipped.append(f'{code}: HTTP {status}; verifică jurnalul')
                except (requests.RequestException, RuntimeError) as exc:
                    skipped.append(f'{code}: {str(exc)[:110]}')
                progress.progress((i + 1) / max(1, len(selected)))
            status_box.empty()
            if rows:
                df = pd.DataFrame(rows).sort_values('Estimare (%)', ascending=False).head(max_matches)
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.download_button('Descarcă analiza CSV', df.to_csv(index=False).encode('utf-8-sig'), 'analiza_meciuri_v21.csv', 'text/csv')
            else:
                st.warning('Nu există selecții care să îndeplinească filtrele. Încearcă altă dată sau un prag mai mic.')
            st.caption(f'Competiții accesibile: {len(selected)}. Meciuri programate examinate: {checked}. Orele sunt în fusul României.')
            if skipped:
                with st.expander(f'Competiții omise ({len(skipped)})'):
                    st.write(', '.join(skipped))
        except Exception as exc:
            st.error(f'Nu s-au putut prelua datele: {exc}')


else:
    st.subheader('📊 Testare V4: campionate, praguri și perioade')
    st.caption('Pentru fiecare meci, modelul folosește numai rezultatele anterioare acelui meci. Se testează ultimele 60 de zile dintr-un interval istoric de 365 de zile.')
    end_date = st.date_input('Ultima zi inclusă în test', value=date.today() - timedelta(days=1), max_value=date.today() - timedelta(days=1), key='test_end')
    min_games_bt = st.slider('Minimum meciuri istorice per echipă', 5, 12, 8, key='test_min_games')
    st.caption('Comparăm simultan pragurile 55%, 60%, 65% și 70%, fără să alegem unul după rezultate.')
    compare_period = st.checkbox('Compară și cu o perioadă anterioară (mai multe cereri API)', value=False)
    if compare_period:
        other_end = st.date_input('Ultima zi a perioadei anterioare', value=end_date - timedelta(days=120), max_value=end_date - timedelta(days=60), key='other_end')
    st.warning('Atenție: fără cote nu putem calcula profitabilitatea. Datele disponibile în planul API pot limita numărul de meciuri testate.')
    if not KEY:
        st.warning('Configurează FOOTBALL_DATA_KEY în Streamlit → Manage app → Settings → Secrets.')
    elif st.button('Testează toate campionatele', type='primary'):
        try:
            available = competitions()
            selected = [c for c in available if c.get('type') == 'LEAGUE' and c.get('code')]
            periods = [('Perioada principală', end_date)]
            if compare_period:
                periods.append(('Perioada anterioară', other_end))
            results, skipped = [], []
            progress = st.progress(0)
            status_box = st.empty()
            for period_name, period_end in periods:
                start_date = period_end - timedelta(days=364)
                test_start = period_end - timedelta(days=59)
                for i, comp in enumerate(selected):
                    code = comp['code']
                    status_box.caption(f'{period_name}: {i + 1}/{len(selected)} — {comp.get("name", code)}')
                    try:
                        all_matches = matches_for_competition(code, start_date, period_end)
                        finished = [m for m in all_matches if m.get('status') == 'FINISHED'
                                    and m.get('score', {}).get('fullTime', {}).get('home') is not None
                                    and m.get('score', {}).get('fullTime', {}).get('away') is not None
                                    and m.get('utcDate')]
                        finished.sort(key=lambda m: m['utcDate'])
                        for match in finished:
                            match_day = date.fromisoformat(match['utcDate'][:10])
                            if not (test_start <= match_day <= period_end):
                                continue
                            home, away = match.get('homeTeam', {}), match.get('awayTeam', {})
                            if not home.get('id') or not away.get('id'):
                                continue
                            # Exclude target match and all games with same or later kickoff.
                            past = [m for m in finished if m['utcDate'] < match['utcDate']]
                            h = team_stats(past, home['id'], match_day + timedelta(days=1), min_games=min_games_bt)
                            a = team_stats(past, away['id'], match_day + timedelta(days=1), min_games=min_games_bt)
                            if h is None or a is None:
                                continue
                            gg, over25 = estimate(h, a)
                            hg, ag = match['score']['fullTime']['home'], match['score']['fullTime']['away']
                            for market, prob, actual in [('GG – DA', gg, int(hg > 0 and ag > 0)),
                                                         ('Peste 2,5', over25, int(hg + ag >= 3))]:
                                results.append({'Perioada': period_name, 'Data': match_day.isoformat(),
                                                'Campionat': comp.get('name', code),
                                                'Meci': f"{home.get('name', '?')} – {away.get('name', '?')}",
                                                'Piață': market, 'Estimat (%)': round(prob * 100, 1),
                                                'Rezultat real': 'DA' if actual else 'NU',
                                                'Probabilitate': prob, 'Real': actual})
                    except requests.HTTPError as exc:
                        status = exc.response.status_code if exc.response is not None else '?'
                        skipped.append(f'{period_name} / {code}: HTTP {status}')
                    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                        skipped.append(f'{period_name} / {code}: {str(exc)[:90]}')
                    progress.progress((periods.index((period_name, period_end)) * len(selected) + i + 1) / max(1, len(periods) * len(selected)))
            status_box.empty()
            if not results:
                st.warning('Nu sunt suficiente meciuri istorice pentru testare cu aceste setări. Încearcă o altă dată sau un minim de 5 meciuri.')
            else:
                df = pd.DataFrame(results)
                counts = df.groupby('Perioada').size().floordiv(2).to_dict()
                st.success('Meciuri evaluate: ' + ', '.join(f'{k}: {v}' for k, v in counts.items()))
                summary = []
                for (period_name, league, market), part in df.groupby(['Perioada', 'Campionat', 'Piață']):
                    for threshold in (55, 60, 65, 70):
                        chosen = part[part['Estimat (%)'] >= threshold]
                        brier = ((part['Probabilitate'] - part['Real']) ** 2).mean()
                        summary.append({'Perioada': period_name, 'Campionat': league, 'Piață': market,
                                        'Prag (%)': threshold, 'Meciuri testate': len(part),
                                        'Frecvența reală DA (%)': round(100 * part['Real'].mean(), 1),
                                        'Estimare medie (%)': round(100 * part['Probabilitate'].mean(), 1),
                                        'Selecții': len(chosen),
                                        'Reușită (%)': round(100 * chosen['Real'].mean(), 1) if len(chosen) else None,
                                        'Brier': round(brier, 3)})
                report = pd.DataFrame(summary)
                st.subheader('Rezultate generale pe perioade și praguri')
                overall = []
                for (period_name, market), part in df.groupby(['Perioada', 'Piață']):
                    for threshold in (55, 60, 65, 70):
                        chosen = part[part['Estimat (%)'] >= threshold]
                        overall.append({'Perioada': period_name, 'Piață': market, 'Prag (%)': threshold,
                                        'Meciuri testate': len(part), 'Selecții': len(chosen),
                                        'Reușită (%)': round(100 * chosen['Real'].mean(), 1) if len(chosen) else None,
                                        'Frecvență reală DA (%)': round(100 * part['Real'].mean(), 1),
                                        'Brier': round(((part['Probabilitate'] - part['Real']) ** 2).mean(), 3)})
                st.dataframe(pd.DataFrame(overall), use_container_width=True, hide_index=True)
                st.subheader('Rezultate pe fiecare campionat')
                st.caption('Atenție: 100% din 3 selecții nu demonstrează un model bun. Comparați și numărul de selecții. Pragurile au fost fixate înainte de evaluare.')
                st.dataframe(report, use_container_width=True, hide_index=True)
                st.download_button('Descarcă raportul pe campionate CSV', report.to_csv(index=False).encode('utf-8-sig'), 'football_v4_campionate.csv', 'text/csv')
                visible = df.drop(columns=['Probabilitate', 'Real']).sort_values(['Perioada', 'Data', 'Campionat'], ascending=[True, False, True])
                with st.expander('Vezi meciurile testate'):
                    st.dataframe(visible, use_container_width=True, hide_index=True)
                st.download_button('Descarcă meciurile CSV', visible.to_csv(index=False).encode('utf-8-sig'), 'football_v4_meciuri.csv', 'text/csv')
                st.caption('Fără cote istorice nu putem estima profitul. Scorul Brier mai mic este mai bun; verificați și comparația cu un model de bază.')
            if skipped:
                with st.expander(f'Competiții omise ({len(skipped)})'):
                    st.write(', '.join(skipped))
        except Exception as exc:
            st.error(f'Nu s-a putut finaliza testarea: {exc}')
