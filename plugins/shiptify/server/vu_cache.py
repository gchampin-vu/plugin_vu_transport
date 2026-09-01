#!/usr/bin/env python3
"""
Cache local SQLite, partage par les connecteurs de l'equipe.

Pourquoi ce module existe
-------------------------
Les API metier de l'equipe ne savent pas agreger et ne rendent aucun total :
la seule facon de repondre a "combien sur douze mois" est de paginer. Or
paginer coute deux choses. Du temps, mais surtout - et c'est ce qui a ete
mesure le 2026-08-28 - de la **fenetre de conversation** : cent envois
Shiptify en colonnes completes pesent quarante mille tokens, cent visiteurs
Peripass huit mille. Reposer la meme question le lendemain refait le meme
trajet, et cote Peripass il est facture au quota, qui compte les ressources
lues et non les requetes.

Le cache casse ce couple. On rapatrie une fois, on interroge ensuite en SQL,
instantanement et sans consommer de quota. C'est la forme que le connecteur
Yooz a prise le premier ; ce module la rend disponible aux autres.

Ce fichier est **identique dans les trois plugins**. Il est duplique et non
partage parce qu'un plugin Claude Code est autonome : il embarque son serveur,
son venv et ses dependances, et ne peut pas importer le voisin. Une correction
ici se recopie donc dans les trois - le test test_offline.py de chaque plugin
compare l'empreinte du fichier pour que la divergence se voie plutot que de
s'installer en silence.

Lecture seule sur le contenu : le seul ecrivain est la synchronisation.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import re
import sqlite3
from typing import Any, Iterable


class CacheError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Ouverture
# --------------------------------------------------------------------------

def quote(name: str) -> str:
    """Echappe un nom de colonne. Les colonnes aplaties portent des points."""
    return '"' + name.replace('"', '""') + '"'


def table_name(raw: str) -> str:
    """Normalise un nom de jeu de donnees en nom de table sur."""
    name = re.sub(r"[^a-z0-9_]+", "_", (raw or "").strip().lower()).strip("_")
    if not name:
        raise CacheError("Nom de jeu de donnees vide.")
    if name.startswith("sqlite") or name.startswith("_"):
        raise CacheError(f"Nom de jeu de donnees refuse : '{raw}'.")
    return name


def open_rw(path: pathlib.Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    # WAL : une lecture pendant une synchronisation ne bloque pas.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def open_ro(path: pathlib.Path, hint: str) -> sqlite3.Connection:
    if not path.exists():
        raise CacheError(f"Le cache local est vide ({path}).\n{hint}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY name"
        )
    ]


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({quote(table)})")]
    except sqlite3.Error:
        return []


# --------------------------------------------------------------------------
# Ecriture
# --------------------------------------------------------------------------

def _cell(value: Any) -> Any:
    """Une valeur SQLite. Les structures sont figees en JSON compact."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, bool):
        return 1 if value else 0
    if value is None or isinstance(value, (int, float, str)):
        return value
    return str(value)


def to_number(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def ensure_table(
    conn: sqlite3.Connection,
    table: str,
    cols: Iterable[str],
    numeric: set[str],
    index_on: Iterable[str] = (),
) -> None:
    """Cree la table, ou lui ajoute les colonnes apparues depuis.

    Le schema suit la donnee : une API qui ajoute un champ ne doit pas casser
    la synchronisation du lendemain. ALTER TABLE ADD COLUMN est instantane sous
    SQLite, on peut se le permettre a chaque passage.
    """
    existing = columns(conn, table)
    wanted = list(dict.fromkeys(cols))
    if not existing:
        body = ", ".join(
            f"{quote(c)} {'REAL' if c in numeric else 'TEXT'}" for c in wanted
        )
        conn.execute(
            f"CREATE TABLE {quote(table)} (_row_key TEXT PRIMARY KEY, _synced_at TEXT"
            + (f", {body}" if body else "")
            + ")"
        )
    else:
        for col in wanted:
            if col not in existing:
                kind = "REAL" if col in numeric else "TEXT"
                conn.execute(
                    f"ALTER TABLE {quote(table)} ADD COLUMN {quote(col)} {kind}"
                )
    present = set(columns(conn, table))
    for col in index_on:
        if col in present:
            safe = re.sub(r"[^a-z0-9_]+", "_", col.lower())
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {quote('idx_' + table + '_' + safe)} "
                f"ON {quote(table)} ({quote(col)})"
            )


