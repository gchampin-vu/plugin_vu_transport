#!/usr/bin/env python3
"""
MCP yooz-factures : lecture de la base Yooz (Yooz Rising, API publique v2, region eu1).

Trois usages :
    python server.py            # mode serveur MCP (stdio) - lance par Claude Code
    python server.py doctor     # diagnostic : config, jeton, appel API, cache
    python server.py sync       # rapatriement d'un rapport dans le cache local

Deux facons d'interroger Yooz, et c'est l'API qui l'impose
----------------------------------------------------------
L'API publique Yooz v2 (surface complete relevee dans le README) n'a **aucun
endpoint de recherche de documents** : le seul GET sur les documents est la
liste des types. La donnee facture sort par un **data report** defini dans
l'application, sans filtre serveur - ni fournisseur, ni periode, ni montant.

D'ou deux chemins, exposes separement :

  1. **L'historique** - `yooz_sync` rapatrie un data report page par page dans
     un cache SQLite local, puis `yooz_sql` / `yooz_invoices` / `yooz_summary`
     l'interrogent instantanement. C'est le seul moyen de repondre a "total par
     fournisseur sur juillet" sans retelecharger l'historique a chaque fois.

  2. **Les petites requetes** - ce que l'API rend vraiment vite, sans cache :
     `yooz_reports` (liste des rapports), `yooz_report_peek` (une page courte),
     `yooz_referential` (un fournisseur par son code : un appel, un objet),
     `yooz_org_units`, `yooz_document_types`, `yooz_exports`.

Lecture seule. Le seul POST du serveur est l'echange du refresh token contre un
access token. `POST /documents` (import de document) n'est deliberement pas
expose : ce serveur ne doit rien pouvoir ecrire dans Yooz.

Authentification
----------------
Un refresh token *offline* par societe (genere dans Yooz), echange contre un
access token de courte duree. Les secrets arrivent par deux voies, jamais par le
code ni par la bibliotheque SharePoint :

  - **en mode plugin** : la configuration du plugin Claude Code, saisie par
    chacun sur son poste (les champs sensibles vont dans le coffre du systeme) ;
  - **en direct** : un fichier yooz.env hors du vault (_candidate_env_files).

Une variable d'environnement non vide gagne toujours sur le fichier.
"""

from __future__ import annotations

import csv
import io
import json
import os
import pathlib
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API_TIMEOUT_S = 120.0
TOKEN_TIMEOUT_S = 60.0
DEFAULT_BASE_URL = "https://eu1.getyooz.com"
API_ROOT = "/yooz/v2/api"
# Pages du data report : plafonnees a 1 000 elements par l'API, valeur prise
# par defaut si pageSize n'est pas precise.
MAX_PAGE_SIZE = 1000
# Les referentiels, eux, plafonnent a 100 lignes par appel.
MAX_REFERENTIAL_LIMIT = 100
# Plancher de rapatriement : une date tres ancienne vaut "tout l'historique".
SINCE_FLOOR = "2023-01-01T00:00:00.000Z"
REPORT_PATH = API_ROOT + "/dataReports/data/{report_id}"
DEFAULT_DATASET = "factures"

# Familles de referentiel Yooz : nom parlant -> type de donnee de l'API.
# Chaque famille expose : /referentials, /referentials/{code}/count,
# /referentials/{code}/data?limit&offset (max 100), /referentials/{code}/data/{element}
REFERENTIAL_KINDS = {
    "fournisseur": "YZ_SUPPLIER",
    "client": "YZ_CUSTOMER",
    "compte": "YZ_ACCOUNT",
    "cause_blocage": "YZ_BLOCKING_CAUSE",
    "cause_refus": "YZ_REFUSE_CAUSE",
    "cause_suppression": "YZ_REMOVING_CAUSE",
    "categorie_facture": "YZ_INVOICE_CATEGORY",
    "categorie_speciale": "YZ_INVOICE_SPECIAL_CATEGORY",
    "devise": "YZ_CURRENCY",
    "mode_paiement": "YZ_PAYMENT_METHOD",
    "profil_tva": "YZ_TAX_PROFILE",
    "journal": "YZ_LEDGER",
    "periode_comptable": "YZ_ACCOUNTING_PERIOD",
    "article": "YZ_ITEM",
    "immobilisation": "YZ_FIXED_ASSET",
    "unite_mesure": "YZ_MEASURE_UNIT",
    "adresse": "YZ_ORGUNIT_ADDRESS",
    "compte_bancaire": "YZ_ORGUNIT_BANK_ACCOUNT",
}

# Colonnes du rapport factures a typer en numerique dans le cache : sans ca,
# un SUM() en SQL additionne des chaines et rend n'importe quoi.
NUMERIC_COLUMNS = {
    "amount",
    "taxAmount",
    "totalAmount",
    "convertedAmount",
    "convertedTotalAmount",
    "convertedTaxAmount",
    "convertedAmountAgainstReferenceCurrency",
    "convertedTaxAmountAgainstReferenceCurrency",
    "convertedTotalAmountAgainstReferenceCurrency",
    "totalAmountAgainstReferenceCurrency",
    "YZ_TOTAL_TAX_AMOUNT_1_YZ_COMMONS",
    "YZ_TOTAL_TAX_AMOUNT_2_YZ_COMMONS",
    "YZ_DISCREPANCY_AMOUNT_YZ_INVOICE",
    "YZ_DISCREPANCY_PERCENT_YZ_INVOICE",
    "YZ_EXCHANGE_RATE_YZ_COMMONS",
}
# Colonne des lignes de facture : une liste imbriquee, ecartee du cache par
# defaut comme dans le script Power Query (Table.RemoveColumns).
DEFAULT_DROP_COLUMNS = ("YZ_INVOICE_LINE",)

# Colonnes utiles a la lecture rapide d'une facture, dans l'ordre metier.
SUMMARY_COLUMNS = [
    "source_app",
    "orgUnitCode",
    "yoozNumber",
    "YZ_NUMBER_YZ_COMMONS",
    "thirdPartyName",
    "YZ_DATE_YZ_COMMONS",
    "YZ_DUE_DATE_YZ_COMMONS",
    "currency",
    "amount",
    "taxAmount",
    "totalAmount",
    "blockedBoolean",
    "blockingCause",
    "YZ_PORTAL_STATUS",
]


class ConfigError(RuntimeError):
    pass


class AuthError(RuntimeError):
    pass


class YoozError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Configuration : fichier d'environnement hors du vault
# --------------------------------------------------------------------------

def _clean(value: str | None) -> str:
    """Nettoie une valeur d'environnement.

    Une substitution de plugin non resolue arrive telle quelle - literalement
    '${user_config.client_secret}'. Sans ce filtre, elle serait prise pour un
    secret et Yooz repondrait un 401 incomprehensible.
    """
    val = (value or "").strip()
    if val.startswith("${") and val.endswith("}"):
        return ""
    return val


def _local_root() -> pathlib.Path:
    """Racine locale de l'outil : cache SQLite, exports, jetons renouveles.

    **Pas sous %LOCALAPPDATA%**, et ce n'est pas un detail de gout. Un
    interpreteur Windows empaquete (Microsoft Store, Python Manager) donne a ses
    processus enfants une **vue virtualisee de %LOCALAPPDATA%** : le serveur
    lance par le plugin et le meme serveur lance en ligne de commande ne
    verraient pas le meme cache, sans aucune erreur. Le profil utilisateur, lui,
    n'est pas virtualise. Une seule racine, les deux modes voient le meme cache.
    """
    raw = _clean(os.environ.get("YOOZ_HOME"))
    if raw:
        return pathlib.Path(raw)
    return pathlib.Path.home() / ".yooz-mcp"


def _legacy_root() -> pathlib.Path:
    """Ancienne racine (%LOCALAPPDATA%\\yooz-mcp), d'avant le passage en plugin.

    Encore lue pour le fichier de configuration et le venv, jamais ecrite.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return pathlib.Path(base) / "yooz-mcp"


def _guard_not_synced(path: pathlib.Path, what: str) -> None:
    """Interdit qu'un fichier porteur de secrets vive dans un dossier synchronise.

    Le vault est une bibliotheque SharePoint partagee avec l'equipe L&T : un
    fichier de secrets pose dedans part chez tout le monde, et la regle du
    cerveau commun est explicite (aucun secret dans la base).
    """
    flat = str(path).replace("\\", "/").lower()
    for forbidden in (
        "/onedrive",
        "cafom",
        "sharepoint",
        "dropbox",
        "google drive",
        "transport btoc",
    ):
        if forbidden in flat:
            raise ConfigError(
                f"{what} ne peut pas vivre dans un dossier synchronise ({path}) : "
                f"il contient des secrets Yooz. Laisse la valeur par defaut "
                f"(~/.yooz-mcp) ou pointe un dossier local."
            )


def _packaged_python() -> str:
    """Detecte un processus Windows empaquete (MSIX). Rend la raison, ou "".

    Pourquoi ca compte : un `python` lance par l'alias du Microsoft Store donne
    a **toute sa descendance** une vue virtualisee de %LOCALAPPDATA%. Mesure sur
    ce poste le 2026-08-27 : dans %LOCALAPPDATA%\\yooz-mcp, PowerShell et
    l'interpreteur du venv lance directement voient ['venv', 'yooz.env'] ; le
    meme interpreteur de venv, lance par l'alias du Store, ne voit que ['venv'].
    Un fichier de configuration pose la est donc **invisible sans erreur**.

    La detection ne peut pas se faire sur `sys.base_prefix` : dans le processus
    enfant, il pointe l'installation reelle (pythoncore-...), sans trace du
    paquet. On interroge donc l'API Windows sur le processus courant, qui est la
    seule source fiable, avec repli sur le chemin de l'executable.
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes

        length = ctypes.c_uint32(0)
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)
        # 15700 = APPMODEL_ERROR_NO_PACKAGE : le processus n'est pas empaquete.
        # 122 = ERROR_INSUFFICIENT_BUFFER : il l'est, il faut un tampon.
        if rc in (0, 122):
            buf = ctypes.create_unicode_buffer(max(length.value, 512))
            ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), buf)
            return f"processus empaquete (MSIX) : {buf.value or 'paquet inconnu'}"
    except Exception:
        pass
    for probe in (sys.executable, sys.base_prefix):
        if "windowsapps" in (probe or "").lower():
            return f"interpreteur empaquete detecte ({probe})"
    return ""


