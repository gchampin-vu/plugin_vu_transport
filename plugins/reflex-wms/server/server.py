#!/usr/bin/env python3
"""
MCP reflex-wms : interrogation en LECTURE SEULE du WMS Reflex de Vente-unique.

Trois usages :
    python server.py            # mode serveur MCP (stdio) - lance par Claude Code
    python server.py doctor     # diagnostic de configuration et de connexion
    python server.py --help

Ce que ce connecteur apporte, et qui n'existe nulle part ailleurs : le MLD
Hardis des 1 986 tables Reflex est EMBARQUE dans le serveur, sous forme d'un
catalogue compresse de 536 Ko (server/catalog/reflex_mld.jsonl.gz, 48 534
colonnes avec leur libelle, leur type et leur rang dans la cle). Les outils de
schema repondent donc hors ligne, en quelques millisecondes, sans requete de
decouverte sur la production. C'est ce qui permet de partir d'une question en
francais et d'arriver a une requete T-SQL juste : le nom de colonne ne
s'invente pas, il se lit.

LECTURE SEULE PAR CONSTRUCTION, a quatre niveaux :

1. Aucun outil n'ecrit. Il n'y a ni INSERT, ni UPDATE, ni procedure stockee.
2. Toute requete passe par un analyseur qui refuse ce qui n'est pas un SELECT
   ou une CTE - commentaires et litteraux retires avant analyse, pour qu'un
   mot-cle cache dans une chaine ne serve pas de passe-droit.
3. La session s'ouvre en READ UNCOMMITTED : le connecteur ne pose aucun verrou
   sur une base de production, meme si la requete oublie WITH (NOLOCK).
4. Le compte utilise est celui du poste (authentification Windows par defaut).
   Ses droits sont ceux de la personne, pas ceux du connecteur.

Ce que ce serveur ne fait PAS, et ne fera pas : debloquer un stock, solder une
preparation, corriger un emplacement. Une ecriture dans Reflex passe par
Reflex, par l'entrepot, ou par la Webfacto - pas par un agent.

Configuration : un fichier .env hors du vault, complete par la configuration
d'equipe de 08_ENGINE/04_mcp/00_config/reflex.shared.env. Voir README.md.
"""

from __future__ import annotations

import csv
import datetime as dt
import decimal
import functools
import gzip
import io
import json
import os
import pathlib
import re
import sys
import threading
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

try:
    import pyodbc
except ImportError:  # diagnostique proprement plutot que de planter au demarrage
    pyodbc = None  # type: ignore[assignment]

HERE = pathlib.Path(__file__).resolve().parent
CATALOG_FILE = HERE / "catalog" / "reflex_mld.jsonl.gz"
RECIPES_DIR = HERE / "recipes"

# --- Valeurs par defaut ---------------------------------------------------
#
# Relevees dans la competence d'equipe `reflex-mld` (Projets Claude/
# _Competences/reflex-mld/SKILL.md), qui fait foi sur les parametres de
# connexion. Elles sont surchargeables par le fichier d'equipe et par le poste.

DEFAULT_SERVER = "172.17.151.114"
DEFAULT_DATABASE = "RFXCAFPRDDAT"
# La base d'epuration. Reflex epure : ce qui est sorti de la base courante vit
# la, avec exactement le meme schema et les memes tables. Une question qui
# porte sur de l'historique et qui ne rend rien sur PRD n'est pas une question
# sans reponse - c'est une question posee a la mauvaise base.
DEFAULT_DATABASE_EPU = "RFXCAFPRDEPU"
DEFAULT_SCHEMA = "reflex"
DEFAULT_LOGIN_TIMEOUT_S = 10
DEFAULT_MAX_ROWS = 200
DEFAULT_EXPORT_MAX_ROWS = 100_000

# --- Les garde-fous -------------------------------------------------------
#
# Une base de production de WMS se lit a 40 millions de lignes par table. Une
# requete mal bornee n'y rend pas un mauvais resultat : elle occupe le serveur,
# fait attendre l'entrepot, et le poste qui l'a lancee reste bloque. Quatre
# protections, independantes les unes des autres :
#
#   1. Le gouverneur de cout. SQL Server ESTIME le cout du plan avant de
#      l'executer et REFUSE de demarrer au-dela du plafond. C'est la seule
#      protection qui agit avant que la premiere ligne ne soit lue - elle
#      coute une milliseconde et arrete un produit cartesien net.
#   2. Le delai d'execution, pose sur le pilote ODBC.
#   3. Un chien de garde cote client, qui annule le curseur si le pilote
#      n'honore pas le delai (le pilote "SQL Server" livre avec Windows est
#      ancien, on ne lui fait pas une confiance aveugle).
#   4. Une seule requete a la fois par serveur : un agent qui enchaine cinq
#      questions ne lance pas cinq balayages en parallele.
#
# S'y ajoute, cote analyse : WITH (NOLOCK) exige sur chaque table, et le refus
# d'une jointure sans condition.

DEFAULT_QUERY_TIMEOUT_S = 60
MAX_QUERY_TIMEOUT_S = 600
DEFAULT_EXPORT_TIMEOUT_S = 300

# Plafond de cout estime du plan. L'unite est interne a SQL Server ; comme
# repere, une lecture indexee coute quelques unites, un balayage complet de
# HLPRPLP plusieurs milliers. 5000 laisse passer les requetes metier lourdes
# (la jointure preparations x chargements de l'equipe) et arrete les plans
# catastrophiques. 0 desactive le gouverneur.
DEFAULT_COST_LIMIT = 5000

# Plafond de caracteres rendus dans la conversation par un outil de liste.
RENDER_MAX_CHARS = 24_000

# Pilotes ODBC essayes, du plus recent au plus ancien. Le dernier - "SQL
# Server" - est livre avec Windows depuis toujours : c'est lui qui garantit
# qu'aucun logiciel n'est a installer en plus du plugin. Les "ODBC Driver NN"
# sont meilleurs (TLS moderne, types recents) mais ne sont pas partout.
DRIVER_PREFERENCE = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)

# Dossiers synchronises : un mot de passe n'y vit pas.
SYNCED_MARKERS = ("/onedrive", "cafom", "sharepoint", "dropbox", "google drive")


class ConfigError(RuntimeError):
    pass


class ReflexError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Configuration : le fichier .env du poste
# --------------------------------------------------------------------------

_ENV_LOADED_FROM: str = ""
_ENV_LOAD_ERROR: str = ""
_ENV_DONE: bool = False
_ENV_FILE_KEYS: set[str] = set()


def _is_synced(path: pathlib.Path) -> bool:
    flat = str(path).replace("\\", "/").lower()
    return any(marker in flat for marker in SYNCED_MARKERS)


def _packaged_python() -> str:
    """Detecte un interpreteur Windows empaquete. Rend la raison, ou "".

    Un Python livre par le Microsoft Store ou par le Python Manager tourne dans
    un conteneur d'application, et ses processus enfants heritent d'une vue
    virtualisee de %LOCALAPPDATA%. Un .env pose la par PowerShell peut donc
    etre invisible pour ce serveur, sans aucune erreur. On le detecte pour le
    dire, au lieu de rapporter "absent" et d'envoyer chercher un fichier qui
    est bien la. Constate sur le connecteur shiptify, meme cause, meme effet.
    """
    flat = str(pathlib.Path(sys.base_prefix)).replace("\\", "/").lower()
    for marker in ("/windowsapps/", "/packages/pythonsoftwarefoundation", "/python/pythoncore-"):
        if marker in flat:
            return f"interpreteur empaquete detecte ({sys.base_prefix})"
    return ""


def _candidate_env_files() -> list[pathlib.Path]:
    """Emplacements de .env essayes, dans l'ordre."""
    out: list[pathlib.Path] = []
    explicit = (os.environ.get("REFLEX_ENV_FILE") or "").strip()
    if explicit:
        out.append(pathlib.Path(explicit))
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        out.append(pathlib.Path(plugin_data) / "reflex.env")
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    out.append(pathlib.Path(base) / "reflex-mcp" / "reflex.env")
    out.append(pathlib.Path.home() / ".reflex-mcp" / "reflex.env")
    out.append(HERE / "reflex.env")
    return out


def _parse_env(text: str) -> dict[str, str]:
    """Parseur .env minimal : KEY=VALUE, # en commentaire, guillemets optionnels.

    Un mot de passe peut contenir n'importe quoi (%, *, /, #). La valeur n'est
    donc jamais decoupee sur un # sans espace devant, et des guillemets la
    preservent telle quelle.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].rstrip()
        if key:
            out[key] = val
    return out


def _load_env_file() -> None:
    """Charge le premier .env exploitable. Une variable du process non vide gagne.

    Le "non vide" n'est pas un detail : en mode plugin, la configuration arrive
    par l'environnement, et un champ laisse vide dans /plugin pose une variable
    VIDE. Un setdefault la considererait comme renseignee, et le .env du poste
    serait ignore en silence.
    """
    global _ENV_LOADED_FROM, _ENV_LOAD_ERROR, _ENV_DONE
    if _ENV_DONE:
        return
    _ENV_DONE = True
    for path in _candidate_env_files():
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if _is_synced(path):
            _ENV_LOAD_ERROR = (
                f"Un fichier reflex.env a ete trouve dans un dossier synchronise "
                f"({path}) et n'a PAS ete lu : il porterait un mot de passe de "
                "base de production sur le drive partage de l'equipe. Deplace-le "
                "vers %LOCALAPPDATA%\\reflex-mcp\\reflex.env (c'est ce que fait "
                "install.ps1), ou pointe un emplacement local avec REFLEX_ENV_FILE."
            )
            continue
        try:
            values = _parse_env(path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            _ENV_LOAD_ERROR = f"Lecture impossible de {path} : {exc}"
            continue
        for key, val in values.items():
            if not os.environ.get(key):
                os.environ[key] = val
                _ENV_FILE_KEYS.add(key)
        _ENV_LOADED_FROM = str(path)
        return


# --------------------------------------------------------------------------
# La configuration d'equipe : le fichier partage de 08_ENGINE
# --------------------------------------------------------------------------
#
# Ce fichier porte TOUT ce qui est identique sur tous les postes : l'adresse du
# serveur, les noms des deux bases, le schema, les plafonds - ET l'identifiant
# de service `query` de l'equipe.
#
# PORTEE ELARGIE LE 2026-09-01, sur decision explicite de Guillaume Champin.
# Deux raisons, et la premiere ne laisse pas le choix :
#
#   1. l'instance Reflex REFUSE l'authentification Windows des comptes du
#      domaine CAFOM (erreur 18456, constatee le 2026-08-28). L'acces passe
#      donc obligatoirement par un compte SQL ;
#   2. il n'y en a qu'un, il est en lecture, et toute l'equipe l'a deja. Le
#      faire ressaisir vingt-six fois n'ajouterait aucune protection - seulement
#      vingt-six mises en service qui echouent et une rotation impossible a
#      propager.
#
# Meme raisonnement que la cle de service Shiptify, meme decision.
#
# CE QUE CA IMPLIQUE, ET QUI EST ASSUME : cette bibliotheque est lisible par les
# 26 espaces de 07_EQUIPE, donc le mot de passe l'est aussi. En echange, le
# remplacer se fait ici une fois et se propage a tous au prochain demarrage de
# session. Le jour ou cet acces devrait cesser d'etre partage - un compte
# nominatif, un prestataire externe, un audit - la reponse est la configuration
# du plugin sur le poste, qui passe DEVANT le fichier d'equipe (voir _env), pas
# le retrait de la ligne.
#
# La liste blanche RESTE, mais elle a change d'objet : ce n'est plus une
# barriere anti-secret, c'est un garde-fou contre la faute de frappe. Une
# variable mal orthographiee posee ici serait sinon ignoree en silence, et on
# chercherait longtemps pourquoi le reglage d'equipe ne prend pas.
#
# Ce qui n'a toujours pas sa place ici : un chemin local. REFLEX_EXPORT_DIR
# reste refuse - un chemin d'export valable sur un poste n'existe pas sur les
# vingt-cinq autres.

SHARED_FILE_NAME = "reflex.shared.env"
ENGINE_DIR_NAME = "08_ENGINE"
SHARED_SUBPATH = ("04_mcp", "00_config")

SHARED_ALLOWED_KEYS = frozenset(
    {
        "REFLEX_SERVER",
        "REFLEX_DATABASE",
        "REFLEX_DATABASE_EPU",
        "REFLEX_SCHEMA",
        "REFLEX_DRIVER",
        "REFLEX_AUTH",
        "REFLEX_ENCRYPT",
        "REFLEX_QUERY_TIMEOUT_S",
        "REFLEX_EXPORT_TIMEOUT_S",
        "REFLEX_LOGIN_TIMEOUT_S",
        "REFLEX_COST_LIMIT",
        "REFLEX_REQUIRE_NOLOCK",
        "REFLEX_MAX_ROWS",
        "REFLEX_EXPORT_MAX_ROWS",
        # L'identifiant de service de l'equipe. Autorises ici le 2026-09-01.
        "REFLEX_USER",
        "REFLEX_PASSWORD",
    }
)

# Ce qui reste REFUSE dans le fichier d'equipe. Le dire plutot que l'ignorer :
# une valeur posee la ne provoquerait aucune erreur ailleurs, et le poste
# ecrirait ses exports dans un dossier qui n'existe pas chez lui.
SHARED_FORBIDDEN_KEYS = frozenset({"REFLEX_EXPORT_DIR"})

# Les cles du fichier d'equipe dont la VALEUR ne s'affiche jamais, meme dans un
# diagnostic. Le NOM de la cle, lui, se dit : c'est ce qui permet de savoir
# d'ou vient la valeur active sans la reveler.
SHARED_SECRET_KEYS = frozenset({"REFLEX_PASSWORD"})

_SHARED_CACHE: dict[str, str] | None = None
_SHARED_LOADED_FROM: str = ""
_SHARED_REJECTED: list[str] = []
_SHARED_FORBIDDEN_SEEN: list[str] = []


def _engine_roots() -> list[pathlib.Path]:
    """Racines `08_ENGINE` plausibles, dans l'ordre de preference.

    Le plugin est installe depuis 08_ENGINE/03_plugins/..., mais Claude Code en
    fait une copie dans ~/.claude/plugins/. On ne peut donc pas se contenter de
    remonter depuis le code : on remonte quand meme (installation directe
    depuis la bibliotheque), et on complete par le profil utilisateur, ou
    OneDrive synchronise la bibliotheque d'equipe.
    """
    out: list[pathlib.Path] = []

    def add(path: pathlib.Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved not in out:
            out.append(resolved)

    explicit = (os.environ.get("VU_ENGINE_DIR") or "").strip()
    if explicit:
        add(pathlib.Path(explicit))

    starts = [pathlib.Path(__file__).resolve()]
    plugin_root = (os.environ.get("CLAUDE_PLUGIN_ROOT") or "").strip()
    if plugin_root:
        starts.append(pathlib.Path(plugin_root))
    for start in starts:
        for parent in start.parents:
            if parent.name == ENGINE_DIR_NAME:
                add(parent)
                break

    home = pathlib.Path.home()
    for pattern in ("CAFOM/*/" + ENGINE_DIR_NAME, "*/CAFOM/*/" + ENGINE_DIR_NAME):
        try:
            for hit in sorted(home.glob(pattern)):
                if hit.is_dir():
                    add(hit)
        except OSError:
            continue
    return out


def _shared_env_candidates() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    explicit = (os.environ.get("REFLEX_SHARED_ENV") or "").strip()
    if explicit:
        out.append(pathlib.Path(explicit))
    for root in _engine_roots():
        out.append(root.joinpath(*SHARED_SUBPATH, SHARED_FILE_NAME))
    return out


def _load_shared_env() -> dict[str, str]:
    """Lit le premier fichier d'equipe trouve, filtre par la liste blanche.

    Ne leve jamais : un fichier d'equipe absent, illisible ou mal rempli ne doit
    pas empecher le connecteur de tourner sur la configuration du poste.

    Les valeurs ne sont PAS versees dans os.environ : elles restent dans ce
    cache, et c'est _env qui va les chercher en dernier recours.
    """
    global _SHARED_CACHE, _SHARED_LOADED_FROM, _SHARED_REJECTED, _SHARED_FORBIDDEN_SEEN
    if _SHARED_CACHE is not None:
        return _SHARED_CACHE
    values: dict[str, str] = {}
    rejected: list[str] = []
    forbidden: list[str] = []
    for path in _shared_env_candidates():
        try:
            if not path.is_file():
                continue
            raw = _parse_env(path.read_text(encoding="utf-8-sig"))
        except OSError:
            continue
        for key, val in raw.items():
            upper = key.strip().upper()
            if upper in SHARED_FORBIDDEN_KEYS:
                forbidden.append(upper)
            elif upper in SHARED_ALLOWED_KEYS:
                if val.strip():
                    values[upper] = val.strip()
            else:
                rejected.append(upper)
        _SHARED_LOADED_FROM = str(path)
        break
    _SHARED_REJECTED = rejected
    _SHARED_FORBIDDEN_SEEN = forbidden
    _SHARED_CACHE = values
    return values


def _shared_report() -> list[str]:
    """Les lignes que setup_status et doctor affichent sur le fichier d'equipe."""
    shared = _load_shared_env()
    lines: list[str] = []
    if _SHARED_LOADED_FROM:
        lines.append(f"Config d'equipe      : {_SHARED_LOADED_FROM}")
        lines.append(
            "  reglages repris    : " + (", ".join(sorted(shared)) if shared else "(aucun)")
        )
        secrets = sorted(k for k in shared if k in SHARED_SECRET_KEYS)
        if secrets:
            lines.append(
                "  dont secrets       : "
                + ", ".join(secrets)
                + " (valeur jamais affichee ; retenue seulement si ce poste "
                "n'en definit pas)"
            )
    else:
        lines.append("Config d'equipe      : aucune (valeurs par defaut du serveur)")
        for path in _shared_env_candidates():
            lines.append(f"    absent  {path}")
    if _SHARED_FORBIDDEN_SEEN:
        lines.append("")
        lines.append(
            "  ALERTE : le fichier d'equipe porte "
            + ", ".join(sorted(set(_SHARED_FORBIDDEN_SEEN)))
            + ", qui y sont INTERDITS et ont ete ignores. Un chemin d'export "
            "valable sur un poste n'existe pas sur les vingt-cinq autres : "
            "REFLEX_EXPORT_DIR se regle dans /plugin, poste par poste."
        )
    if _SHARED_REJECTED:
        lines.append("")
        lines.append(
            "  ATTENTION : cles inconnues dans le fichier d'equipe, IGNOREES : "
            + ", ".join(sorted(set(_SHARED_REJECTED)))
            + ". Dans la quasi-totalite des cas c'est une faute de frappe : "
            "compare avec reflex.env.example. Une cle ignoree ne produit aucune "
            "erreur ailleurs - c'est ici, et seulement ici, que ca se voit."
        )
    return lines


