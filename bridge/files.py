"""Serve only artifacts referenced by this chat inside its workspace/visualizations."""
import hashlib
import re
from pathlib import Path

from .model import ordered_turns, items_array

MARKDOWN_PATH = re.compile(r'!?\[[^\]\n]*\]\((?:<([^>]+)>|([^\n)]+))\)')


def artifact_paths(state, codex_home):
    roots = [Path(codex_home) / 'visualizations']
    if state.get('cwd'):
        roots.append(Path(state['cwd']))
    roots = [root.resolve() for root in roots]
    candidates = set()
    for turn in ordered_turns(state):
        for item in items_array(turn.get('items')):
            if item.get('type') in ('agentMessage', 'assistantMessage'):
                for match in MARKDOWN_PATH.finditer(item.get('text', '')):
                    candidates.add(match[1] or match[2])
            for attachment in item.get('content', []) if isinstance(item.get('content'), list) else []:
                if isinstance(attachment, dict) and attachment.get('type') in ('localImage', 'image', 'file'):
                    value = attachment.get('path', attachment.get('url'))
                    if isinstance(value, str):
                        candidates.add(value)
    result = {}
    for raw in candidates:
        # Preserve the displayed link while removing an editor line suffix from the path.
        value = re.sub(r':\d+$', '', raw)
        path = Path(value)
        if not path.is_absolute():
            continue
        try:
            path = path.resolve(strict=True)
            if not any(root in path.parents for root in roots) or not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
                continue
        except OSError:
            continue
        key = hashlib.sha256(str(path).encode()).hexdigest()
        result[key] = {'path': path, 'reference': raw, 'name': path.name,
                       'image': path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp')}
    return result
