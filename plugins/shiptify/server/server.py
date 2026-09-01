#!/usr/bin/env python3
"""
MCP shiptify : interrogation en LECTURE SEULE de la base Shiptify.

Trois usages :
    python server.py            # mode serveur MCP (stdio) - lance par Claude Code
    python server.py doctor     # diagnostic de configuration et de connexion
    python server.py --help

Lecture seule par construction. L'API publique Shiptify expose 277 operations,
dont 91 en GET : ce serveur n'expose que les GET. Aucun POST, PUT, PATCH ni
DELETE n'est atteignable, y compris par l'outil generique shiptify_get, qui
valide le chemin demande contre la liste blanche des chemins GET du contrat
OpenAPI (openapi_get_paths.json, a cote de ce fichier).

Consequence a connaitre : creer un transport, annuler un envoi, confirmer un
enlevement ou poster un message dans un chat Shiptify ne se font pas ici. C'est
volontaire, et cela rejoint la regle du vault : jamais d'envoi automatique.

Contrat de reference : https://api-docs.shiptify.com/ (OpenAPI 3.0.2, spec
publiee a shiptify-public-api.openapi.json). Releve le 2026-08-27.

Configuration : un fichier .env, hors du vault. Voir README.md.
"""

from __future__ import annotations

import concurrent.futures
import csv
import datetime as dt
import functools
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import unicodedata
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

import vu_cache as cache

DEFAULT_BASE_URL = "https://api.shiptify.com"
DEFAULT_AUTH_PREFIX = "Api-Key"

# Limite imposee par l'API sur la plupart des collections.
PAGE_LIMIT = 100
# /visits accepte 200.
PAGE_LIMIT_VISITS = 200

DEFAULT_TIMEOUT_S = 60.0
# Garde-fou de pagination : au-dela, on exporte en CSV plutot que de boucler.
DEFAULT_MAX_PAGES = 60

# Plafond de caracteres rendus dans la conversation par un outil de liste.
RENDER_MAX_CHARS = 24_000

# Nombre de tentatives sur une erreur qui a des chances de passer au coup
# suivant. Sans cela, un 429 en milieu de pagination perd les 40 pages deja
# ramenees, et l'utilisateur relance tout depuis zero - ce qui consomme le
# quota une seconde fois.
RETRY_ATTEMPTS = 3
RETRY_STATUSES = (429, 500, 502, 503, 504)

# Colonnes numeriques du cache. Sans ce typage, un SUM() en SQL additionne des
# chaines et rend n'importe quoi.
NUMERIC_COLUMNS = {
    "total_weight",
    "total_volume",
    "total_linear_meters",
    "total_taxable_weight",
    "cost",
    "price",
    "quantity",
    "nb_packages",
}

# Colonnes indexees a la synchronisation : celles sur lesquelles on filtre.
INDEX_COLUMNS = ("date", "created_at", "carrier.name", "address_dest.country", "status")

LEXIQUE_FILE = pathlib.Path(__file__).resolve().parent / "lexique.json"

# Dossiers synchronises : un secret n'y vit pas.
SYNCED_MARKERS = ("/onedrive", "cafom", "sharepoint", "dropbox", "google drive")

SPEC_FILE = pathlib.Path(__file__).resolve().parent / "openapi_get_paths.json"

# Projection par defaut pour les envois : un enregistrement Shiptify aplati
# porte plus de 100 colonnes, les rendre toutes sature la conversation pour
# rien. fields="*" rend tout.
SHIPMENT_BRIEF = (
    "id,code,status,internal_ref,other_reference,created_at,date,"
    "carrier.name,shipment_mode.name,"
    "address_from.city,address_from.country,"
    "address_dest.city,address_dest.zipcode,address_dest.country,"
    "total_weight,total_volume,total_linear_meters,cost,price,"
    "real_departure_time,real_arrival_time,tracking_code"
)


class ConfigError(RuntimeError):
    pass


class ShiptifyError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Liste blanche des chemins GET
# --------------------------------------------------------------------------

