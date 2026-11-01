# Football Analyzer — versiunea 0.1

Aplicație web responsive (telefon și calculator), cu selecții experimentale pentru **GG – DA** și **Peste 2,5 goluri**.

## Pornire locală

1. Instalează Python 3.11+.
2. Deschide terminalul în folderul proiectului și rulează `pip install -r requirements.txt`.
3. Creează o cheie la https://www.api-football.com/.
4. Configurează variabila de mediu `API_FOOTBALL_KEY` cu cheia ta (nu o publica).
5. Rulează `streamlit run app.py` și deschide adresa afișată.

## Telefon + calculator

Publică aplicația într-un serviciu de găzduire compatibil Streamlit, cu `API_FOOTBALL_KEY` configurată ca secret. Deschide URL-ul rezultat din orice browser. Publicarea nu este inclusă în acest pachet.

## Actualizare

Datele se reîncarcă la apăsarea butonului **Analizează meciurile**; cache-ul expiră după 30–60 minute. **Nu există încă actualizare automată nesupravegheată**. Pentru aceasta, următoarea versiune va avea un job programat, o bază de date și notificări opționale.

## Limitări importante

- Modelul Poisson este un **prototip necalibrat**, nu o predicție validată sau un indicator de profit.
- Media atacului și a apărării se bazează pe până la 12 rezultate anterioare, fără ponderarea forței adversarilor.
- API-ul poate limita numărul de solicitări; fiecare echipă necesită un apel suplimentar.
- În versiunea 0.1 nu sunt integrate cotele Superbet, accidentările, xG, evaluarea valorii pariului și backtesting-ul.
- Selectarea primelor 3 meciuri după probabilitate nu înseamnă că sunt pariuri profitabile.
- Unele meciuri pot lipsi din cauza limitelor API sau a lipsei datelor.

## Pași următori

1. Backtesting cronologic și calibrarea probabilităților.
2. Statistici acasă/deplasare și ajustarea forței adversarilor.
3. Surse autorizate pentru cote, xG și accidentări.
4. Calcularea valorii așteptate, cu praguri de selecție și opțiunea «niciun pariu».
5. Actualizare automată programată, istoric și notificări.
