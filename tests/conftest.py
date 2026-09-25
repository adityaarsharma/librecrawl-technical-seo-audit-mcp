import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="lc-tests-"))
os.environ["LIBRECRAWL_STATE_DB"] = str(_TMP / "state.db")
os.environ["REPORTS_DIR"] = str(_TMP / "reports")
os.environ["LIBRECRAWL_URL"] = "http://127.0.0.1:9"  # nothing listens; tests mock upstream calls
os.environ.pop("LIBRECRAWL_ALLOW_PRIVATE_TARGETS", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