def _env(name: str, default: str = "") -> str:
    """La valeur retenue pour un reglage, dans un ordre de priorite fixe.

    1. l'environnement du processus - la configuration du plugin, et le .env
       local que _load_env_file y a deja verse : ce que ce poste a decide ;
    2. le fichier d'equipe de 08_ENGINE - ce que l'equipe a decide ;
    3. la valeur par defaut du serveur.

    Le poste passe donc devant l'equipe, et non l'inverse.
    """
    _load_env_file()
    from_process = (os.environ.get(name) or "").strip()
    if from_process:
        return from_process
    from_team = _load_shared_env().get(name, "").strip()
    if from_team:
        return from_team
    return default.strip()


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _server() -> str:
    return _env("REFLEX_SERVER", DEFAULT_SERVER)


def _database(base: str = "") -> str:
    """La base ciblee. `base` vaut "" ou "prod" pour la courante, "epu" pour l'epuration.

    Les deux portent le meme schema et les memes tables : une requete ecrite
    pour l'une tourne sur l'autre sans modification. C'est ce qui permet de
    reposer telle quelle une question restee sans reponse sur la courante.
    """
    key = _norm(base).strip()
    if key in ("epu", "epuration", "archive", "historique"):
        return _env("REFLEX_DATABASE_EPU", DEFAULT_DATABASE_EPU)
    if key in ("", "auto", "prod", "prd", "courant", "courante", "dat"):
        return _env("REFLEX_DATABASE", DEFAULT_DATABASE)
    raise ReflexError(
        f"Base inconnue : {base!r}. Valeurs acceptees : 'auto' (defaut : "
        "epuration puis courante), 'epu' (la base d'epuration) ou 'prod' (la "
        "base courante)."
    )


def _base_cascade(base: str) -> list[str]:
    """Les bases a interroger, dans l'ordre.

    Le defaut est la cascade EPURATION PUIS COURANTE. C'est contre-intuitif -
    on s'attendrait a interroger d'abord la base vivante - et c'est pourtant le
    bon ordre pour ce que l'equipe demande a Reflex.

    La raison : les questions posees ici portent presque toujours sur quelque
    chose qui a DEJA eu lieu - une preparation partie, un conteneur recu, un
    chargement de la semaine derniere. Reflex epure en continu, donc cet
    historique migre vers la base d'epuration au fil du temps, et le moment ou
    il bascule n'est pas connu de celui qui pose la question. Chercher d'abord
    dans l'epuration, c'est trouver du premier coup le cas ancien, et ne payer
    la seconde lecture que pour le cas recent.

    Ce que ca coute, et qu'il faut savoir : quand l'epuration ne rend rien, la
    requete est executee DEUX fois. Sur une lecture bornee, c'est negligeable.
    Sur une extraction lourde dont on sait qu'elle porte sur du recent, poser
    base='prod' explicitement evite le premier passage.

    Piege a connaitre : la cascade se declenche sur ZERO LIGNE rendue. Une
    requete d'agregat - COUNT(*), SUM(...) sans GROUP BY - rend TOUJOURS une
    ligne, meme quand elle ne compte rien. Elle ne bascule donc jamais, et
    s'arrete sur le compte de l'epuration. Pour compter sur les deux, poser la
    question sur chaque base explicitement, ou grouper.
    """
    key = _norm(base).strip()
    if key in ("", "auto"):
        return ["epu", "prod"]
    return [key]


def _schema() -> str:
    return _env("REFLEX_SCHEMA", DEFAULT_SCHEMA)


def _query_timeout() -> int:
    return min(_int_env("REFLEX_QUERY_TIMEOUT_S", DEFAULT_QUERY_TIMEOUT_S), MAX_QUERY_TIMEOUT_S)


def _cost_limit() -> int:
    raw = _env("REFLEX_COST_LIMIT")
    if not raw:
        return DEFAULT_COST_LIMIT
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_COST_LIMIT


def _require_nolock() -> bool:
    return _env("REFLEX_REQUIRE_NOLOCK", "1").lower() not in ("0", "no", "non", "false")


def _auth_mode() -> str:
    mode = _env("REFLEX_AUTH", "trusted").lower()
    return "sql" if mode in ("sql", "login", "password") else "trusted"


def _export_dir() -> pathlib.Path:
    raw = _env("REFLEX_EXPORT_DIR")
    if raw:
        return pathlib.Path(raw).expanduser()
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        return pathlib.Path(plugin_data) / "exports"
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return pathlib.Path(base) / "reflex-mcp" / "exports"


# --------------------------------------------------------------------------
# Le catalogue MLD embarque
# --------------------------------------------------------------------------
#
# 1 985 tables, 48 534 colonnes, 536 Ko compresses. Charge une seule fois, a la
# premiere question de schema - pas au demarrage : une session qui ne pose
# aucune question de schema ne paie pas la lecture.

_CATALOG: dict[str, dict[str, Any]] | None = None
_CATALOG_META: dict[str, Any] = {}
_CATALOG_BY_PREFIX: dict[str, list[str]] = {}


def _catalog() -> dict[str, dict[str, Any]]:
    global _CATALOG, _CATALOG_META, _CATALOG_BY_PREFIX
    if _CATALOG is not None:
        return _CATALOG
    if not CATALOG_FILE.is_file():
        raise ConfigError(
            f"Catalogue MLD introuvable ({CATALOG_FILE}). Il fait partie du "
            "connecteur et se regenere depuis le MLD Hardis :\n"
            "  python server/tools/build_catalog.py \"<racine du MLD>\""
        )
    tables: dict[str, dict[str, Any]] = {}
    by_prefix: dict[str, list[str]] = {}
    try:
        with gzip.open(CATALOG_FILE, "rt", encoding="utf-8") as fh:
            for line in fh:
                record = json.loads(line)
                if "_meta" in record:
                    _CATALOG_META = record["_meta"]
                    continue
                tables[record["t"]] = record
                if record.get("p"):
                    by_prefix.setdefault(record["p"].upper(), []).append(record["t"])
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Catalogue MLD illisible ({CATALOG_FILE}) : {exc}") from exc
    _CATALOG = tables
    _CATALOG_BY_PREFIX = by_prefix
    return tables


TYPE_LABELS = {
    "A": "Alphanumeric -> varchar",
    "P": "Packed numeric -> numeric",
    "N": "Numerique etendu -> numeric",
    "B": "Binary -> smallint/numeric",
    "I": "Image -> image",
    "T": "TimeStamp -> datetime",
    "?": "type non documente",
}


def _norm(text: str) -> str:
    """Minuscules sans accent, pour une recherche qui pardonne l'accentuation.

    Les libelles du MLD sont accentues ("Preparation - En-tete"), les questions
    posees ne le sont pas toujours, et l'inverse est vrai aussi. Sans ce
    pliage, chercher "depot" ne trouve pas "Dépôt physique" - la table la plus
    demandee de la base.
    """
    table = str.maketrans(
        "àâäáãåçéèêëíìîïñóòôöõúùûüýÿÀÂÄÁÃÅÇÉÈÊËÍÌÎÏÑÓÒÔÖÕÚÙÛÜÝ",
        "aaaaaaceeeeiiiinooooouuuuyyAAAAAACEEEEIIIINOOOOOUUUUY",
    )
    return text.translate(table).lower()


# --------------------------------------------------------------------------
# Le garde-fou de lecture seule
# --------------------------------------------------------------------------

# Mots-cles refuses, cherches comme des mots entiers apres retrait des
# commentaires et des litteraux. INTO est dans la liste : SELECT ... INTO cree
# une table, c'est une ecriture qui ne commence pas par INSERT.
FORBIDDEN = (
    "insert", "update", "delete", "merge", "truncate", "drop", "alter",
    "create", "grant", "revoke", "deny", "backup", "restore", "shutdown",
    "reconfigure", "dbcc", "kill", "waitfor", "openrowset", "opendatasource",
    "openquery", "bulk", "into", "use", "exec", "execute",
)

_STRING = re.compile(r"'(?:[^']|'')*'")
_BRACKET = re.compile(r"\[[^\]]*\]")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9#@]*")


def _strip_for_analysis(sql: str) -> str:
    """Retire commentaires, litteraux et identifiants entre crochets.

    L'ordre compte : les commentaires d'abord, sinon un `--` a l'interieur
    d'une chaine ferait disparaitre la fin de la ligne. Les litteraux ensuite,
    pour qu'un WHERE PECDPO = 'DROP' ne soit pas pris pour un DROP. Les
    crochets enfin, parce que [delete] est un nom de colonne legal.
    """
    out = _BLOCK_COMMENT.sub(" ", sql)
    out = _LINE_COMMENT.sub(" ", out)
    out = _STRING.sub(" '' ", out)
    out = _BRACKET.sub(" x ", out)
    return out


