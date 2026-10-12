"""Mint a bearer token for a real deployment (``AUTH_MODE=token``).

This is an *operator* tool, not an endpoint. Issuance is deliberately not an HTTP
route: a ``/token`` endpoint in this service would be an authentication bypass with
extra steps, because it would have to decide who may ask for a token without any
identity to decide with. In production the token comes from an identity provider or
from an operator running this command against the same ``AUTH_SECRET`` the API verifies
with.

The secret is read from configuration (``AUTH_SECRET``, from the environment or the
root ``.env``) and never from the command line, so it does not land in shell history or
the process table. The token is written to stdout so it can be piped; it is a
credential, so treat it as one.

    python -m scripts.issue_token --subject alice --role reviewer --ttl 3600
"""

from __future__ import annotations

import argparse
import sys

from app.adapters.auth import issue_token
from app.config import get_settings
from app.domain.identity import known_roles

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.issue_token",
        description="Issue an HS256 bearer token for AUTH_MODE=token deployments.",
    )
    parser.add_argument("--subject", required=True, help="the identity the token asserts")
    parser.add_argument(
        "--role",
        required=True,
        choices=known_roles,
        help="the role the token asserts (only 'reviewer' unlocks the decision gate)",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        help="token lifetime in seconds (default: configured AUTH_TOKEN_TTL_SECONDS)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()

    if not settings.auth_secret:
        print(
            "AUTH_SECRET is not set; refusing to mint a token with no signing key. "
            "Set it in the environment or the root .env (32+ random bytes recommended).",
            file=sys.stderr,
        )
        return 2

    ttl = args.ttl if args.ttl is not None else settings.auth_token_ttl_seconds
    token = issue_token(
        secret=settings.auth_secret,
        subject=args.subject,
        role=args.role,
        issuer=settings.auth_issuer,
        ttl_seconds=ttl,
    )
    print(token)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via main()
    sys.exit(main())
