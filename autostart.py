#!/usr/bin/env python3
"""Hidden Scheduled Task entry point; configure through configure.py."""
import argparse
import sys
from pathlib import Path

from bridge.autostart import profile_from_settings, run_worker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    profile = profile_from_settings(Path(__file__).resolve().parent, args.settings)
    try:
        run_worker(profile)
    except Exception as error:
        profile.write_status("error", error=type(error).__name__)
        if sys.stderr:
            print(type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