def assert_read_only(sql: str) -> str:
    """Valide qu'une requete est une lecture, ou leve. Rend le SQL nettoye.

    Ce n'est pas la seule protection - la session tourne en READ UNCOMMITTED et
    le compte est celui du poste - mais c'est celle qui parle : elle dit
    pourquoi elle refuse, ce qui evite de chercher du cote de la base.
    """
    raw = (sql or "").strip()
    if not raw:
        raise ReflexError("Requete vide.")
    body = raw.rstrip().rstrip(";").strip()
    analysed = _strip_for_analysis(body)

    if ";" in analysed:
        raise ReflexError(
            "Une seule instruction par appel. Le point-virgule qui separe deux "
            "instructions est refuse : c'est par la qu'une lecture se "
            "transforme en ecriture. Une CTE (WITH ... AS (...) SELECT ...) "
            "est une seule instruction, elle passe."
        )

    words = [w.lower() for w in _WORD.findall(analysed)]
    if not words:
        raise ReflexError("Requete vide apres retrait des commentaires.")
    if words[0] not in ("select", "with"):
        raise ReflexError(
            f"Une requete doit commencer par SELECT ou WITH. Celle-ci commence "
            f"par '{words[0].upper()}'. Ce connecteur est en lecture seule : "
            "une ecriture dans Reflex passe par Reflex ou par la Webfacto, "
            "jamais par un agent."
        )
    hits = sorted({w for w in words if w in FORBIDDEN})
    if hits:
        detail = ""
        if "into" in hits:
            detail = (
                " (INTO est refuse meme derriere un SELECT : SELECT ... INTO "
                "cree une table.)"
            )
        raise ReflexError(
            "Mot-cle d'ecriture ou d'administration refuse : "
            + ", ".join(h.upper() for h in hits)
            + "." + detail
        )
    for word in words:
        if word.startswith(("sp_", "xp_")):
            raise ReflexError(
                f"Procedure systeme refusee : {word}. Seule une lecture de "
                "tables et de vues est autorisee."
            )
    _check_nolock(analysed)
    _check_joins(analysed)
    return body


# Les mots qui ne peuvent PAS etre un alias de table. Sans cette exclusion, le
# groupe d'alias avale le WITH de `FROM reflex.HLPRENP WITH (NOLOCK)` - la
# table sans alias etait alors signalee comme depourvue de NOLOCK, alors
# qu'elle en portait un. Trouve par test_offline.py.
_NOT_AN_ALIAS = (
    "with|on|where|group|order|having|union|except|intersect|"
    "inner|left|right|full|cross|outer|join|apply|option|for|into"
)

# Une reference de table dans un FROM ou un JOIN : le nom qualifie, un alias
# facultatif, puis - ou non - l'indication WITH (NOLOCK).
_TABLE_REF = re.compile(
    r"\b(?:from|join)\s+"
    r"(?P<table>[A-Za-z_][\w.]*)"                       # reflex.HLPRENP, ou une CTE
    rf"(?P<tail>(?:\s+(?:as\s+)?(?!(?:{_NOT_AN_ALIAS})\b)[A-Za-z_]\w*)?"  # alias facultatif
    r"(?:\s+with\s*\([^)]*\))?)",                       # indication facultative
    re.I,
)

# Une indication de table : WITH (NOLOCK), WITH (INDEX(...)), etc.
_TABLE_HINT = re.compile(r"\bwith\s*\([^)]*\)", re.I)


def _check_nolock(analysed: str) -> None:
    """Exige WITH (NOLOCK) sur chaque table du schema Reflex.

    La session tourne deja en READ UNCOMMITTED, donc rien ne serait verrouille
    meme sans. Ce controle a une autre raison d'etre, et c'est la bonne : une
    requete ecrite ici finit toujours par etre recopiee ailleurs - dans SSMS,
    dans une source Power BI, dans un script d'equipe - ou ce reglage de
    session n'existe pas. Une requete sans NOLOCK qui sort d'ici est une
    requete qui posera des verrous sur la production le jour ou quelqu'un la
    colle dans Power Query. On refuse donc a la source.

    Ne s'applique qu'aux tables prefixees par le schema : une CTE, une
    sous-requete ou une table systeme n'en prennent pas.
    """
    if not _require_nolock():
        return
    schema = _schema().lower()
    missing: list[str] = []
    for match in _TABLE_REF.finditer(analysed):
        table = match.group("table")
        if not table.lower().startswith(schema + "."):
            continue
        if "nolock" not in match.group("tail").lower():
            missing.append(table)
    if missing:
        raise ReflexError(
            "WITH (NOLOCK) manquant sur : "
            + ", ".join(sorted(set(missing)))
            + ".\nRegle d'equipe : chaque table lue le porte, sans exception. "
            "Ecris `FROM reflex.HLPRENP pe WITH (NOLOCK)`, et de meme sur "
            "chaque JOIN.\n"
            "Pourquoi c'est refuse et pas seulement signale : une requete "
            "ecrite ici est recopiee dans SSMS ou dans Power BI, ou la session "
            "n'est pas en READ UNCOMMITTED - et elle y posera des verrous sur "
            "la production de l'entrepot."
        )


def _has_top_level_comma(segment: str) -> bool:
    """Une virgule hors de toute parenthese ? Les autres ne separent pas des tables."""
    depth = 0
    for char in segment:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            return True
    return False


def _check_joins(analysed: str) -> None:
    """Refuse une jointure sans condition - la source des produits cartesiens.

    Un JOIN sans ON sur deux tables Reflex ne rend pas un mauvais resultat : il
    rend le produit des deux, soit des milliards de lignes, et il occupe le
    serveur jusqu'au delai. Le gouverneur de cout l'arreterait aussi, mais une
    seconde plus tard et avec un message que personne ne rattache a la cause.
    """
    low = analysed.lower()
    joins = len(re.findall(r"\bjoin\b", low))
    if not joins:
        # FROM a, b : la virgule est l'autre facon d'ecrire un produit, et la
        # plus discrete. Les indications de table sont retirees d'abord - leurs
        # parentheses masquaient la virgule dans la version precedente, et
        # `FROM t1 WITH (NOLOCK), t2 WITH (NOLOCK)` passait au travers. Trouve
        # par test_offline.py.
        for match in re.finditer(r"\bfrom\b(.*?)(?:\bwhere\b|\bgroup\b|\border\b|$)", low, re.S):
            segment = _TABLE_HINT.sub(" ", match.group(1))
            if _has_top_level_comma(segment):
                raise ReflexError(
                    "Jointure implicite par virgule dans le FROM. Ecris un "
                    "INNER JOIN ... ON explicite : sans condition, la requete "
                    "rend le produit des deux tables."
                )
        return
    ons = len(re.findall(r"\bon\b", low))
    if ons < joins - low.count("cross join"):
        raise ReflexError(
            f"{joins} jointure(s) pour {ons} condition(s) ON. Une jointure sans "
            "ON rend le produit des deux tables - des milliards de lignes sur "
            "Reflex. Complete la condition, ou ecris CROSS JOIN si le produit "
            "est vraiment voulu."
        )


def _validate_identifier(name: str, kind: str) -> str:
    """Un nom de table ou de colonne, valide pour interpolation.

    Une table ne peut pas etre passee en parametre lie a ODBC : elle est
    interpolee dans le SQL. On n'accepte donc que des lettres, des chiffres et
    l'underscore - de quoi ecrire HLPRENP et PECDPO, de quoi ecrire rien
    d'autre.
    """
    cleaned = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", cleaned):
        raise ReflexError(
            f"{kind} invalide : {name!r}. Attendu un nom simple, sans espace ni "
            "ponctuation (ex. HLPRENP, PECDPO)."
        )
    return cleaned


# --------------------------------------------------------------------------
# La connexion
# --------------------------------------------------------------------------

def _available_drivers() -> list[str]:
    if pyodbc is None:
        return []
    try:
        return list(pyodbc.drivers())
    except Exception:  # pragma: no cover - depend du poste
        return []


def _driver() -> str:
    """Le pilote ODBC retenu, ou leve en disant ce qui a ete trouve."""
    forced = _env("REFLEX_DRIVER")
    available = _available_drivers()
    if forced:
        if available and forced not in available:
            raise ConfigError(
                f"Pilote ODBC force a {forced!r}, absent de ce poste. "
                f"Pilotes presents : {', '.join(available) or '(aucun)'}."
            )
        return forced
    for candidate in DRIVER_PREFERENCE:
        if candidate in available:
            return candidate
    raise ConfigError(
        "Aucun pilote ODBC SQL Server sur ce poste. C'est inattendu : le "
        "pilote 'SQL Server' est livre avec Windows. Verifie avec "
        "`Get-OdbcDriver` en PowerShell, ou force le nom exact avec "
        "REFLEX_DRIVER.\nPilotes vus : "
        + (", ".join(available) or "(aucun)")
    )


def _conn_str(mask: bool = False, base: str = "") -> str:
    """La chaine de connexion, avec le mot de passe masque si demande."""
    driver = _driver()
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={_server()}",
        f"DATABASE={_database(base)}",
        # Identifie la session cote serveur : dans sp_who2, la ligne porte
        # "reflex-mcp" et non "Python". Un DBA qui voit une requete longue sait
        # d'ou elle vient.
        "APP=reflex-mcp",
    ]
    if _auth_mode() == "sql":
        user = _env("REFLEX_USER")
        password = _env("REFLEX_PASSWORD")
        if not user or not password:
            raise ConfigError(
                "REFLEX_AUTH=sql demande REFLEX_USER et REFLEX_PASSWORD, et "
                "l'un des deux manque.\n"
                "\n"
                "Dans le cas normal, les deux viennent de la configuration "
                "d'equipe, 08_ENGINE/04_mcp/00_config/reflex.shared.env, et il "
                "n'y a rien a saisir. S'ils manquent, c'est presque toujours "
                "que ce fichier n'a pas ete trouve : /reflex-setup dit ou il a "
                "ete cherche.\n"
                "\n"
                "Si la bibliotheque d'equipe n'est pas synchronisee sur ce "
                "poste, deux sorties : pointer le fichier avec "
                "REFLEX_SHARED_ENV, ou saisir compte et mot de passe dans "
                "/plugin > reflex-wms."
            )
        parts.append(f"UID={user}")
        parts.append("PWD=" + ("********" if mask else password))
    else:
        parts.append("Trusted_Connection=yes")

    # Les mots-cles de chiffrement ne sont compris que des pilotes recents. Le
    # pilote 'SQL Server' livre avec Windows rejette ce qu'il ne connait pas :
    # les ajouter systematiquement casserait le seul pilote garanti present.
    if driver.startswith("ODBC Driver"):
        encrypt = _env("REFLEX_ENCRYPT", "optional").lower()
        if encrypt in ("yes", "oui", "1", "true"):
            parts.append("Encrypt=yes")
            parts.append("TrustServerCertificate=yes")
        elif encrypt in ("no", "non", "0", "false"):
            parts.append("Encrypt=no")
        else:
            # Le pilote 18 chiffre par defaut et refuse un certificat auto-signe.
            # Reflex est sur le reseau interne, avec un certificat de ce type :
            # sans cette ligne, la connexion echoue sur un message de certificat
            # que personne ne rattache a la cause.
            parts.append("TrustServerCertificate=yes")
    return ";".join(parts) + ";"


def _connect(base: str = "", timeout_s: int = 0):
    if pyodbc is None:
        raise ConfigError(
            "Le module pyodbc n'est pas installe dans cet interpreteur "
            f"({sys.executable}). En mode plugin, bootstrap.py s'en charge au "
            "premier demarrage. En mode direct :\n"
            "  python -m pip install -r requirements.txt"
        )
    try:
        conn = pyodbc.connect(
            _conn_str(base=base),
            timeout=_int_env("REFLEX_LOGIN_TIMEOUT_S", DEFAULT_LOGIN_TIMEOUT_S),
            autocommit=True,
        )
    except Exception as exc:  # pyodbc.Error et ses variantes
        raise ReflexError(_explain_connect_error(exc, base)) from exc
    conn.timeout = timeout_s or _query_timeout()
    # READ UNCOMMITTED : le connecteur ne pose aucun verrou sur la production,
    # meme si la requete a oublie WITH (NOLOCK). C'est la garantie qui compte -
    # une convention d'ecriture s'oublie, un reglage de session non.
    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
    except Exception:  # pragma: no cover - ne doit pas empecher la lecture
        pass
    # Le gouverneur de cout : SQL Server refuse de DEMARRER un plan dont le
    # cout estime depasse le plafond. C'est le seul garde-fou qui agit avant
    # que la premiere ligne ne soit lue, et il ne coute rien.
    limit = _cost_limit()
    if limit:
        try:
            conn.execute(f"SET QUERY_GOVERNOR_COST_LIMIT {limit}")
        except Exception:  # pragma: no cover - droit refuse sur certains comptes
            pass
    return conn


