"""Operator commands: ``python -m app.cli <command>`` (M3a §2.4).

``create-admin --phone +234XXXXXXXXXX --name "Full Name"``
    Creates a user with role ``admin``. The phone is normalised as the OTP flow
    does (:func:`app.services.auth.normalize_phone`). The admin then logs in with
    the normal phone OTP flow.

    * New phone: the admin is created, an audit event (``actor_role='system'``,
      action ``user.admin_created``) is written in the same transaction, and the
      user id is printed. Exit 0.
    * The phone already belongs to an active admin: nothing changes, the existing
      id is printed. Exit 0 (the command is safe to re-run).
    * The phone belongs to a user with another role, or to an inactive or deleted
      admin: nothing changes (a role is never changed silently, and an admin is
      never reactivated; amendment A21). Exit 1.
    * A bad phone, or a name that is blank or longer than 200 characters (the
      API's ``FullName`` limit): nothing is read or written. Exit 2.

Only the user id reaches stdout; errors go to stderr and never echo the phone.

``seed-catalogue --file path.csv`` (M3a §4.2)
    Idempotent upsert of catalogue items on ``(make, model)`` from a CSV with the
    columns ``category,make,model,mount,band,aliases`` (see
    :mod:`app.services.catalogue_seed`). The whole file is validated and written in
    one transaction.

    * Success: prints ``inserted=N updated=N unchanged=N``. Exit 0.
    * A bad row or header (unknown category, bad band, missing make or model,
      duplicate ``(make, model)``): nothing is written, the first bad line is named
      on stderr. Exit 1.
    * The file cannot be read: Exit 2.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import User
from app.db.models.user import RenterRole
from app.db.session import create_database_engine, create_database_sessionmaker
from app.services.audit import Actor, record_audit
from app.services.auth import normalize_phone
from app.services.catalogue_seed import SeedError, apply_seed, category_ids, parse_seed

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
# The same limit as the API's ``FullName`` (app.schemas.auth).
MAX_NAME_LENGTH = 200


class CommandRefused(Exception):
    """The command cannot proceed; the message is safe to print."""


async def create_admin(session: AsyncSession, *, phone: str, full_name: str) -> uuid.UUID:
    """The id of the admin for the normalised ``phone``, created if it does not exist.

    Raises :class:`CommandRefused` if the phone belongs to a non-admin or to an
    admin who is inactive or deleted. Flushes; the caller commits.
    """
    existing = (
        await session.scalars(select(User).where(User.phone == phone).with_for_update())
    ).first()
    if existing is not None:
        if existing.role != RenterRole.admin:
            raise CommandRefused(
                f"this phone already belongs to a {existing.role.value}; "
                "roles are never changed by this command"
            )
        if not existing.is_active or existing.deleted_at is not None:
            raise CommandRefused("this phone belongs to an inactive or deleted admin")
        return existing.id

    user = User(phone=phone, full_name=full_name, role=RenterRole.admin, is_active=True)
    session.add(user)
    await session.flush()
    await record_audit(
        session,
        actor=Actor.system(),
        entity="user",
        entity_id=user.id,
        action="user.admin_created",
        before=None,
        after={"role": RenterRole.admin, "is_active": True},
    )
    return user.id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="create the admin for a phone number")
    admin.add_argument("--phone", required=True, help="+234XXXXXXXXXX or 0XXXXXXXXXX")
    admin.add_argument("--name", required=True, help="the admin's full name")
    seed = commands.add_parser("seed-catalogue", help="upsert catalogue items from a CSV file")
    seed.add_argument("--file", required=True, help="CSV: category,make,model,mount,band,aliases")
    return parser


async def _seed_catalogue_command(file_raw: str) -> int:
    try:
        text = await asyncio.to_thread(Path(file_raw).read_text, encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: cannot read --file ({exc.__class__.__name__})", file=sys.stderr)
        return EXIT_USAGE

    engine = create_database_engine(get_settings())
    try:
        async with create_database_sessionmaker(engine)() as session:
            try:
                known = frozenset(await category_ids(session))
                rows = parse_seed(text, known)
                counts = await apply_seed(session, rows)
                await session.commit()
            except SeedError as exc:
                await session.rollback()
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_REFUSED
    finally:
        await engine.dispose()
    print(f"inserted={counts.inserted} updated={counts.updated} unchanged={counts.unchanged}")
    return EXIT_OK


async def _create_admin_command(phone_raw: str, name_raw: str) -> int:
    try:
        phone = normalize_phone(phone_raw)
    except ValueError as exc:
        print(f"error: --phone {exc}", file=sys.stderr)
        return EXIT_USAGE
    full_name = name_raw.strip()
    if not full_name:
        print("error: --name must not be blank", file=sys.stderr)
        return EXIT_USAGE
    if len(full_name) > MAX_NAME_LENGTH:
        print(f"error: --name must be at most {MAX_NAME_LENGTH} characters", file=sys.stderr)
        return EXIT_USAGE

    engine = create_database_engine(get_settings())
    try:
        async with create_database_sessionmaker(engine)() as session:
            try:
                user_id = await create_admin(session, phone=phone, full_name=full_name)
                await session.commit()
            except CommandRefused as exc:
                await session.rollback()
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_REFUSED
            except IntegrityError:
                # Another process created a user with this phone at the same time.
                await session.rollback()
                print("error: this phone was registered concurrently; run again", file=sys.stderr)
                return EXIT_REFUSED
    finally:
        await engine.dispose()
    print(user_id)
    return EXIT_OK


async def run(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and run the command; returns the exit status."""
    args = _parser().parse_args(argv)
    if args.command == "seed-catalogue":
        return await _seed_catalogue_command(args.file)
    return await _create_admin_command(args.phone, args.name)


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
