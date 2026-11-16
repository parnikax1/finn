# FINN · NSE portfolio research demo

FINN is a mobile-first FastAPI prototype for reviewing portfolio filings, finding
relevant primary-source excerpts, and identifying a synthetic contact who could
help an investor interpret a filing.

[Open the hosted demo](https://finn-08qn.onrender.com/).

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

The **12 November 2026 IST** snapshot contains 32 NSE filings from 6–12 November
across 13 names in the 15-company portfolio. It includes selected WIPRO and MARUTI
PDF text and the 11 November market refresh. The core walkthrough can run offline
after dependencies are installed. Original PDFs and CSVs are linked at their sources.

| Time | What to try |
| --- | --- |
| 0–20s | Open **Brief**. Under **Priority filings**, switch from **Last 7 days** to **Latest day**. **View all filings** changes from 32 to 11. In the archive, open HDFC Bank and use **Open original filing** beside the missing-excerpt message. |
| 20–45s | Return to **Last 7 days** and open Wipro. Switch from **Page 2 · Appointment** to **Page 3 · Previous role**. Open **Contact question** for the synthetic match. **Sources and export** contains the original PDF and downloadable brief. |
| 45–70s | In **Research**, select **MARUTI capacity**. The prepared walkthrough compares reported capacity of about 2.9 million units/year with a 4 million target and identifies the 500,000 units/year added in H1 FY 2026–27. **Sources searched** shows PDF links and page coverage. |
| 70–90s | In **Network**, find contacts for **MARUTI** to see an explained synthetic company match. In **Brief**, expand **All signals and baselines** under **Market signals** for the dated NSE metrics and source CSVs. |

Brief defaults to the last seven snapshot days so the Wipro filing is visible.
**Latest day** shows the latest available IST calendar day in the data.

After the walkthrough, try `CUMMINSIND` with **How did domestic sales and exports change?**
Select **Search excerpts**, then **Fetch documents** if needed. The transcript is shown
first when the report index includes table-like pages with mixed figures and labels.
The report’s original link remains available.

The raw refresh was verified against NSE on 15 November 2026 IST, using the
11 November session and 9, 8 and 6 November baselines. All 15 symbols returned usable
rows after updating `TATAMOTORS` to `TMPV` per NSE circular 70875.
`demo/market-verification.json` contains source URL, date, close and volume for each
input row. To repeat the same command, use:

```bash
python -m finn_app.market --as-of 2026-11-11 --price-pct 1.5 --volume-multiple 1.5 --volume-window 3
```

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

- Check the opening viewport and the filing window in a private phone tab.
- Follow WIPRO → pages 2 and 3 → synthetic contact question, then MARUTI → capacity comparison and follow-up.
- Expand the 15-company market status on the unfiltered feed.
- Try the live CUMMINSIND fetch separately. Confirm the transcript leads when
  mixed table pages are excluded and that unavailable sources show a useful status.

## Optional answers for local use

The app hides the answer button unless the server has `OPENAI_API_KEY` and the
search returned evidence. Create a key at
[OpenAI API keys](https://platform.openai.com/api-keys), then start the service
from the same terminal:

```bash
export OPENAI_API_KEY="$(python -c 'import getpass; print(getpass.getpass("OpenAI API key: "))')"
sh start.sh
```

The optional answer route uses `gpt-5-mini` for a bounded synthesis task. It
sends the ticker, question and retrieved excerpts from the server. It checks
that returned quotes match their cited
excerpts and shows unanswered question parts. The answer button was tested with
**simulated model responses against real PDF excerpts**. No live model request
was used in release verification.

## Data and deployment notes

- **Push:** `finn_app.refresh` fetches NSE filings and imports metadata and
  available, page-linked evidence into SQLite. Explicit rules prioritise filings
  for review. `finn_app.market` refreshes end-of-day signals separately.
- **Market signals:** dated NSE EQ rows feed the previous-close price change
  and the mean of prior session volumes. The sample uses 1.5% and 1.5× thresholds
  and three prior sessions, including the 8 November Muhurat session. The market
  command rejects mismatched CSV dates and keeps earlier results when inputs fail.
- **Pull:** SQLite stores page text for keyword and phrase search. The MARUTI comparison is a prepared walkthrough. The bundled
  MARUTI sample is limited to selected pages; extracted tables can lose layout.
  Browser discovery validates each symbol and uses bounded fetch and extraction.
- **Network:** the demo ships fictional contact profiles and collects no real
  contact records. Matches use exact company tags first, followed by sector tags.
  A private deployment would use per-investor authentication and access controls,
  collect only consented career fields, retain them while consent or account access
  is active, and delete them on withdrawal or account closure.
- **Storage:** `demo/snapshot.json` contains curated public filing fields, selected
  source excerpts. `demo/market-verification.json` contains curated market observations. The repository omits original PDFs,
  raw responses, local databases, runtime logs and credentials. Local app state
  defaults to `.finn/`. Startup reconstructs the packaged snapshot
  from the bundled JSON.
- **Operations:** Render runs FastAPI, Jinja and HTMX against an instance-local
  SQLite database seeded from the checked-in snapshot. `python -m finn_app.refresh --help` and
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

## Reviewer flow and backend follow-ups

Use **Brief**, **Research**, and **Network** to switch between the feed, source
search, and fictional contact lookup. Filing links preserve the active filters
and page. From WIPRO, open the cited excerpt or the original PDF, copy the
synthetic contact’s question, and download the offline FINN Markdown brief.
In Research, **Sources searched** identifies the PDFs, dates, publisher where
verified, and page coverage. Missing metadata is labelled unverified.

- Bundled MARUTI excerpt pages currently satisfy fetch eligibility for their
  document types. Prioritize fetching full documents when only sample pages exist.
- Research chooses one document per type before ranking excerpts. Evaluate
  ranking across more than one document of each type.
- Mixed-table exclusion is specific to CUMMINSIND. Prioritize evidence quality
  checks across tickers, including annual-report tables.
- The hosted fetch runs one job at a time. The current UI reports when the
  worker is busy.

Market CSV export, scheduled refresh, and model changes are deferred.

### Navigation on phones

Use the bottom Brief, Research, and Network bar to switch destinations. Each
tab reopens its last URL for the browser session. Within the current page
shell, returning to a tab also restores its draft fields and scroll position.
Direct URLs and ordinary links work without HTMX. On Brief, expand Market
signals for rows and CSVs. On WIPRO detail, expand Sources and export for
the exchange PDFs and the FINN Markdown brief.

### Short Brief and repeat navigation

Brief shows the WIPRO walkthrough when it matches the window, then up to two
additional flagged filings in priority order. View all filings carries the selected window
into the searchable list. Both WIPRO source filings have their own entries there.

Recently loaded tab screens can be reused in the same browser session. Research
can show a restored screen immediately while requesting the current fetch status
in the background. If a tab request fails, the current screen remains readable
and Retry requests the destination again.

The filing controls and results update as one fragment. Failed filing requests
restore the last applied filters. Returning to a cached Brief restores the
applied filter values with the loading message cleared. If Research cannot
reach the status endpoint, it shows this separately from a worker failure and
offers a link to check status.

The Brief spotlight combines published filings, the prepared Maruti capacity
walkthrough and dated market signals. Filing eligibility follows the selected
filing window. Maruti is shown as research context when the relevant 10 Nov
filing is included. Market tiles show figures from the latest available signal
date independently of that filing selector. Each tile links to its baseline and
original CSV. The carousel respects reduced-motion preferences and pauses while
offscreen, in an inactive tab, or while being interacted with.