def _explain_connect_error(exc: Exception, base: str = "") -> str:
    """Traduit l'erreur ODBC en cause probable, avec le geste qui repare."""
    text = str(exc)
    low = text.lower()
    lines = [f"Connexion a {_server()} / {_database(base)} impossible.", "", text, ""]
    if "login timeout" in low or "0x2749" in low or "tcp provider" in low or "timeout" in low:
        lines.append(
            "Cause la plus frequente : le serveur n'est pas joignable depuis ce "
            "poste. Reflex est sur le reseau interne - il faut etre au bureau "
            "ou sur le VPN. Verifie d'abord :"
        )
        lines.append(f"  Test-NetConnection {_server()} -Port 1433")
    elif "login failed" in low or "18456" in low:
        mode = _auth_mode()
        lines.append("Le serveur REPOND - le reseau va bien - mais il refuse le compte.\n")
        if mode == "trusted":
            lines.append(
                "Ce poste tente une authentification WINDOWS, et l'instance "
                "Reflex ne l'accepte pas pour les comptes du domaine CAFOM "
                "(constate le 2026-08-28). L'acces de l'equipe passe par le "
                "compte SQL de service.\n"
                "\n"
                "Il est pose dans la configuration d'equipe, "
                "08_ENGINE/04_mcp/00_config/reflex.shared.env, avec "
                "REFLEX_AUTH=sql. Si ce poste retombe sur 'trusted', c'est que "
                "ce fichier n'a pas ete trouve : /reflex-setup dit ou il a ete "
                "cherche. La bibliotheque d'equipe est-elle synchronisee ?"
            )
        else:
            lines.append(
                f"Ce poste presente le compte SQL '{_env('REFLEX_USER') or '?'}' "
                "et le serveur le rejette. Trois causes, par ordre de "
                "frequence :\n"
                "  - le mot de passe a change cote base et le fichier d'equipe "
                "n'a pas suivi. C'est la cause la plus probable, et la "
                "correction se fait UNE fois dans reflex.shared.env pour tout "
                "le monde ;\n"
                "  - ce poste surcharge le compte dans /plugin > reflex-wms "
                "avec une valeur perimee - la configuration du poste passe "
                "DEVANT celle de l'equipe ;\n"
                "  - le compte a ete desactive cote IT."
            )
        lines.append(
            "\nEn attendant, tout ce qui est hors ligne fonctionne : "
            "reflex_guide, reflex_tables, reflex_columns, reflex_find_column, "
            "reflex_prefix et reflex_recipes repondent sur le MLD embarque. Une "
            "requete peut donc etre preparee et relue sans acces a la base."
        )
    elif "data source name" in low or "im002" in low:
        lines.append(
            "Le pilote ODBC nomme n'existe pas sur ce poste. Laisse "
            "REFLEX_DRIVER vide pour que le connecteur choisisse, ou donne le "
            "nom exact rendu par `Get-OdbcDriver`."
        )
    elif "certificate" in low or "ssl" in low:
        lines.append(
            "Negociation TLS refusee. Pose REFLEX_ENCRYPT=no si le serveur "
            "interne ne chiffre pas, ou laisse le reglage par defaut qui "
            "accepte le certificat interne."
        )
    lines.append("")
    lines.append("Diagnostic complet : outil reflex_doctor.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Execution et rendu
# --------------------------------------------------------------------------

def _coerce(value: Any) -> Any:
    """Rend une valeur SQL Server lisible en CSV et en JSON.

    Le rstrip sur les chaines n'est pas cosmetique : Reflex vient d'AS/400 et
    ses colonnes de code sont a longueur fixe, remplies d'espaces a droite.
    Sans lui, un depot rendu vaut 'AMB' ici et 'AMB   ' la, et un
    rapprochement avec une autre source echoue sans qu'on voie pourquoi.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.rstrip()
    if isinstance(value, decimal.Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        head = bytes(value[:32]).hex()
        return f"0x{head}..." if len(value) > 32 else f"0x{head}"
    if isinstance(value, bool):
        return int(value)
    return value


# Une requete a la fois, par processus. Un agent qui enchaine cinq questions
# n'a aucune raison de lancer cinq balayages simultanes sur la production : ils
# se ralentissent mutuellement et le poste attend les cinq. Le refus est
# immediat et explicite, plutot qu'une attente muette.
_QUERY_LOCK = threading.Lock()


class _Watchdog:
    """Annule le curseur si le delai est depasse, quoi qu'en fasse le pilote.

    `conn.timeout` de pyodbc pose SQL_ATTR_QUERY_TIMEOUT, et le pilote est
    cense l'honorer. Le pilote "SQL Server" livre avec Windows date de 2000 :
    on ne lui fait pas une confiance aveugle sur ce point precis, parce que
    l'echec est justement celui qu'on veut eviter - un poste bloque sur une
    requete qui ne rend jamais la main. `Cursor.cancel()` est documente comme
    appelable depuis un autre fil, c'est exactement son usage.
    """

    def __init__(self, cursor, seconds: int) -> None:
        self.fired = False
        self._timer = threading.Timer(seconds + 5, self._cancel, args=(cursor,))
        self._timer.daemon = True

    def _cancel(self, cursor) -> None:
        self.fired = True
        try:
            cursor.cancel()
        except Exception:
            pass

    def __enter__(self) -> "_Watchdog":
        self._timer.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._timer.cancel()


def run_query(
    sql: str,
    max_rows: int,
    base: str = "",
    timeout_s: int = 0,
) -> tuple[list[str], list[list[Any]], bool]:
    """Execute une lecture. Rend (colonnes, lignes, tronque)."""
    body = assert_read_only(sql)
    budget = timeout_s or _query_timeout()
    if not _QUERY_LOCK.acquire(blocking=False):
        raise ReflexError(
            "Une requete Reflex est deja en cours dans cette session. Ce "
            "connecteur n'en execute qu'une a la fois, pour ne pas empiler des "
            "balayages sur la base de production. Attends qu'elle rende la "
            f"main - elle est bornee a {budget} s."
        )
    try:
        conn = _connect(base=base, timeout_s=budget)
        try:
            cursor = conn.cursor()
            with _Watchdog(cursor, budget) as watchdog:
                try:
                    cursor.execute(body)
                except Exception as exc:
                    if watchdog.fired:
                        raise ReflexError(_explain_timeout(budget)) from exc
                    raise ReflexError(_explain_query_error(exc)) from exc
                if cursor.description is None:
                    raise ReflexError(
                        "La requete n'a rendu aucun jeu de resultats. Un "
                        "connecteur en lecture seule attend un SELECT qui rend "
                        "des colonnes."
                    )
                columns = [d[0] for d in cursor.description]
                # max_rows + 1 : la ligne en trop ne sert qu'a savoir s'il y en
                # avait d'autres. Elle n'est pas rendue, mais elle evite
                # d'annoncer "200 lignes" pour un ensemble qui en compte 40 000.
                fetched = cursor.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            rows = [[_coerce(v) for v in row] for row in fetched[:max_rows]]
            if truncated:
                # Annuler explicitement : sans ca, fermer la connexion sur un
                # jeu de resultats a moitie lu laisse le serveur produire les
                # lignes restantes.
                try:
                    cursor.cancel()
                except Exception:
                    pass
            return columns, rows, truncated
        finally:
            try:
                conn.close()
            except Exception:
                pass
    finally:
        _QUERY_LOCK.release()


def run_query_cascade(
    sql: str,
    max_rows: int,
    base: str = "",
    timeout_s: int = 0,
) -> tuple[list[str], list[list[Any]], bool, str, list[str]]:
    """Execute la lecture sur les bases de la cascade, et s'arrete a la premiere
    qui rend quelque chose.

    Rend (colonnes, lignes, tronque, base_retenue, bases_essayees).

    Le cas ou toutes rendent zero ligne n'est pas un echec : c'est une reponse,
    et elle doit dire qu'on a bien regarde partout. On rend alors le resultat
    de la derniere base essayee, avec la liste de ce qui a ete tente.
    """
    bases = _base_cascade(base)
    # La liste rendue est celle des bases PREVUES, pas seulement de celles
    # atteintes. C'est ce qui permet a l'appelant de savoir qu'il etait en mode
    # cascade meme quand la premiere base a repondu - et donc d'avertir sur le
    # piege du referentiel. Une premiere version ne remontait que les bases
    # effectivement lues : quand l'epuration repondait du premier coup, la
    # cascade devenait invisible et l'avertissement ne partait jamais.
    prevues = [_database(b) for b in bases]
    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated = False
    retenue = bases[-1]
    for candidate in bases:
        columns, rows, truncated = run_query(sql, max_rows, base=candidate, timeout_s=timeout_s)
        retenue = candidate
        if rows:
            break
    return columns, rows, truncated, _database(retenue), prevues


def _cascade_note(base_retenue: str, tried: list[str], rows: list[list[Any]]) -> str:
    """La ligne qui dit quelle base a repondu. Jamais implicite.

    Un chiffre dont on ignore s'il vient de la base courante ou de l'epuration
    n'est pas citable : les deux ne couvrent pas la meme periode.
    """
    if len(tried) <= 1:
        return f"Base interrogee : {base_retenue}."
    if rows:
        note = (
            f"Base interrogee : {base_retenue} (cascade prevue : "
            + " puis ".join(tried)
            + f"). {base_retenue} a rendu des lignes, la cascade s'y est "
            "arretee - le perimetre temporel est donc celui de cette base."
        )
        # L'avertissement qui evite la reponse fausse la plus probable de la
        # cascade. Constate le 2026-09-01 sur HLDEPPP : l'epuration porte les
        # depots 001, AMB et MOR (Moreuil, un site ferme) et PAS AUV, alors que
        # la base courante porte AMB et AUV. Un referentiel lu depuis
        # l'epuration est une PHOTO ANCIENNE - il rend des lignes, donc la
        # cascade s'arrete la, et la reponse est fausse sans qu'aucune erreur
        # ne soit levee. C'est le prix de la regle, et il se paie en silence si
        # on ne le dit pas.
        if _norm(base_retenue) == _norm(_database("epu")):
            note += (
                "\nATTENTION si la question porte sur un REFERENTIEL - depots, "
                "articles, etats, transporteurs : l'epuration en porte une "
                "photo ancienne, qui peut contenir des entites fermees et "
                "ignorer les recentes. Refais la lecture avec base='prod' "
                "avant de citer une liste de referentiel."
            )
        return note
    return (
        "Aucune ligne, sur AUCUNE des bases essayees : "
        + " puis ".join(tried)
        + ". Le vide est donc constate des deux cotes, pas seulement sur la "
        "base courante. Si un resultat etait attendu, c'est le filtre qu'il "
        "faut revoir - un code depot, un format de reference, un top teste a "
        "'O' au lieu de '1'."
    )


def _explain_timeout(budget: int) -> str:
    return (
        f"Requete annulee : elle depassait le delai de {budget} s.\n\n"
        "Ce n'est pas une panne, c'est le garde-fou. Ce qui marche, dans "
        "l'ordre :\n"
        "  1. filtrer sur le depot (PECDPO, P1CDPO, RPCDPO selon la table) - "
        "c'est ce qui divise le plus ;\n"
        "  2. borner la periode sur le SIECLE de la date (PESLEF > 0 et "
        "PEALEF >= 26), pas sur la valeur reconstituee : un filtre pose sur "
        "RFX_DHB_DATE2DATETIME(...) empeche l'usage des index ;\n"
        "  3. compter avant de lister - un COUNT(*) dit si le perimetre tient ;\n"
        "  4. en dernier recours, relever REFLEX_QUERY_TIMEOUT_S, en sachant "
        f"que le plafond dur est {MAX_QUERY_TIMEOUT_S} s."
    )


def _explain_query_error(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    lines = ["La requete a ete refusee par SQL Server.", "", text, ""]
    if "invalid object name" in low or "208" in low:
        lines.append(
            f"Nom de table inconnu. Sur cette base les tables sont prefixees "
            f"`{_schema()}.` et portent leur nom physique court (reflex.HLPRENP), "
            "jamais `dbo.` ni le nom logique long (HL_PREPA_ENTETE n'existe pas "
            "en table). Verifie avec l'outil reflex_tables."
        )
    elif "invalid column name" in low or "207" in low:
        lines.append(
            "Nom de colonne inconnu. Ne l'invente pas : reflex_columns rend la "
            "liste exacte des colonnes de la table, avec leur libelle."
        )
    elif "string_agg" in low or "trim" in low or "concat_ws" in low:
        lines.append(
            "Fonction absente : cette instance est en SQL Server 2016 ou "
            "anterieur. Remplace STRING_AGG par STUFF + FOR XML PATH(''), TRIM "
            "par LTRIM(RTRIM(...)), CONCAT_WS par un + explicite."
        )
    elif "8649" in text or "query governor" in low or "gouverneur" in low:
        lines.append(
            f"Le gouverneur de cout a REFUSE de demarrer cette requete : SQL "
            f"Server estime son plan au-dela du plafond ({_cost_limit()}).\n"
            "C'est un garde-fou, et il a presque toujours raison - un plan "
            "estime aussi cher est un balayage complet, souvent une jointure "
            "sans condition assez selective. Regarde d'abord :\n"
            "  - manque-t-il un filtre sur le depot ?\n"
            "  - la jointure porte-t-elle bien sur la cle complete (activite, "
            "depot, millesime, numero) et pas seulement sur le numero ?\n"
            "  - le filtre de date porte-t-il sur les colonnes brutes (siecle, "
            "annee) plutot que sur RFX_DHB_DATE2DATETIME(...) ?\n"
            "Si la requete est bien celle qu'il faut et qu'elle est lourde par "
            "nature, releve REFLEX_COST_LIMIT en connaissance de cause."
        )
    elif "timeout" in low or "annul" in low or "canceled" in low:
        lines.append(_explain_timeout(_query_timeout()))
    return "\n".join(lines)


def _to_csv_text(columns: list[str], rows: list[list[Any]], delimiter: str = ";") -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def _render_rows(
    columns: list[str],
    rows: list[list[Any]],
    header: str,
    truncated: bool,
    max_chars: int = RENDER_MAX_CHARS,
) -> str:
    lines = [header, f"{len(rows)} ligne(s) rendues."]
    if truncated:
        lines.append(
            "ATTENTION : resultat tronque au plafond max_rows. Le compte "
            "ci-dessus n'est PAS un total : ne le cite pas comme un volume. "
            "Pour un total, demande un COUNT(*). Pour l'ensemble des lignes, "
            "resserre les filtres - ou passe par reflex_export_csv, mais "
            "seulement si l'utilisateur a demande un export."
        )
    if not rows:
        lines.append("(aucune ligne)")
        return "\n".join(lines)
    body = _to_csv_text(columns, rows)
    if len(body) > max_chars:
        kept: list[str] = []
        size = 0
        for line in body.splitlines():
            if size + len(line) > max_chars:
                break
            kept.append(line)
            size += len(line) + 1
        body = "\n".join(kept)
        lines.append(
            f"Rendu limite a {max(0, len(kept) - 1)} ligne(s) pour ne pas "
            "saturer la conversation. Selectionne moins de colonnes, ou "
            "resserre les filtres."
        )
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def _guard(fn):
    """Rend les erreurs de configuration et de base comme message lisible.

    functools.wraps n'est pas cosmetique : il pose __wrapped__, que
    inspect.signature suit. Sans lui, FastMCP lit la signature du wrapper et
    publie chaque outil avec deux parametres (args, kwargs) au lieu des vrais.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ConfigError, ReflexError) as exc:
            return f"ERREUR : {exc}"

    return wrapper