def _candidate_env_files() -> list[pathlib.Path]:
    """Emplacements de fichier de configuration essayes, dans l'ordre.

    Le dossier de donnees du plugin passe en premier : il vit sous
    ~/.claude/plugins/data/, hors de la zone que les interpreteurs empaquetes
    virtualisent. C'est le seul emplacement de fichier fiable quand c'est le
    plugin qui lance python.
    """
    out: list[pathlib.Path] = []
    explicit = _clean(os.environ.get("YOOZ_ENV_FILE"))
    if explicit:
        out.append(pathlib.Path(explicit))
    plugin_data = _clean(os.environ.get("CLAUDE_PLUGIN_DATA"))
    if plugin_data:
        out.append(pathlib.Path(plugin_data) / "yooz.env")
    out.append(_local_root() / "yooz.env")
    out.append(_legacy_root() / "yooz.env")
    return out


_ENV_CACHE: dict[str, str] | None = None
# Ce que le serveur a reellement lu, pour que yooz_status puisse le dire.
_ENV_SOURCE: str = "(pas encore lu)"


def _env_file() -> pathlib.Path:
    """Le fichier de configuration retenu : le premier qui existe.

    A defaut, l'emplacement ou il faudrait le creer. Un emplacement synchronise
    est ignore en silence **sauf s'il a ete demande explicitement** par
    YOOZ_ENV_FILE : passer outre un choix explicite serait pire que d'echouer.
    """
    explicit = _clean(os.environ.get("YOOZ_ENV_FILE"))
    if explicit:
        _guard_not_synced(pathlib.Path(explicit), "Le fichier d'environnement Yooz")
    candidates = _candidate_env_files()
    for path in candidates:
        try:
            _guard_not_synced(path, "Le fichier d'environnement Yooz")
        except ConfigError:
            continue
        if path.exists():
            return path
    preferred = candidates[0] if candidates else _local_root() / "yooz.env"
    _guard_not_synced(preferred, "Le fichier d'environnement Yooz")
    return preferred


def _load_env_file() -> dict[str, str]:
    """Lit le fichier de configuration. Format KEY=VALUE, # pour un commentaire.

    Une variable **non vide** de l'environnement du processus gagne. Le "non
    vide" n'est pas un detail : en mode plugin, la configuration arrive par
    l'environnement, et un champ que le collegue n'a pas rempli pose une
    variable vide qu'il ne faut pas prendre pour une valeur.
    """
    global _ENV_CACHE, _ENV_SOURCE
    if _ENV_CACHE is not None:
        return _ENV_CACHE
    values: dict[str, str] = {}
    path = _env_file()
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            values[key.strip().upper()] = val
        _ENV_SOURCE = str(path)
    else:
        _ENV_SOURCE = f"(aucun fichier ; cherche : {', '.join(str(p) for p in _candidate_env_files())})"
    _ENV_CACHE = values
    return values


# --------------------------------------------------------------------------
# La configuration d'equipe : le fichier partage de 08_ENGINE
# --------------------------------------------------------------------------
#
# Ce fichier porte TOUT ce qui est commun a l'equipe : le reglage - region Yooz,
# liste des societes, identifiant du data report, applicationId et client_id de
# chaque societe, colonnes ecartees - **et les identifiants de connexion** de
# chaque societe.
#
# Decision d'equipe du 2026-08-28. Les identifiants Yooz sont ceux d'une
# APPLICATION, pas d'une personne : un jeu par societe, le meme pour tout le
# monde, deja connu de l'equipe. Les faire ressaisir vingt-six fois n'ajoutait
# aucune protection - seulement vingt-six mises en service qui echouent sur un
# applicationId pris pour un client_id, et une rotation impossible a propager.
# Ils sont donc poses une fois ici, avec le reste.
#
# Ce que ca implique, et qu'il faut assumer : la bibliotheque est lisible par
# toute l'equipe L&T, donc ces identifiants le sont aussi. Le jour ou l'un
# d'eux doit cesser de l'etre, la reponse est la configuration du plugin sur le
# poste, qui passe DEVANT le fichier d'equipe (voir _env), pas le retrait de la
# ligne partagee.
#
# UN PIEGE PROPRE AU REFRESH TOKEN, a connaitre avant d'en poser un ici.
# Keycloak fait tourner le refresh token a chaque echange : le serveur range le
# nouveau dans un magasin LOCAL (_store_refresh), et l'ancien peut etre invalide
# cote Yooz. Un refresh token partage n'est donc PAS partageable indefiniment -
# le premier poste qui s'en sert peut le perimer pour les autres, qui verront un
# invalid_grant. Deux facons de vivre avec :
#   - laisser la rotation desactivee cote Yooz pour ce client (a verifier avec
#     l'administrateur Yooz) ; c'est la seule solution vraiment stable ;
#   - sinon, poser le client_secret ici - il ne tourne pas - et garder le
#     refresh token sur chaque poste.
# _get_token le dit explicitement quand l'echange echoue.
#
# La liste blanche RESTE, mais elle a change d'objet : ce n'est plus une barriere
# anti-secret, c'est un garde-fou contre la faute de frappe. Une variable mal
# orthographiee serait sinon ignoree en silence, et on chercherait longtemps
# pourquoi le reglage d'equipe ne prend pas. Elle est refusee **et signalee** par
# /yooz-setup.
#
# Ce qui n'a toujours pas sa place ici : un chemin local (YOOZ_CACHE_DB,
# YOOZ_EXPORT_DIR) - il n'existe pas sur les vingt-cinq autres postes.

SHARED_FILE_NAME = "yooz.shared.env"
ENGINE_DIR_NAME = "08_ENGINE"
SHARED_SUBPATH = ("04_mcp", "00_config")

# Reglages globaux autorises dans le fichier d'equipe.
SHARED_ALLOWED_KEYS = frozenset(
    {
        "YOOZ_BASE_URL",
        "YOOZ_COMPANIES",
        "YOOZ_DEFAULT_REPORT_ID",
        "YOOZ_DROP_COLUMNS",
    }
)

# Suffixes autorises pour une variable propre a une societe (YOOZ_<CLE>_<SUFFIXE>).
# Le nom de societe n'est pas connu a l'avance : il vient de YOOZ_COMPANIES.
# _CLIENT_SECRET et _REFRESH_TOKEN ont ete ouverts le 2026-08-28.
SHARED_ALLOWED_SUFFIXES = (
    "_LABEL",
    "_APPLICATION_ID",
    "_CLIENT_ID",
    "_REPORT_ID",
    "_CLIENT_SECRET",
    "_REFRESH_TOKEN",
)

# Les suffixes dont la VALEUR ne s'affiche jamais, meme dans un diagnostic. Le
# NOM de la variable, lui, se dit : c'est ce qui permet de savoir d'ou vient la
# valeur active sans la reveler.
SHARED_SECRET_SUFFIXES = ("_CLIENT_SECRET", "_REFRESH_TOKEN")

_SHARED_CACHE: dict[str, str] | None = None
_SHARED_LOADED_FROM: str = ""
# Cles ignorees a la lecture, parce qu'absentes de la liste blanche. Remontees
# par yooz_status : c'est presque toujours une faute de frappe, et elle ne se
# voit nulle part ailleurs.
_SHARED_REJECTED: list[str] = []


def _shared_key_allowed(key: str) -> bool:
    if key in SHARED_ALLOWED_KEYS:
        return True
    if not key.startswith("YOOZ_"):
        return False
    return any(key.endswith(suffix) for suffix in SHARED_ALLOWED_SUFFIXES)


def _shared_key_is_secret(key: str) -> bool:
    return any(key.endswith(suffix) for suffix in SHARED_SECRET_SUFFIXES)


def _parse_env_text(text: str) -> dict[str, str]:
    """Parseur KEY=VALUE minimal, # en commentaire, guillemets optionnels."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key.strip().upper()] = val
    return out


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

    explicit = _clean(os.environ.get("VU_ENGINE_DIR"))
    if explicit:
        add(pathlib.Path(explicit))

    starts = [pathlib.Path(__file__).resolve()]
    plugin_root = _clean(os.environ.get("CLAUDE_PLUGIN_ROOT"))
    if plugin_root:
        starts.append(pathlib.Path(plugin_root))
    for start in starts:
        for parent in start.parents:
            if parent.name == ENGINE_DIR_NAME:
                add(parent)
                break

    # Profil utilisateur : OneDrive pose la bibliotheque d'equipe sous
    # <profil>\CAFOM\<bibliotheque>\. Le nom exact varie d'un poste a l'autre,
    # on ne le devine pas, on le cherche.
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
    explicit = _clean(os.environ.get("YOOZ_SHARED_ENV"))
    if explicit:
        out.append(pathlib.Path(explicit))
    for root in _engine_roots():
        out.append(root.joinpath(*SHARED_SUBPATH, SHARED_FILE_NAME))
    return out


def _load_shared_env() -> dict[str, str]:
    """Lit le premier fichier d'equipe trouve, filtre par la liste blanche.

    Ne leve jamais : un fichier d'equipe absent, illisible ou mal rempli ne
    doit pas empecher le connecteur de tourner sur la configuration du poste.
    Ce qui a ete refuse est garde de cote pour que yooz_status le dise.

    Ce chargeur ne passe **pas** par _guard_not_synced, et c'est voulu : ce
    fichier vit dans un dossier synchronise par construction, et depuis le
    2026-08-28 il porte deliberement les identifiants d'application de l'equipe.
    _guard_not_synced continue de proteger ce qui est PROPRE au poste - le
    yooz.env local, le magasin de refresh tokens renouveles - qui n'a, lui,
    aucune raison de monter sur le drive.

    Les valeurs lues ne sont PAS versees dans os.environ : elles restent dans ce
    cache, et c'est _env qui va les chercher en dernier recours. Elles
    n'apparaissent donc pas dans l'environnement du processus, ni dans ce qu'un
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
            raw = _parse_env_text(path.read_text(encoding="utf-8-sig"))
        except OSError:
            continue
        for key, val in raw.items():
            if _shared_key_allowed(key):
                if val.strip():
                    values[key] = val.strip()
            else:
                rejected.append(key)
        _SHARED_LOADED_FROM = str(path)
        break
    _SHARED_REJECTED = rejected
    _SHARED_CACHE = values
    return values


