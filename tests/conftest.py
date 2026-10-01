import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Configure the app for tests before anything imports app.config
_tmp = Path(tempfile.mkdtemp(prefix="web-scraper-tests-"))
# Same LLM profiles, but retrieval uses TF-IDF, so tests never call a model.
_config = (ROOT / "config" / "llm.toml").read_text(encoding="utf-8").replace(
    'embedding_provider = "ollama"', 'embedding_provider = "tfidf"')
(_tmp / "llm.toml").write_text(_config, encoding="utf-8")

os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'test.db').as_posix()}"
os.environ["LLM_CONFIG_FILE"] = str(_tmp / "llm.toml")
os.environ["USER_AGENT"] = "WebScraperBot/1.0 (+https://example.com/bot-info)"
os.environ["MIN_REQUEST_INTERVAL"] = "0"
os.environ["MAX_RETRIES"] = "1"
os.environ["LLM_PROVIDER"] = "none"
os.environ["OPENROUTER_API_KEY"] = ""  # never use the developer's real key from .env
os.environ["ALLOW_PRIVATE_HOSTS"] = "false"
os.environ["PRESIDIO_ENABLED"] = "false"
os.environ["PLAYWRIGHT_ENABLED"] = "false"
os.environ["AUDIT_LOG_FILE"] = ""
