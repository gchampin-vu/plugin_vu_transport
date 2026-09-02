#!/usr/bin/env python3
"""
MCP trustpilot : lecture des avis de service Trustpilot des dix-huit domaines
du groupe, et interpretation des commentaires clients.

Trois usages :
    python server.py            # mode serveur MCP (stdio) - lance par Claude Code
    python server.py doctor     # diagnostic de configuration et de connexion
    python server.py --help

LECTURE SEULE PAR CONSTRUCTION. L'API Trustpilot sait repondre a un avis, poser
un tag, contacter l'auteur d'un avis et envoyer des invitations. **Aucune de ces
operations n'est exposee ici**, et ce n'est pas un reglage : le serveur n'emet
que des GET, et il valide chaque chemin contre la liste blanche de
api_paths.json. Repondre publiquement a un client engage la marque sur une page
publique et indexee - c'est la regle n. 4 du CLAUDE.md de la bibliotheque,
l'envoi est un geste humain.

Deux regimes d'authentification, et c'est la premiere chose a comprendre :

  - les chemins PUBLICS demandent la seule cle d'API, envoyee en en-tete
    `apikey`. Ils rendent les avis, mais **sans aucun filtre de date**, et sans
    le referenceId ni le referralEmail ;
  - les chemins PRIVES demandent un jeton OAuth (`Authorization: Bearer`),
    donc la cle **et** le secret. Ils portent les bornes de date, le
    referenceId - notre numero de commande - et le referralEmail.

Le connecteur marche avec la seule cle : il retombe alors sur le chemin public
et le dit. Il ne se bloque pas parce que le secret manque.

Origine : ce connecteur reprend le script Power Query de Guillaume Champin
(dix-huit domaines, fusion prive + public avec deduplication sur l'identifiant
d'avis, depart au 2023-01-01). La fusion et sa deduplication sont conservees a
l'identique dans le mode `fusion`.

Documentation de reference : https://developers.trustpilot.com/service-reviews-api/
et /business-units-api/. Relevee le 2026-09-01. Trustpilot ne publie pas de
contrat OpenAPI telechargeable : la liste blanche est ecrite a la main.

Configuration : voir README.md. Rien de secret ne vit dans ce dossier.
"""

from __future__ import annotations

import base64
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
from mcp.types import ToolAnnotations

import vu_cache as cache

DEFAULT_BASE_URL = "https://api.trustpilot.com"

# Plafond impose par l'API sur les collections d'avis.
PAGE_LIMIT = 100

DEFAULT_TIMEOUT_S = 60.0

# Garde-fou de pagination du DIRECT. Au-dela, on passe par le cache
# (trustpilot_sync) ou par un export. 60 pages x 100 = 6 000 avis par domaine.
DEFAULT_MAX_PAGES = 60

# Date de depart du perimetre, reprise du script Power Query d'origine.
DEFAULT_START_DATE = "2023-01-01"

# Plafond de caracteres rendus dans la conversation par un outil de liste.
RENDER_MAX_CHARS = 24_000

# Verbatims : combien on en rend, et sur quelle longueur. Un avis Trustpilot
# fait 300 caracteres en moyenne et jusqu'a 2 000 : sans plafond, quarante avis
# saturent la fenetre.
VERBATIM_DEFAULT_COUNT = 40
VERBATIM_MAX_COUNT = 200
VERBATIM_TEXT_CHARS = 600

RETRY_ATTEMPTS = 3
RETRY_STATUSES = (429, 500, 502, 503, 504)

# Les dix-huit domaines du perimetre, dans l'ordre du script d'origine.
DEFAULT_DOMAINS = (
    "www.vente-unique.com",
    "www.habitat.fr",
    "www.kauf-unique.de",
    "www.kauf-unique.at",
    "www.vente-unique.be",
    "www.vente-unique.ch",
    "www.vente-unique.dk",
    "www.vente-unique.es",
    "www.vente-unique.ie",
    "www.vente-unique.it",
    "www.vente-unique.nl",
    "www.vente-unique.no",
    "www.vente-unique.pl",
    "www.vente-unique.pt",
    "www.vente-unique.se",
    "www.vente-unique.uk",
    "www.vente-unique.lu",
    "www.habitat-design.com",
)

# Colonnes numeriques du cache. Sans ce typage, un AVG() en SQL moyenne des
# chaines et rend n'importe quoi - et une note moyenne fausse part en revue.
NUMERIC_COLUMNS = {
    "stars",
    "numberOfLikes",
    "consumer.numberOfReviews",
    "delai_reponse_h",
    "delai_experience_j",
}

# Colonnes indexees a la synchronisation : celles sur lesquelles on filtre.
INDEX_COLUMNS = ("createdAt", "annee_mois", "domaine", "pays", "stars", "language")

# Donnees personnelles. Elles ne sortent JAMAIS dans la conversation, et ne
# sortent en CSV que sur demande explicite. Un avis est public ; l'adresse mail
# du client invite ne l'est pas, et elle n'a rien a faire dans un livrable ni
# dans un mail transporteur.
PERSONAL_COLUMNS = frozenset(
    {
        "referralEmail",
        "consumer.id",
        "consumer.links",
        "findReviewer",
        "reportData",
    }
)

# Projection courte des avis. Un avis aplati porte plus de soixante colonnes ;
# les rendre toutes sature la conversation pour rien. fields="*" rend tout.
REVIEW_BRIEF = (
    "id,domaine,pays,enseigne,stars,createdAt,experiencedAt,language,"
    "titre,texte_court,a_reponse,delai_reponse_h,themes,transporteurs_cites,"
    "source,isVerified,referenceId,review_source"
)

HERE = pathlib.Path(__file__).resolve().parent
LEXIQUE_FILE = HERE / "lexique.json"
SPEC_FILE = HERE / "api_paths.json"

# Dossiers synchronises : un secret n'y vit pas.
SYNCED_MARKERS = ("/onedrive", "cafom", "sharepoint", "dropbox", "google drive")


class ConfigError(RuntimeError):
    pass


class TrustpilotError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Liste blanche des chemins GET
# --------------------------------------------------------------------------

_SPEC: dict[str, Any] | None = None


def _load_spec() -> dict[str, Any]:
    try:
        return json.loads(SPEC_FILE.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(
            f"Liste des chemins GET introuvable ({SPEC_FILE}) : {exc}. "
            "Ce fichier fait partie du serveur, il est versionne a cote de "
            "server.py. Sans lui, aucun appel n'est possible : le serveur ne "
            "connait plus aucun chemin autorise."
        ) from exc


def _spec() -> dict[str, Any]:
    global _SPEC
    if _SPEC is None:
        _SPEC = _load_spec()
    return _SPEC


def _get_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Les gabarits de la liste blanche, LITTERAUX D'ABORD.

    L'ordre n'est pas cosmetique. `/v1/business-units/find` et
    `/v1/business-units/search` matchent aussi le gabarit
    `/v1/business-units/{businessUnitId}`, puisqu'une accolade devient
    `[^/]+`. Si le gabarit parametre est essaye en premier, _spec_for rend le
    mauvais - et avec lui le mauvais nom de parametre de pagination : la
    recherche de business unit attend `perpage` tout en minuscules, les avis
    attendent `perPage`. Le connecteur enverrait alors un parametre que l'API
    ignore, et appliquerait un plafond par defaut en silence.

    Un chemin litteral est donc toujours plus specifique qu'un chemin
    parametre, et passe devant.
    """
    out: list[tuple[str, re.Pattern[str]]] = []
    chemins = sorted(_spec()["paths"], key=lambda p: ("{" in p, p))
    for path in chemins:
        regex = "^" + re.sub(r"\\\{[^/}]+\\\}", "[^/]+", re.escape(path)) + "$"
        out.append((path, re.compile(regex)))
    return out


def _check_get_path(path: str) -> str:
    """Normalise et valide un chemin contre la liste blanche des GET.

    Rend le chemin canonique (celui de la liste blanche, avec ses accolades)
    et le chemin concret separement n'aurait pas de sens ici : on rend le
    chemin concret, et _spec_for retrouve son gabarit.
    """
    path = (path or "").strip()
    if "?" in path:
        raise TrustpilotError(
            "Passe les parametres de requete dans query_json, pas dans le chemin."
        )
    if not path.startswith("/"):
        path = "/" + path
    for _, pattern in _get_patterns():
        if pattern.match(path):
            return path
    alt = path[:-1] if path.endswith("/") else path + "/"
    for _, pattern in _get_patterns():
        if pattern.match(alt):
            return alt
    raise TrustpilotError(
        f"Chemin inconnu ou non accessible en lecture : {path}\n"
        f"Ce serveur est en LECTURE SEULE : seuls les {len(_spec()['paths'])} "
        "chemins GET releves dans api_paths.json sont atteignables, et aucune "
        "ecriture n'est possible - ni reponse a un avis, ni tag, ni invitation.\n"
        "Appelle trustpilot_list_paths pour voir la liste et les parametres."
    )


def _spec_for(path: str) -> dict[str, Any]:
    """Le gabarit de la liste blanche qui correspond a un chemin concret."""
    for template, pattern in _get_patterns():
        if pattern.match(path):
            return _spec()["paths"][template]
    return {}


def _auth_for(path: str) -> str:
    """'apikey' ou 'oauth'. C'est la liste blanche qui le dit, pas une devinette."""
    return _spec_for(path).get("auth") or "apikey"


def _supports_paging(path: str) -> bool:
    return bool(_spec_for(path).get("paged"))


def _page_params(path: str) -> tuple[str, str]:
    """Les noms des parametres de pagination. Ils ne sont pas les memes partout.

    /business-units/search attend `perpage` tout en minuscules, la ou les
    collections d'avis attendent `perPage`. Envoyer le mauvais est un HTTP 400,
    ou pire : un plafond par defaut applique en silence.
    """
    spec = _spec_for(path)
    return spec.get("page_param") or "page", spec.get("per_page_param") or "perPage"


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
    VIRTUALISEE de %LOCALAPPDATA% : un fichier pose d'un cote est invisible de
    l'autre, sans aucune erreur. C'est pour cela que la racine locale de ce
    connecteur est sous le profil utilisateur (voir _local_root).
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


def _candidate_env_files() -> list[pathlib.Path]:
    """Emplacements de .env essayes, dans l'ordre."""
    out: list[pathlib.Path] = []
    explicit = (os.environ.get("TRUSTPILOT_ENV_FILE") or "").strip()
    if explicit:
        out.append(pathlib.Path(explicit))
    out.append(_local_root() / ".env")
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        out.append(pathlib.Path(plugin_data) / ".env")
    # Voisin du script : refuse a la lecture s'il est dans un dossier
    # synchronise, mais on le regarde quand meme pour pouvoir le DIRE plutot
    # que de rester muet sur un fichier que quelqu'un a bien rempli.
    out.append(HERE / ".env")
    return out


def _parse_env(text: str) -> dict[str, str]:
    """Parseur .env minimal : KEY=VALUE, # en commentaire, guillemets optionnels.

    Un secret Trustpilot peut contenir n'importe quoi (%, *, /, #, =). La valeur
    n'est donc jamais decoupee sur un # sans espace devant, et des guillemets la
    preservent telle quelle. Le `=` interne est preserve par partition().
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
    """Charge le premier .env exploitable. Une variable du process NON VIDE gagne.

    Le "non vide" n'est pas un detail : en mode plugin, la configuration arrive
    par l'environnement (`TRUSTPILOT_API_KEY=${user_config.api_key}`). Si le
    champ n'est pas rempli, la substitution pose une variable VIDE, et un
    setdefault la considererait comme renseignee - le .env du poste serait alors
    ignore en silence.
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
                f"({path}) et n'a PAS ete lu : il porterait un secret sur le "
                "drive partage de l'equipe. Deplace-le vers "
                f"{_local_root() / '.env'}, ou pointe un emplacement local "
                "avec TRUSTPILOT_ENV_FILE."
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
# Ce fichier porte ce qui est identique sur les vingt-six postes : la racine de
# l'API, la liste des dix-huit domaines, la date de depart du perimetre, les
# plafonds - et les identifiants d'APPLICATION (cle et secret), qui sont ceux
# d'une integration, pas d'une personne.
#
# CE QU'IL NE PORTE PAS, ET POURQUOI. Le refresh token OAuth est refuse ici,
# exactement comme sur le connecteur JIRA : Trustpilot fait TOURNER le refresh
# token a chaque echange, le serveur range le nouveau dans un magasin LOCAL, et
# l'ancien peut etre invalide cote Trustpilot. Le premier poste qui s'en sert
# perimerait donc le jeton partage pour les vingt-cinq autres, qui verraient un
# invalid_grant sans comprendre pourquoi. Chacun saisit le sien s'il en a un -
# et le chemin nominal reste le client_credentials, qui ne tourne pas.
#
# La liste blanche ci-dessous est un garde-fou contre la faute de frappe : une
# variable mal orthographiee dans le fichier partage serait sinon ignoree en
# silence, et on chercherait longtemps pourquoi le reglage d'equipe ne prend
# pas. Elle est refusee ET signalee par /trustpilot-setup.

SHARED_FILE_NAME = "trustpilot.shared.env"
ENGINE_DIR_NAME = "08_ENGINE"
SHARED_SUBPATH = ("04_mcp", "00_config")

SHARED_ALLOWED_KEYS = frozenset(
    {
        "TRUSTPILOT_BASE_URL",
        "TRUSTPILOT_DOMAINS",
        "TRUSTPILOT_START_DATE",
        "TRUSTPILOT_PER_PAGE",
        "TRUSTPILOT_MAX_PAGES",
        "TRUSTPILOT_TIMEOUT_S",
        "TRUSTPILOT_API_KEY",
        "TRUSTPILOT_API_SECRET",
    }
)

# Les cles dont la VALEUR ne s'affiche jamais, meme dans un diagnostic. Le NOM,
# lui, se dit : c'est ce qui permet de savoir d'ou vient la valeur active sans
# la reveler.
SHARED_SECRET_KEYS = frozenset({"TRUSTPILOT_API_KEY", "TRUSTPILOT_API_SECRET"})

_SHARED_CACHE: dict[str, str] | None = None
_SHARED_LOADED_FROM: str = ""
_SHARED_REJECTED: list[str] = []


def _engine_roots() -> list[pathlib.Path]:
    """Racines `08_ENGINE` plausibles, dans l'ordre de preference.

    Le plugin est installe depuis `08_ENGINE/03_plugins/...`, mais Claude Code
    en fait une copie dans `~/.claude/plugins/`. On ne peut donc pas se
    contenter de remonter depuis le code : on remonte quand meme (installation
    directe depuis la bibliotheque), et on complete par le profil utilisateur,
    ou OneDrive pose la bibliotheque d'equipe. Le point de montage differe
    selon l'OS - c'est pour cela qu'on le CHERCHE au lieu de le deviner.
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

    starts = [HERE]
    plugin_root = (os.environ.get("CLAUDE_PLUGIN_ROOT") or "").strip()
    if plugin_root:
        starts.append(pathlib.Path(plugin_root))
    for start in starts:
        for parent in start.parents:
            if parent.name == ENGINE_DIR_NAME:
                add(parent)
                break

    home = pathlib.Path.home()
    for pattern in (
        "CAFOM/*/" + ENGINE_DIR_NAME,
        "*/CAFOM/*/" + ENGINE_DIR_NAME,
        "Library/CloudStorage/*/" + ENGINE_DIR_NAME,
        "Library/CloudStorage/*/*/" + ENGINE_DIR_NAME,
    ):
        try:
            for hit in sorted(home.glob(pattern)):
                if hit.is_dir():
                    add(hit)
        except OSError:
            continue
    return out


def _shared_env_candidates() -> list[pathlib.Path]:
    """Emplacements du fichier d'equipe essayes, dans l'ordre.

    **Un chemin explicite est EXCLUSIF.** Si TRUSTPILOT_SHARED_ENV est pose, on
    ne cherche rien d'autre : qui pointe un fichier precis attend ce fichier,
    pas un repli silencieux sur la configuration de production. C'est aussi ce
    qui permet a test_offline.py de simuler un poste sans configuration
    d'equipe - sans quoi la suite tournerait avec les vraies cles et ne
    prouverait rien.
    """
    explicit = (os.environ.get("TRUSTPILOT_SHARED_ENV") or "").strip()
    if explicit:
        return [pathlib.Path(explicit)]
    return [root.joinpath(*SHARED_SUBPATH, SHARED_FILE_NAME) for root in _engine_roots()]


def _load_shared_env() -> dict[str, str]:
    """Lit le premier fichier d'equipe trouve, filtre par la liste blanche.

    Ne leve jamais : un fichier absent, illisible ou mal rempli ne doit pas
    empecher le connecteur de tourner sur la configuration du poste. Ce qui a
    ete refuse est garde de cote pour que setup_status le dise.

    Les valeurs lues ne sont PAS versees dans os.environ : elles restent dans ce
    cache, et c'est _env qui va les chercher en dernier recours. Les secrets
    d'equipe n'apparaissent donc pas dans l'environnement du processus, ni dans
    ce qu'un sous-processus en heriterait.
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
            "  Deux causes possibles, et une seule est benigne. Soit c'est une "
            "faute de frappe dans le nom de la variable - compare avec "
            ".env.example. Soit c'est TRUSTPILOT_REFRESH_TOKEN, refuse "
            "volontairement : Trustpilot fait tourner ce jeton, deux postes qui "
            "le partagent se le cassent mutuellement. Il se saisit dans /plugin."
        )
    return lines


def _env(name: str, default: str = "") -> str:
    """La valeur retenue pour un reglage, dans un ordre de priorite fixe.

    1. l'environnement du processus - la configuration du plugin, et le .env
       local que _load_env_file y a deja verse : ce que CE POSTE a decide ;
    2. le fichier d'equipe de 08_ENGINE - ce que l'EQUIPE a decide ;
    3. la valeur par defaut du serveur.

    Le poste passe donc devant l'equipe, et non l'inverse : un reglage d'equipe
    est un point de depart commun, pas une contrainte. C'est ce qui permet a
    quelqu'un d'utiliser une cle nominative sans toucher au fichier partage.
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
    key = _env("TRUSTPILOT_API_KEY")
    if not key:
        tried = "\n".join(f"  - {p}" for p in _candidate_env_files())
        packaged = _packaged_python()
        raise ConfigError(
            "TRUSTPILOT_API_KEY manquant."
            + (f"\n\n{_ENV_LOAD_ERROR}" if _ENV_LOAD_ERROR else "")
            + (
                f"\n\nATTENTION : {packaged}. Un .env pose sous %LOCALAPPDATA% "
                "peut etre invisible pour ce processus (virtualisation). "
                "Renseigne la cle dans la configuration du plugin."
                if packaged
                else ""
            )
            + "\n\nEmplacements de .env essayes, dans l'ordre :\n"
            + tried
            + "\n\nPour la mettre en place : appelle trustpilot_setup_status, "
            "qui donne la marche a suivre pour ce poste. En resume, en mode "
            "plugin : /plugin > trustpilot > configuration > « Cle d'API "
            "Trustpilot ». La saisie se fait dans l'interface de Claude Code, "
            "pas dans la conversation.\n\n"
            "La cle se trouve dans Trustpilot Business > Integrations > API "
            "applications. C'est le « API Key », aussi appele Client ID."
        )
    return key


def _api_secret() -> str:
    """Le secret. Vide n'est pas une erreur : il n'est utile qu'au chemin prive."""
    return _env("TRUSTPILOT_API_SECRET")


