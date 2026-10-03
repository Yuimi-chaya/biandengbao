#!/usr/bin/env python3
"""Source-only handover; works with the existing v0.2.0 manager binary."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.manager import default_config
from bridge.startup_migration import StartupMigration


def main():
    parser = argparse.ArgumentParser(description="Inspect/migrate the legacy Windows startup task.")
    parser.add_argument("--legacy-config", type=Path, required=True)
    parser.add_argument("--legacy-control", type=Path, required=True)
    parser.add_argument("--legacy-worker", type=Path, required=True)
    parser.add_argument("--target-config", type=Path, default=default_config())
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.yes:
        parser.error("--apply requires --yes; without --apply this command is read-only")
    for path in (args.legacy_config, args.legacy_control, args.legacy_worker, args.target_config):
        if not path.is_absolute():
            parser.error("All configuration/worker paths must be absolute")
    if args.backup_dir and not args.backup_dir.is_absolute():
        parser.error("--backup-dir must be absolute")
    migration = StartupMigration(args.legacy_config, args.legacy_control,
                                 args.legacy_worker, args.target_config)
    if args.apply:
        result = migration.apply(args.backup_dir or args.target_config.parent / "migration-backups")
    else:
        result, _ = migration.plan()
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    os.umask(0o077)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