# --------------------------------------------------------------------------
# Le memo Reflex : ce qu'il faut savoir avant d'ecrire une requete
# --------------------------------------------------------------------------
#
# Ce texte n'est pas de la documentation decorative : c'est ce qui separe une
# requete qui tourne d'une requete qui tourne juste. Chaque point a coute une
# erreur reelle a quelqu'un de l'equipe, et la source est citee a chaque fois.

GUIDE_GENERAL = """\
REFLEX WMS - CE QU'IL FAUT SAVOIR AVANT D'ECRIRE UNE REQUETE

Base       : SQL Server {server}, schema `{schema}`
             {database} = la base courante
             {database_epu} = la base d'epuration (l'historique sorti de la
             courante). MEME schema, MEMES tables : une requete tourne sur
             l'une comme sur l'autre.

             PAR DEFAUT, UNE LECTURE INTERROGE L'EPURATION PUIS LA COURANTE, et
             s'arrete a la premiere qui rend des lignes. C'est contre-intuitif
             et c'est voulu : les questions posees ici portent presque toujours
             sur quelque chose qui a deja eu lieu, Reflex epure en continu, et
             le moment ou un dossier bascule n'est pas connu de celui qui pose
             la question. Chercher d'abord dans l'epuration trouve le cas
             ancien du premier coup.

             La base qui a repondu est TOUJOURS indiquee. Ne cite jamais un
             chiffre sans elle : les deux bases ne couvrent pas la meme
             periode. base='prod' ou base='epu' forcent une seule base.

             TROIS CHOSES A SAVOIR.

             1. UN REFERENTIEL SE LIT SUR base='prod', TOUJOURS. Depots,
                articles, etats, transporteurs : l'epuration en porte une photo
                ancienne. Mesure du 2026-09-01 sur HLDEPPP - l'epuration rend
                001, AMB et MOR (Moreuil, site ferme) et PAS AUV, la courante
                rend AMB et AUV. Le referentiel repond, donc la cascade s'y
                arrete, et la liste est fausse sans qu'aucune erreur ne soit
                levee. La cascade est faite pour l'HISTORIQUE d'un dossier, pas
                pour une table de codes.

             2. Quand l'epuration ne rend rien, la requete est executee DEUX
                fois - negligeable sur une lecture bornee, a eviter sur une
                extraction lourde qu'on sait recente (poser base='prod').

             3. La cascade se declenche sur ZERO LIGNE : un COUNT(*) sans
                GROUP BY rend toujours une ligne, donc il ne bascule jamais et
                s'arrete sur le compte de l'epuration.
Dialecte   : T-SQL, instance <= 2016. Pas de STRING_AGG, TRIM, CONCAT_WS,
             GREATEST/LEAST. A la place : STUFF + FOR XML PATH(''),
             LTRIM(RTRIM(...)), un + explicite.
MLD        : Hardis v{mld_version} ({mld_tables} tables), embarque dans ce
             connecteur - reflex_tables, reflex_columns, reflex_find_column.

LES SIX REGLES

1. Prefixe `{schema}.` sur chaque table. Jamais `dbo.` : il n'existe pas pour
   les tables Reflex.
2. Nom physique court (HLPRENP), jamais le nom logique long (HL_PREPA_ENTETE
   n'existe pas en table physique).
3. WITH (NOLOCK) SUR CHAQUE TABLE LUE, sans exception. Ce n'est pas une
   convention de style : une requete sans NOLOCK pose des verrous sur la base
   de production de l'entrepot le jour ou quelqu'un la recopie dans Power BI.
   Le connecteur REFUSE une requete qui l'oublie.
4. Filtrer d'abord sur le depot. Les volumetries sont importantes : PECDPO sur
   HLPRENP, P1CDPO sur HLPRPLP, RPCDPO sur HLRCPEP, EXCDPO sur HLEXPEP.
   Les deux depots sont AMB (Amblainville) et AUV (Montbeugny/Moulins).
5. Aucun nom de colonne ne s'invente. reflex_columns rend la liste exacte.
6. Un chiffre se cite avec son perimetre : depot, periode, filtres. Un chiffre
   sans perimetre est inutilisable en reunion.

ECRIRE UNE REQUETE QUI NE BLOQUE PAS LA PRODUCTION

Le connecteur borne ce qui sort : gouverneur de cout (le plan est refuse avant
execution s'il est trop cher), delai d'execution, une requete a la fois,
plafond de lignes. Mais un garde-fou qui se declenche est du temps perdu -
mieux vaut ecrire la requete bornee du premier coup :

  - Filtrer sur le depot AVANT tout. C'est ce qui divise le plus.
  - Borner la periode sur les colonnes de date BRUTES (PESLEF > 0 AND
    PEALEF >= 26), jamais sur RFX_DHB_DATE2DATETIME(...) : un filtre pose sur
    le resultat de la fonction empeche l'usage des index, et le plan bascule
    en balayage complet. Reconstituer la date dans le SELECT, la filtrer dans
    le WHERE sur les composants.
  - Joindre sur la CLE COMPLETE. Sur Reflex elle fait presque toujours quatre
    colonnes : activite, depot, millesime, numero. Joindre sur le seul numero
    marche sur un jeu d'essai et explose en production.
  - Compter avant de lister : un COUNT(*) dit en une seconde si le perimetre
    tient. C'est le reflexe qui evite la requete annulee.
  - Un SELECT DISTINCT sur une table large est un tri complet. S'en passer, ou
    le poser sur le moins de colonnes possible.

LES DATES - LE PIEGE N.1

Une date Reflex n'est pas une colonne, c'est CINQ colonnes : Siecle, Annee sur
2 chiffres, Mois, Jour, Heure au format HHMMSS. Les comparer entre elles ne
donne rien.

  REFLEX.RFX_DHB_DATE2DATETIME(<S>, <A>, <M>, <J>, <H>)   -> datetime

  -- date de lancement d'une preparation (HLPRENP, prefixe PE)
  REFLEX.RFX_DHB_DATE2DATETIME(pe.PESLEF, pe.PEALEF, pe.PEMLEF, pe.PEJLEF, pe.PEHLEF)

Test "date renseignee" : le SIECLE vaut 0 tant que l'evenement n'a pas eu lieu.
`pe.PESSOL = 0` = non soldee. `pe.PESLEF > 0` = lancee. C'est le filtre le plus
fiable, et il ne coute pas d'appel de fonction.

LES TOPS - LE PIEGE N.2

La doc Hardis annonce des tops a 'O' / 'N'. Chez Vente-unique ils valent '1' /
'0' (constate en prod le 2026-05-28). Un `WHERE col <> 'O'` laisse donc passer
toutes les lignes a '1' : bug silencieux, resultat faux, personne ne le voit.

  -- exclure le "oui"
  AND ISNULL(col, '0') NOT IN ('O', '1', 'Y')
  -- ne garder que le "oui"
  AND ISNULL(col, '0')     IN ('O', '1', 'Y')

Toute colonne dont la 3e lettre est T est un top : PETSOL, P1TSLO, RETRVA,
RPTRPS, VETGEX...

LA CONVENTION DE NOMMAGE DES COLONNES

Format XX + Y + ZZZ. XX = prefixe de la table (PE = HLPRENP, P1 = HLPRPLP).
Y = type : C code, N numero, T top, U utilisateur, Q quantite, P/V poids ou
volume, R reference externe, L libelle, et S/A/M/J/H pour les cinq morceaux
d'une date. ZZZ = la semantique (PRE preparation, LEF lancement effectif,
SOL soldage, SOR sortie de stock, CRE creation, MAJ mise a jour).

Lire une colonne inconnue dans une requete heritee : reflex_prefix('PE') dit de
quelle table elle sort, reflex_columns('HLPRENP', 'SOL') dit ce qu'elle vaut.

LES UNITES - valide en prod 2026-07

PEVATP (volume) est en dm3, PAS en m3 : diviser par 1000. PEPATP est en kg.
Un seuil volumetrique pose sans conversion est faux d'un facteur 1000.

POUR ALLER PLUS LOIN

reflex_guide('domaines')     la carte des tables par metier
reflex_guide('preparations') / ('receptions') / ('expeditions') / ('stock')
reflex_recipes()             les requetes deja validees en production
"""

GUIDE_DOMAINES = """\
CARTE DES TABLES PAR DOMAINE - les points d'entree, pas l'exhaustivite

PREPARATION (sortie, commande client)
  HLPRENP  PE  En-tete preparation          219 colonnes
  HLPRPLP  P1  Ligne detail preparation     165
  HLPRETP  PZ  Historique d'etat
  HLETPPP  T0  Referentiel des etats (T0CEPP -> T0LEPP)
  HLMANQP  M3  Manquant de preparation

RECEPTION
  HLRCPEP  RP  Reception previsionnelle - en-tete (ETA, conteneur attendu)
  HLRCPLP  R2  Reception previsionnelle - ligne
  HLRECPP  RE  Reception reelle - en-tete (dechargement, validation)

AVIS D'EXPEDITION (ASN fournisseur)
  HLAEXEP  VE  En-tete     HLAEXLP  VN  Ligne     HLAEXDP  VZ  Detail

EXPEDITION ET CHARGEMENT
  HLEXPEP  EX  Expedition (27 colonnes, PAS de tonnage)
  HLCHARP  CG  Chargement

STOCK ET MOUVEMENTS
  HLMVSTP  MS  Mouvement de stock
  HLMVAEP  MV  Mouvement physique a executer
  HLMVEXP  MX  Mouvement physique execute
  HLEMPLP  EM  Emplacement (90 colonnes)

REFERENTIELS
  HLARTIP  AR  Article        HLDEPPP  DP  Depot physique
  HLDEPLP  DL  Depot logique  HLDTRPP  TP  Transporteur (DMS)

FACTURATION TRANSPORTEUR COTE WMS (module DMS)
  HLDFAEP  DF  En-tete de facture transporteur
  HLDFALP  DF  Ligne de facture     HLDFADP  DF  Detail de ligne

Rien de tout cela n'est fige : reflex_tables('facture'), reflex_tables('rdv'),
reflex_find_column('conteneur') fouillent les 1 985 tables du MLD.
"""

GUIDE_PREPARATIONS = """\
PREPARATIONS - definitions validees

"En cours" = lancee, ni soldee, ni sortie de stock :
  WHERE pe.PESLEF > 0 AND pe.PESSOL = 0 AND pe.PESSOR = 0

"Non envoyee", au niveau ligne, demande TROIS conditions - la troisieme est
celle qu'on oublie, et sans elle on ramasse des preparations completes qui
n'attendent que leur expedition :
  ligne non soldee, ligne non sortie de stock, ET P1QPRE < P1QAPR.

Jointure en-tete / ligne : P1NANN = PENANN AND P1NPRE = PENPRE. Ajouter le
depot si on filtre dessus.

Colonnes utiles de HLPRENP : PENPRE (n. preparation), PENANN (millesime),
PERODP (reference ODP), PENCOM (n. commande), PECDPO (depot physique),
PECTPR (type), PECFPR (famille), PECETL (code etat -> HLETPPP.T0CEPP),
PEVATP / PEPATP (volume dm3 / poids kg theoriques),
PEVTAV / PEPNTV (volume / poids valides).

Code article P1CART : c'est le SKU Reflex (VU1428-1), PAS l'identifiant du
back-office. Un produit groupe (parent) n'a pas de ligne de prepa : c'est le
composant qui est pique. Une recherche par ID BO qui ne rend rien se retente en
LIKE '%VUxxxx%'.
"""

GUIDE_RECEPTIONS = """\
RECEPTIONS - une reception previsionnelle est "en retard et non clotures" a
QUATRE conditions simultanees. En retirer une seule fait entrer des conteneurs
deja traites :

  1. date d'arrivee prevue < aujourd'hui - seuil de tolerance
  2. top de reception previsionnelle soldee non positionne (RPTRPS)
  3. GEI non generes cote reception reelle (RETGEI) - la marchandise n'est pas
     en stock
  4. reception reelle non validee (RETRVA)

LE NUMERO DE CONTENEUR N'A PAS D'EMPLACEMENT UNIQUE. Il faut le chercher, en
une passe prealable, dans HLRCPEP.RPRRPR, HLRECPP.RERREC, HLAEXEP.VERLIX et
les trois champs de numero de bordereau fournisseur. Ne figer la jointure
qu'apres avoir constate ou il se trouve reellement.

PIEGE DE JOINTURE, coute cher : relier la ligne de reception attendue a la
ligne de preparation PAR LE CODE ARTICLE produit des faux positifs - l'article
peut avoir du stock disponible ailleurs que dans le conteneur en retard. La
commande apparait bloquee alors qu'elle ne l'est pas. Passer par la reference
de reservation ODP (R2RRSO) quand elle est alimentee.
"""

