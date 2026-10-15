"""Settings read from the environment (.env is loaded if present)."""
import os

from dotenv import load_dotenv

load_dotenv()

FALKORDB_HOST = os.getenv("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_PASSWORD = os.getenv("FALKORDB_PASSWORD") or None
GRAPH_NAME = os.getenv("GRAPH_NAME", "brain_eval")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or None
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or None
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-5-5")
DEMO_REPLAY = os.getenv("DEMO_REPLAY", "0") == "1" or not ANTHROPIC_API_KEY

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
