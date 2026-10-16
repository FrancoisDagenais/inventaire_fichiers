#!/usr/bin/env python3
"""Inventaire incrémental de fichiers avec SQLite et détection de doublons."""

import argparse
import csv
import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_size ON files(size)")
    conn.commit()
    return conn


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def scan(conn: sqlite3.Connection, roots: list[Path]) -> None:
    # Charger les métadonnées existantes pour éviter de recalculer les hashes.
    existing = {
        row[0]: (row[1], row[2], row[3])
        for row in conn.execute("SELECT path, size, mtime_ns, sha256 FROM files")
    }
    seen: set[str] = set()
    examined = hashed = reused = errors = 0
    now = datetime.now(timezone.utc).isoformat()

    for root in roots:
        if not root.exists() or not root.is_dir():
            print(f"AVERTISSEMENT : dossier inaccessible ou inexistant : {root}", file=sys.stderr)
            errors += 1
            continue

        for folder, dirnames, filenames in os.walk(root, followlinks=False):
            # Ne pas entrer dans les dossiers symboliques.
            dirnames[:] = [
                name for name in dirnames
                if not (Path(folder) / name).is_symlink()
            ]
            for filename in filenames:
                path = Path(folder) / filename
                if path.is_symlink():
                    continue

                try:
                    stat = path.stat()
                    if not path.is_file():
                        continue
                    path_key = str(path.resolve())
                    seen.add(path_key)
                    examined += 1
                    previous = existing.get(path_key)

                    if previous and previous[0] == stat.st_size and previous[1] == stat.st_mtime_ns:
                        checksum = previous[2]
                        reused += 1
                    else:
                        # Éviter d'enregistrer un hash si le fichier change pendant la lecture.
                        checksum = sha256_file(path)
                        stat_after = path.stat()
                        if stat.st_size != stat_after.st_size or stat.st_mtime_ns != stat_after.st_mtime_ns:
                            print(f"MODIFIÉ PENDANT LA LECTURE, ignoré pour cette passe : {path}", file=sys.stderr)
                            errors += 1
                            continue
                        hashed += 1

                    conn.execute("""
                        INSERT INTO files(path, size, mtime_ns, sha256, checked_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            size=excluded.size,
                            mtime_ns=excluded.mtime_ns,
                            sha256=excluded.sha256,
                            checked_at=excluded.checked_at
                    """, (path_key, stat.st_size, stat.st_mtime_ns, checksum, now))
                except (OSError, PermissionError) as exc:
                    print(f"ERREUR : {path} : {exc}", file=sys.stderr)
                    errors += 1

    # Supprimer de l'inventaire les fichiers disparus des racines analysées.
    # Les autres racines (non passées à cette commande) restent intactes.
    removed = 0
    all_paths = [row[0] for row in conn.execute("SELECT path FROM files")]
    for old_path in all_paths:
        old = Path(old_path)
        if any(is_inside(old, root) for root in roots) and old_path not in seen:
            conn.execute("DELETE FROM files WHERE path = ?", (old_path,))
            removed += 1

    conn.commit()
    print(f"Fichiers examinés : {examined}")
    print(f"Checksums calculés : {hashed}")
    print(f"Checksums réutilisés : {reused}")
    print(f"Entrées supprimées de l'inventaire : {removed}")
    print(f"Erreurs / avertissements : {errors}")


def duplicates(conn: sqlite3.Connection, csv_path: Path | None) -> None:
    query = """
        SELECT f.sha256, f.size, f.path, groups.file_count
        FROM files AS f
        JOIN (
            SELECT sha256, COUNT(*) AS file_count
            FROM files
            GROUP BY sha256
            HAVING COUNT(*) > 1
        ) AS groups ON groups.sha256 = f.sha256
        ORDER BY f.sha256, f.path
    """
    rows = conn.execute(query).fetchall()

    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["sha256", "nombre_fichiers", "taille_octets", "dossier", "fichier", "chemin_complet"])
            for checksum, size, full_path, count in rows:
                path = Path(full_path)
                writer.writerow([checksum, count, size, str(path.parent), path.name, full_path])
        print(f"Rapport CSV écrit : {csv_path}")
        print(f"Fichiers dans les groupes de doublons : {len(rows)}")
        return

    if not rows:
        print("Aucun doublon trouvé dans l'inventaire.")
        return

    current_checksum = None
    for checksum, size, full_path, count in rows:
        if checksum != current_checksum:
            if current_checksum is not None:
                print()
            print(f"SHA-256 : {checksum}  |  {count} fichiers  |  {size} octets")
            current_checksum = checksum
        print(f"  {full_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventaire incrémental de fichiers et recherche de doublons par SHA-256."
    )
    parser.add_argument("--db", type=Path, default=Path("inventaire_fichiers.db"),
                        help="fichier SQLite (défaut : ./inventaire_fichiers.db)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="créer ou actualiser l'inventaire")
    scan_parser.add_argument("roots", nargs="+", type=Path, help="un ou plusieurs dossiers à analyser")

    dup_parser = subparsers.add_parser("duplicates", help="afficher ou exporter les doublons")
    dup_parser.add_argument("--csv", type=Path, help="exporter le rapport dans un fichier CSV")

    args = parser.parse_args()
    db_path = args.db.expanduser().resolve()

    try:
        with connect_db(db_path) as conn:
            if args.command == "scan":
                roots = [root.expanduser().resolve() for root in args.roots]
                scan(conn, roots)
            elif args.command == "duplicates":
                duplicates(conn, args.csv.expanduser() if args.csv else None)
    except sqlite3.Error as exc:
        print(f"Erreur SQLite : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