GUIDE_EXPEDITIONS = """\
EXPEDITIONS ET CHARGEMENTS - valide en prod 2026-07

HLEXPEP (EX) relie une preparation a son expedition transmise au TMS.
  EXNEXP  n. d'expedition = la cle transmise a TELIAE
  EXCCHA  code chargement, qui porte le touliv
  EXCRGE  code regroupement : QUASI TOUJOURS VIDE chez VU. Ne pas filtrer.
  EXCDPO / EXNANN / EXNPRE -> HLPRENP

Volume et poids ne sont PAS sur HLEXPEP : les prendre sur HLPRENP (PEVATP en
dm3, PEPATP en kg) et sommer par EXNEXP ou par chargement.

Code chargement : 4 chiffres (le touliv) plus un suffixe de sous-tour, tiret
puis chiffre, ou alpha (AM, TL, VI, RE, MO). Normaliser au tiret :
  LEFT(x, CHARINDEX('-', x) - 1)
Regroupements VUD connus : 6128, toute la famille 71%, 8120.

DATE DE DEPART : HLCHARP.CGSDET... n'est PAS peuple chez VU (rend 1901-01-01).
Utiliser la date de chargement EXSSCA/EXANCA/EXMOCA/EXJOCA, qui est le jour de
depart reel du camion.

Ordre de grandeur : une expedition VUD (= un client) plafonne vers 6 m3 et
350 kg. Un seuil XXL n'a de sens qu'au niveau CHARGEMENT, agrege par
depot + EXCCHA + date.
"""

GUIDE_STOCK = """\
STOCK - ce qu'il faut savoir avant de compter

REE (rebut entrepot) et MQT (manquant) restent PREPARABLES et doivent continuer
d'etre comptes dans le disponible. Les en soustraire fait apparaitre des
ruptures qui n'existent pas. C'est la difference avec les motifs traites par le
process de deblocage (BQ, BMC, CTM, DEP, MOU).

La procedure de deblocage du depot se declenche sur un seuil : les references
dont plus de 75 % du stock est bloque. En dessous, le depot ne traite pas, et
c'est normal - ce n'est pas un oubli a signaler.

Un BMC ou un CTM ancien est un signal, pas un oubli : ce sont des blocages
momentanes par nature, donc leur persistance indique un destockage en attente
de validation.

Piege d'epuration a connaitre sur toute requete d'historique : Reflex epure.
Une absence de ligne ancienne n'est pas une absence d'evenement.

Source : 03_SYSTEMES/REFLEX_WMS/03_procedures/PROC_STOCK_BLOQUE_MOTIFS.
"""

GUIDE_SQL = """\
ECRIRE POUR CE CONNECTEUR, POUR SSMS, OU POUR POWER BI

Ce connecteur   : une seule instruction. Une CTE (WITH ... SELECT) passe. Un
                  DECLARE ne passe pas : le point-virgule est refuse. Les
                  parametres se posent donc en litteraux dans le WHERE.
                  WITH (NOLOCK) est exige sur chaque table du schema reflex.
SSMS / VS Code  : DECLARE + tables temporaires autorises, plus performant sur
                  les gros volumes grace aux index de tables temp.
Power Query     : le connecteur SQL Server de Power BI n'accepte QU'UN SELECT.
                  Donc : pas de DECLARE, pas de #temp, pas de IF/ELSE, pas de
                  PRINT. Chainer des CTE et finir par un seul SELECT.

Convention d'equipe pour une requete metier destinee a Power BI : livrer deux
fichiers, `XX_<nom>.sql` (DECLARE + temp, pour SSMS) et
`XX_<nom>_pour_power_bi.sql` (CTE, single SELECT).

Gabarit accepte ici :

  WITH base AS (
      SELECT ... FROM reflex.HLxxxxP WITH (NOLOCK) WHERE ...
  ),
  enrichie AS (
      SELECT ... FROM base JOIN reflex.HLyyyyP yy WITH (NOLOCK) ON ...
  )
  SELECT col1 AS alias1, col2 AS alias2
  FROM enrichie
  ORDER BY ...

Decouverte cote base, quand le MLD embarque ne suffit pas (colonne ajoutee par
un correctif, vue maison) :
  SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
  FROM INFORMATION_SCHEMA.COLUMNS WITH (NOLOCK)
  WHERE TABLE_SCHEMA = 'reflex' AND TABLE_NAME = 'HLPRENP'
  ORDER BY ORDINAL_POSITION
"""

GUIDE_ECARTS = """\
ECARTS ENTRE LA COMPETENCE D'EQUIPE `reflex-mld` ET LE MLD HARDIS

Releve le 2026-08-28 en construisant ce connecteur, colonne par colonne contre
le MLD editeur v9.14. NON ARBITRE : la competence est portee par Jimmy
Mieuzet, c'est a lui de trancher. En attendant, ces trois points changent le
resultat SANS lever d'erreur - c'est ce qui les rend dangereux.

1. PESLEF n'est PAS "lancement effectif".
   MLD : PESLEF = "Date livraison effectuee - siecle". Meme famille :
   PEULEF = "Code utilisateur livraison effectuee", PETLEF = "Top livraison
   effectuee". LEF = Livraison EFfectuee.
   Consequence : le filtre canonique de la competence,
   "PESLEF > 0 AND PESSOL = 0 AND PESSOR = 0", se lit "livraison effectuee ET
   ni soldee ni sortie de stock" - un etat contradictoire, pas une preparation
   en cours.
   Ce que le MLD nomme sans ambiguite : PETSOL "Top preparation soldee",
   PETSOP "Top sortie stock effectuee pour la preparation".

2. P1TSLO et P1TSOR ne sont PAS les tops de soldage et de sortie de stock.
   MLD : P1TSLO = "Top substitution sur ligne odp", P1TSOR = "Top service
   obligatoire sur reference reservation".
   Le top de ligne qui existe reellement est P1TVLP, "Top ligne preparation
   validee".
   Consequence : le filtre d'origine ecarte des lignes substituees, pas des
   lignes soldees. La population change, silencieusement.
   La condition sure reste la comparaison de quantites : P1QPRE < P1QAPR.

3. PENCOM n'est PAS le numero de commande.
   MLD : PENCOM = "Numero de commentaire". Le lien vers la commande client
   passe par PERODP, "Reference donneur d'ordres ordre de preparation".

Les recettes de ce connecteur sont ecrites sur le MLD, et chacune porte l'ecart
en tete. Si un chiffre issu d'ici ne recoupe pas un chiffre Power BI existant,
regarde ici en premier : c'est probablement la cause, et non une erreur de
perimetre.
"""

GUIDES = {
    "": GUIDE_GENERAL,
    "general": GUIDE_GENERAL,
    "domaines": GUIDE_DOMAINES,
    "ecarts": GUIDE_ECARTS,
    "preparations": GUIDE_PREPARATIONS,
    "receptions": GUIDE_RECEPTIONS,
    "expeditions": GUIDE_EXPEDITIONS,
    "chargements": GUIDE_EXPEDITIONS,
    "stock": GUIDE_STOCK,
    "sql": GUIDE_SQL,
}


# --------------------------------------------------------------------------
# Outils MCP
# --------------------------------------------------------------------------

# Les instructions de serveur : ce que le client MCP affiche avant meme le
# premier appel. C'est le seul endroit ou l'on peut dire, une fois pour toutes,
# par quoi commencer - sans quoi chaque session redecouvre l'ordre des outils.
SERVER_INSTRUCTIONS = """\
Connecteur en LECTURE SEULE sur le WMS Reflex de Vente-unique (SQL Server,
schema `reflex`, 1 985 tables).

Commence par reflex_guide() : il porte les deux pieges qui produisent des
resultats faux sans lever d'erreur - les dates eclatees en cinq colonnes
(siecle, annee, mois, jour, heure) et les tops qui valent '1'/'0' en production
alors que la doc de l'editeur annonce 'O'/'N'.

Le MLD complet est embarque : reflex_tables, reflex_columns, reflex_find_column
et reflex_prefix repondent hors ligne, en quelques millisecondes, sans toucher
la base. N'invente JAMAIS un nom de colonne - lis-le.

Les requetes exigent WITH (NOLOCK) sur chaque table du schema reflex, et sont
bornees : gouverneur de cout, delai, une requete a la fois. Par defaut, une
lecture interroge la base d'epuration puis la base courante, et dit laquelle a
repondu.

N'exporte en CSV que si l'utilisateur a demande un export.\
"""

# Les annotations de comportement de la specification MCP. Elles sont l'ecriture
# formelle de ce que ce connecteur promet : `readOnlyHint` a vrai partout, et
# `destructiveHint` a faux, parce qu'aucun outil n'a de chemin d'ecriture.
#
# Ce ne sont que des indications - la specification demande explicitement aux
# clients de ne pas leur faire confiance. Ce qui GARANTIT la lecture seule
# reste le code : pas d'outil d'ecriture, l'analyseur assert_read_only, et la
# session en READ UNCOMMITTED. Les annotations disent au client ce que le code
# tient deja.
#
# `openWorldHint` distingue les deux familles d'outils, et cette distinction est
# utile : faux pour ce qui lit le catalogue embarque - ensemble ferme, reponse
# reproductible - vrai pour ce qui interroge la base, dont le contenu bouge a
# chaque mouvement d'entrepot.

def _hints(titre: str, monde_ouvert: bool, idempotent: bool = True) -> ToolAnnotations:
    return ToolAnnotations(
        title=titre,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=monde_ouvert,
    )


HORS_LIGNE = False   # lit le catalogue MLD embarque
SUR_LA_BASE = True   # interroge Reflex

mcp = FastMCP("reflex", instructions=SERVER_INSTRUCTIONS)


@mcp.tool(annotations=_hints("Memo Reflex : conventions et pieges", HORS_LIGNE))
@_guard
def reflex_guide(sujet: str = "") -> str:
    """Le memo Reflex : conventions, pieges et carte des tables. A LIRE EN PREMIER.

    Repond hors ligne, instantanement. Aucune requete n'est envoyee a la base.

    Lis-le avant d'ecrire ta premiere requete d'une session : il porte les deux
    pieges qui produisent des resultats faux sans lever d'erreur - les dates
    eclatees en cinq colonnes, et les tops qui valent '1'/'0' chez Vente-unique
    et non 'O'/'N' comme l'annonce la doc de l'editeur.

    sujet : vide pour le memo general, ou l'un de
            domaines, preparations, receptions, expeditions, stock, sql
    """
    key = _norm(sujet).strip()
    if key not in GUIDES:
        return (
            f"Sujet inconnu : {sujet!r}.\nSujets disponibles : "
            + ", ".join(k for k in GUIDES if k)
        )
    text = GUIDES[key]
    if key in ("", "general"):
        meta = {}
        try:
            _catalog()
            meta = _CATALOG_META
        except ConfigError:
            pass
        text = text.format(
            server=_server(),
            database=_database(),
            database_epu=_database("epu"),
            schema=_schema(),
            mld_version=meta.get("version", "9.14"),
            mld_tables=meta.get("tables", "1986"),
        )
    return text


