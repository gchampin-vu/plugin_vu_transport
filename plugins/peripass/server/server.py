#!/usr/bin/env python3
"""
MCP peripass : interrogation en LECTURE SEULE du yard management Peripass.

Trois usages :
    python server.py            # mode serveur MCP (stdio) - lance par Claude Code
    python server.py doctor     # diagnostic de configuration et de connexion
    python server.py --help

Lecture seule par construction. Le contrat OpenAPI Peripass v2.0 expose 60
operations, dont 18 en GET : ce serveur n'expose que les GET. Aucun PUT, POST
ni DELETE n'est atteignable, y compris par l'outil generique peripass_get, qui
valide le chemin demande contre la liste blanche des chemins GET du contrat
(openapi_get_paths.json, a cote de ce fichier).

Consequence a connaitre : creer un visiteur, changer un statut (checkedin,
departed, blocked), deplacer un asset ou poser une tache ne se font pas ici.
C'est volontaire. Un changement de statut dans Peripass fait bouger un camion
sur une cour : c'est un geste humain, dans l'interface Peripass.

MULTI-SITE. Contrairement aux autres connecteurs de l'equipe, Peripass n'a pas
une base mais une par site : AUV (Moulins) et AMB (Amblainville) sont deux
tenants distincts, avec deux cles d'API distinctes. Chaque outil accepte donc
un parametre `site`, et par defaut interroge TOUS les sites configures en
ajoutant une colonne `site` au resultat - exactement ce que faisait le
Table.Combine du script Power Query d'origine.

Contrat de reference :
https://restapiprd.peripass.app/documentation/v2.0/PeripassRestApi.yaml
Releve le 2026-08-28.

CONFIGURATION, EN DEUX COUCHES. Ce qui est identique sur tous les postes -
sites, racines d'API, tenants, plafonds, et selon la decision d'equipe les cles
de service - vit dans le fichier partage 08_ENGINE/04_mcp/00_config/
peripass.shared.env. Ce qui est propre a un poste vit dans peripass.env, hors
du vault. Ordre de priorite : configuration du plugin > peripass.env du poste >
fichier d'equipe > defaut du serveur. Le poste passe devant l'equipe.
Voir README.md.
"""

from __future__ import annotations

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
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# Les deux sites de Vente-unique, et leur racine d'API historique. Ce sont les
# URL du script Power Query, celles qui sont eprouvees. La forme moderne
# (https://restapiprd.peripass.app/api/v2 + parametre tenant) est supportee :
# voir PERIPASS_<SITE>_BASE_URL et PERIPASS_<SITE>_TENANT dans le README.
DEFAULT_SITES: dict[str, str] = {
    "AUV": "https://vente-unique-logistics-auv.peripass.app/api/v2",
    "AMB": "https://vente-unique-logistics-amb.peripass.app/api/v2",
}
DEFAULT_SITE_ORDER = "AUV,AMB"

# Racine partagee, pour une configuration par tenant plutot que par hote.
SHARED_BASE_URL = "https://restapiprd.peripass.app/api/v2"

# Plafond impose par l'API : "the maximum number for pageSize is 100".
PAGE_SIZE_MAX = 100

DEFAULT_TIMEOUT_S = 60.0
# Garde-fou de pagination, PAR SITE : 60 pages de 100 = 6000 lignes.
DEFAULT_MAX_PAGES = 60

# Plafond de caracteres rendus dans la conversation par un outil de liste.
RENDER_MAX_CHARS = 24_000

# Dossiers synchronises : un secret n'y vit pas.
SYNCED_MARKERS = ("/onedrive", "cafom", "sharepoint", "dropbox", "google drive")

SPEC_FILE = pathlib.Path(__file__).resolve().parent / "openapi_get_paths.json"

# Deux chemins GET du contrat sont volontairement injoignables :
#   - validateeticket attend la cle d'API en PARAMETRE D'URL. Un secret dans
#     une query string finit dans les journaux des proxies. C'est de plus une
#     operation de borne d'accueil, pas une lecture d'analyse.
#   - le telechargement de piece jointe rend un binaire, pas du JSON.
BLOCKED_PATHS = {
    "/visitors/validateeticket": (
        "chemin volontairement non expose. Il attend la cle d'API en "
        "parametre d'URL, et un secret dans une query string part dans les "
        "journaux des proxies. C'est de plus une operation de borne "
        "d'accueil, pas une lecture d'analyse."
    ),
    "/visitors/{id}/attachments/{attachmentId}/download": (
        "chemin volontairement non expose. Il rend un fichier binaire, pas du "
        "JSON. Utilise peripass_visitor_attachments pour lister les pieces "
        "jointes, et telecharge depuis l'interface Peripass si besoin."
    ),
}

# Projection par defaut pour les visiteurs. Un visiteur porte une quarantaine
# de colonnes systeme, plus autant de champs personnalises selon le tenant.
# `fields.*` est un joker de prefixe : il prend tous les champs personnalises
# reellement presents, sans avoir a les nommer - ce que le script Power Query
# faisait a la main, en listant "Numero RDV", "Transporteur", "FRAQ"...
VISITOR_BRIEF = (
    "site,id,displayName,status,currentLocation,"
    "activeProfile,dispatchDashboard,yardLocation,"
    "visitorTimeStampSlotStart,visitorTimeStampSlotEnd,"
    "visitorTimeStampArrived,visitorTimeStampCheckedIn,"
    "visitorTimeStampCheckedOut,visitorTimeStampDeparted,"
    "waitingTimeMinutes,turnaroundTimeMinutes,processingTimeMinutes,"
    "currentHost.name,fields.*"
)

ASSET_BRIEF = (
    "site,id,displayName,status,category.name,location.name,"
    "assetTimeStampOnYard,assetTimeStampLeftYard,"
    "dropOffVisitor.displayName,pickUpVisitor.displayName,fields.*"
)

TASK_BRIEF = (
    "site,id,taskTemplateId,objectType,objectId,status,assignedTo,"
    "toLocationId,dispatchDashboardId,taskTimeStampStarted,"
    "taskTimeStampFinished,taskCreated,taskLastModified,fields.*"
)


class ConfigError(RuntimeError):
    pass


class PeripassError(RuntimeError):
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
            "server.py. Regenere-le depuis https://restapiprd.peripass.app"
            "/documentation/v2.0/PeripassRestApi.yaml."
        ) from exc


_SPEC: dict[str, Any] | None = None


def _spec() -> dict[str, Any]:
    global _SPEC
    if _SPEC is None:
        _SPEC = _load_spec()
    return _SPEC


def _enum(name: str) -> list[str]:
    return list(_spec().get("enums", {}).get(name) or [])


_PATTERNS: list[tuple[str, re.Pattern[str]]] | None = None


