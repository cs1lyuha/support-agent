import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = Path(os.environ.get("SUPPORT_KB_DIR", ROOT / "kb"))
DB_PATH = Path(os.environ.get("SUPPORT_DB", ROOT / "data" / "support.db"))

# "auto" = Claude dacă există credențiale Anthropic, altfel creierul offline determinist.
BRAIN = os.environ.get("SUPPORT_BRAIN", "auto")
MODEL = os.environ.get("SUPPORT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("SUPPORT_EFFORT", "medium")
MAX_AGENT_STEPS = 6

# Reguli de business (vezi kb/politica-retur.md)
REFUND_WINDOW_DAYS = 30
MAX_REFUND_REQUESTS_PER_SESSION = 1
# Câte întrebări consecutive fără răspuns în documentație până la fallback la om.
MAX_KB_MISSES = 2
