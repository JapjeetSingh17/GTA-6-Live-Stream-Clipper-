import sys
from pathlib import Path

# Add project root to Python module search path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gta6_clipper.app import app

# Vercel ASGI handler
handler = app
app = app
