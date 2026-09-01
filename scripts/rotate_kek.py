"""Rotate the key-encryption key.

Envelope encryption exists so this is cheap: each blob has its own data key, and
only those wrapped keys are re-encrypted. Ciphertext is never touched, so
rotating a hundred gigabytes of resumes costs the same as rotating one.

    APP_KEK_OLD=<current> APP_KEK=<new> python scripts/rotate_kek.py --dry-run
    APP_KEK_OLD=<current> APP_KEK=<new> python scripts/rotate_kek.py --apply

Both keys must be present: the old to unwrap, the new to re-wrap. Losing the old
one before rotation completes loses the data, which is why --dry-run is the
default and reports what it would do without writing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api" / "src"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from screener_api.models import PiiMap  # noqa: E402
from screener_api.security.crypto import (  # noqa: E402
    DecryptionError,
    Envelope,
    decrypt,
    derive_kek,
    encrypt,
)
from screener_api.settings import get_settings  # noqa: E402


async def rotate(*, apply: bool) -> int:
    settings = get_settings()
    old_secret = os.environ.get("APP_KEK_OLD")
    if not old_secret:
        print("APP_KEK_OLD is required (the key currently protecting the data)")
        return 2

    old_version = int(os.environ.get("APP_KEK_OLD_VERSION", settings.app_kek_version))
    new_version = old_version + 1
    old_kek = derive_kek(old_secret, old_version)
    new_kek = derive_kek(settings.app_kek.get_secret_value(), new_version)

    if old_kek == new_kek:
        print("APP_KEK and APP_KEK_OLD derive the same key; nothing to rotate")
        return 2

    engine = create_async_engine(settings.dsn)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    rewrapped = blobs = failures = 0
    async with maker() as session:
        maps = (await session.execute(select(PiiMap))).scalars().all()
        for row in maps:
            try:
                plaintext = decrypt(
                    Envelope.from_bytes(row.ciphertext),
                    kek=old_kek,
                    aad=str(row.org_id).encode(),
                )
            except DecryptionError:
                failures += 1
                print(f"  FAILED to unwrap pii_map {row.id} — wrong APP_KEK_OLD?")
                continue
            if apply:
                row.ciphertext = encrypt(
                    plaintext, kek=new_kek, kek_version=new_version,
                    aad=str(row.org_id).encode(),
                ).to_bytes()
            rewrapped += 1

        # Blobs carry their own envelopes and must be re-wrapped too. They live
        # in a Docker volume, so this pass only does anything when the script
        # runs where that volume is mounted — inside a worker container. On the
        # host the directory is absent and the pass is skipped loudly rather
        # than silently reporting success over data it never touched.
        clean = pathlib.Path(settings.storage_local_path) / "clean"
        if not clean.is_dir():
            print(f"  NOTE: {clean} not present — blob re-wrap SKIPPED.")
            print("        Run inside the worker container to rotate blobs:")
            print("        docker compose exec worker-ai python /app/scripts/rotate_kek.py --apply")
        else:
            for path in clean.rglob("*"):
                if not path.is_file():
                    continue
                blobs += 1
                if not apply:
                    continue
                envelope = Envelope.from_bytes(path.read_bytes())
                for org_hint in {str(m.org_id) for m in maps}:
                    try:
                        data = decrypt(envelope, kek=old_kek, aad=org_hint.encode())
                    except DecryptionError:
                        continue
                    path.write_bytes(
                        encrypt(data, kek=new_kek, kek_version=new_version,
                                aad=org_hint.encode()).to_bytes()
                    )
                    break

        if apply:
            await session.commit()
    await engine.dispose()

    verb = "re-wrapped" if apply else "would re-wrap"
    print(f"\n  {verb} {rewrapped} pii_map row(s) and inspected {blobs} blob(s)")
    print(f"  kek version {old_version} -> {new_version}")
    if failures:
        print(f"  {failures} failure(s) — resolve before setting APP_KEK_VERSION={new_version}")
        return 1
    if apply:
        print(f"  now set APP_KEK_VERSION={new_version} and restart, then retire APP_KEK_OLD")
    else:
        print("  dry run: nothing written. Re-run with --apply to rotate.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually re-wrap (default is a dry run)")
    args = parser.parse_args()
    return asyncio.run(rotate(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