def _base_url() -> str:
    return _env("TRUSTPILOT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _start_date() -> str:
    return _env("TRUSTPILOT_START_DATE", DEFAULT_START_DATE)


def _timeout() -> float:
    try:
        return float(_env("TRUSTPILOT_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _per_page() -> int:
    try:
        return max(1, min(PAGE_LIMIT, int(_env("TRUSTPILOT_PER_PAGE") or PAGE_LIMIT)))
    except ValueError:
        return PAGE_LIMIT


def _max_pages() -> int:
    try:
        return max(1, int(_env("TRUSTPILOT_MAX_PAGES") or DEFAULT_MAX_PAGES))
    except ValueError:
        return DEFAULT_MAX_PAGES


def _domains() -> list[str]:
    raw = _env("TRUSTPILOT_DOMAINS")
    if not raw:
        return list(DEFAULT_DOMAINS)
    out = [d.strip() for d in raw.replace(";", ",").split(",") if d.strip()]
    return out or list(DEFAULT_DOMAINS)


def _local_root() -> pathlib.Path:
    """Racine locale de l'outil : cache SQLite, jeton OAuth, exports.

    **Sous le profil utilisateur, et le meme chemin sur les deux systemes.**
    C'est la convention de construction de l'equipe (08_ENGINE/04_mcp/README.md),
    et elle tient a deux mesures. Un Python empaquete donne a ses enfants une
    vue virtualisee de %LOCALAPPDATA% - le plugin ecrit d'un cote, le terminal
    lit de l'autre, sans erreur. Et le dossier de donnees du plugin depend de
    l'identifiant d'installation : un jeton range sous l'un serait invisible
    sous l'autre.
    """
    # os.environ et NON _env : cette fonction est appelee pendant le chargement
    # du .env, et passer par _env ferait un aller-retour. Un chemin local n'a
    # de toute facon pas sa place dans la configuration d'equipe.
    raw = (os.environ.get("TRUSTPILOT_HOME") or "").strip()
    if raw:
        return pathlib.Path(raw)
    return pathlib.Path.home() / ".trustpilot-mcp"


def _cache_path() -> pathlib.Path:
    raw = _env("TRUSTPILOT_CACHE_DB")
    return pathlib.Path(raw) if raw else _local_root() / "trustpilot_cache.sqlite"


def _export_dir() -> pathlib.Path:
    """Ou atterrissent les CSV. Jamais dans la bibliotheque d'equipe.

    Un CSV depose dans un dossier synchronise part chez les vingt-six, et la
    base de connaissance ne porte pas de donnees - encore moins des verbatims
    clients.
    """
    raw = _env("TRUSTPILOT_EXPORT_DIR")
    if raw:
        return pathlib.Path(raw)
    return _local_root() / "exports"


def _bu_store_path() -> pathlib.Path:
    return _local_root() / "business_units.json"


def _token_store_path() -> pathlib.Path:
    return _local_root() / "token.json"


def _key_source() -> str:
    """D'ou sort la cle : la premiere question quand ca ne marche pas.

    Une cle d'equipe qui ne marche plus se corrige dans 08_ENGINE, pour tout le
    monde. Une cle de poste se corrige dans /plugin, pour soi seul. Sans cette
    ligne, on corrige au mauvais endroit.
    """
    _load_env_file()
    if not os.environ.get("TRUSTPILOT_API_KEY"):
        if _load_shared_env().get("TRUSTPILOT_API_KEY"):
            return f"config d'equipe ({_SHARED_LOADED_FROM})"
        return "(aucune)"
    if "TRUSTPILOT_API_KEY" in _ENV_FILE_KEYS:
        return f"fichier {_ENV_LOADED_FROM}"
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "configuration du plugin (userConfig)"
    return "environnement du processus"


def _fingerprint(secret: str) -> str:
    """Empreinte courte d'un secret, pour comparer sans jamais l'afficher."""
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def _mask(secret: str) -> str:
    if not secret:
        return "(non renseigne)"
    if len(secret) <= 6:
        return "*" * len(secret)
    return f"{secret[:2]}{'*' * (len(secret) - 5)}{secret[-3:]}"


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
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"droits NTFS non modifies : {exc}"
    if done.returncode != 0:
        return "droits NTFS non modifies (icacls a echoue)"
    return f"droits NTFS restreints a {user}"


def _key_store_path() -> pathlib.Path:
    explicit = (os.environ.get("TRUSTPILOT_ENV_FILE") or "").strip()
    if explicit:
        return pathlib.Path(explicit)
    return _local_root() / ".env"


def _stored_secrets() -> dict[str, str]:
    """Les secrets tels qu'ils sont ecrits sur la machine."""
    path = _key_store_path()
    try:
        if not path.is_file():
            return {}
        values = _parse_env(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}
    return {
        k: v
        for k, v in values.items()
        if k
        in (
            "TRUSTPILOT_API_KEY",
            "TRUSTPILOT_API_SECRET",
            "TRUSTPILOT_REFRESH_TOKEN",
        )
    }


def _write_key_store(values: dict[str, str]) -> tuple[pathlib.Path, str]:
    """Ecrit des secrets dans le magasin local. Rend (chemin, note sur les droits).

    Idempotent : on retire les lignes existantes de ces cles et on les reecrit.
    Le reste du fichier est preserve - il peut porter d'autres reglages.
    """
    path = _key_store_path()
    if _is_synced(path):
        raise ConfigError(
            f"Refus d'ecrire dans un dossier synchronise ({path}) : le secret "
            "partirait sur le drive partage de l'equipe. Definis "
            "TRUSTPILOT_ENV_FILE sur un chemin local."
        )
    keys = list(values)
    lines: list[str] = []
    try:
        if path.is_file():
            pattern = re.compile(r"\s*(" + "|".join(re.escape(k) for k in keys) + r")\s*=")
            lines = [
                line
                for line in path.read_text(encoding="utf-8-sig").splitlines()
                if not pattern.match(line)
            ]
    except OSError as exc:
        raise ConfigError(f"Lecture impossible de {path} : {exc}") from exc

    if not lines:
        lines = [
            "# Configuration du serveur MCP trustpilot.",
            "# Fichier local, hors du vault synchronise.",
            f"# Ecrit le {dt.date.today().isoformat()} par trustpilot_save_key.",
            "",
        ]
    for key, val in values.items():
        lines.append(f'{key}="{val}"')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Ecriture impossible dans {path} : {exc}") from exc
    return path, _restrict_permissions(path)


# --------------------------------------------------------------------------
# OAuth : le jeton du chemin prive
# --------------------------------------------------------------------------
#
# Ce que ce bloc resout. Les chemins prives - les seuls a porter les bornes de
# date et le referenceId - demandent un `Authorization: Bearer`. Le jeton
# d'acces vit 100 heures ; le redemander a chaque session serait du gaspillage,
# et sur le grant `password` c'est aussi un appel plafonne. On le range donc
# dans la racine locale avec sa date d'expiration.
#
# Trois facons d'en obtenir un, essayees dans cet ordre :
#
#   1. `refresh_token`, si le poste en a un. Il vit 30 jours.
#   2. `password`, si un couple identifiant / mot de passe Trustpilot est pose.
#      DEPRECIE par Trustpilot, garde parce que des integrations existantes
#      tournent encore dessus.
#   3. `client_credentials`, avec la cle et le secret de l'application. C'est
#      le chemin NOMINAL : rien ne tourne, rien n'expire au bout de 30 jours,
#      et il ne depend d'aucun compte utilisateur.
#
# LE PIEGE DU REFRESH TOKEN, mesure sur le connecteur Yooz avant d'etre ecrit
# ici : Trustpilot fait TOURNER le refresh token a chaque echange. Le nouveau
# est range en local ; l'ancien peut etre invalide cote Trustpilot. Deux postes
# qui partagent le meme refresh token se le cassent donc mutuellement. C'est
# pour cela qu'il est REFUSE dans le fichier d'equipe (voir
# SHARED_ALLOWED_KEYS) et qu'il se saisit poste par poste.

TOKEN_PATH = "/v1/oauth/oauth-business-users-for-applications/accesstoken"
TOKEN_REFRESH_PATH = "/v1/oauth/oauth-business-users-for-applications/refresh"

# Marge avant expiration : on renouvelle avant d'etre au bord, sinon un appel
# lance juste avant l'echeance part avec un jeton mort.
TOKEN_MARGIN_S = 300

_TOKEN_LOCK = threading.Lock()
_TOKEN_MEM: dict[str, Any] = {}


def _refresh_token() -> str:
    return _env("TRUSTPILOT_REFRESH_TOKEN")


def _oauth_username() -> str:
    return _env("TRUSTPILOT_USERNAME")


def _oauth_password() -> str:
    return _env("TRUSTPILOT_PASSWORD")


def _oauth_grant() -> str:
    """Le grant qui SERA utilise, ou "" si le prive n'est pas atteignable.

    Sert au diagnostic autant qu'a l'execution : c'est la ligne qui explique
    pourquoi un poste voit le referenceId et un autre non.
    """
    if not _env("TRUSTPILOT_API_KEY") or not _api_secret():
        return ""
    if _refresh_token():
        return "refresh_token"
    if _oauth_username() and _oauth_password():
        return "password"
    return "client_credentials"


def _has_private() -> bool:
    return bool(_oauth_grant())


def _basic_header() -> str:
    raw = f"{_api_key()}:{_api_secret()}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _read_token_store() -> dict[str, Any]:
    path = _token_store_path()
    try:
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_token_store(data: dict[str, Any]) -> None:
    path = _token_store_path()
    if _is_synced(path):
        # Ne leve pas : un jeton non range coute un appel de plus, pas une
        # panne. Mais on ne l'ecrit pas dans un dossier partage.
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _restrict_permissions(path)
    except OSError:
        return


def _token_valid(data: dict[str, Any]) -> bool:
    """Le jeton range est-il encore bon, et pour CETTE application ?

    L'empreinte de la cle est comparee : un poste qui change de cle d'API ne
    doit pas continuer a presenter le jeton de l'ancienne, qui rendrait un 401
    incomprehensible.
    """
    if not data.get("access_token"):
        return False
    if data.get("key_fingerprint") != _fingerprint(_env("TRUSTPILOT_API_KEY")):
        return False
    try:
        expires_at = float(data.get("expires_at") or 0)
    except (TypeError, ValueError):
        return False
    return expires_at - TOKEN_MARGIN_S > time.time()


def _fetch_token() -> dict[str, Any]:
    """Demande un jeton d'acces. Rend le magasin a ranger."""
    grant = _oauth_grant()
    if not grant:
        manquant = "la cle" if not _env("TRUSTPILOT_API_KEY") else "le secret"
        raise ConfigError(
            "Le chemin PRIVE de l'API Trustpilot n'est pas atteignable : il "
            f"demande la cle d'API ET le secret de l'application, et {manquant} "
            "manque.\n\n"
            "Consequence exacte, pour que tu saches ce que tu perds : les avis "
            "restent lisibles par le chemin PUBLIC, mais sans filtre de date "
            "cote serveur, et SANS le referenceId - donc sans lien vers le "
            "numero de commande, donc sans lien vers le transporteur.\n\n"
            "Le secret se trouve dans Trustpilot Business > Integrations > API "
            "applications, a cote de la cle. Il se saisit dans /plugin > "
            "trustpilot > configuration > « Secret d'API Trustpilot », jamais "
            "dans la conversation."
        )

    if grant == "refresh_token":
        url = _base_url() + TOKEN_REFRESH_PATH
        body = {"grant_type": "refresh_token", "refresh_token": _refresh_token()}
    elif grant == "password":
        url = _base_url() + TOKEN_PATH
        body = {
            "grant_type": "password",
            "username": _oauth_username(),
            "password": _oauth_password(),
        }
    else:
        url = _base_url() + TOKEN_PATH
        body = {"grant_type": "client_credentials"}

    headers = {
        "Authorization": _basic_header(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    try:
        resp = _client().post(url, headers=headers, data=body)
    except httpx.HTTPError as exc:
        raise TrustpilotError(
            f"Demande de jeton OAuth impossible ({grant}) : {exc}"
        ) from exc

    if resp.status_code >= 400:
        detail = (resp.text or "")[:400]
        hint = ""
        if grant == "refresh_token" and (
            "invalid_grant" in detail or resp.status_code in (400, 401)
        ):
            hint = (
                "\n\nCAUSE LA PLUS PROBABLE, et elle n'est pas theorique : "
                "Trustpilot fait TOURNER le refresh token a chaque echange. Si "
                "ce jeton est partage avec un autre poste, ou s'il a deja servi "
                "ailleurs, il est perime ici. Deux sorties : saisir un refresh "
                "token frais dans /plugin, ou - mieux - vider ce champ pour "
                "laisser le connecteur utiliser client_credentials, qui ne "
                "tourne pas."
            )
        elif grant == "password":
            hint = (
                "\n\nLe grant `password` est DEPRECIE par Trustpilot et ne "
                "marche pas si le compte a une authentification multifacteur. "
                "Vide TRUSTPILOT_USERNAME / TRUSTPILOT_PASSWORD pour basculer "
                "sur client_credentials."
            )
        else:
            hint = (
                "\n\nVerifie que la cle ET le secret sont ceux de la MEME "
                "application Trustpilot, et que cette application a bien les "
                "droits sur les business units interrogees. Une cle valide avec "
                "un secret d'une autre application rend exactement ce code."
            )
        raise TrustpilotError(
            f"HTTP {resp.status_code} sur la demande de jeton OAuth "
            f"(grant {grant}) | {detail}{hint}"
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        raise TrustpilotError(
            f"Reponse non JSON sur la demande de jeton : {(resp.text or '')[:300]}"
        ) from exc

    token = (payload.get("access_token") or "").strip()
    if not token:
        raise TrustpilotError(
            "La demande de jeton a reussi mais ne contient pas d'access_token : "
            + json.dumps(payload, ensure_ascii=False)[:300]
        )
    try:
        lifetime = float(payload.get("expires_in") or 360000)
    except (TypeError, ValueError):
        lifetime = 360000.0

    store: dict[str, Any] = {
        "access_token": token,
        "expires_at": time.time() + lifetime,
        "obtained_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "grant": grant,
        "key_fingerprint": _fingerprint(_env("TRUSTPILOT_API_KEY")),
    }
    # Le refresh token peut avoir TOURNE : le nouveau est range, sinon le
    # prochain echange presenterait un jeton mort.
    rotated = (payload.get("refresh_token") or "").strip()
    if rotated:
        store["refresh_token"] = rotated
        store["refresh_token_rotated"] = True
    return store


def _bearer() -> str:
    """Le jeton d'acces a presenter, en le renouvelant si besoin."""
    with _TOKEN_LOCK:
        if _token_valid(_TOKEN_MEM):
            return str(_TOKEN_MEM["access_token"])
        stored = _read_token_store()
        if _token_valid(stored):
            _TOKEN_MEM.clear()
            _TOKEN_MEM.update(stored)
            return str(stored["access_token"])
        fresh = _fetch_token()
        _write_token_store(fresh)
        _TOKEN_MEM.clear()
        _TOKEN_MEM.update(fresh)
        return str(fresh["access_token"])


# --------------------------------------------------------------------------
# Couche HTTP
# --------------------------------------------------------------------------

_CLIENT: httpx.Client | None = None
_CLIENT_LOCK = threading.Lock()


def _client() -> httpx.Client:
    """Le client HTTP du processus, partage et garde ouvert.

    Un appel isole rouvrirait la connexion TLS a chaque page. Sur dix-huit
    domaines et soixante pages chacun, ca compte. httpx.Client est sur en usage
    concurrent : c'est ce qui permet le fan-out de _request_many, qui interroge
    les dix-huit domaines de front.
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


def _headers(path: str) -> dict[str, str]:
    """Les en-tetes d'un appel. Le regime depend du CHEMIN, pas d'un reglage."""
    head = {
        "Accept": "application/json",
        "User-Agent": "myrddin-mcp-trustpilot/1.0",
    }
    if _auth_for(path) == "oauth":
        head["Authorization"] = f"Bearer {_bearer()}"
    else:
        head["apikey"] = _api_key()
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


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """Combien attendre avant de rejouer. L'en-tete du serveur fait foi."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if raw:
        try:
            return max(0.0, min(30.0, float(raw)))
        except ValueError:
            pass
    return min(8.0, 0.5 * (2**attempt))


def _request(path: str, query: dict[str, Any] | None = None) -> Any:
    """Un GET sur un chemin de la liste blanche. Seule methode emise.

    Il n'y a pas de parametre `method` a cette fonction, et ce n'est pas un
    oubli : c'est ce qui rend l'ecriture impossible depuis n'importe quel outil
    du serveur, y compris l'echappatoire trustpilot_get. Le seul POST du
    connecteur est la demande de jeton OAuth, dans _fetch_token.
    """
    url = _base_url() + path
    headers = _headers(path)
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
                raise TrustpilotError(f"Appel {path} impossible : {exc}") from exc
            time.sleep(0.5)
            continue
        if resp.status_code in RETRY_STATUSES and attempt < RETRY_ATTEMPTS - 1:
            time.sleep(_retry_after(resp, attempt))
            continue
        break
    if resp is None:
        raise TrustpilotError(f"Appel {path} impossible : {last_error}")

    if resp.status_code == 401:
        if _auth_for(path) == "oauth":
            # Un jeton range peut avoir ete revoque avant son expiration. On le
            # jette pour que la tentative suivante en redemande un, plutot que
            # de boucler sur un 401 avec un jeton mort en cache.
            _TOKEN_MEM.clear()
            raise TrustpilotError(
                "HTTP 401 sur un chemin PRIVE : le jeton OAuth a ete refuse. Il "
                "a peut-etre ete revoque cote Trustpilot, ou la cle a change. Le "
                "jeton en cache vient d'etre jete : relance l'appel. Si ca "
                "persiste, trustpilot_doctor dit quel grant est utilise."
            )
        raise TrustpilotError(
            "HTTP 401 : cle d'API refusee. Verifie TRUSTPILOT_API_KEY - une cle "
            "revoquee rend le meme code. La cle se lit dans Trustpilot Business "
            "> Integrations > API applications."
        )
    if resp.status_code == 403:
        raise TrustpilotError(
            f"HTTP 403 sur {path} : l'application est authentifiee mais n'a pas "
            "acces a cette ressource. Sur un chemin prive, c'est presque "
            "toujours que l'application Trustpilot n'a pas de droit sur CETTE "
            "business unit - le compte a acces a un domaine mais pas aux "
            "dix-huit. trustpilot_business_units dit lesquels repondent."
        )
    if resp.status_code == 404:
        raise TrustpilotError(
            f"HTTP 404 sur {path} : ressource inexistante. Sur un identifiant de "
            "business unit, c'est souvent un domaine mal orthographie - "
            "Trustpilot enregistre la forme AVEC www chez nous."
        )
    if resp.status_code == 429:
        raise TrustpilotError(
            "HTTP 429 : quota d'appels Trustpilot atteint, et il l'etait encore "
            f"apres {RETRY_ATTEMPTS} tentatives espacees. Le quota se compte par "
            "APPLICATION, donc il est partage par toute l'equipe : quelqu'un "
            "d'autre est peut-etre en train de synchroniser. Resserre la "
            "periode, limite-toi a un domaine, ou passe par le cache local "
            "(trustpilot_sync une fois, puis trustpilot_sql sans rappeler l'API)."
        )
    if resp.status_code >= 400:
        body = (resp.text or "")[:600]
        raise TrustpilotError(f"HTTP {resp.status_code} sur {path} | {body}")

    if not resp.content:
        return []
    try:
        return resp.json()
    except ValueError as exc:
        raise TrustpilotError(
            f"Reponse non JSON sur {path} : {(resp.text or '')[:300]}"
        ) from exc


def _rows(payload: Any) -> list[dict[str, Any]]:
    """L'API rend soit une liste, soit un objet {reviews|businessUnits: [...]}."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("reviews", "businessUnits", "results", "data", "items", "tags"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
        return [payload]
    return []


def _request_many(calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """Joue plusieurs GET independants de front. Rend les charges dans l'ordre.

    Sert a interroger les dix-huit domaines en une fois : les enchainer en serie
    multiplierait la latence par dix-huit. Une erreur sur un appel est rendue
    comme VALEUR, pas levee : si l'application n'a pas de droit sur un domaine,
    les dix-sept autres restent utiles - et le domaine muet se voit.
    """
    if not calls:
        return []
    if len(calls) == 1:
        path, query = calls[0]
        try:
            return [_request(path, query)]
        except (ConfigError, TrustpilotError) as exc:
            return [f"ERREUR : {exc}"]

    def one(item: tuple[str, dict[str, Any]]) -> Any:
        path, query = item
        try:
            return _request(path, query)
        except (ConfigError, TrustpilotError) as exc:
            return f"ERREUR : {exc}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(calls))) as pool:
        return list(pool.map(one, calls))


def _paginate(
    path: str,
    query: dict[str, Any] | None = None,
    max_rows: int = 1000,
    max_pages: int = 0,
    stop_before: str = "",
    date_field: str = "createdAt",
) -> tuple[list[dict[str, Any]], str]:
    """Pagine par numero de page. Rend (lignes, raison_d_arret).

    La raison est une chaine VIDE quand la collection a ete lue en entier, et
    sinon dit POURQUOI on s'est arrete. Elle vaut mieux qu'un booleen : les
    arrets n'appellent pas la meme correction, et un rendu tronque n'est pas
    citable comme un total.

    ARRET SUR PAGE PARTIELLE. La pagination Trustpilot est par `page` /
    `perPage`, donc les pages sont des tranches deterministes : une page qui
    rend moins que `perPage` est la derniere. On s'arrete donc la, sans payer
    l'appel de confirmation. C'est une difference assumee avec le script Power
    Query d'origine, qui bouclait jusqu'a la page VIDE et payait donc un appel
    de plus par domaine - dix-huit appels inutiles a chaque execution.

    `stop_before` est la borne basse quand l'API ne sait pas filtrer par date -
    c'est le cas de TOUT le chemin public. On lit alors du plus recent au plus
    ancien (orderBy=createdat.desc) et on ferme des qu'on passe sous la borne.
    Sans ca, une question sur « le mois dernier » lirait la collection entiere.
    """
    page_param, per_page_param = _page_params(path)
    per_page = _per_page()
    cap = max_pages if max_pages > 0 else _max_pages()
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        page_query = dict(query or {})
        page_query[page_param] = page
        page_query[per_page_param] = per_page
        try:
            batch = _rows(_request(path, page_query))
        except TrustpilotError as exc:
            # Le chemin public plafonne la profondeur de pagination et rend un
            # 400 au-dela, au lieu d'une page vide. Ce n'est pas une panne :
            # c'est la fin de ce que l'API veut bien donner, et il faut le DIRE
            # plutot que de faire echouer toute la lecture.
            if "HTTP 400" in str(exc) and page > 1:
                return out, "plafond_api"
            raise
        if not batch:
            return out, ""
        if stop_before:
            garde: list[dict[str, Any]] = []
            fini = False
            for row in batch:
                stamp = str(row.get(date_field) or "")[:10]
                if stamp and stamp < stop_before:
                    fini = True
                    continue
                garde.append(row)
            out.extend(garde)
            if fini:
                return out[:max_rows], ""
        else:
            out.extend(batch)
        if len(out) > max_rows:
            return out[:max_rows], "max_rows"
        if len(batch) < per_page:
            # Page partielle : fin de collection.
            return out, ""
        if page >= cap:
            return out, "max_pages"
        page += 1


# --------------------------------------------------------------------------
# Aplatissement, projection, rendu
# --------------------------------------------------------------------------

def _flatten(row: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Aplatit les objets imbriques en notation pointee.

    Reproduit ce que faisait Table.ExpandRecordColumn dans le script Power
    Query : consumer.displayName, businessUnit.identifyingName,
    companyReply.text. Les listes sont rendues en JSON compact - elles n'ont pas
    de forme de colonne stable.
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


def _strip_personal(
    rows: list[dict[str, Any]], keep: bool = False
) -> list[dict[str, Any]]:
    """Retire les colonnes de donnees personnelles.

    Un avis Trustpilot est public : le pseudo, la note et le texte se citent. Le
    referralEmail, lui, est l'adresse du client que NOUS avons invite : il n'a
    rien a faire dans une conversation, dans un livrable, ni dans un mail a un
    transporteur. Il ne sort qu'en CSV, et seulement sur demande explicite.
    """
    if keep:
        return rows
    return [{k: v for k, v in row.items() if k not in PERSONAL_COLUMNS} for row in rows]


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
        "compte ci-dessus n'est PAS un total : ne le cite pas comme un nombre "
        "d'avis, et ne calcule pas de note moyenne dessus. Pour un compte ou une "
        "note justes sur un large perimetre, utilise trustpilot_summary, qui "
        "agrege ce qu'il a parcouru et dit s'il est alle au bout."
    ),
    "max_pages": (
        "ATTENTION : resultat tronque, le garde-fou TRUSTPILOT_MAX_PAGES a ete "
        "atteint. Le compte ci-dessus n'est PAS un total. Resserre la periode, "
        "limite-toi a un domaine, ou passe par le cache (trustpilot_sync), qui "
        "n'a pas ce plafond."
    ),
    "plafond_api": (
        "ATTENTION : l'API Trustpilot a refuse d'aller plus loin dans la "
        "pagination. C'est une limite du chemin PUBLIC, pas une panne : au-dela "
        "d'une certaine profondeur il ne rend plus de page. Le compte ci-dessus "
        "n'est PAS un total. Le chemin PRIVE n'a pas cette limite et sait "
        "filtrer par date cote serveur : il demande le secret d'API "
        "(trustpilot_setup_status)."
    ),
    "budget": (
        "Lecture arretee sur le budget d'affichage : les avis suivants "
        "n'auraient pas ete affiches de toute facon. Le compte ci-dessus n'est "
        "PAS un total."
    ),
}


def _render_table(
    rows: list[dict[str, Any]],
    header: str,
    truncated: Any = "",
    fields: str = "",
    max_chars: int = RENDER_MAX_CHARS,
) -> str:
    """Rend une collection en CSV point-virgule, borne en taille."""
    flat = _select(_strip_personal([_flatten(r) for r in rows]), fields)
    lines = [header, f"{len(rows)} avis ramene(s)."]
    if truncated:
        lines.append(
            _STOP_MESSAGES.get(
                str(truncated),
                "ATTENTION : resultat tronque. Le compte ci-dessus n'est PAS un "
                "total.",
            )
        )
    else:
        lines.append(
            "Lecture complete sur ce perimetre : la collection a ete parcourue "
            "jusqu'a la derniere page. Ce compte est citable, avec ses filtres."
        )
    if not flat:
        lines.append("(aucun avis)")
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
            f"Rendu limite a {max(0, len(kept) - 1)} avis sur {len(flat)} "
            "ramenes, pour ne pas saturer la conversation. Restreins avec "
            "fields=... ou resserre les filtres. N'exporte en CSV que si "
            "l'utilisateur l'a demande."
        )
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def _render_json(payload: Any, header: str, max_chars: int = RENDER_MAX_CHARS) -> str:
    if isinstance(payload, dict):
        payload = {k: v for k, v in payload.items() if k not in PERSONAL_COLUMNS}
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
        except (ConfigError, TrustpilotError, cache.CacheError) as exc:
            return f"ERREUR : {exc}"

    return wrapper


# --------------------------------------------------------------------------
# Le contexte maison : lexique, business units, themes, transporteurs
# --------------------------------------------------------------------------
#
# Pourquoi ce bloc est dans le SERVEUR et pas dans la skill. Une skill est lue
# une fois, en debut de session, et le modele ne la relit pas avant chaque
# appel : le vocabulaire maison y est une intention, pas une garantie. Ici,
# c'est du code - « les avis italiens » devient toujours la bonne business
# unit, et un verbatim allemand est toujours classe avec les mots allemands.

_LEXIQUE: dict[str, Any] | None = None


def _norm(text: Any) -> str:
    """Minuscules, sans accent, espaces normalises. Les deux cotes d'une
    comparaison passent par la, sinon « Suede » ne trouve jamais « Suède »."""
    raw = str(text or "")
    decomposed = unicodedata.normalize("NFKD", raw)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _lexique() -> dict[str, Any]:
    global _LEXIQUE
    if _LEXIQUE is None:
        try:
            _LEXIQUE = json.loads(LEXIQUE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"Lexique introuvable ou illisible ({LEXIQUE_FILE}) : {exc}. "
                "Il fait partie du serveur et est versionne a cote de server.py. "
                "Sans lui, le connecteur ne sait plus traduire un pays en "
                "business unit ni classer un verbatim."
            ) from exc
    return _LEXIQUE


def _bu_catalogue() -> dict[str, dict[str, Any]]:
    """Le catalogue des dix-huit domaines, sans la cle de commentaire."""
    return {
        k: v
        for k, v in _lexique().get("business_units", {}).items()
        if not k.startswith("_") and isinstance(v, dict)
    }


def _bu_info(domain: str) -> dict[str, Any]:
    return _bu_catalogue().get(domain, {})


def _resolve_domain(terme: str) -> str:
    """Traduit ce que dit un humain en nom de domaine Trustpilot.

    Accepte : le domaine exact, sans www, avec https://, un code pays (« it »),
    un nom de pays (« Italie », « Suede »), un nom d'enseigne (« Habitat »).
    Leve une erreur qui DIT les valeurs possibles plutot que de retomber en
    silence sur la France - un perimetre faux qui a l'air juste est le pire
    resultat possible.
    """
    brut = (terme or "").strip()
    if not brut:
        raise TrustpilotError(
            "Aucun domaine demande. Precise un domaine, un pays ou une "
            "enseigne. trustpilot_lexique('domaines') donne la liste des "
            "dix-huit."
        )
    # Un identifiant de business unit Trustpilot : 24 caracteres hexadecimaux.
    if re.fullmatch(r"[0-9a-fA-F]{24}", brut):
        return brut

    nettoye = re.sub(r"^https?://", "", brut).rstrip("/").split("/")[0]
    catalogue = _bu_catalogue()
    if nettoye in catalogue:
        return nettoye
    if "www." + nettoye in catalogue:
        return "www." + nettoye
    sans_www = nettoye[4:] if nettoye.startswith("www.") else nettoye
    for domain in catalogue:
        if domain == sans_www or domain[4:] == sans_www:
            return domain

    alias = {
        _norm(k): v
        for k, v in _lexique().get("alias_business_units", {}).items()
        if not k.startswith("_")
    }
    cible = alias.get(_norm(brut))
    if cible and cible in catalogue:
        return cible

    # Dernier recours : le pays ou l'enseigne, tels qu'ils sont ecrits dans le
    # catalogue. Deux domaines peuvent partager une enseigne : on refuse plutot
    # que de choisir a la place de l'utilisateur.
    besoin = _norm(brut)
    par_pays = [
        d for d, i in catalogue.items() if _norm(i.get("pays")) == besoin
    ]
    if len(par_pays) == 1:
        return par_pays[0]
    par_libelle = [
        d for d, i in catalogue.items() if _norm(i.get("pays_libelle")) == besoin
    ]
    if len(par_libelle) == 1:
        return par_libelle[0]
    if len(par_pays) > 1 or len(par_libelle) > 1:
        candidats = sorted(set(par_pays + par_libelle))
        raise TrustpilotError(
            f"'{terme}' correspond a plusieurs domaines : "
            + ", ".join(candidats)
            + ". Precise lequel."
        )
    raise TrustpilotError(
        f"Domaine inconnu : '{terme}'.\n"
        "Valeurs acceptees : un domaine du perimetre, un code pays (fr, de, it, "
        "es, uk...), un nom de pays, ou un identifiant de business unit a 24 "
        "caracteres.\n"
        "Les dix-huit domaines : " + ", ".join(sorted(_bu_catalogue())) + "\n"
        "trustpilot_lexique('domaines') donne les alias reconnus."
    )


def _resolve_domains(domaine: str) -> list[str]:
    """Un, plusieurs (separes par des virgules), ou TOUS si vide.

    Le defaut « tous » est volontairement dangereux a l'usage - dix-huit
    domaines coutent dix-huit paginations. Les outils qui l'acceptent exigent
    donc une periode, et le disent dans leur reponse.
    """
    brut = (domaine or "").strip()
    if not brut or _norm(brut) in ("tous", "tout", "all", "*"):
        return [d for d in _domains() if d in _bu_catalogue()] or list(_bu_catalogue())
    return [_resolve_domain(part) for part in re.split(r"[,;]", brut) if part.strip()]


# --- Les identifiants de business unit, resolus une fois puis ranges -------
#
# Un identifiant Trustpilot ne change pas. Le resoudre a chaque session est un
# appel par domaine, soit dix-huit appels pour rien, sur un quota partage par
# toute l'equipe. Ils sont donc ranges dans la racine locale.

_BU_MEM: dict[str, str] = {}
_BU_LOCK = threading.Lock()


def _read_bu_store() -> dict[str, str]:
    try:
        path = _bu_store_path()
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: str(v) for k, v in data.items() if isinstance(k, str)}
    except (OSError, ValueError):
        return {}


def _write_bu_store(data: dict[str, str]) -> None:
    try:
        path = _bu_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        return


def _bu_id(domaine: str, refresh: bool = False) -> str:
    """L'identifiant Trustpilot d'un domaine. Resolu par /business-units/find."""
    domain = _resolve_domain(domaine)
    if re.fullmatch(r"[0-9a-fA-F]{24}", domain):
        return domain
    with _BU_LOCK:
        if not refresh:
            if domain in _BU_MEM:
                return _BU_MEM[domain]
            store = _read_bu_store()
            if store.get(domain):
                _BU_MEM[domain] = store[domain]
                return store[domain]
        payload = _request("/v1/business-units/find", {"name": domain})
        bu_id = ""
        if isinstance(payload, dict):
            bu_id = str(payload.get("id") or "")
        if not bu_id:
            raise TrustpilotError(
                f"Trustpilot ne rend pas d'identifiant pour '{domain}'. "
                "Verifie l'orthographe du domaine : Trustpilot enregistre la "
                "forme AVEC www chez nous. La reponse recue : "
                + json.dumps(payload, ensure_ascii=False, default=str)[:300]
            )
        _BU_MEM[domain] = bu_id
        store = _read_bu_store()
        store[domain] = bu_id
        _write_bu_store(store)
        return bu_id


# --- Classement thematique des commentaires -------------------------------
#
# CE QUE C'EST : une detection de mots, par langue de l'avis, compilee une fois
# en expressions regulieres a frontieres de mot.
#
# CE QUE CE N'EST PAS, et il faut le dire a chaque restitution : une
# comprehension. Le lexique ignore la negation - « aucun retard » tombe dans le
# theme delai -, l'ironie, et tout theme exprime sans les mots attendus. Il
# sert a ORIENTER une lecture, pas a produire un chiffre qu'on cite seul.
# C'est pour cela que chaque outil qui l'utilise rend son taux de non-classes,
# et que trustpilot_verbatims existe : le classement trie, l'humain lit.
#
# La polarite, elle, ne vient JAMAIS du texte : elle vient de la note, qui est
# une donnee.

_THEME_RE: dict[str, dict[str, re.Pattern[str]]] | None = None


def _themes_def() -> dict[str, dict[str, Any]]:
    return {
        k: v
        for k, v in _lexique().get("themes", {}).items()
        if not k.startswith("_") and isinstance(v, dict)
    }


def _theme_patterns() -> dict[str, dict[str, re.Pattern[str]]]:
    """{theme: {langue: regex}}, compile une fois.

    La frontiere de mot n'est pas cosmetique : sans elle, « vis » (la visserie)
    se declencherait dans « vis-a-vis », « television », « service ».
    """
    global _THEME_RE
    if _THEME_RE is None:
        built: dict[str, dict[str, re.Pattern[str]]] = {}
        for theme, spec in _themes_def().items():
            par_langue: dict[str, re.Pattern[str]] = {}
            for langue, mots in (spec.get("mots") or {}).items():
                termes = [_norm(m) for m in mots if str(m).strip()]
                if not termes:
                    continue
                # Les termes longs d'abord : « pas de reponse » avant « reponse ».
                termes.sort(key=len, reverse=True)
                motif = _motif_termes(termes)
                if not motif:
                    continue
                par_langue[langue.lower()] = re.compile(motif)
            if par_langue:
                built[theme] = par_langue
        _THEME_RE = built
    return _THEME_RE


# Longueur a partir de laquelle un terme tolere une terminaison.
FLEXION_MIN = 6
FLEXION_MAX = 3


def _motif_termes(termes: list[str]) -> str:
    """Compile une liste de termes en une regex, avec tolerance de FLEXION.

    Pourquoi la tolerance existe. Le polonais et les langues nordiques
    flechissent lourdement : le lexique porte « opoznienie », l'avis dit
    « opoznieniem », et une frontiere de mot stricte ne trouve rien. Mesure a
    l'ecriture de ce connecteur : c'est le theme delai du polonais qui tombait
    entierement.

    Pourquoi elle est BORNEE, et pourquoi elle ne vaut qu'au-dela de six
    caracteres. « cher » tolerant trois lettres matcherait « chercher », et
    « prix » matcherait « prixxx » : les termes courts restent donc stricts. Un
    terme de six caracteres ou plus est un radical assez discriminant pour
    qu'une terminaison de trois lettres reste le meme mot - « retard » ->
    « retards », « beschadigt » -> « beschadigte ».

    Les termes courts et les termes longs vivent dans la MEME expression, sinon
    il faudrait evaluer deux regex par theme et par langue.
    """
    longs = [t for t in termes if len(t) >= FLEXION_MIN]
    courts = [t for t in termes if len(t) < FLEXION_MIN]
    morceaux: list[str] = []
    if longs:
        morceaux.append(
            "(?:" + "|".join(re.escape(t) for t in longs) + ")"
            + f"[a-z]{{0,{FLEXION_MAX}}}"
        )
    if courts:
        morceaux.append("(?:" + "|".join(re.escape(t) for t in courts) + ")")
    if not morceaux:
        return ""
    return r"(?<![a-z0-9])(?:" + "|".join(morceaux) + r")(?![a-z0-9])"


def _langue_key(langue: Any) -> str:
    """Normalise un code langue. Trustpilot code le norvegien 'nb' OU 'no'."""
    code = _norm(langue)[:2]
    if code == "no":
        return "nb"
    return code


def _classify(text: str, langue: Any = "") -> list[str]:
    """Les themes detectes dans un texte. Liste, eventuellement vide.

    Le jeu de mots applique est celui de la LANGUE de l'avis. C'est ce qui evite
    les faux positifs entre langues proches - « retour » vaut en francais et en
    neerlandais, mais « caro » (cher, espagnol) ne doit pas se declencher sur un
    avis italien. Quand la langue est absente ou inconnue, on retombe sur toutes
    les langues et le resultat est plus bruyant : c'est assume, et c'est mieux
    que de ne rien classer.
    """
    plat = _norm(text)
    if not plat:
        return []
    code = _langue_key(langue)
    out: list[str] = []
    for theme, par_langue in _theme_patterns().items():
        motifs = []
        if code and code in par_langue:
            motifs.append(par_langue[code])
        elif not code or code not in par_langue:
            motifs.extend(par_langue.values())
        if "*" in par_langue:
            motifs.append(par_langue["*"])
        if any(m.search(plat) for m in motifs):
            out.append(theme)
    return out


# --- Detection des transporteurs cites ------------------------------------

_CARRIER_RE: list[tuple[str, list[str], re.Pattern[str]]] | None = None


def _carriers_def() -> dict[str, dict[str, Any]]:
    return {
        k: v
        for k, v in _lexique().get("transporteurs", {}).items()
        if not k.startswith("_") and isinstance(v, dict)
    }


def _carrier_patterns() -> list[tuple[str, list[str], re.Pattern[str]]]:
    global _CARRIER_RE
    if _CARRIER_RE is None:
        built: list[tuple[str, list[str], re.Pattern[str]]] = []
        for code, spec in _carriers_def().items():
            alias = [_norm(a) for a in (spec.get("alias") or []) if str(a).strip()]
            if not alias:
                continue
            alias.sort(key=len, reverse=True)
            motif = "|".join(re.escape(a) for a in alias)
            built.append(
                (
                    code,
                    [str(p).upper() for p in (spec.get("pays") or [])],
                    re.compile(r"(?<![a-z0-9])(?:" + motif + r")(?![a-z0-9])"),
                )
            )
        _CARRIER_RE = built
    return _CARRIER_RE


def _carriers_in(text: str, pays: str = "") -> list[str]:
    """Les transporteurs cites dans un texte, restreints au pays de l'avis.

    LA RESTRICTION PAR PAYS EST LE COEUR DE LA FONCTION, pas un raffinement.
    Trois raisons, toutes mesurables sur nos donnees :

      - « Rhenus » designe QUATRE entites. Sur un avis italien c'est Rhenus
        Italie, sur un avis polonais Rhenus Pologne. Sans le pays, la mention
        est inexploitable.
      - « Bring » et « Posten » sont deux transporteurs distincts du meme
        groupe : Bring livre la Suede, Posten Bring la Norvege. Les confondre
        melange deux pays et deux baremes.
      - « posten » veut aussi dire « la poste » en norvegien courant, et
        « ader » est un mot allemand. Restreindre a l'Espagne pour ADER et a la
        Norvege pour Posten supprime ces faux positifs.

    Quand le pays est inconnu - c'est le cas de habitat-design.com, qui sert six
    locales -, on ne restreint pas, et la mention est donc moins fiable.
    """
    plat = _norm(text)
    if not plat:
        return []
    cible = (pays or "").upper()
    out: list[str] = []
    for code, pays_du_transporteur, motif in _carrier_patterns():
        if cible and cible not in ("MULTI", "") and pays_du_transporteur:
            if cible not in pays_du_transporteur:
                continue
        if motif.search(plat):
            out.append(code)
    return out


# --------------------------------------------------------------------------
# Lecture des avis : les deux chemins, et leur fusion
# --------------------------------------------------------------------------
#
# La fusion est reprise du script Power Query d'origine : on lit le prive, on
# lit le public, et on retire du public les identifiants deja vus en prive. La
# raison d'etre de ce montage : le prive porte le referenceId et les bornes de
# date, le public porte parfois des avis que le prive ne rend pas (avis
# organiques anciens). Une colonne `review_source` dit d'ou vient chaque ligne -
# c'est ce que faisait la colonne ReviewSource du script.

PUBLIC_PATH = "/v1/business-units/{}/reviews"
PRIVATE_PATH = "/v1/private/business-units/{}/reviews"


def _iso_date(valeur: str, borne: str) -> str:
    """Valide une date de filtre. Rend "" si vide, leve si illisible.

    On ne devine pas une date. Un « 01/03/2026 » interprete a l'envers decale
    un perimetre de neuf mois sans que rien ne le signale.
    """
    brut = (valeur or "").strip()
    if not brut:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}", brut):
        # Un mois seul : borne basse au 1er, borne haute au dernier jour.
        annee, mois = (int(x) for x in brut.split("-"))
        if borne == "fin":
            fin = dt.date(annee + (mois // 12), (mois % 12) + 1, 1) - dt.timedelta(days=1)
            return fin.isoformat()
        return dt.date(annee, mois, 1).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", brut):
        return brut
    raise TrustpilotError(
        f"Date illisible : '{valeur}'. Attendu AAAA-MM-JJ, ou AAAA-MM pour un "
        "mois entier. Le connecteur ne devine pas un format de date : un "
        "01/03/2026 lu a l'envers decale le perimetre de neuf mois sans erreur."
    )


def _enrich(
    flat: dict[str, Any], domaine: str, route: str, avec_analyse: bool = True
) -> dict[str, Any]:
    """Ajoute les colonnes maison a un avis aplati.

    Ce sont elles qui rendent le connecteur utilisable par l'equipe : le
    domaine et le pays (que l'API ne rend pas sous cette forme), le fait qu'il y
    ait une reponse et en combien de temps, l'ecart entre l'experience vecue et
    la publication, les themes, et les transporteurs cites.
    """
    info = _bu_info(domaine)
    out = dict(flat)
    out["domaine"] = domaine
    out["pays"] = info.get("pays", "")
    out["enseigne"] = info.get("enseigne", "")
    out["review_source"] = route

    stamp = str(flat.get("createdAt") or "")
    out["annee_mois"] = stamp[:7]
    out["date"] = stamp[:10]

    try:
        out["stars"] = int(flat.get("stars") or 0) or ""
    except (TypeError, ValueError):
        out["stars"] = ""

    titre = str(flat.get("title") or "")
    texte = str(flat.get("text") or "")
    out["titre"] = titre
    out["texte_court"] = re.sub(r"\s+", " ", texte)[:200]

    reponse = str(flat.get("companyReply.text") or "").strip()
    out["a_reponse"] = "oui" if reponse else "non"
    out["delai_reponse_h"] = _heures_entre(
        stamp, str(flat.get("companyReply.createdAt") or "")
    )
    out["delai_experience_j"] = _jours_entre(
        str(flat.get("experiencedAt") or ""), stamp
    )

    if avec_analyse:
        themes = _classify(f"{titre} {texte}", flat.get("language"))
        out["themes"] = ";".join(themes)
        out["nb_themes"] = len(themes)
        transporteurs = _carriers_in(f"{titre} {texte}", out["pays"])
        out["transporteurs_cites"] = ";".join(transporteurs)
    return out


def _parse_stamp(valeur: str) -> dt.datetime | None:
    brut = (valeur or "").strip()
    if not brut:
        return None
    brut = brut.replace("Z", "+00:00")
    try:
        stamp = dt.datetime.fromisoformat(brut)
    except ValueError:
        try:
            stamp = dt.datetime.fromisoformat(brut[:19])
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp


def _heures_entre(debut: str, fin: str) -> Any:
    a, b = _parse_stamp(debut), _parse_stamp(fin)
    if a is None or b is None:
        return ""
    return round((b - a).total_seconds() / 3600, 1)


def _jours_entre(debut: str, fin: str) -> Any:
    a, b = _parse_stamp(debut), _parse_stamp(fin)
    if a is None or b is None:
        return ""
    return round((b - a).total_seconds() / 86400, 1)


def _fetch_reviews(
    domaine: str,
    date_from: str = "",
    date_to: str = "",
    stars: str = "",
    langue: str = "",
    repondu: str = "",
    source: str = "",
    mode: str = "auto",
    max_rows: int = 2000,
    max_pages: int = 0,
    avec_analyse: bool = True,
) -> tuple[list[dict[str, Any]], str, str]:
    """Lit les avis d'UN domaine. Rend (avis enrichis, arret, route utilisee).

    `mode` :
      auto    - prive si le secret est la, public sinon. Le defaut.
      prive   - force le prive. Echoue clairement si le secret manque.
      public  - force le public, meme quand le prive est disponible.
      fusion  - les deux, avec deduplication sur l'identifiant d'avis, comme le
                script Power Query. Coute deux paginations : sur demande.
    """
    mode = _norm(mode) or "auto"
    debut = _iso_date(date_from, "debut")
    fin = _iso_date(date_to, "fin")
    bu = _bu_id(domaine)

    filtres: dict[str, Any] = {}
    if stars:
        filtres["stars"] = stars
    if langue:
        filtres["language"] = _langue_key(langue) if len(str(langue)) <= 3 else langue
    if repondu:
        filtres["responded"] = _norm(repondu) in ("oui", "true", "1", "yes")
    if source:
        filtres["source"] = source

    def lire_prive() -> tuple[list[dict[str, Any]], str]:
        query = dict(filtres)
        query["orderBy"] = "createdat.desc"
        if debut:
            query["startDateTime"] = debut
        if fin:
            query["endDateTime"] = fin
        return _paginate(
            PRIVATE_PATH.format(bu),
            query,
            max_rows=max_rows,
            max_pages=max_pages,
        )

    def lire_public() -> tuple[list[dict[str, Any]], str]:
        query = dict(filtres)
        # Le public ignore `source` : le parametre n'existe pas sur ce chemin,
        # et l'envoyer quand meme rend un 400.
        query.pop("source", None)
        query["orderBy"] = "createdat.desc"
        rows, arret = _paginate(
            PUBLIC_PATH.format(bu),
            query,
            max_rows=max_rows,
            max_pages=max_pages,
            # Pas de filtre de date cote serveur : on lit du plus recent au
            # plus ancien et on ferme des qu'on passe sous la borne basse.
            stop_before=debut[:10] if debut else "",
        )
        if fin:
            limite = fin[:10]
            rows = [r for r in rows if str(r.get("createdAt") or "")[:10] <= limite]
        return rows, arret

    if mode == "prive" or (mode == "auto" and _has_private()):
        rows, arret = lire_prive()
        route = "prive"
    elif mode in ("public", "auto"):
        rows, arret = lire_public()
        route = "public"
    elif mode == "fusion":
        prives, arret_p = lire_prive()
        publics, arret_u = lire_public()
        vus = {str(r.get("id")) for r in prives}
        # La deduplication du script Power Query : on retire du public ce que le
        # prive porte deja, et on garde la version PRIVEE, qui est plus riche.
        rows = prives + [r for r in publics if str(r.get("id")) not in vus]
        arret = arret_p or arret_u
        route = "fusion"
        plats = [_flatten(r) for r in rows]
        enrichis = []
        for i, plat in enumerate(plats):
            d_route = "prive" if i < len(prives) else "public"
            enrichis.append(_enrich(plat, domaine, d_route, avec_analyse))
        return enrichis, arret, route
    else:
        raise TrustpilotError(
            f"mode inconnu : '{mode}'. Valeurs : auto, prive, public, fusion."
        )

    enrichis = [_enrich(_flatten(r), domaine, route, avec_analyse) for r in rows]
    return enrichis, arret, route


def _fetch_many(
    domaines: list[str],
    date_from: str = "",
    date_to: str = "",
    stars: str = "",
    langue: str = "",
    repondu: str = "",
    source: str = "",
    mode: str = "auto",
    max_rows_par_domaine: int = 2000,
    max_pages: int = 0,
    avec_analyse: bool = True,
) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    """Lit plusieurs domaines de front. Rend (avis, avertissements, routes).

    Une erreur sur un domaine n'arrete pas les autres : elle devient un
    avertissement. Un domaine sur lequel l'application n'a pas de droit ne doit
    pas empecher de repondre sur les dix-sept autres - mais il doit se VOIR,
    sinon un total silencieusement partiel part en revue.
    """
    avertissements: list[str] = []
    routes: dict[str, str] = {}
    tous: list[dict[str, Any]] = []

    def une(domaine: str):
        return _fetch_reviews(
            domaine,
            date_from=date_from,
            date_to=date_to,
            stars=stars,
            langue=langue,
            repondu=repondu,
            source=source,
            mode=mode,
            max_rows=max_rows_par_domaine,
            max_pages=max_pages,
            avec_analyse=avec_analyse,
        )

    if len(domaines) == 1:
        resultats = [(domaines[0], _sur_ou_erreur(une, domaines[0]))]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(6, len(domaines))
        ) as pool:
            futurs = {pool.submit(_sur_ou_erreur, une, d): d for d in domaines}
            resultats = [(futurs[f], f.result()) for f in futurs]

    for domaine, issue in resultats:
        if isinstance(issue, str):
            avertissements.append(f"{domaine} : {issue}")
            continue
        rows, arret, route = issue
        routes[domaine] = route
        if arret:
            avertissements.append(
                f"{domaine} : lecture INCOMPLETE ({arret}) - le compte de ce "
                "domaine n'est pas un total."
            )
        tous.extend(rows)
    return tous, avertissements, routes


def _sur_ou_erreur(fn, domaine: str):
    try:
        return fn(domaine)
    except (ConfigError, TrustpilotError) as exc:
        return str(exc)


# --------------------------------------------------------------------------
# Agregation : compter, moyenner, repartir - cote serveur
# --------------------------------------------------------------------------
#
# Pourquoi ca existe. L'API Trustpilot ne rend AUCUN total et ne sait pas
# agreger : la seule facon de repondre a « combien d'avis a une etoile en
# Italie ce trimestre » est de paginer. Faire remonter ces avis dans la
# conversation pour les compter a la main coute une fenetre entiere et donne un
# compte fragile. On pagine donc, on agrege ici, et on ne rend que le resultat -
# avec le nombre d'avis parcourus et le fait que le parcours soit alle au bout.

GROUP_KEYS = {
    "mois": "annee_mois",
    "date": "date",
    "domaine": "domaine",
    "pays": "pays",
    "enseigne": "enseigne",
    "etoiles": "stars",
    "langue": "language",
    "reponse": "a_reponse",
    "source": "source",
    "verifie": "isVerified",
    "route": "review_source",
}


def _moyenne(valeurs: list[float]) -> float | str:
    return round(sum(valeurs) / len(valeurs), 2) if valeurs else ""


def _mediane(valeurs: list[float]) -> float | str:
    if not valeurs:
        return ""
    ordonne = sorted(valeurs)
    milieu = len(ordonne) // 2
    if len(ordonne) % 2:
        return round(ordonne[milieu], 2)
    return round((ordonne[milieu - 1] + ordonne[milieu]) / 2, 2)


def _stats_bloc(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Les mesures d'un groupe d'avis. Toutes citables, toutes definies ici.

    `note_moyenne` est la moyenne des notes des avis PARCOURUS. Ce n'est pas le
    TrustScore : celui-la est pondere et glissant, calcule par Trustpilot, et il
    se lit dans trustpilot_profile. Les deux ne coincident pas, et les confondre
    en revue est une erreur qui se voit.
    """
    notes = [float(r["stars"]) for r in rows if str(r.get("stars") or "").isdigit()]
    bas = sum(1 for n in notes if n <= 2)
    haut = sum(1 for n in notes if n >= 4)
    repondus = sum(1 for r in rows if r.get("a_reponse") == "oui")
    delais = [
        float(r["delai_reponse_h"])
        for r in rows
        if isinstance(r.get("delai_reponse_h"), (int, float))
    ]
    return {
        "nb_avis": len(rows),
        "note_moyenne": _moyenne(notes),
        "nb_1_2_etoiles": bas,
        "pct_1_2_etoiles": round(100 * bas / len(notes), 1) if notes else "",
        "nb_4_5_etoiles": haut,
        "pct_4_5_etoiles": round(100 * haut / len(notes), 1) if notes else "",
        "taux_reponse_pct": round(100 * repondus / len(rows), 1) if rows else "",
        "delai_reponse_median_h": _mediane(delais),
    }


def _aggregate(rows: list[dict[str, Any]], group_by: str) -> list[dict[str, Any]]:
    """Regroupe et mesure. group_by='' rend une seule ligne, tout confondu."""
    cle = _norm(group_by)
    if not cle or cle in ("aucun", "total", "rien"):
        ligne = {"groupe": "(tout le perimetre)"}
        ligne.update(_stats_bloc(rows))
        return [ligne]

    if cle in ("theme", "themes"):
        return _aggregate_multi(rows, "themes")
    if cle in ("transporteur", "transporteurs"):
        return _aggregate_multi(rows, "transporteurs_cites")

    colonne = GROUP_KEYS.get(cle)
    if colonne is None:
        raise TrustpilotError(
            f"group_by inconnu : '{group_by}'. Valeurs : "
            + ", ".join(sorted(list(GROUP_KEYS) + ["theme", "transporteur", "aucun"]))
        )
    paquets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        valeur = row.get(colonne)
        clef = "(vide)" if valeur in (None, "") else str(valeur)
        paquets.setdefault(clef, []).append(row)
    sortie: list[dict[str, Any]] = []
    for clef in sorted(paquets):
        ligne = {"groupe": clef}
        ligne.update(_stats_bloc(paquets[clef]))
        sortie.append(ligne)
    return sortie


def _aggregate_multi(rows: list[dict[str, Any]], colonne: str) -> list[dict[str, Any]]:
    """Regroupe sur une colonne MULTIVALUEE (themes, transporteurs cites).

    Un avis peut porter trois themes : il compte donc dans trois groupes. La
    somme des groupes DEPASSE le nombre d'avis, et ce n'est pas une erreur -
    c'est ecrit dans la reponse de l'outil, parce que quelqu'un finira par
    additionner la colonne.
    """
    paquets: dict[str, list[dict[str, Any]]] = {}
    sans = 0
    for row in rows:
        valeurs = [v for v in str(row.get(colonne) or "").split(";") if v]
        if not valeurs:
            sans += 1
            continue
        for valeur in valeurs:
            paquets.setdefault(valeur, []).append(row)
    libelles = {k: (v.get("libelle") or k) for k, v in _themes_def().items()}
    sortie: list[dict[str, Any]] = []
    for clef in sorted(paquets, key=lambda k: -len(paquets[k])):
        ligne = {"groupe": clef, "libelle": libelles.get(clef, "")}
        ligne.update(_stats_bloc(paquets[clef]))
        ligne["pct_des_avis"] = (
            round(100 * len(paquets[clef]) / len(rows), 1) if rows else ""
        )
        sortie.append(ligne)
    if sans:
        ligne = {"groupe": "(non classe)", "libelle": "aucun mot du lexique detecte"}
        ligne.update(_stats_bloc([r for r in rows if not str(r.get(colonne) or "")]))
        ligne["pct_des_avis"] = round(100 * sans / len(rows), 1) if rows else ""
        sortie.append(ligne)
    return sortie


# --------------------------------------------------------------------------
# Le serveur MCP
# --------------------------------------------------------------------------

SERVER_INSTRUCTIONS = """Connecteur en LECTURE SEULE sur les avis Trustpilot des dix-huit domaines du
groupe (Vente-unique et ses quatorze pays, Kauf-unique, Habitat).

Aucune ecriture n'est possible : ni reponse a un avis, ni tag, ni invitation.
Ces chemins ne sont pas exposes du tout - la liste blanche ne porte que des GET.

Commence par trustpilot_lexique : il porte les pieges qui rendent un chiffre
faux sans lever d'erreur - le TrustScore pondere qui n'est pas la note moyenne
de la periode, le nombre d'avis qui ne mesure pas un volume, les themes
detectes par MOTS et qui ignorent la negation, le norvegien code nb ou no.

Des que la question commence par "combien" ou "quelle note", c'est
trustpilot_summary, qui agrege cote serveur. "De quoi se plaignent-ils" est
trustpilot_themes d'abord, trustpilot_verbatims ensuite - jamais l'inverse.

Le referenceId d'un avis est notre numero de commande, et il n'existe que par
le chemin PRIVE, qui demande le secret d'API. C'est la jointure vers Reflex et
Shiptify, donc vers le transporteur reel : un transporteur CITE dans un
verbatim n'est qu'un indice.

Au-dela de quelques milliers d'avis, passe par trustpilot_sync puis
trustpilot_sql : le direct est plafonne par domaine, et le quota Trustpilot est
compte par application, donc partage par toute l'equipe.

N'exporte en CSV que si l'utilisateur a demande un export.
"""

# Les annotations de comportement de la specification MCP. Elles sont l'ecriture
# formelle de ce que ce connecteur promet cote Trustpilot : AUCUN outil n'a de
# chemin d'ecriture vers l'API - pas de reponse a un avis, pas de tag, pas
# d'invitation.
#
# Ce ne sont que des indications : la specification demande explicitement aux
# clients de ne pas leur faire confiance. Ce qui GARANTIT la lecture seule reste
# le code - la liste blanche api_paths.json, qui ne porte que des GET, et
# _request, qui n'emet que cette methode.
#
# `readOnlyHint` est pris au sens de la specification - l'outil ne modifie pas
# son environnement - et il est donc a FAUX sur les cinq outils qui ecrivent sur
# le disque du POSTE : le magasin de secrets, le cache SQLite et les deux
# exports. Aucun d'eux n'ecrit chez Trustpilot. Annoncer l'inverse ferait
# approuver sans regard un export de verbatims clients.
#
# `openWorldHint` distingue ce qui interroge Trustpilot - dont le contenu bouge
# a chaque avis publie - de ce qui lit le lexique embarque ou le cache local,
# qui repondent hors ligne et de facon reproductible.


def _hints(
    titre: str,
    monde_ouvert: bool,
    lecture_seule: bool = True,
    idempotent: bool = True,
    destructif: bool = False,
) -> ToolAnnotations:
    return ToolAnnotations(
        title=titre,
        readOnlyHint=lecture_seule,
        destructiveHint=destructif,
        idempotentHint=idempotent,
        openWorldHint=monde_ouvert,
    )


HORS_LIGNE = False   # lexique embarque, cache local, configuration du poste
SUR_L_API = True     # interroge Trustpilot

mcp = FastMCP("trustpilot", instructions=SERVER_INSTRUCTIONS)


# --- Mise en service : la cle et le secret -------------------------------
#
# La saisie se fait dans l'interface de Claude Code, par les champs userConfig
# declares dans plugin.json (marques `sensitive`). Claude Code les collecte
# lui-meme et les passe au serveur par l'environnement : ils ne traversent
# jamais la conversation, donc n'entrent ni dans le contexte du modele, ni dans
# la transcription.
#
# C'est pour cette raison qu'aucun outil ici n'accepte un secret en parametre.
# Un `trustpilot_set_api_key("...")` serait plus direct a expliquer, mais il
# ferait passer le secret par le fil de la conversation - exactement ce que le
# champ `sensitive` existe pour eviter. `trustpilot_save_key` ne prend donc
# aucun argument : il range sur la machine ce qui est deja saisi dans
# l'interface.


@mcp.tool(annotations=_hints("Ou en est la mise en service", HORS_LIGNE))
@_guard
def trustpilot_setup_status() -> str:
    """Ou en est la mise en service : cle, secret, et ce que ca ouvre ou ferme.

    A appeler en premier apres l'installation du plugin, et chaque fois qu'un
    outil repond que la cle manque. Ne rend jamais un secret en clair.
    """
    _load_env_file()
    cle = _env("TRUSTPILOT_API_KEY")
    secret = _api_secret()
    ranges = _stored_secrets()
    magasin = _key_store_path()
    plugin_mode = bool(os.environ.get("CLAUDE_PLUGIN_ROOT"))
    grant = _oauth_grant()

    lines = ["Mise en service du connecteur Trustpilot", ""]
    lines.append(f"Cle d'API            : {_mask(cle) if cle else 'AUCUNE'}")
    lines.append(f"Secret d'API         : {_mask(secret) if secret else 'AUCUN'}")
    lines.append(f"Origine de la cle    : {_key_source()}")
    lines.append(f"Magasin sur machine  : {magasin}")
    lines.append(
        "Secrets ranges       : "
        + (", ".join(sorted(ranges)) if ranges else "aucun")
    )
    lines.extend(_shared_report())
    lines.append("")
    lines.append("CE QUE LA CONFIGURATION ACTUELLE OUVRE")
    lines.append("")
    if not cle:
        lines.append(
            "  RIEN. Sans cle d'API, aucun appel n'est possible, meme public."
        )
    elif not grant:
        lines.append("  Chemin PUBLIC seulement (cle presente, secret absent).")
        lines.append("    - les avis, les notes, les profils : OUI")
        lines.append("    - filtrer par periode COTE SERVEUR : NON. Le connecteur")
        lines.append("      lit du plus recent au plus ancien et s'arrete a la borne,")
        lines.append("      ce qui coute des appels et bute sur le plafond de")
        lines.append("      pagination du public sur les longues periodes.")
        lines.append("    - le referenceId, donc le lien vers le numero de commande,")
        lines.append("      donc vers le transporteur : NON.")
        lines.append("    - retrouver l'avis d'une commande precise : NON.")
        lines.append("")
        lines.append(
            "  C'est utilisable, et c'est meme suffisant pour suivre une note. "
            "Mais le lien avis / commande / transporteur - la raison d'etre de "
            "ce connecteur pour l'equipe Transport - demande le secret."
        )
    else:
        lines.append(f"  Chemins PUBLIC et PRIVE (grant OAuth : {grant}).")
        lines.append("    - filtres de date cote serveur : OUI")
        lines.append("    - referenceId et lien vers la commande : OUI")
        lines.append("    - recherche par numero de commande : OUI")
        if grant == "refresh_token":
            lines.append("")
            lines.append(
                "  ATTENTION au grant retenu. Un refresh token est pose sur ce "
                "poste, donc il passe devant client_credentials. Trustpilot "
                "fait TOURNER ce jeton a chaque echange : s'il vient d'ailleurs "
                "ou s'il est partage, il cassera - ici ou chez l'autre. Vider ce "
                "champ dans /plugin fait basculer sur client_credentials, qui ne "
                "tourne pas. C'est le reglage recommande."
            )
        elif grant == "password":
            lines.append("")
            lines.append(
                "  ATTENTION : le grant `password` est DEPRECIE par Trustpilot "
                "et ne marche pas avec une authentification multifacteur. Vider "
                "TRUSTPILOT_USERNAME / TRUSTPILOT_PASSWORD fait basculer sur "
                "client_credentials."
            )

    lines.append("")
    if not cle:
        lines.append("CE QU'IL FAUT FAIRE")
        lines.append("")
        lines.append(
            "Le cas normal est la valeur d'equipe : commence par la ligne "
            "« Config d'equipe » ci-dessus. Si elle dit « aucune », c'est que "
            "08_ENGINE/04_mcp/00_config n'est pas atteignable depuis ce poste "
            "(bibliotheque non synchronisee, ou posee ailleurs). Synchronise-la, "
            "ou pointe-la avec VU_ENGINE_DIR ou TRUSTPILOT_SHARED_ENV."
        )
        lines.append("")
        lines.append("A DEFAUT - saisir dans l'interface de Claude Code :")
        lines.append("")
        if plugin_mode:
            lines.append("  1. tape /plugin")
            lines.append("  2. choisis « trustpilot » (marketplace vu-transport)")
            lines.append(
                "  3. ouvre sa configuration, renseigne « Cle d'API Trustpilot » "
                "et, si tu l'as, « Secret d'API Trustpilot »"
            )
            lines.append("  4. redemarre la session pour que le serveur les reprenne")
        else:
            lines.append(
                "  Ce serveur ne tourne PAS comme plugin : il n'y a donc pas de "
                "champ de configuration. Installe le plugin, ou pose les valeurs "
                f"dans {magasin} (modele : .env.example)."
            )
        lines.append("")
        lines.append(
            "Les deux valeurs se lisent dans Trustpilot Business > Integrations "
            "> API applications. La cle y est appelee « API Key » ou "
            "« Client ID », le secret « API Secret »."
        )
        lines.append("")
        lines.append(
            "Ne colle ni l'une ni l'autre dans la conversation : les champs de "
            "configuration les gardent hors du contexte du modele et hors de la "
            "transcription. Si tu l'as deja fait, fais-les regenerer cote "
            "Trustpilot - les anciennes sont a considerer comme divulguees."
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

    depuis_equipe = bool(
        _load_shared_env().get("TRUSTPILOT_API_KEY")
        and not os.environ.get("TRUSTPILOT_API_KEY")
    )
    if depuis_equipe:
        lines.append(
            "La cle active est celle de l'equipe, lue dans 08_ENGINE : il n'y a "
            "RIEN a saisir sur ce poste. Verifie la connexion avec "
            "trustpilot_doctor et c'est fini."
        )
        lines.append("")
        lines.append(
            "  trustpilot_save_key n'est utile que pour rendre ce poste "
            "autonome de la bibliotheque synchronisee : il recopie en local les "
            "valeurs actives."
        )
    elif not ranges:
        lines.append(
            "Les valeurs sont saisies mais ne sont PAS rangees sur la machine. "
            "Appelle trustpilot_save_key : elles survivront alors a une "
            "reinstallation du plugin, et une installation directe les "
            "retrouvera au meme endroit."
        )
    else:
        lines.append("Tout est en place. Verifie la connexion avec trustpilot_doctor.")
    if cle and not secret:
        lines.append("")
        lines.append(
            "Il te manque le SECRET pour ouvrir le chemin prive - relis ce que "
            "ca ferme, plus haut. Meme endroit que la cle dans Trustpilot "
            "Business, meme champ dans /plugin."
        )
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Ranger les secrets sur cette machine", HORS_LIGNE, lecture_seule=False))
@_guard
def trustpilot_save_key() -> str:
    """Range sur la machine les secrets deja saisis dans l'interface.

    Ne prend aucun argument, **volontairement** : les secrets viennent de la
    configuration du plugin, pas de la conversation. Ils n'ont donc pas a etre
    recopies dans un message pour etre ranges.

    Le fichier est ecrit hors de tout dossier synchronise, et ses droits sont
    restreints a l'utilisateur courant.
    """
    a_ranger: dict[str, str] = {"TRUSTPILOT_API_KEY": _api_key()}
    secret = _api_secret()
    if secret:
        a_ranger["TRUSTPILOT_API_SECRET"] = secret
    jeton = _refresh_token()
    if jeton:
        a_ranger["TRUSTPILOT_REFRESH_TOKEN"] = jeton
    path, droits = _write_key_store(a_ranger)
    lignes = [f"Secrets ranges sur la machine : {path}", ""]
    for cle in sorted(a_ranger):
        lignes.append(f"  {cle} : empreinte {_fingerprint(a_ranger[cle])}")
    lignes.extend(
        [
            "",
            "(l'empreinte est les 12 premiers caracteres du SHA-256 : elle "
            "permet de comparer deux valeurs sans en afficher aucune)",
            f"Droits : {droits}",
            "",
            "Ce fichier n'est pas synchronise et n'est pas versionne. Il sera "
            "relu automatiquement au prochain demarrage du serveur, meme si la "
            "configuration du plugin est perdue.",
            "",
            "Pour l'effacer : trustpilot_forget_key.",
        ]
    )
    return "\n".join(lignes)


@mcp.tool(annotations=_hints("Oublier les secrets ranges sur cette machine", HORS_LIGNE, lecture_seule=False, destructif=True))
@_guard
def trustpilot_forget_key() -> str:
    """Supprime les secrets ranges sur la machine, et le jeton OAuth en cache.

    Ne touche pas a la configuration du plugin : si les valeurs y sont saisies,
    elles continueront d'etre utilisees. Pour les retirer completement, vide
    aussi les champs dans /plugin.
    """
    path = _key_store_path()
    notes: list[str] = []
    try:
        if not path.is_file():
            notes.append(f"Aucun secret range a supprimer ({path} n'existe pas).")
        else:
            motif = re.compile(
                r"\s*(TRUSTPILOT_API_KEY|TRUSTPILOT_API_SECRET"
                r"|TRUSTPILOT_REFRESH_TOKEN)\s*="
            )
            lignes = [
                line
                for line in path.read_text(encoding="utf-8-sig").splitlines()
                if not motif.match(line)
            ]
            if any(l.strip() and not l.strip().startswith("#") for l in lignes):
                path.write_text("\n".join(lignes) + "\n", encoding="utf-8")
                notes.append(
                    f"Lignes de secret retirees de {path} (le reste du fichier "
                    "est conserve)."
                )
            else:
                path.unlink()
                notes.append(f"Fichier supprime : {path}")
    except OSError as exc:
        raise ConfigError(f"Suppression impossible dans {path} : {exc}") from exc

    jeton = _token_store_path()
    try:
        if jeton.is_file():
            jeton.unlink()
            notes.append(f"Jeton OAuth en cache supprime : {jeton}")
    except OSError as exc:
        notes.append(f"Jeton OAuth non supprime ({jeton}) : {exc}")
    _TOKEN_MEM.clear()

    reste = "oui" if _env("TRUSTPILOT_API_KEY") else "non"
    notes.extend(
        [
            "",
            f"Une cle reste active pour cette session : {reste}. Elle vient "
            "alors de la configuration du plugin ou du fichier d'equipe, qui ne "
            "sont pas touches ici.",
        ]
    )
    return "\n".join(notes)


@mcp.tool(annotations=_hints("Diagnostic : configuration lue et vrais appels", SUR_L_API))
def trustpilot_doctor() -> str:
    """Diagnostic : configuration lue, secrets presents, et vrais appels a l'API.

    Le premier outil a appeler quand quelque chose coince. Il verifie les DEUX
    regimes separement - un poste peut tres bien avoir un public qui repond et
    un prive qui echoue, et c'est justement le cas qu'il faut savoir nommer.
    Ne rend jamais un secret en clair.
    """
    _load_env_file()
    lines = ["Configuration du serveur MCP trustpilot", ""]
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    lines.append(
        "Mode                     : "
        + (f"plugin ({plugin_root})" if plugin_root else "installation directe")
    )
    lines.append(f"Interpreteur             : {sys.executable}")
    lines.append(f"Racine locale            : {_local_root()}")
    lines.append(f"Origine de la cle        : {_key_source()}")
    lines.append(f"Fichier .env lu          : {_ENV_LOADED_FROM or '(aucun)'}")
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
            "  Un fichier pose sous %LOCALAPPDATA% peut etre INVISIBLE pour ce "
            "processus (virtualisation du conteneur d'application) : il est "
            "alors rapporte 'absent' alors qu'il existe. C'est pour cela que la "
            "racine locale de ce connecteur est sous le profil utilisateur."
        )
    lines.append("")
    lines.extend(_shared_report())
    lines.append("")
    lines.append(f"TRUSTPILOT_BASE_URL      : {_base_url()}")
    lines.append(f"TRUSTPILOT_API_KEY       : {_mask(_env('TRUSTPILOT_API_KEY'))}")
    lines.append(f"TRUSTPILOT_API_SECRET    : {_mask(_api_secret())}")
    lines.append(
        f"Grant OAuth retenu       : {_oauth_grant() or '(aucun - chemin public seulement)'}"
    )
    jeton = _read_token_store()
    if jeton:
        lines.append(
            f"Jeton en cache           : obtenu le {jeton.get('obtained_at')} "
            f"par {jeton.get('grant')}, "
            + ("encore valide" if _token_valid(jeton) else "PERIME")
        )
        if jeton.get("refresh_token_rotated"):
            lines.append(
                "  NOTE : Trustpilot a fait TOURNER le refresh token lors du "
                "dernier echange. Le nouveau est range en local ; celui qui est "
                "eventuellement saisi dans /plugin est donc perime. C'est normal, "
                "et c'est pourquoi ce jeton ne se partage pas entre postes."
            )
    else:
        lines.append("Jeton en cache           : aucun")
    lines.append(f"TRUSTPILOT_START_DATE    : {_start_date()}")
    lines.append(f"TRUSTPILOT_PER_PAGE      : {_per_page()}")
    lines.append(f"TRUSTPILOT_MAX_PAGES     : {_max_pages()}")
    lines.append(f"TRUSTPILOT_TIMEOUT_S     : {_timeout()}")
    lines.append(f"Domaines du perimetre    : {len(_domains())}")
    lines.append(f"Dossier d'export         : {_export_dir()}")
    lines.append(f"Cache local              : {_cache_path()}")
    try:
        spec = _spec()
        publics = sum(
            1 for p in spec["paths"].values() if p.get("auth") != "oauth"
        )
        prives = len(spec["paths"]) - publics
        lines.append(
            f"Chemins GET connus       : {len(spec['paths'])} "
            f"({publics} publics, {prives} prives) - releve {spec.get('released')}"
        )
    except ConfigError as exc:
        lines.append(f"Chemins GET connus       : ERREUR - {exc}")
    try:
        lines.append(
            f"Lexique                  : {len(_themes_def())} themes, "
            f"{len(_carriers_def())} transporteurs, "
            f"{len(_bu_catalogue())} business units"
        )
    except ConfigError as exc:
        lines.append(f"Lexique                  : ERREUR - {exc}")

    lines.append("")
    lines.append("Appels de verification")
    if not _env("TRUSTPILOT_API_KEY"):
        lines.append("  Aucune cle : appels non tentes.")
        return "\n".join(lines)

    temoin = _domains()[0] if _domains() else "www.vente-unique.com"
    bu = ""
    try:
        payload = _request("/v1/business-units/find", {"name": temoin})
        bu = str(payload.get("id") or "") if isinstance(payload, dict) else ""
        note = ""
        if isinstance(payload, dict):
            score = payload.get("score") or {}
            note = f", TrustScore {score.get('trustScore')}, {payload.get('numberOfReviews')} avis"
        lines.append(f"  PUBLIC  find {temoin} : OK | id {bu}{note}")
    except (ConfigError, TrustpilotError) as exc:
        lines.append(f"  PUBLIC  find {temoin} : ECHEC | {exc}")

    if bu:
        try:
            rows = _rows(
                _request(PUBLIC_PATH.format(bu), {"page": 1, "perPage": 1})
            )
            lines.append(
                f"  PUBLIC  avis         : OK | {len(rows)} avis lu(s) en un appel"
            )
        except (ConfigError, TrustpilotError) as exc:
            lines.append(f"  PUBLIC  avis         : ECHEC | {exc}")

    if not _oauth_grant():
        lines.append(
            "  PRIVE                : non tente, le secret d'API n'est pas "
            "renseigne. Le connecteur fonctionne, mais sans filtre de date cote "
            "serveur et sans referenceId. Voir trustpilot_setup_status."
        )
        return "\n".join(lines)
    if bu:
        try:
            rows = _rows(
                _request(PRIVATE_PATH.format(bu), {"page": 1, "perPage": 1})
            )
            avec_ref = sum(1 for r in rows if r.get("referenceId"))
            lines.append(
                f"  PRIVE   avis         : OK | {len(rows)} avis lu(s), "
                f"{avec_ref} avec referenceId"
            )
        except (ConfigError, TrustpilotError) as exc:
            lines.append(f"  PRIVE   avis         : ECHEC | {exc}")
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Les chemins GET atteignables", HORS_LIGNE))
@_guard
def trustpilot_list_paths(contains: str = "") -> str:
    """Liste les chemins GET atteignables, leur regime d'auth et leurs parametres.

    A appeler avant trustpilot_get, pour ne pas deviner un chemin ou un nom de
    filtre. `contains` filtre sur le chemin ou le resume.
    """
    spec = _spec()
    besoin = contains.strip().lower()
    lines = [
        f"Chemins GET de l'API Trustpilot connus du connecteur "
        f"({len(spec['paths'])} au total, releve {spec.get('released')})",
        f"Source : {spec.get('source')}",
        "",
        "AUCUNE ECRITURE N'EST EXPOSEE. Ce que l'API sait faire et que ce "
        "serveur ne fera pas :",
    ]
    for ligne in spec.get("ecritures_volontairement_absentes", []):
        lines.append(f"  - {ligne}")
    lines.append("")
    montres = 0
    for path in sorted(spec["paths"]):
        info = spec["paths"][path]
        resume = info.get("summary", "")
        if besoin and besoin not in path.lower() and besoin not in resume.lower():
            continue
        montres += 1
        regime = "PRIVE (OAuth)" if info.get("auth") == "oauth" else "public (apikey)"
        lines.append(f"[{regime}] {path}")
        if resume:
            lines.append(f"    {resume}")
        for param in info.get("parameters", []):
            bits = [f"{param['in']} {param['name']}", str(param.get("type") or "?")]
            if param.get("required"):
                bits.append("REQUIS")
            if param.get("enum"):
                bits.append("valeurs " + ", ".join(str(v) for v in param["enum"]))
            ligne = "      " + " | ".join(bits)
            desc = param.get("description") or ""
            if desc:
                ligne += f" | {desc}"
            lines.append(ligne)
        lines.append("")
    if not montres:
        lines.append(f"(aucun chemin ne correspond a '{contains}')")
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Le contexte maison : domaines, themes, transporteurs, pieges", HORS_LIGNE))
@_guard
def trustpilot_lexique(sujet: str = "") -> str:
    """Le contexte maison : domaines, pays, transporteurs, themes, pieges.

    A lire avant de filtrer sur un nom maison, et avant de commenter un chiffre.
    sujet : 'domaines', 'themes', 'transporteurs', 'champs', 'pieges',
    'questions', 'usages'. Vide = le sommaire.
    """
    lex = _lexique()
    besoin = _norm(sujet)
    lines: list[str] = []

    def bloc(titre: str, contenu: Any, niveau: int = 0) -> None:
        marge = "  " * niveau
        if isinstance(contenu, dict):
            lines.append(f"{marge}{titre}")
            for cle, val in contenu.items():
                if cle.startswith("_"):
                    if isinstance(val, list):
                        for item in val:
                            lines.append(f"{marge}  # {item}")
                    else:
                        lines.append(f"{marge}  # {val}")
                    continue
                bloc(str(cle), val, niveau + 1)
        elif isinstance(contenu, list):
            lines.append(f"{marge}{titre}")
            for item in contenu:
                if isinstance(item, dict):
                    lines.append(
                        marge
                        + "  - "
                        + " | ".join(f"{k}: {v}" for k, v in item.items())
                    )
                else:
                    lines.append(f"{marge}  - {item}")
        else:
            lines.append(f"{marge}{titre} : {contenu}")

    sections = {
        "usages": ("A quoi sert ce connecteur", "a_quoi_sert_ce_connecteur"),
        "domaines": ("Les business units", "business_units"),
        "alias": ("Les alias reconnus", "alias_business_units"),
        "themes": ("Les themes de classement", "themes"),
        "transporteurs": ("Les transporteurs", "transporteurs"),
        "champs": ("Les champs a connaitre", "champs_a_connaitre"),
        "pieges": ("Les pieges", "pieges"),
        "questions": ("Questions frequentes", "questions_frequentes"),
    }

    if besoin in sections:
        titre, cle = sections[besoin]
        contenu = lex.get(cle, {})
        if besoin == "themes":
            # Les listes de mots par langue pesent des milliers de tokens et
            # n'aident pas a decider : on rend la definition, pas le lexique.
            allege = {}
            for nom, spec in _themes_def().items():
                allege[nom] = {
                    "libelle": spec.get("libelle", ""),
                    "quoi": spec.get("quoi", ""),
                    "langues": ", ".join(sorted((spec.get("mots") or {}).keys())),
                    "nb_mots": sum(len(v) for v in (spec.get("mots") or {}).values()),
                }
            contenu = {"_pourquoi": lex["themes"].get("_pourquoi", ""), **allege}
        bloc(titre, contenu)
        return "\n".join(lines)

    lines.append("Lexique Trustpilot de l'equipe Logistique & Transport")
    lines.append(f"version {lex.get('version')} - maj {lex.get('updated')} par "
                 f"{lex.get('updated_by')}")
    lines.append("")
    for item in lex.get("_lisez_moi", []):
        lines.append(f"  {item}")
    lines.append("")
    lines.append("Sujets, a demander en argument :")
    for besoin_cle, (titre, cle) in sections.items():
        contenu = lex.get(cle) or {}
        taille = len([k for k in contenu if not str(k).startswith("_")]) if isinstance(
            contenu, (dict, list)
        ) else 0
        lines.append(f"  {besoin_cle:<15} {titre} ({taille} entrees)")
    lines.append("")
    lines.append("Les trois choses a savoir sans avoir a demander :")
    lines.append(
        "  1. Le nombre d'avis n'est PAS un volume d'expedition. Comparer deux "
        "pays sur le nombre d'avis ne compare rien."
    )
    lines.append(
        "  2. Le classement par theme est une detection de MOTS, pas une "
        "comprehension. Il ignore la negation. Toujours lire son taux de "
        "non-classes avant de citer une repartition."
    )
    lines.append(
        "  3. Sur LU, IE, DK, un mois peut ne compter que quelques avis : une "
        "note ne se commente jamais sans son effectif."
    )
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Les business units du perimetre", SUR_L_API))
@_guard
def trustpilot_business_units(domaine: str = "", refresh: bool = False) -> str:
    """Les business units du perimetre : identifiant, TrustScore, nombre d'avis.

    C'est le point d'entree du connecteur et le premier appel a faire sur un
    poste neuf : il dit quels domaines repondent, et il range les identifiants
    en local pour ne plus les redemander.

    domaine : vide = les dix-huit domaines du perimetre, interroges de front.
    refresh : force la re-resolution des identifiants (a n'utiliser que si un
    domaine a change de business unit chez Trustpilot).

    Le TrustScore rendu ici est celui que Trustpilot CALCULE - pondere, glissant.
    Ce n'est pas la moyenne des notes de la periode, que rend trustpilot_summary.
    Les deux ne coincident pas : ne pas les melanger dans un meme tableau.
    """
    domaines = _resolve_domains(domaine)
    if refresh:
        _BU_MEM.clear()
    charges = _request_many(
        [("/v1/business-units/find", {"name": d}) for d in domaines]
    )

    rows: list[dict[str, Any]] = []
    erreurs: list[str] = []
    store = _read_bu_store()
    for domain, charge in zip(domaines, charges):
        info = _bu_info(domain)
        if isinstance(charge, str):
            erreurs.append(f"{domain} : {charge}")
            continue
        if not isinstance(charge, dict):
            erreurs.append(f"{domain} : reponse inattendue")
            continue
        score = charge.get("score") or {}
        nb = charge.get("numberOfReviews")
        total = nb.get("total") if isinstance(nb, dict) else nb
        bu_id = str(charge.get("id") or "")
        if bu_id:
            store[domain] = bu_id
        rows.append(
            {
                "domaine": domain,
                "pays": info.get("pays", ""),
                "enseigne": info.get("enseigne", ""),
                "business_unit_id": bu_id,
                "displayName": charge.get("displayName", ""),
                "trustScore": score.get("trustScore", ""),
                "etoiles": score.get("stars", ""),
                "nb_avis_total": total,
                "statut": charge.get("status", ""),
            }
        )
    _write_bu_store(store)

    lines = [
        f"Business units Trustpilot du perimetre ({len(rows)} sur "
        f"{len(domaines)} interroges)",
        "",
        "Le TrustScore ci-dessous est celui CALCULE par Trustpilot : pondere et "
        "glissant. Ce n'est pas la moyenne des notes d'une periode - pour "
        "celle-la, c'est trustpilot_summary. Ne pas presenter les deux comme le "
        "meme chiffre.",
        "",
    ]
    if rows:
        lines.append(_to_csv_text(rows))
    else:
        lines.append("(aucune business unit n'a repondu)")
    if erreurs:
        lines.append("")
        lines.append("DOMAINES QUI N'ONT PAS REPONDU - a ne pas oublier dans un total :")
        for erreur in erreurs:
            lines.append(f"  - {erreur}")
    lines.append("")
    lines.append(f"Identifiants ranges dans {_bu_store_path()} : plus besoin de les "
                 "redemander a chaque session.")
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Profil d'une business unit : TrustScore et repartition", SUR_L_API))
@_guard
def trustpilot_profile(domaine: str) -> str:
    """Le profil d'une business unit : TrustScore et repartition par etoile.

    C'est la source de la « note Trustpilot » suivie chaque semaine par le
    service client. La repartition par etoile rendue ici porte sur la VIE
    ENTIERE de la business unit, pas sur une periode : pour une periode, c'est
    trustpilot_summary.
    """
    domain = _resolve_domain(domaine)
    bu = _bu_id(domain)
    charges = _request_many(
        [
            (f"/v1/business-units/{bu}/profileinfo", {}),
            (f"/v1/business-units/{bu}", {}),
        ]
    )
    info = _bu_info(domain)
    lines = [
        f"Profil Trustpilot - {domain} ({info.get('pays', '?')}, "
        f"{info.get('enseigne', '?')})",
        f"business unit : {bu}",
        "",
    ]
    if info.get("note"):
        lines.append(f"A savoir sur ce domaine : {info['note']}")
        lines.append("")
    for titre, charge in zip(("profileinfo", "business unit"), charges):
        if isinstance(charge, str):
            lines.append(f"{titre} : {charge}")
            continue
        lines.append(f"--- {titre} ---")
        lines.append(json.dumps(charge, ensure_ascii=False, indent=2, default=str))
        lines.append("")
    lines.append(
        "Rappel : la repartition par etoile ci-dessus est cumulee depuis "
        "l'ouverture de la business unit. Une degradation recente s'y voit a "
        "peine - c'est trustpilot_summary sur une periode qui la montre."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Lire les avis
# --------------------------------------------------------------------------

DEFAUT_JOURS = 90


def _periode(date_from: str, date_to: str, domaines: list[str]) -> tuple[str, str, str]:
    """Applique une periode par defaut, et DIT laquelle. Rend (debut, fin, note).

    Pourquoi un defaut plutot qu'une erreur : « de quoi se plaignent les clients
    espagnols » est une question legitime qui ne porte pas de date. Pourquoi le
    dire a chaque fois : parce qu'une reponse sur quatre-vingt-dix jours
    presentee comme « les avis espagnols » sera relue dans trois mois comme un
    chiffre de toujours.
    """
    if date_from:
        return date_from, date_to, ""
    debut = (dt.date.today() - dt.timedelta(days=DEFAUT_JOURS)).isoformat()
    note = (
        f"PERIODE PAR DEFAUT : aucune date n'a ete demandee, le perimetre porte "
        f"donc sur les {DEFAUT_JOURS} derniers jours (depuis le {debut}). "
        "A citer avec le resultat. Pour un autre perimetre, passe date_from et "
        f"date_to. L'historique complet du connecteur demarre au {_start_date()}."
    )
    if len(domaines) > 6:
        note += (
            f" Et {len(domaines)} domaines sont interroges : c'est long, et le "
            "resultat melange des pays qui n'ont pas les memes transporteurs."
        )
    return debut, date_to, note


def _entete_lecture(
    domaines: list[str],
    debut: str,
    fin: str,
    routes: dict[str, str],
    filtres: dict[str, Any],
    nb: int,
) -> list[str]:
    """L'entete que tout outil de lecture rend. Elle porte de quoi verifier.

    Un chiffre sans son perimetre n'est pas citable : ces quatre lignes sont ce
    qui permet a quelqu'un de recoller le resultat trois semaines plus tard.
    """
    # Les bornes sont sorties de la f-string : imbriquer une apostrophe dans une
    # expression de f-string ne se compile qu'a partir de Python 3.12, et un
    # collegue peut avoir 3.10. Un serveur qui ne demarre pas ne se diagnostique
    # pas depuis la conversation.
    borne_basse = debut or "(origine)"
    borne_haute = fin or "aujourd'hui"
    lignes = [
        f"Perimetre : {', '.join(domaines)}"
        + (f" ({len(domaines)} domaines)" if len(domaines) > 1 else ""),
        f"Periode   : du {borne_basse} au {borne_haute} "
        "(sur createdAt, la date de PUBLICATION de l'avis)",
    ]
    actifs = {k: v for k, v in filtres.items() if v not in ("", None)}
    if actifs:
        lignes.append(
            "Filtres   : "
            + ", ".join(f"{k}={v}" for k, v in sorted(actifs.items()))
        )
    if routes:
        vues = sorted(set(routes.values()))
        lignes.append(
            f"Chemin    : {', '.join(vues)}"
            + (
                "  (le chemin PUBLIC ne filtre pas les dates cote serveur et "
                "plafonne en profondeur : sur une longue periode, le resultat "
                "peut etre partiel)"
                if "public" in vues
                else ""
            )
        )
    lignes.append(f"Avis lus  : {nb}")
    return lignes


@mcp.tool(annotations=_hints("Lister les avis d'une periode", SUR_L_API))
@_guard
def trustpilot_list_reviews(
    domaine: str = "",
    date_from: str = "",
    date_to: str = "",
    stars: str = "",
    langue: str = "",
    repondu: str = "",
    theme: str = "",
    transporteur: str = "",
    mode: str = "auto",
    fields: str = "",
    max_rows: int = 200,
) -> str:
    """Liste les avis d'un ou plusieurs domaines, sur une periode.

    A utiliser quand la question porte sur DES LIGNES : « montre-moi les avis a
    une etoile de la semaine en Italie ». Si la question est « combien », c'est
    trustpilot_summary ; si elle est « de quoi se plaignent-ils », c'est
    trustpilot_themes puis trustpilot_verbatims.

    domaine       : un domaine, un pays, une enseigne ; plusieurs separes par des
                    virgules ; vide = les dix-huit du perimetre.
    date_from/to  : AAAA-MM-JJ, ou AAAA-MM pour un mois entier.
    stars         : 1 a 5, une seule valeur.
    langue        : fr, de, nl, it, es, pt, pl, da, sv, nb, en.
    repondu       : 'oui' ou 'non' - filtre sur la presence d'une reponse.
    theme         : filtre APRES lecture, sur le classement lexical.
    transporteur  : filtre APRES lecture, sur les transporteurs cites.
    mode          : auto, prive, public, fusion.
    fields        : projection ; vide = la projection courte, '*' = tout.

    Les colonnes maison (domaine, pays, themes, transporteurs_cites, a_reponse,
    delai_reponse_h) sont calculees par le connecteur, elles ne viennent pas de
    l'API.
    """
    domaines = _resolve_domains(domaine)
    debut, fin, note = _periode(date_from, date_to, domaines)
    rows, avertissements, routes = _fetch_many(
        domaines,
        date_from=debut,
        date_to=fin,
        stars=stars,
        langue=langue,
        repondu=repondu,
        mode=mode,
        max_rows_par_domaine=max(max_rows, 100),
    )
    filtres = {
        "stars": stars,
        "langue": langue,
        "repondu": repondu,
        "theme": theme,
        "transporteur": transporteur,
    }
    lus = len(rows)
    if theme:
        rows = [r for r in rows if _norm(theme) in _norm(r.get("themes"))]
    if transporteur:
        besoin = _norm(transporteur).replace(" ", "_")
        rows = [r for r in rows if besoin in _norm(r.get("transporteurs_cites"))]

    rows.sort(key=lambda r: str(r.get("createdAt") or ""), reverse=True)
    tronque = "max_rows" if len(rows) > max_rows else ""
    rows = rows[:max_rows]

    entete = _entete_lecture(domaines, debut, fin, routes, filtres, lus)
    if note:
        entete.insert(0, note)
        entete.insert(1, "")
    if theme or transporteur:
        entete.append(
            f"Apres filtre theme/transporteur : {len(rows)} avis retenus sur "
            f"{lus} lus. Ce filtre est LEXICAL : il ne voit que ce qui est "
            "ecrit avec les mots du lexique."
        )
    if avertissements:
        entete.append("")
        entete.append("AVERTISSEMENTS :")
        entete.extend(f"  - {a}" for a in avertissements)
    return _render_table(
        rows, "\n".join(entete), tronque, fields or REVIEW_BRIEF
    )


@mcp.tool(annotations=_hints("La fiche complete d'un avis", SUR_L_API))
@_guard
def trustpilot_get_review(review_id: str, locale: str = "fr-FR") -> str:
    """La fiche complete d'un avis, ses tags et son lien public.

    Passe par le chemin PRIVE si le secret est disponible - c'est le seul a
    rendre le referenceId, donc le numero de commande. Sinon la fiche publique,
    en le disant.

    Le lien public rendu ici est celui a mettre dans un ticket ou dans un mail
    au transporteur : il ouvre l'avis tel que les clients le voient.
    """
    ident = (review_id or "").strip()
    if not ident:
        raise TrustpilotError("review_id manquant.")

    prive = _has_private()
    appels: list[tuple[str, dict[str, Any]]] = []
    if prive:
        appels.append((f"/v1/private/reviews/{ident}", {}))
        appels.append((f"/v1/private/reviews/{ident}/tags", {}))
    else:
        appels.append((f"/v1/reviews/{ident}", {}))
    appels.append((f"/v1/reviews/{ident}/web-links", {"locale": locale}))
    charges = _request_many(appels)

    lignes = [
        f"Avis {ident}",
        f"Chemin : {'PRIVE' if prive else 'PUBLIC'}"
        + (
            ""
            if prive
            else "  (le referenceId - notre numero de commande - n'est PAS "
            "disponible par le chemin public : voir trustpilot_setup_status)"
        ),
        "",
    ]
    fiche = charges[0]
    if isinstance(fiche, dict):
        plat = _flatten(fiche)
        domaine = str(plat.get("businessUnit.identifyingName") or "")
        enrichi = _enrich(plat, domaine if domaine in _bu_catalogue() else "", "prive" if prive else "public")
        resume = {
            "note": enrichi.get("stars"),
            "publie_le": enrichi.get("createdAt"),
            "vecu_le": enrichi.get("experiencedAt"),
            "ecart_experience_publication_j": enrichi.get("delai_experience_j"),
            "langue": enrichi.get("language"),
            "pays_business_unit": enrichi.get("pays"),
            "domaine": domaine,
            "titre": enrichi.get("title"),
            "auteur_affiche": plat.get("consumer.displayName"),
            "reponse_entreprise": enrichi.get("a_reponse"),
            "delai_de_reponse_h": enrichi.get("delai_reponse_h"),
            "source": plat.get("source"),
            "verifie": plat.get("isVerified"),
            "compte_dans_le_trustscore": plat.get("countsTowardsTrustScore"),
            "referenceId_numero_de_commande": plat.get("referenceId") or "(absent)",
            "themes_detectes": enrichi.get("themes") or "(aucun mot du lexique)",
            "transporteurs_cites": enrichi.get("transporteurs_cites") or "(aucun)",
        }
        lignes.append("--- L'essentiel ---")
        for cle, val in resume.items():
            lignes.append(f"  {cle:<32} {val}")
        lignes.append("")
        lignes.append("--- Le texte, tel qu'il est ecrit ---")
        lignes.append(str(fiche.get("text") or "(aucun texte)"))
        reponse = (fiche.get("companyReply") or {})
        if isinstance(reponse, dict) and reponse.get("text"):
            lignes.append("")
            lignes.append("--- La reponse de l'entreprise ---")
            lignes.append(str(reponse.get("text")))
        if plat.get("referenceId"):
            lignes.append("")
            lignes.append(
                f"SUITE A DONNER : le referenceId est {plat['referenceId']}. "
                "C'est le numero de commande : porte-le dans Reflex ou Shiptify "
                "pour trouver l'expedition et le transporteur reel. C'est la "
                "seule facon fiable de relier un avis a un transporteur - un "
                "nom cite dans le texte est un indice, pas une preuve."
            )
    else:
        lignes.append(f"Fiche : {fiche}")

    if prive and len(charges) > 2:
        tags = charges[1]
        lignes.append("")
        lignes.append(
            "--- Tags poses sur l'avis (lecture seule) ---"
        )
        lignes.append(json.dumps(tags, ensure_ascii=False, default=str)[:1500])

    liens = charges[-1]
    lignes.append("")
    lignes.append("--- Lien public ---")
    if isinstance(liens, dict):
        lignes.append(str(liens.get("reviewUrl") or liens))
    else:
        lignes.append(str(liens))
    return "\n".join(lignes)


@mcp.tool(annotations=_hints("Retrouver l'avis d'une commande (chemin prive)", SUR_L_API))
@_guard
def trustpilot_find_review(
    reference_id: str = "",
    email: str = "",
    domaine: str = "",
    date_from: str = "",
    date_to: str = "",
) -> str:
    """Retrouve l'avis d'une COMMANDE, ou d'un client invite. Chemin prive.

    C'est le pont entre un avis et le reste du SI, dans les deux sens :
      - « la commande 1234567 a-t-elle donne un avis, et lequel »
      - un avis a une etoile -> son referenceId -> Reflex ou Shiptify -> le
        transporteur qui a livre.

    reference_id : notre numero de commande, tel qu'il a ete envoye a
                   l'invitation Trustpilot.
    email        : l'adresse du client invite. Le connecteur s'en sert pour
                   filtrer, mais ne la RESTITUE jamais.

    Demande le secret d'API : le chemin public ne connait ni l'un ni l'autre.
    """
    reference = (reference_id or "").strip()
    courriel = (email or "").strip()
    if not reference and not courriel:
        raise TrustpilotError(
            "Precise reference_id (le numero de commande) ou email."
        )
    if not _has_private():
        raise ConfigError(
            "Cette recherche passe par le chemin PRIVE, qui demande la cle ET "
            "le secret d'API - le chemin public ne porte ni le referenceId ni "
            "le referralEmail.\n\n"
            "Sans le secret, il n'y a pas de contournement : aucun filtre "
            "public ne permet de retrouver l'avis d'une commande. Voir "
            "trustpilot_setup_status pour la marche a suivre."
        )

    domaines = _resolve_domains(domaine)
    filtres: dict[str, Any] = {}
    if reference:
        filtres["referenceId"] = reference
    if courriel:
        filtres["referralEmail"] = courriel
    debut = _iso_date(date_from, "debut")
    fin = _iso_date(date_to, "fin")

    appels: list[tuple[str, dict[str, Any]]] = []
    for domain in domaines:
        query = dict(filtres)
        query["perPage"] = PAGE_LIMIT
        query["page"] = 1
        if debut:
            query["startDateTime"] = debut
        if fin:
            query["endDateTime"] = fin
        appels.append((PRIVATE_PATH.format(_bu_id(domain)), query))
    charges = _request_many(appels)

    trouves: list[dict[str, Any]] = []
    erreurs: list[str] = []
    for domain, charge in zip(domaines, charges):
        if isinstance(charge, str):
            erreurs.append(f"{domain} : {charge}")
            continue
        for brut in _rows(charge):
            trouves.append(_enrich(_flatten(brut), domain, "prive"))

    lignes = [
        "Recherche par "
        + (f"referenceId={reference}" if reference else "adresse du client invite")
        + f" sur {len(domaines)} domaine(s)",
        "",
    ]
    if not trouves:
        lignes.append(
            "Aucun avis ne correspond. Trois causes, dans l'ordre de "
            "probabilite :"
        )
        lignes.append(
            "  1. le client n'a pas laisse d'avis - c'est le cas le plus "
            "frequent, le taux de retour sur invitation est de quelques "
            "pourcents ;"
        )
        lignes.append(
            "  2. le referenceId n'est pas celui envoye a l'invitation. Verifie "
            "le format exact cote back-office : un prefixe ou un zero de tete "
            "en moins ne rend rien, sans erreur ;"
        )
        lignes.append(
            "  3. l'avis est sur un autre domaine que ceux interroges. Relance "
            "sans preciser domaine pour balayer les dix-huit."
        )
    else:
        lignes.append(f"{len(trouves)} avis trouve(s).")
        lignes.append("")
        lignes.append(
            _to_csv_text(
                _strip_personal(
                    _select(
                        trouves,
                        "id,domaine,pays,stars,createdAt,experiencedAt,titre,"
                        "texte_court,a_reponse,themes,transporteurs_cites,"
                        "referenceId",
                    )
                )
            )
        )
        lignes.append("")
        lignes.append(
            "Pour le texte complet et le lien public d'un de ces avis : "
            "trustpilot_get_review avec son id."
        )
    if erreurs:
        lignes.append("")
        lignes.append("Domaines qui n'ont pas repondu :")
        lignes.extend(f"  - {e}" for e in erreurs)
    return "\n".join(lignes)


# --------------------------------------------------------------------------
# Interpreter les commentaires
# --------------------------------------------------------------------------
#
# Trois outils, et ils repondent a trois questions differentes. C'est l'ordre
# qui compte :
#
#   1. trustpilot_summary  - « combien, et quelle note » : la mesure.
#   2. trustpilot_themes   - « de quoi ca parle » : le tri, cote serveur.
#   3. trustpilot_verbatims - « qu'est-ce qu'ils disent » : la lecture.
#
# Le serveur ne pretend jamais comprendre un verbatim. Il compte, il classe par
# mots, il selectionne - et il passe la main pour la lecture. C'est ce partage
# qui rend la restitution honnete : le chiffre vient du code, l'interpretation
# vient d'une lecture assumee.


def _render_agg(
    lignes_agg: list[dict[str, Any]], entete: list[str], notes: list[str]
) -> str:
    out = list(entete)
    if notes:
        out.append("")
        out.extend(notes)
    out.append("")
    out.append(_to_csv_text(lignes_agg) if lignes_agg else "(aucun avis)")
    return "\n".join(out)


@mcp.tool(annotations=_hints("Compter et noter, agrege cote serveur", SUR_L_API))
@_guard
def trustpilot_summary(
    domaine: str = "",
    date_from: str = "",
    date_to: str = "",
    group_by: str = "mois",
    stars: str = "",
    langue: str = "",
    theme: str = "",
    transporteur: str = "",
    mode: str = "auto",
    max_rows_par_domaine: int = 6000,
) -> str:
    """Compte les avis et calcule la note moyenne, agreges COTE SERVEUR.

    C'EST L'OUTIL A UTILISER DES QUE LA QUESTION COMMENCE PAR « combien »,
    « quelle note », « quelle repartition », « par mois », « par pays ». Il
    pagine le perimetre, agrege ici, et ne rend que le resultat - au lieu de
    faire remonter des milliers d'avis dans la conversation pour les compter a
    la main.

    group_by : mois, date, domaine, pays, enseigne, etoiles, langue, reponse,
               source, verifie, route, theme, transporteur, aucun.

    Ce qu'il rend pour chaque groupe : nombre d'avis, note moyenne, nombre et
    part d'avis a 1-2 etoiles, nombre et part a 4-5, taux de reponse de
    l'entreprise, delai de reponse median.

    ATTENTION - deux chiffres a ne pas confondre. `note_moyenne` est la moyenne
    des notes des avis de la PERIODE. Le TrustScore affiche par Trustpilot est
    pondere et glissant : il se lit dans trustpilot_profile, et il ne sera pas
    egal. Ne pas les presenter comme le meme indicateur.
    """
    domaines = _resolve_domains(domaine)
    debut, fin, note_periode = _periode(date_from, date_to, domaines)
    rows, avertissements, routes = _fetch_many(
        domaines,
        date_from=debut,
        date_to=fin,
        stars=stars,
        langue=langue,
        mode=mode,
        max_rows_par_domaine=max_rows_par_domaine,
    )
    lus = len(rows)
    if theme:
        rows = [r for r in rows if _norm(theme) in _norm(r.get("themes"))]
    if transporteur:
        besoin = _norm(transporteur).replace(" ", "_")
        rows = [r for r in rows if besoin in _norm(r.get("transporteurs_cites"))]

    lignes_agg = _aggregate(rows, group_by)
    entete = _entete_lecture(
        domaines,
        debut,
        fin,
        routes,
        {"stars": stars, "langue": langue, "theme": theme, "transporteur": transporteur},
        lus,
    )
    entete.insert(0, f"Agregation par {group_by or 'aucun'}")
    if note_periode:
        entete.insert(0, note_periode)
        entete.insert(1, "")

    notes: list[str] = []
    if theme or transporteur:
        notes.append(
            f"Filtre LEXICAL applique apres lecture : {len(rows)} avis retenus "
            f"sur {lus}. Ce filtre ne voit que ce qui est ecrit avec les mots du "
            "lexique - il sous-compte forcement."
        )
    if _norm(group_by) in ("theme", "themes", "transporteur", "transporteurs"):
        notes.append(
            "COLONNE MULTIVALUEE : un avis peut porter plusieurs themes ou citer "
            "plusieurs transporteurs. La somme de la colonne nb_avis DEPASSE "
            "donc le nombre d'avis lus, et ce n'est pas une erreur. Ne pas "
            "additionner cette colonne."
        )
    if avertissements:
        notes.append("")
        notes.append("AVERTISSEMENTS - le total ci-dessus est INCOMPLET :")
        notes.extend(f"  - {a}" for a in avertissements)
    else:
        notes.append(
            "Lecture complete sur ce perimetre : ces comptes sont citables, "
            "avec leur periode et leurs filtres."
        )
    return _render_agg(lignes_agg, entete, notes)


@mcp.tool(annotations=_hints("De quoi parlent les avis", SUR_L_API))
@_guard
def trustpilot_themes(
    domaine: str = "",
    date_from: str = "",
    date_to: str = "",
    stars: str = "",
    langue: str = "",
    croiser: str = "",
    mode: str = "auto",
    max_rows_par_domaine: int = 6000,
) -> str:
    """De quoi parlent les avis : classement par theme, avec la note de chacun.

    C'est le premier outil d'une question du type « de quoi se plaignent les
    clients espagnols ». Il ne repond pas a la question - il dit OU REGARDER,
    et trustpilot_verbatims sert ensuite a lire.

    croiser : '' (les themes seuls), 'etoiles', 'mois', 'pays', 'domaine' - un
    croisement theme x dimension, rendu en tableau long.

    CE QUE C'EST : une detection de MOTS, dans la langue de chaque avis, sur
    treize themes definis dans lexique.json. Un theme n'est detecte que si le
    client emploie les mots attendus.

    CE QUE CE N'EST PAS : une comprehension du texte. Le lexique ignore la
    NEGATION - « aucun retard » compte dans le theme delai -, l'ironie, et tout
    ce qui est dit autrement. Le taux de non-classes est rendu a chaque appel :
    au-dela de 40 %, la repartition n'est pas exploitable telle quelle, il faut
    completer le lexique ou lire les verbatims non classes.

    La note de chaque theme, elle, est solide : elle vient des etoiles, qui sont
    une donnee. C'est le croisement des deux - un theme frequent ET mal note -
    qui designe un sujet.
    """
    domaines = _resolve_domains(domaine)
    debut, fin, note_periode = _periode(date_from, date_to, domaines)
    rows, avertissements, routes = _fetch_many(
        domaines,
        date_from=debut,
        date_to=fin,
        stars=stars,
        langue=langue,
        mode=mode,
        max_rows_par_domaine=max_rows_par_domaine,
    )

    avec_texte = [r for r in rows if str(r.get("text") or "").strip()]
    classes = [r for r in avec_texte if r.get("themes")]
    taux_non_classes = (
        round(100 * (len(avec_texte) - len(classes)) / len(avec_texte), 1)
        if avec_texte
        else 0.0
    )

    dimension = _norm(croiser)
    if dimension:
        colonne = GROUP_KEYS.get(dimension)
        if colonne is None:
            raise TrustpilotError(
                f"croiser inconnu : '{croiser}'. Valeurs : "
                + ", ".join(sorted(GROUP_KEYS))
            )
        libelles = {k: (v.get("libelle") or k) for k, v in _themes_def().items()}
        lignes_agg: list[dict[str, Any]] = []
        paquets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            valeur = row.get(colonne)
            clef2 = "(vide)" if valeur in (None, "") else str(valeur)
            for theme in [t for t in str(row.get("themes") or "").split(";") if t]:
                paquets.setdefault((theme, clef2), []).append(row)
        for (theme, clef2) in sorted(paquets, key=lambda k: (-len(paquets[k]), k)):
            groupe = paquets[(theme, clef2)]
            ligne = {"theme": theme, "libelle": libelles.get(theme, ""), croiser: clef2}
            ligne.update(_stats_bloc(groupe))
            lignes_agg.append(ligne)
    else:
        lignes_agg = _aggregate_multi(rows, "themes")

    entete = _entete_lecture(
        domaines, debut, fin, routes, {"stars": stars, "langue": langue}, len(rows)
    )
    entete.insert(0, "Classement thematique des commentaires")
    if note_periode:
        entete.insert(0, note_periode)
        entete.insert(1, "")
    entete.append(f"Avis porteurs d'un texte : {len(avec_texte)} sur {len(rows)}")
    entete.append(
        f"Taux de NON-CLASSES : {taux_non_classes} % des avis avec texte - "
        "aucun mot du lexique detecte"
    )

    notes = [
        "COMMENT LIRE CE TABLEAU.",
        "  - `nb_avis` par theme : un avis peut porter plusieurs themes, donc la "
        "somme de la colonne DEPASSE le nombre d'avis. Ne pas l'additionner.",
        "  - `note_moyenne` par theme : c'est le chiffre solide, il vient des "
        "etoiles. Un theme frequent ET mal note est un sujet ; un theme "
        "frequent bien note est souvent un compliment (« livraison rapide » "
        "tombe dans le theme delai).",
        "  - le classement est LEXICAL : il ignore la negation et l'ironie. "
        "Utilise-le pour choisir ou regarder, puis lis avec "
        "trustpilot_verbatims. Ne presente jamais cette repartition comme une "
        "analyse de contenu.",
    ]
    if taux_non_classes > 40:
        notes.insert(
            0,
            f"ATTENTION : {taux_non_classes} % des avis avec texte ne sont "
            "classes dans AUCUN theme. La repartition ci-dessous ne decrit donc "
            "qu'une minorite du perimetre : ne la cite pas comme « ce dont "
            "parlent les clients ». Lis un echantillon de non-classes "
            "(trustpilot_verbatims avec theme='(non classe)') pour voir ce qui "
            "manque au lexique.",
        )
    if avertissements:
        notes.append("")
        notes.append("AVERTISSEMENTS - lecture incomplete :")
        notes.extend(f"  - {a}" for a in avertissements)
    notes.append("")
    notes.append(
        "SUITE LOGIQUE : trustpilot_verbatims sur le theme le plus mal note, "
        "pour lire ce que les clients ecrivent vraiment."
    )
    return _render_agg(lignes_agg, entete, notes)


@mcp.tool(annotations=_hints("Les commentaires clients a lire", SUR_L_API))
@_guard
def trustpilot_verbatims(
    domaine: str = "",
    date_from: str = "",
    date_to: str = "",
    stars: str = "",
    langue: str = "",
    theme: str = "",
    transporteur: str = "",
    contient: str = "",
    ordre: str = "pires",
    nombre: int = VERBATIM_DEFAULT_COUNT,
    longueur: int = VERBATIM_TEXT_CHARS,
    mode: str = "auto",
    max_rows_par_domaine: int = 6000,
) -> str:
    """Les commentaires clients eux-memes, selectionnes et bornes, a LIRE.

    C'est l'outil qui passe la main : le serveur choisit et borne, la lecture et
    l'interpretation sont a toi. Il n'y a aucune analyse de sentiment ici, et
    c'est volontaire - la note en etoiles est une donnee, un sentiment devine
    sur un texte n'en est pas une.

    ordre : comment l'echantillon est constitue. C'est le parametre qui change
    le sens du resultat, et il est TOUJOURS rappele dans la reponse :
      pires    - les moins bien notes d'abord (le defaut : c'est ce qu'on
                 cherche quand on ouvre Trustpilot en reunion transport) ;
      recents  - les plus recents d'abord ;
      anciens  - les plus anciens d'abord ;
      repartis - un echantillon regulier sur toute la periode, pour ne pas
                 confondre un pic avec une tendance ;
      meilleurs - les mieux notes, pour verifier ce qui marche.

    theme        : un theme du lexique, ou '(non classe)' pour voir ce qui
                   echappe au classement - c'est comme ca qu'on ameliore le
                   lexique.
    contient     : un mot ou une expression cherche directement dans le texte,
                   sans passer par le lexique. Insensible aux accents et a la
                   casse.
    longueur     : caracteres rendus par avis. Un avis long est coupe, et c'est
                   dit.

    LE TEXTE EST RENDU TEL QUEL, dans sa langue. Il n'est ni traduit ni
    reformule par le serveur : ce que tu lis est ce que le client a ecrit.
    """
    domaines = _resolve_domains(domaine)
    debut, fin, note_periode = _periode(date_from, date_to, domaines)
    nombre = max(1, min(VERBATIM_MAX_COUNT, int(nombre)))
    longueur = max(80, min(2000, int(longueur)))

    rows, avertissements, routes = _fetch_many(
        domaines,
        date_from=debut,
        date_to=fin,
        stars=stars,
        langue=langue,
        mode=mode,
        max_rows_par_domaine=max_rows_par_domaine,
    )
    lus = len(rows)
    rows = [r for r in rows if str(r.get("text") or "").strip()]
    avec_texte = len(rows)

    if theme:
        if _norm(theme) in ("non classe", "(non classe)", "aucun"):
            rows = [r for r in rows if not r.get("themes")]
        else:
            rows = [r for r in rows if _norm(theme) in _norm(r.get("themes"))]
    if transporteur:
        besoin = _norm(transporteur).replace(" ", "_")
        rows = [r for r in rows if besoin in _norm(r.get("transporteurs_cites"))]
    if contient:
        besoin = _norm(contient)
        rows = [
            r
            for r in rows
            if besoin in _norm(f"{r.get('title')} {r.get('text')}")
        ]
    retenus = len(rows)

    sens = _norm(ordre) or "pires"
    if sens == "recents":
        rows.sort(key=lambda r: str(r.get("createdAt") or ""), reverse=True)
        echantillon = rows[:nombre]
    elif sens == "anciens":
        rows.sort(key=lambda r: str(r.get("createdAt") or ""))
        echantillon = rows[:nombre]
    elif sens == "meilleurs":
        rows.sort(
            key=lambda r: (-(int(r.get("stars") or 0)), str(r.get("createdAt") or ""))
        )
        echantillon = rows[:nombre]
    elif sens == "repartis":
        rows.sort(key=lambda r: str(r.get("createdAt") or ""))
        pas = max(1, len(rows) // nombre) if rows else 1
        echantillon = rows[::pas][:nombre]
    elif sens == "pires":
        rows.sort(
            key=lambda r: (int(r.get("stars") or 6), str(r.get("createdAt") or ""))
        )
        echantillon = rows[:nombre]
    else:
        raise TrustpilotError(
            f"ordre inconnu : '{ordre}'. Valeurs : pires, meilleurs, recents, "
            "anciens, repartis."
        )

    explications = {
        "pires": "les avis les MOINS BIEN notes d'abord",
        "meilleurs": "les avis les MIEUX notes d'abord",
        "recents": "les avis les PLUS RECENTS d'abord",
        "anciens": "les avis les PLUS ANCIENS d'abord",
        "repartis": "un echantillon REGULIER sur toute la periode",
    }

    entete = _entete_lecture(
        domaines,
        debut,
        fin,
        routes,
        {
            "stars": stars,
            "langue": langue,
            "theme": theme,
            "transporteur": transporteur,
            "contient": contient,
        },
        lus,
    )
    entete.insert(0, "Verbatims clients - a lire, pas a compter")
    if note_periode:
        entete.insert(0, note_periode)
        entete.insert(1, "")
    entete.append(f"Avis porteurs d'un texte : {avec_texte}")
    entete.append(f"Correspondant aux filtres : {retenus}")
    entete.append(
        f"ECHANTILLON RENDU : {len(echantillon)} avis - {explications[sens]}. "
        "Ce n'est PAS le perimetre entier : ne compte rien sur cet echantillon, "
        "et ne le presente pas comme representatif. Pour compter, "
        "trustpilot_summary ; pour la repartition, trustpilot_themes."
    )
    if avertissements:
        entete.append("")
        entete.append("AVERTISSEMENTS :")
        entete.extend(f"  - {a}" for a in avertissements)

    lignes = ["\n".join(entete), ""]
    if not echantillon:
        lignes.append("(aucun avis ne correspond)")
        if theme:
            lignes.append("")
            lignes.append(
                "Le filtre theme est LEXICAL : un theme absent ne veut pas dire "
                "que le sujet n'existe pas, seulement que les mots du lexique "
                "n'apparaissent pas. Essaie `contient` avec un mot de la langue "
                "du pays."
            )
        return "\n".join(lignes)

    for i, row in enumerate(echantillon, 1):
        texte = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
        coupe = len(texte) > longueur
        entetes_avis = [
            f"[{i}] {row.get('stars')}/5 - {row.get('date')} - "
            f"{row.get('domaine')} ({row.get('pays')}) - {row.get('language')}"
        ]
        details = []
        if row.get("themes"):
            details.append(f"themes: {row['themes']}")
        if row.get("transporteurs_cites"):
            details.append(f"transporteur cite: {row['transporteurs_cites']}")
        if row.get("referenceId"):
            details.append(f"commande: {row['referenceId']}")
        details.append(f"reponse: {row.get('a_reponse')}")
        details.append(f"id: {row.get('id')}")
        if details:
            entetes_avis.append("    " + " | ".join(details))
        if row.get("titre"):
            entetes_avis.append(f"    « {row['titre']} »")
        entetes_avis.append(
            "    " + texte[:longueur] + (" [...coupe]" if coupe else "")
        )
        lignes.append("\n".join(entetes_avis))
        lignes.append("")

    lignes.append(
        "POUR RESTITUER CES VERBATIMS. Cite-les en indiquant la note, la date "
        "et le pays ; c'est ce qui les rend verifiables. Ne les traduis que si "
        "on te le demande, et dis-le quand tu le fais. N'extrapole pas d'un "
        "verbatim a une tendance : trois avis qui disent la meme chose sur "
        "quatre mille ne sont pas un signal - c'est trustpilot_summary et "
        "trustpilot_themes qui disent si c'en est un."
    )
    lignes.append(
        "Et si un avis met en cause un transporteur : son referenceId est le "
        "numero de commande. Passe par Reflex ou Shiptify pour savoir QUI a "
        "reellement livre, avant de porter le sujet en revue transporteur."
    )
    return "\n".join(lignes)


@mcp.tool(annotations=_hints("Les transporteurs cites par les clients", SUR_L_API))
@_guard
def trustpilot_transporteurs(
    domaine: str = "",
    date_from: str = "",
    date_to: str = "",
    stars: str = "",
    mode: str = "auto",
    max_rows_par_domaine: int = 6000,
) -> str:
    """Les transporteurs CITES par les clients dans leurs avis, et leur note.

    C'est le seul endroit ou le lien avis / transporteur est direct sans passer
    par le numero de commande. C'est utile, et c'est piegeux : lis les trois
    avertissements que rend cet outil avant d'en tirer quoi que ce soit.

    La detection est restreinte aux transporteurs qui livrent le PAYS de l'avis.
    C'est ce qui permet de distinguer Rhenus Italie de Rhenus Pologne, et Bring
    (Suede) de Posten Bring (Norvege) - et ce qui evite de compter le mot
    norvegien « posten », qui veut aussi dire « la poste ».
    """
    domaines = _resolve_domains(domaine)
    debut, fin, note_periode = _periode(date_from, date_to, domaines)
    rows, avertissements, routes = _fetch_many(
        domaines,
        date_from=debut,
        date_to=fin,
        stars=stars,
        mode=mode,
        max_rows_par_domaine=max_rows_par_domaine,
    )
    avec_texte = [r for r in rows if str(r.get("text") or "").strip()]
    cites = [r for r in avec_texte if r.get("transporteurs_cites")]
    lignes_agg = _aggregate_multi(rows, "transporteurs_cites")
    notes_carriers = {k: (v.get("note") or "") for k, v in _carriers_def().items()}
    for ligne in lignes_agg:
        ligne["a_savoir"] = notes_carriers.get(str(ligne.get("groupe")), "")

    entete = _entete_lecture(
        domaines, debut, fin, routes, {"stars": stars}, len(rows)
    )
    entete.insert(0, "Transporteurs cites dans les avis clients")
    if note_periode:
        entete.insert(0, note_periode)
        entete.insert(1, "")
    entete.append(
        f"Avis citant au moins un transporteur : {len(cites)} sur "
        f"{len(avec_texte)} avis avec texte "
        + (
            f"({round(100 * len(cites) / len(avec_texte), 1)} %)"
            if avec_texte
            else ""
        )
    )

    notes = [
        "TROIS AVERTISSEMENTS, a lire avant d'utiliser ce tableau.",
        "",
        "  1. UN TAUX DE MENTION N'EST PAS UN TAUX D'INCIDENT. On ne cite le "
        "transporteur que quand quelque chose frappe - en bien ou en mal -, et "
        "un transporteur qui livre beaucoup est nomme plus souvent. Ce tableau "
        "ne classe pas la qualite des transporteurs. La mesure de la qualite "
        "est le Scorecard Transporteur.",
        "",
        "  2. LA MENTION N'EST PAS UNE PREUVE. Un client peut nommer le mauvais "
        "transporteur, ou nommer celui du dernier kilometre pour un probleme "
        "venu de l'amont. La seule chaine fiable est referenceId -> commande -> "
        "Reflex ou Shiptify -> transporteur reel.",
        "",
        "  3. LES EFFECTIFS SONT PETITS. Peu de clients nomment un "
        "transporteur : une note moyenne calculee sur cinq mentions ne se "
        "commente pas. Regarde la colonne nb_avis avant la colonne "
        "note_moyenne.",
    ]
    if avertissements:
        notes.append("")
        notes.append("AVERTISSEMENTS DE LECTURE :")
        notes.extend(f"  - {a}" for a in avertissements)
    notes.append("")
    notes.append(
        "SUITE UTILE : trustpilot_verbatims avec transporteur='<CODE>' pour "
        "lire les avis qui le citent."
    )
    return _render_agg(lignes_agg, entete, notes)


# --------------------------------------------------------------------------
# Export CSV
# --------------------------------------------------------------------------

def _ecrire_csv(rows: list[dict[str, Any]], nom: str) -> pathlib.Path:
    cible_dir = _export_dir()
    try:
        cible_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TrustpilotError(
            f"Dossier d'export inutilisable ({cible_dir}) : {exc}"
        ) from exc
    if not nom.lower().endswith(".csv"):
        nom += ".csv"
    cible = cible_dir / pathlib.Path(nom).name
    try:
        # newline="" plutot que write_text : sans lui, l'ecriture traduit les
        # fins de ligne en CRLF sous Windows et les laisse en LF sous macOS. Le
        # meme code produirait deux fichiers differents selon le poste, ce que
        # la convention de construction interdit (08_ENGINE/04_mcp/README.md).
        # utf-8-sig : le BOM est ce qui fait qu'Excel FR ouvre le fichier sans
        # passer par l'assistant d'import.
        with cible.open("w", encoding="utf-8-sig", newline="") as handle:
            handle.write(_to_csv_text(rows))
    except OSError as exc:
        raise TrustpilotError(f"Ecriture impossible dans {cible} : {exc}") from exc
    return cible


@mcp.tool(annotations=_hints("Exporter les avis en CSV (sur demande explicite)", SUR_L_API, lecture_seule=False, idempotent=False))
@_guard
def trustpilot_export_csv(
    domaine: str = "",
    date_from: str = "",
    date_to: str = "",
    stars: str = "",
    langue: str = "",
    repondu: str = "",
    theme: str = "",
    transporteur: str = "",
    mode: str = "auto",
    fields: str = "*",
    filename: str = "",
    max_rows_par_domaine: int = 20000,
    inclure_donnees_personnelles: bool = False,
) -> str:
    """Exporte en CSV le perimetre demande, en paginant la collection entiere.

    ECRIT UN FICHIER SUR LE DISQUE. A n'appeler que si l'utilisateur a demande
    un fichier, un export, un CSV ou un classeur. Sinon, reponds dans la session
    avec trustpilot_summary, trustpilot_themes ou trustpilot_verbatims.

    Il prend le MEME perimetre que les outils de lecture - le meme que la
    question posee en francais -, et il pagine tout, pas seulement ce qui
    tiendrait dans la conversation. C'est la difference entre regarder et
    livrer.

    CSV point-virgule, UTF-8 avec BOM : il s'ouvre dans Excel FR sans assistant
    d'import. Les objets imbriques sortent en colonnes pointees
    (consumer.displayName, companyReply.text), comme le faisait le script Power
    Query.

    inclure_donnees_personnelles : par defaut FAUX, et le referralEmail - la
    boite mail du client invite - est retire du fichier. Ne le passe a VRAI que
    si l'utilisateur en a besoin et sait ou le fichier va atterrir : une adresse
    client n'a rien a faire dans un mail transporteur ni dans la bibliotheque
    d'equipe.

    Le fichier va dans le dossier d'export local, JAMAIS dans la bibliotheque
    synchronisee. Le chemin exact est rendu en reponse.
    """
    domaines = _resolve_domains(domaine)
    debut, fin, note_periode = _periode(date_from, date_to, domaines)
    rows, avertissements, routes = _fetch_many(
        domaines,
        date_from=debut,
        date_to=fin,
        stars=stars,
        langue=langue,
        repondu=repondu,
        mode=mode,
        max_rows_par_domaine=max_rows_par_domaine,
    )
    lus = len(rows)
    if theme:
        rows = [r for r in rows if _norm(theme) in _norm(r.get("themes"))]
    if transporteur:
        besoin = _norm(transporteur).replace(" ", "_")
        rows = [r for r in rows if besoin in _norm(r.get("transporteurs_cites"))]
    rows.sort(key=lambda r: str(r.get("createdAt") or ""), reverse=True)

    prets = _select(
        _strip_personal(rows, keep=bool(inclure_donnees_personnelles)), fields
    )
    if not filename.strip():
        etiquette = (
            _norm(domaine).replace(" ", "_").replace(",", "-")
            if domaine
            else "tous_domaines"
        )
        etiquette = re.sub(r"[^a-z0-9_.-]+", "_", etiquette).strip("_") or "perimetre"
        filename = f"trustpilot_{etiquette}_{debut or 'origine'}_{fin or dt.date.today().isoformat()}.csv"
    cible = _ecrire_csv(prets, filename)

    colonnes = _columns(prets)
    sortie: list[str] = []
    if avertissements:
        sortie.append(
            "ATTENTION - L'EXPORT EST INCOMPLET. A dire AVANT de livrer le "
            "fichier : un export tronque dont on ne dit rien est un chiffre "
            "faux qui part dans un mail."
        )
        sortie.extend(f"  - {a}" for a in avertissements)
        sortie.append("")
    sortie.append(f"Export termine : {cible}")
    sortie.append(f"{len(prets)} ligne(s), {len(colonnes)} colonne(s).")
    if note_periode:
        sortie.append("")
        sortie.append(note_periode)
    sortie.append("")
    sortie.extend(
        _entete_lecture(
            domaines,
            debut,
            fin,
            routes,
            {
                "stars": stars,
                "langue": langue,
                "repondu": repondu,
                "theme": theme,
                "transporteur": transporteur,
            },
            lus,
        )
    )
    if theme or transporteur:
        sortie.append(
            f"Filtre lexical applique : {len(rows)} avis retenus sur {lus} lus."
        )
    if not avertissements:
        sortie.append("")
        sortie.append(
            "Lecture complete : chaque domaine a ete parcouru jusqu'a sa "
            "derniere page sur ce perimetre."
        )
    sortie.append("")
    sortie.append(
        "Donnees personnelles : "
        + (
            "INCLUSES (referralEmail present dans le fichier). Ne depose ce "
            "fichier ni dans la bibliotheque d'equipe, ni en piece jointe a un "
            "transporteur."
            if inclure_donnees_personnelles
            else "retirees (referralEmail et identifiants consommateur absents)."
        )
    )
    if colonnes:
        sortie.append("")
        sortie.append("Colonnes : " + ", ".join(colonnes[:60]))
        if len(colonnes) > 60:
            sortie.append(f"... et {len(colonnes) - 60} autres.")
    return "\n".join(sortie)


# --------------------------------------------------------------------------
# L'echappatoire
# --------------------------------------------------------------------------

@mcp.tool(annotations=_hints("Appeler un chemin GET de la liste blanche", SUR_L_API))
@_guard
def trustpilot_get(
    path: str, query_json: str = "{}", max_rows: int = 200, fields: str = ""
) -> str:
    """Appelle n'importe quel chemin GET de la liste blanche. Echappatoire.

    Pour les chemins qui n'ont pas d'outil dedie : categories, images, liens
    web, derniers avis toutes business units, recherche de business unit, tags
    d'un avis. Appelle trustpilot_list_paths d'abord pour le chemin exact et ses
    parametres.

    Un chemin absent de la liste blanche est refuse, et seule la methode GET est
    emise : aucune ecriture n'est possible par cet outil - ni reponse a un avis,
    ni tag, ni invitation.
    """
    verifie = _check_get_path(path)
    try:
        query = json.loads(query_json or "{}")
    except ValueError as exc:
        raise TrustpilotError(
            f"query_json n'est pas du JSON valide : {exc}"
        ) from exc
    if not isinstance(query, dict):
        raise TrustpilotError("query_json doit etre un objet JSON.")

    regime = "PRIVE" if _auth_for(verifie) == "oauth" else "public"
    entete = (
        f"GET {verifie} [{regime}] | filtres : "
        + json.dumps(_clean_query(query), ensure_ascii=False)
    )
    page_param, per_page_param = _page_params(verifie)
    if _supports_paging(verifie) and page_param not in query:
        rows, arret = _paginate(verifie, query, max_rows=max_rows)
        return _render_table(rows, entete, arret, fields)
    charge = _request(verifie, query)
    rows = _rows(charge)
    if isinstance(charge, list) or len(rows) > 1:
        arret = "max_rows" if len(rows) > max_rows else ""
        return _render_table(rows[:max_rows], entete, arret, fields)
    return _render_json(charge, entete)


# --------------------------------------------------------------------------
# Le cache local : l'historique, sans replafonner
# --------------------------------------------------------------------------
#
# Ce que le cache debloque, et que le direct ne peut pas faire :
#   - un historique qui depasse le garde-fou de pagination - et sur dix-huit
#     domaines depuis 2023, on le depasse toujours ;
#   - la MEME question reposee demain sans reconsommer le quota, qui est
#     compte par APPLICATION donc partage par les vingt-six postes ;
#   - du SQL libre, donc des croisements que l'API n'expose pas : la note par
#     mois et par pays, l'evolution d'un theme sur douze mois, le delai de
#     reponse par enseigne.
#
# Ce qu'il ne fait pas : il n'est pas l'etat courant. Un avis publie depuis la
# synchro n'y est pas, et une reponse ajoutee depuis n'y est pas non plus. Pour
# le jour meme, c'est le direct qui fait foi - et chaque rendu du cache affiche
# sa date de synchro.


def _sync_hint() -> str:
    return (
        "Lance d'abord trustpilot_sync : il rapatrie les avis dans le cache "
        "local, une fois, et trustpilot_sql l'interroge ensuite sans rappeler "
        "l'API."
    )


def _cache_key(flat: dict[str, Any]) -> str:
    """Cle stable d'une ligne en cache : l'identifiant Trustpilot de l'avis.

    INSERT OR REPLACE sur cette cle : resynchroniser une periode deja rapatriee
    est donc toujours juste et ne cree jamais de doublon. C'est ce qui permet de
    dire a l'utilisateur qu'en cas de doute, il relance.
    """
    ident = flat.get("id")
    if ident not in (None, ""):
        return f"id={ident}"
    return hashlib.sha256(
        json.dumps(flat, sort_keys=True, ensure_ascii=False, default=str).encode(
            "utf-8"
        )
    ).hexdigest()


@mcp.tool(annotations=_hints("Rapatrier les avis dans le cache local", SUR_L_API, lecture_seule=False, idempotent=False))
@_guard
def trustpilot_sync(
    domaine: str = "",
    date_from: str = "",
    date_to: str = "",
    mode: str = "auto",
    max_rows_par_domaine: int = 200000,
    max_pages: int = 4000,
) -> str:
    """Rapatrie les avis dans le cache local SQLite, avec leurs colonnes maison.

    C'est le chemin de l'HISTORIQUE : il n'est pas soumis au garde-fou
    TRUSTPILOT_MAX_PAGES qui plafonne le direct. Une fois les avis en cache,
    trustpilot_sql repond instantanement, autant de fois qu'on veut, sans
    reconsommer le quota partage.

    ECRIT SUR LE DISQUE (un SQLite dans la racine locale, hors de tout dossier
    synchronise). A lancer sur demande, ou quand une question porte sur plus de
    quelques milliers d'avis.

    Le classement thematique et la detection de transporteur sont calcules A LA
    SYNCHRO et ranges en colonnes : le SQL peut donc filtrer dessus. Si le
    lexique change, il faut resynchroniser pour que les colonnes suivent - c'est
    le prix du calcul en amont, et il est assume : classer a la lecture couterait
    le meme calcul a chaque requete.

    domaine : vide = les dix-huit. date_from : vide = TRUSTPILOT_START_DATE.
    """
    domaines = _resolve_domains(domaine)
    debut = _iso_date(date_from, "debut") or _start_date()
    fin = _iso_date(date_to, "fin")

    table = cache.table_name("reviews")
    demarre = dt.datetime.now(dt.timezone.utc)
    conn = cache.open_rw(_cache_path())
    ecrits = 0
    par_domaine: list[str] = []
    incidents: list[str] = []
    try:
        for domain in domaines:
            try:
                rows, arret, route = _fetch_reviews(
                    domain,
                    date_from=debut,
                    date_to=fin,
                    mode=mode,
                    max_rows=max_rows_par_domaine,
                    max_pages=max_pages,
                )
            except (ConfigError, TrustpilotError) as exc:
                incidents.append(f"{domain} : {exc}")
                continue
            n = cache.upsert(
                conn, table, rows, _cache_key, NUMERIC_COLUMNS, INDEX_COLUMNS
            )
            ecrits += n
            par_domaine.append(
                f"  {domain:<26} {n:>7} avis  [{route}]"
                + (f"  ARRET : {arret}" if arret else "")
            )
            if arret:
                incidents.append(
                    f"{domain} : rapatriement INCOMPLET ({arret}) - le cache ne "
                    "couvre pas tout le perimetre demande pour ce domaine."
                )
        horodatage = demarre.isoformat(timespec="seconds")
        cache.meta_set(conn, f"last_sync:{table}", horodatage)
        cache.meta_set(
            conn,
            f"scope:{table}",
            json.dumps(
                {
                    "domaines": domaines,
                    "date_from": debut,
                    "date_to": fin or "(aujourd'hui)",
                    "mode": mode,
                },
                ensure_ascii=False,
            ),
        )
        conn.commit()
        total = conn.execute(
            f"SELECT COUNT(*) FROM {cache.quote(table)}"
        ).fetchone()[0]
    finally:
        conn.close()

    sortie = [
        f"Synchronisation terminee : {ecrits} avis ecrit(s) dans [{table}].",
        f"Cache : {_cache_path()}",
        f"Total en cache pour cette table : {total} avis.",
        f"Perimetre demande : {len(domaines)} domaine(s), du {debut} au "
        + (fin or "aujourd'hui"),
        "",
    ]
    sortie.extend(par_domaine)
    if incidents:
        sortie.append("")
        sortie.append("INCIDENTS - le cache ne couvre PAS tout le perimetre :")
        sortie.extend(f"  - {i}" for i in incidents)
    else:
        sortie.append("")
        sortie.append(
            "Tous les domaines ont ete parcourus jusqu'a leur derniere page : "
            "le cache couvre bien le perimetre demande."
        )
    sortie.append("")
    sortie.append(
        "Le cache est en INSERT OR REPLACE sur l'identifiant d'avis : relancer "
        "une synchro est toujours juste et ne cree jamais de doublon."
    )
    sortie.append(
        "Interroge maintenant avec trustpilot_sql, ou trustpilot_tables pour "
        "voir ce que le cache contient."
    )
    return "\n".join(sortie)


@mcp.tool(annotations=_hints("Ce que le cache local contient", HORS_LIGNE))
@_guard
def trustpilot_tables() -> str:
    """Ce que le cache local contient : volumes, date de synchro, perimetre."""
    conn = cache.open_ro(_cache_path(), _sync_hint())
    try:
        noms = cache.tables(conn)
        if not noms:
            return "Cache vide. " + _sync_hint()
        lignes = [f"Cache local : {_cache_path()}", ""]
        for table in noms:
            total = conn.execute(
                f"SELECT COUNT(*) FROM {cache.quote(table)}"
            ).fetchone()[0]
            colonnes = cache.columns(conn, table)
            lignes.append(f"[{table}] {total} ligne(s), {len(colonnes)} colonne(s)")
            lignes.append(
                f"    derniere synchro : "
                f"{cache.meta_get(conn, f'last_sync:{table}') or '(inconnue)'}"
            )
            lignes.append(
                f"    perimetre        : "
                f"{cache.meta_get(conn, f'scope:{table}') or '(inconnu)'}"
            )
            try:
                bornes = conn.execute(
                    f"SELECT MIN(\"createdAt\"), MAX(\"createdAt\") FROM "
                    f"{cache.quote(table)}"
                ).fetchone()
                if bornes and bornes[0]:
                    lignes.append(
                        f"    avis du          : {str(bornes[0])[:10]} au "
                        f"{str(bornes[1])[:10]}"
                    )
            except Exception:
                pass
        lignes.append("")
        lignes.append(
            "Le cache n'est PAS l'etat courant de Trustpilot : un avis publie "
            "depuis la synchro n'y est pas, et une reponse ajoutee depuis n'y "
            "est pas non plus. Pour le jour meme, utilise les outils de lecture, "
            "qui appellent l'API en direct."
        )
        return "\n".join(lignes)
    finally:
        conn.close()


@mcp.tool(annotations=_hints("Colonnes du cache et taux de remplissage", HORS_LIGNE))
@_guard
def trustpilot_columns(table: str = "reviews") -> str:
    """Colonnes d'une table du cache, avec leur taux de remplissage.

    A lire avant d'ecrire du SQL. Le taux de remplissage n'est pas un detail :
    `referenceId` n'existe que sur les avis issus d'une invitation et lus par le
    chemin prive - compter dessus une colonne remplie a 30 % donne un chiffre
    qui a l'air d'un total.
    """
    conn = cache.open_ro(_cache_path(), _sync_hint())
    try:
        nom = cache.table_name(table)
        colonnes = cache.columns(conn, nom)
        if not colonnes:
            return f"Table '{nom}' absente du cache. " + _sync_hint()
        total = conn.execute(f"SELECT COUNT(*) FROM {cache.quote(nom)}").fetchone()[0]
        lignes = [f"[{nom}] {total} ligne(s), {len(colonnes)} colonne(s)", ""]
        if not total:
            lignes.extend("  " + c for c in colonnes)
            return "\n".join(lignes)
        expression = ", ".join(
            f"SUM(CASE WHEN {cache.quote(c)} IS NULL OR {cache.quote(c)} = '' "
            f"THEN 0 ELSE 1 END)"
            for c in colonnes
        )
        comptes = conn.execute(
            f"SELECT {expression} FROM {cache.quote(nom)}"
        ).fetchone()
        for colonne, remplies in zip(colonnes, comptes):
            genre = "num" if colonne in NUMERIC_COLUMNS else "txt"
            lignes.append(
                f"  {colonne:<44} {genre}  "
                f"{100 * (remplies or 0) / total:5.1f}% rempli"
            )
        lignes.append("")
        lignes.append(
            "Les colonnes maison, calculees par le connecteur et absentes de "
            "l'API : domaine, pays, enseigne, annee_mois, date, titre, "
            "texte_court, a_reponse, delai_reponse_h, delai_experience_j, "
            "themes, nb_themes, transporteurs_cites, review_source."
        )
        return "\n".join(lignes)
    finally:
        conn.close()


@mcp.tool(annotations=_hints("Interroger le cache local en SQL", HORS_LIGNE))
@_guard
def trustpilot_sql(sql: str, max_rows: int = 100) -> str:
    """Interroge le cache local en SQL (SQLite, lecture seule).

    Pour tout ce que l'API ne sait pas faire : un croisement, un historique
    long, l'evolution d'un theme sur douze mois. Instantane, et sans consommer
    le quota.

    Les colonnes venant de l'API portent leur nom aplati, avec des points : il
    faut les guillemeter. trustpilot_columns les liste.

    Exemple - la note par mois et par pays :
      SELECT annee_mois, pays, COUNT(*) nb, ROUND(AVG(stars),2) note
      FROM reviews WHERE annee_mois >= '2026-01'
      GROUP BY 1,2 ORDER BY 1,2

    Exemple - le poids du theme livraison par pays :
      SELECT pays, COUNT(*) nb,
             SUM(CASE WHEN themes LIKE '%livraison_delai%' THEN 1 ELSE 0 END) delai
      FROM reviews GROUP BY 1 ORDER BY 2 DESC
    """
    propre = cache.guard_sql(sql)
    conn = cache.open_ro(_cache_path(), _sync_hint())
    try:
        rows, reste = cache.select(conn, propre, limit=int(max_rows))
        if not rows:
            return "(aucune ligne)\n\nRequete : " + propre
        lignes = [f"{len(rows)} ligne(s) rendues."]
        if reste:
            reel = cache.count_of(conn, propre)
            lignes.append(
                f"ATTENTION : la requete rend {reel} ligne(s) au total, "
                f"{max_rows} sont affichees. Le compte affiche n'est PAS le "
                "total - c'est celui-ci qui l'est. Releve max_rows, ou agrege "
                "dans la requete."
            )
        horodatage = cache.meta_get(conn, "last_sync:reviews")
        lignes.append(
            f"Cache synchronise le {horodatage or '(inconnu)'} - ce qui a bouge "
            "dans Trustpilot depuis n'est pas ici."
        )
        lignes.append("")
        lignes.append(cache.to_csv_text(rows))
        return "\n".join(lignes)
    finally:
        conn.close()


@mcp.tool(annotations=_hints("Exporter le resultat d'une requete (sur demande explicite)", HORS_LIGNE, lecture_seule=False, idempotent=False))
@_guard
def trustpilot_export_sql(
    sql: str, filename: str = "", max_rows: int = 500000
) -> str:
    """Exporte en CSV le resultat d'une requete sur le cache. SUR DEMANDE.

    C'est l'export sans plafond de pagination : il lit le cache, pas l'API. A
    n'appeler que si l'utilisateur a demande un fichier.
    """
    propre = cache.guard_sql(sql)
    conn = cache.open_ro(_cache_path(), _sync_hint())
    try:
        curseur = conn.execute(propre)
        colonnes = [d[0] for d in curseur.description]
        rows = curseur.fetchmany(int(max_rows))
        reste = curseur.fetchone() is not None
    except Exception as exc:
        raise TrustpilotError(f"SQL refuse par SQLite : {exc}") from exc
    finally:
        conn.close()

    cible_dir = _export_dir()
    try:
        cible_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TrustpilotError(
            f"Dossier d'export inutilisable ({cible_dir}) : {exc}"
        ) from exc
    nom = (filename or "").strip() or (
        "trustpilot_sql_" + dt.datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".csv"
    )
    if not nom.lower().endswith(".csv"):
        nom += ".csv"
    cible = cible_dir / pathlib.Path(nom).name
    try:
        with cible.open("w", encoding="utf-8-sig", newline="") as handle:
            ecrivain = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            ecrivain.writerow(colonnes)
            ecrivain.writerows(rows)
    except OSError as exc:
        raise TrustpilotError(f"Ecriture impossible dans {cible} : {exc}") from exc

    sortie = [
        f"Export termine : {cible}",
        f"{len(rows)} ligne(s), {len(colonnes)} colonne(s).",
    ]
    if reste:
        sortie.append(
            f"ATTENTION : la requete rendait PLUS de {max_rows} lignes, le "
            "fichier est TRONQUE. Releve max_rows, ou resserre la requete."
        )
    else:
        sortie.append(
            "Resultat complet : la requete ne rendait pas plus de lignes."
        )
    sortie.append(
        "Ce fichier peut contenir des verbatims clients : il reste dans le "
        "dossier local du connecteur, il ne se depose pas dans la bibliotheque "
        "d'equipe."
    )
    return "\n".join(sortie)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

HELP = """\
MCP trustpilot - lecture des avis de service Trustpilot du groupe.

  python server.py            mode serveur MCP (stdio), lance par Claude Code
  python server.py doctor     diagnostic de configuration et de connexion
  python server.py --help     cette aide

Configuration : la cle et le secret d'API. En mode plugin, ils se saisissent
dans /plugin > trustpilot > configuration. En installation directe, dans
~/.trustpilot-mcp/.env (modele : .env.example, a cote de ce fichier).

Les reglages communs a l'equipe - racine de l'API, les dix-huit domaines, date
de depart, plafonds - vivent dans 08_ENGINE/04_mcp/00_config/trustpilot.shared.env.

LECTURE SEULE : aucune reponse a un avis, aucun tag, aucune invitation.
"""


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if arg == "doctor":
        rapport = trustpilot_doctor()
        print(rapport)
        return 0 if ": OK |" in rapport else 1
    if arg:
        print(f"Argument inconnu : {arg}\n")
        print(HELP)
        return 2
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
