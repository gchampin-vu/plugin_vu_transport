#!/usr/bin/env python3
"""
MCP gisco : referentiel geographique europeen, en LECTURE SEULE.

Trois usages :
    python server.py            # mode serveur MCP (stdio) - lance par Claude Code
    python server.py doctor     # diagnostic de configuration et de connexion
    python server.py --help

Ce que ce connecteur apporte. GISCO est le service geographique d'Eurostat : il
publie, librement et sans compte, le decoupage administratif europeen - pays,
regions NUTS, communes LAU, villes, codes postaux. C'est le referentiel qui
permet de repondre a « ce code postal, c'est quelle commune, quelle region,
zone dense ou rurale ? » sans acheter de base d'adresses.

Lecture seule par construction : GISCO ne publie que des fichiers statiques et
une API de recherche en GET. Il n'y a rien a ecrire, et rien n'est ecrit.

Comment il travaille. Les fichiers GISCO sont gros - 75 Mo pour les communes,
200 Mo pour les codes postaux. On ne les retelecharge pas a chaque question :
`gisco_sync` les rapatrie UNE fois dans un cache SQLite local, et toutes les
lectures se font ensuite hors ligne, en millisecondes. Le format retenu est le
GeoPackage, qui est lui-meme une base SQLite : le `sqlite3` de la bibliotheque
standard l'ouvre, sans GDAL ni geopandas a installer chez le collegue.

Le Royaume-Uni n'est plus dans GISCO depuis le Brexit. Son equivalent vient de
l'ONS et se charge dans les MEMES tables, avec source='ONS'. Chaque reponse dit
d'ou vient chaque ligne : melanger les deux sans le dire serait un chiffre faux.

Aucun secret n'est necessaire : GISCO et l'ArcGIS de l'ONS sont ouverts. Il n'y
a donc ni cle a saisir, ni fichier .env a remplir.
"""

from __future__ import annotations

import csv
import datetime as dt
import functools
import json
import logging
import os
import pathlib
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from typing import Any, Callable, Iterable, Iterator

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import geo

HERE = pathlib.Path(__file__).resolve().parent
CATALOGUE_FILE = HERE / "catalogue.json"

DEFAULT_TIMEOUT_S = 120.0
# Au-dela, un telechargement est refuse sans confirmation explicite. Le plus
# gros fichier connu (les codes postaux) pese 200 Mo.
DEFAULT_MAX_DOWNLOAD_MB = 400
# Les couches sous ce poids se synchronisent toutes seules a la premiere
# question qui en a besoin : 1,5 Mo pour les NUTS au 20M, 1 Mo pour les pays.
# Au-dela, la synchronisation reste un geste demande - elle ecrit sur le disque.
DEFAULT_AUTOSYNC_MAX_MB = 6

# Plafond de caracteres rendus dans la conversation par un outil de liste.
RENDER_MAX_CHARS = 20_000
# Taille des lots d'insertion. Compromis memoire / nombre de transactions.
BATCH = 5_000
# Pagination de l'ArcGIS de l'ONS. 2 000 est la limite de transfert du service.
ARCGIS_PAGE = 2_000

RETRY_ATTEMPTS = 3
RETRY_STATUSES = (429, 500, 502, 503, 504)

# Dossiers synchronises : un export n'y va jamais.
SYNCED_MARKERS = ("onedrive", "cafom", "sharepoint", "dropbox", "google drive")


class GiscoError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Configuration - aucun secret, uniquement des chemins et des plafonds
# --------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _local_root() -> pathlib.Path:
    """Racine locale de l'outil, hors de tout dossier synchronise.

    PAS %LOCALAPPDATA% : un Python empaquete (Microsoft Store, Python Manager)
    en donne a ses processus enfants une vue virtualisee, et le plugin ecrirait
    d'un cote pendant qu'un terminal lirait de l'autre, sans erreur. Le profil
    utilisateur existe des deux cotes et n'est pas virtualise.
    """
    raw = _env("GISCO_HOME")
    if raw:
        return pathlib.Path(raw)
    return pathlib.Path.home() / ".gisco-mcp"


def _download_dir() -> pathlib.Path:
    return _local_root() / "downloads"


def _cache_db() -> pathlib.Path:
    raw = _env("GISCO_CACHE_DB")
    return pathlib.Path(raw) if raw else _local_root() / "gisco.sqlite"


def _export_dir() -> pathlib.Path:
    raw = _env("GISCO_EXPORT_DIR")
    return pathlib.Path(raw) if raw else _local_root() / "exports"


def _max_download_mb() -> int:
    try:
        return int(_env("GISCO_MAX_DOWNLOAD_MB", str(DEFAULT_MAX_DOWNLOAD_MB)))
    except ValueError:
        return DEFAULT_MAX_DOWNLOAD_MB


def _autosync_max_mb() -> int:
    try:
        return int(_env("GISCO_AUTOSYNC_MAX_MB", str(DEFAULT_AUTOSYNC_MAX_MB)))
    except ValueError:
        return DEFAULT_AUTOSYNC_MAX_MB


def _timeout_s() -> float:
    try:
        return float(_env("GISCO_TIMEOUT_S", str(DEFAULT_TIMEOUT_S)))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _log(message: str) -> None:
    """Journal sur stderr. JAMAIS stdout : le protocole MCP y parle JSON-RPC."""
    print(f"[gisco-mcp] {message}", file=sys.stderr, flush=True)


# httpx journalise chaque requete en INFO. Utile en mise au point, bruyant en
# service : une synchronisation ONSPD ecrirait 900 lignes de journal pour rien.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Catalogue embarque
# --------------------------------------------------------------------------

_CATALOGUE: dict[str, Any] | None = None


def _catalogue() -> dict[str, Any]:
    global _CATALOGUE
    if _CATALOGUE is None:
        try:
            _CATALOGUE = json.loads(CATALOGUE_FILE.read_text(encoding="utf-8"))
        except OSError as exc:
            raise GiscoError(
                f"Catalogue introuvable ({CATALOGUE_FILE}) : {exc}. Ce fichier "
                "fait partie du serveur, il est versionne a cote de server.py."
            ) from exc
        except ValueError as exc:
            raise GiscoError(f"Catalogue illisible ({CATALOGUE_FILE}) : {exc}") from exc
    return _CATALOGUE


def _layer_spec(couche: str) -> dict[str, Any]:
    key = _norm(couche).replace(" ", "_")
    alias = {
        "commune": "lau", "communes": "lau", "lau": "lau",
        "nuts": "nuts", "region": "nuts", "regions": "nuts",
        "code_postal": "pcode", "codes_postaux": "pcode", "pcode": "pcode",
        "postal": "pcode", "cp": "pcode",
        "ville": "urau", "villes": "urau", "urau": "urau",
        "pays": "countries", "countries": "countries", "cntr": "countries",
    }
    key = alias.get(key, key)
    spec = (_catalogue().get("couches") or {}).get(key)
    if spec is None:
        raise GiscoError(
            f"Couche inconnue : {couche}. Valeurs : "
            + ", ".join(sorted(_catalogue().get("couches") or {}))
            + ". gisco_catalogue() les decrit."
        )
    return {**spec, "nom": key}


def _uk_spec(nom: str) -> dict[str, Any]:
    spec = (_catalogue().get("couches_uk") or {}).get(nom)
    if spec is None:
        raise GiscoError(f"Couche britannique inconnue : {nom}")
    return {**spec, "nom": nom}


# --------------------------------------------------------------------------
# Petites fonctions de texte
# --------------------------------------------------------------------------

def _norm(text: Any) -> str:
    """Minuscules sans accent : la seule facon de retrouver « Dibër » en tapant
    « diber », et « Münster » en tapant « munster »."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKD", str(text))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def _unquote(value: Any) -> Any:
    """Retire les guillemets litteraux du jeu pcode.

    NUTS3_2024 y arrive comme '"NL366"', guillemets compris. Une jointure sur la
    valeur brute rend zero ligne SANS lever d'erreur - c'est exactement le genre
    de piege qui produit un fichier vide qu'on croit complet.
    """
    if isinstance(value, str):
        v = value.strip()
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            return v[1:-1]
        return v
    return value


def _clean_country(code: str) -> str:
    """Normalise un code pays vers la nomenclature Eurostat.

    GISCO ecrit EL pour la Grece et UK pour le Royaume-Uni. Un referentiel
    maison qui porte GR ou GB rate ces deux pays en silence : on traduit.
    """
    c = (code or "").strip().upper()
    return {"GR": "EL", "GB": "UK"}.get(c, c)


def _country_variants(code: str) -> tuple[str, ...]:
    """Les deux ecritures possibles d'un pays, pour interroger sans se tromper.

    Le versant britannique est charge sous 'UK' cote GISCO et sous 'GB' dans les
    identifiants ONS ; on interroge les deux plutot que d'imposer une graphie.
    """
    c = (code or "").strip().upper()
    if c in ("GB", "UK"):
        return ("UK", "GB")
    if c in ("GR", "EL"):
        return ("EL", "GR")
    return (c,) if c else ()


def _fmt_num(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return f"{value:.6g}"
    return str(value)


def _render(rows: list[tuple], cols: list[str], max_chars: int = RENDER_MAX_CHARS) -> str:
    """Rend un jeu de lignes en tableau texte, borne.

    Un rendu qui deborde est tronque, et il le DIT. Une liste coupee en silence
    se lit comme une liste complete.
    """
    if not rows:
        return "(aucune ligne)"
    widths = [len(c) for c in cols]
    body = []
    for row in rows:
        cells = [_fmt_num(v) for v in row]
        body.append(cells)
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], min(len(cell), 48))
    out = [" | ".join(c.ljust(widths[i])[:48] for i, c in enumerate(cols))]
    out.append("-+-".join("-" * w for w in widths))
    used = len(out[0]) + len(out[1])
    shown = 0
    for cells in body:
        line = " | ".join(cells[i].ljust(widths[i])[:48] for i in range(len(cols)))
        if used + len(line) > max_chars:
            out.append(
                f"... RENDU TRONQUE : {shown} ligne(s) affichees sur {len(body)}. "
                "Resserre les filtres, baisse 'limite', ou passe par gisco_export_csv."
            )
            break
        out.append(line)
        used += len(line) + 1
        shown += 1
    return "\n".join(out)


# --------------------------------------------------------------------------
# Reseau
# --------------------------------------------------------------------------

_CLIENT: httpx.Client | None = None


def _client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(
            timeout=_timeout_s(),
            follow_redirects=True,
            headers={
                "User-Agent": "vu-transport-gisco-mcp/1.0 (Vente-unique.com, Logistique & Transport)",
                "Accept": "application/json, application/geo+json, */*",
            },
        )
    return _CLIENT


def _get(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            res = _client().get(url, params=params)
        except httpx.HTTPError as exc:
            last = exc
            if attempt == RETRY_ATTEMPTS:
                break
            time.sleep(1.5 * attempt)
            continue
        if res.status_code in RETRY_STATUSES and attempt < RETRY_ATTEMPTS:
            time.sleep(1.5 * attempt)
            continue
        if res.status_code >= 400:
            raise GiscoError(
                f"{res.status_code} sur {url} : {res.text[:300]}"
            )
        return res
    raise GiscoError(
        f"Service injoignable apres {RETRY_ATTEMPTS} tentatives ({url}) : {last}. "
        "Verifie l'acces reseau, et le proxy si le poste est derriere un proxy."
    )


def _head_size(url: str) -> int:
    """Poids annonce d'un fichier, en octets. 0 si le service ne le dit pas."""
    try:
        res = _client().head(url)
        if res.status_code >= 400:
            raise GiscoError(f"{res.status_code} sur {url}")
        return int(res.headers.get("content-length") or 0)
    except httpx.HTTPError as exc:
        raise GiscoError(f"Fichier injoignable ({url}) : {exc}") from exc


def _download(url: str, target: pathlib.Path, expected: int = 0) -> int:
    """Telecharge en flux vers un fichier. Rend le nombre d'octets ecrits.

    Ecriture dans un fichier temporaire puis renommage : un telechargement
    interrompu ne laisse jamais un GeoPackage a moitie ecrit que la
    synchronisation suivante prendrait pour valide.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".partiel")
    written = 0
    step = 20 * 1024 * 1024
    next_log = step
    try:
        with _client().stream("GET", url) as res:
            if res.status_code >= 400:
                raise GiscoError(f"{res.status_code} sur {url}")
            with tmp.open("wb") as handle:
                for chunk in res.iter_bytes(1024 * 256):
                    handle.write(chunk)
                    written += len(chunk)
                    if written >= next_log:
                        _log(f"  {written // 1048576} Mo recus")
                        next_log += step
    except httpx.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise GiscoError(f"Telechargement interrompu ({url}) : {exc}") from exc
    if expected and written != expected:
        tmp.unlink(missing_ok=True)
        raise GiscoError(
            f"Telechargement incomplet : {written} octets recus sur {expected} "
            f"annonces ({url}). Relance la synchronisation."
        )
    tmp.replace(target)
    return written


def _arcgis_pages(
    url: str, params: dict[str, Any], max_rows: int = 0
) -> Iterator[list[dict[str, Any]]]:
    """Pagine une couche ArcGIS de l'ONS.

    On avance de l'effectif REELLEMENT rendu, pas d'un pas fixe : le service
    applique sa propre limite de transfert, et un pas fixe saute des lignes des
    qu'il rend moins que demande.
    """
    offset = 0
    total = 0
    while True:
        page = dict(params)
        page["resultOffset"] = str(offset)
        page["resultRecordCount"] = str(ARCGIS_PAGE)
        res = _get(url, page)
        try:
            payload = res.json()
        except ValueError as exc:
            raise GiscoError(f"Reponse ArcGIS illisible sur {url} : {exc}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise GiscoError(f"ArcGIS a refuse la requete : {payload['error']}")
        feats = payload.get("features") or []
        if not feats:
            return
        yield feats
        total += len(feats)
        offset += len(feats)
        if max_rows and total >= max_rows:
            return


# --------------------------------------------------------------------------
# Cache local
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS nuts (
    nuts_id TEXT PRIMARY KEY, levl_code INTEGER, cntr_code TEXT,
    name_latn TEXT, nuts_name TEXT, nom_norm TEXT,
    mount_type INTEGER, urbn_type INTEGER, coast_type INTEGER,
    source TEXT, edition TEXT,
    min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL, geom BLOB
);
CREATE INDEX IF NOT EXISTS ix_nuts_pays ON nuts(cntr_code, levl_code);
CREATE INDEX IF NOT EXISTS ix_nuts_nom  ON nuts(nom_norm);

CREATE TABLE IF NOT EXISTS lau (
    gisco_id TEXT PRIMARY KEY, cntr_code TEXT, lau_id TEXT,
    lau_name TEXT, nom_norm TEXT,
    pop INTEGER, pop_dens REAL, area_km2 REAL,
    source TEXT, edition TEXT,
    min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL, geom BLOB
);
CREATE INDEX IF NOT EXISTS ix_lau_pays ON lau(cntr_code);
CREATE INDEX IF NOT EXISTS ix_lau_nom  ON lau(nom_norm);

CREATE TABLE IF NOT EXISTS pcode (
    pc_cntr TEXT PRIMARY KEY, postcode TEXT, pc_norm TEXT, cntr_id TEXT,
    lau_name TEXT, lau_id TEXT, nuts3 TEXT, dgurba INTEGER,
    fua_id TEXT, city_id TEXT, lon REAL, lat REAL,
    source TEXT, edition TEXT
);
CREATE INDEX IF NOT EXISTS ix_pcode_pays  ON pcode(cntr_id, pc_norm);
CREATE INDEX IF NOT EXISTS ix_pcode_nuts3 ON pcode(nuts3);
CREATE INDEX IF NOT EXISTS ix_pcode_lau   ON pcode(lau_id);

CREATE TABLE IF NOT EXISTS urau (
    urau_code TEXT PRIMARY KEY, urau_catg TEXT, cntr_code TEXT,
    urau_name TEXT, nom_norm TEXT, city_cptl TEXT, fua_code TEXT,
    area_km2 REAL, nuts3 TEXT, source TEXT, edition TEXT,
    min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL, geom BLOB
);
CREATE INDEX IF NOT EXISTS ix_urau_pays ON urau(cntr_code, urau_catg);
CREATE INDEX IF NOT EXISTS ix_urau_nom  ON urau(nom_norm);

CREATE TABLE IF NOT EXISTS countries (
    cntr_id TEXT PRIMARY KEY, name_engl TEXT, name_fren TEXT, nom_norm TEXT,
    iso3_code TEXT, capt TEXT, eu_stat TEXT, efta_stat TEXT, cc_stat TEXT,
    source TEXT, edition TEXT,
    min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL, geom BLOB
);

CREATE TABLE IF NOT EXISTS couches (
    couche TEXT PRIMARY KEY, cible TEXT, edition TEXT, millesime TEXT, url TEXT,
    telecharge_le TEXT, lignes INTEGER, octets INTEGER, remarque TEXT
);

CREATE TABLE IF NOT EXISTS catalogue_distant (
    jeu TEXT PRIMARY KEY, editions TEXT, fichiers TEXT, releve_le TEXT
);
"""