@mcp.tool(annotations=_hints("Chercher une table dans le MLD Reflex", HORS_LIGNE))
@_guard
def reflex_tables(recherche: str = "", prefixe: str = "", limit: int = 40) -> str:
    """Cherche une table dans le MLD Reflex : 1 985 tables, hors ligne, instantane.

    La recherche porte sur le nom physique (HLPRENP), le nom de l'entite
    logique (HL_PREPA_ENTETE) et la description en francais ("Preparation -
    En-tete"). Accents et casse sont ignores.

    recherche : un mot metier ("reception", "facture", "rendez-vous") ou un
                fragment de nom de table ("HLPRE").
    prefixe   : les deux lettres qui prefixent les colonnes de la table ("PE").
    limit     : nombre de tables rendues.
    """
    tables = _catalog()
    needle = _norm(recherche).strip()
    pref = (prefixe or "").strip().upper()
    hits = []
    for record in tables.values():
        if pref and record.get("p", "").upper() != pref:
            continue
        if needle:
            haystack = _norm(f"{record['t']} {record['e']} {record['d']}")
            if needle not in haystack:
                continue
        hits.append(record)
    if not hits:
        return (
            f"Aucune table pour recherche={recherche!r} prefixe={prefixe!r}.\n"
            "Essaie un mot plus court, ou reflex_find_column si tu cherches "
            "en fait une colonne."
        )
    # Le nom exact d'abord, puis les plus riches : une table a 200 colonnes est
    # presque toujours la table principale du domaine, celle a 3 colonnes une
    # table de liaison.
    hits.sort(key=lambda r: (_norm(r["t"]) != needle, -len(r["c"]), r["t"]))
    shown = hits[:limit]
    lines = [
        f"{len(hits)} table(s) trouvee(s)" + (f", {len(shown)} affichee(s)." if len(hits) > len(shown) else "."),
        "",
        "table     prefixe  colonnes  entite logique          description",
    ]
    for r in shown:
        lines.append(
            f"{r['t']:<9} {r.get('p', ''):<8} {len(r['c']):>8}  {r['e']:<22}  {r['d']}"
        )
    lines.append("")
    lines.append("Colonnes d'une table : reflex_columns('<TABLE>').")
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Colonnes exactes d'une table Reflex", HORS_LIGNE))
@_guard
def reflex_columns(
    table: str,
    recherche: str = "",
    cles_seules: bool = False,
    limit: int = 120,
) -> str:
    """Les colonnes d'une table Reflex, avec libelle, type, longueur et rang de cle.

    Hors ligne, instantane, et c'est la source a utiliser AVANT d'ecrire une
    requete : un nom de colonne Reflex ne se devine pas, et une colonne
    inventee produit soit une erreur, soit - pire - une jointure qui rend des
    lignes fausses.

    table       : le nom physique, ex. HLPRENP.
    recherche   : filtre sur le nom ou le libelle, ex. "sol" pour tout ce qui
                  touche au soldage, "date" pour les composants de date.
    cles_seules : n'affiche que les colonnes de la cle primaire, dans l'ordre.
    """
    tables = _catalog()
    name = (table or "").strip().upper()
    record = tables.get(name)
    if record is None:
        near = [t for t in tables if name and name in t][:10]
        hint = ("\nTables approchantes : " + ", ".join(near)) if near else ""
        return (
            f"Table inconnue dans le MLD : {table!r}.{hint}\n"
            "Cherche avec reflex_tables('<mot metier>')."
        )
    needle = _norm(recherche).strip()
    rows = []
    for col in record["c"]:
        cname, logical, label, ctype, length, key, virtual = col
        if cles_seules and not key:
            continue
        if needle and needle not in _norm(f"{cname} {logical} {label}"):
            continue
        rows.append((cname, ctype, length, key, virtual, label, logical))
    header = (
        f"{record['t']} - {record['d']}  (entite {record['e']}, prefixe "
        f"{record.get('p', '?')}, {len(record['c'])} colonnes au total)"
    )
    if not rows:
        return header + f"\n\nAucune colonne ne correspond a recherche={recherche!r}."
    shown = rows[:limit]
    lines = [
        header,
        "",
        f"{len(rows)} colonne(s)" + (f", {len(shown)} affichee(s)." if len(rows) > len(shown) else "."),
        "",
        "colonne  type  lg      cle  virt  libelle",
    ]
    for cname, ctype, length, key, virtual, label, _logical in shown:
        lines.append(
            f"{cname:<8} {ctype:<5} {length:<7} {key or '-':<4} {virtual or '-':<5} {label}"
        )
    lines.append("")
    lines.append(
        "type : " + " | ".join(f"{k}={v}" for k, v in TYPE_LABELS.items() if k != "?")
    )
    lines.append(
        "cle = rang dans la cle primaire. virt = colonne virtuelle (calculee "
        "par Reflex, pas toujours materialisee)."
    )
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Retrouver la table d'une colonne", HORS_LIGNE))
@_guard
def reflex_find_column(terme: str, limit: int = 40) -> str:
    """Retrouve dans quelle(s) table(s) vit une colonne, par son nom ou son libelle.

    Hors ligne. C'est l'outil du "j'ai lu PECDPO dans une vieille requete, c'est
    quoi" et du "quelle table porte le numero de conteneur".

    terme : un nom de colonne (PECDPO), un fragment (CDPO), ou un mot du
            libelle (conteneur, transporteur, poids).
    """
    tables = _catalog()
    needle = _norm(terme).strip()
    if not needle:
        return "Donne un terme a chercher : un nom de colonne, ou un mot de son libelle."
    exact: list[tuple[str, list[str]]] = []
    partial: list[tuple[str, list[str]]] = []
    for record in tables.values():
        for col in record["c"]:
            cname, logical, label = col[0], col[1], col[2]
            if _norm(cname) == needle:
                exact.append((record["t"], col))
            elif needle in _norm(f"{cname} {logical} {label}"):
                partial.append((record["t"], col))
    hits = exact + partial
    if not hits:
        return f"Aucune colonne ne correspond a {terme!r}."
    shown = hits[:limit]
    lines = [
        f"{len(hits)} colonne(s) trouvee(s)"
        + (f", {len(shown)} affichee(s)." if len(hits) > len(shown) else ".")
        + (f" Dont {len(exact)} en nom exact." if exact else ""),
        "",
        "table     colonne  type  lg      cle  libelle",
    ]
    for tname, col in shown:
        cname, _logical, label, ctype, length, key, _virtual = col
        lines.append(f"{tname:<9} {cname:<8} {ctype:<5} {length:<7} {key or '-':<4} {label}")
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Table d'un prefixe de colonne", HORS_LIGNE))
@_guard
def reflex_prefix(prefixe: str = "") -> str:
    """De quelle table sort une colonne, a partir de ses deux premieres lettres.

    Hors ligne. Les colonnes Reflex sont prefixees par table : PE = HLPRENP,
    P1 = HLPRPLP, RP = HLRCPEP. Lire une requete heritee commence presque
    toujours par la.

    prefixe : les deux lettres. Vide = la liste des prefixes des tables les plus
              riches, ce qui donne une carte rapide de la base.
    """
    _catalog()
    pref = (prefixe or "").strip().upper()
    if not pref:
        rows = []
        for key, names in _CATALOG_BY_PREFIX.items():
            best = max(names, key=lambda n: len(_CATALOG[n]["c"]))  # type: ignore[index]
            rows.append((key, best, len(_CATALOG[best]["c"]), _CATALOG[best]["d"]))  # type: ignore[index]
        rows.sort(key=lambda r: -r[2])
        lines = [
            f"{len(_CATALOG_BY_PREFIX)} prefixes dans le MLD. Les 40 tables les "
            "plus riches, par prefixe :",
            "",
            "prefixe  table     colonnes  description",
        ]
        for key, best, ncol, desc in rows[:40]:
            lines.append(f"{key:<8} {best:<9} {ncol:>8}  {desc}")
        return "\n".join(lines)
    names = _CATALOG_BY_PREFIX.get(pref[:2], [])
    if not names:
        return f"Aucune table dont les colonnes sont prefixees {pref!r}."
    lines = [f"Prefixe {pref[:2]} - {len(names)} table(s) :", "", "table     colonnes  description"]
    for name in sorted(names, key=lambda n: -len(_CATALOG[n]["c"])):  # type: ignore[index]
        record = _CATALOG[name]  # type: ignore[index]
        lines.append(f"{name:<9} {len(record['c']):>8}  {record['d']}")
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Requetes de reference deja validees", HORS_LIGNE))
@_guard
def reflex_recipes(nom: str = "") -> str:
    """Les requetes deja validees en production, a lire puis a adapter.

    Hors ligne : cet outil rend du SQL, il ne l'execute pas. Adapte le depot,
    la periode et les filtres, puis passe-le a reflex_query.

    Elles portent les definitions metier sur lesquelles l'equipe s'est mise
    d'accord - ce qu'est une preparation "en cours", une reception "en retard".
    Repartir de la evite de redefinir un indicateur a chaque question.

    nom : vide pour la liste, sinon le nom d'une recette.
    """
    if not RECIPES_DIR.is_dir():
        return f"Aucun dossier de recettes ({RECIPES_DIR})."
    files = sorted(RECIPES_DIR.glob("*.sql"))
    if not nom:
        lines = [f"{len(files)} requete(s) de reference :", ""]
        for path in files:
            first = ""
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("-- "):
                        first = line[3:].strip()
                        break
            except OSError:
                pass
            lines.append(f"  {path.stem:<34} {first}")
        lines.append("")
        lines.append("Le detail : reflex_recipes('<nom>').")
        return "\n".join(lines)
    key = nom.strip().lower().removesuffix(".sql")
    for path in files:
        if path.stem.lower() == key:
            return f"{path.stem}\n\n" + path.read_text(encoding="utf-8")
    return (
        f"Recette inconnue : {nom!r}.\nDisponibles : "
        + ", ".join(p.stem for p in files)
    )


@mcp.tool(annotations=_hints("Executer une lecture T-SQL sur Reflex", SUR_LA_BASE, idempotent=False))
@_guard
def reflex_query(sql: str, max_rows: int = 0, base: str = "auto", timeout_s: int = 0) -> str:
    """Execute une requete T-SQL en LECTURE SEULE sur Reflex et rend le resultat.

    Seuls SELECT et WITH sont acceptes, une instruction par appel. Tout le
    reste - INSERT, UPDATE, DELETE, EXEC, procedure systeme, INTO, plusieurs
    instructions separees par un point-virgule - est refuse avant d'atteindre
    la base.

    WITH (NOLOCK) est OBLIGATOIRE sur chaque table du schema reflex : une
    requete qui l'oublie est refusee, pas seulement signalee.

    Trois garde-fous bornent l'execution : le gouverneur de cout de SQL Server,
    qui refuse de demarrer un plan trop cher ; un delai d'execution ; et une
    seule requete a la fois. Un resultat tronque est signale comme tel.

    Avant d'ecrire : reflex_guide() pour les conventions, reflex_columns() pour
    les noms de colonnes exacts. N'invente jamais un nom de colonne.

    sql       : la requete. Prefixe les tables par `reflex.`, WITH (NOLOCK) sur
                chacune, filtre sur le depot en priorite.
    max_rows  : plafond de lignes rendues (defaut 200). Un resultat tronque
                n'est PAS un total : ne le cite jamais comme un volume.
    base      : 'auto' (defaut) interroge la base d'EPURATION puis, si elle ne
                rend rien, la base COURANTE. Reflex epure en continu, et le
                moment ou un dossier bascule n'est pas connu de celui qui pose
                la question : chercher d'abord dans l'epuration trouve le cas
                ancien du premier coup. 'epu' ou 'prod' forcent une seule base.
                La base qui a repondu est toujours indiquee - un chiffre dont
                on ignore de quelle base il sort n'est pas citable.
                A savoir : la cascade se declenche sur ZERO ligne. Un COUNT(*)
                sans GROUP BY rend toujours une ligne, donc ne bascule jamais.
    timeout_s : delai en secondes pour cette requete. 0 = le reglage d'equipe.
    """
    cap = max_rows if max_rows and max_rows > 0 else _int_env("REFLEX_MAX_ROWS", DEFAULT_MAX_ROWS)
    columns, rows, truncated, retenue, tried = run_query_cascade(
        sql, cap, base=base, timeout_s=timeout_s
    )
    header = f"Reflex - {retenue}\n{_cascade_note(retenue, tried, rows)}"
    return _render_rows(columns, rows, header, truncated)


@mcp.tool(annotations=_hints("Coup d'oeil sur une table, sans ecrire de SQL", SUR_LA_BASE, idempotent=False))
@_guard
def reflex_peek(
    table: str,
    colonnes: str = "",
    where: str = "",
    depot: str = "",
    max_rows: int = 20,
    base: str = "auto",
) -> str:
    """Un coup d'oeil sur une table, sans ecrire de SQL. Pour se reperer.

    Construit et execute un SELECT TOP borne. Utile pour voir a quoi
    ressemblent vraiment les valeurs d'une colonne - un code depot, un format
    de reference, un top a '0'/'1' - avant d'ecrire la vraie requete.

    table    : nom physique, ex. HLPRENP.
    colonnes : liste separee par des virgules. Vide = les 12 premieres colonnes
               de la table, ce qui inclut presque toujours la cle.
    where    : condition SQL, sans le mot WHERE. Ex. "PECDPO = 'AMB'".
    depot    : raccourci. Ajoute le filtre sur la colonne de depot de la table
               si elle en a une (PECDPO, P1CDPO, RPCDPO...).
    """
    tables = _catalog()
    name = _validate_identifier(table, "Nom de table").upper()
    record = tables.get(name)
    if record is None:
        return (
            f"Table inconnue dans le MLD : {table!r}. Cherche avec "
            "reflex_tables('<mot metier>')."
        )
    known = {c[0].upper(): c for c in record["c"]}
    if colonnes.strip():
        wanted = [_validate_identifier(c, "Nom de colonne").upper() for c in colonnes.split(",") if c.strip()]
        unknown = [c for c in wanted if c not in known]
        if unknown:
            return (
                f"Colonne(s) absente(s) de {name} : {', '.join(unknown)}.\n"
                f"Liste exacte : reflex_columns('{name}')."
            )
    else:
        wanted = [c[0] for c in record["c"][:12]]

    clauses: list[str] = []
    if depot.strip():
        prefix = record.get("p", "")
        candidates = [f"{prefix}CDPO", f"{prefix}CDPR"]
        column = next((c for c in candidates if c.upper() in known), "")
        if not column:
            return (
                f"{name} n'a pas de colonne de depot reconnue "
                f"({', '.join(candidates)}). Passe le filtre par `where`."
            )
        # Le depot est une donnee, pas du SQL : il est echappe, pas interpole
        # tel quel. Un connecteur en lecture seule reste un connecteur.
        clauses.append(f"{column} = '{depot.strip().replace(chr(39), chr(39) * 2)}'")
    if where.strip():
        clauses.append(f"({where.strip().rstrip(';')})")

    sql = (
        f"SELECT TOP {max(1, min(max_rows, 500))} {', '.join(wanted)}\n"
        f"FROM {_schema()}.{name} WITH (NOLOCK)"
    )
    if clauses:
        sql += "\nWHERE " + "\n  AND ".join(clauses)

    cap = max(1, min(max_rows, 500))
    columns, rows, truncated, retenue, tried = run_query_cascade(sql, cap, base=base)
    header = (
        f"{name} - {record['d']}  [{retenue}]\n"
        f"{_cascade_note(retenue, tried, rows)}\n\n{sql}"
    )
    return _render_rows(columns, rows, header, truncated)


