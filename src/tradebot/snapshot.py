"""Copie cohérente des journaux SQLite pour la sauvegarde (audit sécurité du 18 nov.).

Copier les fichiers .db / .db-wal pendant que le robot écrit peut donner une base
incohérente, découverte seulement à la restauration. L'API de sauvegarde de SQLite copie une
image cohérente même pendant les écritures. Les copies vont dans data/backups/ ; c'est elles
que le script de sauvegarde archive.

    python -m tradebot.snapshot [--data data] [--out data/backups]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def snapshot(data_dir: str | Path = "data", out_dir: str | Path = "data/backups") -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    done = []
    for db in sorted(Path(data_dir).glob("*.db")):
        # extension distincte : jamais exclue avec les fichiers vivants (*.db) par la sauvegarde
        # (restauration : renommer en .db)
        dest = out / f"{db.stem}.sqlite"
        tmp = out / f"{db.stem}.sqlite.tmp"
        src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)                     # image cohérente, même pendant les écritures
        finally:
            dst.close()
            src.close()
        os.replace(tmp, dest)                   # jamais une copie à moitié écrite
        done.append(dest)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copie cohérente des journaux SQLite")
    parser.add_argument("--data", default="data")
    parser.add_argument("--out", default="data/backups")
    args = parser.parse_args(argv)
    for path in snapshot(args.data, args.out):
        print(f"copie cohérente : {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
