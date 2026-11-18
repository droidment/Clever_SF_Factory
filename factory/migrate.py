"""Initial schema bootstrap. Future revisions must use reviewed forward migrations."""

from factory.config import settings
from factory.store import Store

if __name__ == "__main__":
    Store(settings()).create_schema()
    print("Schema v1 initialized")