def _shared_report() -> list[str]:
    """Les lignes que yooz_status affiche sur le fichier d'equipe."""
    shared = _load_shared_env()
    lines: list[str] = []
    if _SHARED_LOADED_FROM:
        lines.append(f"Config d'equipe         : {_SHARED_LOADED_FROM}")
        lines.append(
            "  reglages repris       : "
            + (
                ", ".join(sorted(k for k in shared if not _shared_key_is_secret(k)))
                or "(aucun)"
            )
        )
        masques = sorted(k for k in shared if _shared_key_is_secret(k))
        if masques:
            lines.append(
                "  dont identifiants     : "
                + ", ".join(masques)
                + " (valeurs jamais affichees ; retenues seulement si ce poste "
                "n'en definit pas)"
            )
    else:
        lines.append("Config d'equipe         : aucune (valeurs par defaut du serveur)")
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
            "  Dans la quasi-totalite des cas, c'est une faute de frappe dans le "
            "nom de la variable, ou une societe absente de YOOZ_COMPANIES : "
            "compare avec .env.example. Une cle ignoree ne produit aucune erreur "
            "ailleurs - c'est ici, et seulement ici, que ca se voit."
        )
    return lines


def _env(name: str, default: str = "") -> str:
    """La valeur retenue pour un reglage, dans un ordre de priorite fixe.

    1. l'environnement du processus - la configuration du plugin ;
    2. le fichier local hors du vault - le mode direct, et les secrets ;
    3. le fichier d'equipe de 08_ENGINE - ce que l'equipe a decide ;
    4. la valeur par defaut du serveur.

    Le poste passe donc devant l'equipe, et non l'inverse : un reglage
    d'equipe est un point de depart commun, pas une contrainte. Depuis le
    2026-08-28, le niveau 3 fournit aussi les identifiants d'application de
    l'equipe : voir la liste blanche de _load_shared_env.
    """
    name = name.upper()
    from_process = _clean(os.environ.get(name))
    if from_process:
        return from_process
    from_local = (_load_env_file().get(name) or "").strip()
    if from_local:
        return from_local
    from_team = _load_shared_env().get(name, "").strip()
    if from_team:
        return from_team
    return default.strip()


def _base_url() -> str:
    return _env("YOOZ_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (text or "").upper()).strip("_")


class Company:
    """Une societe Yooz : une application, un jeu d'identifiants, un rapport."""

    def __init__(self, key: str) -> None:
        self.key = key.lower()
        p = f"YOOZ_{_slug(key)}_"
        self.label = _env(p + "LABEL") or key
        self.client_id = _env(p + "CLIENT_ID")
        self.client_secret = _env(p + "CLIENT_SECRET")
        self.refresh_token = _env(p + "REFRESH_TOKEN")
        self.application_id = _env(p + "APPLICATION_ID")
        self.report_id = _env(p + "REPORT_ID") or _env("YOOZ_DEFAULT_REPORT_ID")

    @property
    def missing(self) -> list[str]:
        out = []
        for name, val in (
            ("CLIENT_ID", self.client_id),
            ("CLIENT_SECRET", self.client_secret),
            ("REFRESH_TOKEN", self.refresh_token),
            ("APPLICATION_ID", self.application_id),
        ):
            if not val:
                out.append(f"YOOZ_{_slug(self.key)}_{name}")
        return out


def _companies() -> dict[str, Company]:
    raw = _env("YOOZ_COMPANIES")
    if not raw:
        raise ConfigError(
            "YOOZ_COMPANIES manquant. Renseigne la liste des societes dans "
            f"{_env_file()} (ex. YOOZ_COMPANIES=distriservice,vente_unique), "
            "puis un bloc de quatre variables par societe. Modele : .env.example."
        )
    keys = [k.strip() for k in re.split(r"[,;\s]+", raw) if k.strip()]
    return {k.lower(): Company(k) for k in keys}


def _resolve_companies(company: str) -> list[Company]:
    all_c = _companies()
    wanted = (company or "").strip().lower()
    if wanted in ("", "all", "*", "toutes"):
        return list(all_c.values())
    out = []
    for key in re.split(r"[,;\s]+", wanted):
        if not key:
            continue
        if key not in all_c:
            raise ConfigError(
                f"Societe inconnue : '{key}'. Societes configurees : "
                f"{', '.join(all_c) or '(aucune)'}."
            )
        out.append(all_c[key])
    return out


def _one_company(company: str) -> Company:
    comps = _resolve_companies(company)
    if not comps:
        raise ConfigError("Aucune societe configuree.")
    return comps[0]


def _report_id(company: Company, override: str = "") -> str:
    rid = (override or company.report_id or "").strip()
    if not rid:
        raise ConfigError(
            f"Aucun rapport cible pour {company.label}. Passe report_id (yooz_reports "
            f"les liste), ou fixe YOOZ_{_slug(company.key)}_REPORT_ID (ou "
            f"YOOZ_DEFAULT_REPORT_ID) dans {_env_file()}."
        )
    return rid


def _drop_columns() -> set[str]:
    raw = _env("YOOZ_DROP_COLUMNS")
    if raw:
        return {c.strip() for c in re.split(r"[,;\s]+", raw) if c.strip()}
    return set(DEFAULT_DROP_COLUMNS)


# --------------------------------------------------------------------------
# Authentification : refresh token offline -> access token
# --------------------------------------------------------------------------

_TOKENS: dict[str, tuple[str, float]] = {}


def _token_store_path() -> pathlib.Path:
    path = _local_root() / "refresh_tokens.json"
    _guard_not_synced(path, "Le magasin de refresh tokens")
    return path


def _stored_refresh(key: str) -> str:
    """Refresh token renouvele par le serveur, s'il y en a un.

    Keycloak peut rendre un nouveau refresh token a chaque echange. S'il n'est
    pas conserve, la configuration finit par se perimer en silence.
    """
    path = _token_store_path()
    if not path.exists():
        return ""
    try:
        return (json.loads(path.read_text(encoding="utf-8")).get(key) or "").strip()
    except Exception:
        return ""


def _store_refresh(key: str, token: str) -> None:
    path = _token_store_path()
    data: dict[str, str] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if data.get(key) == token:
        return
    data[key] = token
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _refresh_key(company: Company) -> str:
    return f"YOOZ_{_slug(company.key)}_REFRESH_TOKEN"


def _refresh_origin(company: Company) -> str:
    """D'ou vient le jeton effectivement presente a Yooz.

    Sert dans le message d'erreur d'authentification, ou c'est la question qui
    fait perdre le plus de temps : un jeton d'equipe se corrige dans 08_ENGINE
    pour tout le monde, un jeton de poste dans /plugin pour soi seul, et un
    jeton renouvele localement se corrige en vidant le magasin.
    """
    key = _refresh_key(company)
    if _stored_refresh(company.key):
        return (
            f"le magasin local des jetons renouvelles, {_token_store_path()} "
            "(supprime l'entree pour repartir du jeton configure)"
        )
    if _clean(os.environ.get(key)):
        return "la configuration du plugin, champ « refresh token » de cette societe"
    if (_load_env_file().get(key) or "").strip():
        return f"le fichier local lu au demarrage ({_ENV_SOURCE})"
    if _load_shared_env().get(key):
        return f"le fichier d'equipe ({_SHARED_LOADED_FROM})"
    return "aucune source identifiee - appelle yooz_status"


def _refresh_from_team(company: Company) -> bool:
    """Le jeton presente vient-il du fichier d'equipe ?"""
    key = _refresh_key(company)
    if _stored_refresh(company.key):
        return False
    if _clean(os.environ.get(key)):
        return False
    if (_load_env_file().get(key) or "").strip():
        return False
    return bool(_load_shared_env().get(key))


def _get_token(company: Company, force: bool = False) -> str:
    cached = _TOKENS.get(company.key)
    if cached and not force and cached[1] > time.time() + 30:
        return cached[0]

    missing = company.missing
    if missing:
        raise ConfigError(
            f"Configuration incomplete pour {company.label} : "
            f"{', '.join(missing)} manquant(s) dans {_env_file()}."
        )

    refresh = _stored_refresh(company.key) or company.refresh_token
    url = f"{_base_url()}/auth/realms/yooz/protocol/openid-connect/token"
    try:
        resp = httpx.post(
            url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "refresh_token",
                "client_id": company.client_id,
                "client_secret": company.client_secret,
                "refresh_token": refresh,
                "scope": "offline_access",
            },
            timeout=TOKEN_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"Appel du service de jeton Yooz impossible : {exc}") from exc

    if resp.status_code >= 400:
        body = (resp.text or "")[:600]
        hint = ""
        if "invalid_grant" in body:
            hint = (
                "\nLe refresh token est revoque, expire, ou n'appartient pas a ce "
                "client_id. Il faut en regenerer un dans Yooz et le remettre a "
                f"l'endroit d'ou il vient : {_refresh_origin(company)}."
            )
            if _refresh_from_team(company):
                hint += (
                    "\n\nCAUSE LA PLUS PROBABLE ICI : ce refresh token vient du "
                    "fichier d'equipe, et Keycloak le fait TOURNER a chaque "
                    "echange. Le premier poste qui s'en sert recoit un nouveau "
                    "jeton, le range chez lui, et celui du fichier partage n'est "
                    "plus valable pour les autres. Un refresh token ne se "
                    "partage donc durablement que si la rotation est desactivee "
                    "cote Yooz pour ce client - a verifier avec l'administrateur "
                    "Yooz. Sinon : laisse le client_secret dans le fichier "
                    "d'equipe (lui ne tourne pas) et fais saisir le refresh "
                    "token par chacun dans /plugin."
                )
        elif "unauthorized_client" in body or "invalid_client" in body:
            hint = "\nLe couple client_id / client_secret est refuse par Yooz."
        raise AuthError(
            f"Echec de l'authentification Yooz pour {company.label} : "
            f"HTTP {resp.status_code} {body}{hint}"
        )

    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise AuthError(
            f"Reponse de jeton inattendue pour {company.label} : {str(data)[:300]}"
        )
    new_refresh = (data.get("refresh_token") or "").strip()
    if new_refresh and new_refresh != refresh:
        _store_refresh(company.key, new_refresh)
    ttl = float(data.get("expires_in") or 300)
    _TOKENS[company.key] = (token, time.time() + ttl - 60)
    return token


