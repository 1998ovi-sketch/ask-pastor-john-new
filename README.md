# Ask Pastor John — Complete Archive RSS

Acest proiect construiește **un singur feed RSS** pentru Ask Pastor John, păstrând:

- toate episoadele care există în RSS-ul oficial actual;
- episoadele istorice care au dispărut din fereastra de 1.000 de intrări;
- episoadele speciale (nu doar cele numerotate);
- linkurile către audio original găzduit de Desiring God.

## De ce este necesar

Feed-ul oficial actual returnează o fereastră de 1.000 de intrări. În fișierul verificat la
10 august 2026, cel mai vechi item este din 18 ianuarie 2019. Arhiva Desiring God însă
păstrează episoadele Ask Pastor John încă din 11 ianuarie 2013.

Generatorul recuperează istoria din arhivele anuale oficiale de interviuri Desiring God și creează un catalog
persistent. După prima rulare, un episod rămâne în feed chiar dacă ulterior cade din
RSS-ul oficial.

## Cea mai simplă instalare: GitHub Pages

1. Creează un repository GitHub gol și încarcă toate fișierele din acest folder.
2. În repository: **Settings → Pages → Build and deployment → Source → GitHub Actions**.
3. Deschide tab-ul **Actions** → workflow-ul **Build complete Ask Pastor John feed** →
   **Run workflow**.
4. Prima rulare durează mai mult deoarece reconstruiește arhiva istorică și verifică audio.
   Rulările următoare sunt rapide.
5. După succes, URL-ul feed-ului va fi:

   `https://USERNAME.github.io/REPOSITORY/ask-pastor-john-complete.rss`

## Adăugare în Apple Podcasts

Pe iPhone:

**Podcasts → Library → ⋯ → Follow a Show by URL**

Lipești URL-ul `ask-pastor-john-complete.rss` de la GitHub Pages.

## Siguranțe incluse

Generatorul este intenționat strict:

- nu publică dacă găsește prea puține episoade sau dacă nu ajunge până la episodul 1 din 11 ianuarie 2013;
- verifică faptul că audio istoric răspunde ca fișier audio;
- pentru episoade cu titlu/data modificată încearcă să găsească URL-ul exact din pagina
  canonică Desiring God;
- nu păstrează `itunes:new-feed-url` din RSS-ul oficial, fiindcă acea etichetă ar putea
  redirecționa Apple Podcasts înapoi către feed-ul oficial trunchiat;
- nu re-găzduiește audio: fișierele rămân pe infrastructura Desiring God;
- `catalog.json` devine memoria persistentă a feed-ului și este actualizat de workflow.

## Rulare locală

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python build_feed.py --output public/ask-pastor-john-complete.rss
```

Pe Windows, activarea este:

```powershell
.venv\Scripts\Activate.ps1
```

## Test cu RSS-ul seed inclus

Fișierul `seed/ask-pastor-john.rss` este copia pe care ai furnizat-o în conversație.
Poate fi folosită pentru a verifica parserul fără a descărca RSS-ul actual:

```bash
python build_feed.py \
  --seed-rss seed/ask-pastor-john.rss \
  --output public/ask-pastor-john-complete.rss
```

Pentru reconstruirea **istoriei**, conexiunea la internet este necesară.


### Sursele folosite de generator

Istoria este luată din paginile oficiale `desiringgod.org/dates/YYYY/interviews`, filtrând doar cardurile etichetate **Ask Pastor John**. RSS-ul curent rămâne sursa pentru episoadele recente.
