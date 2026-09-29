# Fintech Scraper

Fintech Scraper collects financial product data (loans, credit cards, insurance, investments,
bank accounts and payment services) from **websites** and **documents** (PDF, Word, text, Markdown, HTML).

For every source it:

1. **Checks compliance first:** robots.txt, terms of service, rate limits, licence and personal data.
2. **Extracts products with an LLM:** rates, fees, amounts, eligibility, features and conditions.
3. **Removes anything the source doesn't support**, so the stored data and reports contain no invented figures.
4. **Stores the results** in a database and shows them in a web dashboard, with JSON, CSV and PDF reports.


The default LLM runs **locally with Ollama**, so no API key is needed and no data leaves your machine.
Free hosted models on [OpenRouter](https://openrouter.ai) can be used instead.

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Running the app](#running-the-app)
- [Using the app](#using-the-app)
- [Configuration](#configuration)
- [How hallucinations are prevented](#how-hallucinations-are-prevented)
- [Compliance checks](#compliance-checks)
- [REST API](#rest-api)
- [Tests](#tests)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Advanced](#advanced)
- [Exercise requirements](#exercise-requirements)

---

## How it works

```
 Website URL                                    Documents (PDF, DOCX, TXT, MD, HTML)
     │                                                   │
     ▼                                                   ▼
 Compliance checks ──FAIL──▶ job blocked           Load document, detect headings and tables
     │ pass                                              │
     ▼                                                   │
 Fetch page (official API, HTTP, or browser)             │
     │                                                   │
     └──────────────────────┬────────────────────────────┘
                            ▼
      Clean content → redact personal data
      → LLM: understand page → extract products → classify
      → grounding: drop every fact not found in the source
      → validate → store → write summaries and report
                            ▼
              Dashboard · JSON / CSV / PDF reports
```

A source too long for one prompt is read in parts and indexed for retrieval (RAG), so the whole
document is covered. See [Long documents](#long-documents-rag).

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
ollama pull qwen2.5:7b     # the extraction and summary model
ollama pull bge-m3         # embeddings for long documents (optional, TF-IDF is used without it)
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

**No LLM at all:** set `LLM_PROFILE=rules-only`. Extraction then uses rules only, which is less complete but costs nothing.

### 3. Choose a database

**SQLite (default).** Nothing to do. Data is stored in `fintech_scraper.db` in the project folder.

**PostgreSQL**

1. Create an empty database, for example `fintech_scraper`.
2. In `.env`, set:
   ```
   DATABASE_URL=postgresql+psycopg://USER:PASSWORD@localhost:5432/fintech_scraper
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

The **New job** page has two sources.

**Website:** enter a page URL, for example `bank.com/credit-cards`, and click **Start scrape**.
- Optionally choose which data types to collect.
- **Check compliance only** runs the checks without collecting anything.

**Document folder:** click **Choose folder** or **Choose files**, or drop files onto the page.
1. The files are uploaded.
2. You review what will be processed and what was skipped (unsupported, empty or duplicate files).
3. Click **Process documents**. Each document becomes its own job; **Cancel upload** deletes the upload instead.

While a job runs, a progress screen shows each stage. When it finishes, the job page shows:
- the report: headline, overview, detailed summary, sections and key figures
- the extracted products
- the facts that were removed because the source did not support them
- the compliance results
- the full event log

**Jobs** lists every job.

**Re-uploading a document** does not create a duplicate job. A document counts as a re-upload
if it has the same content (checked with a SHA-256 hash) or the same file name as one processed
before. The review panel marks such files and asks before updating. When you confirm with
**Update existing results** (or **Process and update** when new and old documents are mixed):
- the existing job is updated
- new products and facts are added
- a rate or fee with the same label takes the new value
- nothing found earlier is removed

### Command line

Run these commands from the project folder with the virtual environment active.

```bash
python -m app.cli profiles                                  # list LLM profiles and whether they are usable
python -m app.cli run --docs sample_docs                    # process every document in a folder
python -m app.cli run --url https://bank.example/cards      # scrape one web page
python -m app.cli run --docs sample_docs --profile qwen-27b --out reports
```

| Option | Meaning |
| --- | --- |
| `--url` | Web page to scrape |
| `--docs` | Folder of documents (`--recursive` includes sub-folders) |
| `--profile` | LLM profile to use for this run |
| `--data-types` | Comma-separated subset of `rates,fees,amounts,eligibility,features,conditions,tables,text,metadata` |
| `--render` | `auto` (default), `http`, or `browser` for JavaScript pages |
| `--out` | Folder to save one JSON report per job |

The CLI prints each job's products and report headline. Jobs appear in the dashboard too,
because both use the same database. The exit code is non-zero if any job was blocked or failed.

### Sample data

The project ships with sample data about **Northwind Community Bank**, a fictional bank. All
figures are invented and the content is public domain (CC0).

- `sample_docs/`: a PDF (home loans), a Word document (insurance), a Markdown file (CD rates)
  and a text file (payment wallet fees). Upload this folder in the dashboard, or run
  `python -m app.cli run --docs sample_docs`.
- `sample_site/`: a small website. To serve it locally:
  ```bash
  python -m http.server 8080 --bind 127.0.0.1 --directory sample_site
  ```
  Then set `ALLOW_PRIVATE_HOSTS=true` in `.env` and restart the backend. Local addresses are
  refused by default. Scrape `http://127.0.0.1:8080/credit-cards.html`,
  `/personal-loans.html` or `/savings.html`.

  Alternatively, publish the `sample_site` folder on any free static host (Netlify Drop,
  GitHub Pages, Bolt.new) and scrape it there. No setting change is needed then.

---

## Configuration

Settings are split into two files so that secrets never sit next to ordinary settings.

| File | Contains | Committed to git |
| --- | --- | --- |
| [`config/llm.toml`](config/llm.toml) | LLM profiles: model, temperature, token limits, fallbacks, RAG settings | Yes |
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
| `DOCUMENT_ROOTS` | Folders that server-side document paths must be inside. Set this when others can reach the API |
| `MAX_DOCUMENT_BYTES`, `MAX_DOCUMENTS_PER_FOLDER` | Upload limits (20 MB and 100 files by default) |
| `PII_BLOCK_THRESHOLD` | Number of personal-data findings on one page that blocks the job |
| `MIN_PRODUCT_CONFIDENCE` | Products below this confidence (0–1) are rejected |

---

## How hallucinations are prevented

The LLM is constrained both before and after it answers.

**Before: the prompt**
- Temperature 0 and a fixed seed.
- The prompt says the supplied page or document is the only source, and that anything not stated must be left empty, not guessed.
- The source is marked as untrusted data, so instructions hidden in it are ignored.

**After: grounding** ([`app/validation/grounding.py`](app/validation/grounding.py))
- Every rate, fee and amount must appear in the part of the source about **that** product: its section, table row or structured data.
- Features and conditions must match that product's part of the source.
- Anything that fails is removed. It is listed under the product's *unverified* facts, and the product's confidence is lowered.
- Report sentences and key figures that quote numbers not in the source are dropped.
- Products the parser found reliably but the LLM missed are added back.
- Product summaries are written from the validated data only, never from the raw page.

### Long documents (RAG)

A source longer than one prompt (15,000 characters by default) is never cut off:

1. It is split into parts that each fit one prompt. Products are extracted from every part.
2. It is also split into section-aware chunks. The chunks are embedded with `bge-m3`, or ranked with TF-IDF if that model isn't installed.
3. The model writes notes on every part, and the chunks most relevant to six report questions are retrieved.
4. The report is written from the notes and the retrieved chunks, then grounded against the full text.

The chunks are stored in the `document_chunks` table. The report panel shows how the source was covered.

---

## Compliance checks

Websites are checked **before** any content is collected. A **fail** blocks the job. A **warn**
is recorded and shown in the report. Uploaded documents skip the website checks but still get
the personal-data check.

| Check | Blocks the job when |
| --- | --- |
| robots.txt | The URL is disallowed for our user agent, or robots.txt can't be read (401/403/5xx or a network error) |
| Page access | The site returns an error status or a bot challenge / CAPTCHA page. Access controls are never bypassed |
| Rate limits | The site answers HTTP 429. Crawl-delay and rate-limit headers also slow the scraper down |
| Terms of service | The terms prohibit scraping, crawling or automated access |
| Official API | Never blocks. Warns when an API exists so you can use it instead |
| Content licence | Never blocks. Warns on restrictive licences. Only facts with short source quotes are stored |
| Personal data | The page contains personal data at scale. Otherwise personal data is redacted before AI processing and storage |

Every step is logged to the job's event list and to `logs/audit.log`. The checks support human
judgement; they don't replace it. Review terms and licence warnings before relying on data from a source.

---

## REST API

Full, interactive documentation: http://localhost:8000/docs.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/compliance/check` | Run compliance checks for a URL without collecting anything |
| `POST` | `/api/jobs` | Scrape a website: `{"url": "...", "data_types": [...], "llm_profile": "..."}` |
| `POST` | `/api/uploads` | Upload documents (multipart field `files`). Returns a batch to review |
| `POST` | `/api/uploads/{batch_id}/process` | Process a reviewed upload |
| `DELETE` | `/api/uploads/{batch_id}` | Cancel an upload and delete its files |
| `POST` | `/api/documents` | Process a folder on the server: `{"folder": "...", "recursive": false}` |
| `GET` | `/api/jobs` · `/api/jobs/{id}` | List jobs · job details |
| `GET` | `/api/jobs/{id}/report/{json\|csv\|pdf}` | Download a job report |
| `GET` | `/api/products` · `/api/products/export/csv` | Search stored products · export them as CSV |
| `GET` | `/api/llm/profiles` | LLM profiles and whether each is usable |
| `GET` | `/api/stats` · `/api/health` | Totals · health check |

---

## Tests

```bash
pytest
```

The 72 tests need no network and no LLM. They cover compliance, parsing, document loading, RAG,
grounding, LLM configuration and re-uploads, plus full pipeline runs against a mocked website
and the sample documents. Tests use a temporary SQLite database, so your data is not touched.

---

## Project structure

```
fintech_scraper/
├── app/
│   ├── main.py            REST API (FastAPI)
│   ├── cli.py             Command-line interface
│   ├── pipeline.py        Runs every stage of a job
│   ├── config.py          Settings loaded from .env
│   ├── compliance/        robots.txt, terms, rate limits, API discovery, licence, personal data
│   ├── scraper/           HTTP and browser fetching, HTML parsing and cleaning
│   ├── documents/         Document loaders and upload handling
│   ├── ai/                LLM client, extraction, classification, summaries, RAG
│   ├── validation/        Schemas, grounding, merging of re-uploads
│   ├── database/          Database connection and tables
│   └── reports/           CSV and PDF reports
├── frontend/              React dashboard (Vite)
├── config/llm.toml        LLM profiles (no secrets)
├── sample_docs/           Fictional sample documents
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
| PostgreSQL connection fails | Check that the database exists and URL-encode special characters in the password |
| A job is **blocked** | Open the job: the compliance table shows which check failed and why |
| *"has no extractable text"* for a PDF | The PDF is a scanned image. Run OCR on it first |
| OpenRouter model returns errors or is slow | Free models are rate-limited. Try again, or switch to another profile |

---

## Advanced

**Background workers (Celery).** For long-running jobs, set `USE_CELERY=true` and
`REDIS_URL=redis://localhost:6379/0` in `.env`, then start a worker:

```bash
celery -A app.worker worker --loglevel=info      # on Windows add --pool=solo
```

**Custom classifier.** Train a scikit-learn model from a CSV with `text,category` columns:

```bash
python -m app.ai.classification train labelled.csv models/classifier.joblib
```

Then set `CLASSIFIER_MODEL_PATH=models/classifier.joblib`.

**Deployment checklist.**
- Use PostgreSQL.
- Supply secrets as environment variables.
- Keep `ALLOW_PRIVATE_HOSTS=false`.
- Set `DOCUMENT_ROOTS`.
- Serve the built dashboard (`npm run build`) from the API or a static host.
- Use migrations (e.g. Alembic) for schema changes instead of automatic table creation.

---

## Exercise requirements

| Requirement | How it is met |
| --- | --- |
| Minimise hallucinations; ground responses in the source | Temperature 0, source-only prompts, and [grounding](#how-hallucinations-are-prevented) that removes every unsupported fact |
| Use only a sample website | [`sample_site/`](sample_site/): a fictional bank, served locally or on a free static host |
| Use only sample documents with public information | [`sample_docs/`](sample_docs/): fictional, public-domain product documents |
| Use free LLMs with different parameters | Local Ollama model plus free OpenRouter models from 2.6B to 120B parameters ([profiles](#llm-profiles)) |
| Externalise LLM configuration and API keys in a secure file | Parameters in `config/llm.toml`; keys only in the git-ignored `.env`, masked in logs, and rejected if placed in the config file |
| CLI or UI for a website URL and a document folder | Web dashboard (Website / Document folder) and `python -m app.cli run --url … --docs …` |
