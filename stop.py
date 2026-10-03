#!/usr/bin/env python3
"""Ask this gateway to shut down gracefully on Windows and macOS."""
import argparse
from pathlib import Path

from bridge.lifecycle import request_stop
from bridge.service_profile import default_config


def main():
    parser = argparse.ArgumentParser(description='Stop this Codex mobile gateway')
    parser.add_argument('--config', type=Path, default=default_config())
    args = parser.parse_args()
    try:
        request_stop(args.config.parent)
    except RuntimeError as exc:
        parser.exit(1, str(exc) + '\n')
    print('Gateway stopped.')


if __name__ == '__main__':
    main()