def _get_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Les chemins du contrat, du plus litteral au plus generique.

    L'ordre n'est pas cosmetique : /visitors/{reference} matche aussi
    /visitors/validateeticket. Sans tri, un chemin litteral serait attribue au
    mauvais gabarit, et on lirait les parametres du mauvais endpoint.
    """
    global _PATTERNS
    if _PATTERNS is not None:
        return _PATTERNS
    out: list[tuple[str, re.Pattern[str]]] = []
    for path in _spec()["paths"]:
        regex = "^" + re.sub(r"\\\{[^/}]+\\\}", "[^/]+", re.escape(path)) + "$"
        out.append((path, re.compile(regex)))
    out.sort(key=lambda item: (item[0].count("{"), -len(item[0])))
    _PATTERNS = out
    return out


def _template_for(path: str) -> str:
    """Le gabarit du contrat qui correspond a un chemin concret, ou ""."""
    for template, pattern in _get_patterns():
        if pattern.match(path):
            return template
    return ""


def _check_get_path(path: str) -> tuple[str, str]:
    """Normalise et valide un chemin. Rend (chemin concret, gabarit)."""
    path = path.strip()
    if "?" in path:
        raise PeripassError(
            "Passe les parametres de requete dans query_json, pas dans le chemin."
        )
    if not path.startswith("/"):
        path = "/" + path
    # Peripass n'aime pas le slash final : /visitors/ n'est pas /visitors.
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    template = _template_for(path)
    if not template:
        raise PeripassError(
            f"Chemin inconnu ou non accessible en lecture : {path}\n"
            f"Ce serveur est en lecture seule : seuls les "
            f"{len(_get_patterns())} chemins GET du contrat OpenAPI Peripass "
            "sont atteignables. Appelle peripass_list_paths pour voir la liste."
        )
    if template in BLOCKED_PATHS:
        raise PeripassError(f"{path} : {BLOCKED_PATHS[template]}")
    return path, template


def _supports_paging(template: str) -> bool:
    """L'endpoint attend-il pageNumber / pageSize ?

    Sur Peripass ils sont marques `required` quand ils existent, et absents
    ailleurs. La reponse est dans le contrat, on la lit plutot que de la
    deviner.
    """
    params = _spec()["paths"].get(template, {}).get("parameters", [])
    names = {p["name"] for p in params if p.get("in") == "query"}
    return {"pageNumber", "pageSize"} <= names


# --------------------------------------------------------------------------
# Configuration : le fichier peripass.env
# --------------------------------------------------------------------------

_ENV_LOADED_FROM: str = ""
_ENV_LOAD_ERROR: str = ""
_ENV_DONE: bool = False
# Cles qui viennent du fichier, pour que doctor sache dire d'ou sort une cle.
_ENV_FILE_KEYS: set[str] = set()

# Dernieres informations de quota rendues par l'API, par site.
_RATE_LIMIT: dict[str, str] = {}


def _is_synced(path: pathlib.Path) -> bool:
    flat = str(path).replace("\\", "/").lower()
    return any(marker in flat for marker in SYNCED_MARKERS)


def _packaged_python() -> str:
    """Detecte un interpreteur Windows empaquete. Rend la raison, ou "".

    Un Python livre par le Microsoft Store ou par le Python Manager tourne dans
    un conteneur d'application, et ses processus enfants heritent d'une vue
    VIRTUALISEE de %LOCALAPPDATA%. Mesure sur ce poste en empaquetant Yooz :
    PowerShell et l'interpreteur du venv lance directement voient
    ['venv', 'yooz.env'] ; le meme interpreteur lance par l'alias du Store ne
    voit que ['venv'].

    Consequence : un fichier de configuration pose sous %LOCALAPPDATA% peut
    etre invisible pour ce serveur, sans aucune erreur. D'ou la racine locale
    de ce connecteur : ~/.peripass-mcp, dans le profil utilisateur, qui n'est
    pas virtualise. On garde la detection pour pouvoir NOMMER le symptome.
    """
    flat = str(pathlib.Path(sys.base_prefix)).replace("\\", "/").lower()
    for marker in (
        "/windowsapps/",
        "/packages/pythonsoftwarefoundation",
        "/python/pythoncore-",
    ):
        if marker in flat:
            return f"interpreteur empaquete detecte ({sys.base_prefix})"
    return ""


def _local_root() -> pathlib.Path:
    """Racine locale du connecteur. PAS %LOCALAPPDATA% : voir _packaged_python."""
    return pathlib.Path.home() / ".peripass-mcp"


def _candidate_env_files() -> list[pathlib.Path]:
    """Emplacements du fichier de configuration, essayes dans l'ordre.

    Le fichier s'appelle `peripass.env`, pas `.env` : c'est la lecon de Yooz,
    ou un fichier nomme `.env` a ete ignore en silence par un connecteur qui
    cherchait `yooz.env`. On nomme le fichier d'apres le connecteur, et le meme
    nom vaut a tous les emplacements.
    """
    out: list[pathlib.Path] = []
    explicit = (os.environ.get("PERIPASS_ENV_FILE") or "").strip()
    if explicit:
        out.append(pathlib.Path(explicit))
    # Mode plugin d'abord : ce dossier vit sous ~/.claude/, hors de la zone que
    # les interpreteurs empaquetes virtualisent.
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        out.append(pathlib.Path(plugin_data) / "peripass.env")
    out.append(_local_root() / "peripass.env")
    # Voisin du script : refuse si le vault est synchronise, mais on le regarde
    # quand meme pour pouvoir le dire clairement plutot que rester muet.
    out.append(pathlib.Path(__file__).resolve().parent / "peripass.env")
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
    """Charge le premier fichier exploitable. Une variable NON VIDE du process gagne.

    Le "non vide" n'est pas un detail. En mode plugin, la configuration arrive
    par l'environnement : PERIPASS_AUV_API_KEY=${user_config.auv_api_key}. Si
    le champ n'est pas renseigne, la substitution pose une variable VIDE, et un
    setdefault la considererait comme renseignee - le fichier du poste serait
    alors ignore en silence.

    Le fichier est lu en utf-8-sig : un BOM UTF-8 non consomme colle trois
    octets invisibles devant le premier nom de variable, qui devient
    introuvable. C'est ce qui a fait perdre une cle chez Yooz.
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
                f"Un fichier de configuration a ete trouve dans un dossier "
                f"synchronise ({path}) et n'a PAS ete lu : il porterait des "
                "cles d'API sur le drive partage de l'equipe. Deplace-le vers "
                f"{_local_root() / 'peripass.env'} (c'est ce que fait "
                "install.ps1), ou pointe un emplacement local avec "
                "PERIPASS_ENV_FILE."
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
# Ce fichier porte tout ce qui est identique d'un poste a l'autre : la liste
# des sites, leur racine d'API, leur tenant, les plafonds de pagination - et,
# si l'equipe le decide, les cles d'API de service.
#
# Pourquoi il existe. Un reglage ressaisi vingt-six fois, ce sont vingt-six
# occasions de diverger et une correction qui ne se propage jamais. C'est ce
# qui a fait rater la mise en service de Yooz, ou l'applicationId etait
# confondu avec le client_id sur un poste et juste sur un autre.
#
# Les cles d'API. Elles sont ADMISES ici, comme chez shiptify depuis le
# 2026-08-28, parce qu'une cle Peripass est une cle de TENANT et pas de
# personne : elle n'identifie personne, elle vaut pour le site. Ce que ca
# implique et qu'il faut assumer : la bibliotheque est lisible par toute
# l'equipe L&T, donc la cle l'est aussi. Le jour ou elle doit cesser de
# l'etre - cle nominative, prestataire externe, audit - la reponse est la
# configuration du plugin sur le poste, qui passe DEVANT le fichier d'equipe
# (voir _env), pas le retrait de la ligne.
#
# Une valeur lue ici n'est JAMAIS versee dans os.environ : elle reste dans le
# cache ci-dessous, et c'est _env qui va l'y chercher en dernier recours. Une
# cle d'equipe n'apparait donc pas dans l'environnement du processus, ni dans
# ce qu'un sous-processus en heriterait.
#
# La liste blanche n'est pas une barriere anti-secret - elle laisse passer les
# cles - c'est un garde-fou contre la faute de frappe : une variable mal
# orthographiee posee dans le fichier partage serait sinon ignoree en silence,
# et on chercherait longtemps pourquoi le reglage d'equipe ne prend pas.

SHARED_FILE_NAME = "peripass.shared.env"
ENGINE_DIR_NAME = "08_ENGINE"
SHARED_SUBPATH = ("04_mcp", "00_config")

# Les reglages globaux que le fichier d'equipe a le droit de fixer.
SHARED_ALLOWED_GLOBALS = frozenset(
    {
        "PERIPASS_SITES",
        "PERIPASS_BASE_URL",
        "PERIPASS_PAGE_SIZE",
        "PERIPASS_MAX_PAGES",
        "PERIPASS_TIMEOUT_S",
    }
)

# Et, par site, ces quatre suffixes seulement. Le nom de site n'est pas connu
# d'avance : PERIPASS_SITES peut lui-meme venir du fichier d'equipe, et un
# troisieme yard s'ajoutera sans toucher a ce code.
SHARED_SITE_SUFFIXES = ("API_KEY", "BASE_URL", "TENANT", "LABEL")
_SHARED_SITE_RE = re.compile(
    r"^PERIPASS_([A-Z0-9_]+?)_(" + "|".join(SHARED_SITE_SUFFIXES) + r")$"
)

# Connues du serveur, mais qui n'ont RIEN a faire dans un fichier partage :
# un chemin valable sur un poste n'existe pas sur les vingt-cinq autres, et
# PERIPASS_API_KEY sans prefixe de site serait ambigu des qu'il y a deux yards.
SHARED_LOCAL_ONLY = frozenset(
    {
        "PERIPASS_API_KEY",
        "PERIPASS_EXPORT_DIR",
        "PERIPASS_ENV_FILE",
        "PERIPASS_SHARED_ENV",
        "PERIPASS_MCP_VENV",
        "VU_ENGINE_DIR",
    }
)

_SHARED_CACHE: dict[str, str] | None = None
_SHARED_LOADED_FROM: str = ""
# Cles ignorees a la lecture. Remontees par setup_status : c'est presque
# toujours une faute de frappe, et elle ne se voit nulle part ailleurs.
_SHARED_REJECTED: list[str] = []
_SHARED_LOCAL_SEEN: list[str] = []


def _is_secret_key(name: str) -> bool:
    """Une cle dont la VALEUR ne s'affiche jamais, meme dans un diagnostic.

    Le NOM, lui, se dit : c'est ce qui permet de savoir d'ou vient la valeur
    active sans la reveler.
    """
    return name.endswith("_API_KEY")


def _shared_allows(name: str) -> bool:
    if name in SHARED_LOCAL_ONLY:
        return False
    if name in SHARED_ALLOWED_GLOBALS:
        return True
    return bool(_SHARED_SITE_RE.match(name))


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
    # <profil>/CAFOM/<bibliotheque>/. Le nom exact de la bibliotheque varie
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
    """Emplacements du fichier d'equipe essayes, dans l'ordre."""
    out: list[pathlib.Path] = []
    explicit = (os.environ.get("PERIPASS_SHARED_ENV") or "").strip()
    if explicit:
        out.append(pathlib.Path(explicit))
    for root in _engine_roots():
        out.append(root.joinpath(*SHARED_SUBPATH, SHARED_FILE_NAME))
    return out


def _load_shared_env() -> dict[str, str]:
    """Lit le premier fichier d'equipe trouve, filtre par la liste blanche.

    Ne leve jamais : un fichier d'equipe absent, illisible ou mal rempli ne
    doit pas empecher le connecteur de tourner sur la configuration du poste.
    Ce qui a ete refuse est garde de cote pour que setup_status le dise.

    Lu en utf-8-sig : un BOM UTF-8 non consomme collerait trois octets
    invisibles devant le premier nom de variable, qui deviendrait introuvable -
    et, dans un fichier partage, introuvable sur les vingt-six postes a la fois.
    """
    global _SHARED_CACHE, _SHARED_LOADED_FROM, _SHARED_REJECTED, _SHARED_LOCAL_SEEN
    if _SHARED_CACHE is not None:
        return _SHARED_CACHE
    values: dict[str, str] = {}
    rejected: list[str] = []
    local_seen: list[str] = []
    for path in _shared_env_candidates():
        try:
            if not path.is_file():
                continue
            raw = _parse_env(path.read_text(encoding="utf-8-sig"))
        except OSError:
            continue
        for key, val in raw.items():
            upper = key.strip().upper()
            if _shared_allows(upper):
                if val.strip():
                    values[upper] = val.strip()
            elif upper in SHARED_LOCAL_ONLY:
                local_seen.append(upper)
            else:
                rejected.append(upper)
        _SHARED_LOADED_FROM = str(path)
        break
    _SHARED_REJECTED = rejected
    _SHARED_LOCAL_SEEN = local_seen
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
        secrets = sorted(k for k in shared if _is_secret_key(k))
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
    if _SHARED_LOCAL_SEEN:
        lines.append("")
        lines.append(
            "  ATTENTION : le fichier d'equipe porte des cles LOCALES par "
            "nature, elles ont ete ignorees : "
            + ", ".join(sorted(set(_SHARED_LOCAL_SEEN)))
            + "."
        )
        lines.append(
            "  Un chemin valable sur un poste n'existe pas sur les autres, et "
            "une cle d'API sans prefixe de site est ambigue des qu'il y a deux "
            "yards. Ces valeurs se posent dans /plugin, pas dans le collectif."
        )
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
            "le nom de la variable : compare avec peripass.env.example. Une cle "
            "ignoree ne produit aucune erreur ailleurs - c'est ici, et "
            "seulement ici, que ca se voit."
        )
    return lines


def _env(name: str, default: str = "") -> str:
    """La valeur retenue pour un reglage, dans un ordre de priorite fixe.

    1. l'environnement du processus - la configuration du plugin, et le
       peripass.env local que _load_env_file y a deja verse : ce que CE POSTE
       a decide ;
    2. le fichier d'equipe de 08_ENGINE - ce que l'EQUIPE a decide ;
    3. la valeur par defaut du serveur.

    Le poste passe donc devant l'equipe, et non l'inverse : un reglage
    d'equipe est un point de depart commun, pas une contrainte. C'est ce qui
    permet de pointer un tenant de recette, ou d'utiliser une cle nominative,
    sans toucher au fichier partage - donc sans casser les vingt-cinq autres
    postes.
    """
    _load_env_file()
    from_process = (os.environ.get(name) or "").strip()
    if from_process:
        return from_process
    from_team = _load_shared_env().get(name, "").strip()
    if from_team:
        return from_team
    return default.strip()


