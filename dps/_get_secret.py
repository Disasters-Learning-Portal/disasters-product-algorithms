#!/usr/bin/env python
"""Print a MAAP secret's VALUE to stdout, given its NAME as argv[1].

Used by DPS run.sh scripts to pull data-provider credentials (e.g. Copernicus
COP_USER / COP_PASS) from MAAP's encrypted secret store at RUN TIME instead of
taking them as job inputs -- so the values never appear in the job's parameters
or log. The operator stores them once from a MAAP notebook:

    from maap.maap import MAAP
    maap = MAAP()
    maap.secrets.add_secret("COP_USER", "you@example.com")
    maap.secrets.add_secret("COP_PASS", "your-copernicus-password")

Auth is AMBIENT inside a DPS job: after job submission the DPS wrapper requests a
user proxy token and exposes it as MAAP_PGT, which maap-py's _get_api_header()
sends automatically -- so MAAP() here needs no token/host handling.

Only the secret NAME is passed as an argument (names are not sensitive); the
VALUE is written to stdout with NO trailing newline so a shell can capture it
cleanly:  COP_USER="$(python _get_secret.py COP_USER)". Nothing is echoed.
"""
import sys


def main():
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        sys.stderr.write("usage: _get_secret.py <SECRET_NAME>\n")
        return 2

    secret_name = sys.argv[1].strip()

    try:
        from maap.maap import MAAP
        value = MAAP().secrets.get_secret(secret_name)
    except Exception as e:  # noqa: BLE001 -- surface any maap-py/auth error clearly
        sys.stderr.write(f"ERROR: could not read MAAP secret '{secret_name}': {e}\n")
        return 1

    # get_secret returns the value directly on success; a missing/failed lookup
    # can come back empty or as a non-string error payload -- treat both as fatal
    # so run.sh fails fast with a clear message instead of downloading nothing.
    if not isinstance(value, str) or not value:
        sys.stderr.write(
            f"ERROR: MAAP secret '{secret_name}' is empty or not set. "
            f"Store it with maap.secrets.add_secret('{secret_name}', ...).\n"
        )
        return 1

    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