@mcp.tool(annotations=_hints("Exporter en CSV (sur demande explicite)", SUR_LA_BASE, idempotent=False))
@_guard
def reflex_export_csv(
    sql: str,
    nom_fichier: str = "",
    max_rows: int = 0,
    base: str = "auto",
) -> str:
    """Exporte le resultat d'une requete en CSV. UNIQUEMENT sur demande explicite.

    N'appelle jamais cet outil de ta propre initiative : une reponse va dans la
    conversation, pas dans un fichier. Il ne sert que si l'utilisateur a
    demande un export, un fichier, ou de quoi ouvrir dans Excel.

    Memes regles de lecture seule que reflex_query. Le fichier est ecrit en
    UTF-8 avec BOM et separateur point-virgule, pour s'ouvrir directement dans
    Excel en francais.

    sql         : la requete. Memes regles que reflex_query, WITH (NOLOCK)
                  compris.
    nom_fichier : nom du fichier. Vide = un nom horodate.
    max_rows    : plafond de lignes exportees (defaut 100 000).
    base        : 'auto' (defaut) exporte depuis la base d'EPURATION si elle
                  rend des lignes, sinon depuis la COURANTE. La base retenue
                  est indiquee dans le compte rendu : un fichier dont on ignore
                  de quelle base il sort n'est pas exploitable.
                  'epu' ou 'prod' forcent une seule base.
    """
    cap = max_rows if max_rows and max_rows > 0 else _int_env(
        "REFLEX_EXPORT_MAX_ROWS", DEFAULT_EXPORT_MAX_ROWS
    )
    body = assert_read_only(sql)
    # Un export a un budget de temps plus large qu'une lecture ecran : il est
    # demande explicitement, et personne n'attend devant. Il reste borne.
    budget = _int_env("REFLEX_EXPORT_TIMEOUT_S", DEFAULT_EXPORT_TIMEOUT_S)

    stem = (nom_fichier or "").strip() or f"reflex_{dt.datetime.now():%Y%m%d_%H%M%S}"
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).removesuffix(".csv")
    target = _export_dir() / f"{stem}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)

    # Un export cascade comme une lecture, mais le compte a rendre est plus
    # exigeant : on ne peut pas ecrire un fichier vide et laisser croire que la
    # question n'avait pas de reponse. On repere donc la base qui rend des
    # lignes AVANT d'ouvrir le fichier, par une lecture d'une seule ligne.
    bases = _base_cascade(base)
    tried: list[str] = []
    cible = bases[-1]
    if len(bases) > 1:
        for candidate in bases:
            tried.append(_database(candidate))
            _, sonde, _ = run_query(body, 1, base=candidate, timeout_s=budget)
            cible = candidate
            if sonde:
                break
    else:
        tried.append(_database(cible))

    if not _QUERY_LOCK.acquire(blocking=False):
        raise ReflexError(
            "Une requete Reflex est deja en cours dans cette session. Attends "
            "qu'elle rende la main avant de lancer un export."
        )
    written = 0
    truncated = False
    try:
        conn = _connect(base=cible, timeout_s=budget)
        try:
            cursor = conn.cursor()
            with _Watchdog(cursor, budget) as watchdog:
                try:
                    cursor.execute(body)
                except Exception as exc:
                    if watchdog.fired:
                        raise ReflexError(_explain_timeout(budget)) from exc
                    raise ReflexError(_explain_query_error(exc)) from exc
                if cursor.description is None:
                    raise ReflexError("La requete ne rend aucun jeu de resultats.")
                columns = [d[0] for d in cursor.description]
                # Ecriture au fil de l'eau : un export de 100 000 lignes ne
                # tient pas en memoire, et n'a aucune raison d'y passer.
                with open(target, "w", encoding="utf-8-sig", newline="") as fh:
                    writer = csv.writer(fh, delimiter=";", lineterminator="\r\n")
                    writer.writerow(columns)
                    while written < cap:
                        batch = cursor.fetchmany(min(2000, cap - written))
                        if not batch:
                            break
                        for row in batch:
                            writer.writerow([_coerce(v) for v in row])
                        written += len(batch)
                    if written >= cap and cursor.fetchone() is not None:
                        truncated = True
                        try:
                            cursor.cancel()
                        except Exception:
                            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
    finally:
        _QUERY_LOCK.release()

    size = target.stat().st_size
    lines = [
        f"Export ecrit : {target}",
        f"Base         : {_database(cible)}"
        + (f"  (cascade : {' puis '.join(tried)})" if len(tried) > 1 else ""),
        f"{written} ligne(s), {len(columns)} colonne(s), {size // 1024} Ko.",
        "Format : UTF-8 avec BOM, separateur point-virgule (Excel francais).",
    ]
    if truncated:
        lines.append(
            f"ATTENTION : export tronque a {cap} lignes. Le fichier n'est PAS "
            "complet. Resserre le perimetre, ou releve max_rows en connaissance "
            "de cause."
        )
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Etat de la mise en service", HORS_LIGNE))
@_guard
def reflex_setup_status() -> str:
    """Ou en est la mise en service : configuration, pilote ODBC, catalogue MLD.

    Ne se connecte pas a la base - c'est le role de reflex_doctor. Cet outil
    dit ce que le connecteur a lu et ou il l'a lu, ce qui est la premiere chose
    a regarder quand un reglage "ne prend pas".
    """
    _load_env_file()
    lines = ["MISE EN SERVICE DU CONNECTEUR REFLEX", ""]

    lines.append("--- Configuration ---")
    if _ENV_LOADED_FROM:
        lines.append(f"Fichier .env         : {_ENV_LOADED_FROM}")
        lines.append("  cles lues          : " + (", ".join(sorted(_ENV_FILE_KEYS)) or "(aucune)"))
    else:
        lines.append("Fichier .env         : aucun (pas necessaire en mode trusted)")
        for path in _candidate_env_files():
            lines.append(f"    absent  {path}")
    if _ENV_LOAD_ERROR:
        lines.append(f"  ALERTE : {_ENV_LOAD_ERROR}")
    packaged = _packaged_python()
    if packaged and not _ENV_LOADED_FROM:
        lines.append(
            f"  Remarque : {packaged}. Un fichier pose sous %LOCALAPPDATA% peut "
            "etre invisible pour cet interpreteur. Si le .env existe bien, "
            "pointe-le avec REFLEX_ENV_FILE."
        )
    lines.append("")
    lines.extend(_shared_report())

    lines.append("")
    lines.append("--- Cible ---")
    lines.append(f"Serveur              : {_server()}")
    lines.append(f"Base courante        : {_database()}")
    lines.append(f"Base d'epuration     : {_database('epu')}")
    lines.append(f"Schema               : {_schema()}")
    mode = _auth_mode()
    lines.append(
        f"Authentification     : {mode}"
        + (" (session Windows, aucun secret stocke)" if mode == "trusted" else f" (compte {_env('REFLEX_USER') or '?'})")
    )
    lines.append(f"Dossier des exports  : {_export_dir()}")

    lines.append("")
    lines.append("--- Garde-fous ---")
    limit = _cost_limit()
    lines.append(
        f"Gouverneur de cout   : {limit if limit else 'DESACTIVE'}"
        + (" (plan refuse avant execution au-dela)" if limit else " - aucun plan n'est refuse a priori")
    )
    lines.append(f"Delai par requete    : {_query_timeout()} s (plafond dur {MAX_QUERY_TIMEOUT_S} s)")
    lines.append(f"Delai par export     : {_int_env('REFLEX_EXPORT_TIMEOUT_S', DEFAULT_EXPORT_TIMEOUT_S)} s")
    lines.append(f"Plafond de lecture   : {_int_env('REFLEX_MAX_ROWS', DEFAULT_MAX_ROWS)} lignes")
    lines.append(f"Plafond d'export     : {_int_env('REFLEX_EXPORT_MAX_ROWS', DEFAULT_EXPORT_MAX_ROWS)} lignes")
    lines.append(
        "WITH (NOLOCK) exige  : "
        + ("oui - une requete qui l'oublie est refusee" if _require_nolock() else "NON (REFLEX_REQUIRE_NOLOCK=0)")
    )
    lines.append("Requetes simultanees : 1 - les suivantes sont refusees, pas mises en attente")
    lines.append("Isolation            : READ UNCOMMITTED (aucun verrou pose sur la production)")

    lines.append("")
    lines.append("--- Pilote ODBC ---")
    if pyodbc is None:
        lines.append("pyodbc               : ABSENT de cet interpreteur")
        lines.append(f"  interpreteur       : {sys.executable}")
        lines.append(
            "  En mode plugin, bootstrap.py l'installe au premier demarrage. "
            "En mode direct : python -m pip install -r requirements.txt"
        )
    else:
        available = _available_drivers()
        lines.append(f"pyodbc               : {pyodbc.version}")
        lines.append("Pilotes presents     : " + (", ".join(available) or "(aucun)"))
        try:
            chosen = _driver()
            lines.append(f"Pilote retenu        : {chosen}")
            if chosen == "SQL Server":
                lines.append(
                    "  C'est le pilote livre avec Windows. Il suffit pour "
                    "Reflex et n'a rien demande a installer, c'est voulu."
                )
        except ConfigError as exc:
            lines.append(f"Pilote retenu        : AUCUN - {exc}")

    lines.append("")
    lines.append("--- Catalogue MLD embarque ---")
    try:
        tables = _catalog()
        meta = _CATALOG_META
        ncols = sum(len(r["c"]) for r in tables.values())
        lines.append(f"Fichier              : {CATALOG_FILE}")
        lines.append(
            f"Contenu              : {len(tables)} tables, {ncols} colonnes - "
            f"MLD Hardis v{meta.get('version', '?')} (maj editeur "
            f"{meta.get('mld_updated', '?')}, catalogue construit le "
            f"{meta.get('built', '?')})"
        )
    except ConfigError as exc:
        lines.append(f"ALERTE : {exc}")

    lines.append("")
    lines.append("Test de connexion reel : outil reflex_doctor.")
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Diagnostic complet, connexion comprise", SUR_LA_BASE, idempotent=False))
@_guard
def reflex_doctor() -> str:
    """Diagnostic complet, connexion comprise : le connecteur atteint-il Reflex ?

    Tente une connexion et une requete inoffensive. A lancer en premier quand
    quelque chose ne repond pas - il distingue un probleme de reseau (VPN), de
    droits (compte Windows sans acces) et de configuration.
    """
    lines = [reflex_setup_status(), "", "--- Connexion ---"]
    if pyodbc is None:
        lines.append("Test impossible : pyodbc absent.")
        return "\n".join(lines)
    lines.append(f"Chaine               : {_conn_str(mask=True)}")
    try:
        started = dt.datetime.now()
        columns, rows, _ = run_query(
            "SELECT @@VERSION AS version, DB_NAME() AS base, "
            "SUSER_SNAME() AS compte, @@SPID AS spid",
            1,
        )
        elapsed = (dt.datetime.now() - started).total_seconds()
        lines.append(f"Connexion            : OK en {elapsed:.2f} s")
        if rows:
            values = dict(zip(columns, rows[0]))
            version = str(values.get("version", "")).splitlines()[0]
            lines.append(f"  version            : {version}")
            lines.append(f"  base               : {values.get('base')}")
            lines.append(f"  compte             : {values.get('compte')}")
    except (ConfigError, ReflexError) as exc:
        lines.append("Connexion            : ECHEC")
        lines.append("")
        lines.append(str(exc))
        return "\n".join(lines)

    # Le schema et la fonction de date sont les deux dependances du connecteur
    # cote base. Les verifier ici evite de decouvrir leur absence au milieu
    # d'une requete metier, ou l'erreur est bien moins parlante.
    try:
        _, rows, _ = run_query(
            "SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.TABLES WITH (NOLOCK) "
            f"WHERE TABLE_SCHEMA = '{_schema()}'",
            1,
        )
        lines.append(f"Tables dans `{_schema()}`  : {rows[0][0] if rows else '?'}")
    except (ConfigError, ReflexError) as exc:
        lines.append(f"Lecture du schema    : ECHEC - {exc}")

    try:
        _, rows, _ = run_query(
            "SELECT REFLEX.RFX_DHB_DATE2DATETIME(20, 26, 8, 28, 120000) AS test", 1
        )
        lines.append(f"RFX_DHB_DATE2DATETIME: OK -> {rows[0][0] if rows else '?'}")
    except (ConfigError, ReflexError):
        lines.append(
            "RFX_DHB_DATE2DATETIME: introuvable ou inaccessible. Les dates "
            "devront etre reconstituees autrement - signale-le, c'est un ecart "
            "avec ce que documente l'equipe."
        )

    # La base d'epuration est une seconde base, avec ses propres droits : elle
    # peut tres bien etre refusee alors que la courante repond.
    try:
        _, rows, _ = run_query(
            "SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.TABLES WITH (NOLOCK) "
            f"WHERE TABLE_SCHEMA = '{_schema()}'",
            1,
            base="epu",
        )
        lines.append(f"Base d'epuration     : OK, {rows[0][0] if rows else '?'} tables dans `{_schema()}`")
    except (ConfigError, ReflexError) as exc:
        first = str(exc).splitlines()[0]
        lines.append(f"Base d'epuration     : inaccessible - {first}")
        lines.append(
            "  Ce n'est bloquant que pour les questions d'historique : la base "
            "courante suffit au quotidien."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entree
# --------------------------------------------------------------------------

USAGE = """\
MCP reflex-wms - interrogation en lecture seule du WMS Reflex.

  python server.py            mode serveur MCP (stdio), lance par Claude Code
  python server.py doctor     diagnostic de configuration et de connexion
  python server.py --help     ce message

Configuration : voir README.md et reflex.env.example.
"""


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if args and args[0] == "doctor":
        # reflex_doctor est enveloppe par FastMCP ; on appelle la fonction nue.
        print(reflex_doctor.fn() if hasattr(reflex_doctor, "fn") else reflex_doctor())
        return 0
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