def _timeout() -> float:
    try:
        return float(_env("PERIPASS_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _max_pages() -> int:
    try:
        return max(1, int(_env("PERIPASS_MAX_PAGES") or DEFAULT_MAX_PAGES))
    except ValueError:
        return DEFAULT_MAX_PAGES


def _page_size() -> int:
    try:
        want = int(_env("PERIPASS_PAGE_SIZE") or PAGE_SIZE_MAX)
    except ValueError:
        return PAGE_SIZE_MAX
    return max(1, min(PAGE_SIZE_MAX, want))


def _export_dir() -> pathlib.Path:
    """Ou atterrissent les CSV. Quatre cas, dans cet ordre.

    Le serveur tourne dans deux contextes : installe dans le vault de
    Guillaume, ou installe comme plugin chez un collegue qui n'a pas de vault.
    Un chemin relatif au code serait juste dans le premier cas et absurde dans
    le second.
    """
    raw = _env("PERIPASS_EXPORT_DIR")
    if raw:
        return pathlib.Path(raw)
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        return pathlib.Path(plugin_data) / "exports"
    assets = pathlib.Path(__file__).resolve().parents[2] / "Assets"
    if assets.is_dir():
        return assets / "peripass"
    return pathlib.Path.cwd() / "peripass-exports"


# --------------------------------------------------------------------------
# Les sites : la specificite de ce connecteur
# --------------------------------------------------------------------------

def _site_code(raw: str) -> str:
    """Normalise un code de site en identifiant de variable d'environnement."""
    return re.sub(r"[^A-Z0-9]+", "_", raw.strip().upper()).strip("_")


class Site:
    __slots__ = ("code", "base_url", "api_key", "tenant", "problem")

    def __init__(
        self,
        code: str,
        base_url: str,
        api_key: str,
        tenant: str,
        problem: str = "",
    ) -> None:
        self.code = code
        self.base_url = base_url
        self.api_key = api_key
        self.tenant = tenant
        self.problem = problem

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.base_url and not self.problem)


def _declared_sites() -> list[str]:
    raw = _env("PERIPASS_SITES") or DEFAULT_SITE_ORDER
    codes = [_site_code(c) for c in raw.split(",")]
    return [c for c in codes if c]


def _describe_site(code: str) -> Site:
    """Resout la configuration d'un site. Ne leve pas : le site porte son probleme."""
    key = _env(f"PERIPASS_{code}_API_KEY")
    base = _env(f"PERIPASS_{code}_BASE_URL")
    tenant = _env(f"PERIPASS_{code}_TENANT")

    # Repli mono-site : un poste qui n'a qu'un seul yard n'a pas a prefixer.
    if not key and len(_declared_sites()) == 1:
        key = _env("PERIPASS_API_KEY")
    if not base:
        base = DEFAULT_SITES.get(code, "") or _env("PERIPASS_BASE_URL")
    if not base and tenant:
        base = SHARED_BASE_URL

    problem = ""
    if not base:
        problem = (
            f"aucune racine d'API connue pour le site {code}. Renseigne "
            f"PERIPASS_{code}_BASE_URL, ou PERIPASS_{code}_TENANT pour passer "
            f"par {SHARED_BASE_URL}."
        )
    elif not key:
        problem = f"cle d'API absente (PERIPASS_{code}_API_KEY)"
    return Site(code, base.rstrip("/"), key, tenant, problem)


def _all_sites() -> list[Site]:
    return [_describe_site(code) for code in _declared_sites()]


def _sites(selector: str = "") -> list[Site]:
    """Les sites vises par un appel. Vide / '*' / 'ALL' = tous ceux qui repondent.

    Un site declare mais non configure n'est pas une erreur silencieuse : s'il
    est explicitement demande, on refuse ; s'il fait partie du lot par defaut,
    l'appel continue sur les autres et le rendu le dit. Un chiffre calcule sur
    un seul site alors que l'utilisateur en attendait deux est exactement le
    genre de faux qu'on ne veut pas citer.
    """
    everything = _all_sites()
    want = (selector or "").strip()
    if want and want not in {"*", "ALL", "all", "TOUS", "tous"}:
        codes = [_site_code(c) for c in want.split(",") if c.strip()]
        known = {s.code: s for s in everything}
        out: list[Site] = []
        for code in codes:
            site = known.get(code)
            if site is None:
                # Site non declare dans PERIPASS_SITES : il peut avoir sa
                # configuration propre, on la resout avant de refuser.
                candidate = _describe_site(code)
                if candidate.ready:
                    out.append(candidate)
                    continue
                raise ConfigError(
                    f"Site inconnu : {code}. Sites declares : "
                    f"{', '.join(s.code for s in everything) or '(aucun)'}. "
                    "Appelle peripass_sites pour l'etat de la configuration."
                )
            if not site.ready:
                raise ConfigError(
                    f"Site {code} non configure : {site.problem}. "
                    "Appelle peripass_setup_status pour la marche a suivre."
                )
            out.append(site)
        return out

    ready = [s for s in everything if s.ready]
    if not ready:
        details = "\n".join(f"  - {s.code} : {s.problem}" for s in everything)
        packaged = _packaged_python()
        raise ConfigError(
            "Aucun site Peripass configure.\n\n"
            + (details or "  (aucun site declare dans PERIPASS_SITES)")
            + (f"\n\n{_ENV_LOAD_ERROR}" if _ENV_LOAD_ERROR else "")
            + (
                f"\n\nATTENTION : {packaged}. Un fichier pose sous "
                "%LOCALAPPDATA% peut etre invisible pour ce processus. Ce "
                f"connecteur range donc sa configuration dans {_local_root()}."
                if packaged
                else ""
            )
            + "\n\nEmplacements de configuration essayes, dans l'ordre :\n"
            + "\n".join(f"  - {p}" for p in _candidate_env_files())
            + "\n\nConfiguration d'equipe : "
            + (
                _SHARED_LOADED_FROM
                if _SHARED_LOADED_FROM
                else "AUCUNE - aucun de ces emplacements n'existe :\n"
                + "\n".join(f"  - {p}" for p in _shared_env_candidates())
                + "\n  La bibliotheque SharePoint « Transport BtoC » n'est pas "
                "atteignable depuis ce poste. Synchronise-la, ou pointe-la avec "
                "VU_ENGINE_DIR ou PERIPASS_SHARED_ENV : c'est peut-etre tout ce "
                "qui manque."
            )
            + "\n\nPour saisir les cles : appelle peripass_setup_status, qui "
            "donne la marche a suivre pour ce poste.\n\n"
            "En resume, en mode plugin : /plugin > peripass > configuration > "
            "« Cle d'API Peripass - AUV » et « ... - AMB ». La saisie se fait "
            "dans l'interface de Claude Code, jamais dans la conversation."
        )
    return ready


def _missing_sites() -> list[Site]:
    return [s for s in _all_sites() if not s.ready]


# --------------------------------------------------------------------------
# Le magasin de cles, sur la machine
# --------------------------------------------------------------------------

def _fingerprint(secret: str) -> str:
    """Empreinte courte d'une cle, pour comparer sans jamais l'afficher."""
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def _mask(secret: str) -> str:
    if not secret:
        return "(non renseigne)"
    if len(secret) <= 6:
        return "*" * len(secret)
    return f"{secret[:2]}{'*' * (len(secret) - 5)}{secret[-3:]}"


def _key_store_path() -> pathlib.Path:
    """Ou les cles sont rangees sur la machine, quand on les range.

    Le meme fichier que le serveur relit au demarrage : on reutilise la chaine
    de recherche plutot que d'inventer un second format. On ecrit dans le
    premier emplacement fiable, jamais dans un dossier synchronise.
    """
    explicit = (os.environ.get("PERIPASS_ENV_FILE") or "").strip()
    if explicit:
        return pathlib.Path(explicit)
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        return pathlib.Path(plugin_data) / "peripass.env"
    return _local_root() / "peripass.env"


def _stored_values() -> dict[str, str]:
    path = _key_store_path()
    try:
        if not path.is_file():
            return {}
        return _parse_env(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}


def _key_source(code: str) -> str:
    """D'ou sort la cle d'un site : la premiere question quand ca ne marche pas.

    Trois origines possibles, et il faut savoir laquelle : une cle d'equipe qui
    ne marche plus se corrige dans 08_ENGINE, pour tout le monde ; une cle de
    poste se corrige dans /plugin, pour soi seul.
    """
    _load_env_file()
    name = f"PERIPASS_{code}_API_KEY"
    if not os.environ.get(name):
        if _load_shared_env().get(name):
            return f"config d'equipe ({_SHARED_LOADED_FROM})"
        return "(aucune)"
    if name in _ENV_FILE_KEYS:
        return f"fichier {_ENV_LOADED_FROM}"
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "configuration du plugin (userConfig)"
    return "environnement du processus"


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


def _write_key_store(values: dict[str, str]) -> tuple[pathlib.Path, str]:
    """Ecrit des variables dans le magasin local. Rend (chemin, note sur les droits).

    Idempotent par construction : on retire toute ligne portant l'une des cles
    a ecrire, puis on en ecrit exactement une par cle. Le reste du fichier est
    preserve - il peut porter d'autres reglages.
    """
    path = _key_store_path()
    if _is_synced(path):
        raise ConfigError(
            f"Refus d'ecrire des cles dans un dossier synchronise ({path}) : "
            "elles partiraient sur le drive partage de l'equipe. Definis "
            "PERIPASS_ENV_FILE sur un chemin local."
        )
    lines: list[str] = []
    try:
        if path.is_file():
            keep = re.compile(
                r"\s*(" + "|".join(re.escape(k) for k in values) + r")\s*="
            )
            lines = [
                line
                for line in path.read_text(encoding="utf-8-sig").splitlines()
                if not keep.match(line)
            ]
    except OSError as exc:
        raise ConfigError(f"Lecture impossible de {path} : {exc}") from exc

    if not lines:
        lines = [
            "# Configuration du serveur MCP peripass.",
            "# Fichier local, hors du vault synchronise.",
            f"# Ecrit le {dt.date.today().isoformat()} par peripass_save_key.",
            "",
        ]
    for key in sorted(values):
        lines.append(f'{key}="{values[key]}"')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Sans BOM : un BOM UTF-8 non consomme colle trois octets invisibles
        # devant la premiere variable, qui devient introuvable.
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Ecriture impossible dans {path} : {exc}") from exc
    return path, _restrict_permissions(path)


# --------------------------------------------------------------------------
# Couche HTTP
# --------------------------------------------------------------------------

def _headers(site: Site) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-API-Key": site.api_key,
        "User-Agent": "myrddin-mcp-peripass/1.0",
    }


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


