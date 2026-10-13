"""Operator CLI: ``python -m app.cli <command>``.

Commands: create-admin, migrate, seed, export-categories, apply-retention, bench.
Passwords are never taken from argv; they are prompted with getpass (or read from stdin with --password-stdin).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy import select

from .bootstrap import create_user, ensure_initial_admin
from .config import get_settings
from .db import dispose_engine, sessionmaker
from .models import User

BENCH_HOOKS: dict[str, Callable[[], Awaitable[dict]]] = {}


def bench_hook(name: str):
    """Register a benchmark: ``@bench_hook('name') async def run() -> dict``."""

    def deco(fn: Callable[[], Awaitable[dict]]):
        BENCH_HOOKS[name] = fn
        return fn

    return deco


@bench_hook("db-ping")
async def _bench_db_ping() -> dict:
    from sqlalchemy import text

    async with sessionmaker()() as s:
        t0 = time.perf_counter()
        for _ in range(100):
            await s.execute(text("SELECT 1"))
        return {"queries": 100, "avg_ms": round((time.perf_counter() - t0) * 10, 3)}


async def _create_admin(args: argparse.Namespace) -> int:
    settings = get_settings()
    async with sessionmaker()() as s:
        if args.from_env:
            ok = await ensure_initial_admin(s, settings)
            print("Initial administrator created." if ok else "Nothing done: users already exist, or LEAD_INITIAL_ADMIN_EMAIL/PASSWORD are unset or the password is too weak.")
            return 0 if ok else 1
        email = (args.email or input("Administrator email: ")).strip().lower()
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
        else:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Repeat password: "):
                print("Passwords do not match.", file=sys.stderr)
                return 2
        if (await s.execute(select(User.id).where(User.email == email))).first():
            print(f"A user with email {email} already exists.", file=sys.stderr)
            return 1
        try:
            u = await create_user(s, email, password, args.role)
        except ValueError as e:
            print(f"Rejected: {e}", file=sys.stderr)
            return 2
        await s.commit()
        print(f"Created {u.role} {u.email} (id {u.id}).")
        return 0


def _migrate(_: argparse.Namespace) -> int:
    from alembic.config import Config

    from alembic import command

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    command.upgrade(cfg, "head")
    print("Database is at the latest migration.")
    return 0


async def _seed(_: argparse.Namespace) -> int:
    from .api.registry import optional

    async with sessionmaker()() as s:
        tax = optional("app.taxonomy")
        if tax is not None and hasattr(tax, "seed_taxonomy"):
            res = await tax.seed_taxonomy(s)
            print(f"Taxonomy seeded: {res}")
        else:
            print("Taxonomy module not available; skipped.")
        base = optional("app.sources.base")
        if base is not None:
            n = await base.sync_adapter_states(s)
            print(f"Source adapter state rows created: {n}")
        else:
            print("Source registry not available; skipped.")
        await s.commit()
    return 0


async def _export_categories(args: argparse.Namespace) -> int:
    from .api.categories import build_taxonomy_rows, taxonomy_to_csv

    async with sessionmaker()() as s:
        rows = await build_taxonomy_rows(s)
    out = json.dumps({"categories": rows}, indent=2) if args.format == "json" else taxonomy_to_csv(rows)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"Wrote {len(rows)} categories to {args.out}")
    else:
        print(out)
    return 0


async def _retention(_: argparse.Namespace) -> int:
    from .retention import apply_retention

    async with sessionmaker()() as s:
        print(json.dumps(await apply_retention(s), indent=2))
    return 0


async def _bench(args: argparse.Namespace) -> int:
    if args.action == "list" or not args.name:
        print("\n".join(sorted(BENCH_HOOKS)) or "No benchmark hooks registered.")
        return 0
    hook = BENCH_HOOKS.get(args.name)
    if hook is None:
        print(f"Unknown benchmark '{args.name}'. Available: {', '.join(sorted(BENCH_HOOKS))}", file=sys.stderr)
        return 2
    print(json.dumps(await hook(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="Lead Intelligence operator CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("create-admin", help="create an administrator (interactive, or --from-env on first start)")
    a.add_argument("--email")
    a.add_argument("--role", choices=["admin", "operator", "viewer"], default="admin")
    a.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin instead of prompting")
    a.add_argument("--from-env", action="store_true", help="use LEAD_INITIAL_ADMIN_EMAIL/PASSWORD, only if no users exist")
    sub.add_parser("migrate", help="alembic upgrade head")
    sub.add_parser("seed", help="seed taxonomy and source adapter state")
    e = sub.add_parser("export-categories", help="export the taxonomy")
    e.add_argument("--format", choices=["json", "csv"], default="json")
    e.add_argument("--out")
    sub.add_parser("apply-retention", help="apply the data-retention settings now")
    b = sub.add_parser("bench", help="run benchmark hooks")
    b.add_argument("action", nargs="?", choices=["list", "run"], default="list")
    b.add_argument("name", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "migrate":
        return _migrate(args)
    fn = {"create-admin": _create_admin, "seed": _seed, "export-categories": _export_categories, "apply-retention": _retention, "bench": _bench}[args.cmd]

    async def run() -> int:
        try:
            return await fn(args)
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
