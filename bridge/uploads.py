"""Bounded, authenticated local attachments; client paths are never accepted."""
import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path

MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 200 * 1024 * 1024
EXTENSIONS = set((".png .jpg .jpeg .gif .webp .pdf .txt .md .csv .tsv .json .yaml .yml "
                  ".xml .html .css .js .ts .tsx .jsx .py .rs .go .java .c .cpp .h "
                  ".sql .log .toml .ini .zip .docx .xlsx .pptx").split())
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class Uploads:
    def __init__(self, root):
        self.root = Path(root).resolve() / "uploads"
        self.lock = threading.RLock()

    def put(self, thread_id, owner, name, data):
        uuid.UUID(thread_id)
        if not isinstance(name, str) or not name or len(name) > 180 or re.search(r'[\x00-\x1f<>:"/\\|?*]', name):
            raise ValueError("附件名称无效")
        suffix = Path(name).suffix.lower()
        if suffix not in EXTENSIONS:
            raise ValueError("不支持此附件格式")
        if not 0 < len(data) <= MAX_FILE:
            raise ValueError("附件须为 1 字节至 10 MB")
        image = suffix in IMAGES
        if image and not self._image(data, suffix):
            raise ValueError("图片内容与格式不匹配")
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if sum(1 for p in self.root.iterdir() if p.is_dir()) >= 512:
                raise ValueError("附件数量已达 512 个，请在电脑上整理后重试")
            total = sum(p.stat().st_size for p in self.root.glob("*/*") if p.is_file())
            if total + len(data) > MAX_TOTAL:
                raise ValueError("附件存储已达 200 MB，请在电脑上整理后重试")
            upload_id = str(uuid.uuid4())
            folder = self.root / upload_id
            folder.mkdir(mode=0o700)
            path = folder / ("attachment" + suffix)
            path.write_bytes(data)
            meta = {"id": upload_id, "thread": thread_id, "owner": self._owner(owner),
                    "name": name, "size": len(data), "image": image, "suffix": suffix, "at": time.time()}
            (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return {key: meta[key] for key in ("id", "name", "size", "image")}

    def resolve(self, thread_id, owner, ids):
        if not isinstance(ids, list) or len(ids) > 8 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("每条消息最多 8 个不同附件")
        result = []
        for upload_id in ids:
            if str(uuid.UUID(upload_id)) != upload_id:
                raise ValueError("附件标识无效")
            folder = self.root / upload_id
            try:
                meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ValueError("附件不存在，请重新添加") from None
            if meta["thread"] != thread_id or meta["owner"] != self._owner(owner):
                raise PermissionError("附件不属于当前登录会话或线程，请重新添加")
            path = (folder / ("attachment" + meta["suffix"])).resolve()
            if folder.resolve() != path.parent or not path.is_file() or path.stat().st_size != meta["size"]:
                raise ValueError("附件文件无效，请重新添加")
            result.append({"id": upload_id, "name": meta["name"], "path": str(path), "image": meta["image"]})
        return result

    @staticmethod
    def _owner(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _image(data, suffix):
        if suffix == ".png":
            return data.startswith(b"\x89PNG\r\n\x1a\n")
        if suffix in (".jpg", ".jpeg"):
            return data.startswith(b"\xff\xd8\xff")
        if suffix == ".gif":
            return data.startswith((b"GIF87a", b"GIF89a"))
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
