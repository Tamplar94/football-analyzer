import os, math, datetime
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title='Football Analyzer', page_icon='⚽', layout='wide')
st.title('⚽ Football Analyzer — GG & Peste 2,5')
st.caption('Estimări statistice experimentale, nu garanții sau recomandări de pariere.')
KEY = os.getenv('API_FOOTBALL_KEY', '')
BASE = 'https://v3.football.api-sports.io'

def api(path, params):
    r=requests.get(BASE+path,headers={'x-apisports-key':KEY},params=params,timeout=25)
    r.raise_for_status()
    payload=r.json()
    if payload.get('errors'):
        raise RuntimeError(str(payload['errors']))
    return payload.get('response',[])

@st.cache_data(ttl=1800)
def fixtures(date):
    return api('/fixtures',{'date':date,'timezone':'Europe/Bucharest'})

@st.cache_data(ttl=3600)
def recent(team, before_date):
    from datetime import date, timedelta

    end = date.fromisoformat(before_date) - timedelta(days=1)
    start = end - timedelta(days=89)

    data = api('/fixtures', {
        'team': team,
        'from': start.isoformat(),
        'to': end.isoformat(),
        'status': 'FT'
    })

    data = [
        x for x in data
        if x['fixture']['date'][:10] < before_date
    ]

    return sorted(
        data,
        key=lambda x: x['fixture']['date'],
        reverse=True
    )[:30]

def stats(matches, team):
    if len(matches)<8: return None
    gf=[];ga=[]
    for m in matches:
        home=m['teams']['home']['id']==team
        a=m['goals']['home'] if home else m['goals']['away']
        b=m['goals']['away'] if home else m['goals']['home']
        if a is not None and b is not None:gf.append(a);ga.append(b)
    if len(gf)<8:return None
    return sum(gf)/len(gf),sum(ga)/len(ga)

def poisson(k,lam):
    return math.exp(-lam)*lam**k/math.factorial(k)

def predict(h,a):
    # Baseline independent Poisson model, intentionally simple and not calibrated.
    lh=max(.15,min(4.0,(h[0]+a[1])/2))
    la=max(.15,min(4.0,(a[0]+h[1])/2))
    pgg=(1-math.exp(-lh))*(1-math.exp(-la))
    total=lh+la
    pover=1-math.exp(-total)*(1+total+total**2/2)
    return pgg,pover

selected=st.date_input('Data meciurilor (ora României)',datetime.date.today())
min_p=st.slider('Prag minim estimat (%)',50,90,60)
limit=st.slider('Număr maxim de meciuri analizate',10,150,50,10)
st.info('Versiunea 0.1 folosește un model Poisson simplu. Nu include încă xG, accidentări, cote sau validare istorică. Estimările NU sunt calibrate.')
if not KEY:
    st.warning('Configurează API_FOOTBALL_KEY în variabilele de mediu ale serverului. Nu introduce cheia într-un fișier public.')
elif st.button('Analizează meciurile',type='primary'):
    try:
        allgames=fixtures(selected.isoformat())
        upcoming=[x for x in allgames if x['fixture']['status']['short']=='NS'][:limit]
        rows=[]
        bar=st.progress(0)
        for i,g in enumerate(upcoming):
            hid=g['teams']['home']['id']; aid=g['teams']['away']['id']
            h=stats(recent(hid,selected.isoformat()),hid)
            a=stats(recent(aid,selected.isoformat()),aid)
            if h and a:
                gg,over=predict(h,a)
                for market,p in [('GG - DA',gg),('Peste 2,5',over)]:
                    if p*100>=min_p:
                        rows.append({'Ora':g['fixture']['date'][11:16], 'Campionat':g['league']['name'], 'Meci':g['teams']['home']['name']+' – '+g['teams']['away']['name'],'Piață':market,'Probabilitate model (%)':round(p*100,1),'ID meci':g['fixture']['id']})
            bar.progress((i+1)/max(1,len(upcoming)))
        if rows:
            df=pd.DataFrame(rows).sort_values('Probabilitate model (%)',ascending=False)
            st.dataframe(df.head(20),hide_index=True,use_container_width=True)
            st.download_button('Descarcă analiza CSV',df.to_csv(index=False).encode('utf-8-sig'),'analiza.csv','text/csv')
        else:st.warning('Nu există selecții care îndeplinesc criteriile în meciurile analizate.')
        st.caption(f'Au fost examinate {len(upcoming)} meciuri. Limitările de trafic ale API-ului pot necesita un abonament plătit.')
    except Exception as e:
        st.error(f'Nu s-au putut prelua datele: {e}')
