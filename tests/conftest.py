import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Configure the app for tests before anything imports app.config
_tmp = Path(tempfile.mkdtemp(prefix="fintech-scraper-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'test.db').as_posix()}"
os.environ["MIN_REQUEST_INTERVAL"] = "0"
os.environ["MAX_RETRIES"] = "1"
os.environ["LLM_PROVIDER"] = "none"
os.environ["ALLOW_PRIVATE_HOSTS"] = "false"
os.environ["DOCUMENT_ROOTS"] = "[]"
os.environ["UPLOAD_DIR"] = str(_tmp / "uploads")
os.environ["PRESIDIO_ENABLED"] = "false"
os.environ["PLAYWRIGHT_ENABLED"] = "false"
os.environ["AUDIT_LOG_FILE"] = ""
