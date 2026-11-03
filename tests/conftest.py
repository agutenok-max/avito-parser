import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Изолированное окружение для тестов (без реальных секретов)
_tmp = tempfile.mkdtemp(prefix="avito-tests-")
os.environ["DATA_DIR"] = _tmp
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_ADMIN_ID"] = "111"
os.environ["HEALTH_ENABLED"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["STOP_WORDS"] = ""
