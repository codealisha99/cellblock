"""`cellblock` command: run the server, mint keys, and a small admin surface over the HTTP API."""

from __future__ import annotations

import argparse
import os
import sys

from cellblock.config import Settings
from cellblock_sdk import Sandbox
from cellblock_sdk.client import ApiError


def _client():
    from cellblock_sdk import Cellblock

    base = os.environ.get("CELLBLOCK_URL", f"http://127.0.0.1:{Settings.from_env().port}")
    key = os.environ.get("CELLBLOCK_API_KEY")
    if not key:
        sys.exit("set CELLBLOCK_API_KEY (mint one with `cellblock key create NAME`)")
    return Cellblock(base_url=base, api_key=key)


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from cellblock.api import create_app

    uvicorn.run(create_app(Settings.from_env()), host=args.host, port=args.port or Settings.from_env().port)


def _cmd_key_create(args: argparse.Namespace) -> None:
    from cellblock.service import Service
    from cellblock.store import Store

    settings = Settings.from_env()
    service = Service(settings, Store(settings.db_path), backend=None)  # key minting never touches Docker
    print(service.create_key(args.name))


def _cmd_ls(_: argparse.Namespace) -> None:
    for sb in _client().list_sandboxes():
        print(f"{sb['id']}  {sb['status']:<8} {sb['image']}  cpu={sb['cpu']} mem={sb['memory_mb']}M net={sb['network']}")


def _cmd_exec(args: argparse.Namespace) -> None:
    result = Sandbox.connect(args.sandbox_id, client=_client()).exec(args.command, timeout=args.timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    sys.exit(result.exit_code if result.exit_code is not None else 1)


def _cmd_logs(args: argparse.Namespace) -> None:
    for ex in Sandbox.connect(args.sandbox_id, client=_client()).execs():
        print(f"== {ex['id']} {ex['status']} exit={ex['exit_code']} argv={ex['argv']}")
        print(ex["stdout"], end="")
        if ex["stderr"]:
            print(ex["stderr"], end="", file=sys.stderr)


def _cmd_rm(args: argparse.Namespace) -> None:
    Sandbox.connect(args.sandbox_id, client=_client()).delete()
    print(f"removed {args.sandbox_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cellblock", description=__doc__)
    sub = parser.add_subparsers(required=True)

    serve = sub.add_parser("serve", help="run the control plane")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.set_defaults(func=_cmd_serve)

    key = sub.add_parser("key", help="manage API keys").add_subparsers(required=True)
    create = key.add_parser("create", help="mint an API key (printed once)")
    create.add_argument("name")
    create.set_defaults(func=_cmd_key_create)

    sub.add_parser("ls", help="list sandboxes").set_defaults(func=_cmd_ls)

    ex = sub.add_parser("exec", help="run a shell command in a sandbox")
    ex.add_argument("sandbox_id")
    ex.add_argument("command")
    ex.add_argument("--timeout", type=int, default=30)
    ex.set_defaults(func=_cmd_exec)

    logs = sub.add_parser("logs", help="show recent exec history with output")
    logs.add_argument("sandbox_id")
    logs.set_defaults(func=_cmd_logs)

    rm = sub.add_parser("rm", help="destroy a sandbox")
    rm.add_argument("sandbox_id")
    rm.set_defaults(func=_cmd_rm)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except ApiError as exc:  # report the server's message, not a traceback
        sys.exit(f"error: {exc.message}")


if __name__ == "__main__":
    main()