# --------------------------------------------------------------------------
# Appels API
# --------------------------------------------------------------------------

def _api_get(company: Company, path: str, params: dict[str, Any] | None = None) -> Any:
    url = path if path.startswith("http") else f"{_base_url()}{path}"

    def call() -> httpx.Response:
        return httpx.get(
            url,
            params=params,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {_get_token(company)}",
                "applicationId": company.application_id,
            },
            timeout=API_TIMEOUT_S,
        )

    resp = call()
    if resp.status_code == 401:
        # Access token perime plus tot que prevu : on rejoue une fois.
        _get_token(company, force=True)
        resp = call()

    if resp.status_code >= 400:
        body = (resp.text or "")[:1200]
        if "EMPTY_OR_BAD_APPLICATION_ID" in body:
            raise YoozError(
                f"Yooz refuse l'applicationId de {company.label} (403 "
                f"EMPTY_OR_BAD_APPLICATION_ID). L'en-tete applicationId est celui "
                f"de l'application Yooz, et il differe du client_id."
            )
        if resp.status_code == 404:
            raise YoozError(
                f"404 sur {url} ({company.label}). Sur un data report, cela veut "
                f"dire report_id inconnu de cette application - chaque societe a "
                f"son propre identifiant de rapport (yooz_reports les liste) - et "
                f"non un probleme de droits."
            )
        raise YoozError(f"HTTP {resp.status_code} sur GET {url} ({company.label})\n{body}")

    if not resp.content:
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        return {"_raw": resp.text[:2000]}


def _api_get_bytes(company: Company, path: str, params: dict[str, Any] | None = None) -> bytes:
    """Variante binaire, pour le telechargement d'un fichier d'export."""
    url = f"{_base_url()}{path}"
    resp = httpx.get(
        url,
        params=params,
        headers={
            "Authorization": f"Bearer {_get_token(company)}",
            "applicationId": company.application_id,
        },
        timeout=API_TIMEOUT_S,
    )
    if resp.status_code == 401:
        _get_token(company, force=True)
        resp = httpx.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {_get_token(company)}",
                "applicationId": company.application_id,
            },
            timeout=API_TIMEOUT_S,
        )
    if resp.status_code >= 400:
        raise YoozError(
            f"HTTP {resp.status_code} sur GET {url} ({company.label})\n"
            f"{(resp.text or '')[:800]}"
        )
    return resp.content