# Les tables interrogeables et leur cle primaire.
TABLES = {
    "nuts": "nuts_id",
    "lau": "gisco_id",
    "pcode": "pc_cntr",
    "urau": "urau_code",
    "countries": "cntr_id",
    "couches": "couche",
}

# Colonnes ajoutees apres coup. Un cache cree par une version precedente du
# connecteur n'a pas 'millesime' : sans cette reprise, la premiere ecriture
# echoue et la seule issue serait de supprimer le cache - 250 Mo a
# retelecharger pour une colonne.
COLONNES_AJOUTEES = (("couches", "millesime", "TEXT"),)

# Tables ajoutees apres coup, meme raison : 'catalogue_distant' porte le releve
# des millesimes publies, et elle n'existe pas dans un cache monte avant que la
# decouverte des millesimes existe.
TABLES_AJOUTEES = ("catalogue_distant",)


# Le millesime d'une couche GISCO forme les quatre premiers caracteres de son
# edition : '2024_20M' pour les NUTS, '2025' pour les codes postaux. Les couches
# britanniques sont exclues - leur edition est un NOM DE SERVICE
# ('LAD_MAY_2025_UK_BGC_V2'), pas un millesime, et elles ne se comparent a
# aucune liste publiee.
SQL_MILLESIME_A_REPRENDRE = (
    "SELECT COUNT(*) FROM couches "
    "WHERE (millesime IS NULL OR millesime = '') "
    "  AND couche NOT LIKE 'uk_%' "
    "  AND substr(edition, 1, 4) GLOB '[0-9][0-9][0-9][0-9]'"
)


def _migre(conn: sqlite3.Connection) -> None:
    for table, colonne, type_sql in COLONNES_AJOUTEES:
        presentes = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if colonne not in presentes:
            _log(f"reprise du cache : ajout de {table}.{colonne}")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} {type_sql}")

    # Une colonne ajoutee vide n'est pas neutre ici : un millesime inconnu se
    # compare a ce qui est publie comme un RETARD, et gisco_maj proposerait de
    # retelecharger 270 Mo de couches qui sont deja au bon millesime. On le
    # remet donc depuis l'edition, et seulement quand il s'y lit comme une annee.
    repris = conn.execute(
        "UPDATE couches SET millesime = substr(edition, 1, 4) "
        "WHERE (millesime IS NULL OR millesime = '') "
        "  AND couche NOT LIKE 'uk_%' "
        "  AND substr(edition, 1, 4) GLOB '[0-9][0-9][0-9][0-9]'"
    ).rowcount
    if repris:
        _log(
            "reprise du cache : millesime retrouve depuis l'edition pour "
            f"{repris} couche(s)"
        )


def _reprise_en_attente(path: pathlib.Path) -> bool:
    """Le schema de ce cache est-il en retard sur celui du connecteur ?"""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return False
    try:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if any(t not in tables for t in TABLES_AJOUTEES):
            return True
        for table, colonne, _ in COLONNES_AJOUTEES:
            if table not in tables:
                continue
            presentes = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if colonne not in presentes:
                return True
        # La colonne peut etre la et vide : c'est l'etat d'un cache migre par
        # une version qui ajoutait la colonne sans la renseigner.
        if "couches" in tables:
            if conn.execute(SQL_MILLESIME_A_REPRENDRE).fetchone()[0]:
                return True
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()
    return False


def _reprise_si_besoin(path: pathlib.Path) -> None:
    """Rattrape le schema d'un cache existant AVANT une lecture.

    La reprise ne vivait que sur le chemin d'ECRITURE : un cache monte par une
    version precedente ne gagnait la colonne 'millesime' qu'a la synchronisation
    suivante. Entre les deux, toute lecture qui la demandait echouait sur « no
    such column » - a commencer par gisco_couches, c'est-a-dire la premiere
    chose qu'on lance pour savoir ou l'on en est. L'utilisateur n'avait alors
    qu'une issue apparente : supprimer le cache, soit 250 Mo a retelecharger
    pour une colonne vide.

    Une lecture repare donc le schema. Elle ne touche jamais aux donnees, et si
    le fichier n'est pas accessible en ecriture elle le DIT dans le journal
    plutot que de faire echouer la question.
    """
    if not _reprise_en_attente(path):
        return
    try:
        conn = _open_db()
        conn.close()
    except sqlite3.DatabaseError as exc:
        _log(f"reprise du schema du cache impossible ({exc}) : {path}")


def _open_db(readonly: bool = False) -> sqlite3.Connection:
    path = _cache_db()
    if readonly:
        if not path.is_file():
            raise GiscoError(
                f"Aucun cache local ({path}). Rien n'a encore ete synchronise : "
                "lance gisco_sync(couche=\"nuts\") - ou la couche qui t'interesse."
            )
        _reprise_si_besoin(path)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.executescript(SCHEMA)
        _migre(conn)
        conn.commit()
    conn.row_factory = None
    return conn


def _layer_state(couche: str) -> dict[str, Any] | None:
    path = _cache_db()
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT couche, cible, edition, millesime, url, telecharge_le, lignes, "
            "octets, remarque FROM couches WHERE couche = ?",
            (couche,),
        )
        row = cur.fetchone()
    except sqlite3.DatabaseError:
        # Cache ecrit par une version anterieure, sans la colonne 'millesime' :
        # on relit sans elle plutot que de faire echouer toute lecture.
        try:
            row = conn.execute(
                "SELECT couche, cible, edition, NULL, url, telecharge_le, lignes, "
                "octets, remarque FROM couches WHERE couche = ?",
                (couche,),
            ).fetchone()
        except sqlite3.DatabaseError:
            return None
    finally:
        conn.close()
    if not row:
        return None
    keys = ("couche", "cible", "edition", "millesime", "url", "telecharge_le",
            "lignes", "octets", "remarque")
    return dict(zip(keys, row))


def _provenance(couches: Iterable[str]) -> str:
    """La ligne d'en-tete qui dit d'ou sort la reponse.

    Elle n'est pas decorative. Une reponse geographique sans millesime ni date
    de rapatriement est invérifiable : les communes fusionnent, les codes
    postaux naissent, et un decoupage de 2021 ne repond pas comme celui de 2024.
    """
    bits = []
    for nom in couches:
        state = _layer_state(nom)
        if state:
            bits.append(
                f"{nom} [{state['edition']}, {state['lignes']} lignes, "
                f"rapatrie le {str(state['telecharge_le'])[:10]}]"
            )
        else:
            bits.append(f"{nom} [absent du cache]")
    return "Source : " + " ; ".join(bits)


# --------------------------------------------------------------------------
# Synchronisation - GISCO
# --------------------------------------------------------------------------

_SYNC_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Decouverte de ce que GISCO publie REELLEMENT
#
# Pourquoi cette section existe. Les millesimes et les noms de fichiers de GISCO
# changent : un jeu gagne une edition, un fichier change de resolution, une
# colonne change de suffixe. Une liste ecrite en dur vieillit, et le jour ou
# elle vieillit le connecteur refuse un millesime qui existe pourtant - ce qui
# est le pire des deux mondes, puisque l'erreur accuse Eurostat.
#
# On interroge donc l'index d'Eurostat : datasets.json pour les editions, puis
# le fichier d'index de l'edition pour la liste des GeoPackage. Le resultat est
# garde dans le cache local avec une duree de validite, pour ne pas rappeler
# Eurostat a chaque question.
# --------------------------------------------------------------------------

def _ttl_catalogue_jours() -> float:
    try:
        return float(_env("GISCO_CATALOGUE_TTL_JOURS", "7"))
    except ValueError:
        return 7.0