def _load_spec() -> dict[str, Any]:
    try:
        return json.loads(SPEC_FILE.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(
            f"Liste des chemins GET introuvable ({SPEC_FILE}) : {exc}. "
            "Ce fichier fait partie du serveur, il est versionne a cote de "
            "server.py. Regenere-le depuis "
            "https://api-docs.shiptify.com/shiptify-public-api.openapi.json."
        ) from exc


_SPEC: dict[str, Any] | None = None


def _spec() -> dict[str, Any]:
    global _SPEC
    if _SPEC is None:
        _SPEC = _load_spec()
    return _SPEC


def _get_patterns() -> list[tuple[str, re.Pattern[str]]]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for path in _spec()["paths"]:
        # {id} -> [^/]+ ; le reste du chemin est litteral.
        regex = "^" + re.sub(r"\\\{[^/}]+\\\}", "[^/]+", re.escape(path)) + "$"
        out.append((path, re.compile(regex)))
    return out


# --------------------------------------------------------------------------
# Configuration : le fichier .env
# --------------------------------------------------------------------------

_ENV_LOADED_FROM: str = ""
_ENV_LOAD_ERROR: str = ""
_ENV_DONE: bool = False
# Cles qui viennent du fichier, pour que doctor sache dire d'ou sort la cle.
_ENV_FILE_KEYS: set[str] = set()


def _is_synced(path: pathlib.Path) -> bool:
    flat = str(path).replace("\\", "/").lower()
    return any(marker in flat for marker in SYNCED_MARKERS)


def _packaged_python() -> str:
    """Detecte un interpreteur Windows empaquete. Rend la raison, ou "".

    Pourquoi c'est ici et pas dans une note de bas de page : un Python livre
    par le Microsoft Store ou par le Python Manager tourne dans un conteneur
    d'application, et **ses processus enfants heritent d'une vue virtualisee de
    `%LOCALAPPDATA%`**. Mesure sur ce poste : le meme interpreteur de venv voit
    `['.env', 'venv']` lance directement, et `['venv']` seulement lance par le
    Python du Store - `LOCALAPPDATA` valant pourtant la meme chaine.

    Consequence : un `.env` pose sous `%LOCALAPPDATA%` par PowerShell peut etre
    **invisible** pour ce serveur, sans aucune erreur. On le detecte pour le
    dire, au lieu de rapporter "absent" et d'envoyer chercher un fichier qui
    est bien la.
    """
    flat = str(pathlib.Path(sys.base_prefix)).replace("\\", "/").lower()
    for marker in ("/windowsapps/", "/packages/pythonsoftwarefoundation", "/python/pythoncore-"):
        if marker in flat:
            return f"interpreteur empaquete detecte ({sys.base_prefix})"
    return ""


def _candidate_env_files() -> list[pathlib.Path]:
    """Emplacements de .env essayes, dans l'ordre."""
    out: list[pathlib.Path] = []
    explicit = (os.environ.get("SHIPTIFY_ENV_FILE") or "").strip()
    if explicit:
        out.append(pathlib.Path(explicit))
    # La racine locale de la convention d'equipe : ~/.shiptify-mcp. Le profil
    # utilisateur existe des deux cotes, n'est pas virtualise par un Python
    # empaquete, et ne depend pas de l'identifiant d'installation du plugin.
    out.append(_local_root() / ".env")
    # Les trois emplacements suivants sont des HERITAGES, gardes en LECTURE pour
    # ne pas perdre la cle d'un poste configure avant la convention. Plus rien
    # n'y est ecrit : le magasin de _key_store_path est la racine locale.
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        out.append(pathlib.Path(plugin_data) / ".env")
    legacy = os.environ.get("LOCALAPPDATA")
    if legacy:
        out.append(pathlib.Path(legacy) / "shiptify-mcp" / ".env")
    out.append(pathlib.Path.home() / ".shiptify" / ".env")
    # Voisin du script : refuse si le vault est synchronise, mais on le regarde
    # quand meme pour pouvoir le dire clairement plutot que rester muet.
    out.append(pathlib.Path(__file__).resolve().parent / ".env")
    return out


def _parse_env(text: str) -> dict[str, str]:
    """Parseur .env minimal : KEY=VALUE, # en commentaire, guillemets optionnels.

    Une cle d'API peut contenir n'importe quoi (%, *, /, #). La valeur n'est
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
    """Charge le premier .env exploitable. Une variable du process **non vide** gagne.

    Le "non vide" n'est pas un detail. En mode plugin, la configuration arrive
    par l'environnement : `SHIPTIFY_API_KEY=${user_config.api_key}`. Si le
    collegue n'a pas encore renseigne le champ, la substitution pose une
    variable **vide**, et un `setdefault` la considererait comme renseignee -
    le .env du poste serait alors ignore en silence. On ne remplit donc que ce
    qui est vide ou absent.
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
                f"Un fichier .env a ete trouve dans un dossier synchronise "
                f"({path}) et n'a PAS ete lu : il porterait une cle d'API sur "
                "le drive partage de l'equipe. Deplace-le vers "
                "%LOCALAPPDATA%\\shiptify-mcp\\.env (c'est ce que fait "
                "install.ps1), ou pointe un emplacement local avec "
                "SHIPTIFY_ENV_FILE."
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
# Ce fichier porte TOUT ce qui est commun a l'equipe : le reglage - racine de
# l'API, schema d'authentification, plafond de pagination - **et la cle d'API**.
#
# Decision d'equipe du 2026-08-28. La cle Shiptify est une cle de service
# partagee : elle n'identifie personne, toute l'equipe la connait deja, et la
# faire ressaisir vingt-six fois n'ajoutait aucune protection - seulement
# vingt-six mises en service qui echouent et une rotation impossible a
# propager. Elle est donc posee une fois dans le fichier d'equipe.
#
# Ce que ca implique, et qu'il faut assumer : la bibliotheque est lisible par
# toute l'equipe L&T, donc la cle l'est aussi. Le jour ou elle doit cesser de
# l'etre - une cle nominative, un prestataire externe, un audit - la reponse est
# la configuration du plugin sur le poste, qui passe DEVANT le fichier d'equipe
# (voir _env), pas le retrait de cette ligne.
#
# La liste blanche RESTE, mais elle a change d'objet : ce n'est plus une
# barriere anti-secret, c'est un garde-fou contre la faute de frappe. Une cle
# mal orthographiee posee dans le fichier serait sinon ignoree en silence, et on
# chercherait longtemps pourquoi le reglage d'equipe ne prend pas. Elle est
# refusee **et signalee** par /shiptify-setup.
#
# Ce qui n'a toujours pas sa place ici : un chemin local. SHIPTIFY_EXPORT_DIR
# reste dans la liste par compatibilite, mais doit rester vide - un chemin
# d'export valable sur un poste n'existe pas sur les vingt-cinq autres.

SHARED_FILE_NAME = "shiptify.shared.env"
ENGINE_DIR_NAME = "08_ENGINE"
SHARED_SUBPATH = ("04_mcp", "00_config")

SHARED_ALLOWED_KEYS = frozenset(
    {
        "SHIPTIFY_BASE_URL",
        "SHIPTIFY_AUTH_PREFIX",
        "SHIPTIFY_ACCOUNT_ID",
        "SHIPTIFY_MAX_PAGES",
        "SHIPTIFY_TIMEOUT_S",
        "SHIPTIFY_EXPORT_DIR",
        # La cle de service de l'equipe. Autorisee ici le 2026-08-28.
        "SHIPTIFY_API_KEY",
    }
)

# Les cles du fichier d'equipe dont la VALEUR ne s'affiche jamais, meme dans un
# diagnostic. Le NOM de la cle, lui, se dit : c'est ce qui permet de savoir d'ou
# vient la valeur active sans la reveler.
SHARED_SECRET_KEYS = frozenset({"SHIPTIFY_API_KEY"})

_SHARED_CACHE: dict[str, str] | None = None
_SHARED_LOADED_FROM: str = ""
# Cles ignorees a la lecture, parce qu'absentes de la liste blanche. Remontees
# par setup_status : c'est presque toujours une faute de frappe, et elle ne se
# voit nulle part ailleurs.
_SHARED_REJECTED: list[str] = []


def _engine_roots() -> list[pathlib.Path]:
    """Racines `08_ENGINE` plausibles, dans l'ordre de preference.

    Le plugin est installe depuis `08_ENGINE/03_plugins/...`, mais Claude Code
    en fait une copie dans `~/.claude/plugins/`. On ne peut donc pas se
    contenter de remonter depuis le code : on remonte quand meme (cas de
    l'installation directe depuis la bibliotheque), et on complete par le
    profil utilisateur, ou OneDrive synchronise la bibliotheque d'equipe.
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

    # Remontee : le code tourne peut-etre encore dans la bibliotheque.
    starts = [pathlib.Path(__file__).resolve()]
    plugin_root = (os.environ.get("CLAUDE_PLUGIN_ROOT") or "").strip()
    if plugin_root:
        starts.append(pathlib.Path(plugin_root))
    for start in starts:
        for parent in start.parents:
            if parent.name == ENGINE_DIR_NAME:
                add(parent)
                break

    # Profil utilisateur : OneDrive pose la bibliotheque d'equipe sous
    # <profil>\CAFOM\<bibliotheque>\. Le nom exact de la bibliotheque varie
    # d'un poste a l'autre, on ne le devine pas, on le cherche.
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
    """Emplacements du fichier d'equipe essayes, dans l'ordre.

    **Un chemin explicite est EXCLUSIF.** Avant le 2026-08-28, la variable
    d'environnement etait ajoutee en tete puis la recherche continuait sous le
    profil : pointer un fichier precis ne desactivait donc pas le fichier
    d'equipe reel, il le mettait juste en second. Consequence mesuree, et elle
    n'est pas theorique : la suite de controles hors-ligne pointe un fichier
    inexistant pour simuler un poste sans configuration d'equipe, et elle
    tournait en fait avec les vraies cles - sept controles echouaient sur la
    machine de celui qui developpe, c'est-a-dire la seule ou la suite est
    lancee. Un garde-fou qui echoue toujours finit par etre ignore.

    Le meme piege vaut en exploitation : qui pointe un fichier de recette
    attend ce fichier, pas un repli silencieux sur la configuration de
    production.
    """
    out: list[pathlib.Path] = []
    explicit = (os.environ.get("SHIPTIFY_SHARED_ENV") or "").strip()
    if explicit:
        return [pathlib.Path(explicit)]
    for root in _engine_roots():
        out.append(root.joinpath(*SHARED_SUBPATH, SHARED_FILE_NAME))
    return out


def _load_shared_env() -> dict[str, str]:
    """Lit le premier fichier d'equipe trouve, filtre par la liste blanche.

    Ne leve jamais : un fichier d'equipe absent, illisible ou mal rempli ne
    doit pas empecher le connecteur de tourner sur la configuration du poste.
    Ce qui a ete refuse est garde de cote pour que setup_status le dise.

    Les valeurs lues ne sont PAS versees dans os.environ : elles restent dans ce
    cache, et c'est _env qui va les chercher en dernier recours. La cle d'equipe
    n'apparait donc pas dans l'environnement du processus, ni dans ce qu'un
    sous-processus en heriterait.
    """
    global _SHARED_CACHE, _SHARED_LOADED_FROM, _SHARED_REJECTED
    if _SHARED_CACHE is not None:
        return _SHARED_CACHE
    values: dict[str, str] = {}
    rejected: list[str] = []
    for path in _shared_env_candidates():
        try:
            if not path.is_file():
                continue
            raw = _parse_env(path.read_text(encoding="utf-8-sig"))
        except OSError:
            continue
        for key, val in raw.items():
            upper = key.strip().upper()
            if upper in SHARED_ALLOWED_KEYS:
                if val.strip():
                    values[upper] = val.strip()
            else:
                rejected.append(upper)
        _SHARED_LOADED_FROM = str(path)
        break
    _SHARED_REJECTED = rejected
    _SHARED_CACHE = values
    return values


def _shared_report() -> list[str]:
    """Les lignes que setup_status et doctor affichent sur le fichier d'equipe."""
    shared = _load_shared_env()
    lines: list[str] = []
    if _SHARED_LOADED_FROM:
        lines.append(f"Config d'equipe      : {_SHARED_LOADED_FROM}")
        lines.append(
            "  reglages repris    : "
            + (", ".join(sorted(shared)) if shared else "(aucun)")
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
    if _SHARED_REJECTED:
        lines.append("")
        lines.append(
            "  ATTENTION : le fichier d'equipe porte des cles que ce serveur ne "
            "connait pas, elles ont ete IGNOREES : "
            + ", ".join(sorted(set(_SHARED_REJECTED)))
            + "."
        )
        lines.append(
            "  Dans la quasi-totalite des cas, c'est une faute de frappe dans "
            "le nom de la variable : compare avec .env.example. Une cle ignoree "
            "ne produit aucune erreur ailleurs - c'est ici, et seulement ici, "
            "que ca se voit."
        )
    return lines


def _env(name: str, default: str = "") -> str:
    """La valeur retenue pour un reglage, dans un ordre de priorite fixe.

    1. l'environnement du processus - la configuration du plugin, et le .env
       local que _load_env_file y a deja verse : ce que ce poste a decide ;
    2. le fichier d'equipe de 08_ENGINE - ce que l'equipe a decide ;
    3. la valeur par defaut du serveur.

    Le poste passe donc devant l'equipe, et non l'inverse : un reglage
    d'equipe est un point de depart commun, pas une contrainte. C'est aussi ce
    qui permet a quelqu'un de tester une racine d'API de recette sans toucher
    au fichier partage.

    Depuis le 2026-08-28, cet ordre vaut aussi pour SHIPTIFY_API_KEY : la cle
    de service de l'equipe vient du niveau 2. Une cle saisie dans la
    configuration du plugin l'emporte donc, et c'est la sortie a utiliser pour
    un acces nominatif ou de recette - on ne retire pas la ligne du fichier
    partage, on la surcharge sur son poste.
    """
    _load_env_file()
    from_process = (os.environ.get(name) or "").strip()
    if from_process:
        return from_process
    from_team = _load_shared_env().get(name, "").strip()
    if from_team:
        return from_team
    return default.strip()


def _api_key() -> str:
    key = _env("SHIPTIFY_API_KEY")
    if not key:
        tried = "\n".join(f"  - {p}" for p in _candidate_env_files())
        packaged = _packaged_python()
        raise ConfigError(
            "SHIPTIFY_API_KEY manquant."
            + (f"\n\n{_ENV_LOAD_ERROR}" if _ENV_LOAD_ERROR else "")
            + (
                f"\n\nATTENTION : {packaged}. Un .env pose sous %LOCALAPPDATA% "
                "peut etre invisible pour ce processus (virtualisation). "
                "Renseigne la cle dans la configuration du plugin, ou pose le "
                ".env dans %CLAUDE_PLUGIN_DATA%, ou passe SHIPTIFY_ENV_FILE."
                if packaged
                else ""
            )
            + "\n\nEmplacements de .env essayes, dans l'ordre :\n"
            + tried
            + "\n\nPour la saisir : appelle shiptify_setup_status, qui donne la "
            "marche a suivre pour ce poste.\n\n"
            "En resume, en mode plugin : /plugin > shiptify > configuration > "
            "« Cle d'API Shiptify ». La saisie se fait dans l'interface de "
            "Claude Code, pas dans la conversation.\n\n"
            "En installation directe, cree le fichier avec install.ps1, a cote "
            "de server.py :\n"
            f'  powershell -ExecutionPolicy Bypass -File "{pathlib.Path(__file__).resolve().parent / "install.ps1"}"\n'
            "Modele des variables : .env.example."
        )
    return key


def _base_url() -> str:
    return _env("SHIPTIFY_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _auth_prefix() -> str:
    return _env("SHIPTIFY_AUTH_PREFIX", DEFAULT_AUTH_PREFIX)


def _auth_header() -> str:
    return f"{_auth_prefix()} {_api_key()}".strip()


def _account_id() -> str:
    return _env("SHIPTIFY_ACCOUNT_ID")


def _timeout() -> float:
    try:
        return float(_env("SHIPTIFY_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _max_pages() -> int:
    try:
        return max(1, int(_env("SHIPTIFY_MAX_PAGES") or DEFAULT_MAX_PAGES))
    except ValueError:
        return DEFAULT_MAX_PAGES


def _local_root() -> pathlib.Path:
    """Racine locale de l'outil : cache SQLite et exports.

    **Sous le profil utilisateur, pas sous %LOCALAPPDATA% ni sous le dossier de
    donnees du plugin**, et ce n'est pas un detail de gout.

    Deux raisons, mesurees. Un interpreteur Windows empaquete (Microsoft Store,
    Python Manager) donne a ses processus enfants une vue VIRTUALISEE de
    %LOCALAPPDATA% : le serveur lance par le plugin et le meme serveur lance en
    ligne de commande ne verraient pas le meme cache, sans aucune erreur. Et le
    dossier de donnees du plugin depend de l'identifiant d'installation : ce
    poste en porte deja deux (shiptify-inline et shiptify-vu-transport), donc un
    cache construit sous l'un serait invisible sous l'autre.

    Le profil, lui, n'est ni virtualise ni fonction de l'installation. C'est le
    choix qu'avait deja fait le connecteur Yooz.
    """
    # os.environ et NON _env : cette fonction est appelee pendant le chargement
    # du fichier .env, et passer par _env - qui declenche ce chargement - ferait
    # un aller-retour. Un chemin local n'a de toute facon pas sa place dans la
    # configuration d'equipe : il n'existe pas sur les autres postes.
    raw = (os.environ.get("SHIPTIFY_HOME") or "").strip()
    if raw:
        return pathlib.Path(raw)
    return pathlib.Path.home() / ".shiptify-mcp"


def _cache_path() -> pathlib.Path:
    raw = _env("SHIPTIFY_CACHE_DB")
    return pathlib.Path(raw) if raw else _local_root() / "shiptify_cache.sqlite"


def _export_dir() -> pathlib.Path:
    """Ou atterrissent les CSV.

    Le reglage explicite gagne ; sinon la racine locale, qui est la meme quel
    que soit le mode de lancement. L'ancien emplacement (dossier de donnees du
    plugin) n'est plus utilise en ecriture : voir _local_root.
    """
    raw = _env("SHIPTIFY_EXPORT_DIR")
    if raw:
        return pathlib.Path(raw)
    return _local_root() / "exports"


def _key_source() -> str:
    """D'ou sort la cle : la premiere question quand ca ne marche pas.

    Trois origines possibles depuis le 2026-08-28, et il faut savoir laquelle :
    une cle d'equipe qui ne marche plus se corrige dans 08_ENGINE, pour tout le
    monde ; une cle de poste se corrige dans /plugin, pour soi seul.
    """
    _load_env_file()
    if not os.environ.get("SHIPTIFY_API_KEY"):
        # Rien sur le poste : la cle vient peut-etre du fichier d'equipe.
        if _load_shared_env().get("SHIPTIFY_API_KEY"):
            return f"config d'equipe ({_SHARED_LOADED_FROM})"
        return "(aucune)"
    if "SHIPTIFY_API_KEY" in _ENV_FILE_KEYS:
        return f"fichier {_ENV_LOADED_FROM}"
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "configuration du plugin (userConfig)"
    return "environnement du processus"


def _fingerprint(secret: str) -> str:
    """Empreinte courte d'une cle, pour comparer sans jamais l'afficher."""
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def _key_store_path() -> pathlib.Path:
    """Ou la cle est rangee sur la machine, quand on la range.

    Le meme fichier que le serveur relit au demarrage : on reutilise la chaine
    de recherche de .env plutot que d'inventer un second format. On ecrit dans
    le premier emplacement fiable, jamais dans un dossier synchronise.
    """
    explicit = (os.environ.get("SHIPTIFY_ENV_FILE") or "").strip()
    if explicit:
        return pathlib.Path(explicit)
    # Un seul emplacement d'ecriture, le meme sur les deux systemes, et le meme
    # que le serveur relit en premier. Les anciens emplacements restent lus par
    # _candidate_env_files, ils ne sont plus ecrits.
    return _local_root() / ".env"


def _stored_key() -> str:
    """La cle telle qu'elle est ecrite sur la machine, ou "" si absente."""
    path = _key_store_path()
    try:
        if not path.is_file():
            return ""
        return _parse_env(path.read_text(encoding="utf-8-sig")).get(
            "SHIPTIFY_API_KEY", ""
        )
    except OSError:
        return ""


def _restrict_permissions(path: pathlib.Path) -> str:
    """Restreint le fichier au seul utilisateur courant. Rend un compte rendu."""
    if os.name != "nt":
        try:
            path.chmod(0o600)
            return "droits POSIX restreints a 0600"
        except OSError as exc:
            return f"droits POSIX non modifies : {exc}"
    user = os.environ.get("USERNAME") or ""
    if not user:
        return "droits NTFS non modifies : USERNAME inconnu"
    try:
        done = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"droits NTFS non modifies : {exc}"
    if done.returncode != 0:
        return f"droits NTFS non modifies : icacls a rendu {done.returncode}"
    return f"droits NTFS restreints a {user}"


def _write_key_store(key: str) -> tuple[pathlib.Path, str]:
    """Ecrit la cle dans le magasin local. Rend (chemin, note sur les droits).

    Idempotent : on retire toute ligne de cle existante et on en ecrit une.
    Le reste du fichier est preserve - il peut porter d'autres reglages.
    """
    path = _key_store_path()
    if _is_synced(path):
        raise ConfigError(
            f"Refus d'ecrire la cle dans un dossier synchronise ({path}) : elle "
            "partirait sur le drive partage de l'equipe. Definis "
            "SHIPTIFY_ENV_FILE sur un chemin local."
        )
    lines: list[str] = []
    try:
        if path.is_file():
            lines = [
                line
                for line in path.read_text(encoding="utf-8-sig").splitlines()
                if not re.match(r"\s*SHIPTIFY_API_KEY\s*=", line)
            ]
    except OSError as exc:
        raise ConfigError(f"Lecture impossible de {path} : {exc}") from exc

    if not lines:
        lines = [
            "# Configuration du serveur MCP shiptify.",
            "# Fichier local, hors du vault synchronise.",
            f"# Ecrit le {dt.date.today().isoformat()} par shiptify_save_key.",
            "",
        ]
    lines.append(f'SHIPTIFY_API_KEY="{key}"')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Ecriture impossible dans {path} : {exc}") from exc
    return path, _restrict_permissions(path)


def _mask(secret: str) -> str:
    if not secret:
        return "(non renseigne)"
    if len(secret) <= 6:
        return "*" * len(secret)
    return f"{secret[:2]}{'*' * (len(secret) - 5)}{secret[-3:]}"


# --------------------------------------------------------------------------
# Couche HTTP
# --------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    head = {
        "Accept": "application/json",
        "Authorization": _auth_header(),
        "User-Agent": "myrddin-mcp-shiptify/1.0",
    }
    account = _account_id()
    if account:
        head["X-Account-ID"] = account
    return head


def _clean_query(query: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in (query or {}).items():
        if val is None or val == "":
            continue
        if isinstance(val, bool):
            out[key] = "true" if val else "false"
        else:
            out[key] = str(val)
    return out


def _check_get_path(path: str) -> str:
    """Normalise et valide un chemin contre la liste blanche des GET."""
    path = path.strip()
    if "?" in path:
        raise ShiptifyError(
            "Passe les parametres de requete dans query_json, pas dans le chemin."
        )
    if not path.startswith("/"):
        path = "/" + path
    patterns = _get_patterns()
    for _, pattern in patterns:
        if pattern.match(path):
            return path
    # Tolere l'oubli du slash final (/shipments vs /shipments/) et l'inverse.
    alt = path[:-1] if path.endswith("/") else path + "/"
    for _, pattern in patterns:
        if pattern.match(alt):
            return alt
    raise ShiptifyError(
        f"Chemin inconnu ou non accessible en lecture : {path}\n"
        f"Ce serveur est en lecture seule : seuls les {len(patterns)} chemins "
        "GET du contrat OpenAPI Shiptify sont atteignables. "
        "Appelle shiptify_list_paths pour voir la liste."
    )


def _supports_paging(path: str) -> bool:
    """L'endpoint accepte-t-il limit/offset ?

    Tous ne les acceptent pas, et ceux qui ne les acceptent pas les REFUSENT :
    /carriers/active rend un HTTP 400 "limit is not allowed" si on les envoie.
    La reponse est dans le contrat, on la lit plutot que de la deviner.
    """
    params = _spec()["paths"].get(path, {}).get("parameters", [])
    names = {p["name"] for p in params if p.get("in") == "query"}
    return {"limit", "offset"} <= names


def _page_limit_for(path: str) -> int:
    return PAGE_LIMIT_VISITS if path.rstrip("/") == "/visits" else PAGE_LIMIT


_CLIENT: httpx.Client | None = None
_CLIENT_LOCK = threading.Lock()


def _client() -> httpx.Client:
    """Le client HTTP du processus, partage et garde ouvert.

    Un `httpx.get` par appel rouvre la connexion TLS a chaque page. Le gain
    mesure sur cette API est faible - elle repond en 0,15 s par page - mais il
    n'est jamais negatif, et il devient reel des qu'on enchaine des dizaines de
    pages ou qu'on interroge en parallele. httpx.Client est sur en usage
    concurrent, c'est ce qui permet le fan-out de _request_many.
    """
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = httpx.Client(
                    timeout=_timeout(),
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_keepalive_connections=8, max_connections=16
                    ),
                )
    return _CLIENT


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """Combien attendre avant de rejouer. L'en-tete du serveur fait foi."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if raw:
        try:
            return max(0.0, min(30.0, float(raw)))
        except ValueError:
            pass
    return min(8.0, 0.5 * (2 ** attempt))


def _request(path: str, query: dict[str, Any] | None = None) -> Any:
    url = _base_url() + path
    headers = _headers()
    params = _clean_query(query)
    resp: httpx.Response | None = None
    last_error = ""
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = _client().get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            # Coupure reseau, DNS, TLS : ca vaut une seconde tentative, pas
            # trois. Au-dela c'est une panne, pas un alea.
            last_error = str(exc)
            if attempt >= 1:
                raise ShiptifyError(f"Appel {path} impossible : {exc}") from exc
            time.sleep(0.5)
            continue
        if resp.status_code in RETRY_STATUSES and attempt < RETRY_ATTEMPTS - 1:
            time.sleep(_retry_after(resp, attempt))
            continue
        break
    if resp is None:
        raise ShiptifyError(f"Appel {path} impossible : {last_error}")

    if resp.status_code == 401:
        raise ShiptifyError(
            "HTTP 401 : cle d'API refusee. Verifie SHIPTIFY_API_KEY et "
            f"SHIPTIFY_AUTH_PREFIX (actuellement '{_auth_prefix()}'). "
            "Une cle revoquee rend le meme code."
        )
    if resp.status_code == 403:
        raise ShiptifyError(
            "HTTP 403 : cle valide mais compte non autorise sur cette "
            "ressource. Si l'API attend un compte explicite, renseigne "
            "SHIPTIFY_ACCOUNT_ID (shiptify_accounts liste les comptes "
            "autorises)."
        )
    if resp.status_code == 404:
        raise ShiptifyError(f"HTTP 404 sur {path} : ressource inexistante.")
    if resp.status_code == 429:
        raise ShiptifyError(
            f"HTTP 429 : quota d'appels atteint, et il l'etait encore apres "
            f"{RETRY_ATTEMPTS} tentatives espacees. Reduis max_rows, resserre le "
            "perimetre de dates, ou passe par le cache (shiptify_sync une fois, "
            "puis shiptify_sql autant de fois que voulu sans rappeler l'API)."
        )
    if resp.status_code >= 400:
        body = (resp.text or "")[:600]
        raise ShiptifyError(f"HTTP {resp.status_code} sur {path} | {body}")

    if not resp.content:
        return []
    try:
        return resp.json()
    except ValueError as exc:
        raise ShiptifyError(
            f"Reponse non JSON sur {path} : {(resp.text or '')[:300]}"
        ) from exc


def _rows(payload: Any) -> list[dict[str, Any]]:
    """L'API rend soit une liste, soit un objet {results|data|items: [...]}."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "items", "rows"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
        return [payload]
    return []


def _paginate(
    path: str,
    query: dict[str, Any] | None = None,
    max_rows: int = 1000,
    page_limit: int = PAGE_LIMIT,
    render_fields: str = "",
    render_budget: int = 0,
) -> tuple[list[dict[str, Any]], str]:
    """Boucle offset/limit jusqu'a page vide. Rend (lignes, raison_d_arret).

    La raison est une chaine vide quand la collection a ete lue en entier, et
    sinon dit POURQUOI on s'est arrete. Elle vaut mieux qu'un booleen : les
    trois arrets n'appellent pas la meme correction, et le rendu doit pouvoir le
    dire a l'utilisateur.

    L'API ne renvoie aucun total : la seule fin de collection fiable est une
    page vide. C'est la logique du script Power Query d'origine, et elle est
    conservee volontairement. S'arreter sur une page partielle ferait gagner un
    appel, mais sous-compterait en silence si l'API filtre apres avoir applique
    la limite - exactement le genre de chiffre faux qu'on ne veut pas citer.

    Deux corrections par rapport a la version d'avant le 2026-08-28.

    1. **La troncature n'est plus annoncee a tort.** L'ancien test `len(out) >=
       max_rows` declarait tronque un resultat qui faisait EXACTEMENT max_rows
       lignes et etait complet. Comme un rendu tronque n'est pas citable, un
       chiffre juste devenait inutilisable. On lit donc une ligne de plus que
       demande, et on ne parle de troncature que si elle existe.
    2. **On arrete de paginer ce qui ne sera pas affiche.** Le rendu est plafonne
       en caracteres, mais la pagination l'ignorait : sur 300 lignes demandees,
       la moitie etait ramenee puis jetee. render_budget ferme la boucle des que
       la projection depasse ce qui tiendra a l'ecran.
    """
    out: list[dict[str, Any]] = []
    offset = 0
    pages = 0
    cap = _max_pages()
    used = 0
    while True:
        page_query = dict(query or {})
        page_query["limit"] = page_limit
        page_query["offset"] = offset
        batch = _rows(_request(path, page_query))
        pages += 1
        if not batch:
            # Page vide : la collection est finie, quoi qu'il arrive ensuite.
            return out, ""
        out.extend(batch)
        if len(out) > max_rows:
            return out[:max_rows], "max_rows"
        if render_budget and len(batch) >= page_limit:
            # Le budget n'arrete la lecture que sur une page PLEINE. Une page
            # partielle veut dire qu'on touche la fin de la collection : payer
            # un appel de plus pour le confirmer vaut mieux qu'annoncer une
            # lecture incomplete alors qu'il ne restait rien - un rendu dit
            # tronque n'est pas citable, et le chiffre juste devient inutile.
            used += len(
                _to_csv_text(_select([_flatten(r) for r in batch], render_fields))
            )
            if used >= render_budget:
                return out, "budget"
        if pages >= cap:
            return out, "max_pages"
        offset += page_limit


def _request_many(calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """Joue plusieurs GET independants de front. Rend les charges dans l'ordre.

    Sert aux outils qui composent une reponse a partir de sous-ressources
    (un envoi et ses points de suivi, ses contenus, ses pieces jointes) : les
    enchainer en serie multipliait la latence par le nombre d'inclusions.
    Une erreur sur un appel est rendue comme valeur, pas levee : les autres
    sous-ressources restent utiles.
    """
    if not calls:
        return []
    if len(calls) == 1:
        path, query = calls[0]
        try:
            return [_request(path, query)]
        except (ConfigError, ShiptifyError) as exc:
            return [f"ERREUR : {exc}"]

    def one(item: tuple[str, dict[str, Any]]) -> Any:
        path, query = item
        try:
            return _request(path, query)
        except (ConfigError, ShiptifyError) as exc:
            return f"ERREUR : {exc}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(calls))) as pool:
        return list(pool.map(one, calls))


# --------------------------------------------------------------------------
# Aplatissement et rendu
# --------------------------------------------------------------------------

def _flatten(row: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Aplatit les objets imbriques en notation pointee.

    Reproduit ce que faisait Table.ExpandRecordColumn dans le script Power
    Query : address_dest.city, carrier.name, shipment_mode.id. Les listes sont
    rendues en JSON compact : elles n'ont pas de forme de colonne stable.
    """
    out: dict[str, Any] = {}
    for key, val in row.items():
        name = f"{prefix}{key}"
        if isinstance(val, dict):
            out.update(_flatten(val, prefix=f"{name}."))
        elif isinstance(val, list):
            out[name] = json.dumps(val, ensure_ascii=False, default=str) if val else ""
        else:
            out[name] = val
    return out


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    """Union des colonnes, dans l'ordre de premiere apparition."""
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    return list(seen)


def _select(rows: list[dict[str, Any]], fields: str) -> list[dict[str, Any]]:
    wanted = [f.strip() for f in fields.split(",") if f.strip()]
    if not wanted or wanted == ["*"]:
        return rows
    return [{f: r.get(f, "") for f in wanted} for r in rows]


def _to_csv_text(rows: list[dict[str, Any]], delimiter: str = ";") -> str:
    if not rows:
        return ""
    cols = _columns(rows)
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


_STOP_MESSAGES = {
    "max_rows": (
        "ATTENTION : resultat tronque, le plafond max_rows a ete atteint. Le "
        "compte ci-dessus n'est PAS un total : ne le cite pas comme un volume. "
        "Pour un COMPTE juste sur un large perimetre, utilise shiptify_summary, "
        "qui agrege cote serveur et ne ramene que le resultat."
    ),
    "max_pages": (
        "ATTENTION : resultat tronque, le garde-fou SHIPTIFY_MAX_PAGES a ete "
        "atteint. Le compte ci-dessus n'est PAS un total. Resserre le perimetre "
        "de dates, ou passe par shiptify_summary pour un compte, ou par le cache "
        "(shiptify_sync) pour un historique large."
    ),
    "budget": (
        "Lecture arretee sur le budget d'affichage : les lignes suivantes "
        "n'auraient pas ete affichees de toute facon, elles n'ont donc pas ete "
        "demandees a l'API. Le compte ci-dessus n'est PAS un total. Pour "
        "compter, utilise shiptify_summary ; pour voir plus de lignes, resserre "
        "les filtres ou la projection fields=..."
    ),
}


def _render_table(
    rows: list[dict[str, Any]],
    header: str,
    truncated: Any = "",
    fields: str = "",
    max_chars: int = RENDER_MAX_CHARS,
) -> str:
    """Rend une collection en CSV point-virgule, borne en taille.

    `truncated` porte la raison d'arret rendue par _paginate : chaine vide si la
    collection a ete lue en entier. Un booleen reste accepte pour les appels qui
    n'ont pas de pagination derriere eux.
    """
    flat = _select([_flatten(r) for r in rows], fields)
    lines = [header, f"{len(rows)} ligne(s) ramenees."]
    if truncated:
        lines.append(
            _STOP_MESSAGES.get(
                str(truncated),
                "ATTENTION : resultat tronque. Le compte ci-dessus n'est PAS un "
                "total : ne le cite pas comme un volume.",
            )
        )
    else:
        lines.append(
            "Lecture complete sur ce perimetre : la collection a ete parcourue "
            "jusqu'a la derniere page. Ce compte est citable, avec ses filtres."
        )
    if not flat:
        lines.append("(aucune ligne)")
        return "\n".join(lines)

    body = _to_csv_text(flat)
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
            f"Rendu limite a {max(0, len(kept) - 1)} ligne(s) sur "
            f"{len(flat)} ramenees, pour ne pas saturer la conversation. "
            "Restreins avec fields=... ou resserre les filtres. N'exporte en CSV "
            "que si l'utilisateur l'a demande."
        )
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def _render_json(payload: Any, header: str, max_chars: int = RENDER_MAX_CHARS) -> str:
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n... (tronque)"
    return f"{header}\n\n{body}"


def _guard(fn):
    """Rend les erreurs de configuration et d'API comme message lisible.

    functools.wraps n'est pas cosmetique ici : il pose __wrapped__, que
    inspect.signature suit. Sans lui, FastMCP lit la signature du wrapper et
    publie chaque outil avec deux parametres (args, kwargs) au lieu des vrais.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ConfigError, ShiptifyError) as exc:
            return f"ERREUR : {exc}"

    return wrapper


# --------------------------------------------------------------------------
# Le lexique metier : ce qui fait qu'une question en francais trouve sa colonne
# --------------------------------------------------------------------------
#
# Ce que ce bloc resout, et pourquoi il est dans le serveur plutot que dans la
# skill. Une skill est lue une fois, en debut de session, et le modele ne la
# relit pas avant chaque appel : le vocabulaire maison y est une intention, pas
# une garantie. Ici, c'est le serveur qui traduit, et il rend compte de ce qu'il
# a traduit dans l'entete de chaque reponse.
#
# LA confusion qu'il existe pour eviter : Shiptify porte le middle mile. Son
# referentiel de transporteurs est le panel d'affretement, pas le portefeuille
# de livraison. VIR n'y est pas comme transporteur - il y est comme DESTINATION,
# sous trois libelles differents (VIR, JP Home, JPH). Un filtre sur la seule
# chaine « VIR » dans carrier.name rend zero ligne, et zero ligne se lit comme
# « il n'y en a pas ».

_LEXIQUE: dict[str, Any] | None = None
_LEXIQUE_ERROR: str = ""


def _norm(text: Any) -> str:
    """Forme comparable d'un libelle : sans accent, sans casse, sans ponctuation.

    « Prevote I Meru », « prévoté » et « PREVOTE » doivent se rencontrer.
    """
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()


def _lexique() -> dict[str, Any]:
    """Le lexique livre avec le connecteur. Ne leve jamais.

    Un lexique absent ou mal forme ne doit pas empecher le connecteur de
    repondre : il degrade la comprehension, il ne casse pas la lecture.
    """
    global _LEXIQUE, _LEXIQUE_ERROR
    if _LEXIQUE is None:
        try:
            _LEXIQUE = json.loads(LEXIQUE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _LEXIQUE_ERROR = f"{LEXIQUE_FILE} illisible : {exc}"
            _LEXIQUE = {}
    return _LEXIQUE


def _alias(term: str) -> dict[str, Any] | None:
    """L'entree de lexique d'un terme, en suivant les renvois 'memeque'."""
    table = _lexique().get("alias") or {}
    key = _norm(term)
    index = {_norm(k): v for k, v in table.items()}
    entry = index.get(key)
    for _ in range(4):  # garde-fou contre un renvoi circulaire
        if not isinstance(entry, dict) or "memeque" not in entry:
            break
        entry = index.get(_norm(entry["memeque"]))
    return entry if isinstance(entry, dict) else None


_CARRIERS: list[dict[str, Any]] | None = None


def _carriers_index() -> list[dict[str, Any]]:
    """Le referentiel des transporteurs actifs, lu une fois par processus.

    119 lignes au releve du 2026-08-28, un appel de 0,28 s : le garder en
    memoire evite de le redemander a chaque resolution.
    """
    global _CARRIERS
    if _CARRIERS is None:
        _CARRIERS = _rows(_request("/carriers/active"))
    return _CARRIERS


def _resolve(term: str) -> dict[str, Any]:
    """Traduit un terme maison en champ + motifs de recherche.

    Rend toujours un dictionnaire, meme quand rien n'est trouve : c'est le
    connecteur qui doit dire « ce nom n'est pas ici, et voila ou il est »,
    plutot que de laisser sortir un tableau vide.
    """
    term = (term or "").strip()
    out: dict[str, Any] = {
        "terme": term,
        "canonique": term,
        "champ": "carrier.name",
        "motifs": [term] if term else [],
        "notes": [],
        "carriers": [],
        "connu_du_lexique": False,
    }
    if not term:
        return out

    entry = _alias(term)
    if entry:
        out["connu_du_lexique"] = True
        out["canonique"] = entry.get("canonique") or term
        out["champ"] = entry.get("champ") or "carrier.name"
        out["motifs"] = list(entry.get("motifs") or [term])
        for key in ("note", "attention", "source"):
            if entry.get(key):
                out["notes"].append(f"{key} : {entry[key]}")

    # Confrontation au referentiel vivant : le lexique dit ce qu'on cherche,
    # l'API dit ce qui existe reellement aujourd'hui.
    if out["champ"] == "carrier.name":
        motifs = [_norm(m) for m in out["motifs"] if _norm(m)]
        for row in _carriers_index():
            name = _norm(row.get("name"))
            if any(m in name for m in motifs):
                out["carriers"].append(
                    {"id": row.get("id"), "name": row.get("name"), "code": row.get("code")}
                )
        if len(out["carriers"]) > 1:
            out["notes"].append(
                f"{len(out['carriers'])} entites Shiptify portent ce nom : "
                + ", ".join(f"{c['name']} (id {c['id']})" for c in out["carriers"])
                + ". Un filtre sur un seul identifiant sous-compte."
            )
        if not out["carriers"]:
            portefeuille = (
                (_lexique().get("portefeuille_dernier_kilometre") or {}).get("noms") or []
            )
            connu = any(_norm(term) in _norm(n) or _norm(n) in _norm(term)
                        for n in portefeuille if _norm(n))
            if connu:
                out["notes"].append(
                    f"« {term} » est un transporteur du PORTEFEUILLE de livraison, mais "
                    "il n'apparait pas dans le referentiel des transporteurs Shiptify, "
                    "qui ne porte que le panel d'affretement middle mile. Ce n'est pas "
                    "une absence de flux : c'est la mauvaise source. Le cote facture "
                    "est dans Yooz (connecteur yooz-factures), le contrat et la "
                    "performance dans 02_TRANSPORTEURS/ de la bibliotheque d'equipe. "
                    "Verifie aussi address_dest.name : plusieurs prestataires du "
                    "dernier kilometre y apparaissent comme destination."
                )
            else:
                out["notes"].append(
                    f"Aucun transporteur actif Shiptify ne correspond a « {term} ». "
                    "Appelle shiptify_list_carriers pour voir les libelles reels, ou "
                    "cherche sur address_dest.name si c'est une destination."
                )
    return out


def _row_matches(flat: dict[str, Any], champ: str, motifs: list[str]) -> bool:
    """Le filtre cote client : le motif est-il dans la valeur du champ ?"""
    if not motifs:
        return True
    value = _norm(flat.get(champ))
    if not value:
        return False
    return any(_norm(m) in value for m in motifs if _norm(m))


def _apply_client_filter(
    rows: list[dict[str, Any]], champ: str, motifs: list[str]
) -> list[dict[str, Any]]:
    if not motifs:
        return rows
    return [r for r in rows if _row_matches(_flatten(r), champ, motifs)]


# --------------------------------------------------------------------------
# Agregation
# --------------------------------------------------------------------------
#
# Pourquoi le serveur agrege plutot que de laisser le modele compter. Mesure du
# 2026-08-28 : cent envois en colonnes completes pesent quarante mille tokens,
# et la projection courte quatre mille. Compter mille envois dans la
# conversation coute donc quarante mille tokens et un comptage a la main. Le
# meme compte agrege ici tient en trois cents.
#
# La contrepartie, et elle est assumee : l'agregat n'est juste que si le
# perimetre a ete lu en entier. Chaque rendu dit donc combien de lignes ont ete
# parcourues et si le parcours est alle jusqu'au bout.

# Les regroupements calcules, qui ne sont pas des colonnes de l'API.
_DERIVED_GROUPS = {
    "mois": 7,
    "month": 7,
    "jour": 10,
    "day": 10,
    "annee": 4,
    "year": 4,
}


def _date_column(rows: list[dict[str, Any]], prefer: str = "") -> str:
    """La colonne de date d'un regroupement temporel.

    Le choix n'est pas anodin, et il a produit un resultat trompeur avant d'etre
    explicite : un envoi porte `created_at` (quand il a ete cree) ET `date`
    (quand il part). Grouper par mois sur `date` alors qu'on a filtre sur
    `created_date_from` fait apparaitre des mois HORS du perimetre filtre -
    releve le 2026-08-28 : un filtre au 27 aout rendait un seau « 2026-09 ».

    On prefere donc la colonne qui correspond au filtre pose, et le rendu dit
    toujours laquelle a servi.
    """
    order = ["created_at", "date", "real_departure_time", "real_arrival_time"]
    if prefer:
        order = [prefer] + [c for c in order if c != prefer]
    for candidate in order:
        for row in rows:
            if str(row.get(candidate) or "").strip():
                return candidate
    return prefer or "created_at"


def _aggregate(
    flat: list[dict[str, Any]],
    group_by: str,
    metric: str,
    top: int,
    prefer_date: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Regroupe des lignes aplaties. Rend (lignes, total, libelle_du_groupe)."""
    key = (group_by or "").strip()
    cut = _DERIVED_GROUPS.get(key.lower())
    temporel = False
    if cut:
        temporel = True
        column = _date_column(flat, prefer_date)
        label = f"{key.lower()} ({column})"

        def bucket(row: dict[str, Any]) -> str:
            return str(row.get(column) or "")[:cut] or "(sans date)"
    else:
        label = key
        if key.lower() in ("semaine", "week"):
            temporel = True
            column = _date_column(flat, prefer_date)
            label = f"semaine ({column})"

            def bucket(row: dict[str, Any]) -> str:
                raw = str(row.get(column) or "")[:10]
                try:
                    day = dt.date.fromisoformat(raw)
                except ValueError:
                    return "(sans date)"
                year, week, _ = day.isocalendar()
                return f"{year}-S{week:02d}"
        else:

            def bucket(row: dict[str, Any]) -> str:
                return str(row.get(key, "") or "") or "(vide)"

    numeric = metric.strip().lower() not in ("", "nb", "count", "nombre")
    agg: dict[str, dict[str, Any]] = {}
    for row in flat:
        slot_key = bucket(row)
        slot = agg.setdefault(
            slot_key, {label: slot_key, "nb": 0, "somme": 0.0, "nb_sans_valeur": 0}
        )
        slot["nb"] += 1
        if numeric:
            value = row.get(metric)
            number = cache.to_number(value)
            if number is None:
                slot["nb_sans_valeur"] += 1
            else:
                slot["somme"] += number

    out = list(agg.values())
    for slot in out:
        if numeric:
            compte = slot["nb"] - slot["nb_sans_valeur"]
            slot["somme"] = round(slot["somme"], 2)
            slot["moyenne"] = round(slot["somme"] / compte, 2) if compte else ""
        else:
            slot.pop("somme", None)
            slot.pop("nb_sans_valeur", None)
    if temporel:
        # Une serie de mois se lit dans l'ordre du temps, pas du volume.
        out.sort(key=lambda s: str(s[label]))
    else:
        out.sort(
            key=lambda s: (-(s.get("somme") or 0), -s["nb"]) if numeric else -s["nb"]
        )

    total = {
        "nb": sum(s["nb"] for s in out),
        "groupes": len(out),
    }
    if numeric:
        total["somme"] = round(sum(s.get("somme") or 0 for s in out), 2)
        total["nb_sans_valeur"] = sum(s.get("nb_sans_valeur") or 0 for s in out)
    return out[: max(1, int(top))], total, label


# --------------------------------------------------------------------------
# Outils MCP
# --------------------------------------------------------------------------

mcp = FastMCP("shiptify")

# --- Mise en service : la cle d'API -------------------------------------
#
# La saisie se fait dans l'interface de Claude Code, par le champ userConfig
# `api_key` declare dans plugin.json (marque `sensitive`). Claude Code la
# collecte lui-meme et la passe au serveur par l'environnement : elle ne
# traverse jamais la conversation, donc elle n'entre ni dans le contexte du
# modele, ni dans la transcription.
#
# C'est pour cette raison qu'aucun outil ici n'accepte la cle en parametre.
# Un `shiptify_set_api_key("...")` serait plus direct a expliquer, mais il
# ferait passer le secret par le fil de la conversation - exactement ce que le
# champ `sensitive` existe pour eviter. `shiptify_save_key` ne prend donc aucun
# argument : il range sur la machine la cle deja saisie dans l'interface.


@mcp.tool()
@_guard
def shiptify_setup_status() -> str:
    """Ou en est la mise en service : la cle est-elle saisie, est-elle rangee ?

    A appeler en premier apres l'installation du plugin, et chaque fois qu'un
    outil repond que la cle manque. Ne rend jamais la cle en clair.
    """
    _load_env_file()
    active = _env("SHIPTIFY_API_KEY")
    stored = _stored_key()
    store = _key_store_path()
    plugin_mode = bool(os.environ.get("CLAUDE_PLUGIN_ROOT"))

    lines = ["Mise en service du connecteur Shiptify", ""]
    lines.append(f"Cle active           : {_mask(active) if active else 'AUCUNE'}")
    lines.append(f"Origine              : {_key_source()}")
    lines.append(f"Magasin sur machine  : {store}")
    lines.append(
        f"Cle rangee           : {'oui, ' + _mask(stored) if stored else 'non'}"
    )
    lines.extend(_shared_report())
    if active and stored and _fingerprint(active) != _fingerprint(stored):
        lines.append("")
        lines.append(
            "  NOTE : la cle rangee sur la machine n'est PAS celle utilisee. "
            "La configuration du plugin est prioritaire sur le fichier. "
            "Appelle shiptify_save_key pour aligner le fichier, ou "
            "shiptify_forget_key pour supprimer l'ancienne."
        )

    lines.append("")
    if not active:
        lines.append(
            "Rien d'actif. Le cas normal etant la valeur d'equipe, commence par "
            "la ligne « Config d'equipe » ci-dessus : si elle dit « aucune », "
            "c'est que 08_ENGINE/04_mcp/00_config n'est pas atteignable depuis "
            "ce poste (bibliotheque non synchronisee, ou posee ailleurs). "
            "Synchronise-la, ou pointe-la avec VU_ENGINE_DIR ou "
            "SHIPTIFY_SHARED_ENV. C'est la correction dans neuf cas sur dix."
        )
        lines.append("")
        lines.append("A DEFAUT - saisir la valeur dans l'interface de Claude Code :")
        lines.append("")
        if plugin_mode:
            lines.append("  1. tape /plugin")
            lines.append("  2. choisis le plugin « shiptify » (marketplace vu-transport)")
            lines.append("  3. ouvre sa configuration et renseigne « Cle d'API Shiptify »")
            lines.append("  4. redemarre la session pour que le serveur reprenne la cle")
        else:
            lines.append(
                "  Ce serveur ne tourne PAS comme plugin : il n'y a donc pas de "
                "champ de configuration. Deux options :"
            )
            lines.append("  - installer le plugin (voir le README du marketplace), ou")
            lines.append(
                f"  - poser la cle dans {store} via install.ps1 -ApiKey."
            )
        lines.append("")
        lines.append(
            "La cle n'est pas a coller dans la conversation : le champ de "
            "configuration la garde hors du contexte du modele."
        )
        packaged = _packaged_python()
        if packaged:
            lines.append("")
            lines.append(f"  A savoir : {packaged}.")
            lines.append(
                "  Un .env sous %LOCALAPPDATA% peut etre invisible a ce "
                "processus. Le champ de configuration du plugin, lui, marche "
                "toujours."
            )
        return "\n".join(lines)

    from_team = bool(
        _load_shared_env().get("SHIPTIFY_API_KEY")
        and not os.environ.get("SHIPTIFY_API_KEY")
    )
    if from_team:
        lines.append(
            "La cle active est celle de l'equipe, lue dans 08_ENGINE. Il n'y a "
            "RIEN a saisir sur ce poste : verifie la connexion avec "
            "shiptify_doctor et c'est fini."
        )
        lines.append("")
        lines.append(
            "  Deux cas ou tu saisirais quand meme quelque chose dans /plugin : "
            "un acces nominatif, ou un environnement de recette. Ce que le "
            "poste definit passe devant l'equipe, sans toucher au fichier "
            "partage."
        )
        lines.append(
            "  shiptify_save_key n'est utile que pour rendre ce poste autonome "
            "de la bibliotheque synchronisee : il recopie en local la valeur "
            "active."
        )
    elif not stored:
        lines.append(
            "La cle est saisie mais n'est PAS rangee sur la machine. Appelle "
            "shiptify_save_key pour l'ecrire dans le magasin local : elle "
            "survivra alors a une reinstallation du plugin, et l'installation "
            "directe la trouvera aussi."
        )
    else:
        lines.append("Tout est en place. Verifie la connexion avec shiptify_doctor.")
    return "\n".join(lines)


@mcp.tool()
@_guard
def shiptify_save_key() -> str:
    """Range sur la machine la cle deja saisie dans l'interface de Claude Code.

    Ne prend aucun argument, **volontairement** : la cle vient de la
    configuration du plugin, pas de la conversation. Elle n'a donc pas a etre
    recopiee dans un message pour etre rangee.

    Le fichier est ecrit hors de tout dossier synchronise, et ses droits sont
    restreints a l'utilisateur courant.
    """
    key = _api_key()  # leve une ConfigError explicite si rien n'est saisi
    path, permissions = _write_key_store(key)
    return "\n".join(
        [
            f"Cle rangee sur la machine : {path}",
            f"Empreinte : {_fingerprint(key)} (les 12 premiers caracteres du "
            "SHA-256, pour comparer sans afficher la cle)",
            f"Droits : {permissions}",
            "",
            "Ce fichier n'est pas synchronise et n'est pas versionne. Il sera "
            "relu automatiquement au prochain demarrage du serveur, meme si la "
            "configuration du plugin est perdue.",
            "",
            "Pour l'effacer : shiptify_forget_key.",
        ]
    )


@mcp.tool()
@_guard
def shiptify_forget_key() -> str:
    """Supprime la cle rangee sur la machine.

    Ne touche pas a la configuration du plugin : si la cle y est saisie, elle
    continuera d'etre utilisee. Pour la retirer completement, vide aussi le
    champ « Cle d'API Shiptify » dans /plugin.
    """
    path = _key_store_path()
    try:
        if not path.is_file():
            return f"Aucune cle rangee a supprimer ({path} n'existe pas)."
        lines = [
            line
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if not re.match(r"\s*SHIPTIFY_API_KEY\s*=", line)
        ]
        # S'il ne reste que des commentaires et du vide, on retire le fichier.
        if any(l.strip() and not l.strip().startswith("#") for l in lines):
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            note = f"Ligne de cle retiree de {path} (le reste du fichier est conserve)."
        else:
            path.unlink()
            note = f"Fichier supprime : {path}"
    except OSError as exc:
        raise ConfigError(f"Suppression impossible dans {path} : {exc}") from exc

    reste = "oui" if _env("SHIPTIFY_API_KEY") else "non"
    return "\n".join(
        [
            note,
            "",
            f"Une cle reste active pour cette session : {reste}. "
            "Elle vient alors de la configuration du plugin, qui n'est pas "
            "touchee ici - vide le champ dans /plugin pour la retirer aussi.",
        ]
    )


@mcp.tool()
def shiptify_doctor() -> str:
    """Diagnostic : configuration lue, cle presente, et vrai appel a l'API.

    Le premier outil a appeler quand quelque chose coince. Ne rend jamais la
    cle en clair.
    """
    _load_env_file()
    lines = ["Configuration du serveur MCP shiptify", ""]
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    lines.append(
        f"Mode                     : {'plugin (' + plugin_root + ')' if plugin_root else 'installation directe'}"
    )
    lines.append(f"Origine de la cle        : {_key_source()}")
    lines.append(f"Fichier .env lu          : {_ENV_LOADED_FROM or '(aucun)'}")
    if _ENV_LOAD_ERROR:
        lines.append(f"Avertissement            : {_ENV_LOAD_ERROR}")
    lines.append(f"Emplacements essayes     :")
    for path in _candidate_env_files():
        mark = "OK" if str(path) == _ENV_LOADED_FROM else "  "
        try:
            exists = "present" if path.is_file() else "absent"
        except OSError as exc:
            exists = f"illisible : {exc}"
        lines.append(f"  [{mark}] {path} ({exists})")
    packaged = _packaged_python()
    if packaged and not _env("SHIPTIFY_API_KEY"):
        lines.append("")
        lines.append(f"  ATTENTION : {packaged}.")
        lines.append(
            "  Un .env pose sous %LOCALAPPDATA% peut etre INVISIBLE pour ce "
            "processus (virtualisation du conteneur d'application) : il est "
            "alors rapporte 'absent' alors qu'il existe. Renseigne la cle dans "
            "la configuration du plugin, ou pose le .env dans "
            "%CLAUDE_PLUGIN_DATA%, ou passe SHIPTIFY_ENV_FILE."
        )
    lines.append("")
    lines.append(f"SHIPTIFY_BASE_URL        : {_base_url()}")
    lines.append(f"SHIPTIFY_AUTH_PREFIX     : {_auth_prefix()}")
    lines.append(f"SHIPTIFY_API_KEY         : {_mask(_env('SHIPTIFY_API_KEY'))}")
    lines.append(f"SHIPTIFY_ACCOUNT_ID      : {_account_id() or '(non renseigne)'}")
    lines.append(f"SHIPTIFY_MAX_PAGES       : {_max_pages()}")
    lines.append(f"SHIPTIFY_TIMEOUT_S       : {_timeout()}")
    lines.append(f"Dossier d'export         : {_export_dir()}")
    try:
        spec = _spec()
        lines.append(
            f"Chemins GET connus       : {len(spec['paths'])} "
            f"(contrat {spec.get('api_version')}, releve {spec.get('released')})"
        )
    except ConfigError as exc:
        lines.append(f"Chemins GET connus       : ERREUR - {exc}")

    lines.append("")
    lines.append("Appel de verification")
    if not _env("SHIPTIFY_API_KEY"):
        lines.append("  Aucune cle : appel non tente.")
        return "\n".join(lines)
    for path, label in (("/", "racine"), ("/accounts/", "comptes autorises")):
        try:
            payload = _request(path)
            preview = json.dumps(payload, ensure_ascii=False, default=str)[:300]
            lines.append(f"  GET {path} ({label}) : OK | {preview}")
        except (ConfigError, ShiptifyError) as exc:
            lines.append(f"  GET {path} ({label}) : ECHEC | {exc}")
    return "\n".join(lines)


@mcp.tool()
@_guard
def shiptify_list_paths(contains: str = "") -> str:
    """Liste les chemins GET atteignables et leurs parametres.

    A appeler avant shiptify_get, pour ne pas deviner un chemin ou un nom de
    filtre. `contains` filtre sur le chemin ou le resume.
    """
    spec = _spec()
    needle = contains.strip().lower()
    lines = [
        f"Chemins GET de l'API Shiptify ({len(spec['paths'])} au total, "
        f"contrat {spec.get('api_version')}, releve {spec.get('released')})",
        f"Source : {spec.get('source')}",
        "",
    ]
    shown = 0
    for path in sorted(spec["paths"]):
        info = spec["paths"][path]
        summary = info.get("summary", "")
        if needle and needle not in path.lower() and needle not in summary.lower():
            continue
        shown += 1
        lines.append(f"{path}  -  {summary}")
        for param in info.get("parameters", []):
            if param.get("in") == "header":
                continue
            bits = [f"{param['in']} {param['name']}", str(param.get("type") or "?")]
            if param.get("required"):
                bits.append("requis")
            if param.get("enum"):
                bits.append("valeurs " + ", ".join(str(v) for v in param["enum"]))
            desc = param.get("description") or ""
            line = "    " + " | ".join(bits)
            if desc:
                line += f" | {desc[:120]}"
            lines.append(line)
    if not shown:
        lines.append(f"(aucun chemin ne correspond a '{contains}')")
    return "\n".join(lines)


@mcp.tool()
@_guard
def shiptify_accounts(account_type: str = "") -> str:
    """Comptes Shiptify autorises pour cette cle d'API.

    account_type : 'shipper', 'carrier', ou vide pour les deux. A appeler en
    premier si un appel rend un HTTP 403 : l'identifiant a mettre dans
    SHIPTIFY_ACCOUNT_ID vient de la.
    """
    payload = _request("/accounts/", {"type": account_type})
    return _render_json(payload, "GET /accounts/")


@mcp.tool()
@_guard
def shiptify_list_shipments(
    created_date_from: str = "",
    created_date_to: str = "",
    departure_date_min: str = "",
    departure_date_max: str = "",
    arrival_date_min: str = "",
    arrival_date_max: str = "",
    shipment_request_id: int = 0,
    shipment_request_internal_ref: str = "",
    shipper_id: int = 0,
    from_address_id: int = 0,
    dest_address_id: int = 0,
    from_address_internal_ref: str = "",
    dest_address_internal_ref: str = "",
    carrier: str = "",
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Les envois (shipments), filtres et pagines. L'outil principal.

    Dates au format YYYY-MM-DD (ou YYYY-MM-DDTHH:mm:ss). fields est une liste
    de colonnes separees par des virgules, en notation pointee
    (carrier.name, address_dest.zipcode) ; '*' rend les 100+ colonnes.
    Par defaut, une projection courte des colonnes utiles.

    carrier accepte un nom maison ('VIR', 'XPO', 'Sennder') : il est traduit par
    le lexique d'equipe puis applique COTE CLIENT, l'API n'ayant aucun filtre
    transporteur sur /shipments/. shiptify_resolve dit ce qu'un terme devient.

    Pour COMPTER plutot que pour regarder, utilise shiptify_summary : il agrege
    cote serveur et ne rend que le resultat. Pour un volume qui depasse quelques
    centaines de lignes, resserre les filtres : la conversation n'est pas un
    entrepot. Ne bascule sur shiptify_export_csv que si l'utilisateur a demande
    un fichier.
    """
    query = {
        "created_date_from": created_date_from,
        "created_date_to": created_date_to,
        "departure_date_min": departure_date_min,
        "departure_date_max": departure_date_max,
        "arrival_date_min": arrival_date_min,
        "arrival_date_max": arrival_date_max,
        "sh_request_id": shipment_request_id or "",
        "sr_internal_ref": shipment_request_internal_ref,
        "shipper_id": shipper_id or "",
        "from_address_id": from_address_id or "",
        "dest_address_id": dest_address_id or "",
        "from_address_internal_ref": from_address_internal_ref,
        "dest_address_internal_ref": dest_address_internal_ref,
    }
    projection = fields or SHIPMENT_BRIEF
    res = _resolve(carrier) if carrier.strip() else None
    # Avec un filtre transporteur, le budget d'affichage n'a plus de sens : il
    # compterait des lignes qui vont justement etre ecartees. On parcourt donc
    # jusqu'a max_rows, et on dit combien ont ete lues puis retenues.
    rows, stopped = _paginate(
        "/shipments/",
        query,
        max_rows=max_rows,
        render_fields=projection,
        render_budget=0 if res else RENDER_MAX_CHARS,
    )
    header = "GET /shipments/ | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    if res:
        scanned = len(rows)
        rows = _apply_client_filter(rows, res["champ"], res["motifs"])
        header += (
            f"\nFiltre « {res['terme']} » -> {res['canonique']}, applique COTE "
            f"CLIENT sur {res['champ']} (motifs : {', '.join(res['motifs'])}) : "
            f"{scanned} ligne(s) parcourues, {len(rows)} retenues."
        )
        for note in res["notes"]:
            header += "\n  " + note
    return _render_table(rows, header, stopped, projection)


@mcp.tool()
@_guard
def shiptify_get_shipment(shipment_id: int, include: str = "") -> str:
    """Un envoi et, au choix, ses sous-ressources.

    include : liste separee par des virgules parmi tracking-points, contents,
    attachments, metadata, sscc. Vide = l'envoi seul.
    """
    allowed = {
        "tracking-points": f"/shipments/{int(shipment_id)}/tracking-points",
        "attachments": f"/shipments/{int(shipment_id)}/attachments",
        "metadata": f"/shipments/{int(shipment_id)}/metadata",
        "sscc": f"/shipments/{int(shipment_id)}/sscc",
        # contents n'existe en GET que sur le chemin galaxy.
        "contents": f"/galaxy/shipments/{int(shipment_id)}/contents",
    }
    out: dict[str, Any] = {}
    # Les sous-ressources sont independantes : les enchainer en serie
    # multipliait la latence par le nombre d'inclusions, pour rien.
    names = ["shipment"]
    calls: list[tuple[str, dict[str, Any]]] = [
        (f"/shipments/{int(shipment_id)}", {})
    ]
    for name in [x.strip() for x in include.split(",") if x.strip()]:
        if name not in allowed:
            out[name] = f"inconnu. Valeurs possibles : {', '.join(sorted(allowed))}"
            continue
        names.append(name)
        calls.append((allowed[name], {}))
    for name, payload in zip(names, _request_many(calls)):
        out[name] = payload
    return _render_json(out, f"Envoi {shipment_id}")


@mcp.tool()
@_guard
def shiptify_list_shipment_requests(
    internal_ref: str = "", max_rows: int = 300, fields: str = ""
) -> str:
    """Les demandes de transport (shipment requests), paginees.

    Dans le vocabulaire maison, c'est la demande envoyee vers l'agence ; les
    envois qui en decoulent se lisent avec shiptify_get_shipment_request
    (include=shipments).
    """
    rows, stopped = _paginate(
        "/shipment-requests/",
        {"internal_ref": internal_ref},
        max_rows=max_rows,
        render_fields=fields,
        render_budget=RENDER_MAX_CHARS,
    )
    return _render_table(rows, "GET /shipment-requests/", stopped, fields)


@mcp.tool()
@_guard
def shiptify_get_shipment_request(
    shipment_request_id: int = 0, internal_ref: str = "", include: str = ""
) -> str:
    """Une demande de transport, par identifiant ou par reference interne.

    include : shipments, pre-shipments, contents, attachments, metadata,
    invoicing, invoice-line, tracking-points.
    tracking-points et invoice-line ne sont accessibles que par reference
    interne cote API : renseigne internal_ref pour les obtenir.
    """
    if not shipment_request_id and not internal_ref:
        raise ShiptifyError(
            "Donne shipment_request_id ou internal_ref."
        )
    out: dict[str, Any] = {}
    sid = int(shipment_request_id) if shipment_request_id else 0
    ref = internal_ref.strip()

    if sid:
        out["shipment_request"] = _request(f"/shipment-requests/{sid}")

    by_id = {
        "shipments": f"/shipment-requests/{sid}/shipments",
        "pre-shipments": f"/shipment-requests/{sid}/pre-shipments",
        "contents": f"/shipment-requests/{sid}/contents",
        "attachments": f"/shipment-requests/{sid}/attachments",
        "metadata": f"/shipment-requests/{sid}/metadata",
        "invoicing": f"/shipment-requests/{sid}/invoicing/details",
        "invoice-line": f"/shipment-requests/{sid}/invoice-line",
    }
    by_ref = {
        "shipments": f"/shipment-requests/ref/{ref}/shipments",
        "pre-shipments": f"/shipment-requests/ref/{ref}/pre-shipments",
        "attachments": f"/shipment-requests/ref/{ref}/attachments",
        "invoice-line": f"/shipment-requests/ref/{ref}/invoice-line",
        "tracking-points": f"/shipment-requests/ref/{ref}/shipments/tracking-points",
    }
    names: list[str] = []
    calls: list[tuple[str, dict[str, Any]]] = []
    for name in [x.strip() for x in include.split(",") if x.strip()]:
        path = by_id.get(name) if sid else None
        if path is None and ref:
            path = by_ref.get(name)
        if path is None:
            known = sorted(set(by_id) | set(by_ref))
            out[name] = (
                f"non disponible avec ce mode d'acces. Valeurs possibles : "
                f"{', '.join(known)}"
            )
            continue
        names.append(name)
        calls.append((path, {}))
    for name, payload in zip(names, _request_many(calls)):
        out[name] = payload
    if not out:
        out["shipment_request"] = "aucune ressource demandee pour cette reference"
    label = f"Demande de transport {sid or ref}"
    return _render_json(out, label)


@mcp.tool()
@_guard
def shiptify_list_invoices(
    status: str = "",
    accounting_month: str = "",
    carrier_id: str = "",
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Les factures transporteur. accounting_month au format YYYY-MM.

    C'est l'entree du controle de facturation : la facture cote Shiptify, a
    rapprocher de l'estimation du back-office. Le detail ligne a ligne se lit
    avec shiptify_list_invoice_lines.
    """
    query = {
        "status": status,
        "accounting_month": accounting_month,
        "carrier_id": carrier_id,
    }
    rows, stopped = _paginate(
        "/invoices",
        query,
        max_rows=max_rows,
        render_fields=fields,
        render_budget=RENDER_MAX_CHARS,
    )
    header = "GET /invoices | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    return _render_table(rows, header, stopped, fields)


@mcp.tool()
@_guard
def shiptify_get_invoice(invoice_id: int) -> str:
    """Une facture par identifiant, avec ses rattachements."""
    return _render_json(
        _request(f"/invoices/{int(invoice_id)}"), f"Facture {invoice_id}"
    )


@mcp.tool()
@_guard
def shiptify_list_invoice_lines(
    status: str = "",
    account_id: int = 0,
    carrier_id: int = 0,
    from_pick_up_date: str = "",
    to_pick_up_date: str = "",
    from_delivery_date: str = "",
    to_delivery_date: str = "",
    from_accounting_date: str = "",
    to_accounting_date: str = "",
    max_rows: int = 500,
    fields: str = "",
) -> str:
    """Les lignes de facturation, filtrees par transporteur, dates ou statut.

    status : new, started, accepted, blocked, closed, not_priced,
    ready_to_invoice.
    Dates d'enlevement et de livraison au format YYYY-MM-DD ; dates comptables
    au format YYYY-MM.

    La granularite utile pour un ecart de facturation : c'est ici que se lit le
    prix ligne a ligne.
    """
    query = {
        "status": status,
        "account_id": account_id or "",
        "carrier_id": carrier_id or "",
        "from_pick_up_date": from_pick_up_date,
        "to_pick_up_date": to_pick_up_date,
        "from_delivery_date": from_delivery_date,
        "to_delivery_date": to_delivery_date,
        "from_accounting_date": from_accounting_date,
        "to_accounting_date": to_accounting_date,
    }
    rows, stopped = _paginate(
        "/galaxy/invoice-lines",
        query,
        max_rows=max_rows,
        render_fields=fields,
        render_budget=RENDER_MAX_CHARS,
    )
    header = "GET /galaxy/invoice-lines | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    return _render_table(rows, header, stopped, fields)


@mcp.tool()
@_guard
def shiptify_list_orders(
    calculated_departure_date_from: str = "",
    calculated_departure_date_to: str = "",
    shipment_id: int = 0,
    shipment_request_id: int = 0,
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Les commandes (orders), filtrees par date de depart estimee ou par envoi."""
    query = {
        "calculated_departure_date_from": calculated_departure_date_from,
        "calculated_departure_date_to": calculated_departure_date_to,
        "shipment_id": shipment_id or "",
        "shipment_request_id": shipment_request_id or "",
    }
    rows, stopped = _paginate(
        "/orders",
        query,
        max_rows=max_rows,
        render_fields=fields,
        render_budget=RENDER_MAX_CHARS,
    )
    header = "GET /orders | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    return _render_table(rows, header, stopped, fields)


@mcp.tool()
@_guard
def shiptify_list_locations(
    q: str = "", internal_ref: str = "", max_rows: int = 300, fields: str = ""
) -> str:
    """Les lieux (agences, entrepots, points de livraison).

    q cherche dans nom, adresse, ville, pays, reference interne. Utile pour
    retrouver le code d'une agence avant de filtrer les envois dessus.
    """
    rows, stopped = _paginate(
        "/locations",
        {"q": q, "internal_ref": internal_ref},
        max_rows=max_rows,
        render_fields=fields,
        render_budget=RENDER_MAX_CHARS,
    )
    return _render_table(rows, "GET /locations", stopped, fields)


@mcp.tool()
@_guard
def shiptify_list_carriers(
    contains: str = "", internal_ref: str = "", fields: str = ""
) -> str:
    """Les transporteurs actifs du compte. `contains` cherche dans le nom.

    ATTENTION AU PERIMETRE : ce referentiel est le panel d'AFFRETEMENT middle
    mile, pas le portefeuille de livraison. Un transporteur du dernier
    kilometre (VIR, TAMDIS, GLS, Bring...) n'y est pas, et son absence ici ne
    veut pas dire qu'il n'a pas de flux - elle veut dire que la question ne se
    pose pas dans Shiptify. shiptify_resolve le dit terme par terme.

    Un meme nom porte souvent plusieurs identifiants, un par implantation :
    XPO en a quatre. Filtrer sur un seul sous-compte.

    A croiser avec l'annuaire du contexte d'equipe
    (01_CONTEXTE/PORTEFEUILLE_ET_ZONES.md) : un nom Shiptify n'est pas toujours
    le nom maison du transporteur.
    """
    rows = _rows(_request("/carriers/active", {"internal_ref": internal_ref}))
    total = len(rows)
    needle = _norm(contains)
    if needle:
        rows = [r for r in rows if needle in _norm(r.get("name"))]
    header = (
        f"GET /carriers/active | {total} transporteur(s) actif(s)"
        + (f", {len(rows)} correspondant a « {contains} »" if needle else "")
        + "\nCe referentiel est le panel d'affretement middle mile, pas le "
        "portefeuille de livraison."
    )
    return _render_table(rows, header, "", fields or "id,name,code,scac,internal_ref")


@mcp.tool()
@_guard
def shiptify_list_events(
    event: str,
    date_from: str = "",
    date_to: str = "",
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Le journal d'evenements. `event` est obligatoire cote API.

    Valeurs : create_shipment, cancel_shipment, cancel_shipment_request,
    reactivate_shipment, update_shipment_contents, update_tracking,
    update_shipment_dates, added_shipment_to_group,
    removed_shipment_from_group, update_shipment_request_price,
    accept_shipment_request_price, refuse_shipment_request_price.
    """
    query = {"event": event, "date_from": date_from, "date_to": date_to}
    rows, stopped = _paginate(
        "/events",
        query,
        max_rows=max_rows,
        render_fields=fields,
        render_budget=RENDER_MAX_CHARS,
    )
    header = "GET /events | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    return _render_table(rows, header, stopped, fields)


_DICTIONARIES = {
    "shipment-modes": "/dictionary/shipment-modes",
    "metadata-prototypes": "/dictionary/metadata-prototypes",
    "metadata-prototypes-active": "/metadata-prototypes/active",
    "causes": "/dictionary/causes",
    "incidents": "/dictionary/incidents",
    "claims": "/dictionary/claims",
    "attachment-types": "/dictionary/attachment-types",
    "dangerous-goods": "/dictionary/dangerous-goods",
    "shipment-price-details": "/dictionary/shipment-price-details",
    "additional-services": "/dictionary/additional-services",
    "additional-services-active": "/additional-services/active",
    "specificities": "/specificities",
    "accounting-entities": "/accounting-entities",
    "price-details": "/price-details",
    "content-types": "/content-types",
    "content-types-active": "/content-types/active",
    "tags": "/tags",
    "freight-units": "/freight-units",
}


@mcp.tool()
@_guard
def shiptify_dictionary(name: str = "") -> str:
    """Les referentiels : modes, causes, incidents, litiges, services, tags...

    Appeler sans argument liste les referentiels disponibles. Ce sont les
    valeurs que l'API accepte et rend : les lire evite de deviner un libelle.
    """
    key = name.strip().lower()
    if not key:
        return "Referentiels disponibles :\n" + "\n".join(
            f"  {k:32} {v}" for k, v in sorted(_DICTIONARIES.items())
        )
    if key not in _DICTIONARIES:
        raise ShiptifyError(
            f"Referentiel inconnu : {name}. Valeurs : "
            + ", ".join(sorted(_DICTIONARIES))
        )
    path = _DICTIONARIES[key]
    rows = _rows(_request(path))
    return _render_table(rows, f"GET {path}", False)


@mcp.tool()
@_guard
def shiptify_export_csv(
    path: str,
    query_json: str = "{}",
    filename: str = "",
    max_rows: int = 100000,
) -> str:
    """Pagine une collection entiere et l'ecrit en CSV, sans charger la conversation.

    ECRIT UN FICHIER SUR LE DISQUE. A n'appeler que si l'utilisateur a demande
    un fichier, un export, un CSV ou un classeur. Sinon, reponds dans la
    session avec les outils de liste et une projection fields serree.

    C'est l'equivalent du script Power Query : meme pagination, meme
    aplatissement des objets imbriques en colonnes pointees
    (address_dest.city, carrier.name). CSV point-virgule, UTF-8 avec BOM,
    ouvrable directement dans Excel FR.

    path : un chemin GET de collection, par exemple /shipments/ ou
    /galaxy/invoice-lines. query_json : les filtres, en JSON.
    Le fichier va dans le dossier de donnees du plugin, sauf si export_dir
    (SHIPTIFY_EXPORT_DIR) est renseigne. Le chemin exact est rendu en reponse.
    """
    checked = _check_get_path(path)
    try:
        query = json.loads(query_json or "{}")
    except ValueError as exc:
        raise ShiptifyError(f"query_json n'est pas du JSON valide : {exc}") from exc
    if not isinstance(query, dict):
        raise ShiptifyError("query_json doit etre un objet JSON.")

    if _supports_paging(checked):
        # Aucun budget de rendu ici : un export a vocation a etre complet, il ne
        # s'arrete pas a ce qui tiendrait dans la conversation.
        rows, stopped = _paginate(
            checked, query, max_rows=max_rows, page_limit=_page_limit_for(checked)
        )
    else:
        # Referentiels et collections non paginees : un seul appel, et surtout
        # pas de limit/offset, que ces endpoints refusent.
        rows = _rows(_request(checked, query))
        stopped = "max_rows" if len(rows) > max_rows else ""
        rows = rows[:max_rows]
    flat = [_flatten(r) for r in rows]

    target_dir = _export_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ShiptifyError(f"Dossier d'export inutilisable ({target_dir}) : {exc}") from exc

    if filename.strip():
        name = filename.strip()
        if not name.lower().endswith(".csv"):
            name += ".csv"
    else:
        slug = re.sub(r"[^a-z0-9]+", "_", checked.lower()).strip("_") or "export"
        name = f"shiptify_{slug}_{dt.date.today().isoformat()}.csv"
    target = target_dir / name

    try:
        # newline="" plutot que write_text : sans lui, l'ecriture traduit
        # les fins de ligne en CRLF sous Windows et les laisse en LF sous
        # macOS. Le meme code produirait deux fichiers differents selon le
        # poste, ce que la convention de construction interdit
        # (08_ENGINE/04_mcp/README.md).
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            handle.write(_to_csv_text(flat))
    except OSError as exc:
        raise ShiptifyError(f"Ecriture impossible dans {target} : {exc}") from exc

    cols = _columns(flat)
    out = [
        f"Export termine : {target}",
        f"{len(flat)} ligne(s), {len(cols)} colonne(s).",
        f"Requete : GET {checked} | filtres : "
        + json.dumps(_clean_query(query), ensure_ascii=False),
    ]
    if stopped:
        out.append(
            f"ATTENTION : export tronque ({stopped}). Le fichier n'est PAS le "
            "perimetre complet : resserre les dates, releve SHIPTIFY_MAX_PAGES, "
            "ou construis l'export depuis le cache local, qui n'a pas ce plafond "
            "(shiptify_sync puis shiptify_export_sql)."
        )
    else:
        out.append(
            "Lecture complete : la collection a ete parcourue jusqu'a la "
            "derniere page sur ce perimetre."
        )
    if cols:
        out.append("")
        out.append("Colonnes : " + ", ".join(cols[:60]))
        if len(cols) > 60:
            out.append(f"... et {len(cols) - 60} autres.")
    return "\n".join(out)


@mcp.tool()
@_guard
def shiptify_get(
    path: str, query_json: str = "{}", max_rows: int = 200, fields: str = ""
) -> str:
    """Appelle n'importe quel chemin GET du contrat Shiptify. Echappatoire.

    Pour les 70 chemins GET qui n'ont pas d'outil dedie : visites, creneaux,
    points de suivi, unites de fret, pieces jointes, chemins carrier/galaxy.
    Appelle shiptify_list_paths d'abord pour le chemin exact et ses filtres.

    Un chemin absent de la liste blanche est refuse, et seule la methode GET
    est emise : aucune ecriture n'est possible par cet outil.
    """
    checked = _check_get_path(path)
    try:
        query = json.loads(query_json or "{}")
    except ValueError as exc:
        raise ShiptifyError(f"query_json n'est pas du JSON valide : {exc}") from exc
    if not isinstance(query, dict):
        raise ShiptifyError("query_json doit etre un objet JSON.")

    header = f"GET {checked} | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    if _supports_paging(checked) and "limit" not in query and "offset" not in query:
        rows, stopped = _paginate(
            checked,
            query,
            max_rows=max_rows,
            page_limit=_page_limit_for(checked),
            render_fields=fields,
            render_budget=RENDER_MAX_CHARS,
        )
        return _render_table(rows, header, stopped, fields)
    payload = _request(checked, query)
    rows = _rows(payload)
    if isinstance(payload, list) or (rows and len(rows) > 1):
        stopped = "max_rows" if len(rows) > max_rows else ""
        return _render_table(rows[:max_rows], header, stopped, fields)
    return _render_json(payload, header)


# --------------------------------------------------------------------------
# Le vocabulaire maison, expose comme outil
# --------------------------------------------------------------------------

@mcp.tool()
@_guard
def shiptify_lexique(sujet: str = "") -> str:
    """Le vocabulaire maison que ce connecteur comprend : alias, pieges, zones.

    A appeler quand une question porte un nom maison (VIR, JP Home, Moulins,
    Pole Sud, LX) et qu'on ne sait pas a quelle colonne il correspond, ou avant
    de conclure qu'un transporteur n'a pas de flux.

    sujet filtre l'affichage : 'alias', 'pieges', 'prestations', 'zones',
    'portefeuille', 'questions'. Vide = tout, en resume.
    """
    lex = _lexique()
    if not lex:
        raise ShiptifyError(
            f"Lexique indisponible. {_LEXIQUE_ERROR or 'fichier absent'}. Le "
            "connecteur fonctionne quand meme, mais il ne traduit plus les noms "
            "maison : passe les libelles exacts de l'API."
        )
    want = _norm(sujet)
    lines = [
        f"Lexique du connecteur shiptify, version {lex.get('version')} "
        f"(mis a jour le {lex.get('updated')} par {lex.get('updated_by')}).",
        "Source : 01_CONTEXTE/ de la bibliotheque d'equipe. Aucun chiffre ici.",
        "",
    ]

    if not want or want in ("ou", "champs", "alias"):
        lines.append("== Ou chercher quoi ==")
        for note in (lex.get("ou_chercher_quoi") or {}).get("_pourquoi", []):
            lines.append("  " + note)
        for key, val in (lex.get("ou_chercher_quoi") or {}).items():
            if not key.startswith("_"):
                lines.append(f"  {key:<22} {val}")
        lines.append("")

    if not want or want == "alias":
        lines.append("== Alias reconnus ==")
        for name, entry in sorted((lex.get("alias") or {}).items()):
            if "memeque" in entry:
                lines.append(f"  {name:<16} -> voir « {entry['memeque']} »")
                continue
            lines.append(
                f"  {name:<16} {entry.get('canonique')} | champ {entry.get('champ')} "
                f"| motifs {', '.join(entry.get('motifs') or [])}"
            )
            if entry.get("note"):
                lines.append(f"                   {entry['note']}")
            if entry.get("attention"):
                lines.append(f"                   ATTENTION : {entry['attention']}")
        lines.append("")

    if not want or want.startswith("piege"):
        lines.append("== Pieges de nommage ==")
        for key, val in sorted((lex.get("pieges_de_nommage") or {}).items()):
            if not key.startswith("_"):
                lines.append(f"  {key:<20} {val}")
        lines.append("")

    if not want or want.startswith("prestation"):
        lines.append("== Prestations ==")
        for key, val in (lex.get("prestations") or {}).items():
            if not key.startswith("_"):
                lines.append(f"  {key:<6} {val}")
        lines.append("")

    if not want or want.startswith("zone"):
        lines.append("== Zones par pays ==")
        for key, val in (lex.get("zones") or {}).items():
            if not key.startswith("_"):
                lines.append(f"  {key:<4} {val}")
        lines.append("")

    if want.startswith("portefeuille"):
        bloc = lex.get("portefeuille_dernier_kilometre") or {}
        lines.append("== Portefeuille de livraison (hors panel middle mile) ==")
        for note in bloc.get("_pourquoi", []):
            lines.append("  " + note)
        lines.append("  " + ", ".join(bloc.get("noms") or []))
        lines.append("")

    if not want or want.startswith("question"):
        lines.append("== Questions courantes et outil a employer ==")
        for item in lex.get("questions_frequentes") or []:
            lines.append(f"  « {item['question']} »")
            lines.append(f"      {item['outil']} : {item['appel']}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
@_guard
def shiptify_resolve(terme: str) -> str:
    """Traduit un nom maison en filtre Shiptify reel, et dit ce qui existe.

    A appeler AVANT de conclure qu'un transporteur n'a pas de flux. Croise le
    lexique d'equipe et le referentiel vivant de l'API : rend le champ a
    filtrer, les motifs a chercher, et les identifiants Shiptify reels.

    Exemple : « VIR » rend champ=address_dest.name et motifs=VIR, JP HOME, JPH,
    parce que VIR est l'ancien nom de JP Home et que les deux libelles
    coexistent sur les memes agences.
    """
    res = _resolve(terme)
    lines = [
        f"« {res['terme']} » -> {res['canonique']}",
        f"  connu du lexique  : {'oui' if res['connu_du_lexique'] else 'non'}",
        f"  champ a filtrer   : {res['champ']}",
        f"  motifs a chercher : {', '.join(res['motifs']) or '(aucun)'}",
    ]
    if res["carriers"]:
        lines.append(f"  transporteurs Shiptify correspondants ({len(res['carriers'])}) :")
        for car in res["carriers"]:
            lines.append(f"      id {car['id']:<7} {car['name']}")
    for note in res["notes"]:
        lines.append(f"  {note}")
    lines.append("")
    lines.append(
        "Le filtre transporteur de /shipments/ n'existe pas cote API : il est "
        "applique cote client sur les lignes ramenees. Sur un resultat tronque, "
        "il ne donne donc pas un compte."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Agregation : la reponse en un appel
# --------------------------------------------------------------------------

@mcp.tool()
@_guard
def shiptify_summary(
    group_by: str = "carrier.name",
    metric: str = "nb",
    created_date_from: str = "",
    created_date_to: str = "",
    departure_date_min: str = "",
    departure_date_max: str = "",
    arrival_date_min: str = "",
    arrival_date_max: str = "",
    carrier: str = "",
    max_scan: int = 20000,
    top: int = 30,
) -> str:
    """Compte et somme les envois par transporteur, pays, mois... EN UN APPEL.

    C'est l'outil a employer des qu'une question commence par « combien »,
    « quel est le plus », « repartition », « par mois », « par pays ». Il pagine
    le perimetre, agrege cote serveur, et ne rend que le resultat : une question
    qui coutait quarante mille tokens en tient trois cents.

    group_by  une colonne aplatie (carrier.name, address_dest.country,
              address_dest.city, status, shipment_mode.name, address_from.name,
              address_dest.name), ou un regroupement calcule : 'mois',
              'semaine', 'jour', 'annee'.
    metric    'nb' (defaut) pour un comptage, ou une colonne numerique a
              sommer : cost, price, total_weight, total_volume,
              total_linear_meters. La moyenne et le nombre de lignes SANS
              valeur sont rendus avec la somme - une somme sur une colonne
              remplie a 20 % n'est pas un montant.
    carrier   un nom maison ('VIR', 'XPO', 'Sennder'). Il est traduit par le
              lexique puis applique COTE CLIENT, l'API n'ayant pas de filtre
              transporteur sur /shipments/. shiptify_resolve dit ce qu'il
              devient.
    max_scan  plafond de lignes parcourues. Le rendu dit toujours combien ont
              ete lues et si le parcours est alle jusqu'au bout : un agregat sur
              un parcours incomplet n'est pas un total.
    """
    query = {
        "created_date_from": created_date_from,
        "created_date_to": created_date_to,
        "departure_date_min": departure_date_min,
        "departure_date_max": departure_date_max,
        "arrival_date_min": arrival_date_min,
        "arrival_date_max": arrival_date_max,
    }
    if not _clean_query(query):
        raise ShiptifyError(
            "Aucun filtre de date : le perimetre serait la base entiere. Donne "
            "au moins created_date_from, ou une borne de depart ou d'arrivee."
        )

    # La colonne de date du regroupement suit le filtre pose : grouper par mois
    # sur la date de depart alors qu'on a filtre sur la date de creation fait
    # apparaitre des mois hors perimetre.
    prefer = "created_at"
    if departure_date_min or departure_date_max:
        prefer = "date"
    if arrival_date_min or arrival_date_max:
        prefer = "real_arrival_time"

    res = _resolve(carrier) if carrier.strip() else None
    # Pas de budget de rendu ici : on agrege, donc on veut parcourir le
    # perimetre, pas seulement ce qui tiendrait a l'ecran.
    rows, stopped = _paginate("/shipments/", query, max_rows=int(max_scan))
    scanned = len(rows)
    flat = [_flatten(r) for r in rows]
    if res:
        flat = [f for f in flat if _row_matches(f, res["champ"], res["motifs"])]

    groups, total, label = _aggregate(flat, group_by, metric, top, prefer)

    lines = [
        "GET /shipments/ agrege | filtres serveur : "
        + json.dumps(_clean_query(query), ensure_ascii=False),
        f"Regroupement : {label} | mesure : {metric}",
        f"{scanned} ligne(s) parcourues"
        + (f", {len(flat)} retenues apres filtre transporteur" if res else ""),
    ]
    if stopped:
        lines.append(
            f"ATTENTION : parcours INCOMPLET (arret sur {stopped}). Les chiffres "
            "ci-dessous portent sur ce qui a ete lu, ce ne sont PAS des totaux. "
            "Resserre les dates, ou releve max_scan, ou passe par le cache "
            "(shiptify_sync puis shiptify_sql), qui n'a pas ce plafond."
        )
    else:
        lines.append(
            "Parcours COMPLET sur ce perimetre : ces chiffres sont citables, "
            "avec leurs filtres et leurs bornes de dates."
        )
    if res:
        lines.append(
            f"Filtre transporteur « {res['terme']} » -> {res['canonique']}, "
            f"applique COTE CLIENT sur {res['champ']} "
            f"(motifs : {', '.join(res['motifs'])})."
        )
        for note in res["notes"]:
            lines.append("  " + note)
    lines.append("")
    if not groups:
        lines.append("(aucune ligne sur ce perimetre)")
        return "\n".join(lines)
    lines.append(_to_csv_text(groups))
    lines.append("TOTAL : " + json.dumps(total, ensure_ascii=False))
    if total.get("nb_sans_valeur"):
        lines.append(
            f"ATTENTION : {total['nb_sans_valeur']} ligne(s) sur {total['nb']} "
            f"n'ont aucune valeur dans « {metric} ». La somme ne porte donc pas "
            "sur tout le perimetre - dis-le si tu cites ce montant."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Le cache local : l'historique, sans replafonner a 6 000 lignes
# --------------------------------------------------------------------------
#
# Ce que le cache debloque, et que le direct ne peut pas faire :
#   - un historique qui depasse le garde-fou de pagination ;
#   - la MEME question reposee demain sans reconsommer l'API ;
#   - du SQL libre, donc des croisements que l'API n'expose pas.
#
# Ce qu'il ne fait pas : il n'est pas l'etat courant. Une donnee qui a bouge
# dans Shiptify depuis la synchro n'est pas ici. Pour l'etat du jour, c'est le
# direct qui fait foi - et chaque rendu du cache affiche sa date de synchro.

SYNC_SETS: dict[str, dict[str, Any]] = {
    "shipments": {
        "path": "/shipments/",
        "index": ("created_at", "date", "carrier.name", "address_dest.country"),
        "bornes": ("created_date_from", "created_date_to"),
    },
    "shipment_requests": {
        "path": "/shipment-requests/",
        "index": ("created_at",),
        "bornes": (),
    },
    "invoice_lines": {
        "path": "/galaxy/invoice-lines",
        "index": ("status", "carrier_id"),
        "bornes": ("from_accounting_date", "to_accounting_date"),
    },
    "orders": {
        "path": "/orders",
        "index": (),
        "bornes": ("calculated_departure_date_from", "calculated_departure_date_to"),
    },
    "locations": {"path": "/locations", "index": (), "bornes": ()},
    "carriers": {"path": "/carriers/active", "index": (), "bornes": (), "paged": False},
}


def _iter_pages(path: str, query: dict[str, Any], page_limit: int, max_pages: int):
    """Pagine en rendant page par page, pour ecrire au fil de l'eau.

    La synchronisation ne doit pas garder deux cent mille lignes en memoire
    avant d'ecrire la premiere : chaque page part en base des qu'elle arrive.
    """
    offset = 0
    for _ in range(max(1, int(max_pages))):
        page_query = dict(query or {})
        page_query["limit"] = page_limit
        page_query["offset"] = offset
        batch = _rows(_request(path, page_query))
        if not batch:
            return
        yield batch
        offset += page_limit


def _sync_hint() -> str:
    return (
        "Lance d'abord shiptify_sync : il rapatrie une collection dans le cache "
        "local, une fois, et shiptify_sql l'interroge ensuite sans rappeler "
        "l'API."
    )


@mcp.tool()
@_guard
def shiptify_sync(
    collection: str = "shipments",
    date_from: str = "",
    date_to: str = "",
    query_json: str = "{}",
    max_rows: int = 200000,
    max_pages: int = 2000,
) -> str:
    """Rapatrie une collection Shiptify dans le cache local SQLite.

    C'est le chemin de l'HISTORIQUE : il n'est pas soumis au garde-fou
    SHIPTIFY_MAX_PAGES, qui plafonne le direct a 6 000 lignes. Une fois la
    collection en cache, shiptify_sql et shiptify_summary_sql repondent
    instantanement, autant de fois qu'on veut, sans reconsommer l'API.

    ECRIT SUR LE DISQUE (un fichier SQLite dans la racine locale, hors de tout
    dossier synchronise). A lancer sur demande, ou quand une question porte sur
    plus de quelques milliers de lignes.

    collection : shipments, shipment_requests, invoice_lines, orders,
                 locations, carriers.
    date_from / date_to : bornes appliquees COTE SERVEUR sur la colonne de date
                 propre a la collection. Les poser reduit fortement le trajet.
    query_json : filtres supplementaires, en JSON.

    Le cache est en INSERT OR REPLACE sur l'identifiant : relancer une synchro
    complete est toujours juste et ne cree jamais de doublon.
    """
    key = _norm(collection).replace(" ", "_")
    spec = SYNC_SETS.get(key)
    if spec is None:
        raise ShiptifyError(
            f"Collection inconnue : {collection}. Valeurs : "
            + ", ".join(sorted(SYNC_SETS))
        )
    try:
        query = json.loads(query_json or "{}")
    except ValueError as exc:
        raise ShiptifyError(f"query_json n'est pas du JSON valide : {exc}") from exc
    if not isinstance(query, dict):
        raise ShiptifyError("query_json doit etre un objet JSON.")

    bornes = spec.get("bornes") or ()
    if date_from or date_to:
        if not bornes:
            raise ShiptifyError(
                f"La collection '{key}' n'accepte pas de bornes de dates cote "
                "API. Passe les filtres dans query_json, ou synchronise tout."
            )
        if date_from:
            query[bornes[0]] = date_from
        if date_to:
            query[bornes[1]] = date_to

    table = cache.table_name(key)
    started = dt.datetime.now(dt.timezone.utc)
    path = spec["path"]
    conn = cache.open_rw(_cache_path())
    written = 0
    pages = 0
    stopped = ""
    try:
        if spec.get("paged", True):
            for batch in _iter_pages(
                path, query, _page_limit_for(path), int(max_pages)
            ):
                flat = [_flatten(r) for r in batch]
                written += cache.upsert(
                    conn,
                    table,
                    flat,
                    _cache_key,
                    NUMERIC_COLUMNS,
                    spec.get("index") or (),
                )
                pages += 1
                if written >= int(max_rows):
                    stopped = f"plafond max_rows={max_rows}"
                    break
            else:
                if pages >= int(max_pages):
                    stopped = f"plafond max_pages={max_pages}"
        else:
            flat = [_flatten(r) for r in _rows(_request(path, query))]
            written = cache.upsert(
                conn, table, flat, _cache_key, NUMERIC_COLUMNS, spec.get("index") or ()
            )
            pages = 1
        stamp = started.isoformat(timespec="seconds")
        cache.meta_set(conn, f"last_sync:{table}", stamp)
        cache.meta_set(
            conn,
            f"scope:{table}",
            json.dumps(_clean_query(query), ensure_ascii=False) or "(sans filtre)",
        )
        conn.commit()
        total = conn.execute(
            f"SELECT COUNT(*) FROM {cache.quote(table)}"
        ).fetchone()[0]
    finally:
        conn.close()

    out = [
        f"Synchronisation terminee : [{table}] {written} ligne(s) ecrite(s) "
        f"en {pages} appel(s).",
        f"Cache : {_cache_path()}",
        f"Total en cache pour cette table : {total} ligne(s).",
        "Perimetre demande : "
        + (json.dumps(_clean_query(query), ensure_ascii=False) or "(sans filtre)"),
    ]
    if stopped:
        out.append(
            f"ATTENTION : arret sur le {stopped}. La collection n'a PAS ete "
            "rapatriee en entier : resserre les bornes de dates et relance, ou "
            "releve le plafond. Le cache ne represente pas le perimetre demande."
        )
    else:
        out.append(
            "Collection parcourue jusqu'a la derniere page : le cache couvre "
            "bien le perimetre demande."
        )
    out.append("")
    out.append("Interroge maintenant avec shiptify_sql, ou shiptify_tables pour voir le cache.")
    return "\n".join(out)


def _cache_key(flat: dict[str, Any]) -> str:
    """Cle stable d'une ligne en cache. L'identifiant Shiptify quand il existe."""
    for candidate in ("id", "code", "internal_ref"):
        value = flat.get(candidate)
        if value not in (None, ""):
            return f"{candidate}={value}"
    return hashlib.sha256(
        json.dumps(flat, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


@mcp.tool()
@_guard
def shiptify_tables() -> str:
    """Ce que le cache local contient : tables, volumes, date de synchro, perimetre."""
    try:
        conn = cache.open_ro(_cache_path(), _sync_hint())
    except cache.CacheError as exc:
        raise ShiptifyError(str(exc)) from exc
    try:
        names = cache.tables(conn)
        if not names:
            return "Cache vide. " + _sync_hint()
        lines = [f"Cache local : {_cache_path()}", ""]
        for table in names:
            total = conn.execute(
                f"SELECT COUNT(*) FROM {cache.quote(table)}"
            ).fetchone()[0]
            cols = cache.columns(conn, table)
            lines.append(f"[{table}] {total} ligne(s), {len(cols)} colonne(s)")
            stamp = cache.meta_get(conn, f"last_sync:{table}")
            scope = cache.meta_get(conn, f"scope:{table}")
            lines.append(f"    derniere synchro : {stamp or '(inconnue)'}")
            lines.append(f"    perimetre        : {scope or '(inconnu)'}")
        lines.append("")
        lines.append(
            "Le cache n'est pas l'etat courant de Shiptify : ce qui a bouge "
            "depuis la synchro n'y est pas. Pour le jour meme, utilise les "
            "outils de liste, qui appellent l'API en direct."
        )
        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
@_guard
def shiptify_columns(table: str = "shipments") -> str:
    """Colonnes d'une table du cache, avec leur taux de remplissage.

    A lire avant d'ecrire du SQL. Le taux de remplissage n'est pas un detail :
    sommer une colonne remplie a 12 % rend un montant qui a l'air d'un total.
    """
    try:
        conn = cache.open_ro(_cache_path(), _sync_hint())
    except cache.CacheError as exc:
        raise ShiptifyError(str(exc)) from exc
    try:
        name = cache.table_name(table)
        cols = cache.columns(conn, name)
        if not cols:
            return f"Table '{name}' absente du cache. " + _sync_hint()
        total = conn.execute(f"SELECT COUNT(*) FROM {cache.quote(name)}").fetchone()[0]
        lines = [f"[{name}] {total} ligne(s), {len(cols)} colonne(s)", ""]
        if not total:
            lines.extend("  " + c for c in cols)
            return "\n".join(lines)
        expr = ", ".join(
            f"SUM(CASE WHEN {cache.quote(c)} IS NULL OR {cache.quote(c)} = '' "
            f"THEN 0 ELSE 1 END)"
            for c in cols
        )
        counts = conn.execute(f"SELECT {expr} FROM {cache.quote(name)}").fetchone()
        for col, filled in zip(cols, counts):
            kind = "num" if col in NUMERIC_COLUMNS else "txt"
            lines.append(f"  {col:<44} {kind}  {100 * (filled or 0) / total:5.1f}% rempli")
        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
@_guard
def shiptify_sql(sql: str, max_rows: int = 100) -> str:
    """Interroge le cache local en SQL (SQLite, lecture seule).

    Pour tout ce que l'API ne sait pas faire : un croisement, un historique
    long, un classement sur douze mois. Instantane, et sans consommer l'API.

    Les colonnes portent le nom aplati de l'API, avec des points : il faut donc
    les guillemeter. shiptify_columns les liste.

    Exemple :
      SELECT "carrier.name" AS transporteur, COUNT(*) nb, ROUND(SUM(cost),2) cout
      FROM shipments WHERE created_at >= '2026-01-01'
      GROUP BY 1 ORDER BY nb DESC
    """
    try:
        clean = cache.guard_sql(sql)
        conn = cache.open_ro(_cache_path(), _sync_hint())
    except cache.CacheError as exc:
        raise ShiptifyError(str(exc)) from exc
    try:
        rows, more = cache.select(conn, clean, limit=int(max_rows))
        if not rows:
            return "(aucune ligne)\n\nRequete : " + clean
        lines = [f"{len(rows)} ligne(s) rendues."]
        if more:
            reel = cache.count_of(conn, clean)
            lines.append(
                f"ATTENTION : la requete rend {reel} ligne(s) au total, "
                f"{max_rows} sont affichees. Le compte affiche n'est PAS le "
                "total - c'est le total ci-contre qui l'est. Releve max_rows, "
                "ou agrege dans la requete."
            )
        lines.append("")
        lines.append(cache.to_csv_text(rows))
        return "\n".join(lines)
    except cache.CacheError as exc:
        raise ShiptifyError(str(exc)) from exc
    finally:
        conn.close()


@mcp.tool()
@_guard
def shiptify_export_sql(sql: str, filename: str = "", max_rows: int = 500000) -> str:
    """Exporte en CSV le resultat d'une requete sur le cache. SUR DEMANDE.

    C'est l'export sans plafond de pagination : il lit le cache, pas l'API.
    A n'appeler que si l'utilisateur a demande un fichier.
    """
    try:
        clean = cache.guard_sql(sql)
        conn = cache.open_ro(_cache_path(), _sync_hint())
    except cache.CacheError as exc:
        raise ShiptifyError(str(exc)) from exc
    try:
        cur = conn.execute(clean)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(int(max_rows))
        reste = cur.fetchone() is not None
    except Exception as exc:
        raise ShiptifyError(f"SQL refuse par SQLite : {exc}") from exc
    finally:
        conn.close()

    target_dir = _export_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ShiptifyError(f"Dossier d'export inutilisable ({target_dir}) : {exc}") from exc
    name = (filename or "").strip() or (
        "shiptify_sql_" + dt.datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".csv"
    )
    if not name.lower().endswith(".csv"):
        name += ".csv"
    target = target_dir / pathlib.Path(name).name
    try:
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(cols)
            writer.writerows(rows)
    except OSError as exc:
        raise ShiptifyError(f"Ecriture impossible dans {target} : {exc}") from exc

    out = [f"Export termine : {target}", f"{len(rows)} ligne(s), {len(cols)} colonne(s)."]
    if reste:
        out.append(
            f"ATTENTION : la requete rendait PLUS de {max_rows} lignes, le "
            "fichier est tronque. Releve max_rows, ou resserre la requete."
        )
    else:
        out.append("Resultat complet : la requete ne rendait pas plus de lignes.")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

HELP = """\
MCP shiptify - interrogation en lecture seule de la base Shiptify.

  python server.py            mode serveur MCP (stdio), lance par Claude Code
  python server.py doctor     diagnostic de configuration et de connexion
  python server.py --help     cette aide

Configuration : un fichier .env hors du vault. Emplacement par defaut
%LOCALAPPDATA%\\shiptify-mcp\\.env, cree par install.ps1. Modele : .env.example.
Voir README.md.
"""


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if arg == "doctor":
        report = shiptify_doctor()
        print(report)
        return 0 if " : OK |" in report else 1
    if arg:
        print(f"Argument inconnu : {arg}\n")
        print(HELP)
        return 2
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
