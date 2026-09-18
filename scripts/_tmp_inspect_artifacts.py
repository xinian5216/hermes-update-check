"""Release artifact inspection: no secrets, no local state, no machine paths."""

from __future__ import annotations

import re
import tarfile
import zipfile
from pathlib import Path

SUSPECT_NAME = re.compile(r"(\.env|secret|credential|registry\.json|\.patch$|snapshot|token|\.key$|id_rsa)", re.I)
PATH_PAT = re.compile(r"([A-Za-z]:[\\/]Users[\\/]|/home/[a-z]|19201|D:[\\/]software|C:[\\/]Users|hermes-agent)", re.I)
SECRET_PAT = re.compile(r"(ghp_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9-]{16,}|AKIA[0-9A-Z]{12,}|BEGIN [A-Z ]*PRIVATE KEY)")


def scan(kind: str, names: list[str], blobs: list[tuple[str, bytes]]) -> None:
    print(f"--- {kind}: {len(names)} entries ---")
    bad_names = [n for n in names if SUSPECT_NAME.search(n)]
    print("  可疑文件名 (.env/registry/patch/snapshot/token/key):", bad_names or "无")
    path_hits: list[tuple[str, str]] = []
    secret_hits: list[tuple[str, str]] = []
    for name, data in blobs:
        text = data.decode("utf-8", "ignore")
        for m in PATH_PAT.finditer(text):
            path_hits.append((name, m.group(0)))
        for m in SECRET_PAT.finditer(text):
            secret_hits.append((name, m.group(0)[:12] + "…"))
    print("  本机路径/用户名命中:", path_hits[:6] or "无")
    print("  密钥样式命中:", secret_hits[:3] or "无")


def main() -> None:
    wheel = next(Path("dist").glob("*.whl"))
    sdist = next(Path("dist").glob("*.tar.gz"))
    print(f"wheel: {wheel.name} ({wheel.stat().st_size} B)")
    print(f"sdist: {sdist.name} ({sdist.stat().st_size} B)\n")

    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        blobs = [(n, z.read(n)) for n in names if not n.endswith("/")]
        scan("wheel", names, blobs)
        print("\n--- wheel 文件清单 ---")
        for n in names:
            print("  ", n)

    print()
    with tarfile.open(sdist) as t:
        members = t.getmembers()
        names = t.getnames()
        blobs = []
        for member in members:
            if member.isfile():
                handle = t.extractfile(member)
                if handle is not None:
                    blobs.append((member.name, handle.read()))
        scan("sdist", names, blobs)
        print("\n--- sdist 顶层内容 ---")
        for n in sorted({name.split("/", 1)[-1].split("/")[0] for name in names})[:40]:
            print("  ", n)


if __name__ == "__main__":
    main()
