# Web Scraper

Web Scraper collects data from **any website**, following governance rules at every step. You can
give it **keywords**: results for them are shown first, and everything else follows below.

For every page it:

1. **Runs governance checks first:** robots.txt, terms of service, rate limits, licence and personal data.
   If a site doesn't allow scraping, nothing is collected.
2. **Extracts the items the source presents** with an LLM: products, services, articles, events, listings,
   courses and so on, each with its key facts (prices, dates, sizes, requirements, ...).
3. **Searches for your keywords:** passages that mention each keyword (including word forms such as
   plurals), passages related in meaning, and the items that mention it.
4. **Removes anything the source doesn't support**, so the stored data and reports contain no invented figures.
5. **Stores the results** in a database and shows them in a web dashboard, with CSV and PDF reports.

The default LLM runs **locally with Ollama**, so no API key is needed and no data leaves your machine.
Free hosted models on [OpenRouter](https://openrouter.ai) can be used instead.

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Running the app](#running-the-app)
- [Using the app](#using-the-app)
- [Keywords](#keywords)
- [Configuration](#configuration)
- [How hallucinations are prevented](#how-hallucinations-are-prevented)
- [Governance checks](#governance-checks)
- [REST API](#rest-api)
- [Tests](#tests)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Advanced](#advanced)
- [Exercise requirements](#exercise-requirements)

---

## How it works

```
 Website URL (+ optional keywords)
     │
     ▼
 Governance checks ──FAIL──▶ job blocked
     │ pass
     ▼
 Fetch page (official API, HTTP, or browser)
     │
     ▼
      Clean content → redact personal data
      → LLM: understand the page → extract items
      → grounding: drop every fact not found in the source → validate
      → tag items that mention your keywords → store
      → search the whole page for your keywords → write the report
                            ▼
      Dashboard: keyword results first, then the report and all other items
      CSV / PDF reports
```

A page too long for one prompt is read in parts and indexed for retrieval (RAG), so the whole
page is covered. See [Long pages](#long-pages-rag).

---

## Requirements

| Tool | Version | Needed for |
| --- | --- | --- |
| Python | 3.11 or newer (tested on 3.13) | Backend and CLI |
| Node.js | 20.19 or newer (tested on 22) | Web dashboard |
| [Ollama](https://ollama.com) | Any recent version | Default local LLM. Alternatively, use an OpenRouter key |
| PostgreSQL | 14 or newer (tested on 18) | Optional. SQLite is used by default |

The default model (`qwen2.5:7b`) needs about 5 GB of GPU memory. On a machine without a GPU it runs on the CPU, more slowly.

---

## Setup

Commands are run from the project folder. Where the commands differ, **Windows (PowerShell)** is
shown first and **macOS / Linux** second.

### 1. Install the backend

```powershell
# Windows
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Choose an LLM

**Option A: local model with Ollama (the default, no key needed)**

```bash
ollama pull qwen2.5:7b     # extraction, keyword findings and the report
ollama pull bge-m3         # embeddings for keyword search and long pages (optional, TF-IDF is used without it)
```

Keep Ollama running while you use the app.

**Option B: free hosted models on OpenRouter**

1. Create a free key at https://openrouter.ai/keys.
2. In `.env`, set:
   ```
   OPENROUTER_API_KEY=sk-or-...
   LLM_PROFILE=gemma-4-31b
   ```

The available profiles are listed in [LLM profiles](#llm-profiles).

**No LLM at all:** set `LLM_PROFILE=rules-only`. Items then come only from the page's structured data
and tables, and keyword results quote the matching sentences instead of summarising them.

### 3. Choose a database

**SQLite (default).** Nothing to do. Data is stored in `scraper.db` in the project folder.

**PostgreSQL**

1. Create an empty database, for example `webscraper`.
2. In `.env`, set:
   ```
   DATABASE_URL=postgresql+psycopg://USER:PASSWORD@localhost:5432/webscraper
   ```

The tables are created automatically the first time the app starts.

> If the password contains special characters, URL-encode them. For example `!` → `%21`, `@` → `%40`, `#` → `%23`.

### 4. Optional extras

```bash
playwright install chromium                 # render JavaScript-heavy websites
python -m spacy download en_core_web_sm     # more accurate personal-data detection (Presidio)
```

Without these, pages are fetched as static HTML and personal data is detected with regular expressions.

### 5. Install the dashboard

```bash
cd frontend
npm install
```

---

## Running the app

Open two terminals, both in the project folder.

**Terminal 1: backend API**

```bash
# activate the virtual environment first (see step 1)
uvicorn app.main:app --reload --port 8000
```

**Terminal 2: dashboard**

```bash
cd frontend
npm run dev
```

Then open **http://localhost:5173**. The interactive API documentation is at http://localhost:8000/docs.

> **Single-server alternative:** run `npm run build` in `frontend/`. The backend then serves the
> dashboard itself at http://localhost:8000.

Settings are read when the backend starts. **After editing `.env`, restart the backend.** The
`--reload` option only watches `.py` files.

---

## Using the app

### Dashboard

On the **New job** page, enter the address of any web page, for example `example.com/pricing`, and
click **Start scrape**.
- **Run governance checks only** runs the checks without collecting anything.
- **Options** lets you choose what to store (items, raw tables, page text, metadata), which model to
  use, how pages are rendered, and an official API endpoint if the site has one.

The page also asks **"Do you have keywords to look for?"**. Answer **Yes** and type them, separated
by commas, or **No** to scrape the page without keywords. See [Keywords](#keywords).

While a job runs, a progress screen shows each stage as it happens, streamed live from the server, and
**Cancel job** stops it at the next stage. When it finishes, the job page shows, in this order:
1. **Your keywords:** what the source says about each one, the matching passages and items
   (only when you gave keywords)
2. **Items matching your keywords**
3. **The report:** headline, overview, detailed summary, sections and key figures
4. **Other items** (or **Items** when you gave no keywords), including the facts removed because
   the source did not support them
5. **Governance checks**, page understanding and the full event log

**Jobs** lists every job, with chips to show only one status (Active, Completed, Failed, Blocked,
Cancelled). **Retry** on a finished job queues the same request again and leaves the original in place;
**Copy link** copies a page or source address.

### Command line

Run these commands from the project folder with the virtual environment active.

```bash
python -m app.cli profiles                                              # list LLM profiles and whether they are usable
python -m app.cli run --url https://books.toscrape.com/                 # scrape one web page
python -m app.cli run --url https://books.toscrape.com/ --keywords "poetry, price"
python -m app.cli run --url https://books.toscrape.com/ --keywords "poetry" --out reports
```

| Option | Meaning |
| --- | --- |
| `--url` | Web page to scrape (required) |
| `--keywords` | Comma-separated keywords; their results are printed first |
| `--profile` | LLM profile to use for this run |
| `--data-types` | Comma-separated subset of `items,tables,text,metadata` |
| `--render` | `auto` (default), `http`, or `browser` for JavaScript pages |
| `--out` | Folder to save one JSON report per job |

The CLI prints the keyword results first, then each job's items (keyword matches first) and the
report headline. Jobs appear in the dashboard too, because both use the same database. The exit
code is non-zero if any job was blocked or failed.

### Sample data

The project ships with fictional, public-domain (CC0) sample data for testing. It describes a
made-up bank, Northwind Community Bank, but the scraper itself works with any kind of content.

- `sample_site/`: a small website. To serve it locally:
  ```bash
  python -m http.server 8080 --bind 127.0.0.1 --directory sample_site
  ```
  Then set `ALLOW_PRIVATE_HOSTS=true` in `.env` and restart the backend. Local addresses are
  refused by default. Scrape `http://127.0.0.1:8080/credit-cards.html`,
  `/personal-loans.html` or `/savings.html`.

  Alternatively, publish the `sample_site` folder on any free static host (Netlify Drop,
  GitHub Pages, Bolt.new) and scrape it there. No setting change is needed then.

For a public site built for scraping practice, try https://books.toscrape.com/.

---

## Keywords

Keywords are optional. You can give up to 10 per job, each up to 60 characters.

For each keyword the app:

1. **Finds exact matches:** passages that contain the keyword or its word forms
   ("policy" also finds "policies", "price" also finds "prices").
2. **Finds related passages:** passages close in meaning, using the `bge-m3` embedding model
   (TF-IDF word overlap if the model isn't installed).
3. **Tags the items that mention it:** those items are listed first, with the keyword shown on them.
4. **Writes a finding:** with an LLM, a short summary and key points drawn only from those
   passages. Without an LLM, the matching sentences are quoted.
5. **Grounds the finding:** any sentence or point quoting a figure that isn't in the source is removed.

A keyword counts as **found** when a passage contains it or an item mentions it. When only related
passages exist, it is shown as "only related passages". Otherwise it is shown as "not found".

**Governance rule:** keywords that look like personal identifiers (email addresses, phone numbers
or ID numbers) are rejected. The scraper must not be used to look up individuals.

---

## Configuration

Settings are split into two files so that secrets never sit next to ordinary settings.

| File | Contains | Committed to git |
| --- | --- | --- |
| [`config/llm.toml`](config/llm.toml) | LLM profiles (model, temperature, token limits, fallbacks), provider API addresses (`[providers.*]`), RAG and embedding settings | Yes |
| `.env` (copied from [`.env.example`](.env.example)) | API keys, database URL, environment switches | **No**, it is git-ignored |

API keys are protected in four ways:
- They are loaded as masked secrets, so they never appear in logs.
- The API and CLI only report *whether* a key is set, never its value.
- If a key is put in `config/llm.toml`, the backend refuses to start.
- On a server, provide the same variables as environment variables (for example from a secrets manager) instead of a `.env` file.

### LLM profiles

| Profile | Model | Size | Needs |
| --- | --- | --- | --- |
| `ollama-local` **(default)** | `qwen2.5:7b` | 7B | Ollama |
| `gemma-4-31b` | `google/gemma-4-31b-it:free` | 31B | OpenRouter key |
| `nemotron-120b` | `nvidia/nemotron-3-super-120b-a12b:free` | 120B (12B active) | OpenRouter key |
| `qwen-27b` | `qwen/qwen3.8-27b:free` | 27B | OpenRouter key |
| `gemma-4-26b-moe` | `google/gemma-4-26b-a4b-it:free` | 26B (4B active) | OpenRouter key |
| `lfm-2b` | `liquid/lfm-2.5-2.6b:free` | 2.6B | OpenRouter key |
| `rules-only` | none | — | nothing |

All profiles use temperature 0 and a fixed seed, so output stays close to the source and is repeatable.

No model names or provider addresses are built into the code. Every profile must set `model`, and each
provider's address comes from its `[providers.<name>] base_url` entry (or the profile's own `base_url`, or
`LLM_BASE_URL`). If one is missing, the profile is reported as unavailable with the reason. The embedding
model is set in `[rag]`; without it, retrieval uses TF-IDF.

**Which profile is used** (first match wins):
1. `--profile` on the CLI, or `llm_profile` in an API request
2. `LLM_PROFILE` in `.env`
3. `active_profile` in `config/llm.toml`

Free OpenRouter models change over time and are rate-limited. When a model is busy, the
profile's `fallback_models` are tried in order. To add a model, copy a profile in
`config/llm.toml` and change `model`. Free models are listed at
https://openrouter.ai/models?max_price=0; choose one that supports `response_format`.

### Main `.env` settings

Every setting is listed with its default in [`.env.example`](.env.example).

| Setting | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | Key for OpenRouter profiles. `OPENAI_API_KEY`, `GEMINI_API_KEY` and `DASHSCOPE_API_KEY` are also supported |
| `LLM_PROFILE` | Default LLM profile |
| `DATABASE_URL` | SQLite (default) or PostgreSQL connection string |
| `USER_AGENT` | How the scraper identifies itself to websites. Include a contact address |
| `MIN_REQUEST_INTERVAL` | Minimum seconds between requests to the same site |
| `ALLOW_PRIVATE_HOSTS` | Allow `localhost` / private IPs, e.g. the local sample site. **Keep `false` when deployed** |
| `PII_BLOCK_THRESHOLD` | Number of personal-data findings on one page that blocks the job |
| `MIN_ITEM_CONFIDENCE` | Items below this confidence (0–1) are rejected |

---

## How hallucinations are prevented

The LLM is constrained both before and after it answers.

**Before: the prompt**
- Temperature 0 and a fixed seed.
- The prompt says the supplied page is the only source, and that anything not stated must be left out, not guessed.
- The source is marked as untrusted data, so instructions hidden in it are ignored.

**After: grounding** ([`app/validation/grounding.py`](app/validation/grounding.py))
- Every figure in an item's facts must appear in the part of the source about **that** item: its section, table row or structured data.
- Text facts must share most of their words with that item's part of the source.
- A description that quotes a figure the source doesn't contain is removed.
- Anything that fails is removed. It is listed under the item's *unverified* facts, and the item's confidence is lowered.
- Report sentences, key figures and keyword findings that quote numbers not in the source are dropped.
- Items the page's own structure shows (structured data, table rows) but the LLM missed are added back.

### Long pages (RAG)

A page longer than one prompt (15,000 characters by default) is never cut off:

1. It is split into parts that each fit one prompt. Items are extracted from every part.
2. It is also split into section-aware chunks. The chunks are embedded with `bge-m3`, or ranked with TF-IDF if that model isn't installed.
3. The model writes notes on every part, and the chunks most relevant to six report questions are retrieved.
4. The report is written from the notes and the retrieved chunks, then grounded against the full text.

Keyword search always covers the whole page, whatever its length.

---

## Governance checks

Websites are checked **before** any content is collected. A **fail** blocks the job. A **warn**
is recorded and shown in the report.

| Check | Blocks the job when |
| --- | --- |
| robots.txt | The URL is disallowed for our user agent, or robots.txt can't be read (401/403/5xx or a network error) |
| Page access | The site returns an error status or a bot challenge / CAPTCHA page. Access controls are never bypassed |
| Rate limits | The site answers HTTP 429. Crawl-delay and rate-limit headers also slow the scraper down |
| Terms of service | The terms prohibit scraping, crawling or automated access |
| Official API | Never blocks. Warns when an API exists so you can use it instead |
| Content licence | Never blocks. Warns on restrictive licences. Only facts with short source quotes are stored |
| Personal data | The page contains personal data at scale. Otherwise personal data is redacted before AI processing and storage |

Further rules:
- Only public addresses can be scraped; `localhost` and private networks are refused (unless `ALLOW_PRIVATE_HOSTS=true`).
- Keywords can't be personal identifiers, and the extraction prompt forbids collecting private individuals' contact details.
- The scraper identifies itself with `USER_AGENT` and spaces out its requests to each site.

Every step is logged to the job's event list and to `logs/audit.log`. The checks support human
judgement; they don't replace it. Review terms and licence warnings before relying on data from a source.

---

## REST API

Full, interactive documentation: http://localhost:8000/docs.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/compliance/check` | Run the governance checks for a URL without collecting anything |
| `POST` | `/api/jobs` | Scrape a website: `{"url": "...", "keywords": ["..."], "data_types": [...], "llm_profile": "..."}` |
| `GET` | `/api/jobs` · `/api/jobs/{id}` | List jobs · job details (keyword findings are in `page_report.keyword_findings`) |
| `GET` | `/api/jobs?status=failed&status=blocked` | List only these statuses; repeat `status` for several |
| `DELETE` | `/api/jobs/{id}` | Cancel a queued or running job; a running pipeline stops at its next stage |
| `POST` | `/api/jobs/{id}/retry` | Queue a new job with the same request; the original is left as it is |
| `GET` | `/api/jobs/{id}/events?after=N` | Server-sent events: one `stage` event per step as it happens, then `end` |
| `GET` | `/api/jobs/{id}/report/{csv\|pdf}` | Download a job report (the same data as JSON: `/api/jobs/{id}`) |
| `GET` | `/api/items?type=&q=` · `/api/items/export/csv` | Search stored items · export them as CSV |
| `GET` | `/api/llm/profiles` | LLM profiles and whether each is usable |
| `GET` | `/api/stats` · `/api/health` | Totals · health check |

---

## Tests

```bash
pytest
```

The 65 tests need no network and no LLM. They cover governance checks, parsing, keyword search,
RAG, grounding, validation, LLM configuration and job control (cancel, retry, status filters, the event
stream), plus full pipeline runs against a mocked website. Tests use a temporary SQLite database and
TF-IDF retrieval, so your data and your Ollama models are not touched.

---

## Project structure

```
webscraper/
├── app/
│   ├── main.py            REST API (FastAPI)
│   ├── cli.py             Command-line interface
│   ├── pipeline.py        Runs every stage of a job
│   ├── config.py          Settings loaded from .env
│   ├── compliance/        Governance checks: robots.txt, terms, rate limits, API discovery, licence, personal data
│   ├── scraper/           HTTP and browser fetching, HTML parsing and cleaning
│   ├── ai/                LLM client, page understanding, item extraction, keyword search, report, RAG
│   ├── validation/        Schemas, grounding, merging of repeat scrapes
│   ├── database/          Database connection and tables
│   └── reports/           CSV and PDF reports
├── frontend/              React dashboard (Vite)
├── config/llm.toml        LLM profiles (no secrets)
├── sample_site/           Fictional sample website
├── tests/                 Test suite
├── .env.example           Template for .env
└── requirements.txt       Python dependencies
```

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Report says *"LLM not configured"* or jobs use rules only | Ollama isn't running or the model isn't pulled: run `ollama pull qwen2.5:7b`. For OpenRouter, check `OPENROUTER_API_KEY` and restart the backend. `python -m app.cli profiles` shows why a profile is unusable |
| Dashboard shows *"Cannot reach the API"* | Start the backend on port 8000 (see [Running the app](#running-the-app)) |
| Changes to `.env` have no effect | Restart the backend. `--reload` doesn't watch `.env` |
| *"local addresses are not allowed"* | Set `ALLOW_PRIVATE_HOSTS=true` to scrape `localhost` / `127.0.0.1` (development only) |
| A keyword is rejected as a *"personal identifier"* | Email addresses and phone or ID numbers can't be used as keywords (governance rule) |
| A keyword shows *"not found"* although you can see it on the page | Navigation menus, sidebars, banners and ads are removed before analysis; the keyword may only appear there |
| PostgreSQL connection fails | Check that the database exists and URL-encode special characters in the password |
| A job is **blocked** | Open the job: the governance table shows which check failed and why |
| OpenRouter model returns errors or is slow | Free models are rate-limited. Try again, or switch to another profile |

---

## Advanced

**Background workers (Celery).** For long-running jobs, set `USE_CELERY=true` and
`REDIS_URL=redis://localhost:6379/0` in `.env`, then start a worker:

```bash
celery -A app.worker worker --loglevel=info      # on Windows add --pool=solo
```

**Deployment checklist.**
- Use PostgreSQL.
- Supply secrets as environment variables.
- Keep `ALLOW_PRIVATE_HOSTS=false`.
- Serve the built dashboard (`npm run build`) from the API or a static host.
- Use migrations (e.g. Alembic) for schema changes instead of automatic table creation.

---

## Exercise requirements

| Requirement | How it is met |
| --- | --- |
| Minimise hallucinations; ground responses in the source | Temperature 0, source-only prompts, and [grounding](#how-hallucinations-are-prevented) that removes every unsupported fact |
| Use only a sample website | [`sample_site/`](sample_site/): a fictional website, served locally or on a free static host |
| Use free LLMs with different parameters | Local Ollama model plus free OpenRouter models from 2.6B to 120B parameters ([profiles](#llm-profiles)) |
| Externalise LLM configuration and API keys in a secure file | Parameters in `config/llm.toml`; keys only in the git-ignored `.env`, masked in logs, and rejected if placed in the config file |
| CLI or UI for a website URL | Web dashboard (with an optional keyword question) and `python -m app.cli run --url … --keywords …` |