def _rows_of(payload: Any) -> list[dict]:
    """Extrait la liste de lignes d'une reponse de data report."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        # Certaines reponses enveloppent la liste : on prend la premiere liste
        # de dictionnaires trouvee plutot que de supposer un nom de champ.
        for value in payload.values():
            if isinstance(value, list) and (not value or isinstance(value[0], dict)):
                return [r for r in value if isinstance(r, dict)]
    return []


def _iter_report_pages(
    company: Company,
    report_id: str,
    since: str,
    page_size: int,
    max_pages: int,
):
    """Pagine le data report, page par page.

    pageOffset est un **numero de page** (documentation Yooz : "si vous voulez la
    3e page, precisez pageOffset=2"), pas un decalage en lignes. pageSize est
    plafonne a 1 000 par l'API.
    """
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    for page in range(max_pages):
        params = {"pageOffset": str(page), "pageSize": str(page_size)}
        if since:
            params["lastExecutionDatetime"] = since
        rows = _rows_of(_api_get(company, REPORT_PATH.format(report_id=report_id), params))
        if not rows:
            return
        yield rows
        if len(rows) < page_size:
            return


# --------------------------------------------------------------------------
# Aplatissement des objets Yooz (referentiels, unites d'organisation)
# --------------------------------------------------------------------------

def _flatten_deep(obj: Any, prefix: str = "", out: dict[str, Any] | None = None) -> dict[str, Any]:
    """Aplatit un objet Yooz imbrique en chemins pointes lisibles.

    Les objets de referentiel arrivent sous la forme
    data.dataBlocks.<BLOC>.<champ>.value : on retire les enveloppes 'data',
    'dataBlocks' et 'value', qui n'apportent rien a la lecture.
    """
    if out is None:
        out = {}
    if isinstance(obj, dict):
        # Enveloppe a un seul champ : on la traverse sans l'ecrire.
        for wrapper in ("value", "data", "dataBlocks"):
            if set(obj.keys()) == {wrapper}:
                return _flatten_deep(obj[wrapper], prefix, out)
        for key, val in obj.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            if key in ("data", "dataBlocks"):
                child = prefix
            _flatten_deep(val, child, out)
        return out
    if isinstance(obj, list):
        if not obj:
            return out
        if all(not isinstance(x, (dict, list)) for x in obj):
            out[prefix] = ", ".join(str(x) for x in obj)
            return out
        for i, val in enumerate(obj):
            _flatten_deep(val, f"{prefix}[{i}]", out)
        return out
    if obj not in (None, "", []):
        out[prefix] = obj
    return out


def _render_object(payload: Any, title: str, max_lines: int = 120) -> str:
    flat = _flatten_deep(payload)
    if not flat:
        return f"{title}\n  (objet vide)"
    lines = [title]
    for i, (key, val) in enumerate(sorted(flat.items())):
        if i >= max_lines:
            lines.append(f"  [... {len(flat) - max_lines} champ(s) de plus]")
            break
        lines.append(f"  {key:<58} {_shorten(val, 300)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Cache SQLite local
# --------------------------------------------------------------------------

def _db_path() -> pathlib.Path:
    raw = _env("YOOZ_CACHE_DB")
    path = pathlib.Path(raw) if raw else _local_root() / "yooz_cache.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_of(dataset: str) -> str:
    """Nom de table pour un jeu de donnees. Un data report = une table."""
    name = re.sub(r"[^a-z0-9_]+", "_", (dataset or DEFAULT_DATASET).strip().lower()).strip("_")
    if not name:
        name = DEFAULT_DATASET
    if name.startswith("sqlite") or name.startswith("_"):
        raise YoozError(f"Nom de jeu de donnees refuse : '{dataset}'.")
    return name


def _open_rw() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _open_ro() -> sqlite3.Connection:
    path = _db_path()
    if not path.exists():
        raise YoozError(
            "Le cache local est vide : lance d'abord yooz_sync (ou "
            "'python server.py sync') pour rapatrier un rapport."
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '\\_%' ESCAPE '\\'"
        )
    ]


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(table)})")]
    except sqlite3.Error:
        return []


def _flatten(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value


def _to_number(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        # Yooz rend les montants en texte, parfois avec virgule decimale.
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _row_key(row: dict, source: str) -> str:
    for candidate in ("id", "yoozNumber"):
        val = row.get(candidate)
        if val:
            return f"{source}|{row.get('orgUnitCode') or ''}|{val}"
    return f"{source}|" + json.dumps(row, sort_keys=True, ensure_ascii=False)[:400]


def _ensure_table(conn: sqlite3.Connection, table: str, columns: list[str]) -> None:
    existing = _table_columns(conn, table)
    if not existing:
        cols = ", ".join(
            f"{_quote(c)} {'REAL' if c in NUMERIC_COLUMNS else 'TEXT'}" for c in columns
        )
        conn.execute(
            f"CREATE TABLE {_quote(table)} "
            f"(_row_key TEXT PRIMARY KEY, source_app TEXT, _synced_at TEXT"
            + (f", {cols}" if cols else "")
            + ")"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {_quote('idx_' + table + '_source')} "
            f"ON {_quote(table)} (source_app)"
        )
        return
    for col in columns:
        if col not in existing:
            kind = "REAL" if col in NUMERIC_COLUMNS else "TEXT"
            conn.execute(f"ALTER TABLE {_quote(table)} ADD COLUMN {_quote(col)} {kind}")


def _upsert(conn: sqlite3.Connection, table: str, rows: list[dict], company: Company) -> int:
    drop = _drop_columns()
    prepared: list[dict] = []
    for row in rows:
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if key in drop or key in ("_row_key", "_synced_at", "source_app"):
                continue
            clean[key] = _to_number(value) if key in NUMERIC_COLUMNS else _flatten(value)
        # Cle de rapprochement avec les lignes de facture, comme dans le
        # script Power Query : orgUnitCode & "-" & yoozNumber.
        if row.get("orgUnitCode") is not None or row.get("yoozNumber") is not None:
            clean["keyToInvoiceLines"] = (
                f"{row.get('orgUnitCode') or ''}-{row.get('yoozNumber') or ''}"
            )
        prepared.append(clean)

    columns: list[str] = []
    for row in prepared:
        for key in row:
            if key not in columns:
                columns.append(key)
    _ensure_table(conn, table, columns)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    all_cols = ["_row_key", "source_app", "_synced_at"] + columns
    sql = (
        f"INSERT OR REPLACE INTO {_quote(table)} "
        f"({', '.join(_quote(c) for c in all_cols)}) "
        f"VALUES ({', '.join('?' for _ in all_cols)})"
    )
    payload = [
        [_row_key(src, company.label), company.label, now] + [row.get(c) for c in columns]
        for row, src in zip(prepared, rows)
    ]
    conn.executemany(sql, payload)
    return len(payload)


def _meta_get(conn: sqlite3.Connection, key: str) -> str:
    try:
        cur = conn.execute("SELECT value FROM _meta WHERE key = ?", (key,))
    except sqlite3.Error:
        return ""
    row = cur.fetchone()
    return (row[0] if row else "") or ""


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO _meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _sync_company(
    company: Company,
    dataset: str,
    report_id: str,
    since: str,
    page_size: int,
    max_pages: int,
) -> dict[str, Any]:
    table = _table_of(dataset)
    started = datetime.now(timezone.utc)
    conn = _open_rw()
    try:
        pages = 0
        written = 0
        for rows in _iter_report_pages(company, report_id, since, page_size, max_pages):
            written += _upsert(conn, table, rows, company)
            pages += 1
        _meta_set(
            conn,
            f"watermark:{table}:{company.key}",
            started.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
        _meta_set(
            conn, f"last_sync:{table}:{company.key}", started.isoformat(timespec="seconds")
        )
        _meta_set(conn, f"report:{table}:{company.key}", report_id)
        conn.commit()
        total = 0
        if _table_columns(conn, table):
            total = conn.execute(
                f"SELECT COUNT(*) FROM {_quote(table)} WHERE source_app = ?",
                (company.label,),
            ).fetchone()[0]
    finally:
        conn.close()
    return {
        "societe": company.label,
        "table": table,
        "rapport": report_id,
        "depuis": since or "(sans lastExecutionDatetime)",
        "pages": pages,
        "lignes_rapatriees": written,
        "lignes_en_cache": total,
        "tronque": pages >= max_pages,
    }


# --------------------------------------------------------------------------
# Restitution
# --------------------------------------------------------------------------

def _shorten(value: Any, limit: int) -> Any:
    if isinstance(value, str) and limit and len(value) > limit:
        return value[:limit] + " [...]"
    return value


def _to_csv(
    rows: list[dict],
    max_rows: int | None = None,
    cell_limit: int = 300,
    columns: list[str] | None = None,
) -> str:
    if not rows:
        return "(0 ligne)"
    cols = list(columns or [])
    if not cols:
        for row in rows:
            for key in row:
                if key not in cols:
                    cols.append(key)
    shown = rows if max_rows is None else rows[:max_rows]
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=",", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(cols)
    for row in shown:
        writer.writerow([_shorten(row.get(c, ""), cell_limit) for c in cols])
    out = buf.getvalue().rstrip("\n")
    if max_rows is not None and len(rows) > max_rows:
        out += (
            f"\n\n[tronque : {len(rows)} lignes, {max_rows} affichees. Affine le "
            f"filtre, ou passe par yooz_export_csv pour tout recuperer.]"
        )
    else:
        out += f"\n\n[{len(shown)} ligne(s)]"
    return out


_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
    re.I,
)


def _guard_sql(sql: str) -> str:
    stripped = re.sub(r"--[^\n]*", " ", sql or "")
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.S).strip().rstrip(";").strip()
    if not stripped:
        raise YoozError("Requete SQL vide.")
    if ";" in stripped:
        raise YoozError("Une seule instruction SQL a la fois.")
    head = stripped.lstrip("(").lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise YoozError("Seules les requetes SELECT (ou WITH ... SELECT) sont acceptees.")
    if _FORBIDDEN_SQL.search(stripped):
        raise YoozError(
            "Mot-cle d'ecriture detecte : ce serveur est en lecture seule sur le cache."
        )
    return stripped


def _select(sql: str, params: tuple = (), limit: int = 200) -> list[dict]:
    conn = _open_ro()
    try:
        cur = conn.execute(f"SELECT * FROM ({sql}) LIMIT {int(limit) + 1}", params)
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        raise YoozError(f"SQL refuse par SQLite : {exc}\nRequete : {sql}") from exc
    finally:
        conn.close()


def _fail(exc: Exception) -> str:
    return f"ECHEC ({type(exc).__name__}) : {exc}"


def _iso_date(value: str, name: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", value):
        raise YoozError(f"{name} doit etre une date ISO (AAAA-MM-JJ), recu '{value}'.")
    return value


def _kind_of(kind: str) -> str:
    key = (kind or "").strip().lower()
    if key in REFERENTIAL_KINDS:
        return REFERENTIAL_KINDS[key]
    upper = (kind or "").strip().upper()
    if upper in REFERENTIAL_KINDS.values():
        return upper
    raise YoozError(
        f"Famille de referentiel inconnue : '{kind}'. Choix : "
        f"{', '.join(sorted(REFERENTIAL_KINDS))}."
    )


# --------------------------------------------------------------------------
# Serveur MCP
# --------------------------------------------------------------------------

mcp = FastMCP("yooz-factures")


@mcp.tool()
def yooz_status() -> str:
    """Etat de la configuration Yooz, des societes, du jeton et du cache local.

    A appeler en premier en cas d'erreur : dit quel fichier d'environnement est
    lu, quelles societes sont configurees, ce qu'il manque, et ou en est le
    cache (jeux de donnees, lignes, date de derniere synchro). N'affiche jamais
    de secret.
    """
    lines: list[str] = []
    _load_env_file()
    in_plugin = bool(_clean(os.environ.get("CLAUDE_PLUGIN_DATA")))
    lines.append(
        "Mode                    : "
        + ("plugin Claude Code" if in_plugin else "direct (ligne de commande)")
    )
    lines.append(f"Configuration lue       : {_ENV_SOURCE}")
    if not _load_env_file():
        for path in _candidate_env_files():
            lines.append(f"    {'present' if path.exists() else 'absent '} {path}")
    lines.extend(_shared_report())
    lines.append(f"Interpreteur            : {sys.executable}")
    lines.append(f"Racine locale           : {_local_root()}")
    lines.append(f"Base URL                : {_base_url()}")
    lines.append(f"Cache SQLite            : {_db_path()}")
    lines.append(f"Colonnes ecartees       : {', '.join(sorted(_drop_columns())) or '(aucune)'}")
    packaged = _packaged_python()
    if packaged:
        lines.append(f"Interpreteur empaquete  : {packaged}")
    # Le cas mesure le 2026-08-27 : lance par l'alias Python du Microsoft Store,
    # le serveur ne voit pas un fichier pose sous %LOCALAPPDATA% par PowerShell,
    # alors que le meme interpreteur le voit lance directement. On ne pretend pas
    # detecter la cause a coup sur : on signale la possibilite, avec la sortie.
    if not _load_env_file() and _legacy_root().is_dir():
        lines.append("")
        lines.append(
            f"ATTENTION : {_legacy_root()} existe mais aucun yooz.env n'y est visible."
        )
        lines.append(
            "  Un fichier pose sous %LOCALAPPDATA% peut etre INVISIBLE pour cet "
            "interpreteur (virtualisation des interpreteurs Windows empaquetes), "
            "sans aucune erreur."
        )
        lines.append(
            "  Sorties, dans l'ordre : la configuration du plugin (immunisee), un "
            f"fichier dans {_local_root()}, ou YOOZ_ENV_FILE vers un chemin explicite."
        )
    lines.append("")

    try:
        companies = _companies()
    except ConfigError as exc:
        lines.append(_fail(exc))
        return "\n".join(lines)

    for comp in companies.values():
        lines.append(f"[{comp.key}] {comp.label}")
        lines.append(f"  applicationId : {comp.application_id or '(manquant)'}")
        lines.append(
            f"  client_id     : {(comp.client_id[:8] + '...') if comp.client_id else '(manquant)'}"
        )
        lines.append(f"  secret        : {'renseigne' if comp.client_secret else 'MANQUANT'}")
        lines.append(f"  refresh token : {'renseigne' if comp.refresh_token else 'MANQUANT'}")
        lines.append(f"  jeton presente: {_refresh_origin(comp)}")
        lines.append(f"  rapport       : {comp.report_id or '(manquant)'}")
        if comp.missing:
            lines.append(f"  -> a completer : {', '.join(comp.missing)}")
            lines.append(
                "     Le cas normal est que tout vienne du fichier d'equipe : si "
                "la ligne « Config d'equipe » ci-dessus dit « aucune », c'est "
                "que 08_ENGINE/04_mcp/00_config n'est pas atteignable depuis ce "
                "poste. Synchronise la bibliotheque, ou pointe-la avec "
                "VU_ENGINE_DIR / YOOZ_SHARED_ENV, plutot que de tout ressaisir."
            )
            continue
        try:
            _get_token(comp, force=True)
            lines.append("  jeton         : OK")
        except (AuthError, ConfigError) as exc:
            lines.append(f"  jeton         : ECHEC - {exc}")
            continue
        try:
            rows = _rows_of(
                _api_get(
                    comp,
                    REPORT_PATH.format(report_id=_report_id(comp)),
                    {"pageOffset": "0", "pageSize": "1", "lastExecutionDatetime": SINCE_FLOOR},
                )
            )
            lines.append(f"  appel API     : OK ({len(rows)} ligne(s) sur un test a 1)")
        except (YoozError, ConfigError) as exc:
            lines.append(f"  appel API     : ECHEC - {exc}")

    lines.append("")
    db = _db_path()
    if not db.exists():
        lines.append("Cache : absent. Lance yooz_sync pour le constituer.")
        return "\n".join(lines)
    conn = _open_ro()
    try:
        tables = _tables(conn)
        if not tables:
            lines.append("Cache : fichier present mais aucun jeu de donnees. Lance yooz_sync.")
            return "\n".join(lines)
        lines.append("Cache :")
        for table in tables:
            cols = _table_columns(conn, table)
            lines.append(f"  [{table}] {len(cols)} colonnes")
            for row in conn.execute(
                f"SELECT source_app, COUNT(*) n, MAX(_synced_at) at "
                f"FROM {_quote(table)} GROUP BY source_app"
            ):
                lines.append(
                    f"    {row['source_app']:<20} {row['n']:>7} lignes, "
                    f"dernier ecrit {row['at']}"
                )
    finally:
        conn.close()
    return "\n".join(lines)


# --- 1. L'historique : rapatriement et interrogation du cache ---------------

@mcp.tool()
def yooz_sync(
    company: str = "all",
    dataset: str = DEFAULT_DATASET,
    mode: str = "full",
    since: str = "",
    report_id: str = "",
    page_size: int = MAX_PAGE_SIZE,
    max_pages: int = 200,
) -> str:
    """Rapatrie un data report Yooz dans le cache local : c'est le chemin de l'historique.

    company   cle de societe ('distriservice'), plusieurs separees par une
              virgule, ou 'all'.
    dataset   nom du jeu de donnees en cache, une table par rapport. 'factures'
              par defaut ; utilise un autre nom pour un autre rapport (lignes de
              facture, commandes...) afin de ne pas melanger deux structures.
    mode      'full' (defaut) rapatrie tout l'historique ; 'delta' ne demande
              que ce qui a change depuis la derniere synchro ; 'brut' n'envoie
              pas de lastExecutionDatetime et laisse le rapport decider.
    since     force une borne ISO (2026-07-01) au lieu du mode.
    report_id force un autre rapport que celui configure (yooz_reports les liste).

    Sur 'delta', une reserve a connaitre : d'apres la documentation Yooz,
    lastExecutionDatetime ne filtre **que si le data report porte lui-meme un
    filtre sur cette date**. Si ce n'est pas le cas, 'delta' rend le meme volume
    que 'full' - sans risque de perte, mais sans gain. Le cache etant en
    INSERT OR REPLACE, un 'full' est toujours juste et ne cree pas de doublon.
    """
    try:
        table = _table_of(dataset)
        mode = (mode or "full").strip().lower()
        if since:
            bound = since.strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", bound):
                bound = _iso_date(bound, "since")[:10] + "T00:00:00.000Z"
        elif mode in ("brut", "raw", "none"):
            bound = ""
        elif mode == "delta":
            bound = ""  # resolu par societe juste apres
        elif mode in ("full", "complet", "all"):
            bound = SINCE_FLOOR
        else:
            raise YoozError(f"mode inconnu : '{mode}'. Attendu full, delta ou brut.")

        out: list[str] = []
        for comp in _resolve_companies(company):
            effective = bound
            if mode == "delta" and not since:
                conn = _open_rw()
                try:
                    effective = _meta_get(conn, f"watermark:{table}:{comp.key}") or SINCE_FLOOR
                finally:
                    conn.close()
            res = _sync_company(
                comp,
                table,
                _report_id(comp, report_id),
                effective,
                int(page_size),
                int(max_pages),
            )
            line = (
                f"{res['societe']} -> [{res['table']}] {res['lignes_rapatriees']} ligne(s) "
                f"sur {res['pages']} appel(s), depuis {res['depuis']} -> "
                f"{res['lignes_en_cache']} en cache"
            )
            if res["tronque"]:
                line += f"  [ARRET sur max_pages={max_pages} : il reste des pages]"
            out.append(line)
        return "\n".join(out) or "(aucune societe)"
    except (ConfigError, AuthError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_tables() -> str:
    """Jeux de donnees presents dans le cache local, avec leur rapport d'origine.

    Utile quand plusieurs rapports ont ete rapatries : dit quelle table
    interroger avec yooz_sql.
    """
    try:
        conn = _open_ro()
        try:
            tables = _tables(conn)
            if not tables:
                return "Cache vide. Lance yooz_sync."
            lines: list[str] = []
            for table in tables:
                total = conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
                lines.append(
                    f"[{table}] {total} ligne(s), {len(_table_columns(conn, table))} colonnes"
                )
                for row in conn.execute(
                    "SELECT key, value FROM _meta WHERE key LIKE ?", (f"report:{table}:%",)
                ):
                    lines.append(f"    rapport {row['key'].split(':')[-1]:<16} {row['value']}")
                for row in conn.execute(
                    "SELECT key, value FROM _meta WHERE key LIKE ?", (f"last_sync:{table}:%",)
                ):
                    lines.append(f"    synchro {row['key'].split(':')[-1]:<16} {row['value']}")
            return "\n".join(lines)
        finally:
            conn.close()
    except (ConfigError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_columns(dataset: str = DEFAULT_DATASET, fill_rate: bool = True) -> str:
    """Colonnes d'un jeu de donnees du cache, avec leur taux de remplissage.

    A lire avant d'ecrire du SQL : les noms viennent du data report Yooz et ne
    sont pas devinables (YZ_DATE_YZ_COMMONS, CUSTOM_1_YZ_INVOICE...).
    """
    try:
        table = _table_of(dataset)
        conn = _open_ro()
        try:
            cols = _table_columns(conn, table)
            if not cols:
                return f"Jeu de donnees '{table}' absent du cache. Lance yooz_sync, ou yooz_tables."
            total = conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
            lines = [f"Table {table} : {total} ligne(s), {len(cols)} colonne(s)", ""]
            if not fill_rate or not total:
                lines.extend(cols)
                return "\n".join(lines)
            expr = ", ".join(
                f"SUM(CASE WHEN {_quote(c)} IS NULL OR {_quote(c)} = '' THEN 0 ELSE 1 END)"
                for c in cols
            )
            counts = conn.execute(f"SELECT {expr} FROM {_quote(table)}").fetchone()
            for col, filled in zip(cols, counts):
                kind = "num" if col in NUMERIC_COLUMNS else "txt"
                lines.append(f"  {col:<52} {kind}  {100 * (filled or 0) / total:5.1f}% rempli")
            return "\n".join(lines)
        finally:
            conn.close()
    except (ConfigError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_sql(sql: str, max_rows: int = 100) -> str:
    """Interroge le cache en SQL (SQLite, lecture seule).

    Table par defaut : 'factures' (yooz_tables liste les autres). Colonnes
    utiles : source_app (societe), orgUnitCode / orgUnitName, yoozNumber,
    YZ_NUMBER_YZ_COMMONS (numero fournisseur), thirdPartyName,
    YZ_DATE_YZ_COMMONS (date de facture), YZ_DUE_DATE_YZ_COMMONS (echeance),
    amount / taxAmount / totalAmount (numeriques), blockedBoolean,
    blockingCause, YZ_PORTAL_STATUS. yooz_columns donne la liste complete.

    Exemple : SELECT thirdPartyName, SUM(totalAmount) t, COUNT(*) n
              FROM factures WHERE YZ_DATE_YZ_COMMONS >= '2026-07-01'
              GROUP BY 1 ORDER BY t DESC
    """
    try:
        clean = _guard_sql(sql)
        rows = _select(clean, limit=int(max_rows))
        return _to_csv(rows, max_rows=int(max_rows))
    except (ConfigError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_invoices(
    company: str = "",
    third_party: str = "",
    org_unit: str = "",
    number: str = "",
    date_from: str = "",
    date_to: str = "",
    amount_min: float = 0.0,
    amount_max: float = 0.0,
    blocked: str = "",
    dataset: str = DEFAULT_DATASET,
    max_rows: int = 50,
) -> str:
    """Recherche de factures dans le cache, sans ecrire de SQL.

    Tous les filtres sont optionnels et se cumulent. third_party, org_unit et
    number sont des recherches partielles, insensibles a la casse. blocked
    accepte 'oui' / 'non' (vide = les deux). Les dates portent sur la date de
    facture (YZ_DATE_YZ_COMMONS), au format AAAA-MM-JJ.
    """
    try:
        table = _table_of(dataset)
        where: list[str] = []
        params: list[Any] = []
        if company:
            labels = [c.label for c in _resolve_companies(company)]
            where.append("source_app IN (" + ",".join("?" for _ in labels) + ")")
            params.extend(labels)
        if third_party:
            where.append(
                "LOWER(COALESCE(thirdPartyName,'') || ' ' || COALESCE(thirdPartyCode,'')) LIKE ?"
            )
            params.append(f"%{third_party.lower()}%")
        if org_unit:
            where.append(
                "LOWER(COALESCE(orgUnitName,'') || ' ' || COALESCE(orgUnitCode,'')) LIKE ?"
            )
            params.append(f"%{org_unit.lower()}%")
        if number:
            where.append(
                "LOWER(COALESCE(yoozNumber,'') || ' ' || COALESCE(YZ_NUMBER_YZ_COMMONS,'')) LIKE ?"
            )
            params.append(f"%{number.lower()}%")
        if date_from:
            where.append("YZ_DATE_YZ_COMMONS >= ?")
            params.append(_iso_date(date_from, "date_from"))
        if date_to:
            where.append("YZ_DATE_YZ_COMMONS <= ?")
            params.append(_iso_date(date_to, "date_to"))
        if amount_min:
            where.append("totalAmount >= ?")
            params.append(float(amount_min))
        if amount_max:
            where.append("totalAmount <= ?")
            params.append(float(amount_max))
        flag = (blocked or "").strip().lower()
        if flag in ("oui", "yes", "true", "1"):
            where.append("LOWER(COALESCE(blockedBoolean,'')) IN ('true','1','oui','yes')")
        elif flag in ("non", "no", "false", "0"):
            where.append("LOWER(COALESCE(blockedBoolean,'')) NOT IN ('true','1','oui','yes')")

        conn = _open_ro()
        try:
            available = _table_columns(conn, table)
        finally:
            conn.close()
        if not available:
            return f"Jeu de donnees '{table}' absent du cache. Lance yooz_sync."
        cols = [c for c in SUMMARY_COLUMNS if c in available]

        sql = (
            f"SELECT {', '.join(_quote(c) for c in cols)} FROM {_quote(table)}"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY YZ_DATE_YZ_COMMONS DESC"
        )
        rows = _select(sql, tuple(params), limit=int(max_rows))
        return _to_csv(rows, max_rows=int(max_rows), columns=cols)
    except (ConfigError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_invoice(number: str, company: str = "", dataset: str = DEFAULT_DATASET) -> str:
    """Detail complet d'une facture du cache, par numero Yooz ou numero fournisseur.

    Rend tous les champs non vides du rapport pour la ou les factures qui
    correspondent. Point de depart du controle d'une facture transporteur.
    """
    try:
        table = _table_of(dataset)
        number = (number or "").strip()
        if not number:
            raise YoozError("Passe un numero (yoozNumber ou numero fournisseur).")
        low = number.lower()
        where = [
            "(LOWER(COALESCE(yoozNumber,'')) = ? OR LOWER(COALESCE(YZ_NUMBER_YZ_COMMONS,'')) = ? "
            "OR LOWER(COALESCE(yoozNumber,'')) LIKE ? "
            "OR LOWER(COALESCE(YZ_NUMBER_YZ_COMMONS,'')) LIKE ?)"
        ]
        params: list[Any] = [low, low, f"%{low}%", f"%{low}%"]
        if company:
            labels = [c.label for c in _resolve_companies(company)]
            where.append("source_app IN (" + ",".join("?" for _ in labels) + ")")
            params.extend(labels)
        rows = _select(
            f"SELECT * FROM {_quote(table)} WHERE " + " AND ".join(where), tuple(params), limit=5
        )
        if not rows:
            return (
                f"Aucune facture pour '{number}' dans le cache. Verifie la synchro "
                f"(yooz_status), ou que la facture est bien dans le perimetre du rapport."
            )
        out: list[str] = []
        for row in rows[:5]:
            out.append(f"--- {row.get('source_app')} / {row.get('yoozNumber')} ---")
            for key, value in row.items():
                if key.startswith("_") or value in (None, ""):
                    continue
                out.append(f"  {key:<50} {_shorten(value, 400)}")
            out.append("")
        return "\n".join(out)
    except (ConfigError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_summary(
    group_by: str = "thirdPartyName",
    company: str = "",
    date_from: str = "",
    date_to: str = "",
    metric: str = "totalAmount",
    dataset: str = DEFAULT_DATASET,
    max_rows: int = 30,
) -> str:
    """Agregation du cache : montants et volumes par tiers, societe, mois, statut.

    group_by  une colonne du cache (thirdPartyName, orgUnitName, source_app,
              currency, YZ_PORTAL_STATUS, blockingCause...), ou 'mois' pour un
              regroupement par mois de facture.
    metric    colonne numerique a sommer (totalAmount par defaut).
    """
    try:
        table = _table_of(dataset)
        conn = _open_ro()
        try:
            available = _table_columns(conn, table)
        finally:
            conn.close()
        if not available:
            return f"Jeu de donnees '{table}' absent du cache. Lance yooz_sync."

        if group_by.lower() in ("mois", "month"):
            expr = "SUBSTR(YZ_DATE_YZ_COMMONS, 1, 7)"
            label = "mois"
        else:
            if group_by not in available:
                return f"Colonne '{group_by}' absente du cache. Lance yooz_columns."
            expr = _quote(group_by)
            label = group_by
        if metric not in available or metric not in NUMERIC_COLUMNS:
            return (
                f"metric '{metric}' n'est pas une colonne numerique du cache. "
                f"Choix : {', '.join(sorted(NUMERIC_COLUMNS & set(available)))}"
            )

        where: list[str] = []
        params: list[Any] = []
        if company:
            labels = [c.label for c in _resolve_companies(company)]
            where.append("source_app IN (" + ",".join("?" for _ in labels) + ")")
            params.extend(labels)
        if date_from:
            where.append("YZ_DATE_YZ_COMMONS >= ?")
            params.append(_iso_date(date_from, "date_from"))
        if date_to:
            where.append("YZ_DATE_YZ_COMMONS <= ?")
            params.append(_iso_date(date_to, "date_to"))

        sql = (
            f"SELECT {expr} AS {_quote(label)}, COUNT(*) AS nb, "
            f"ROUND(SUM({_quote(metric)}), 2) AS {_quote(metric)} "
            f"FROM {_quote(table)}"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " GROUP BY 1 ORDER BY 3 DESC"
        )
        rows = _select(sql, tuple(params), limit=int(max_rows))
        return _to_csv(rows, max_rows=int(max_rows))
    except (ConfigError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_export_csv(sql: str, filename: str = "", max_rows: int = 200000) -> str:
    """Exporte le resultat d'une requete SQL du cache dans un fichier CSV.

    Pour ce qui ne doit pas passer par la fenetre de conversation : un
    rapprochement de facturation complet, une extraction a joindre a un mail.
    Le fichier est ecrit dans YOOZ_EXPORT_DIR (par defaut ~/.yooz-mcp/exports,
    ou le dossier choisi a l'installation du plugin). Rend le chemin du fichier.
    """
    try:
        clean = _guard_sql(sql)
        conn = _open_ro()
        try:
            cur = conn.execute(clean)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(int(max_rows))
        except sqlite3.Error as exc:
            raise YoozError(f"SQL refuse par SQLite : {exc}") from exc
        finally:
            conn.close()

        raw = _env("YOOZ_EXPORT_DIR")
        out_dir = pathlib.Path(raw) if raw else _local_root() / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = (filename or "").strip() or (
            "yooz_export_" + datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".csv"
        )
        if not name.lower().endswith(".csv"):
            name += ".csv"
        path = out_dir / pathlib.Path(name).name
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(cols)
            writer.writerows(rows)
        return f"{len(rows)} ligne(s) ecrite(s) dans {path}"
    except (ConfigError, YoozError) as exc:
        return _fail(exc)


# --- 2. Les petites requetes : appels directs, sans cache -------------------

@mcp.tool()
def yooz_reports(company: str = "") -> str:
    """Liste les data reports de Yooz : identifiant, nom, createur.

    GET /yooz/v2/api/dataReports - un appel, quelques lignes. C'est par la
    qu'on trouve l'identifiant a passer a yooz_report_peek ou yooz_sync quand
    on cherche autre chose que le rapport factures configure (lignes de
    facture, commandes, un rapport monte pour l'occasion).
    """
    try:
        out: list[str] = []
        for comp in _resolve_companies(company):
            payload = _api_get(comp, API_ROOT + "/dataReports")
            rows = _rows_of(payload)
            out.append(f"=== {comp.label} : {len(rows)} rapport(s) ===")
            for r in rows:
                flags = " [multi-tenant]" if r.get("isMultiTenant") in (True, "true") else ""
                out.append(
                    f"  {r.get('reportId')}  {str(r.get('name') or '')[:60]:<60} "
                    f"{str(r.get('creator') or '')[:24]}{flags}"
                )
            if comp.report_id:
                known = any(str(r.get("reportId")) == comp.report_id for r in rows)
                out.append(
                    f"  (rapport configure {comp.report_id} : "
                    f"{'present' if known else 'ABSENT de cette liste'})"
                )
            out.append("")
        return "\n".join(out) or "(aucune societe)"
    except (ConfigError, AuthError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_report_peek(
    company: str = "",
    report_id: str = "",
    page_size: int = 5,
    page: int = 0,
    columns: str = "",
    since: str = "",
) -> str:
    """Lit une page courte d'un data report, en direct, sans toucher au cache.

    La requete la moins couteuse cote Yooz : un appel, page_size lignes (5 par
    defaut, 1 000 au maximum). Trois usages : verifier la fraicheur de la
    donnee, decouvrir les colonnes d'un rapport avant de le rapatrier, lire un
    rapport qu'on ne veut pas mettre en cache.

    columns  liste de colonnes a afficher, separees par des virgules, pour
             garder la sortie courte.
    page     numero de page (pageOffset) : 0 pour la premiere.
    """
    try:
        want = [c.strip() for c in re.split(r"[,;]", columns) if c.strip()]
        out: list[str] = []
        for comp in _resolve_companies(company):
            params = {
                "pageOffset": str(max(0, int(page))),
                "pageSize": str(max(1, min(int(page_size), MAX_PAGE_SIZE))),
            }
            if since:
                params["lastExecutionDatetime"] = since
            rid = _report_id(comp, report_id)
            rows = _rows_of(_api_get(comp, REPORT_PATH.format(report_id=rid), params))
            out.append(f"=== {comp.label} / rapport {rid} / page {params['pageOffset']} ===")
            if rows and not want:
                out.append(f"({len(rows[0])} colonnes dans le rapport)")
            out.append(_to_csv(rows, max_rows=int(page_size), columns=want or None))
            out.append("")
        return "\n".join(out) or "(aucune societe)"
    except (ConfigError, AuthError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_referential(
    kind: str = "fournisseur",
    referential: str = "",
    code: str = "",
    limit: int = 25,
    offset: int = 0,
    count: bool = False,
    company: str = "",
) -> str:
    """Interroge un referentiel Yooz : fournisseurs, comptes, causes de blocage...

    C'est la petite requete efficace du connecteur. Trois formes, de la plus
    legere a la plus lourde :

      code renseigne  -> un seul element, un appel : la fiche d'un fournisseur
                         par son code (GET .../data/{code}).
      count=True      -> juste le nombre d'elements du referentiel.
      sinon           -> une page de donnees, **100 lignes au maximum** (limite
                         de l'API), avec offset pour paginer.

    kind         famille : fournisseur, client, compte, cause_blocage,
                 cause_refus, categorie_facture, devise, mode_paiement,
                 profil_tva, journal, periode_comptable, article, unite_mesure,
                 immobilisation, adresse, compte_bancaire...
    referential  code du referentiel dans la famille. Laisse vide pour lister
                 les referentiels disponibles (premier appel a faire).
    """
    try:
        data_type = _kind_of(kind)
        comp = _one_company(company)
        base = f"{API_ROOT}/{data_type}/referentials"

        if not referential:
            payload = _api_get(comp, base)
            names = payload if isinstance(payload, list) else _rows_of(payload)
            lines = [
                f"=== {comp.label} / {data_type} : {len(names)} referentiel(s) ===",
                "Passe l'un de ces codes en parametre 'referential'.",
            ]
            lines += [f"  {n if isinstance(n, str) else json.dumps(n, ensure_ascii=False)}" for n in names]
            return "\n".join(lines)

        if count:
            payload = _api_get(comp, f"{base}/{referential}/count")
            return f"{comp.label} / {data_type} / {referential} : {payload}"

        if code:
            payload = _api_get(comp, f"{base}/{referential}/data/{code}")
            return _render_object(
                payload, f"=== {comp.label} / {data_type} / {referential} / {code} ==="
            )

        capped = max(1, min(int(limit), MAX_REFERENTIAL_LIMIT))
        payload = _api_get(
            comp,
            f"{base}/{referential}/data",
            {"limit": str(capped), "offset": str(max(0, int(offset)))},
        )
        items = payload if isinstance(payload, list) else _rows_of(payload)
        head = (
            f"=== {comp.label} / {data_type} / {referential} : {len(items)} element(s) "
            f"(offset {offset}, limite API {MAX_REFERENTIAL_LIMIT}) ==="
        )
        flat = [_flatten_deep(it) for it in items]
        return head + "\n" + _to_csv(flat, max_rows=capped, cell_limit=60)
    except (ConfigError, AuthError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_org_units(code: str = "", company: str = "", dimension_set: bool = False) -> str:
    """Unites d'organisation Yooz : la liste, ou la fiche d'une unite par son code.

    Sans code : GET /orgUnits, la liste des unites du contexte. Avec code : la
    fiche complete de l'unite (GET /orgUnits/{code}), ou son jeu de dimensions
    si dimension_set=True. Sert a relier un orgUnitCode d'une facture (7000,
    7005...) a l'entite qui la porte.
    """
    try:
        comp = _one_company(company)
        if not code:
            payload = _api_get(comp, API_ROOT + "/orgUnits")
            flat = _flatten_deep(payload)
            lines = [f"=== {comp.label} : unites d'organisation ==="]
            for key, val in sorted(flat.items()):
                lines.append(f"  {key:<48} {_shorten(val, 200)}")
            return "\n".join(lines)
        suffix = "/dimensionSet" if dimension_set else ""
        payload = _api_get(comp, f"{API_ROOT}/orgUnits/{code}{suffix}")
        return _render_object(payload, f"=== {comp.label} / orgUnit {code}{suffix} ===")
    except (ConfigError, AuthError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_document_types(tags: str = "", company: str = "") -> str:
    """Types de document disponibles dans l'application Yooz.

    GET /documentTypes, filtrable par tags separes par des virgules. Dit ce que
    l'application sait porter (facture, avoir, commande...), donc ce qu'un data
    report peut contenir.
    """
    try:
        comp = _one_company(company)
        params = {"tags": tags} if (tags or "").strip() else None
        payload = _api_get(comp, API_ROOT + "/documentTypes", params)
        items = payload if isinstance(payload, list) else _rows_of(payload)
        lines = [f"=== {comp.label} : {len(items)} type(s) de document ==="]
        for it in items:
            flat = _flatten_deep(it)
            keep = {k: v for k, v in flat.items() if re.search(r"code|name|tag", k, re.I)}
            lines.append("  " + "; ".join(f"{k}={v}" for k, v in sorted(keep.items())) or "  (vide)")
        return "\n".join(lines)
    except (ConfigError, AuthError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_exports(
    company: str = "",
    org_unit: str = "",
    already_downloaded: str = "",
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Liste les resultats d'export Yooz (les fichiers comptables generes).

    Sans org_unit : les exports consolides (GET /exportResults, avec limit /
    offset / alreadyDownloaded). Avec org_unit : les exports de cette unite.
    Rend generatedFileId, nom, code d'export et le drapeau 'deja telecharge'.

    already_downloaded : 'oui', 'non', ou vide pour tout.
    """
    try:
        comp = _one_company(company)
        if org_unit:
            payload = _api_get(comp, f"{API_ROOT}/orgUnits/{org_unit}/exportResults")
            params_note = f"unite {org_unit}"
        else:
            params: dict[str, Any] = {
                "limit": str(max(1, int(limit))),
                "offset": str(max(0, int(offset))),
            }
            flag = (already_downloaded or "").strip().lower()
            if flag in ("oui", "yes", "true", "1"):
                params["alreadyDownloaded"] = "true"
            elif flag in ("non", "no", "false", "0"):
                params["alreadyDownloaded"] = "false"
            payload = _api_get(comp, API_ROOT + "/exportResults", params)
            params_note = f"consolides, offset {offset}"
        items = payload if isinstance(payload, list) else _rows_of(payload)
        head = f"=== {comp.label} / exports ({params_note}) : {len(items)} fichier(s) ==="
        return head + "\n" + _to_csv(items, max_rows=int(limit))
    except (ConfigError, AuthError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_export_download(
    generated_file_id: str,
    company: str = "",
    filename: str = "",
    mark_as_downloaded: bool = False,
) -> str:
    """Telecharge un fichier d'export Yooz sur le poste, sans le marquer comme lu.

    Attention, c'est le seul appel du serveur qui peut avoir un effet dans
    Yooz : par defaut, cette API **marque l'export comme telecharge**, ce qui
    peut faire sauter le fichier a l'integration comptable qui le consomme. Le
    serveur envoie donc ignoreMarkAsDownloaded=true par defaut. Ne mets
    mark_as_downloaded=True que si tu veux deliberement consommer le fichier.
    """
    try:
        comp = _one_company(company)
        fid = str(generated_file_id).strip()
        if not fid.isdigit():
            raise YoozError("generated_file_id doit etre l'identifiant numerique rendu par yooz_exports.")
        content = _api_get_bytes(
            comp,
            f"{API_ROOT}/exportResults/{fid}",
            {"ignoreMarkAsDownloaded": "false" if mark_as_downloaded else "true"},
        )
        raw = _env("YOOZ_EXPORT_DIR")
        out_dir = pathlib.Path(raw) if raw else _local_root() / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = pathlib.Path((filename or f"yooz_export_{fid}.dat").strip()).name
        path = out_dir / name
        path.write_bytes(content)
        flag = "MARQUE comme telecharge" if mark_as_downloaded else "non marque comme telecharge"
        return f"{len(content)} octet(s) ecrits dans {path} ({flag})"
    except (ConfigError, AuthError, YoozError) as exc:
        return _fail(exc)


@mcp.tool()
def yooz_api_get(path: str, company: str = "", query: str = "") -> str:
    """Appel GET brut sur l'API Yooz, pour un chemin non couvert par les outils.

    Lecture seule : seul GET est possible, aucun POST / PUT / DELETE. La surface
    de l'API v2 est documentee dans le README du connecteur.

    path   chemin de l'API, ex. /yooz/v2/api/users
    query  parametres au format JSON, ex. {"limit": "10"}
    """
    try:
        path = (path or "").strip()
        if not path.startswith("/"):
            raise YoozError("path doit commencer par '/', ex. /yooz/v2/api/orgUnits")
        params = json.loads(query) if (query or "").strip() else None
        comp = _one_company(company)
        payload = _api_get(comp, path, params)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(text) > 12000:
            text = text[:12000] + "\n[... tronque]"
        return f"=== {comp.label} GET {path} ===\n{text}"
    except (ConfigError, AuthError, YoozError, json.JSONDecodeError) as exc:
        return _fail(exc)


# --------------------------------------------------------------------------
# Modes ligne de commande
# --------------------------------------------------------------------------

def _cli_doctor() -> int:
    report = yooz_status()
    print(report)
    return 0 if "appel API     : OK" in report else 1


def _cli_sync() -> int:
    args = sys.argv[2:]
    company = args[0] if args else "all"
    mode = args[1] if len(args) > 1 else "full"
    dataset = args[2] if len(args) > 2 else DEFAULT_DATASET
    print(yooz_sync(company=company, mode=mode, dataset=dataset))
    return 0


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if arg in ("doctor", "sync"):
        try:
            return _cli_doctor() if arg == "doctor" else _cli_sync()
        except (ConfigError, AuthError, YoozError) as exc:
            print(_fail(exc))
            return 1
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
