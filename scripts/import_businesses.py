from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.domains.businesses.repository import BusinessRepository
from app.settings import settings
from app.store import Store


def main() -> None:
    store = Store(settings.database_target)
    try:
        profiles = BusinessRepository(store).import_directory(settings.businesses_dir)
    finally:
        store.close()
    for profile in profiles:
        print(
            f"published {profile.slug} version {profile.version_number} "
            f"({profile.version_id})"
        )
    print(f"imported {len(profiles)} business profile(s)")


if __name__ == "__main__":
    main()
