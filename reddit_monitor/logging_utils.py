from datetime import datetime
from pathlib import Path


def append_log(path, message):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    line = "[%s] %s\n" % (stamp, message)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def tail_lines(path, count=10):
    path = Path(path)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-count:]