def _request(site: Site, path: str, query: dict[str, Any] | None = None) -> Any:
    if not site.ready:
        raise ConfigError(f"Site {site.code} non configure : {site.problem}")
    params = _clean_query(query)
    if site.tenant:
        params.setdefault("tenant", site.tenant)
    url = site.base_url + path
    try:
        resp = httpx.get(
            url,
            headers=_headers(site),
            params=params,
            timeout=_timeout(),
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise PeripassError(f"[{site.code}] Appel {path} impossible : {exc}") from exc

    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        _RATE_LIMIT[site.code] = (
            f"{remaining} appel(s) restant(s) sur "
            f"{resp.headers.get('X-RateLimit-Limit', '?')}"
        )

    if resp.status_code == 401:
        raise PeripassError(
            f"[{site.code}] HTTP 401 : en-tete d'authentification absente ou "
            f"cle refusee. Verifie PERIPASS_{site.code}_API_KEY. Une cle "
            "revoquee rend le meme code."
        )
    if resp.status_code == 403:
        # Mesure le 2026-08-28 contre les deux tenants : Peripass rend un 403
        # A CORPS VIDE aussi bien pour une cle ABSENTE que pour une cle
        # FAUSSE. Sa documentation annonce un 401 pour l'en-tete manquante :
        # ce n'est pas ce qui se passe. Le message ne doit donc pas envoyer
        # chercher un probleme de droits alors que la cle n'est simplement pas
        # arrivee jusqu'au serveur.
        raise PeripassError(
            f"[{site.code}] HTTP 403. Peripass rend ce code dans TROIS cas "
            "differents, et ne dit pas lequel (corps vide) :\n"
            f"  1. aucune cle envoyee - PERIPASS_{site.code}_API_KEY est vide "
            "ou n'est pas arrivee jusqu'au serveur ;\n"
            "  2. cle erronee ou revoquee ;\n"
            "  3. cle valide, mais sans droit sur cette operation.\n"
            "Commence par peripass_doctor : il dit si une cle est bien lue et "
            "d'ou elle vient. Si elle l'est, c'est le cas 2 ou 3, et ca se "
            "tranche avec le support Peripass en donnant le nom du tenant."
        )
    if resp.status_code == 404:
        raise PeripassError(
            f"[{site.code}] HTTP 404 sur {path} : ressource inexistante sur ce "
            "site. Un identifiant Peripass est propre a un tenant : un id "
            "valide sur AUV n'existe pas sur AMB."
        )
    if resp.status_code == 429:
        raise PeripassError(
            f"[{site.code}] HTTP 429 : quota d'appels atteint. Peripass compte "
            "les RESSOURCES lues, pas les requetes : ramener 6000 visiteurs "
            "coute 6000 unites de quota. Resserre le perimetre de dates, "
            "baisse max_rows, ou attends la fenetre suivante "
            f"({resp.headers.get('X-RateLimit-Reset', 'horodatage non fourni')})."
        )
    if resp.status_code >= 400:
        body = (resp.text or "")[:600]
        raise PeripassError(f"[{site.code}] HTTP {resp.status_code} sur {path} | {body}")

    if not resp.content:
        return []
    try:
        return resp.json()
    except ValueError as exc:
        raise PeripassError(
            f"[{site.code}] Reponse non JSON sur {path} : {(resp.text or '')[:300]}"
        ) from exc


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Normalise la reponse en liste d'enregistrements.

    Peripass rend une liste nue sur les collections. Les enveloppes
    items/content/data sont acceptees quand meme : c'est ce que faisait la
    fonction NormalizeToList du script Power Query, et ca coute deux lignes de
    ne pas casser si l'API en ajoute une un jour.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("items", "content", "data", "results", "rows"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
        return [payload]
    return []


def _paginate(
    site: Site,
    path: str,
    query: dict[str, Any] | None = None,
    max_rows: int = 1000,
) -> tuple[list[dict[str, Any]], bool]:
    """Boucle pageNumber / pageSize jusqu'a page vide. Rend (lignes, tronque).

    pageNumber est ZERO-BASED, et pageSize plafonne a 100 : c'est le contrat.
    L'API ne renvoie aucun total, la seule fin de collection fiable est donc
    une page vide - c'est la logique du script Power Query d'origine
    (List.Generate ... each [Cnt] > 0), conservee volontairement. S'arreter sur
    une page partielle ferait gagner un appel, mais sous-compterait en silence
    si l'API filtrait apres avoir applique la limite.
    """
    out: list[dict[str, Any]] = []
    page = 0
    size = _page_size()
    cap = _max_pages()
    while True:
        page_query = dict(query or {})
        page_query["pageNumber"] = page
        page_query["pageSize"] = size
        batch = _rows(_request(site, path, page_query))
        out.extend(batch)
        page += 1
        if not batch:
            return out, False
        if len(out) >= max_rows:
            return out[:max_rows], True
        if page >= cap:
            return out, True


def _collect(
    sites: list[Site],
    path: str,
    query: dict[str, Any] | None = None,
    max_rows: int = 300,
    paged: bool = True,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Interroge chaque site et empile les lignes. Rend (lignes, tronques, notes).

    Chaque ligne porte une colonne `site` en tete. C'est la colonne que le
    script Power Query ajoutait avant Table.Combine : sans elle, deux
    enregistrements d'id 42 venus de deux tenants sont indiscernables.

    max_rows s'applique PAR SITE, pas au total. Deux sites a 300 lignes rendent
    donc jusqu'a 600 lignes, et le rendu le dit.
    """
    rows: list[dict[str, Any]] = []
    truncated: list[str] = []
    notes: list[str] = []
    for site in sites:
        try:
            if paged:
                batch, cut = _paginate(site, path, query, max_rows=max_rows)
            else:
                batch = _rows(_request(site, path, query))
                cut = len(batch) > max_rows
                batch = batch[:max_rows]
        except (ConfigError, PeripassError) as exc:
            notes.append(f"[{site.code}] ECHEC : {exc}")
            continue
        if cut:
            truncated.append(site.code)
        for row in batch:
            rows.append({"site": site.code, **row})
        notes.append(f"[{site.code}] {len(batch)} ligne(s)")
    return rows, truncated, notes


# --------------------------------------------------------------------------
# Aplatissement et rendu
# --------------------------------------------------------------------------

def _flatten(row: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Aplatit les objets imbriques en notation pointee.

    Reproduit ce que faisait Table.ExpandRecordColumn dans le script Power
    Query : fields."Numero RDV", currentHost.name, category.name. Les listes
    sont rendues en JSON compact : elles n'ont pas de forme de colonne stable,
    et les eclater multiplierait les lignes.
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
    """Projette des colonnes. Accepte le joker de prefixe `prefixe.*`.

    Le joker n'est pas un confort : les champs personnalises d'un visiteur
    (`fields.Transporteur`, `fields.Numero RDV`) sont propres au tenant et
    changent avec la configuration Peripass. Les nommer en dur dans une
    projection par defaut, c'est ce que faisait le script Power Query - et
    c'est ce qui le cassait des qu'un champ etait renomme.
    """
    wanted = [f.strip() for f in fields.split(",") if f.strip()]
    if not wanted or wanted == ["*"]:
        return rows
    available = _columns(rows)
    resolved: list[str] = []
    for token in wanted:
        if token == "*":
            resolved.extend(available)
        elif token.endswith(".*"):
            prefix = token[:-1]  # on garde le point
            resolved.extend(c for c in available if c.startswith(prefix))
        else:
            resolved.append(token)
    ordered = list(dict.fromkeys(resolved))
    return [{c: r.get(c, "") for c in ordered} for r in rows]


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


def _render_table(
    rows: list[dict[str, Any]],
    header: str,
    truncated: list[str],
    notes: list[str],
    fields: str = "",
    max_chars: int = RENDER_MAX_CHARS,
) -> str:
    """Rend une collection en CSV point-virgule, borne en taille."""
    flat = _select([_flatten(r) for r in rows], fields)
    lines = [header]
    if notes:
        lines.append("Par site : " + " | ".join(notes))
    lines.append(f"{len(rows)} ligne(s) rendues, tous sites confondus.")
    if truncated:
        lines.append(
            "ATTENTION : resultat tronque sur "
            + ", ".join(truncated)
            + " (max_rows par site, ou garde-fou PERIPASS_MAX_PAGES, "
            "atteint). Le compte ci-dessus n'est PAS un total : ne le cite "
            "pas comme un volume. Resserre les filtres jusqu'a ce que le "
            "perimetre tienne. Un export CSV ne se fait que si l'utilisateur "
            "en a demande un."
        )
    if any("ECHEC" in n for n in notes):
        lines.append(
            "ATTENTION : au moins un site n'a pas repondu. Le resultat est "
            "PARTIEL - il ne couvre pas le perimetre demande."
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
            f"Rendu limite a {max(0, len(kept) - 1)} ligne(s) pour ne pas "
            "saturer la conversation. Restreins avec fields=... ou resserre "
            "les filtres. N'exporte en CSV que si l'utilisateur l'a demande."
        )
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def _render_json(payload: Any, header: str, max_chars: int = RENDER_MAX_CHARS) -> str:
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n... (tronque)"
    return f"{header}\n\n{body}"


def _check_enum(value: str, enum_name: str, label: str) -> str:
    """Valide une valeur contre un enum du contrat. Rend la valeur telle quelle.

    Peripass ignore en silence un filtre mal forme ("If format is incorrect we
    will ignore the filter") : une faute de frappe ne rend pas une erreur, elle
    rend TOUTES les lignes. On refuse donc avant d'emettre.
    """
    if not value:
        return ""
    allowed = _enum(enum_name)
    if not allowed:
        return value
    for candidate in allowed:
        if candidate.lower() == value.strip().lower():
            return candidate
    raise PeripassError(
        f"{label} : valeur inconnue « {value} ». Valeurs permises : "
        + ", ".join(allowed)
        + ". Attention : Peripass ignore en SILENCE un filtre mal forme, il "
        "rendrait donc toutes les lignes sans le dire."
    )


def _guard(fn):
    """Rend les erreurs de configuration et d'API comme message lisible.

    functools.wraps n'est pas cosmetique ici : il pose __wrapped__, que
    inspect.signature suit. Sans lui, FastMCP lit la signature du wrapper et
    publie chaque outil avec deux parametres (args, kwargs) au lieu des vrais -
    les outils sortent alors du handshake, mais sont INAPPELABLES. Le bug est
    passe une fois chez shiptify, il ne repasse pas ici.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ConfigError, PeripassError) as exc:
            return f"ERREUR : {exc}"

    return wrapper


# --------------------------------------------------------------------------
# Outils MCP
# --------------------------------------------------------------------------

mcp = FastMCP("peripass")

# --- Mise en service : les cles d'API -----------------------------------
#
# La saisie se fait dans l'interface de Claude Code, par les champs userConfig
# declares dans plugin.json (marques `sensitive`). Claude Code les collecte
# lui-meme et les passe au serveur par l'environnement : elles ne traversent
# jamais la conversation, donc elles n'entrent ni dans le contexte du modele,
# ni dans la transcription.
#
# C'est pour cette raison qu'aucun outil ici n'accepte une cle en parametre.
# Un peripass_set_api_key("...") serait plus direct a expliquer, mais il ferait
# passer le secret par le fil de la conversation - exactement ce que le champ
# `sensitive` existe pour eviter. peripass_save_key ne prend donc aucune cle en
# argument : il range celles qui sont deja saisies.


@mcp.tool()
@_guard
def peripass_setup_status() -> str:
    """Ou en est la mise en service : quelles cles sont saisies, sont-elles rangees ?

    A appeler en premier apres l'installation du plugin, et chaque fois qu'un
    outil repond qu'aucun site n'est configure. Ne rend jamais une cle en clair.
    """
    _load_env_file()
    sites = _all_sites()
    stored = _stored_values()
    store = _key_store_path()
    plugin_mode = bool(os.environ.get("CLAUDE_PLUGIN_ROOT"))

    lines = ["Mise en service du connecteur Peripass", ""]
    lines.append(f"Sites declares       : {', '.join(s.code for s in sites) or '(aucun)'}")
    lines.append(f"Magasin sur machine  : {store}")
    lines.extend(_shared_report())
    lines.append("")
    for site in sites:
        name = f"PERIPASS_{site.code}_API_KEY"
        on_disk = stored.get(name, "")
        lines.append(f"Site {site.code}")
        lines.append(f"  Racine d'API   : {site.base_url or '(inconnue)'}")
        if site.tenant:
            lines.append(f"  Tenant         : {site.tenant}")
        lines.append(f"  Cle active     : {_mask(site.api_key) if site.api_key else 'AUCUNE'}")
        lines.append(f"  Origine        : {_key_source(site.code)}")
        lines.append(f"  Cle rangee     : {'oui, ' + _mask(on_disk) if on_disk else 'non'}")
        if site.problem:
            lines.append(f"  A FAIRE        : {site.problem}")
        if site.api_key and on_disk and _fingerprint(site.api_key) != _fingerprint(on_disk):
            lines.append(
                "  NOTE           : la cle rangee sur la machine n'est PAS "
                "celle utilisee. La configuration du plugin est prioritaire "
                "sur le fichier. peripass_save_key aligne le fichier."
            )
        lines.append("")

    missing = [s for s in sites if not s.ready]
    if missing:
        covered = [
            s
            for s in sites
            if s.ready
            and _load_shared_env().get(f"PERIPASS_{s.code}_API_KEY")
            and not os.environ.get(f"PERIPASS_{s.code}_API_KEY")
        ]
        if covered:
            lines.append(
                "RIEN a saisir pour "
                + ", ".join(s.code for s in covered)
                + " : la cle vient de la configuration d'equipe. Ne la "
                "ressaisis pas, il n'y a que les sites ci-dessous a traiter."
            )
            lines.append("")
        if not _SHARED_LOADED_FROM:
            lines.append(
                "Avant de saisir quoi que ce soit : la ligne « Config d'equipe » "
                "ci-dessus dit « aucune ». La bibliotheque SharePoint "
                "« Transport BtoC » n'est donc pas atteignable depuis ce poste, "
                "ou elle est posee ailleurs - et tout ce que l'equipe a deja "
                "regle (sites, racines d'API, tenants, et selon la decision "
                "d'equipe les cles) est perdu. Synchronise-la, ou pointe-la "
                "avec VU_ENGINE_DIR ou PERIPASS_SHARED_ENV. C'est la correction "
                "dans une bonne partie des cas."
            )
            lines.append("")
        lines.append(
            "A FAIRE - saisir les cles manquantes, dans l'interface de Claude "
            "Code :"
        )
        lines.append("")
        if plugin_mode:
            lines.append("  1. tape /plugin")
            lines.append("  2. choisis le plugin « peripass » (marketplace vu-transport)")
            lines.append("  3. ouvre sa configuration et renseigne :")
            for site in missing:
                lines.append(f"       - « Cle d'API Peripass - {site.code} »")
            lines.append("  4. redemarre la session pour que le serveur reprenne les cles")
        else:
            lines.append(
                "  Ce serveur ne tourne PAS comme plugin : il n'y a donc pas de "
                "champ de configuration. Deux options :"
            )
            lines.append("  - installer le plugin (voir le README du marketplace), ou")
            lines.append(f"  - poser les cles dans {store} via install.ps1.")
        lines.append("")
        lines.append(
            "Les cles ne sont pas a coller dans la conversation : le champ de "
            "configuration les garde hors du contexte du modele. Elles "
            "s'obtiennent aupres de Peripass, une par tenant - le plugin n'en "
            "fournit aucune."
        )
        packaged = _packaged_python()
        if packaged:
            lines.append("")
            lines.append(f"  A savoir : {packaged}.")
            lines.append(
                "  Ce connecteur range sa configuration dans le profil "
                "utilisateur (~/.peripass-mcp) et non sous %LOCALAPPDATA%, "
                "justement pour ne pas dependre de la virtualisation."
            )
        return "\n".join(lines)

    shared = _load_shared_env()
    from_team = [
        s
        for s in sites
        if shared.get(f"PERIPASS_{s.code}_API_KEY")
        and not os.environ.get(f"PERIPASS_{s.code}_API_KEY")
    ]
    unsaved = [
        s
        for s in sites
        if s.api_key
        and s not in from_team
        and not stored.get(f"PERIPASS_{s.code}_API_KEY")
    ]
    if from_team:
        lines.append(
            "Cle(s) fournie(s) par l'equipe pour "
            + ", ".join(s.code for s in from_team)
            + ", lues dans 08_ENGINE. Il n'y a RIEN a saisir sur ce poste pour "
            "ces sites : verifie la connexion avec peripass_doctor."
        )
        lines.append("")
        lines.append(
            "  Deux cas ou tu saisirais quand meme quelque chose dans /plugin : "
            "un acces nominatif, ou un tenant de recette. Ce que le poste "
            "definit passe devant l'equipe, sans toucher au fichier partage."
        )
        lines.append(
            "  peripass_save_key n'est utile que pour rendre ce poste autonome "
            "de la bibliotheque synchronisee : il recopie en local les valeurs "
            "actives."
        )
        lines.append("")
    if unsaved:
        lines.append(
            "Les cles de "
            + ", ".join(s.code for s in unsaved)
            + " sont saisies mais ne sont PAS rangees sur la machine. Appelle "
            "peripass_save_key : elles survivront alors a une reinstallation "
            "du plugin, et l'installation directe les trouvera aussi."
        )
    elif not from_team:
        lines.append("Tout est en place. Verifie la connexion avec peripass_doctor.")
    return "\n".join(lines)


@mcp.tool()
@_guard
def peripass_save_key() -> str:
    """Range sur la machine les cles deja saisies dans l'interface de Claude Code.

    Ne prend aucun argument, **volontairement** : les cles viennent de la
    configuration du plugin, pas de la conversation. Elles n'ont donc pas a
    etre recopiees dans un message pour etre rangees.

    Le fichier est ecrit hors de tout dossier synchronise, et ses droits sont
    restreints a l'utilisateur courant.
    """
    sites = [s for s in _all_sites() if s.api_key]
    if not sites:
        raise ConfigError(
            "Aucune cle a ranger : aucun site n'a de cle saisie. Appelle "
            "peripass_setup_status pour la marche a suivre."
        )
    # On ne recopie en local que ce que l'equipe NE fournit PAS deja. Figer
    # ici une racine d'API ou un tenant venu du fichier partage reviendrait a
    # se rendre sourd a sa prochaine correction : le poste passe devant
    # l'equipe, donc la valeur locale gagnerait pour toujours. La cle, elle,
    # est recopiee meme si elle vient de l'equipe - c'est justement ce qui rend
    # le poste autonome de la bibliotheque synchronisee.
    shared = _load_shared_env()
    values = {f"PERIPASS_{s.code}_API_KEY": s.api_key for s in sites}
    for site in sites:
        tenant_key = f"PERIPASS_{site.code}_TENANT"
        base_key = f"PERIPASS_{site.code}_BASE_URL"
        if site.tenant and not shared.get(tenant_key):
            values[tenant_key] = site.tenant
        if (
            site.base_url
            and site.base_url != DEFAULT_SITES.get(site.code)
            and not shared.get(base_key)
        ):
            values[base_key] = site.base_url
    path, permissions = _write_key_store(values)
    lines = [f"Cles rangees sur la machine : {path}", ""]
    for site in sites:
        lines.append(
            f"  {site.code} : empreinte {_fingerprint(site.api_key)} "
            "(les 12 premiers caracteres du SHA-256, pour comparer sans "
            "afficher la cle)"
        )
    lines.append("")
    lines.append(f"Droits : {permissions}")
    lines.append("")
    lines.append(
        "Ce fichier n'est pas synchronise et n'est pas versionne. Il sera relu "
        "automatiquement au prochain demarrage du serveur, meme si la "
        "configuration du plugin est perdue."
    )
    lines.append("")
    lines.append("Pour l'effacer : peripass_forget_key.")
    return "\n".join(lines)


@mcp.tool()
@_guard
def peripass_forget_key(site: str = "") -> str:
    """Supprime du magasin local la cle d'un site, ou de tous si `site` est vide.

    Ne touche pas a la configuration du plugin : si une cle y est saisie, elle
    continuera d'etre utilisee. Pour la retirer completement, vide aussi le
    champ correspondant dans /plugin.
    """
    path = _key_store_path()
    codes = (
        [_site_code(c) for c in site.split(",") if c.strip()]
        if site.strip()
        else [s.code for s in _all_sites()]
    )
    if not codes:
        raise ConfigError(
            "Aucun site a oublier : PERIPASS_SITES est vide et aucun site "
            "n'a ete passe en argument."
        )
    try:
        if not path.is_file():
            return f"Aucune cle rangee a supprimer ({path} n'existe pas)."
        drop = re.compile(
            r"\s*(" + "|".join(f"PERIPASS_{c}_API_KEY" for c in codes) + r")\s*="
        )
        original = path.read_text(encoding="utf-8-sig").splitlines()
        lines = [line for line in original if not drop.match(line)]
        removed = len(original) - len(lines)
        if any(l.strip() and not l.strip().startswith("#") for l in lines):
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            note = (
                f"{removed} ligne(s) de cle retiree(s) de {path} "
                "(le reste du fichier est conserve)."
            )
        else:
            path.unlink()
            note = f"Fichier supprime : {path}"
    except OSError as exc:
        raise ConfigError(f"Suppression impossible dans {path} : {exc}") from exc

    still = [s.code for s in _all_sites() if s.api_key]
    return "\n".join(
        [
            note,
            "",
            "Sites qui gardent une cle active pour cette session : "
            + (", ".join(still) if still else "aucun")
            + ". Elle vient alors de la configuration du plugin, qui n'est pas "
            "touchee ici - vide le champ dans /plugin pour la retirer aussi.",
        ]
    )


@mcp.tool()
def peripass_doctor() -> str:
    """Diagnostic : configuration lue, cles presentes, et vrai appel par site.

    Le premier outil a appeler quand quelque chose coince. Ne rend jamais une
    cle en clair.
    """
    _load_env_file()
    lines = ["Configuration du serveur MCP peripass", ""]
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    lines.append(
        "Mode                     : "
        + (f"plugin ({plugin_root})" if plugin_root else "installation directe")
    )
    lines.append(f"Fichier de config lu     : {_ENV_LOADED_FROM or '(aucun)'}")
    if _ENV_LOAD_ERROR:
        lines.append(f"Avertissement            : {_ENV_LOAD_ERROR}")
    lines.append("Emplacements essayes     :")
    for path in _candidate_env_files():
        mark = "OK" if str(path) == _ENV_LOADED_FROM else "  "
        try:
            exists = "present" if path.is_file() else "absent"
        except OSError as exc:
            exists = f"illisible : {exc}"
        lines.append(f"  [{mark}] {path} ({exists})")
    packaged = _packaged_python()
    if packaged:
        lines.append("")
        lines.append(f"  A savoir : {packaged}.")
        lines.append(
            "  Les processus enfants d'un interpreteur empaquete voient une "
            "vue virtualisee de %LOCALAPPDATA%. Ce connecteur range donc sa "
            f"configuration dans {_local_root()}, qui n'est pas virtualise. "
            "Si un dossier existe mais parait vide ici, c'est ce symptome."
        )
    lines.append("")
    lines.extend(_shared_report())
    lines.append("")
    lines.append(
        "Ordre de priorite        : configuration du plugin > peripass.env du "
        "poste > config d'equipe > defaut du serveur"
    )
    lines.append("")
    lines.append(f"PERIPASS_SITES           : {', '.join(_declared_sites())}")
    lines.append(f"PERIPASS_PAGE_SIZE       : {_page_size()} (plafond API : {PAGE_SIZE_MAX})")
    lines.append(f"PERIPASS_MAX_PAGES       : {_max_pages()} (par site)")
    lines.append(f"PERIPASS_TIMEOUT_S       : {_timeout()}")
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
    lines.append("Sites")
    for site in _all_sites():
        lines.append(f"  {site.code}")
        lines.append(f"    Racine       : {site.base_url or '(inconnue)'}")
        if site.tenant:
            lines.append(f"    Tenant       : {site.tenant}")
        lines.append(f"    Cle          : {_mask(site.api_key)}")
        lines.append(f"    Origine      : {_key_source(site.code)}")
        if site.problem:
            lines.append(f"    PROBLEME     : {site.problem}")

    lines.append("")
    lines.append("Appel de verification")
    ready = [s for s in _all_sites() if s.ready]
    if not ready:
        lines.append("  Aucun site configure : appel non tente.")
        return "\n".join(lines)
    for site in ready:
        for path, label, query in (
            ("/profiles", "profils", {"pageNumber": 0, "pageSize": 1}),
            ("/visitors", "visiteurs", {"pageNumber": 0, "pageSize": 1}),
        ):
            try:
                payload = _request(site, path, query)
                preview = json.dumps(payload, ensure_ascii=False, default=str)[:240]
                lines.append(f"  [{site.code}] GET {path} ({label}) : OK | {preview}")
            except (ConfigError, PeripassError) as exc:
                lines.append(f"  [{site.code}] GET {path} ({label}) : ECHEC | {exc}")
        if site.code in _RATE_LIMIT:
            lines.append(f"  [{site.code}] quota : {_RATE_LIMIT[site.code]}")
    return "\n".join(lines)


@mcp.tool()
@_guard
def peripass_sites() -> str:
    """Les sites Peripass configures, leur racine d'API et leur etat.

    A appeler avant toute question qui porte sur un seul site : le parametre
    `site` des autres outils attend l'un de ces codes.
    """
    sites = _all_sites()
    lines = [
        "Sites Peripass declares (PERIPASS_SITES)",
        "",
        "Un site = un tenant Peripass = une base et une cle distinctes. Les "
        "identifiants ne sont PAS partages entre sites : un visiteur id 42 sur "
        "AUV n'a aucun rapport avec un visiteur id 42 sur AMB.",
        "",
    ]
    for site in sites:
        state = "pret" if site.ready else f"NON CONFIGURE - {site.problem}"
        lines.append(f"{site.code} : {state}")
        lines.append(f"    racine : {site.base_url or '(inconnue)'}")
        if site.tenant:
            lines.append(f"    tenant : {site.tenant}")
        if site.code in _RATE_LIMIT:
            lines.append(f"    quota  : {_RATE_LIMIT[site.code]}")
    lines.append("")
    lines.append(
        "Par defaut, les outils interrogent TOUS les sites prets et ajoutent "
        "une colonne `site`. Passe site=\"AUV\" pour n'en viser qu'un, ou "
        "site=\"AUV,AMB\" pour une liste explicite."
    )
    return "\n".join(lines)


@mcp.tool()
@_guard
def peripass_list_paths(contains: str = "") -> str:
    """Liste les chemins GET atteignables, leurs parametres et leurs valeurs permises.

    A appeler avant peripass_get, pour ne pas deviner un chemin ou un nom de
    filtre. `contains` filtre sur le chemin ou le resume.
    """
    spec = _spec()
    needle = contains.strip().lower()
    lines = [
        f"Chemins GET de l'API Peripass ({len(spec['paths'])} au total, "
        f"contrat {spec.get('api_version')}, releve {spec.get('released')})",
        f"Source : {spec.get('source')}",
        "",
        "Lecture seule : les 42 autres operations du contrat ecrivent dans le "
        "yard et ne sont pas exposees.",
        "",
    ]
    shown = 0
    for path in sorted(spec["paths"]):
        info = spec["paths"][path]
        summary = info.get("summary", "")
        if needle and needle not in path.lower() and needle not in summary.lower():
            continue
        shown += 1
        blocked = BLOCKED_PATHS.get(path)
        lines.append(f"{path}  -  {summary}")
        if blocked:
            lines.append(f"    NON EXPOSE : {blocked}")
            continue
        for param in info.get("parameters", []):
            bits = [f"{param['in']} {param['name']}", str(param.get("type") or "?")]
            if param.get("required"):
                bits.append("requis")
            if param.get("enum"):
                bits.append("valeurs " + ", ".join(str(v) for v in param["enum"]))
            desc = param.get("description") or ""
            line = "    " + " | ".join(bits)
            if desc:
                line += f" | {desc[:160]}"
            lines.append(line)
    if not shown:
        lines.append(f"(aucun chemin ne correspond a '{contains}')")
    lines.append("")
    lines.append("Valeurs permises des enums du contrat :")
    for name, values in sorted(spec.get("enums", {}).items()):
        lines.append(f"  {name} : {', '.join(values)}")
    return "\n".join(lines)


# --- Visiteurs : le coeur du connecteur ---------------------------------


@mcp.tool()
@_guard
def peripass_list_visitors(
    site: str = "",
    timestamp_field: str = "",
    timestamp_start: str = "",
    timestamp_end: str = "",
    status: str = "",
    updated_since: str = "",
    validity_from: str = "",
    validity_until: str = "",
    profile_site_id: int = 0,
    search_object: str = "",
    search_field: str = "",
    search_text: str = "",
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Les visiteurs (creneaux, arrivees, check-in, departs). L'outil principal.

    C'est le portage direct du script Power Query : meme endpoint, meme
    pagination, meme colonne `site`, et les champs personnalises sortent
    aplatis en `fields.<nom>`.

    Les filtres, dans l'ordre d'utilite :

    - timestamp_field + timestamp_start / timestamp_end : LE filtre de periode.
      timestamp_field vaut SlotStart (creneau planifie), Arrived, CheckedIn ou
      Departed. Dates en ISO 8601 avec fuseau : 2026-08-01T00:00:00Z ou
      2026-08-01T00:00:00+02:00.
    - status : Logged, WaitingForApproval, WaitingSequence, WaitingForDispatch,
      CheckedIn, CheckedOut, Blocked, WaitingForDocuments, ReadyForCheckout.
    - updated_since : tout ce qui a bouge depuis un instant. Pour un
      rafraichissement incremental, pas pour une analyse de periode.
    - validity_from / validity_until : sur la validite du profil (YYYY-MM-DD),
      pas sur le creneau. A ne pas confondre avec timestamp_*.
    - search_object + search_field + search_text : recherche EXACTE et SENSIBLE
      A LA CASSE sur un champ. search_object vaut SystemField (Pincode, Status,
      CurrentLocation), CustomField (nom technique d'un champ personnalise, cf.
      peripass_fields) ou CurrentHost (Name, Email, Mobile).
    - profile_site_id : le siteId Peripass, c'est-a-dire un site A L'INTERIEUR
      d'un tenant. Rien a voir avec le parametre `site` de cet outil, qui
      choisit le tenant (AUV / AMB).

    Piege a connaitre : Peripass IGNORE EN SILENCE un filtre mal forme ("If
    format is incorrect we will ignore the filter"). Une date mal ecrite ne
    rend pas une erreur, elle rend toutes les lignes. Les valeurs d'enum sont
    donc validees ici, avant l'appel.
    """
    query = {
        "timestampFilterField": _check_enum(
            timestamp_field, "TimestampFilterField", "timestamp_field"
        ),
        "timestampFilterStart": timestamp_start,
        "timestampFilterEnd": timestamp_end,
        "status": _check_enum(status, "VisitorStatus", "status"),
        "updatedSince": updated_since,
        "validityFrom": validity_from,
        "validityUntil": validity_until,
        "siteId": profile_site_id or "",
        "searchObject": _check_enum(
            search_object, "VisitorSearchObject", "search_object"
        ),
        "searchField": search_field,
        "searchText": search_text,
    }
    if (timestamp_start or timestamp_end) and not query["timestampFilterField"]:
        raise PeripassError(
            "timestamp_start / timestamp_end sans timestamp_field : Peripass "
            "ignorerait le filtre en silence et rendrait toute la base. "
            "Precise timestamp_field parmi "
            + ", ".join(_enum("TimestampFilterField"))
            + "."
        )
    if search_text and not query["searchObject"]:
        raise PeripassError(
            "search_text sans search_object : le filtre serait ignore. "
            "Precise search_object parmi "
            + ", ".join(_enum("VisitorSearchObject"))
            + ", et search_field."
        )
    sites = _sites(site)
    rows, truncated, notes = _collect(sites, "/visitors", query, max_rows=max_rows)
    header = (
        "GET /visitors | sites : "
        + ", ".join(s.code for s in sites)
        + " | filtres : "
        + json.dumps(_clean_query(query), ensure_ascii=False)
        + f" | max_rows={max_rows} PAR SITE"
    )
    return _render_table(rows, header, truncated, notes, fields or VISITOR_BRIEF)


@mcp.tool()
@_guard
def peripass_get_visitor(reference: str, site: str = "") -> str:
    """Un visiteur, par reference. Cherche sur tous les sites vises.

    `reference` s'ecrit "<identifiant>:<valeur>". L'identifiant est soit `id`
    (l'id technique Peripass), soit le NOM TECHNIQUE d'un champ personnalise :

        "id:10"          le visiteur 10
        "tms_id:90"      le visiteur dont le champ tms_id vaut 90

    Sans prefixe, l'id technique est suppose. Les noms techniques se trouvent
    avec peripass_fields.

    Un id est propre a un tenant : le meme id existe des deux cotes et designe
    deux visiteurs differents. C'est pour ca que cet outil interroge par defaut
    les deux sites et etiquette chaque reponse.
    """
    ref = reference.strip()
    if not ref:
        raise PeripassError("reference est obligatoire. Exemple : \"id:10\".")
    if ref.count(":") > 1 or "." in ref:
        raise PeripassError(
            "Une reference ne peut contenir ni point ni double deux-points. "
            "Forme attendue : \"<identifiant>:<valeur>\", par exemple "
            "\"id:10\" ou \"tms_id:90\"."
        )
    out: dict[str, Any] = {}
    for target in _sites(site):
        try:
            out[target.code] = _request(target, f"/visitors/{ref}")
        except PeripassError as exc:
            out[target.code] = f"ERREUR : {exc}"
    return _render_json(out, f"GET /visitors/{ref}")


@mcp.tool()
@_guard
def peripass_visitor_history(visitor_id: int, site: str, max_rows: int = 300) -> str:
    """L'historique d'un visiteur : les changements d'etat, horodates.

    `site` est OBLIGATOIRE ici : un id de visiteur n'a de sens que dans son
    tenant, et interroger le mauvais site rendrait l'historique d'un autre
    camion sans le dire.
    """
    if not site.strip():
        raise PeripassError(
            "site est obligatoire pour cet outil : un id de visiteur n'existe "
            "que dans un tenant. Precise AUV ou AMB."
        )
    sites = _sites(site)
    rows, truncated, notes = _collect(
        sites, f"/visitors/{int(visitor_id)}/history", None, max_rows=max_rows
    )
    return _render_table(
        rows,
        f"GET /visitors/{int(visitor_id)}/history | site : "
        + ", ".join(s.code for s in sites),
        truncated,
        notes,
        "",
    )


@mcp.tool()
@_guard
def peripass_visitor_detail(visitor_id: int, site: str, include: str = "") -> str:
    """Le detail d'un visiteur par son id technique, et ses sous-ressources.

    include : liste separee par des virgules parmi dispatch, profiles,
    attachments, documenttypes. Vide = dispatch seul.

    `site` est OBLIGATOIRE : un id n'existe que dans son tenant.
    """
    if not site.strip():
        raise PeripassError(
            "site est obligatoire pour cet outil : un id de visiteur n'existe "
            "que dans un tenant. Precise AUV ou AMB."
        )
    target = _sites(site)[0]
    vid = int(visitor_id)
    allowed = {
        "dispatch": (f"/visitors/{vid}/dispatchdashboardinfo", False),
        "profiles": (f"/visitors/{vid}/assignedprofiles", True),
        "attachments": (f"/visitors/{vid}/attachments", True),
        "documenttypes": (f"/visitors/{vid}/alloweddocumenttypes", True),
    }
    wanted = [x.strip() for x in include.split(",") if x.strip()] or ["dispatch"]
    out: dict[str, Any] = {}
    for name in wanted:
        if name not in allowed:
            out[name] = (
                f"inconnu. Valeurs possibles : {', '.join(sorted(allowed))}"
            )
            continue
        path, paged = allowed[name]
        try:
            if paged:
                rows, cut = _paginate(target, path, None, max_rows=200)
                out[name] = rows
                if cut:
                    out[f"{name}_note"] = "resultat tronque a 200 lignes"
            else:
                out[name] = _request(target, path)
        except PeripassError as exc:
            out[name] = f"ERREUR : {exc}"
    return _render_json(out, f"Visiteur {vid} sur {target.code}")


@mcp.tool()
@_guard
def peripass_visitor_attachments(visitor_id: int, site: str, max_rows: int = 200) -> str:
    """Les pieces jointes d'un visiteur : nom, type, date, auteur.

    Le CONTENU des fichiers n'est pas telechargeable par ce connecteur : le
    chemin de telechargement rend un binaire, il n'a rien a faire dans une
    conversation. Ouvre la piece depuis l'interface Peripass.
    """
    if not site.strip():
        raise PeripassError(
            "site est obligatoire pour cet outil : un id de visiteur n'existe "
            "que dans un tenant. Precise AUV ou AMB."
        )
    sites = _sites(site)
    rows, truncated, notes = _collect(
        sites, f"/visitors/{int(visitor_id)}/attachments", None, max_rows=max_rows
    )
    return _render_table(
        rows,
        f"GET /visitors/{int(visitor_id)}/attachments | site : "
        + ", ".join(s.code for s in sites),
        truncated,
        notes,
        "",
    )


@mcp.tool()
@_guard
def peripass_fields(collection: str = "visitors", sample: int = 200, site: str = "") -> str:
    """Les champs personnalises reellement presents, et leur taux de remplissage.

    L'API Peripass n'expose aucun schema des champs personnalises : ils
    arrivent dans un objet `fields` dont les cles dependent de la configuration
    du tenant. Or search_field attend le NOM TECHNIQUE exact, sensible a la
    casse. Cet outil echantillonne la collection et rend la liste reelle.

    A appeler AVANT toute recherche par champ personnalise, et avant de
    comparer deux sites : rien ne garantit que AUV et AMB portent les memes
    champs, ni les memes libelles.

    collection : visitors, assets ou tasks.
    """
    paths = {"visitors": "/visitors", "assets": "/assets", "tasks": "/tasks"}
    key = collection.strip().lower()
    if key not in paths:
        raise PeripassError(
            f"collection inconnue : {collection}. Valeurs possibles : "
            + ", ".join(sorted(paths))
        )
    sites = _sites(site)
    limit = max(1, min(1000, int(sample)))
    lines = [
        f"Champs personnalises presents dans {key}, sur un echantillon de "
        f"{limit} ligne(s) par site.",
        "",
        "Le nom technique est la cle a passer a search_field. Il est SENSIBLE "
        "A LA CASSE.",
        "",
    ]
    for target in sites:
        try:
            rows, _ = _paginate(target, paths[key], None, max_rows=limit)
        except (ConfigError, PeripassError) as exc:
            lines.append(f"[{target.code}] ECHEC : {exc}")
            lines.append("")
            continue
        counts: dict[str, int] = {}
        samples: dict[str, list[str]] = {}
        for row in rows:
            custom = row.get("fields")
            if not isinstance(custom, dict):
                continue
            for name, value in custom.items():
                text = "" if value is None else str(value).strip()
                if not text:
                    continue
                counts[name] = counts.get(name, 0) + 1
                bucket = samples.setdefault(name, [])
                if len(bucket) < 3 and text not in bucket:
                    bucket.append(text[:40])
        lines.append(f"[{target.code}] {len(rows)} ligne(s) lues, {len(counts)} champ(s) renseigne(s)")
        if not counts:
            lines.append("  (aucun champ personnalise renseigne sur cet echantillon)")
        for name in sorted(counts, key=lambda n: (-counts[n], n)):
            rate = 100 * counts[name] / max(1, len(rows))
            lines.append(
                f"  {name}  -  rempli sur {counts[name]}/{len(rows)} "
                f"({rate:.0f} %)  -  ex. : {', '.join(samples.get(name, []))}"
            )
        lines.append("")
    lines.append(
        "Un champ absent de cette liste n'est pas forcement absent du tenant : "
        "il peut n'etre renseigne sur aucune ligne de l'echantillon. Elargis "
        "sample ou change de perimetre avant de conclure."
    )
    return "\n".join(lines)


# --- Assets, taches, referentiels ---------------------------------------


@mcp.tool()
@_guard
def peripass_list_assets(
    site: str = "",
    updated_since: str = "",
    search_object: str = "",
    search_field: str = "",
    search_text: str = "",
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Les assets sur la cour : remorques, caisses, contenants.

    search_object vaut SystemField (DisplayName, Status, Category) ou
    CustomField (nom technique, cf. peripass_fields). La recherche est EXACTE
    et sensible a la casse.

    Statuts possibles : ToLoad, FinishedFull, ToUnload, FinishedEmpty, Offsite.
    """
    query = {
        "updatedSince": updated_since,
        "searchObject": _check_enum(search_object, "AssetSearchObject", "search_object"),
        "searchField": search_field,
        "searchText": search_text,
    }
    if search_text and not query["searchObject"]:
        raise PeripassError(
            "search_text sans search_object : le filtre serait ignore en "
            "silence. Precise search_object parmi "
            + ", ".join(_enum("AssetSearchObject"))
            + "."
        )
    sites = _sites(site)
    rows, truncated, notes = _collect(sites, "/assets", query, max_rows=max_rows)
    header = (
        "GET /assets | sites : "
        + ", ".join(s.code for s in sites)
        + " | filtres : "
        + json.dumps(_clean_query(query), ensure_ascii=False)
    )
    return _render_table(rows, header, truncated, notes, fields or ASSET_BRIEF)


@mcp.tool()
@_guard
def peripass_get_asset(asset_id: int, site: str) -> str:
    """Un asset par son id. `site` est obligatoire : un id est propre a un tenant."""
    if not site.strip():
        raise PeripassError(
            "site est obligatoire pour cet outil : un id d'asset n'existe que "
            "dans un tenant. Precise AUV ou AMB."
        )
    target = _sites(site)[0]
    payload = _request(target, f"/assets/{int(asset_id)}")
    return _render_json(payload, f"GET /assets/{int(asset_id)} sur {target.code}")


@mcp.tool()
@_guard
def peripass_list_tasks(
    site: str = "",
    updated_since: str = "",
    search_object: str = "",
    search_field: str = "",
    search_text: str = "",
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Les taches de cour : ce qui est demande aux operateurs, et ou ca en est.

    search_object vaut SystemField (TaskTemplateId, Status, AssignedOperatorId)
    ou CustomField. Recherche exacte, sensible a la casse.
    """
    query = {
        "updatedSince": updated_since,
        "searchObject": _check_enum(search_object, "TaskSearchObject", "search_object"),
        "searchField": search_field,
        "searchText": search_text,
    }
    if search_text and not query["searchObject"]:
        raise PeripassError(
            "search_text sans search_object : le filtre serait ignore en "
            "silence. Precise search_object parmi "
            + ", ".join(_enum("TaskSearchObject"))
            + "."
        )
    sites = _sites(site)
    rows, truncated, notes = _collect(sites, "/tasks", query, max_rows=max_rows)
    header = (
        "GET /tasks | sites : "
        + ", ".join(s.code for s in sites)
        + " | filtres : "
        + json.dumps(_clean_query(query), ensure_ascii=False)
    )
    return _render_table(rows, header, truncated, notes, fields or TASK_BRIEF)


@mcp.tool()
@_guard
def peripass_get_task(task_id: int, site: str) -> str:
    """Une tache par son id. `site` est obligatoire : un id est propre a un tenant."""
    if not site.strip():
        raise PeripassError(
            "site est obligatoire pour cet outil : un id de tache n'existe que "
            "dans un tenant. Precise AUV ou AMB."
        )
    target = _sites(site)[0]
    payload = _request(target, f"/tasks/{int(task_id)}")
    return _render_json(payload, f"GET /tasks/{int(task_id)} sur {target.code}")


@mcp.tool()
@_guard
def peripass_list_certified_persons(
    site: str = "",
    person_filter: str = "",
    updated_since: str = "",
    search_field: str = "",
    search_text: str = "",
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Les personnes certifiees et leurs habilitations.

    person_filter vaut Active, Archived ou All.

    Attention : cette collection porte des donnees NOMINATIVES (nom, prenom,
    telephone, date de naissance, numero d'identification). Elle sert a
    verifier qu'une habilitation existe et qu'elle est valide, pas a constituer
    une liste de personnes. Ne recopie pas ces lignes dans une note ni dans un
    livrable.
    """
    query = {
        "filter": person_filter,
        "updatedSince": updated_since,
        "searchObject": "SystemField" if search_text else "",
        "searchField": search_field,
        "searchText": search_text,
    }
    if person_filter and person_filter not in {"Active", "Archived", "All"}:
        raise PeripassError(
            f"person_filter : valeur inconnue « {person_filter} ». Valeurs "
            "permises : Active, Archived, All."
        )
    sites = _sites(site)
    rows, truncated, notes = _collect(
        sites, "/certifiedpersons", query, max_rows=max_rows
    )
    header = (
        "GET /certifiedpersons | sites : "
        + ", ".join(s.code for s in sites)
        + " | filtres : "
        + json.dumps(_clean_query(query), ensure_ascii=False)
        + "\nDonnees nominatives : a lire, pas a recopier."
    )
    return _render_table(rows, header, truncated, notes, fields)


@mcp.tool()
@_guard
def peripass_referential(name: str, site: str = "", max_rows: int = 300) -> str:
    """Les referentiels de configuration d'un tenant.

    name vaut :
      - profiles              les profils de visiteur (le type de transport)
      - dispatchdashboards    les tableaux de dispatch (les quais, les zones)
      - selfservicekiosks     les bornes d'accueil

    A lire AVANT de filtrer : un nom de profil n'est pas garanti identique
    entre AUV et AMB, et c'est la premiere cause d'un chiffre qui ne se
    compare pas d'un site a l'autre.
    """
    known = {
        "profiles": "/profiles",
        "dispatchdashboards": "/dispatchdashboards",
        "selfservicekiosks": "/selfservicekiosks",
    }
    key = name.strip().lower().replace("-", "").replace("_", "")
    if key not in known:
        raise PeripassError(
            f"Referentiel inconnu : {name}. Valeurs possibles : "
            + ", ".join(sorted(known))
        )
    sites = _sites(site)
    rows, truncated, notes = _collect(sites, known[key], None, max_rows=max_rows)
    return _render_table(
        rows,
        f"GET {known[key]} | sites : " + ", ".join(s.code for s in sites),
        truncated,
        notes,
        "",
    )


# --- Export et echappatoire ---------------------------------------------


@mcp.tool()
@_guard
def peripass_export_csv(
    path: str = "/visitors",
    query_json: str = "{}",
    site: str = "",
    filename: str = "",
    max_rows: int = 20000,
) -> str:
    """Pagine une collection entiere et ecrit un CSV. SUR DEMANDE EXPLICITE.

    C'est le remplacant du script Power Query : meme pagination
    pageNumber/pageSize jusqu'a page vide, meme aplatissement des objets
    imbriques en colonnes pointees, et la meme colonne `site` qui distingue
    AUV de AMB.

    N'appelle cet outil QUE si l'utilisateur a demande un fichier, un export ou
    un CSV. Il ecrit sur le disque : ce n'est pas une etape de routine, et un
    perimetre trop gros pour la conversation ne le justifie pas.

    CSV point-virgule, UTF-8 avec BOM : il s'ouvre dans Excel FR sans passer
    par l'assistant d'import.
    """
    checked, template = _check_get_path(path)
    try:
        query = json.loads(query_json or "{}")
    except ValueError as exc:
        raise PeripassError(f"query_json n'est pas du JSON valide : {exc}") from exc
    if not isinstance(query, dict):
        raise PeripassError("query_json doit etre un objet JSON.")

    sites = _sites(site)
    rows, truncated, notes = _collect(
        sites,
        checked,
        query,
        max_rows=max(1, int(max_rows)),
        paged=_supports_paging(template),
    )
    flat = [_flatten(r) for r in rows]

    target_dir = _export_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PeripassError(
            f"Dossier d'export inutilisable ({target_dir}) : {exc}"
        ) from exc

    if filename.strip():
        name = filename.strip()
        if not name.lower().endswith(".csv"):
            name += ".csv"
    else:
        slug = re.sub(r"[^a-z0-9]+", "_", checked.lower()).strip("_") or "export"
        name = f"peripass_{slug}_{dt.date.today().isoformat()}.csv"
    target = target_dir / name

    try:
        target.write_text(_to_csv_text(flat), encoding="utf-8-sig")
    except OSError as exc:
        raise PeripassError(f"Ecriture impossible dans {target} : {exc}") from exc

    cols = _columns(flat)
    out = [
        f"Export termine : {target}",
        f"{len(flat)} ligne(s), {len(cols)} colonne(s).",
        f"Requete : GET {checked} | sites : "
        + ", ".join(s.code for s in sites)
        + " | filtres : "
        + json.dumps(_clean_query(query), ensure_ascii=False),
        "Par site : " + " | ".join(notes),
    ]
    if truncated:
        out.append(
            "ATTENTION : export tronque sur "
            + ", ".join(truncated)
            + " (max_rows par site, ou PERIPASS_MAX_PAGES, atteint). Le "
            "fichier n'est PAS le perimetre complet : resserre les dates, ou "
            "releve PERIPASS_MAX_PAGES."
        )
    if any("ECHEC" in n for n in notes):
        out.append(
            "ATTENTION : au moins un site n'a pas repondu. Le fichier ne "
            "couvre pas tout le perimetre demande."
        )
    if cols:
        out.append("")
        out.append("Colonnes : " + ", ".join(cols[:60]))
        if len(cols) > 60:
            out.append(f"... et {len(cols) - 60} autres.")
    return "\n".join(out)


@mcp.tool()
@_guard
def peripass_get(
    path: str,
    query_json: str = "{}",
    site: str = "",
    max_rows: int = 200,
    fields: str = "",
) -> str:
    """Appelle n'importe quel chemin GET du contrat Peripass. Echappatoire.

    Pour les chemins qui n'ont pas d'outil dedie. Appelle peripass_list_paths
    d'abord, pour le chemin exact et ses filtres.

    Un chemin absent de la liste blanche est refuse, et seule la methode GET
    est emise : aucune ecriture n'est possible par cet outil.
    """
    checked, template = _check_get_path(path)
    try:
        query = json.loads(query_json or "{}")
    except ValueError as exc:
        raise PeripassError(f"query_json n'est pas du JSON valide : {exc}") from exc
    if not isinstance(query, dict):
        raise PeripassError("query_json doit etre un objet JSON.")

    sites = _sites(site)
    header = (
        f"GET {checked} | sites : "
        + ", ".join(s.code for s in sites)
        + " | filtres : "
        + json.dumps(_clean_query(query), ensure_ascii=False)
    )
    paged = _supports_paging(template) and "pageNumber" not in query
    rows, truncated, notes = _collect(
        sites, checked, query, max_rows=max_rows, paged=paged
    )
    return _render_table(rows, header, truncated, notes, fields)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

HELP = """\
MCP peripass - interrogation en lecture seule du yard management Peripass.

  python server.py            mode serveur MCP (stdio), lance par Claude Code
  python server.py doctor     diagnostic de configuration et de connexion
  python server.py --help     cette aide

Configuration, en deux couches :
  - l'equipe : 08_ENGINE/04_mcp/00_config/peripass.shared.env, sur le drive
    partage. Sites, racines d'API, tenants, plafonds. Rien a y faire.
  - le poste : peripass.env hors du vault, par defaut
    ~/.peripass-mcp/peripass.env, cree par install.ps1. Modele :
    peripass.env.example.
Priorite : configuration du plugin > peripass.env du poste > fichier d'equipe >
defaut du serveur. Voir README.md.
"""


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if arg == "doctor":
        report = peripass_doctor()
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
