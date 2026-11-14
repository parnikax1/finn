# FINN · NSE portfolio research demo

FINN is a mobile-first FastAPI prototype for reviewing portfolio filings, finding
relevant primary-source excerpts, and identifying a synthetic contact who could
help an investor interpret a filing.

## Run a clean checkout

Use Python 3.12 on macOS or Linux.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-app.txt
sh start.sh
```

Open [Company research](http://127.0.0.1:8000/research) or the
[portfolio feed](http://127.0.0.1:8000/). Startup seeds SQLite from the bundled
snapshot. It needs only the repository files after dependencies are installed.
The seed runs once per snapshot version and database.

## Reviewer walkthrough

The **12 November 2026 IST** demo snapshot contains 30 real NSE filings from
6–12 November across 12 companies in the 15-company demo portfolio. It includes
selected WIPRO and MARUTI PDF text with physical page numbers and canonical source
links. The MARUTI market example covers the 11 November 2026 trading session.
The source files themselves are external. Search and the evidence text in this
walkthrough remain available if those sites are down.

| Step | What to try |
| --- | --- |
| Push and Network | On the feed, tap **WIPRO appointment**. Confirm the appointment excerpt cites physical PDF page 2. Check the **Synthetic contact** badge, the sector match and the suggested question. |
| Pull | Tap **MARUTI research**. The prefilled search asks about exports and production capacity. The FY 2025–26 annual report, PDF page 68, reports the increase from approximately 2.4 million to 2.9 million annual units during FY 2026–27 and separately describes 4 million units as an objective. Review the cited excerpts and the 27 October 2026 call results. |
| Market signals | Tap **MARUTI market signals**. Check **+1.99%** and **1.88×** for 11 November, the prior close, and the three dated volume-baseline rows. |
| Live discovery | On Company research, enter `CUMMINSIND` and `How did domestic sales and exports change?`. Tap **Search excerpts** and then **Fetch documents**. The page reports progress and any source failures. This optional step needs upstream access. |

## Publish to GitHub and deploy on Render

1. Extract the release ZIP into a new directory. Create an empty **public**
   repository named `finn` at [GitHub New Repository](https://github.com/new).
   Leave README, license, and gitignore initialization unchecked.
2. From the extracted `finn-submission` directory, run the commands below.
   Replace `YOUR_GITHUB_USERNAME` with your account name.

```bash
git init -b main
git add .
git status --short
git commit -m "Package FINN reviewer demo"
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/finn.git
git push -u origin main
```

3. In the [Render dashboard](https://dashboard.render.com/), choose
   **New → Web Service**, connect GitHub, and select your `finn` repository.

   | Setting | Value |
   | --- | --- |
   | Branch | `main` |
   | Runtime | `Python 3` |
   | Root directory | Leave blank |
   | Instance type | Free |
   | Build command | `python -m pip install -r requirements-app.txt` |
   | Start command | `FINN_HOST=0.0.0.0 sh start.sh` |
   | Health check path | `/health` |
   | Environment variables | Leave blank for the public demo |

4. Click **Deploy Web Service**. Once healthy, open the assigned HTTPS
   `onrender.com` URL and run the phone checklist below.

Render provides `PORT`; the repository selects Python 3.12 via
`.python-version`. The free instance can take about a minute to wake. Runtime
SQLite and live downloads may disappear on a restart or sleep. Each fresh
instance rebuilds the review snapshot offline. Live fetch depends on current
NSE access and may fail or time out on a hosting network.

### Phone checklist

- Open the HTTPS URL in a private browser tab. Check that the snapshot date and
  three walkthrough links fit without horizontal scrolling.
- Open WIPRO. Check for the page 2 excerpt, original filing link and synthetic
  contact. Return to the feed.
- Open MARUTI research. Confirm the capacity excerpt and PDF page link. The
  search works without credentials and **Answer from excerpts** is hidden.
- Check the dated market signals. Try the unseen ticker last and confirm either
  cited results or a clear source failure.

## Optional answers for local use

The app hides the answer button unless the server has `OPENAI_API_KEY` and the
search returned evidence. Create a key at
[OpenAI API keys](https://platform.openai.com/api-keys), then start the service
from the same terminal:

```bash
export OPENAI_API_KEY="$(python -c 'import getpass; print(getpass.getpass("OpenAI API key: "))')"
sh start.sh
```

The optional answer route calls OpenAI from the server with the ticker, question
and retrieved excerpts. It checks that returned quotes match their cited
excerpts and shows unanswered question parts. The answer button was tested with
**simulated model responses against real PDF excerpts**. No live model request
was used in release verification.

## Data and deployment notes

- **Push:** exchange metadata is prioritised by explicit rules. The refresh
  command imports filings and page-linked evidence. A flag is a review cue.
- **Market signals:** dated NSE EQ rows feed the previous-close price change
  and the mean of prior session volumes. The sample uses 1.5% and 1.5× thresholds
  and three prior sessions, including the 8 November Muhurat session. The market
  command rejects mismatched CSV dates and keeps earlier results when inputs fail.
- **Pull:** SQLite stores page text for keyword and phrase search. The bundled
  MARUTI sample is limited to selected pages; extracted tables can lose layout.
  Browser discovery validates each symbol and uses bounded fetch and extraction.
- **Network:** contact profiles are fictional. Company or sector tags explain
  matches. A real private network would require authentication, access control
  and consent before storing any contacts.
- **Storage:** `demo/snapshot.json` contains curated public filing fields, selected
  source excerpts and market observations. The repository omits original PDFs,
  raw responses, local databases, runtime logs and credentials. Local app state
  defaults to `.finn/`. Startup reconstructs the packaged snapshot
  from the bundled JSON.
- **Operations:** `python -m finn_app.refresh --help` and
  `python -m finn_app.market --help` describe the separate refresh commands.
  Browser requests read the local index for cached searches. No scheduled refresh,
  trading execution, or autonomous agent is included.

Developer tests:

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests
```

For submission, include the Render URL, public GitHub URL, AI build-history links
or export, and the short architecture writeup in the covering email.