def upsert(
    conn: sqlite3.Connection,
    table: str,
    rows: list[dict[str, Any]],
    key_of,
    numeric: set[str],
    index_on: Iterable[str] = (),
) -> int:
    """Ecrit des lignes deja aplaties. Rend le nombre de lignes ecrites.

    INSERT OR REPLACE sur une cle stable : une resynchronisation complete est
    donc toujours juste et ne cree jamais de doublon. C'est ce qui permet de
    dire a l'utilisateur qu'en cas de doute, il relance un 'full'.
    """
    if not rows:
        return 0
    cols: list[str] = []
    for row in rows:
        for key in row:
            if key not in ("_row_key", "_synced_at") and key not in cols:
                cols.append(key)
    ensure_table(conn, table, cols, numeric, index_on)

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    all_cols = ["_row_key", "_synced_at"] + cols
    sql = (
        f"INSERT OR REPLACE INTO {quote(table)} "
        f"({', '.join(quote(c) for c in all_cols)}) "
        f"VALUES ({', '.join('?' for _ in all_cols)})"
    )
    payload = []
    for row in rows:
        line: list[Any] = [key_of(row), now]
        for col in cols:
            value = row.get(col)
            line.append(to_number(value) if col in numeric else _cell(value))
        payload.append(line)
    conn.executemany(sql, payload)
    return len(payload)


def meta_get(conn: sqlite3.Connection, key: str) -> str:
    try:
        row = conn.execute("SELECT value FROM _meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""
    try:
        return (row["value"] or "") if isinstance(row, sqlite3.Row) else (row[0] or "")
    except (IndexError, KeyError):
        return ""


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO _meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma"
    r"|vacuum|reindex)\b",
    re.I,
)


def guard_sql(sql: str) -> str:
    """N'accepte qu'un SELECT. Le cache est en lecture seule pour l'utilisateur."""
    stripped = re.sub(r"--[^\n]*", " ", sql or "")
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.S).strip().rstrip(";").strip()
    if not stripped:
        raise CacheError("Requete SQL vide.")
    if ";" in stripped:
        raise CacheError("Une seule instruction SQL a la fois.")
    head = stripped.lstrip("(").lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise CacheError(
            "Seules les requetes SELECT (ou WITH ... SELECT) sont acceptees."
        )
    if _FORBIDDEN_SQL.search(stripped):
        raise CacheError(
            "Mot-cle d'ecriture detecte : ce serveur est en lecture seule sur le cache."
        )
    return stripped


def select(
    conn: sqlite3.Connection, sql: str, params: tuple = (), limit: int = 200
) -> tuple[list[dict[str, Any]], bool]:
    """Execute une requete bornee. Rend (lignes, il_en_reste).

    Le drapeau compte autant que les lignes : sans lui, un rendu tronque
    annonce un nombre de lignes qui n'est pas un total, et ce nombre finit cite
    comme un volume.
    """
    try:
        cur = conn.execute(f"SELECT * FROM ({sql}) LIMIT {int(limit) + 1}", params)
        rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        raise CacheError(f"SQL refuse par SQLite : {exc}\nRequete : {sql}") from exc
    more = len(rows) > int(limit)
    return rows[: int(limit)], more


def count_of(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """Le VRAI total d'une requete, sans la rapatrier."""
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()
    except sqlite3.Error as exc:
        raise CacheError(f"SQL refuse par SQLite : {exc}") from exc
    return int(row[0]) if row else 0


def to_csv_text(
    rows: list[dict[str, Any]], cols: list[str] | None = None, delimiter: str = ";"
) -> str:
    if not rows:
        return ""
    if not cols:
        cols = []
        for row in rows:
            for key in row:
                if key not in cols:
                    cols.append(key)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=cols,
        delimiter=delimiter,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in cols})
    return buf.getvalue()