def _releve_lu(jeu: str) -> dict[str, Any] | None:
    """Le dernier releve de l'index d'Eurostat pour ce jeu, s'il est encore frais."""
    path = _cache_db()
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return None
    try:
        row = conn.execute(
            "SELECT editions, fichiers, releve_le FROM catalogue_distant WHERE jeu = ?",
            (jeu,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    if not row:
        return None
    try:
        releve = dt.datetime.fromisoformat(row[2])
    except (TypeError, ValueError):
        return None
    age = (dt.datetime.now() - releve).total_seconds() / 86400.0
    return {
        "editions": json.loads(row[0] or "{}"),
        "fichiers": json.loads(row[1] or "{}"),
        "releve_le": row[2],
        "age_jours": age,
        "frais": age <= _ttl_catalogue_jours(),
    }


def _releve_ecrit(jeu: str, editions: dict[str, str], fichiers: dict[str, list[str]]) -> None:
    conn = _open_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO catalogue_distant (jeu, editions, fichiers, releve_le) "
            "VALUES (?,?,?,?)",
            (
                jeu,
                json.dumps(editions, ensure_ascii=False),
                json.dumps(fichiers, ensure_ascii=False),
                dt.datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _releve(jeu: str, rafraichir: bool = False) -> dict[str, Any]:
    """Ce que GISCO publie pour ce jeu : editions et GeoPackage de chacune.

    Rend un dictionnaire avec 'editions' (millesime -> nom du fichier d'index),
    'fichiers' (millesime -> liste des GeoPackage) et 'origine', qui dit d'ou
    sort l'information : 'reseau', 'cache local' ou 'catalogue embarque'.

    Le repli est explicite et progressif. Reseau d'abord ; a defaut le dernier
    releve local, meme perime, en le disant ; a defaut seulement, la liste
    embarquee. On ne renonce jamais en silence : une reponse construite sur une
    liste vieille de deux ans doit se voir.
    """
    lu = _releve_lu(jeu)
    if lu and lu["frais"] and not rafraichir:
        return {**lu, "origine": "cache local"}

    racine = _catalogue()["racine"]
    try:
        payload = _get(f"{racine}/distribution/v2/{jeu}/datasets.json").json()
    except (GiscoError, ValueError) as exc:
        if lu:
            return {
                **lu,
                "origine": f"cache local PERIME ({lu['age_jours']:.0f} jours) - "
                           f"Eurostat injoignable : {exc}",
            }
        spec = next(
            (s for s in (_catalogue().get("couches") or {}).values() if s.get("jeu") == jeu),
            {},
        )
        annees = spec.get("annees_connues") or []
        return {
            "editions": {a: "" for a in annees},
            "fichiers": {},
            "releve_le": "",
            "age_jours": None,
            "frais": False,
            "origine": f"catalogue EMBARQUE - Eurostat injoignable : {exc}",
        }

    # Les clefs sont de la forme "<jeu>-<annee>". On garde l'annee et le nom du
    # fichier d'index, que l'on ne reconstruit pas : datasets.json le donne.
    editions: dict[str, str] = {}
    for clef, entree in (payload or {}).items():
        annee = str(clef).rsplit("-", 1)[-1]
        if not annee.isdigit():
            continue
        editions[annee] = (entree or {}).get("files") or f"{jeu}-{annee}-files.json"

    fichiers = dict((lu or {}).get("fichiers") or {})
    if editions:
        # On ne rapatrie l'index de fichiers que pour le dernier millesime : les
        # autres se liront a la demande. Un index pese quelques kilo-octets,
        # mais il y en a jusqu'a quatorze par jeu.
        derniere = max(editions, key=lambda a: int(a))
        try:
            idx = _get(f"{racine}/distribution/v2/{jeu}/{editions[derniere]}").json()
            gpkg = (idx or {}).get("gpkg") or {}
            fichiers[derniere] = sorted(gpkg) if isinstance(gpkg, dict) else sorted(gpkg)
        except (GiscoError, ValueError):
            pass

    try:
        _releve_ecrit(jeu, editions, fichiers)
    except sqlite3.DatabaseError:
        pass
    return {
        "editions": editions,
        "fichiers": fichiers,
        "releve_le": dt.datetime.now().isoformat(timespec="seconds"),
        "age_jours": 0.0,
        "frais": True,
        "origine": "reseau",
    }


def _millesimes(jeu: str, rafraichir: bool = False) -> list[str]:
    """Les millesimes publies, du plus recent au plus ancien."""
    rel = _releve(jeu, rafraichir)
    return sorted(rel["editions"], key=lambda a: int(a), reverse=True)


def _derniere_edition(jeu: str, rafraichir: bool = False) -> str:
    annees = _millesimes(jeu, rafraichir)
    if not annees:
        raise GiscoError(
            f"Aucun millesime trouve pour le jeu '{jeu}', ni chez Eurostat ni "
            "dans le catalogue embarque. Verifie l'acces reseau avec gisco_doctor."
        )
    return annees[0]


def _gpkg_publies(jeu: str, annee: str, rafraichir: bool = False) -> list[str]:
    """Les GeoPackage publies pour ce millesime. Liste vide si on ne sait pas."""
    rel = _releve(jeu, rafraichir)
    connus = rel["fichiers"].get(annee)
    if connus is not None:
        return connus
    index = rel["editions"].get(annee)
    if not index:
        return []
    try:
        idx = _get(f"{_catalogue()['racine']}/distribution/v2/{jeu}/{index}").json()
    except (GiscoError, ValueError):
        return []
    gpkg = (idx or {}).get("gpkg") or {}
    noms = sorted(gpkg) if isinstance(gpkg, dict) else sorted(gpkg)
    rel["fichiers"][annee] = noms
    try:
        _releve_ecrit(jeu, rel["editions"], rel["fichiers"])
    except sqlite3.DatabaseError:
        pass
    return noms


def _choisir_fichier(
    spec: dict[str, Any], annee: str, resolution: str, niveau: str
) -> tuple[str, str, str, str]:
    """Choisit le GeoPackage a rapatrier. Rend (fichier, annee, resolution, origine).

    L'ordre de travail est : quel millesime, puis quel fichier dans ce
    millesime. Le nom est CONFRONTE a la liste publiee quand on l'a, plutot que
    seulement construit depuis un gabarit : c'est ce qui permet de suivre un
    renommage sans toucher au code, et de dire ce qui existe reellement quand
    la demande ne correspond a rien.
    """
    jeu = spec.get("jeu") or spec["nom"]
    demande = (annee or "").strip().lower()
    auto = demande in ("", "derniere", "dernière", "latest", "auto", "max")
    if auto:
        annee = _derniere_edition(jeu)
        origine_annee = "dernier millesime publie"
    else:
        annee = (annee or "").strip()
        if not annee.isdigit():
            raise GiscoError(
                f"Millesime illisible : {annee!r}. Donne une annee a quatre "
                "chiffres, ou laisse vide pour prendre le dernier publie."
            )
        publies = _millesimes(jeu)
        if publies and annee not in publies:
            raise GiscoError(
                f"Millesime {annee} non publie pour le jeu '{jeu}'. "
                f"Publies : {', '.join(publies)}."
            )
        origine_annee = "millesime demande"

    publies = _gpkg_publies(jeu, annee)
    niveau = (niveau or "").strip()
    gabarit = spec["gabarit_niveau"] if (niveau and spec.get("gabarit_niveau")) else spec["gabarit"]
    motif = spec.get("motif_fichier_niveau" if niveau else "motif_fichier")

    # Quelles resolutions ce millesime propose-t-il vraiment ?
    dispo: list[str] = []
    if publies and motif:
        rx = re.compile(motif)
        for nom in publies:
            m = rx.match(nom)
            if not m or m.groupdict().get("annee") != annee:
                continue
            if niveau and m.groupdict().get("niveau") != niveau:
                continue
            dispo.append((m.groupdict().get("resolution") or "").upper())
        dispo = sorted(set(x for x in dispo if x))

    resolution = (resolution or "").strip().upper()
    if not resolution:
        preferences = [r.upper() for r in (spec.get("resolution_preferee") or [])]
        choix = next((r for r in preferences if r in dispo), "")
        if not choix and dispo:
            # Aucune preference n'existe dans ce millesime : on prend la plus
            # grossiere disponible, la plus legere, et on le dit.
            choix = dispo[-1]
        resolution = choix or (preferences[0] if preferences else "")
    elif dispo and resolution not in dispo:
        raise GiscoError(
            f"Resolution {resolution} non publiee pour {jeu} {annee}. "
            f"Publiees : {', '.join(dispo) or '(aucune - ce jeu n a pas de resolution)'}."
        )

    fichier = (
        gabarit.replace("{annee}", annee)
        .replace("{resolution}", resolution)
        .replace("{niveau}", niveau)
    )
    if publies and fichier not in publies:
        # Le gabarit ne colle pas : on cherche dans ce qui est reellement la.
        candidats = []
        if motif:
            rx = re.compile(motif)
            for nom in publies:
                m = rx.match(nom)
                if not m or m.groupdict().get("annee") != annee:
                    continue
                if niveau and m.groupdict().get("niveau") != niveau:
                    continue
                if resolution and (m.groupdict().get("resolution") or "").upper() != resolution:
                    continue
                candidats.append(nom)
        if not candidats:
            apercu = ", ".join(publies[:12]) + (" ..." if len(publies) > 12 else "")
            raise GiscoError(
                f"Aucun GeoPackage ne correspond a {jeu} {annee}"
                + (f" resolution {resolution}" if resolution else "")
                + (f" niveau {niveau}" if niveau else "")
                + f".\nFichiers publies pour ce millesime : {apercu}\n"
                "Le nommage a peut-etre change chez Eurostat : le motif est dans "
                "catalogue.json, cle 'motif_fichier'."
            )
        fichier = candidats[0]

    origine = origine_annee + (
        f", resolution choisie parmi celles publiees ({', '.join(dispo)})" if dispo else ""
    )
    return fichier, annee, resolution, origine


def _gisco_url(jeu: str, fichier: str) -> str:
    return f"{_catalogue()['racine']}/distribution/v2/{jeu}/gpkg/{fichier}"


def _resolve_columns(
    spec: dict[str, Any], annee: str, reelles: list[str]
) -> tuple[dict[str, str], list[str]]:
    """Fait correspondre les colonnes voulues aux colonnes REELLES du fichier.

    Trois passes, dans cet ordre : le nom tel quel ; le nom avec le millesime
    substitue ; enfin un motif, pour les colonnes dont le suffixe d'annee ne se
    devine pas. C'est la troisieme passe qui compte : la population s'appelle
    POP_2024 ici et POP_2023 la, et le NUTS3 du jeu des codes postaux porte
    2024 dans le fichier 2025. Calculer ces noms revient a parier ; les lire
    ferme la question.

    Rend aussi la liste des colonnes voulues et INTROUVABLES, pour que la
    synchronisation le dise au lieu de charger des colonnes vides.
    """
    presentes = {c.upper(): c for c in reelles}
    motifs = spec.get("motifs_colonnes") or {}
    out: dict[str, str] = {}
    manquantes: list[str] = []
    for cible, source in (spec.get("colonnes") or {}).items():
        candidat = source.replace("{annee}", annee)
        trouve = presentes.get(candidat.upper()) or presentes.get(source.upper())
        if not trouve and cible in motifs:
            rx = re.compile(motifs[cible], re.IGNORECASE)
            # Le plus recent d'abord : POP_2024 avant POP_2020 si les deux sont la.
            for nom in sorted(reelles, reverse=True):
                if rx.match(nom):
                    trouve = nom
                    break
        if trouve:
            out[cible] = trouve
        else:
            out[cible] = candidat
            manquantes.append(f"{cible} (cherchee sous {candidat})")
    return out, manquantes


def _bbox_of(feature: dict[str, Any]) -> tuple[float | None, ...]:
    env = feature.get("__env")
    if env:
        return env
    wkb = feature.get("__wkb")
    if not wkb:
        return (None, None, None, None)
    try:
        return geo.bbox(geo.wkb_to_geojson(wkb))
    except geo.GeoError:
        return (None, None, None, None)


def _sync_gisco(
    couche: str, annee: str, resolution: str, niveau: str, force: bool
) -> str:
    spec = _layer_spec(couche)
    nom = spec["nom"]
    jeu = spec.get("jeu") or nom
    rel = _releve(jeu)
    nom_fichier, annee, resolution, origine = _choisir_fichier(spec, annee, resolution, niveau)
    url = _gisco_url(jeu, nom_fichier)
    fichier = _download_dir() / nom_fichier
    edition = "_".join(x for x in (annee, resolution, f"LEVL{niveau}" if niveau else "") if x)

    taille = _head_size(url)
    plafond = _max_download_mb() * 1048576
    if taille and taille > plafond and not force:
        raise GiscoError(
            f"{nom_fichier} pese {taille / 1048576:.0f} Mo, au-dela du "
            f"plafond de {_max_download_mb()} Mo. Relance avec force=True si "
            "c'est voulu, ou releve GISCO_MAX_DOWNLOAD_MB."
        )

    if force or not fichier.is_file() or (taille and fichier.stat().st_size != taille):
        _log(f"telechargement {url} ({taille / 1048576:.1f} Mo)")
        octets = _download(url, fichier, taille)
    else:
        octets = fichier.stat().st_size
        _log(f"fichier deja present, reutilise : {fichier.name}")

    # Les colonnes sont lues dans le fichier, pas calculees depuis le millesime.
    reelles = geo.gpkg_columns(fichier)
    cols, manquantes = _resolve_columns(spec, annee, reelles)

    conn = _open_db()
    try:
        lignes = _charger_gpkg(conn, nom, spec, cols, fichier, edition)
        conn.execute(
            "INSERT OR REPLACE INTO couches "
            "(couche, cible, edition, millesime, url, telecharge_le, lignes, octets, remarque) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                nom, spec["table"], edition, annee, url,
                dt.datetime.now().isoformat(timespec="seconds"),
                lignes, octets, spec.get("remarque", ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    publies = sorted(rel["editions"], key=lambda a: int(a), reverse=True)
    out = [
        f"Couche '{nom}' synchronisee.",
        f"  millesime : {annee}" + (f" ({origine})" if origine else ""),
        f"  edition   : {edition}",
        f"  publies   : {', '.join(publies) or '(inconnus)'}",
        f"  catalogue : {rel['origine']}",
        f"  source    : {url}",
        f"  fichier   : {fichier} ({octets / 1048576:.1f} Mo)",
        f"  chargees  : {lignes} ligne(s) dans la table '{spec['table']}'",
        f"  cache     : {_cache_db()}",
    ]
    if manquantes:
        out.append(
            "\nCOLONNES INTROUVABLES dans ce millesime, laissees vides : "
            + " ; ".join(manquantes)
            + f"\nColonnes reellement presentes : {', '.join(reelles)}"
            + "\nCe n'est pas une erreur de configuration : Eurostat ne publie pas "
            "les memes attributs d'un millesime a l'autre. Si la colonne compte "
            "pour la question posee, essaie le millesime precedent."
        )
    if publies and annee != publies[0]:
        out.append(
            f"\nCe n'est PAS le dernier millesime publie ({publies[0]}). "
            "C'est peut-etre voulu - lis le piege "
            "'le_dernier_millesime_n_est_pas_le_plus_complet' dans gisco_catalogue()."
        )
    return "\n".join(out)


def _charger_gpkg(
    conn: sqlite3.Connection,
    nom: str,
    spec: dict[str, Any],
    cols: dict[str, str],
    fichier: pathlib.Path,
    edition: str,
) -> int:
    table = spec["table"]
    # On efface d'abord ce que GISCO avait pose : reprendre un millesime ne doit
    # pas laisser les unites disparues du precedent. Les lignes ONS, elles, ne
    # sont pas touchees - elles ont leur propre synchronisation.
    conn.execute(f"DELETE FROM {table} WHERE source = 'GISCO'")

    if table == "pcode":
        rows = _lignes_pcode(fichier, cols, edition)
        sql = (
            "INSERT OR REPLACE INTO pcode (pc_cntr, postcode, pc_norm, cntr_id, "
            "lau_name, lau_id, nuts3, dgurba, fua_id, city_id, lon, lat, source, edition) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
    elif table == "nuts":
        rows = _lignes_nuts(fichier, cols, edition)
        sql = (
            "INSERT OR REPLACE INTO nuts (nuts_id, levl_code, cntr_code, name_latn, "
            "nuts_name, nom_norm, mount_type, urbn_type, coast_type, source, edition, "
            "min_lon, min_lat, max_lon, max_lat, geom) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
    elif table == "lau":
        rows = _lignes_lau(fichier, cols, edition)
        sql = (
            "INSERT OR REPLACE INTO lau (gisco_id, cntr_code, lau_id, lau_name, nom_norm, "
            "pop, pop_dens, area_km2, source, edition, min_lon, min_lat, max_lon, max_lat, geom) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
    elif table == "urau":
        rows = _lignes_urau(fichier, cols, edition)
        sql = (
            "INSERT OR REPLACE INTO urau (urau_code, urau_catg, cntr_code, urau_name, "
            "nom_norm, city_cptl, fua_code, area_km2, nuts3, source, edition, "
            "min_lon, min_lat, max_lon, max_lat, geom) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
    elif table == "countries":
        rows = _lignes_countries(fichier, cols, edition)
        sql = (
            "INSERT OR REPLACE INTO countries (cntr_id, name_engl, name_fren, nom_norm, "
            "iso3_code, capt, eu_stat, efta_stat, cc_stat, source, edition, "
            "min_lon, min_lat, max_lon, max_lat, geom) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
    else:
        raise GiscoError(f"Chargement non prevu pour la table {table}")

    total = 0
    lot: list[tuple] = []
    for row in rows:
        lot.append(row)
        if len(lot) >= BATCH:
            conn.executemany(sql, lot)
            total += len(lot)
            lot.clear()
            if total % (BATCH * 20) == 0:
                _log(f"  {total} lignes chargees")
    if lot:
        conn.executemany(sql, lot)
        total += len(lot)
    return total


def _val(feature: dict[str, Any], cols: dict[str, str], cible: str) -> Any:
    return feature.get(cols.get(cible, ""))


def _lignes_nuts(fichier, cols, edition) -> Iterator[tuple]:
    for f in geo.iter_gpkg(fichier):
        b = _bbox_of(f)
        nom = _val(f, cols, "name_latn") or _val(f, cols, "nuts_name")
        yield (
            _val(f, cols, "nuts_id"), _val(f, cols, "levl_code"), _val(f, cols, "cntr_code"),
            _val(f, cols, "name_latn"), _val(f, cols, "nuts_name"), _norm(nom),
            _val(f, cols, "mount_type"), _val(f, cols, "urbn_type"), _val(f, cols, "coast_type"),
            "GISCO", edition, b[0], b[1], b[2], b[3], f.get("__wkb"),
        )


def _lignes_lau(fichier, cols, edition) -> Iterator[tuple]:
    for f in geo.iter_gpkg(fichier):
        b = _bbox_of(f)
        gid = _val(f, cols, "gisco_id") or ""
        # LAU_ID existe jusqu'au millesime 2023 et a disparu en 2024. Quand la
        # colonne manque, le code national est la partie de GISCO_ID qui suit
        # le trait de soulignement (AL_AL141 -> AL141).
        lau_id = _val(f, cols, "lau_id")
        if not lau_id:
            lau_id = gid.split("_", 1)[1] if "_" in gid else gid
        nom = _val(f, cols, "lau_name")
        pop = _val(f, cols, "pop")
        yield (
            gid, _val(f, cols, "cntr_code"), lau_id, nom, _norm(nom),
            int(pop) if pop not in (None, "") else None,
            _val(f, cols, "pop_dens"), _val(f, cols, "area_km2"),
            "GISCO", edition, b[0], b[1], b[2], b[3], f.get("__wkb"),
        )


def _lignes_urau(fichier, cols, edition) -> Iterator[tuple]:
    for f in geo.iter_gpkg(fichier):
        b = _bbox_of(f)
        nom = _val(f, cols, "urau_name")
        yield (
            _val(f, cols, "urau_code"), _val(f, cols, "urau_catg"), _val(f, cols, "cntr_code"),
            nom, _norm(nom), _val(f, cols, "city_cptl"), _val(f, cols, "fua_code"),
            _val(f, cols, "area_km2"), _unquote(_val(f, cols, "nuts3")),
            "GISCO", edition, b[0], b[1], b[2], b[3], f.get("__wkb"),
        )


def _lignes_countries(fichier, cols, edition) -> Iterator[tuple]:
    for f in geo.iter_gpkg(fichier):
        b = _bbox_of(f)
        nom = _val(f, cols, "name_fren") or _val(f, cols, "name_engl")
        yield (
            _val(f, cols, "cntr_id"), _val(f, cols, "name_engl"), _val(f, cols, "name_fren"),
            _norm(nom), _val(f, cols, "iso3_code"), _val(f, cols, "capt"),
            _val(f, cols, "eu_stat"), _val(f, cols, "efta_stat"), _val(f, cols, "cc_stat"),
            "GISCO", edition, b[0], b[1], b[2], b[3], f.get("__wkb"),
        )


def _lignes_pcode(fichier, cols, edition) -> Iterator[tuple]:
    for f in geo.iter_gpkg(fichier):
        lon = lat = None
        wkb = f.get("__wkb")
        if wkb:
            try:
                g = geo.wkb_to_geojson(wkb)
                if g.get("type") == "Point":
                    lon, lat = float(g["coordinates"][0]), float(g["coordinates"][1])
                else:
                    lon, lat = geo.centroid(g)
            except (geo.GeoError, ValueError, IndexError, TypeError):
                lon = lat = None
        code = _unquote(_val(f, cols, "postcode"))
        pays = _val(f, cols, "cntr_id")
        pc_cntr = _unquote(_val(f, cols, "pc_cntr")) or f"{pays}_{code}"
        dg = _val(f, cols, "dgurba")
        yield (
            pc_cntr, code, _norm(code).replace(" ", ""), pays,
            _val(f, cols, "lau_name"), _unquote(_val(f, cols, "lau_id")),
            _unquote(_val(f, cols, "nuts3")),
            int(dg) if str(dg or "").strip().isdigit() else None,
            _unquote(_val(f, cols, "fua_id")), _unquote(_val(f, cols, "city_id")),
            lon, lat, "GISCO", edition,
        )


# --------------------------------------------------------------------------
# Synchronisation - Royaume-Uni (ONS)
# --------------------------------------------------------------------------

def _sync_uk(couche: str, confirmer: bool) -> str:
    spec = _uk_spec(couche)
    racine = _catalogue()["couches_uk"]["racine"]
    url = racine + spec["chemin"]
    edition = spec["chemin"].strip("/").split("/")[0]

    if couche == "uk_onspd" and not confirmer:
        raise GiscoError(
            "ONSPD compte plus de 1,8 million de codes postaux actifs, ramenes "
            "par pages de 2 000 : compte une bonne dizaine de minutes et environ "
            "900 appels. Relance avec confirmer=True si c'est bien ce que tu veux. "
            "Pour une question ponctuelle sur un code postal britannique, "
            "gisco_geocoder repond sans rien rapatrier."
        )

    conn = _open_db()
    try:
        if couche == "uk_lad":
            lignes = _charger_uk_lad(conn, url, spec, edition)
        elif couche == "uk_itl3":
            lignes = _charger_uk_itl3(conn, url, spec, edition)
        elif couche == "uk_onspd":
            lignes = _charger_uk_onspd(conn, url, spec, edition, racine)
        else:
            raise GiscoError(f"Chargement non prevu pour {couche}")
        conn.execute(
            "INSERT OR REPLACE INTO couches "
            "(couche, cible, edition, url, telecharge_le, lignes, octets, remarque) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                couche, spec["table"], edition, url,
                dt.datetime.now().isoformat(timespec="seconds"),
                lignes, 0, spec.get("remarque", ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return (
        f"Couche '{couche}' synchronisee depuis l'ONS.\n"
        f"  edition  : {edition}\n"
        f"  source   : {url}\n"
        f"  chargees : {lignes} ligne(s) dans la table '{spec['table']}' avec source='ONS'"
    )


def _params_uk(spec: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "where": spec.get("filtre_actifs") or "1=1",
        "outFields": spec["champs"],
        "f": "geojson" if spec.get("geometrie") else "json",
    }
    if spec.get("geometrie"):
        params["outSR"] = "4326"
    else:
        params["returnGeometry"] = "false"
    return params


def _charger_uk_lad(conn, url, spec, edition) -> int:
    conn.execute("DELETE FROM lau WHERE source = 'ONS'")
    sql = (
        "INSERT OR REPLACE INTO lau (gisco_id, cntr_code, lau_id, lau_name, nom_norm, "
        "pop, pop_dens, area_km2, source, edition, min_lon, min_lat, max_lon, max_lat, geom) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    total = 0
    for page in _arcgis_pages(url, _params_uk(spec)):
        lot = []
        for feat in page:
            props = feat.get("properties") or feat.get("attributes") or {}
            geom = feat.get("geometry")
            wkb = bb = None
            if geom:
                try:
                    wkb = geo.geojson_to_wkb(geom)
                    bb = geo.bbox(geom)
                except geo.GeoError:
                    wkb = bb = None
            bb = bb or (None, None, None, None)
            code = props.get("LAD25CD") or ""
            nom = props.get("LAD25NM")
            aire = props.get("Shape__Area")
            lot.append((
                f"GB_{code}", "UK", code, nom, _norm(nom),
                # La population n'est PAS dans la couche ONS : elle reste vide,
                # et c'est un manque assume, pas un zero.
                None, None,
                # Shape__Area est en m2 dans la projection britannique (27700).
                (float(aire) / 1_000_000.0) if aire not in (None, "") else None,
                "ONS", edition, bb[0], bb[1], bb[2], bb[3], wkb,
            ))
        conn.executemany(sql, lot)
        total += len(lot)
        _log(f"  uk_lad : {total} lignes")
    return total


def _charger_uk_itl3(conn, url, spec, edition) -> int:
    conn.execute("DELETE FROM nuts WHERE source = 'ONS'")
    sql = (
        "INSERT OR REPLACE INTO nuts (nuts_id, levl_code, cntr_code, name_latn, nuts_name, "
        "nom_norm, mount_type, urbn_type, coast_type, source, edition, "
        "min_lon, min_lat, max_lon, max_lat, geom) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    total = 0
    for page in _arcgis_pages(url, _params_uk(spec)):
        lot = []
        for feat in page:
            props = feat.get("properties") or feat.get("attributes") or {}
            geom = feat.get("geometry")
            wkb = bb = None
            if geom:
                try:
                    wkb = geo.geojson_to_wkb(geom)
                    bb = geo.bbox(geom)
                except geo.GeoError:
                    wkb = bb = None
            bb = bb or (None, None, None, None)
            code = props.get("ITL325CD")
            nom = props.get("ITL325NM")
            lot.append((
                code, 3, "UK", nom, nom, _norm(nom), None, None, None,
                "ONS", edition, bb[0], bb[1], bb[2], bb[3], wkb,
            ))
        conn.executemany(sql, lot)
        total += len(lot)
        _log(f"  uk_itl3 : {total} lignes")
    return total


def _charger_uk_onspd(conn, url, spec, edition, racine) -> int:
    # La correspondance LAD -> ITL3, dedupliquee AVANT la jointure : sans cela,
    # la jointure multiplie les lignes et l'export sort des doublons qui ont
    # l'air d'etre des codes postaux.
    lookup_spec = _uk_spec("uk_lookup")
    lookup: dict[str, tuple[str, str]] = {}
    for page in _arcgis_pages(racine + lookup_spec["chemin"], _params_uk(lookup_spec)):
        for feat in page:
            a = feat.get("attributes") or feat.get("properties") or {}
            code = a.get("LAD25CD")
            if code and code not in lookup:
                lookup[code] = (a.get("LAD25NM") or "", a.get("ITL325CD") or "")
    _log(f"  correspondance LAD->ITL3 : {len(lookup)} districts")

    conn.execute("DELETE FROM pcode WHERE source = 'ONS'")
    sql = (
        "INSERT OR REPLACE INTO pcode (pc_cntr, postcode, pc_norm, cntr_id, lau_name, "
        "lau_id, nuts3, dgurba, fua_id, city_id, lon, lat, source, edition) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    total = 0
    for page in _arcgis_pages(url, _params_uk(spec)):
        lot = []
        for feat in page:
            a = feat.get("attributes") or feat.get("properties") or {}
            pcd = (a.get("pcd8") or "").strip()
            lad = a.get("lad25cd") or ""
            nom_lad, itl3 = lookup.get(lad, ("", ""))
            lat, lon = a.get("lat"), a.get("long")
            # L'ONS code les positions inconnues par 99.999999 : ce n'est pas
            # une coordonnee, c'est une absence.
            if lat in (None, "") or float(lat) > 90 or float(lat) < -90:
                lat = lon = None
            lot.append((
                f"GB_{pcd.replace(' ', '')}", pcd, _norm(pcd).replace(" ", ""), "UK",
                nom_lad, f"GB_{lad}" if lad else None, itl3 or None,
                None, None, None,
                float(lon) if lon not in (None, "") else None,
                float(lat) if lat not in (None, "") else None,
                "ONS", edition,
            ))
        conn.executemany(sql, lot)
        total += len(lot)
        if total % 100_000 < ARCGIS_PAGE:
            _log(f"  uk_onspd : {total} lignes")
    return total


# --------------------------------------------------------------------------
# Acces au cache, avec synchronisation automatique des couches legeres
# --------------------------------------------------------------------------

def _besoin(couche: str) -> None:
    """Verifie qu'une couche est en cache. Rapatrie toute seule si elle est legere.

    La synchronisation ECRIT sur le disque : au-dela de quelques megaoctets,
    c'est un geste qu'on demande, pas un effet de bord d'une question. En
    dessous - les NUTS au 20M pesent 1,5 Mo, les pays 1 Mo - l'exiger ne
    protegerait personne et ferait echouer la premiere question de chacun.
    """
    if _layer_state(couche):
        return
    spec = _layer_spec(couche)
    jeu = spec.get("jeu") or spec["nom"]
    try:
        nom_fichier, _, _, _ = _choisir_fichier(spec, "", "", "")
        taille = _head_size(_gisco_url(jeu, nom_fichier))
    except GiscoError:
        taille = 0
    if taille and taille <= _autosync_max_mb() * 1048576:
        _log(f"couche '{couche}' absente et legere ({taille / 1048576:.1f} Mo) : rapatriement")
        with _SYNC_LOCK:
            if not _layer_state(couche):
                _sync_gisco(couche, "", "", "", False)
        return
    poids = f"{taille / 1048576:.0f} Mo" if taille else "poids inconnu"
    raise GiscoError(
        f"La couche '{couche}' n'est pas dans le cache local, et elle est trop "
        f"lourde ({poids}) pour etre rapatriee sans qu'on le demande.\n"
        f"Lance : gisco_sync(couche=\"{couche}\")\n"
        "C'est une fois pour toutes : les lectures suivantes sont hors ligne."
    )


def _guard(fn: Callable) -> Callable:
    """Transforme une erreur attendue en message lisible plutot qu'en trace."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (GiscoError, geo.GeoError) as exc:
            return f"ERREUR : {exc}"
        except sqlite3.DatabaseError as exc:
            return (
                f"ERREUR : le cache local a refuse la lecture ({exc}). "
                f"Fichier : {_cache_db()}. Si le message parle de base corrompue, "
                "supprime ce fichier et relance gisco_sync."
            )

    return wrapper


def _select(
    table: str,
    colonnes: list[str],
    where: list[str],
    params: list[Any],
    ordre: str,
    limite: int,
) -> tuple[list[tuple], list[str], int]:
    conn = _open_db(readonly=True)
    try:
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) FROM {table}{clause}", params
        ).fetchone()[0]
        sql = f"SELECT {', '.join(colonnes)} FROM {table}{clause}"
        if ordre:
            sql += f" ORDER BY {ordre}"
        sql += " LIMIT ?"
        rows = conn.execute(sql, [*params, int(limite)]).fetchall()
    finally:
        conn.close()
    return rows, colonnes, total


def _entete(total: int, rendu: int, limite: int, couches: list[str], filtres: str) -> str:
    lignes = [_provenance(couches)]
    if filtres:
        lignes.append(f"Filtres : {filtres}")
    if total > rendu:
        lignes.append(
            f"{total} ligne(s) correspondent, {rendu} affichees (limite={limite}). "
            "Ce n'est PAS un total complet : releve 'limite', resserre les "
            "filtres, ou passe par gisco_export_csv pour le perimetre entier."
        )
    else:
        lignes.append(f"{total} ligne(s), toutes affichees.")
    return "\n".join(lignes)


# --------------------------------------------------------------------------
# Serveur MCP
# --------------------------------------------------------------------------

SERVER_INSTRUCTIONS = """\
Connecteur en LECTURE SEULE sur GISCO, le referentiel geographique d'Eurostat :
pays, regions NUTS 0 a 3, communes LAU, villes et zones urbaines fonctionnelles,
codes postaux europeens. Le versant britannique vient de l'ONS et se charge dans
les MEMES tables, avec source='ONS' - une reponse qui melange les deux origines
doit le dire.

Aucune cle, aucun compte : GISCO est un service public ouvert. Si quelqu'un
cherche a configurer un secret pour ce connecteur, c'est une erreur.

Commence par gisco_couches() : il dit ce qui est rapatrie sur ce poste et
signale les couches en retard sur ce qu'Eurostat publie. Neuf fois sur dix, une
donnee "absente" est une couche pas encore synchronisee, et le message d'erreur
donne la commande exacte a lancer. Les millesimes ne sont pas ecrits en dur :
ils sont decouverts chez Eurostat, et gisco_maj() remet le cache a niveau - en
simulation par defaut, parce qu'une mise a jour des codes postaux pese 200 Mo.

Chaque reponse porte son MILLESIME et sa date de rapatriement : recopie-les. Un
decoupage administratif change, et une reponse geographique sans millesime n'est
pas verifiable.

Quatre pieges rendent une reponse fausse SANS lever d'erreur ; ils sont detailles
dans catalogue.json, que gisco_catalogue() restitue. PT dans un nom de fichier
designe une geometrie PONCTUELLE, pas le Portugal. La population des communes
francaises et espagnoles est ABSENTE du millesime LAU 2024 - elle y vaut zero -
alors qu'elle est complete en 2023 : un zero n'est pas une population. La Grece
est EL et le Royaume-Uni UK, pas GR ni GB. Un code postal ne porte qu'UNE
commune de rattachement, ce qui n'est pas "la" commune du code postal.

gisco_distance rend une orthodromie, jamais un kilometrage routier. GISCO ne
porte aucune adresse et aucune donnee Vente-unique : il fournit la grille
geographique sur laquelle poser des volumes, pas les volumes.

N'exporte en CSV que si l'utilisateur a demande un export, et jamais dans un
dossier synchronise.
"""


def _hints(titre: str, monde_ouvert: bool, idempotent: bool = True) -> ToolAnnotations:
    """Les annotations de comportement de la specification MCP.

    `readOnlyHint` a vrai partout : aucun outil n'a de chemin d'ecriture vers la
    source. GISCO et l'ONS ne servent que des fichiers statiques, en GET, et le
    cache local est ouvert en lecture seule pour toute requete SQL. Ce ne sont
    que des indications - la specification demande aux clients de ne pas leur
    faire confiance - ce qui garantit la lecture seule reste le code.

    Quatre outils ecrivent neanmoins sur le disque LOCAL : gisco_sync et
    gisco_maj remplacent une couche du cache, les deux exports deposent un CSV.
    Ils portent `idempotentHint=False`, qui est la facon juste de le dire : leur
    effet n'est pas celui d'une lecture repetee.

    `openWorldHint` distingue ce qui ne lit que le cache local - schema, SQL,
    export d'un SQL - de ce qui peut rappeler Eurostat. Un simple lecteur de
    referentiel est du cote ouvert : si la couche manque encore et qu'elle est
    legere, la premiere question la rapatrie.
    """
    return ToolAnnotations(
        title=titre,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=monde_ouvert,
    )


HORS_LIGNE = False
SUR_LA_SOURCE = True

mcp = FastMCP("gisco", instructions=SERVER_INSTRUCTIONS)


@mcp.tool(annotations=_hints("Diagnostic complet, acces GISCO compris", SUR_LA_SOURCE, idempotent=False))
@_guard
def gisco_doctor() -> str:
    """Diagnostic : interpreteur, chemins locaux, couches en cache, acces reseau.

    C'est le premier outil a lancer quand quelque chose ne repond pas. Il dit
    quel Python execute le serveur, ou vivent le cache et les exports, quelles
    couches sont rapatriees et depuis quand, et si GISCO repond.
    """
    lignes = [
        "MCP gisco - diagnostic",
        "",
        f"Interpreteur      : {sys.executable}",
        f"Version Python    : {sys.version.split()[0]} ({os.name})",
        f"Racine locale     : {_local_root()}",
        f"Cache SQLite      : {_cache_db()}"
        + (f" ({_cache_db().stat().st_size / 1048576:.1f} Mo)" if _cache_db().is_file() else " (absent)"),
        f"Telechargements   : {_download_dir()}",
        f"Dossier d'export  : {_export_dir()}",
        f"Plafond de telechargement : {_max_download_mb()} Mo",
        f"Rapatriement auto sous    : {_autosync_max_mb()} Mo",
        "",
    ]
    cible = str(_export_dir()).lower()
    if any(m in cible for m in SYNCED_MARKERS):
        lignes.append(
            "ATTENTION : le dossier d'export est dans un espace SYNCHRONISE. Un "
            "CSV depose la part chez tout le monde. Change GISCO_EXPORT_DIR."
        )
        lignes.append("")

    lignes.append("Couches en cache :")
    state_rows: list[tuple] = []
    if _cache_db().is_file():
        conn = sqlite3.connect(f"file:{_cache_db()}?mode=ro", uri=True)
        try:
            state_rows = conn.execute(
                "SELECT couche, cible, edition, lignes, substr(telecharge_le,1,16) "
                "FROM couches ORDER BY couche"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            lignes.append(f"  cache illisible : {exc}")
        finally:
            conn.close()
    if state_rows:
        lignes.append("  " + _render(
            state_rows, ["couche", "table", "edition", "lignes", "rapatrie le"]
        ).replace("\n", "\n  "))
    else:
        lignes.append("  (aucune) - lance gisco_sync(couche=\"nuts\") pour commencer.")

    lignes.append("")
    try:
        res = _get(_catalogue()["racine"] + "/distribution/v2/nuts/datasets.json")
        lignes.append(f"Acces GISCO       : OK | {len(res.json())} millesimes NUTS publies")
    except GiscoError as exc:
        lignes.append(f"Acces GISCO       : ECHEC | {exc}")
    return "\n".join(lignes)


@mcp.tool(annotations=_hints("Couches connues et millesimes publies", SUR_LA_SOURCE))
@_guard
def gisco_catalogue(couche: str = "", rafraichir: bool = False) -> str:
    """Ce que GISCO publie REELLEMENT : millesimes, resolutions, colonnes, pieges.

    Les millesimes ne sont pas ecrits en dur : ils sont lus chez Eurostat
    (datasets.json), gardes une semaine dans le cache local, et l'outil dit
    toujours d'ou sort l'information - reseau, cache local, ou catalogue
    embarque quand le reseau ne repond pas. C'est l'outil a lire avant
    d'inventer un nom de couche ou un millesime.

    couche      vide pour la vue d'ensemble, sinon nuts, lau, pcode, urau,
                countries - ou un mot francais : communes, regions, codes
                postaux, villes, pays.
    rafraichir  force la relecture chez Eurostat, sans attendre l'expiration du
                releve local.
    """
    cat = _catalogue()
    if couche:
        spec = _layer_spec(couche)
        jeu = spec.get("jeu") or spec["nom"]
        rel = _releve(jeu, rafraichir)
        publies = sorted(rel["editions"], key=lambda a: int(a), reverse=True)
        lignes = [
            f"Couche '{spec['nom']}' - {spec['titre']}",
            f"  table du cache : {spec['table']} (cle : {spec['cle']})",
            f"  millesimes     : {', '.join(publies) or '(inconnus)'}",
            f"  le plus recent : {publies[0] if publies else '-'}  <- pris par defaut",
            f"  releve         : {rel['origine']}",
        ]
        if publies:
            fichiers = _gpkg_publies(jeu, publies[0])
            motif = spec.get("motif_fichier")
            dispo = []
            if motif:
                rx = re.compile(motif)
                dispo = sorted({
                    (m.groupdict().get("resolution") or "").upper()
                    for m in (rx.match(f) for f in fichiers) if m
                } - {""})
            lignes.append(
                f"  resolutions    : {', '.join(dispo) or '(sans objet pour ce jeu)'}"
            )
            preferee = [r for r in (spec.get("resolution_preferee") or []) if r in dispo]
            if preferee:
                lignes.append(f"  preferee       : {preferee[0]}")
        lignes.append("  colonnes voulues -> nom dans le fichier :")
        motifs = spec.get("motifs_colonnes") or {}
        for cible, source in (spec.get("colonnes") or {}).items():
            suffixe = f"   [retrouvee par motif {motifs[cible]}]" if cible in motifs else ""
            lignes.append(f"      {cible:<12} <- {source}{suffixe}")
        if spec.get("remarque"):
            lignes += ["", "  A savoir : " + spec["remarque"]]
        state = _layer_state(spec["nom"])
        lignes.append("")
        if state:
            retard = (
                f"  EN RETARD : {publies[0]} est publie."
                if publies and state.get("millesime") and state["millesime"] != publies[0]
                else "  A jour."
            )
            lignes.append(
                f"  Etat du cache : {state['lignes']} lignes, edition {state['edition']}, "
                f"rapatriee le {str(state['telecharge_le'])[:16]}\n{retard}"
            )
        else:
            lignes.append("  Etat du cache : couche absente.")
        return "\n".join(lignes)

    lignes = [
        "Jeux GISCO connus du connecteur",
        "Le millesime affiche est le DERNIER PUBLIE, pas une valeur figee.",
        "",
    ]
    rows = []
    for nom, spec in (cat.get("couches") or {}).items():
        state = _layer_state(nom)
        jeu = spec.get("jeu") or nom
        try:
            publies = _millesimes(jeu, rafraichir)
        except GiscoError:
            publies = []
        en_cache = (state or {}).get("millesime") or ""
        etat = "absente"
        if state:
            etat = f"{state['lignes']} l."
            if publies and en_cache and en_cache != publies[0]:
                etat += f" ({en_cache}, EN RETARD)"
            elif en_cache:
                etat += f" ({en_cache})"
        rows.append((
            nom, spec["titre"][:40],
            publies[0] if publies else "?",
            str(len(publies)) if publies else "?",
            etat,
        ))
    lignes.append(_render(
        rows, ["couche", "titre", "dernier", "nb millesimes", "cache"]
    ))
    lignes += ["", "Versant britannique (ONS, hors GISCO depuis le Brexit) :"]
    rows = []
    for nom, spec in (cat.get("couches_uk") or {}).items():
        # uk_lookup n'est pas une couche : c'est la table de correspondance que
        # le chargement des codes postaux britanniques consomme au passage. La
        # lister la ferait croire a une couche manquante.
        if not isinstance(spec, dict) or "titre" not in spec or not spec.get("table"):
            continue
        state = _layer_state(nom)
        rows.append((nom, spec["titre"][:52], f"{state['lignes']} l." if state else "absente"))
    lignes.append(_render(rows, ["couche", "titre", "cache"]))

    lignes += ["", "Millesimes publies, par jeu :"]
    for nom, spec in (cat.get("couches") or {}).items():
        jeu = spec.get("jeu") or nom
        try:
            rel = _releve(jeu, rafraichir)
        except GiscoError as exc:
            lignes.append(f"  {nom:<10} : injoignable ({exc})")
            continue
        publies = sorted(rel["editions"], key=lambda a: int(a), reverse=True)
        lignes.append(f"  {nom:<10} : {', '.join(publies) or '(inconnus)'}   [{rel['origine']}]")

    lignes += ["", "Pieges a connaitre :"]
    for nom, texte in (cat.get("pieges") or {}).items():
        lignes.append(f"  - {nom} : " + " ".join(t for t in texte if t))
    return "\n".join(lignes)


@mcp.tool(annotations=_hints("Rapatrier une couche - ECRIT sur le disque", SUR_LA_SOURCE, idempotent=False))
@_guard
def gisco_sync(
    couche: str,
    annee: str = "",
    resolution: str = "",
    niveau: str = "",
    force: bool = False,
    confirmer: bool = False,
) -> str:
    """Rapatrie une couche GISCO ou ONS dans le cache local. ECRIT SUR LE DISQUE.

    Par defaut, prend LE DERNIER MILLESIME PUBLIE chez Eurostat - il est
    decouvert, pas devine, et le rendu dit lequel a ete pris et quels autres
    existent. A lancer une fois par couche : les lectures qui suivent se font
    hors ligne, en millisecondes.

    couche      nuts, lau, pcode, urau, countries - ou le versant britannique :
                uk_lad (communes), uk_itl3 (NUTS3), uk_onspd (codes postaux).
    annee       vide (ou "derniere") pour le dernier millesime publie. Une annee
                a quatre chiffres pour en figer un - ce qui a un usage precis :
                le dernier millesime est le meilleur DECOUPAGE, pas forcement le
                mieux RENSEIGNE. La population des communes francaises et
                espagnoles est absente en 2024 et presente en 2023.
    resolution  60M a 01M pour nuts et countries. Vide prend la resolution
                preferee parmi celles reellement publiees pour ce millesime -
                20M, qui suffit pour savoir dans quelle region tombe un point.
    niveau      pour nuts seulement : 0, 1, 2 ou 3 pour ne charger qu'un niveau.
                Vide charge les quatre d'un coup, ce qui est le cas courant.
    force       retelecharge meme si le fichier est deja la, et passe outre le
                plafond de poids.
    confirmer   exige pour uk_onspd, qui rapatrie 1,8 million de codes postaux.

    Une couche deja presente est REMPLACEE par le millesime demande : les deux
    ne cohabitent pas dans le cache. Le fichier est ecrit dans la racine locale
    de l'outil, hors de tout dossier synchronise.
    """
    nom = _norm(couche).replace(" ", "_")
    with _SYNC_LOCK:
        if nom.startswith("uk_"):
            return _sync_uk(nom, confirmer)
        return _sync_gisco(couche, annee, resolution, niveau, force)


@mcp.tool(annotations=_hints("Ce qui est rapatrie sur ce poste", SUR_LA_SOURCE))
@_guard
def gisco_couches(verifier: bool = True) -> str:
    """Ce qui est rapatrie sur ce poste, et si un millesime plus recent est publie.

    A lire avant de conclure qu'une donnee manque : neuf fois sur dix, la couche
    n'est simplement pas encore synchronisee.

    verifier  compare chaque couche GISCO au dernier millesime publie et signale
              celles qui sont en retard. Cela demande un petit appel reseau par
              jeu, mis en cache une semaine. Mets False pour rester hors ligne.
    """
    if not _cache_db().is_file():
        return (
            "Aucun cache local. Rien n'a encore ete rapatrie.\n"
            "Commence par gisco_sync(couche=\"nuts\") puis gisco_sync(couche=\"pcode\")."
        )
    conn = _open_db(readonly=True)
    try:
        rows = conn.execute(
            "SELECT couche, cible, edition, millesime, lignes, substr(telecharge_le,1,16), "
            "round(octets/1048576.0,1) FROM couches ORDER BY couche"
        ).fetchall()
        detail = []
        for table in ("nuts", "lau", "pcode", "urau", "countries"):
            for src, n in conn.execute(
                f"SELECT source, COUNT(*) FROM {table} GROUP BY source"
            ):
                detail.append((table, src, n))
    finally:
        conn.close()

    enrichies = []
    retards = []
    for couche, cible, edition, millesime, lignes, date, mo in rows:
        etat = "-"
        if verifier and not str(couche).startswith("uk_"):
            try:
                spec = _layer_spec(couche)
                publies = _millesimes(spec.get("jeu") or couche)
            except GiscoError:
                publies = []
            if publies and millesime:
                if millesime == publies[0]:
                    etat = "a jour"
                else:
                    etat = f"EN RETARD -> {publies[0]}"
                    retards.append((couche, millesime, publies[0]))
            elif publies:
                etat = f"dernier publie {publies[0]}"
        elif str(couche).startswith("uk_"):
            # Le millesime britannique est dans le NOM du service ArcGIS : il ne
            # se compare pas a une liste, il se met a jour dans catalogue.json.
            etat = "ONS, suivi manuel"
        enrichies.append((couche, cible, edition, lignes, date, mo, etat))

    out = [
        f"Cache : {_cache_db()} ({_cache_db().stat().st_size / 1048576:.1f} Mo)",
        "",
        _render(enrichies, ["couche", "table", "edition", "lignes", "rapatrie le", "Mo", "millesime"]),
        "",
        "Contenu des tables, par origine :",
        _render(detail, ["table", "source", "lignes"]),
    ]
    if retards:
        out += [
            "",
            f"{len(retards)} couche(s) en retard sur ce qui est publie : "
            + ", ".join(f"{c} ({v} -> {d})" for c, v, d in retards)
            + ".\ngisco_maj() les remet toutes au dernier millesime. Avant de le "
            "lancer sur 'lau', lis le piege "
            "'le_dernier_millesime_n_est_pas_le_plus_complet' : le plus recent "
            "n'est pas le mieux renseigne.",
        ]
    elif verifier:
        out += ["", "Toutes les couches GISCO du cache sont au dernier millesime publie."]
    return "\n".join(out)


@mcp.tool(annotations=_hints("Remettre le cache au dernier millesime - ECRIT sur le disque", SUR_LA_SOURCE, idempotent=False))
@_guard
def gisco_maj(simuler: bool = True, couches: str = "") -> str:
    """Remet les couches du cache au dernier millesime publie. ECRIT SUR LE DISQUE.

    simuler   True par defaut : dit ce qui serait retelecharge, et ce que ca
              pese, sans rien faire. C'est volontaire - une mise a jour de
              'pcode' represente 200 Mo, et personne ne veut la declencher en
              posant une question.
    couches   limite la mise a jour a ces couches, separees par des virgules.
              Vide reprend toutes les couches GISCO presentes dans le cache.

    Les couches britanniques ne sont pas concernees : leur millesime vit dans le
    NOM du service ArcGIS et dans les noms de champs, donc il se met a jour dans
    catalogue.json, a la main.
    """
    if not _cache_db().is_file():
        return "Aucun cache local : il n'y a rien a mettre a jour."
    demandees = {_norm(c).replace(" ", "_") for c in couches.split(",") if c.strip()}
    conn = _open_db(readonly=True)
    try:
        presentes = conn.execute(
            "SELECT couche, millesime FROM couches ORDER BY couche"
        ).fetchall()
    finally:
        conn.close()

    plan, a_jour, ignorees = [], [], []
    for couche, millesime in presentes:
        if demandees and couche not in demandees:
            continue
        if str(couche).startswith("uk_"):
            ignorees.append(couche)
            continue
        try:
            spec = _layer_spec(couche)
            publies = _millesimes(spec.get("jeu") or couche)
        except GiscoError as exc:
            ignorees.append(f"{couche} ({exc})")
            continue
        if not publies:
            ignorees.append(f"{couche} (millesimes inconnus)")
            continue
        if millesime == publies[0]:
            a_jour.append(f"{couche} ({millesime})")
            continue
        try:
            nom_fichier, annee, _, _ = _choisir_fichier(spec, "", "", "")
            taille = _head_size(_gisco_url(spec.get("jeu") or couche, nom_fichier))
        except GiscoError as exc:
            ignorees.append(f"{couche} ({exc})")
            continue
        plan.append((couche, millesime or "?", annee, taille, nom_fichier))

    out = []
    if a_jour:
        out.append("Deja au dernier millesime : " + ", ".join(a_jour))
    if ignorees:
        out.append("Non concernees : " + ", ".join(ignorees))
    if not plan:
        out.append("Rien a mettre a jour.")
        return "\n".join(out)

    total = sum(t for *_, t, _ in plan)
    out += [
        "",
        f"{len(plan)} couche(s) a mettre a jour, {total / 1048576:.0f} Mo a telecharger :",
        _render(
            [(c, v, n, f"{t / 1048576:.1f}", f) for c, v, n, t, f in plan],
            ["couche", "en cache", "vers", "Mo", "fichier"],
        ),
    ]
    if simuler:
        out += [
            "",
            "SIMULATION - rien n'a ete telecharge. Relance avec simuler=False "
            "pour executer.",
        ]
        if any(c == "lau" for c, *_ in plan):
            out.append(
                "\nAVANT de mettre 'lau' a jour : au millesime 2024, la "
                "population de la France et de l'Espagne est ABSENTE, alors "
                "qu'elle est presente en 2023. Si des reponses de population "
                "dependent de cette couche, garde le millesime actuel ou "
                "assume la perte."
            )
        return "\n".join(out)

    out.append("")
    for couche, _, _, _, _ in plan:
        try:
            with _SYNC_LOCK:
                _sync_gisco(couche, "", "", "", False)
            state = _layer_state(couche)
            out.append(
                f"  {couche} : OK, millesime {state['millesime']}, "
                f"{state['lignes']} lignes"
            )
        except (GiscoError, geo.GeoError) as exc:
            out.append(f"  {couche} : ECHEC - {exc}")
    return "\n".join(out)


@mcp.tool(annotations=_hints("Referentiel des pays, statut UE / AELE", SUR_LA_SOURCE))
@_guard
def gisco_pays(recherche: str = "", ue_seulement: bool = False, limite: int = 60) -> str:
    """Referentiel des pays : code Eurostat, nom, ISO3, capitale, statut UE / AELE.

    Attention aux deux codes qui ne suivent pas l'ISO : EL pour la Grece et UK
    pour le Royaume-Uni. Un rapprochement avec un referentiel maison en GR ou GB
    rate ces deux pays sans lever d'erreur.

    recherche     un code, un nom francais ou anglais, meme partiel et sans accent.
    ue_seulement  ne garde que les Etats membres.
    """
    _besoin("countries")
    where, params = [], []
    if recherche.strip():
        m = _norm(recherche)
        where.append("(nom_norm LIKE ? OR lower(cntr_id) = ? OR lower(iso3_code) = ?)")
        params += [f"%{m}%", m, m]
    if ue_seulement:
        where.append("eu_stat = 'T'")
    cols = ["cntr_id", "name_fren", "name_engl", "iso3_code", "capt", "eu_stat", "efta_stat"]
    rows, _, total = _select("countries", cols, where, params, "cntr_id", limite)
    filtres = f"recherche={recherche!r} ue_seulement={ue_seulement}"
    return _entete(total, len(rows), limite, ["countries"], filtres) + "\n\n" + _render(rows, cols)


@mcp.tool(annotations=_hints("Regions NUTS, niveaux 0 a 3", SUR_LA_SOURCE))
@_guard
def gisco_nuts(
    niveau: int = 3,
    pays: str = "",
    nom: str = "",
    nuts_id: str = "",
    limite: int = 50,
) -> str:
    """Regions NUTS : le decoupage statistique europeen, du pays (0) au departement (3).

    Les quatre niveaux : 0 = pays, 1 = grande region, 2 = region, 3 = ce qui
    correspond en France au departement. Un plan de transport se raisonne
    presque toujours en NUTS3.

    niveau   0 a 3. Mets -1 pour ne pas filtrer sur le niveau.
    pays     code Eurostat (FR, DE, ES, IT, BE, NL, PT, EL, UK...).
    nom      recherche partielle, insensible aux accents et a la casse.
    nuts_id  un code precis (FRE22, DE111...). Le prefixe fonctionne : 'FRE'
             rend tout ce qui commence par FRE.

    Le Royaume-Uni n'apparait que si uk_itl3 a ete synchronise, et ses lignes
    portent source='ONS' avec des codes en TL, pas en UK.
    """
    _besoin("nuts")
    where, params = [], []
    if niveau >= 0:
        where.append("levl_code = ?")
        params.append(int(niveau))
    variants = _country_variants(pays)
    if variants:
        where.append("cntr_code IN (" + ",".join("?" * len(variants)) + ")")
        params += list(variants)
    if nom.strip():
        where.append("nom_norm LIKE ?")
        params.append(f"%{_norm(nom)}%")
    if nuts_id.strip():
        where.append("nuts_id LIKE ?")
        params.append(nuts_id.strip().upper() + "%")
    cols = ["nuts_id", "levl_code", "cntr_code", "name_latn", "urbn_type", "coast_type", "source"]
    rows, _, total = _select("nuts", cols, where, params, "nuts_id", limite)
    filtres = f"niveau={niveau} pays={pays!r} nom={nom!r} nuts_id={nuts_id!r}"
    return (
        _entete(total, len(rows), limite, ["nuts", "uk_itl3"], filtres)
        + "\n\nurbn_type : 1 urbain, 2 intermediaire, 3 rural | coast_type : 1 cotier, 3 non cotier\n\n"
        + _render(rows, cols)
    )


def _alerte_population(pays: str) -> str:
    """Previent quand la population est vide sur TOUT le perimetre demande.

    Le millesime LAU 2024 ne porte aucune population pour la France ni pour
    l'Espagne : 34 946 et 8 132 communes a zero. Le millesime 2023 est complet.
    Sans cette alerte, une question de population sur la France rend zero
    partout - ce qui se lit comme un resultat, alors que c'est une absence.
    """
    variants = _country_variants(pays)
    if not variants:
        return ""
    conn = _open_db(readonly=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN pop IS NULL OR pop = 0 THEN 1 ELSE 0 END) "
            "FROM lau WHERE cntr_code IN (" + ",".join("?" * len(variants)) + ")",
            list(variants),
        ).fetchone()
    except sqlite3.DatabaseError:
        return ""
    finally:
        conn.close()
    total, vides = row[0] or 0, row[1] or 0
    if not total or vides < total:
        return ""
    if variants[0] == "UK":
        return (
            "\nPOPULATION ABSENTE : l'ONS ne publie pas la population dans la "
            "couche des districts. Ce n'est pas un zero, c'est un manque."
        )
    state = _layer_state("lau")
    edition = state["edition"] if state else "?"
    return (
        f"\nPOPULATION ABSENTE sur tout ce perimetre au millesime {edition} : "
        f"les {total} communes sont a zero, ce qui est une ABSENCE DE MESURE et "
        "non un chiffre. GISCO ne publie la population ni de la France ni de "
        "l'Espagne au millesime 2024. Pour une question de population, recharge "
        "le millesime 2023 : gisco_sync(couche=\"lau\", annee=\"2023\"). Pour une "
        "question de perimetre ou de geometrie, 2024 reste le bon millesime."
    )


@mcp.tool(annotations=_hints("Communes LAU", SUR_LA_SOURCE))
@_guard
def gisco_lau(
    pays: str = "",
    nom: str = "",
    gisco_id: str = "",
    pop_min: int = 0,
    limite: int = 50,
) -> str:
    """Communes europeennes (LAU) : nom, population, superficie.

    98 000 communes sur 34 pays. La population vaut 0 - et non vide - pour les
    pays hors Union : un zero y est une absence, pas un chiffre. Ne somme pas
    sans filtrer.

    pays      code Eurostat. UK n'apparait que si uk_lad est synchronise, et
              alors sans population : l'ONS ne la publie pas dans cette couche.
    nom       recherche partielle sans accent.
    gisco_id  identifiant GISCO complet (FR_60016) ou prefixe (FR_60).
    pop_min   ne garde que les communes au-dessus de ce seuil.
    """
    _besoin("lau")
    where, params = [], []
    variants = _country_variants(pays)
    if variants:
        where.append("cntr_code IN (" + ",".join("?" * len(variants)) + ")")
        params += list(variants)
    if nom.strip():
        where.append("nom_norm LIKE ?")
        params.append(f"%{_norm(nom)}%")
    if gisco_id.strip():
        where.append("gisco_id LIKE ?")
        params.append(gisco_id.strip().upper() + "%")
    if pop_min > 0:
        where.append("pop >= ?")
        params.append(int(pop_min))
    cols = ["gisco_id", "cntr_code", "lau_name", "pop", "pop_dens", "area_km2", "source"]
    ordre = "pop DESC" if pop_min > 0 else "gisco_id"
    rows, _, total = _select("lau", cols, where, params, ordre, limite)
    filtres = f"pays={pays!r} nom={nom!r} gisco_id={gisco_id!r} pop_min={pop_min}"
    return (
        _entete(total, len(rows), limite, ["lau", "uk_lad"], filtres)
        + _alerte_population(pays)
        + "\n\n"
        + _render(rows, cols)
    )


@mcp.tool(annotations=_hints("Villes et zones urbaines fonctionnelles", SUR_LA_SOURCE))
@_guard
def gisco_villes(
    pays: str = "",
    nom: str = "",
    categorie: str = "",
    limite: int = 50,
) -> str:
    """Villes et zones urbaines fonctionnelles (Urban Audit).

    Trois lectures d'une meme ville, et elles ne recouvrent pas le meme
    territoire : C est la ville au sens administratif, K son noyau dense, F la
    zone urbaine fonctionnelle - l'aire d'attraction, bassin de main-d'oeuvre
    compris. « Livrer sur Lyon » ne veut pas dire la meme chose selon celle
    qu'on retient, et l'ecart de surface va de un a dix.

    categorie  C, K ou F. Vide rend les trois.
    """
    _besoin("urau")
    where, params = [], []
    variants = _country_variants(pays)
    if variants:
        where.append("cntr_code IN (" + ",".join("?" * len(variants)) + ")")
        params += list(variants)
    if nom.strip():
        where.append("nom_norm LIKE ?")
        params.append(f"%{_norm(nom)}%")
    if categorie.strip():
        where.append("urau_catg = ?")
        params.append(categorie.strip().upper())
    cols = ["urau_code", "urau_catg", "cntr_code", "urau_name", "nuts3", "area_km2", "fua_code"]
    rows, _, total = _select("urau", cols, where, params, "urau_name", limite)
    filtres = f"pays={pays!r} nom={nom!r} categorie={categorie!r}"
    legende = " | ".join(f"{k} = {v}" for k, v in (_catalogue().get("urau_catg") or {}).items())
    return (
        _entete(total, len(rows), limite, ["urau"], filtres)
        + f"\nCategories : {legende}\nMalgre son nom d'origine (AREA_SQM), la surface est en km2.\n\n"
        + _render(rows, cols)
    )


@mcp.tool(annotations=_hints("Codes postaux et leur rattachement", SUR_LA_SOURCE))
@_guard
def gisco_codes_postaux(
    pays: str = "",
    prefixe: str = "",
    nuts3: str = "",
    commune: str = "",
    dgurba: int = 0,
    limite: int = 50,
) -> str:
    """Codes postaux europeens, avec leur commune, leur NUTS3 et leur densite.

    C'est la couche la plus utile pour un zonage de transport : chaque code
    postal porte sa commune, sa region NUTS3, sa zone urbaine fonctionnelle,
    son degre d'urbanisation et sa position.

    pays     code Eurostat. UK n'est present que si uk_onspd est synchronise.
    prefixe  debut de code postal - '60' rend l'Oise, '75' Paris.
    nuts3    un code NUTS3 (FRE22...).
    commune  nom de commune, partiel et sans accent.
    dgurba   1 zone dense, 2 intermediaire, 3 peu peuplee. 0 = pas de filtre.
             C'est l'indicateur qui distingue une livraison urbaine d'une
             livraison rurale sans avoir a le deviner du code postal.
    """
    _besoin("pcode")
    where, params = [], []
    variants = _country_variants(pays)
    if variants:
        where.append("cntr_id IN (" + ",".join("?" * len(variants)) + ")")
        params += list(variants)
    if prefixe.strip():
        where.append("pc_norm LIKE ?")
        params.append(_norm(prefixe).replace(" ", "") + "%")
    if nuts3.strip():
        where.append("nuts3 = ?")
        params.append(nuts3.strip().upper())
    if commune.strip():
        where.append("lower(lau_name) LIKE ?")
        params.append(f"%{_norm(commune)}%")
    if dgurba:
        where.append("dgurba = ?")
        params.append(int(dgurba))
    cols = ["postcode", "cntr_id", "lau_name", "nuts3", "dgurba", "fua_id", "lon", "lat", "source"]
    rows, _, total = _select("pcode", cols, where, params, "cntr_id, pc_norm", limite)
    filtres = f"pays={pays!r} prefixe={prefixe!r} nuts3={nuts3!r} commune={commune!r} dgurba={dgurba}"
    legende = " | ".join(f"{k} = {v}" for k, v in (_catalogue().get("dgurba") or {}).items())
    return (
        _entete(total, len(rows), limite, ["pcode", "uk_onspd"], filtres)
        + f"\ndgurba : {legende}\n\n"
        + _render(rows, cols)
    )


@mcp.tool(annotations=_hints("Resoudre une liste de codes postaux", SUR_LA_SOURCE))
@_guard
def gisco_resoudre(pays: str, codes: str) -> str:
    """Traduit un ou plusieurs codes postaux en commune, region et position.

    C'est l'outil du rapprochement : on lui donne la colonne de codes postaux
    d'un fichier, il rend pour chacun la commune, le NUTS3, le degre
    d'urbanisation et les coordonnees - et il DIT lesquels sont introuvables,
    au lieu de les laisser disparaitre.

    pays   code Eurostat du lot (FR, BE, NL...). Un seul pays a la fois : le
           meme code postal existe dans plusieurs pays.
    codes  les codes, separes par des virgules, des points-virgules, des
           espaces ou des retours a la ligne.
    """
    _besoin("pcode")
    pays_v = _country_variants(pays)
    if not pays_v:
        raise GiscoError("Donne le pays : le meme code postal existe dans plusieurs pays.")
    liste = [c for c in re.split(r"[,;\s]+", codes or "") if c.strip()]
    if not liste:
        raise GiscoError("Aucun code postal donne.")
    if len(liste) > 5000:
        raise GiscoError(
            f"{len(liste)} codes demandes : au-dela de 5 000, passe par "
            "gisco_export_csv, qui ecrit le resultat dans un fichier."
        )

    conn = _open_db(readonly=True)
    trouves: dict[str, tuple] = {}
    try:
        for i in range(0, len(liste), 400):
            lot = [_norm(c).replace(" ", "") for c in liste[i:i + 400]]
            marks = ",".join("?" * len(lot))
            pmarks = ",".join("?" * len(pays_v))
            for row in conn.execute(
                f"SELECT pc_norm, postcode, cntr_id, lau_name, lau_id, nuts3, dgurba, "
                f"fua_id, lon, lat, source FROM pcode "
                f"WHERE cntr_id IN ({pmarks}) AND pc_norm IN ({marks})",
                [*pays_v, *lot],
            ):
                trouves.setdefault(row[0], row)
    finally:
        conn.close()

    cols = ["code demande", "postcode", "pays", "commune", "lau_id", "nuts3", "dgurba", "fua_id", "lon", "lat", "source"]
    rows = []
    manquants = []
    for code in liste:
        key = _norm(code).replace(" ", "")
        hit = trouves.get(key)
        if hit:
            rows.append((code, *hit[1:]))
        else:
            manquants.append(code)
    out = [_provenance(["pcode", "uk_onspd"])]
    out.append(f"{len(rows)} code(s) resolus sur {len(liste)} demandes.")
    if manquants:
        out.append(
            f"INTROUVABLES ({len(manquants)}) : " + ", ".join(manquants[:40])
            + (" ..." if len(manquants) > 40 else "")
            + "\nUn code introuvable n'est pas forcement faux : il peut etre "
            "trop recent pour le millesime charge, ou le pays peut ne pas etre "
            "couvert. gisco_couches() dit quel millesime est en cache."
        )
    out += ["", _render(rows, cols)]
    return "\n".join(out)


@mcp.tool(annotations=_hints("Rattacher un point a ses contours, hors ligne", SUR_LA_SOURCE))
@_guard
def gisco_localiser(lat: float, lon: float, couches: str = "countries,nuts,lau") -> str:
    """Dit dans quel pays, quelle region et quelle commune tombe un point.

    C'est un vrai test d'appartenance geometrique, calcule sur les contours
    rapatries - pas une recherche par proximite. Il fonctionne hors ligne.

    lat, lon  en degres decimaux, WGS84. Attention a l'ordre : la latitude
              d'abord. Inverses, 48.85 / 2.35 (Paris) tombe au large de la
              Somalie, et rien ne le signale.
    couches   celles a interroger, separees par des virgules : countries, nuts,
              lau, urau. Retirer 'lau' rend la reponse instantanee sur un gros
              lot ; les communes sont la couche la plus lourde.
    """
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise GiscoError(
            f"Coordonnees hors bornes (lat={lat}, lon={lon}). La latitude va de "
            "-90 a 90, la longitude de -180 a 180. Verifie l'ordre des deux."
        )
    demandees = [c.strip() for c in couches.split(",") if c.strip()]
    plan = {
        "countries": ("countries", "cntr_id", ["cntr_id", "name_fren", "iso3_code", "eu_stat"]),
        "nuts": ("nuts", "nuts_id", ["nuts_id", "levl_code", "name_latn", "source"]),
        "lau": ("lau", "gisco_id", ["gisco_id", "lau_name", "pop", "area_km2", "source"]),
        "urau": ("urau", "urau_code", ["urau_code", "urau_catg", "urau_name", "source"]),
    }
    out = [f"Point : lat {lat}, lon {lon} (WGS84)"]
    utilisees = []
    for nom in demandees:
        if nom not in plan:
            out.append(f"  couche inconnue, ignoree : {nom}")
            continue
        table, cle, cols = plan[nom]
        try:
            _besoin(nom)
        except GiscoError as exc:
            out.append(f"  {nom} : {exc}")
            continue
        utilisees.append(nom)
        conn = _open_db(readonly=True)
        try:
            # Le cadre englobant elimine 99,9 % des candidats sans decoder la
            # geometrie. Sans ce filtre, chaque appel decoderait 98 000 communes.
            candidats = conn.execute(
                f"SELECT {', '.join(cols)}, geom FROM {table} "
                "WHERE min_lon <= ? AND max_lon >= ? AND min_lat <= ? AND max_lat >= ? "
                "AND geom IS NOT NULL",
                (lon, lon, lat, lat),
            ).fetchall()
        finally:
            conn.close()
        touches = []
        for row in candidats:
            try:
                g = geo.wkb_to_geojson(row[-1])
            except geo.GeoError:
                continue
            if geo.contains(g, lon, lat):
                touches.append(row[:-1])
        out.append("")
        out.append(f"{nom} : {len(touches)} unite(s) contiennent ce point "
                   f"({len(candidats)} candidats testes)")
        out.append(_render(touches, cols) if touches else
                   "  (aucune - le point est hors du perimetre de la couche rapatriee)")
    if utilisees:
        out.insert(1, _provenance(utilisees))
    return "\n".join(out)


@mcp.tool(annotations=_hints("Trouver des coordonnees (OpenStreetMap, non officiel)", SUR_LA_SOURCE))
@_guard
def gisco_geocoder(q: str, pays: str = "", limite: int = 5) -> str:
    """Cherche une adresse ou un lieu et rend ses coordonnees. EN LIGNE.

    Cet outil interroge l'API de recherche de GISCO, qui s'appuie sur
    OpenStreetMap. Ce n'est donc PAS de la donnee administrative officielle
    comme le reste du connecteur : c'est de la donnee collaborative, tres bonne
    en ville et inegale en zone rurale. Pour un rattachement administratif ferme
    - quelle commune, quelle region - enchaine sur gisco_localiser avec les
    coordonnees rendues ici.

    q       l'adresse ou le lieu, en clair.
    pays    restreint au pays (code ISO2 : FR, BE, DE...).
    """
    if not q.strip():
        raise GiscoError("Donne quelque chose a chercher.")
    params: dict[str, Any] = {"q": q.strip(), "limit": max(1, min(int(limite), 20))}
    res = _get(_catalogue()["racine"] + "/api/", params)
    try:
        payload = res.json()
    except ValueError as exc:
        raise GiscoError(f"Reponse de l'API de recherche illisible : {exc}") from exc
    filtre = (pays or "").strip().upper()
    if filtre in ("EL",):
        filtre = "GR"
    if filtre in ("UK",):
        filtre = "GB"
    rows = []
    for feat in payload.get("features") or []:
        p = feat.get("properties") or {}
        if filtre and (p.get("countrycode") or "").upper() != filtre:
            continue
        c = (feat.get("geometry") or {}).get("coordinates") or [None, None]
        rows.append((
            p.get("name"), p.get("street"), p.get("postcode"), p.get("city") or p.get("locality"),
            p.get("county"), p.get("countrycode"), p.get("osm_value"),
            round(c[1], 6) if c[1] is not None else None,
            round(c[0], 6) if c[0] is not None else None,
        ))
    cols = ["nom", "rue", "code postal", "ville", "departement", "pays", "type", "lat", "lon"]
    return (
        "Source : API de recherche GISCO, adossee a OpenStreetMap - donnee "
        "collaborative, PAS le referentiel administratif.\n"
        f"Recherche : {q!r}" + (f" restreinte a {filtre}" if filtre else "") + "\n\n"
        + _render(rows, cols)
        + "\n\nPour le rattachement administratif ferme, reprends lat/lon dans gisco_localiser."
    )


@mcp.tool(annotations=_hints("Distance a vol d'oiseau", SUR_LA_SOURCE))
@_guard
def gisco_distance(depart: str, arrivee: str) -> str:
    """Distance a vol d'oiseau entre deux points, deux codes postaux, ou l'un et l'autre.

    ATTENTION : c'est une orthodromie, pas un kilometrage routier. L'ecart
    courant est de 20 a 30 %, davantage des qu'il y a un relief, un fleuve ou un
    bras de mer. Pour un cout de transport c'est un ordre de grandeur, jamais
    une base de facturation.

    Chaque extremite s'ecrit soit 'PAYS:CODE' (FR:60110, NL:3204XD), soit
    'lat,lon' (49.2028,2.1208).
    """
    def resoudre(texte: str) -> tuple[float, float, str]:
        t = (texte or "").strip()
        if not t:
            raise GiscoError("Extremite vide.")
        m = re.fullmatch(r"\s*(-?\d+(?:[.,]\d+)?)\s*[,;]\s*(-?\d+(?:[.,]\d+)?)\s*", t)
        if m:
            return float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", ".")), t
        if ":" not in t:
            raise GiscoError(
                f"Extremite illisible : {t!r}. Ecris 'FR:60110' pour un code "
                "postal, ou '49.2028,2.1208' pour un point."
            )
        pays, code = t.split(":", 1)
        _besoin("pcode")
        variants = _country_variants(pays)
        conn = _open_db(readonly=True)
        try:
            row = conn.execute(
                "SELECT lat, lon, postcode, lau_name FROM pcode WHERE cntr_id IN ("
                + ",".join("?" * len(variants)) + ") AND pc_norm = ? AND lat IS NOT NULL",
                [*variants, _norm(code).replace(" ", "")],
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise GiscoError(
                f"Code postal introuvable dans le cache : {t}. gisco_couches() "
                "dit quels pays sont rapatries."
            )
        return float(row[0]), float(row[1]), f"{t} ({row[3]})"

    lat1, lon1, l1 = resoudre(depart)
    lat2, lon2, l2 = resoudre(arrivee)
    km = geo.haversine_km(lat1, lon1, lat2, lon2)
    return (
        f"{l1}  ->  {l2}\n"
        f"  depart  : lat {lat1:.5f}, lon {lon1:.5f}\n"
        f"  arrivee : lat {lat2:.5f}, lon {lon2:.5f}\n"
        f"  distance a vol d'oiseau : {km:.1f} km\n"
        "\nCe n'est PAS un kilometrage routier. Compter 20 a 30 % de plus par la "
        "route, davantage en relief ou de part et d'autre d'un bras de mer."
    )


@mcp.tool(annotations=_hints("Compter et repartir, cote serveur", SUR_LA_SOURCE))
@_guard
def gisco_repartition(
    couche: str,
    par: str = "pays",
    mesure: str = "nb",
    pays: str = "",
    limite: int = 40,
) -> str:
    """Compte et somme EN UN APPEL : combien de communes par region, de codes
    postaux par densite, quelle population par pays.

    C'est l'outil des questions en « combien », « repartition », « le plus ».
    L'agregation se fait dans le cache : une question qui couterait des milliers
    de lignes rendues tient en quelques dizaines.

    couche  nuts, lau, pcode, urau.
    par     ce qui regroupe : pays, nuts3, dgurba, categorie, niveau, source,
            commune. Toutes ne valent pas pour toutes les couches - l'outil le
            dit si le regroupement n'existe pas.
    mesure  'nb' pour compter, 'population' ou 'surface' pour sommer (lau
            seulement). Une somme est rendue avec le nombre de lignes SANS
            valeur : une somme sur une colonne a moitie vide n'est pas un total.
    """
    spec = _layer_spec(couche)
    nom = spec["nom"]
    table = spec["table"]
    _besoin(nom)

    groupes = {
        "nuts": {"pays": "cntr_code", "niveau": "levl_code", "source": "source"},
        "lau": {"pays": "cntr_code", "source": "source"},
        "pcode": {
            "pays": "cntr_id", "nuts3": "nuts3", "dgurba": "dgurba",
            "commune": "lau_name", "source": "source", "fua": "fua_id",
        },
        "urau": {"pays": "cntr_code", "categorie": "urau_catg", "nuts3": "nuts3", "source": "source"},
        "countries": {"ue": "eu_stat", "aele": "efta_stat"},
    }[table]
    cle = groupes.get(_norm(par))
    if cle is None:
        raise GiscoError(
            f"Regroupement '{par}' inconnu pour la couche {nom}. Valeurs : "
            + ", ".join(sorted(groupes))
        )

    mesures = {
        "nb": ("COUNT(*)", None),
        "population": ("SUM(pop)", "pop"),
        "surface": ("SUM(area_km2)", "area_km2"),
    }
    m = _norm(mesure)
    if m not in mesures:
        raise GiscoError(f"Mesure inconnue : {mesure}. Valeurs : nb, population, surface.")
    expr, colonne = mesures[m]
    if colonne and table not in ("lau", "urau"):
        raise GiscoError(
            f"La mesure '{mesure}' n'existe pas sur la couche {nom} : seule 'nb' "
            "y a un sens."
        )
    if colonne == "pop" and table != "lau":
        raise GiscoError("La population n'est portee que par la couche lau.")

    where, params = [], []
    pays_col = groupes.get("pays")
    variants = _country_variants(pays)
    if variants and pays_col:
        where.append(f"{pays_col} IN (" + ",".join("?" * len(variants)) + ")")
        params += list(variants)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    conn = _open_db(readonly=True)
    try:
        sql = (
            f"SELECT {cle}, {expr}, COUNT(*)"
            + (f", SUM(CASE WHEN {colonne} IS NULL OR {colonne} = 0 THEN 1 ELSE 0 END)" if colonne else "")
            + f" FROM {table}{clause} GROUP BY {cle} ORDER BY 2 DESC LIMIT ?"
        )
        rows = conn.execute(sql, [*params, int(limite)]).fetchall()
        total = conn.execute(f"SELECT {expr} FROM {table}{clause}", params).fetchone()[0]
        groupes_n = conn.execute(
            f"SELECT COUNT(DISTINCT {cle}) FROM {table}{clause}", params
        ).fetchone()[0]
    finally:
        conn.close()

    cols = [par, mesure, "lignes"] + (["sans valeur"] if colonne else [])
    out = [
        _provenance([nom]),
        f"Regroupement : {par} ({cle}) | mesure : {mesure}"
        + (f" | pays : {pays}" if pays else ""),
        f"{groupes_n} groupe(s), {min(limite, groupes_n)} affiches. Total sur le perimetre : {_fmt_num(total)}",
    ]
    if colonne:
        out.append(
            "La colonne 'sans valeur' compte les lignes vides OU a zero. Sur la "
            "population, un zero est une ABSENCE DE MESURE, pas un chiffre : une "
            "somme qui les compte pour zero sous-estime sans le dire. Au "
            "millesime LAU 2024, la France et l'Espagne sont entierement a zero "
            "- pour une somme de population sur ces deux pays, il faut le "
            "millesime 2023."
        )
    out += ["", _render(rows, cols)]
    return "\n".join(out)


@mcp.tool(annotations=_hints("Contour d'une entite", SUR_LA_SOURCE))
@_guard
def gisco_geometrie(couche: str, identifiant: str, resume: bool = True) -> str:
    """Rend le contour d'une unite en GeoJSON. SUR DEMANDE - c'est volumineux.

    resume=True (defaut) ne rend pas le contour mais sa carte d'identite : type,
    nombre de points, cadre englobant, point figuratif, poids qu'aurait le
    GeoJSON. C'est ce qu'il faut dans neuf cas sur dix. Un contour de commune au
    01M pese des dizaines de milliers de caracteres : le rendre dans la
    conversation coute cher pour rien, alors qu'un export l'ecrit dans un
    fichier.
    """
    spec = _layer_spec(couche)
    _besoin(spec["nom"])
    conn = _open_db(readonly=True)
    try:
        row = conn.execute(
            f"SELECT {spec['cle']}, geom FROM {spec['table']} WHERE {spec['cle']} = ?",
            (identifiant.strip(),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise GiscoError(
            f"Aucune unite '{identifiant}' dans la couche {spec['nom']}. "
            "Cherche-la d'abord par son nom."
        )
    if not row[1]:
        raise GiscoError(f"L'unite {identifiant} n'a pas de geometrie dans le cache.")
    g = geo.wkb_to_geojson(row[1])
    texte = json.dumps(g, separators=(",", ":"))
    if not resume:
        if len(texte) > 200_000:
            raise GiscoError(
                f"Ce contour pese {len(texte)} caracteres : trop pour la "
                "conversation. Passe par gisco_export_csv, qui l'ecrit dans un "
                "fichier avec une colonne GeometryJson."
            )
        return texte

    def compte(node) -> int:
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)):
                return 1
            return sum(compte(x) for x in node)
        return 0

    b = geo.bbox(g)
    cx, cy = geo.centroid(g)
    return (
        f"{spec['nom']} / {row[0]}\n"
        f"  type            : {g['type']}\n"
        f"  points          : {compte(g.get('coordinates'))}\n"
        f"  cadre englobant : lon {b[0]:.5f} a {b[2]:.5f} | lat {b[1]:.5f} a {b[3]:.5f}\n"
        f"  point figuratif : lat {cy:.5f}, lon {cx:.5f}\n"
        f"  poids en GeoJSON: {len(texte)} caracteres\n"
        "\nLe point figuratif est le centroide du plus grand anneau. Ce n'est ni "
        "le chef-lieu, ni forcement un point interieur sur une commune en "
        "croissant.\nresume=False rend le contour lui-meme, s'il tient."
    )


# --------------------------------------------------------------------------
# SQL sur le cache
# --------------------------------------------------------------------------

_SQL_INTERDIT = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
    re.IGNORECASE,
)


def _guard_sql(sql: str) -> str:
    clean = (sql or "").strip().rstrip(";").strip()
    if not clean:
        raise GiscoError("Requete vide.")
    if ";" in clean:
        raise GiscoError(
            "Une seule instruction a la fois : le point-virgule est refuse."
        )
    if not re.match(r"^(select|with)\b", clean, re.IGNORECASE):
        raise GiscoError("Seuls SELECT et WITH sont acceptes : le cache est en lecture seule.")
    if _SQL_INTERDIT.search(clean):
        raise GiscoError("Mot-cle d'ecriture refuse : le cache est en lecture seule.")
    return clean


@mcp.tool(annotations=_hints("Schema du cache local", HORS_LIGNE))
@_guard
def gisco_tables() -> str:
    """Les tables du cache et leurs colonnes - pour ecrire un SQL juste.

    Repond hors ligne. N'invente jamais un nom de colonne : lis-le ici.
    """
    lignes = ["Tables du cache local (SQLite), interrogeables par gisco_sql :", ""]
    if not _cache_db().is_file():
        lignes.append("(le cache n'existe pas encore - lance gisco_sync)")
        lignes.append("")
    for table in TABLES:
        cols = re.findall(
            rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", SCHEMA, re.DOTALL
        )
        noms = []
        if cols:
            for part in cols[0].split(","):
                part = part.strip()
                if part:
                    noms.append(part.split()[0])
        n = ""
        if _cache_db().is_file():
            conn = _open_db(readonly=True)
            try:
                n = f" - {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]} lignes"
            except sqlite3.DatabaseError:
                n = ""
            finally:
                conn.close()
        lignes.append(f"{table}{n}")
        lignes.append("    " + ", ".join(noms))
    lignes += [
        "",
        "La colonne 'geom' porte la geometrie en WKB binaire : ne la selectionne "
        "pas en SQL, passe par gisco_geometrie. Les colonnes min_lon / min_lat / "
        "max_lon / max_lat portent le cadre englobant, elles, sont exploitables.",
        "'nom_norm' et 'pc_norm' sont les versions minuscules sans accent : c'est "
        "sur elles qu'on filtre un nom, pas sur la colonne d'affichage.",
        "'source' vaut GISCO ou ONS. Un decompte qui melange les deux sans le "
        "dire n'est pas comparable d'un pays a l'autre.",
    ]
    return "\n".join(lignes)


@mcp.tool(annotations=_hints("Lecture SQL sur le cache local", HORS_LIGNE))
@_guard
def gisco_sql(sql: str, limite: int = 200) -> str:
    """Interroge le cache local en SQL. Lecture seule.

    C'est le chemin des croisements que les outils dedies ne couvrent pas :
    joindre les codes postaux aux communes, compter les communes par tranche de
    population, comparer deux pays. Lis d'abord gisco_tables() : on n'invente
    pas un nom de colonne.
    """
    clean = _guard_sql(sql)
    conn = _open_db(readonly=True)
    try:
        cur = conn.execute(clean)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(int(limite))
        reste = cur.fetchone() is not None
    except sqlite3.DatabaseError as exc:
        raise GiscoError(f"SQL refuse par SQLite : {exc}") from exc
    finally:
        conn.close()
    out = [f"SQL : {clean}", f"{len(rows)} ligne(s) rendues."]
    if reste:
        out.append(
            f"ATTENTION : la requete rendait PLUS de {limite} lignes. Releve "
            "'limite', ou passe par gisco_export_csv."
        )
    out += ["", _render(rows, cols)]
    return "\n".join(out)


# --------------------------------------------------------------------------
# Export CSV
# --------------------------------------------------------------------------

def _ecrire_csv(cols: list[str], rows: Iterable[tuple], nom_fichier: str) -> tuple[pathlib.Path, int]:
    dossier = _export_dir()
    if any(m in str(dossier).lower() for m in SYNCED_MARKERS):
        raise GiscoError(
            f"Le dossier d'export ({dossier}) est dans un espace SYNCHRONISE. Un "
            "CSV depose la part chez toute l'equipe, et la base de connaissance "
            "ne porte pas de donnees. Change GISCO_EXPORT_DIR."
        )
    try:
        dossier.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GiscoError(f"Dossier d'export inutilisable ({dossier}) : {exc}") from exc
    nom = (nom_fichier or "").strip() or (
        "gisco_" + dt.datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".csv"
    )
    if not nom.lower().endswith(".csv"):
        nom += ".csv"
    cible = dossier / pathlib.Path(nom).name
    n = 0
    try:
        # newline="" et non write_text : sans lui, le meme code produit des fins
        # de ligne differentes selon le poste. utf-8-sig pour qu'Excel FR ouvre
        # le fichier sans passer par l'assistant d'import.
        with cible.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(cols)
            for row in rows:
                writer.writerow(row)
                n += 1
    except OSError as exc:
        raise GiscoError(f"Ecriture impossible dans {cible} : {exc}") from exc
    return cible, n


@mcp.tool(annotations=_hints("Exporter en CSV (sur demande explicite)", SUR_LA_SOURCE, idempotent=False))
@_guard
def gisco_export_csv(
    couche: str,
    pays: str = "",
    nom: str = "",
    niveau: int = -1,
    prefixe: str = "",
    nuts3: str = "",
    dgurba: int = 0,
    categorie: str = "",
    avec_geometrie: bool = False,
    fichier: str = "",
) -> str:
    """Exporte en CSV le perimetre exact demande. SUR DEMANDE EXPLICITE.

    Contrairement a un rendu en conversation, qui s'arrete au budget
    d'affichage, l'export ecrit TOUTES les lignes du perimetre. C'est la seule
    sortie acceptable pour un controle, une etude de plan ou une piece jointe :
    sans elle, on recopie a la main ce qui a ete affiche, et c'est la que les
    chiffres se deforment.

    couche          nuts, lau, pcode, urau, countries.
    pays            code Eurostat. Vide = tous les pays de la couche.
    nom             filtre sur le nom, partiel et sans accent.
    niveau          pour nuts : 0 a 3. -1 = tous.
    prefixe         pour pcode : debut de code postal.
    nuts3           pour pcode et urau : un code NUTS3.
    dgurba          pour pcode : 1 dense, 2 intermediaire, 3 peu peuple.
    categorie       pour urau : C, K ou F.
    avec_geometrie  ajoute une colonne GeometryJson avec le contour GeoJSON.
                    Le fichier devient tres lourd - c'est utile pour Power BI,
                    inutile pour un tableur.
    fichier         nom voulu. Par defaut un nom horodate.

    Le fichier est ecrit en CSV point-virgule, UTF-8 avec BOM : il s'ouvre dans
    Excel FR sans assistant d'import. Il atterrit dans le dossier local du
    connecteur, JAMAIS dans la bibliotheque d'equipe.
    """
    spec = _layer_spec(couche)
    table = spec["table"]
    _besoin(spec["nom"])

    colonnes = {
        "nuts": ["nuts_id", "levl_code", "cntr_code", "name_latn", "nuts_name",
                 "mount_type", "urbn_type", "coast_type", "source", "edition"],
        "lau": ["gisco_id", "cntr_code", "lau_id", "lau_name", "pop", "pop_dens",
                "area_km2", "source", "edition"],
        "pcode": ["pc_cntr", "postcode", "cntr_id", "lau_name", "lau_id", "nuts3",
                  "dgurba", "fua_id", "city_id", "lon", "lat", "source", "edition"],
        "urau": ["urau_code", "urau_catg", "cntr_code", "urau_name", "city_cptl",
                 "fua_code", "area_km2", "nuts3", "source", "edition"],
        "countries": ["cntr_id", "name_engl", "name_fren", "iso3_code", "capt",
                      "eu_stat", "efta_stat", "cc_stat", "source", "edition"],
    }[table]

    pays_col = {"nuts": "cntr_code", "lau": "cntr_code", "pcode": "cntr_id",
                "urau": "cntr_code", "countries": "cntr_id"}[table]
    where, params = [], []
    variants = _country_variants(pays)
    if variants:
        where.append(f"{pays_col} IN (" + ",".join("?" * len(variants)) + ")")
        params += list(variants)
    if nom.strip() and table != "pcode":
        where.append("nom_norm LIKE ?")
        params.append(f"%{_norm(nom)}%")
    if nom.strip() and table == "pcode":
        where.append("lower(lau_name) LIKE ?")
        params.append(f"%{_norm(nom)}%")
    if niveau >= 0 and table == "nuts":
        where.append("levl_code = ?")
        params.append(int(niveau))
    if prefixe.strip() and table == "pcode":
        where.append("pc_norm LIKE ?")
        params.append(_norm(prefixe).replace(" ", "") + "%")
    if nuts3.strip() and table in ("pcode", "urau"):
        where.append("nuts3 = ?")
        params.append(nuts3.strip().upper())
    if dgurba and table == "pcode":
        where.append("dgurba = ?")
        params.append(int(dgurba))
    if categorie.strip() and table == "urau":
        where.append("urau_catg = ?")
        params.append(categorie.strip().upper())

    if avec_geometrie and table == "pcode":
        avec_geometrie = False  # la couche est ponctuelle : lon/lat suffisent

    select = list(colonnes) + (["geom"] if avec_geometrie else [])
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    conn = _open_db(readonly=True)
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM {table}{clause}", params).fetchone()[0]
        cur = conn.execute(
            f"SELECT {', '.join(select)} FROM {table}{clause} ORDER BY {spec['cle']}", params
        )

        def lignes() -> Iterator[tuple]:
            while True:
                lot = cur.fetchmany(BATCH)
                if not lot:
                    return
                for row in lot:
                    if avec_geometrie:
                        blob = row[-1]
                        try:
                            gj = json.dumps(geo.wkb_to_geojson(blob), separators=(",", ":")) if blob else ""
                        except geo.GeoError:
                            gj = ""
                        yield (*row[:-1], gj)
                    else:
                        yield row

        entetes = list(colonnes) + (["GeometryJson"] if avec_geometrie else [])
        defaut = f"gisco_{spec['nom']}_{(pays or 'tous').lower()}_{dt.datetime.now():%Y-%m-%d_%H%M%S}.csv"
        cible, n = _ecrire_csv(entetes, lignes(), fichier or defaut)
    finally:
        conn.close()

    filtres = json.dumps(
        {k: v for k, v in {
            "pays": pays, "nom": nom, "niveau": niveau if niveau >= 0 else "",
            "prefixe": prefixe, "nuts3": nuts3, "dgurba": dgurba or "",
            "categorie": categorie,
        }.items() if v not in ("", None)},
        ensure_ascii=False,
    )
    out = [
        f"Export termine : {cible}",
        f"{n} ligne(s), {len(entetes)} colonne(s).",
        _provenance([spec["nom"]]),
        f"Filtres appliques : {filtres or '(aucun - la couche entiere)'}",
    ]
    if n != total:
        out.insert(0, f"ATTENTION : {total} lignes attendues, {n} ecrites. Verifie le fichier.")
    else:
        out.append("Perimetre complet : toutes les lignes du filtre sont dans le fichier.")
    if table == "lau":
        out.append(
            "Rappel : POP vaut 0 - et non vide - pour les pays hors Union, et "
            "reste vide pour le Royaume-Uni, que l'ONS ne publie pas ici."
        )
    return "\n".join(out)


@mcp.tool(annotations=_hints("Exporter le resultat d'un SQL en CSV", HORS_LIGNE, idempotent=False))
@_guard
def gisco_export_sql(sql: str, fichier: str = "", max_lignes: int = 500000) -> str:
    """Exporte en CSV le resultat d'une requete SQL sur le cache. SUR DEMANDE.

    C'est l'export des croisements : ce que gisco_export_csv ne sait pas
    exprimer en filtres, une requete le dit. Lis gisco_tables() d'abord.
    """
    clean = _guard_sql(sql)
    conn = _open_db(readonly=True)
    try:
        cur = conn.execute(clean)
        cols = [d[0] for d in cur.description]
        if "geom" in cols:
            raise GiscoError(
                "La colonne 'geom' est binaire : elle n'a pas de sens dans un "
                "CSV. Retire-la de la requete, ou passe par gisco_export_csv "
                "avec avec_geometrie=True."
            )
        rows = cur.fetchmany(int(max_lignes))
        reste = cur.fetchone() is not None
    except sqlite3.DatabaseError as exc:
        raise GiscoError(f"SQL refuse par SQLite : {exc}") from exc
    finally:
        conn.close()
    defaut = f"gisco_sql_{dt.datetime.now():%Y-%m-%d_%H%M%S}.csv"
    cible, n = _ecrire_csv(cols, rows, fichier or defaut)
    out = [f"Export termine : {cible}", f"{n} ligne(s), {len(cols)} colonne(s).", f"SQL : {clean}"]
    if reste:
        out.insert(
            0,
            f"ATTENTION : la requete rendait PLUS de {max_lignes} lignes, le "
            "fichier est TRONQUE. Releve max_lignes, ou resserre la requete.",
        )
    else:
        out.append("Resultat complet : la requete ne rendait pas plus de lignes.")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

HELP = """\
MCP gisco - referentiel geographique europeen (Eurostat GISCO), lecture seule.

  python server.py            mode serveur MCP (stdio), lance par Claude Code
  python server.py doctor     diagnostic de configuration et de connexion
  python server.py --help     cette aide

Aucun secret n'est necessaire : GISCO est un service public ouvert. La seule
configuration utile est l'emplacement du cache et des exports - voir README.md.
"""


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if arg == "doctor":
        rapport = gisco_doctor()
        print(rapport)
        return 0 if "Acces GISCO       : OK" in rapport else 1
    if arg:
        print(f"Argument inconnu : {arg}\n")
        print(HELP)
        return 2
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
