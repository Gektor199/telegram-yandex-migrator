from __future__ import annotations

import sys

from app.config import get_settings
from app.yandex_disk import YandexDiskClient, YandexDiskError


def main() -> int:
    settings = get_settings()
    if not settings.yandex_disk_oauth_token:
        print("YANDEX_DISK_OAUTH_TOKEN is empty", file=sys.stderr)
        return 1

    client = YandexDiskClient(
        api_base=settings.yandex_disk_api_base,
        root_reference=settings.yandex_disk_root_reference,
    )

    try:
        result = client.probe(token=settings.yandex_disk_oauth_token, org_id=settings.yandex_disk_org_id)
    except YandexDiskError as exc:
        print(f"Disk probe failed: {exc}", file=sys.stderr)
        return 2

    print("Disk probe ok")
    print(f"path={result.path}")
    print(f"public_url={result.public_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
