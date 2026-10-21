"""`gr` command line: `gr hash-password`, `gr serve`."""

import argparse
import getpass
import sys
from collections.abc import Sequence

from gr.auth import hash_password


def _hash_password(args: argparse.Namespace) -> int:
    if args.stdin or not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Пароль: ")
        if getpass.getpass("Повторите пароль: ") != password:
            print("Пароли не совпадают.", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("Пароль короче 8 символов.", file=sys.stderr)
        return 1
    encoded = hash_password(password)
    print(f"{args.login}:{encoded}" if args.login else encoded)
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from gr.settings import get_settings

    uvicorn.run(
        "gr.app:create_app",
        factory=True,
        host=args.host,
        port=get_settings().app_port,
        proxy_headers=True,
        # None: uvicorn takes FORWARDED_ALLOW_IPS from the environment (default 127.0.0.1).
        forwarded_allow_ips=None,
        workers=1,  # the in-process JobRunner and the SQLite writer lock assume one process
        log_config=None,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gr", description="AI-анализ НПА")
    commands = parser.add_subparsers(dest="command", required=True)

    hashing = commands.add_parser("hash-password", help="строка для AUTH_USERS")
    hashing.add_argument("--login", help="вывести сразу в формате login:hash")
    hashing.add_argument("--stdin", action="store_true", help="прочитать пароль из stdin")
    hashing.set_defaults(handler=_hash_password)

    serve = commands.add_parser("serve", help="запустить веб-сервис")
    serve.add_argument("--host", default="127.0.0.1", help="в контейнере — 0.0.0.0")
    serve.set_defaults(handler=_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.handler(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
