#!/usr/bin/env python3
"""
MCP indices-eu : les indices europeens qui font bouger un tarif de transport.

Trois series, trois sources publiques, aucune authentification :

  gazole            Weekly Oil Bulletin de la Commission europeenne. Prix a la
                    pompe, TTC et hors taxes, 29 pays de l'Union plus les
                    moyennes UE et zone euro, depuis 2005. C'est la source d'une
                    clause de surcharge carburant.
  inflation         Eurostat prc_hicp_minr - IPCH harmonise, mensuel. Taux
                    annuel, taux mensuel, moyenne glissante 12 mois, indice.
                    C'est la source d'une clause d'indexation contractuelle.
  salaire_minimum   Eurostat earn_mw_cur - salaire minimum legal national,
                    semestriel, en euros, en monnaie nationale ou en standard
                    de pouvoir d'achat.

Trois usages :
    python server.py            # mode serveur MCP (stdio) - lance par Claude Code
    python server.py doctor     # diagnostic de configuration et de source
    python server.py --help

LECTURE SEULE par construction : les trois sources ne sont qu'un fichier et une
API de diffusion, il n'existe aucune operation d'ecriture a exposer. Le seul
disque touche est la racine locale du connecteur : son cache et ses exports.

CE QUE CE CONNECTEUR NE FAIT PAS. Il ne calcule aucune surcharge carburant, ne
revalorise aucune grille et n'applique aucune clause. Il rend la donnee publique
et son millesime. La formule, elle, est dans le contrat du transporteur -
02_TRANSPORTEURS/<NOM>/02_tarif/ - et deux transporteurs n'ont pas la meme.

CONFIGURATION, EN DEUX COUCHES, comme les autres connecteurs de l'equipe. Ce qui
est identique sur tous les postes - URL du bulletin petrolier, plafonds, duree
de cache - vit dans 08_ENGINE/04_mcp/00_config/indices.shared.env. Ce qui est
propre a un poste vit dans indices.env, hors du vault. Le poste passe devant
l'equipe. Aucun secret nulle part : les trois sources sont ouvertes.
"""

from __future__ import annotations

import csv
import datetime as dt
import functools
import io
import json
import logging
import os
import pathlib
import re
import socket
import ssl
import sys
import time
import unicodedata
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# --------------------------------------------------------------------------
# Les sources
# --------------------------------------------------------------------------

# Le classeur du Weekly Oil Bulletin. L'URL porte un identifiant de document que
# la Commission fait tourner de temps en temps, et ce jour-la le connecteur rend
# un 404 franc. C'est pour ca qu'elle est surchargeable par INDICES_GASOIL_URL,
# et qu'elle a sa place dans le fichier d'equipe : elle se corrige une fois pour
# les vingt-six postes.
DEFAULT_WOB_URL = (
    "https://energy.ec.europa.eu/document/download/"
    "906e60ca-8b6a-44e7-8589-652854d2fd3f_en"
    "?filename=Weekly_Oil_Bulletin_Prices_History_maticni_4web.xlsx"
)

# Les deux feuilles de prix du classeur, et le fragment d'en-tete qui les
# distingue. Une colonne a la forme <CODE>_price_with_tax_<produit>.
WOB_SHEETS = {
    "ttc": ("Prices with taxes", "_price_with_tax_"),
    "ht": ("Prices wo taxes", "_price_wo_tax_"),
}

# Les six produits du bulletin, et leur unite. L'unite n'est pas decorative : le
# gazole est en euros par 1000 litres, le fioul lourd en euros par tonne. Les
# melanger dans un meme tableau donne un ecart de facteur mille.
WOB_PRODUITS = {
    "diesel": ("Gazole routier (gas oil automobile)", "EUR/1000 l"),
    "euro95": ("Essence sans plomb 95", "EUR/1000 l"),
    "heating_oil": ("Fioul domestique", "EUR/1000 l"),
    "fuel_oil_1": ("Fioul lourd, soufre <= 1%", "EUR/t"),
    "fuel_oil_2": ("Fioul lourd, soufre > 1%", "EUR/t"),
    "LPG": ("GPL carburant", "EUR/1000 l"),
}

# --------------------------------------------------------------------------
# Les sources NATIONALES, pour les trois pays que le bulletin europeen ne
# couvre pas ou plus
# --------------------------------------------------------------------------
#
# Le bulletin petrolier ne couvre que l'Union, et le Royaume-Uni y est FIGE au
# 2020-12-21. Trois pays de livraison de Vente-unique restaient donc sans prix
# du carburant : GB, CH, NO. Ce bloc en recupere deux au prix reel, et le
# troisieme en indice (voir HICP_POSTES plus bas).
#
# Ces series ne sont PAS dans la meme unite ni a la meme frequence que le
# bulletin : pence par litre et par semaine pour le Royaume-Uni, couronnes par
# litre et par mois pour la Norvege, contre euros par 1000 litres et par
# semaine pour l'Union. Chaque ligne porte donc son unite, sa frequence et sa
# source, et le connecteur NE CONVERTIT RIEN : convertir supposerait un taux de
# change a la date, c'est-a-dire fabriquer un chiffre. Ce qui se compare d'un
# pays a l'autre, c'est la VARIATION en pourcentage - elle est sans unite.

# Royaume-Uni. L'URL du CSV porte un identifiant d'asset que gov.uk fait
# tourner a CHAQUE publication hebdomadaire - le figer serait garantir un 404
# sous huit jours. L'API de contenu de gov.uk, elle, est stable et donne l'URL
# courante : c'est par elle qu'on passe. Le bulletin europeen n'a pas cet
# equivalent, d'ou INDICES_GASOIL_URL et sa place dans le fichier d'equipe.
DESNZ_CONTENT_API = (
    "https://www.gov.uk/api/content/government/statistics/weekly-road-fuel-prices"
)

# Les deux carburants routiers du releve DESNZ, et le produit du bulletin
# europeen auquel ils correspondent. Le fioul domestique, les fiouls lourds et
# le GPL ne sont PAS dans ce releve : une question sur ces produits au
# Royaume-Uni doit dire qu'elle n'a pas de source, pas rendre une ligne vide.
DESNZ_PRODUITS = {"diesel": "ULSD", "euro95": "ULSP"}
DESNZ_UNITE = "GBp/l"

# Norvege. PxWebApi v2 de SSB, table 09654. Le piege est dans la selection :
# sans valueCodes explicites sur les TROIS variables, l'API rend les 13 derniers
# mois - avec un HTTP 200 et sans un mot. On demande donc tout, explicitement.
SSB_TABLE_URL = (
    "https://data.ssb.no/api/pxwebapi/v2-beta/tables/09654/data"
    "?lang=en&format=json-stat2"
    "&valueCodes[PetroleumProd]=*&valueCodes[ContentsCode]=*&valueCodes[Tid]=*"
)
SSB_PRODUITS = {"035": "diesel", "031": "euro95"}
SSB_UNITE = "NOK/l"

# Royaume-Uni, salaire minimum. gov.uk publie les taux dans un TABLEAU HTML,
# pas dans une serie : c'est une page, lue par l'API de contenu. Deux
# consequences a ne pas masquer. Le taux est HORAIRE quand Eurostat publie un
# montant MENSUEL, et il s'applique a partir d'AVRIL quand Eurostat decoupe en
# semestres. Le connecteur ne convertit pas : passer de l'un a l'autre suppose
# une duree de travail hebdomadaire, donc un chiffre invente.
NMW_CONTENT_API = "https://www.gov.uk/api/content/national-minimum-wage-rates"
NMW_UNITE = "GBP/heure"

# Quel pays est servi par quelle source nationale quand le bulletin ne l'a pas.
# La Suisse n'y est pas : aucune API federale de prix du carburant n'existe -
# la division "prix" est absente de l'API PX-Web de l'OFS, energiedashboard.
# admin.ch est une application web sans API derriere, et opendata.swiss n'en
# porte qu'une republication cantonale. Verifie le 2026-09-02. Pour la Suisse,
# la reponse est l'indice IPCH CP0722.
GASOIL_SOURCES_NATIONALES = {"UK": "desnz", "NO": "ssb"}

EUROSTAT_BASE = (
    "https://ec.europa.eu/eurostat/api/dissemination/sdmx/3.0/data/dataflow/ESTAT"
)

# L'inflation. UN SEUL flux, et c'est le point a ne pas rater : prc_hicp_manr
# (taux annuel) et prc_hicp_midx (indice) existent encore, repondent 200, et
# sont FIGES depuis le 2026-02-06 - leur derniere periode est 2025-12. Ils
# portent l'ancienne nomenclature COICOP. Le flux vivant est prc_hicp_minr,
# classe en coicop18, et il porte TOUTES les unites, taux annuel compris.
# Interroger manr aujourd'hui, c'est publier une inflation vieille de huit mois
# sans qu'aucune erreur ne le signale. Releve le 2026-09-01.
HICP_FLOW = "prc_hicp_minr"

# Les postes COICOP interrogeables sur ce flux. `carburants` est ce qui rend la
# SUISSE lisible : le bulletin petrolier ne la couvre pas, mais Eurostat publie
# son IPCH, sous-position carburants comprise. Verifie le 2026-09-02 : CH et NO
# repondent, le Royaume-Uni non (il est sorti de l'IPCH fin 2020).
#
# Ce que ce poste est, et ce qu'il n'est PAS. C'est un INDICE de panier de
# consommation - essence, gazole et lubrifiants ponderes ensemble - MENSUEL.
# Il repond a « de combien a bouge le carburant en Suisse », ce qui suffit a une
# clause d'indexation. Il ne repond PAS a « combien coute le gazole en Suisse »,
# et il ne se compare pas a un prix au litre.
HICP_POSTES = {
    "total": ("TOTAL", "tous postes", "l'inflation d'ensemble"),
    "carburants": (
        "CP0722",
        "carburants et lubrifiants pour vehicules personnels",
        "l'indice carburant - la seule mesure disponible pour la Suisse",
    ),
}
HICP_MESURES = {
    "annuel": ("RCH_A", "Taux de variation annuel, %", "l'inflation au sens courant"),
    "mensuel": ("RCH_M", "Taux de variation mensuel, %", "la variation d'un mois sur l'autre"),
    "moyenne_12m": (
        "RCH_MV12MAVR",
        "Moyenne glissante sur 12 mois, %",
        "ce que visent les clauses d'indexation : elle lisse la saisonnalite",
    ),
    "indice": ("I25", "Indice, 2025 = 100", "le niveau, pour un ecart entre deux dates"),
    "indice_2015": ("I15", "Indice, 2015 = 100", "le niveau en base 2015"),
}

MW_FLOW = "earn_mw_cur"
MW_DEVISES = {
    "EUR": "Euros",
    "NAC": "Monnaie nationale",
    "PPS": "Standard de pouvoir d'achat (SPA)",
}

# Les pays ou Eurostat publie la ligne mais ou il n'y a PAS de salaire minimum
# legal national : le plancher y est fixe par convention collective, branche par
# branche. Une case vide n'y veut pas dire "donnee manquante", elle veut dire
# "cette notion n'existe pas ici". Le script Power Query d'origine comblait ces
# trous par 0 - un salaire minimum a zero euro au Danemark, c'est un chiffre
# faux qui part dans une etude de cout.
SANS_SALAIRE_MINIMUM_LEGAL = ("DK", "IT", "AT", "FI", "SE", "NO", "IS", "CH")

DEFAULT_TIMEOUT_S = 120.0
DEFAULT_CACHE_TTL_H = 24.0
# Depuis quand on rapatrie les series Eurostat. Large : le cout est nul, et une
# question d'indexation remonte volontiers a la signature du contrat.
DEFAULT_DEPUIS = "2010"
RENDER_MAX_CHARS = 24_000
DEFAULT_MAX_LIGNES = 300

LEXIQUE_FILE = pathlib.Path(__file__).resolve().parent / "lexique.json"
SYNCED_MARKERS = ("/onedrive", "cafom", "sharepoint", "dropbox", "google drive")


class ConfigError(RuntimeError):
    """Un reglage manque ou est inutilisable."""


class SourceError(RuntimeError):
    """Une source n'a pas repondu, ou a repondu autre chose que ce qu'on attend."""


# --------------------------------------------------------------------------
# Configuration : le poste, puis l'equipe, puis le defaut du serveur
# --------------------------------------------------------------------------

def _is_synced(path: pathlib.Path) -> bool:
    flat = str(path).replace("\\", "/").lower()
    return any(marker in flat for marker in SYNCED_MARKERS)


def _local_root() -> pathlib.Path:
    """Racine locale du connecteur.

    PAS %LOCALAPPDATA% : un Python empaquete (Microsoft Store, Python Manager)
    donne a ses processus enfants une vue VIRTUALISEE de ce dossier - le plugin
    ecrit d'un cote, le terminal lit de l'autre, sans erreur. Le profil
    utilisateur n'est pas virtualise, et il existe des deux cotes.
    """
    return pathlib.Path.home() / ".indices-mcp"


def _parse_env(text: str) -> dict[str, str]:
    """Parseur .env minimal : KEY=VALUE, # en commentaire, guillemets optionnels.

    Une URL porte des `?`, des `&` et parfois un `#`. La valeur n'est donc
    jamais coupee sur un `#` colle, et des guillemets la preservent telle quelle.
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


_ENV_DONE = False
_ENV_LOADED_FROM = ""
_ENV_LOAD_ERROR = ""


def _candidate_env_files() -> list[pathlib.Path]:
    """Le fichier du poste, essaye dans l'ordre.

    Il s'appelle `indices.env`, pas `.env` : un fichier nomme `.env` a deja ete
    ignore en silence chez Yooz par un serveur qui cherchait `yooz.env`. Le
    fichier porte le nom du connecteur, et le meme nom a tous les emplacements.
    """
    explicit = (os.environ.get("INDICES_ENV_FILE") or "").strip()
    if explicit:
        return [pathlib.Path(explicit)]
    out: list[pathlib.Path] = []
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        out.append(pathlib.Path(plugin_data) / "indices.env")
    out.append(_local_root() / "indices.env")
    out.append(pathlib.Path(__file__).resolve().parent / "indices.env")
    return out


def _load_env_file() -> None:
    """Charge le premier fichier exploitable. Une variable NON VIDE du process gagne.

    Le "non vide" n'est pas un detail : en mode plugin la configuration arrive
    par substitution, et un champ laisse vide pose une variable vide. Un
    setdefault la prendrait pour une valeur, et le fichier du poste serait
    ignore sans un mot.

    Lu en utf-8-sig : un BOM non consomme colle trois octets invisibles devant
    le premier nom de variable, qui devient introuvable.
    """
    global _ENV_DONE, _ENV_LOADED_FROM, _ENV_LOAD_ERROR
    if _ENV_DONE:
        return
    _ENV_DONE = True
    for path in _candidate_env_files():
        try:
            if not path.is_file():
                continue
            values = _parse_env(path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            _ENV_LOAD_ERROR = f"Lecture impossible de {path} : {exc}"
            continue
        for key, val in values.items():
            if not os.environ.get(key):
                os.environ[key] = val
        _ENV_LOADED_FROM = str(path)
        return


SHARED_FILE_NAME = "indices.shared.env"
ENGINE_DIR_NAME = "08_ENGINE"
SHARED_SUBPATH = ("04_mcp", "00_config")

# Ce que le fichier d'equipe a le droit de fixer. La liste blanche n'est pas une
# barriere - il n'y a aucun secret ici - c'est un garde-fou contre la faute de
# frappe : une variable mal orthographiee dans un fichier partage serait sinon
# ignoree en silence sur vingt-six postes a la fois.
SHARED_ALLOWED = frozenset(
    {
        "INDICES_GASOIL_URL",
        "INDICES_GASOIL_TLS",
        "INDICES_EUROSTAT_BASE",
        "INDICES_DESNZ_API",
        "INDICES_SSB_URL",
        "INDICES_NMW_API",
        "INDICES_PAYS_DEFAUT",
        "INDICES_DEPUIS",
        "INDICES_TIMEOUT_S",
        "INDICES_CACHE_TTL_H",
        "INDICES_MAX_LIGNES",
    }
)

# Connues du serveur, mais qui n'ont rien a faire dans un fichier partage : un
# chemin valable sur un poste n'existe pas sur les vingt-cinq autres.
SHARED_LOCAL_ONLY = frozenset(
    {
        "INDICES_EXPORT_DIR",
        "INDICES_CACHE_DIR",
        "INDICES_ENV_FILE",
        "INDICES_SHARED_ENV",
        "INDICES_GASOIL_FILE",
        "INDICES_MCP_VENV",
        "VU_ENGINE_DIR",
    }
)

_SHARED_CACHE: dict[str, str] | None = None
_SHARED_LOADED_FROM = ""
_SHARED_REJECTED: list[str] = []
_SHARED_LOCAL_SEEN: list[str] = []


def _engine_roots() -> list[pathlib.Path]:
    """Racines `08_ENGINE` plausibles, dans l'ordre de preference.

    Le plugin est publie depuis `08_ENGINE/03_plugins/`, mais Claude Code en
    fait une copie sous `~/.claude/plugins/` : remonter depuis le code ne suffit
    donc pas. On remonte quand meme - cas de l'installation directe - et on
    complete par le profil utilisateur, ou OneDrive pose la bibliotheque.
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
    """Un chemin explicite est EXCLUSIF : il desactive la recherche du fichier reel.

    Sans ca, pointer un fichier de recette ne le mettrait qu'en premier, et le
    fichier d'equipe de production repondrait encore en second - ce qui a deja
    fait passer une suite de controles hors-ligne avec les vraies valeurs.
    """
    explicit = (os.environ.get("INDICES_SHARED_ENV") or "").strip()
    if explicit:
        return [pathlib.Path(explicit)]
    return [r.joinpath(*SHARED_SUBPATH, SHARED_FILE_NAME) for r in _engine_roots()]


def _load_shared_env() -> dict[str, str]:
    """Lit le premier fichier d'equipe trouve. Ne leve jamais.

    Un fichier absent, illisible ou mal rempli ne doit pas empecher le
    connecteur de tourner : les trois sources sont publiques, il n'a besoin de
    rien pour demarrer.
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
            if upper in SHARED_ALLOWED:
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


def _env(name: str, default: str = "") -> str:
    """La valeur retenue, dans un ordre de priorite fixe.

    1. l'environnement du processus - la configuration du plugin, et le
       indices.env du poste que _load_env_file y a deja verse ;
    2. le fichier d'equipe de 08_ENGINE ;
    3. le defaut du serveur.

    Le poste passe devant l'equipe : un reglage d'equipe est un point de depart
    commun, pas une contrainte.
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
        return float(_env("INDICES_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _ttl_hours() -> float:
    try:
        return max(0.0, float(_env("INDICES_CACHE_TTL_H") or DEFAULT_CACHE_TTL_H))
    except ValueError:
        return DEFAULT_CACHE_TTL_H


def _max_lignes_defaut() -> int:
    try:
        return max(1, int(_env("INDICES_MAX_LIGNES") or DEFAULT_MAX_LIGNES))
    except ValueError:
        return DEFAULT_MAX_LIGNES


def _cache_dir() -> pathlib.Path:
    raw = _env("INDICES_CACHE_DIR")
    return pathlib.Path(raw) if raw else _local_root() / "cache"


def _export_dir() -> pathlib.Path:
    """Ou atterrissent les CSV. Jamais dans la bibliotheque d'equipe.

    Le dossier de donnees du plugin n'est pas utilise : il depend de
    l'identifiant d'installation, et un poste peut en porter deux - un export
    ecrit sous l'un serait introuvable sous l'autre.
    """
    raw = _env("INDICES_EXPORT_DIR")
    return pathlib.Path(raw) if raw else _local_root() / "exports"


def _log(message: str) -> None:
    """Journal sur stderr. JAMAIS stdout : le protocole MCP y parle JSON-RPC."""
    print(f"[indices-mcp] {message}", file=sys.stderr, flush=True)


# httpx journalise chaque requete en INFO. Ce n'est pas dangereux - le journal
# part sur stderr - mais ca noie les trois lignes qui disent vraiment quelque
# chose, et l'URL d'une requete Eurostat fait deux cents caracteres.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Le cache : trois fichiers JSON, pas une base
# --------------------------------------------------------------------------
#
# Le classeur du bulletin petrolier pese 4,4 Mo et se republie une fois par
# semaine ; les series Eurostat se republient une fois par mois. Le retelecharger
# a chaque question serait long sans rien rendre de neuf. Le cache est donc un
# fichier par serie, avec l'heure de rapatriement et le millesime rendu par la
# source. Pas de SQLite : il n'y a aucune jointure a faire ici, et une base a
# maintenir pour trois series serait de l'outillage pour l'outillage.

def _cache_path(key: str) -> pathlib.Path:
    return _cache_dir() / f"{key}.json"


def _cache_read(key: str, ttl_h: float | None = None) -> dict[str, Any] | None:
    """Rend l'entree de cache si elle est encore fraiche. Sinon None."""
    path = _cache_path(key)
    try:
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ttl = _ttl_hours() if ttl_h is None else ttl_h
    if ttl <= 0:
        return payload
    age_h = (time.time() - float(payload.get("fetched_at_epoch") or 0)) / 3600.0
    if age_h > ttl:
        return None
    return payload


def _cache_write(key: str, payload: dict[str, Any]) -> None:
    path = _cache_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload["fetched_at_epoch"] = time.time()
        payload["fetched_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError) as exc:
        _log(f"cache non ecrit ({path}) : {exc}")


def _cache_stamp(key: str) -> str:
    path = _cache_path(key)
    try:
        if not path.is_file():
            return "absent"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "illisible"
    stamp = payload.get("fetched_at") or "?"
    age_h = (time.time() - float(payload.get("fetched_at_epoch") or 0)) / 3600.0
    return f"{stamp} (il y a {age_h:.1f} h)"


# --------------------------------------------------------------------------
# HTTP, et le certificat casse du bulletin petrolier
# --------------------------------------------------------------------------
#
# INCIDENT DU 2026-09-01, CLOS LE 2026-09-02. Ce jour-la, energy.ec.europa.eu
# presentait un certificat Amazon emis pour `europa.eu` et `*.europa.eu`
# seulement. Un joker ne couvre qu'UN label : `*.europa.eu` vaut pour
# `ec.europa.eu` et PAS pour `energy.ec.europa.eu`, donc la verification echouait
# sur "Hostname mismatch". Ce n'etait ni le poste ni le proxy d'entreprise -
# l'emetteur etait bien Amazon : une erreur de rotation cote Commission.
#
# Verifie le 2026-09-02 : la Commission a corrige. Le classeur se telecharge en
# mode strict, certificat verifie, et c'est ce que fait le connecteur sans aucun
# reglage. IL N'Y A PLUS RIEN A ARMER - si une page te dit le contraire, elle
# n'a pas ete relue.
#
# Le repli reste en place, parce que la rotation qui a casse une fois recassera :
# le code essaie STRICT d'abord et ne bascule que sur un echec de verification de
# certificat, jamais de lui-meme. Le message d'erreur du mode strict dit alors
# quoi faire. Ne le desarme pas, et ne l'arme pas d'avance.
#
# INDICES_GASOIL_TLS=chaine-seule leve le controle du nom d'hote, MAIS :
#   - la chaine de certification reste verifiee (un certificat auto-signe ou
#     expire est toujours refuse) ;
#   - le certificat servi doit couvrir europa.eu, donc appartenir a la
#     Commission. Sans ce second controle, n'importe quel certificat valide pour
#     n'importe quel domaine ferait l'affaire, et un prix de gazole falsifie
#     entrerait directement dans un calcul de surcharge ;
#   - chaque reponse qui s'en sert le DIT, dans l'en-tete du rendu.

def _tls_mode() -> str:
    mode = (_env("INDICES_GASOIL_TLS", "strict") or "strict").strip().lower()
    return mode if mode in {"strict", "chaine-seule"} else "strict"


def _peer_cert_names(host: str, port: int = 443) -> list[str]:
    """Les noms DNS du certificat servi, chaine verifiee, nom d'hote non verifie.

    check_hostname=False avec CERT_REQUIRED : la chaine est bien validee, seul
    l'appariement du nom est mis de cote - c'est ce qu'il faut pour pouvoir dire
    POUR QUI le certificat a ete emis.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    with socket.create_connection((host, port), timeout=min(30.0, _timeout())) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert() or {}
    return [val for key, val in cert.get("subjectAltName", ()) if key == "DNS"]


def _covers_europa(names: list[str]) -> bool:
    return any(n == "europa.eu" or n.endswith(".europa.eu") for n in names)


def _http_get(url: str, ctx: Any = True) -> httpx.Response:
    with httpx.Client(verify=ctx, follow_redirects=True, timeout=_timeout()) as client:
        response = client.get(url, headers={"User-Agent": "vu-indices-mcp/1.0"})
    if response.status_code != 200:
        raise SourceError(
            f"HTTP {response.status_code} sur {url.split('?')[0]}. "
            + (
                "L'URL du bulletin porte un identifiant de document que la "
                "Commission fait tourner : si elle a change, corrige "
                "INDICES_GASOIL_URL dans le fichier d'equipe "
                "(08_ENGINE/04_mcp/00_config/indices.shared.env)."
                if "energy.ec.europa.eu" in url
                else "La source a repondu autre chose qu'un 200."
            )
        )
    return response


# --------------------------------------------------------------------------
# Le bulletin petrolier : telechargement et lecture du classeur
# --------------------------------------------------------------------------

def _wob_workbook_bytes() -> tuple[bytes, str]:
    """Rend le classeur et une phrase qui dit d'ou il vient.

    Trois provenances possibles, dans l'ordre : un fichier local pose a la main
    (INDICES_GASOIL_FILE, le recours quand le reseau ou le certificat bloque),
    le telechargement strict, puis - seulement s'il a ete arme - le
    telechargement a nom d'hote non verifie.
    """
    local = _env("INDICES_GASOIL_FILE")
    if local:
        path = pathlib.Path(local)
        if not path.is_file():
            raise ConfigError(
                f"INDICES_GASOIL_FILE pointe un fichier absent : {path}. "
                "Retire le reglage pour revenir au telechargement."
            )
        return path.read_bytes(), f"fichier local pose a la main : {path}"

    url = _env("INDICES_GASOIL_URL", DEFAULT_WOB_URL)
    host = httpx.URL(url).host
    try:
        return _http_get(url).content, f"telecharge de {host}, certificat verifie"
    except httpx.ConnectError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise SourceError(f"Connexion impossible a {host} : {exc}") from exc
        detail = str(exc)

    # A partir d'ici : echec de verification du certificat, et lui seul.
    try:
        names = _peer_cert_names(host)
    except (OSError, ssl.SSLError) as exc:
        names = []
        detail += f" | lecture du certificat impossible : {exc}"

    if _tls_mode() != "chaine-seule":
        raise SourceError(
            f"Certificat refuse pour {host}. Le certificat servi est emis pour "
            + (", ".join(names) if names else "un domaine indetermine")
            + ". C'est le meme incident que le 2026-09-01, corrige par la "
            "Commission le 2026-09-02 : une rotation de certificat qui ne couvre "
            "que europa.eu et *.europa.eu, et un joker ne vaut que pour UN label "
            "- il ne couvre donc pas energy.ec.europa.eu. Si tu lis ce message, "
            "la rotation a recasse. Deux issues, et aucune n'est de desactiver la "
            "verification en bloc : (1) telecharger le classeur a la main dans un "
            "navigateur et pointer INDICES_GASOIL_FILE dessus ; (2) armer "
            "INDICES_GASOIL_TLS=chaine-seule, qui garde la verification de la "
            "chaine et exige que le certificat couvre europa.eu, mais ne verifie "
            "plus le nom d'hote. Desarme-le des que la source repond en strict. "
            "L'inflation et le salaire minimum ne sont pas concernes : ils "
            f"passent par ec.europa.eu. Detail : {detail}"
        )

    if not _covers_europa(names):
        raise SourceError(
            f"Mode chaine-seule arme, mais le certificat servi par {host} ne "
            "couvre PAS europa.eu (" + (", ".join(names) or "aucun nom lisible")
            + "). Telechargement refuse : en mode chaine-seule, l'appartenance a "
            "la Commission est le seul controle qui reste, on ne le contourne pas."
        )

    ctx = httpx.create_ssl_context()
    ctx.check_hostname = False
    content = _http_get(url, ctx=ctx).content
    return content, (
        f"telecharge de {host} en mode CHAINE-SEULE : chaine de certification "
        "verifiee et certificat emis pour " + ", ".join(names) + ", mais nom "
        "d'hote NON verifie"
    )


def _parse_wob(blob: bytes) -> dict[str, Any]:
    """Lit les deux feuilles de prix et rend une structure en colonnes.

    Forme retenue : {"ttc": {"dates": [...], "series": {"FR": {"diesel": [...]}}}}.
    Des listes alignees sur une liste de dates, plutot que 190 000 lignes
    plates : le fichier de cache passe de 20 Mo a 3 Mo et se relit en 50 ms.

    Le code pays est lu dans le NOM de la colonne (`FR_price_with_tax_diesel`),
    pas dans la valeur de la colonne CTR. Le script Power Query d'origine
    prenait les deux premiers caracteres de CTR : `EU_` et `EUR_` y donnaient
    tous deux "EU", et la moyenne de l'Union ecrasait celle de la zone euro.
    """
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - garde-fou d'installation
        raise ConfigError(
            "Le module openpyxl manque : c'est lui qui lit le classeur du "
            "bulletin petrolier. Relance le serveur, le bootstrap l'installe."
        ) from exc

    try:
        book = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    except Exception as exc:
        raise SourceError(
            "Le fichier telecharge n'est pas un classeur Excel lisible "
            f"({exc}). La Commission a peut-etre change l'URL du document : "
            "voir INDICES_GASOIL_URL."
        ) from exc

    out: dict[str, Any] = {}
    for taxes, (sheet_name, marker) in WOB_SHEETS.items():
        if sheet_name not in book.sheetnames:
            raise SourceError(
                f"Feuille '{sheet_name}' absente du classeur "
                f"(feuilles presentes : {', '.join(book.sheetnames)}). "
                "La structure du bulletin a change."
            )
        sheet = book[sheet_name]
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None) or ()
        next(rows, None)  # ligne de libelle produit
        next(rows, None)  # ligne d'unite
        colonnes: dict[int, tuple[str, str]] = {}
        for pos, value in enumerate(header):
            if not value or marker not in str(value):
                continue
            code, _, produit = str(value).partition(marker)
            code = code.strip().upper()
            if code and produit in WOB_PRODUITS:
                colonnes[pos] = (code, produit)
        if not colonnes:
            raise SourceError(
                f"Aucune colonne '{marker}' dans la feuille '{sheet_name}'. "
                "La nomenclature des en-tetes du bulletin a change."
            )
        dates: list[str] = []
        series: dict[str, dict[str, list[float | None]]] = {}
        for row in rows:
            when = row[0] if row else None
            if not isinstance(when, dt.datetime):
                # Le bas du classeur porte des notes : une ligne sans date n'est
                # pas une observation. On la saute au lieu de la convertir en
                # valeur nulle et de la compter dans les effectifs.
                continue
            dates.append(when.date().isoformat())
            for pos, (code, produit) in colonnes.items():
                value = row[pos] if pos < len(row) else None
                series.setdefault(code, {}).setdefault(produit, []).append(
                    float(value) if isinstance(value, (int, float)) else None
                )
        out[taxes] = {"dates": dates, "series": series}
    book.close()
    return out


def _wob_data(force: bool = False) -> dict[str, Any]:
    entry = None if force else _cache_read("gasoil")
    if entry is not None:
        return entry
    blob, provenance = _wob_workbook_bytes()
    _log(f"bulletin petrolier : {len(blob)} octets, {provenance}")
    parsed = _parse_wob(blob)
    entry = {
        "source": _env("INDICES_GASOIL_URL", DEFAULT_WOB_URL),
        "provenance": provenance,
        "data": parsed,
    }
    _cache_write("gasoil", entry)
    return entry


# --------------------------------------------------------------------------
# Royaume-Uni : le releve hebdomadaire DESNZ
# --------------------------------------------------------------------------

def _desnz_csv_urls() -> tuple[list[str], str]:
    """Les URL courantes des CSV, demandees a l'API de contenu de gov.uk.

    On ne fige pas l'URL d'un asset : gov.uk lui donne un identifiant neuf a
    chaque publication hebdomadaire, donc une URL en dur garantit un 404 sous
    huit jours. C'est le meme piege que l'URL du bulletin europeen, sauf qu'ici
    il existe une API stable pour la resoudre.

    Deux fichiers : 2003-2017 et 2018 a aujourd'hui. On prend les deux, ce qui
    donne une serie qui commence AVANT celle du bulletin europeen (2005).
    """
    api = _env("INDICES_DESNZ_API", DESNZ_CONTENT_API)
    try:
        doc = _http_get(api).json()
    except ValueError as exc:
        raise SourceError(
            f"L'API de contenu de gov.uk n'a pas rendu du JSON : {exc}"
        ) from exc
    pieces = ((doc.get("details") or {}).get("attachments")) or []
    urls = [
        a["url"]
        for a in pieces
        if a.get("content_type") == "text/csv" and a.get("url")
    ]
    if not urls:
        raise SourceError(
            "Aucune piece jointe CSV sur la page DESNZ des prix hebdomadaires. "
            f"gov.uk a change la structure de la page : voir {api}. "
            f"Types de pieces vus : {[a.get('content_type') for a in pieces]}."
        )
    return urls, doc.get("public_updated_at") or ""


def _parse_desnz(textes: list[str]) -> dict[str, Any]:
    """Lit les CSV DESNZ et rend la meme forme que le bulletin europeen.

    Sept colonnes, identiques dans les deux fichiers : la date, le prix a la
    pompe de l'essence et du gazole, le DROIT D'ACCISE de chacun, et le TAUX DE
    TVA de chacun. C'est ce qui permet de reconstituer le hors taxes, que le
    releve ne publie pas directement :

        pompe = (hors taxes + accise) x (1 + tva / 100)

    donc hors taxes = pompe / (1 + tva / 100) - accise. Les deux varient dans
    l'histoire - la TVA est passee de 17,5 % a 20 %, l'accise a bouge plusieurs
    fois - donc le calcul se fait ligne par ligne, jamais avec un taux suppose.

    La date est en jj/mm/aaaa. Lue comme aaaa-mm-jj, le 09/06/2003 passerait
    pour un releve de juin sur certaines lignes et leverait une erreur sur
    d'autres : le genre d'inversion qui decale une serie entiere en silence.
    """
    par_date: dict[str, dict[str, dict[str, float]]] = {}
    for texte in textes:
        lecteur = csv.reader(io.StringIO(texte))
        entete = next(lecteur, None)
        premiere = (entete[0] if entete else "").strip().lstrip("﻿").lower()
        if premiere != "date":
            raise SourceError(
                "Le CSV DESNZ ne commence pas par une colonne 'Date' "
                f"(vu : {entete[:2] if entete else 'fichier vide'}). "
                "La structure du releve a change."
            )
        for ligne in lecteur:
            if len(ligne) < 7 or not (ligne[0] or "").strip():
                continue
            try:
                jour = dt.datetime.strptime(ligne[0].strip(), "%d/%m/%Y").date()
            except ValueError:
                continue
            for produit, (i_prix, i_accise, i_tva) in (
                ("euro95", (1, 3, 5)),
                ("diesel", (2, 4, 6)),
            ):
                try:
                    pompe = float(ligne[i_prix])
                    accise = float(ligne[i_accise])
                    tva = float(ligne[i_tva])
                except (TypeError, ValueError):
                    continue
                ht = pompe / (1.0 + tva / 100.0) - accise
                bloc = par_date.setdefault(jour.isoformat(), {})
                bloc[produit] = {"ttc": round(pompe, 2), "ht": round(ht, 2)}

    dates = sorted(par_date)
    if not dates:
        raise SourceError("Aucune ligne exploitable dans les CSV DESNZ.")
    out: dict[str, Any] = {}
    for taxes in ("ttc", "ht"):
        colonnes: dict[str, list[float | None]] = {}
        for produit in DESNZ_PRODUITS:
            colonnes[produit] = [
                (par_date[d].get(produit) or {}).get(taxes) for d in dates
            ]
        out[taxes] = {"dates": dates, "series": {"UK": colonnes}}
    return out


def _desnz_data(force: bool = False) -> dict[str, Any]:
    entry = None if force else _cache_read("gasoil_uk")
    if entry is not None:
        return entry
    urls, maj = _desnz_csv_urls()
    textes = []
    for url in urls:
        textes.append(_http_get(url).content.decode("utf-8-sig", errors="replace"))
    _log(f"DESNZ : {len(urls)} fichier(s), {sum(len(t) for t in textes)} caracteres")
    parsed = _parse_desnz(textes)
    entry = {
        "source": ", ".join(urls),
        "provenance": (
            "telecharge de gov.uk (DESNZ, Weekly road fuel prices), URL resolue "
            f"par l'API de contenu ; page mise a jour le {maj or 'date inconnue'}"
        ),
        "data": parsed,
    }
    _cache_write("gasoil_uk", entry)
    return entry


# --------------------------------------------------------------------------
# Norvege : la table 09654 de SSB
# --------------------------------------------------------------------------

def _parse_ssb(doc: dict[str, Any]) -> dict[str, Any]:
    """Lit le JSON-stat de SSB et rend la meme forme que le bulletin europeen.

    Deux differences a ne pas gommer : la periode est un MOIS (2026M07) et non
    une semaine, et le prix est celui a la pompe - SSB ne publie pas de hors
    taxes. Le bloc `ht` reste donc VIDE, et une question hors taxes sur la
    Norvege doit le dire au lieu de rendre le TTC en silence.
    """
    dim = doc.get("dimension") or {}
    prods = ((dim.get("PetroleumProd") or {}).get("category") or {}).get("index") or {}
    temps = ((dim.get("Tid") or {}).get("category") or {}).get("index") or {}
    if not prods or not temps:
        raise SourceError(
            "Reponse SSB inattendue : la dimension 'PetroleumProd' ou 'Tid' "
            "manque. La table 09654 a change de structure."
        )
    ordre_t = sorted(temps, key=lambda c: int(temps[c]))
    n_t = len(ordre_t)
    valeurs = doc.get("value") or []

    dates: list[str] = []
    for code in ordre_t:
        annee, _, mois = code.partition("M")
        try:
            dates.append(dt.date(int(annee), int(mois), 1).isoformat())
        except ValueError:
            dates.append("")

    colonnes: dict[str, list[float | None]] = {}
    for code_prod, pos_prod in sorted(prods.items(), key=lambda kv: int(kv[1])):
        produit = SSB_PRODUITS.get(code_prod)
        if not produit:
            continue
        serie: list[float | None] = []
        for ti in range(n_t):
            plat = int(pos_prod) * n_t + ti
            if isinstance(valeurs, dict):
                val = valeurs.get(str(plat), valeurs.get(plat))
            else:
                val = valeurs[plat] if plat < len(valeurs) else None
            serie.append(round(float(val), 2) if val is not None else None)
        colonnes[produit] = serie

    return {
        "ttc": {"dates": dates, "series": {"NO": colonnes}},
        # SSB ne publie pas le hors taxes. Un bloc vide, pas un bloc recopie.
        "ht": {"dates": [], "series": {}},
    }


def _ssb_data(force: bool = False) -> dict[str, Any]:
    entry = None if force else _cache_read("gasoil_no")
    if entry is not None:
        return entry
    url = _env("INDICES_SSB_URL", SSB_TABLE_URL)
    try:
        doc = _http_get(url).json()
    except ValueError as exc:
        raise SourceError(f"SSB n'a pas rendu du JSON : {exc}") from exc
    parsed = _parse_ssb(doc)
    n = len(parsed["ttc"]["dates"])
    _log(f"SSB 09654 : {n} mois")
    if n <= 20:
        # La selection explicite des trois variables est ce qui evite ca : sans
        # elle, l'API rend les 13 derniers mois avec un HTTP 200 et sans un mot.
        # Si on y retombe, il faut le VOIR, pas le decouvrir au milieu d'une
        # etude d'indexation sur dix ans.
        _log(
            f"SSB : seulement {n} periodes rendues. La selection valueCodes "
            "n'a probablement pas ete prise en compte - serie tronquee."
        )
    entry = {
        "source": url,
        "provenance": (
            "telecharge de data.ssb.no (Statistisk sentralbyra, table 09654), "
            f"{n} mois"
        ),
        "millesime": doc.get("updated") or "",
        "data": parsed,
    }
    _cache_write("gasoil_no", entry)
    return entry


# --------------------------------------------------------------------------
# Royaume-Uni : le salaire minimum legal, en taux HORAIRE
# --------------------------------------------------------------------------

_RE_TABLE = re.compile(r"<table.*?</table>", re.S)
_RE_TR = re.compile(r"<tr.*?</tr>", re.S)
_RE_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_RE_BALISE = re.compile(r"<[^>]+>")
_RE_LIVRES = re.compile("£\\s*([0-9]+(?:\\.[0-9]{1,2})?)")
_MOIS_ANGLAIS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _texte_cellule(html: str) -> str:
    return re.sub(r"\s+", " ", _RE_BALISE.sub("", html)).strip()


def _debut_nmw(texte: str) -> dt.date | None:
    """« April 2026 » ou « April 2025 to March 2026 » -> date de prise d'effet.

    On retient le PREMIER couple mois/annee : c'est la prise d'effet. Prendre le
    dernier daterait le taux de sa fin d'application.
    """
    mots = (texte or "").lower().replace(",", " ").split()
    mois = annee = None
    for mot in mots:
        if mois is None and mot in _MOIS_ANGLAIS:
            mois = _MOIS_ANGLAIS[mot]
        elif mois is not None and annee is None and mot.isdigit() and len(mot) == 4:
            annee = int(mot)
            break
    if mois is None or annee is None:
        return None
    try:
        return dt.date(annee, mois, 1)
    except ValueError:
        return None


def _parse_nmw(corps: str) -> list[dict[str, Any]]:
    """Extrait les taux horaires des tableaux HTML de gov.uk.

    Un tableau par periode d'application, avec en tete les tranches d'age et en
    premiere colonne la periode (« April 2026 », « April 2025 to March 2026 »).

    LES TRANCHES D'AGE CHANGENT DANS LE TEMPS - « 25 and over » avant 2021,
    « 23 and over », puis « 21 and over » aujourd'hui. On garde donc la premiere
    tranche, qui est toujours la tranche adulte haute (le National Living Wage),
    ET on rend son libelle : sans lui, on comparerait deux populations
    differentes en croyant suivre une seule serie.
    """
    trouvees: list[dict[str, Any]] = []
    for table in _RE_TABLE.findall(corps):
        rangs = _RE_TR.findall(table)
        if len(rangs) < 2:
            continue
        entete = [t for t in (_texte_cellule(c) for c in _RE_CELL.findall(rangs[0])) if t]
        if not entete:
            continue
        tranche_haute = entete[0]
        for rang in rangs[1:]:
            cellules = [_texte_cellule(c) for c in _RE_CELL.findall(rang)]
            if not cellules:
                continue
            debut = _debut_nmw(cellules[0])
            if debut is None:
                continue
            montants = _RE_LIVRES.findall(" ".join(cellules[1:]))
            if not montants:
                continue
            trouvees.append(
                {
                    "periode_source": cellules[0],
                    "debut": debut,
                    "tranche": tranche_haute,
                    "valeur": float(montants[0]),
                }
            )
    # Une periode peut apparaitre dans deux tableaux : on garde la premiere vue,
    # les tableaux etant ordonnes du plus recent au plus ancien.
    vues: dict[str, dict[str, Any]] = {}
    for ligne in trouvees:
        vues.setdefault(ligne["debut"].isoformat(), ligne)
    return [vues[k] for k in sorted(vues, reverse=True)]


def _nmw_data(force: bool = False) -> dict[str, Any]:
    entry = None if force else _cache_read("salaire_minimum_uk")
    if entry is not None:
        return entry
    api = _env("INDICES_NMW_API", NMW_CONTENT_API)
    try:
        doc = _http_get(api).json()
    except ValueError as exc:
        raise SourceError(
            f"L'API de contenu de gov.uk n'a pas rendu du JSON : {exc}"
        ) from exc
    corps = ((doc.get("details") or {}).get("body")) or ""
    lignes = _parse_nmw(corps)
    if not lignes:
        raise SourceError(
            "Aucun taux horaire lisible sur la page gov.uk du salaire minimum. "
            f"La structure de la page a change : voir {api}. C'est un tableau "
            "HTML, pas une serie de donnees - il n'existe pas d'API de serie "
            "pour ce taux, c'est la limite acceptee de cette source."
        )
    entry = {
        "source": api,
        # public_updated_at de gov.uk n'est PAS fiable ici : releve le
        # 2026-09-02, la page annonce 2024-10-30 alors que son corps porte deja
        # les taux d'avril 2026. Le millesime retenu est donc la periode la plus
        # recente LUE dans le tableau, pas ce que la page declare d'elle-meme.
        "page_updated_at": doc.get("public_updated_at") or "",
        "millesime": lignes[0]["debut"].isoformat(),
        "provenance": (
            "lu dans le TABLEAU HTML de gov.uk (National Minimum Wage and "
            "National Living Wage rates) - il n'existe pas d'API de serie"
        ),
        "lignes": [
            {
                "periode_source": x["periode_source"],
                "debut": x["debut"].isoformat(),
                "tranche": x["tranche"],
                "valeur": x["valeur"],
            }
            for x in lignes
        ],
    }
    _cache_write("salaire_minimum_uk", entry)
    _log(f"gov.uk NMW : {len(lignes)} periodes, la plus recente {entry['millesime']}")
    return entry


# --------------------------------------------------------------------------
# Eurostat : lecture generique du JSON-stat
# --------------------------------------------------------------------------

def _eurostat_url(flow: str, filtres: dict[str, str], depuis: str) -> str:
    base = _env("INDICES_EUROSTAT_BASE", EUROSTAT_BASE).rstrip("/")
    parts = [f"c[{k}]={v}" for k, v in filtres.items()]
    parts.append(f"c[TIME_PERIOD]=ge:{depuis}")
    parts.extend(("compress=false", "format=json", "lang=fr"))
    return f"{base}/{flow}/1.0/*?" + "&".join(parts)


def _jsonstat_points(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Deplie un document JSON-stat en points {dimensions..., valeur, statut}.

    Eurostat rend `value` comme un dictionnaire index -> valeur, et cet index
    est CREUX : les combinaisons sans donnee n'y figurent pas. C'est ce qui rend
    le decodage par pas (strides) plus sur qu'un remodelage en matrice. Le
    script Power Query d'origine completait les index manquants par 0 : sur le
    salaire minimum, ca donnait 0 euro pour le Danemark, qui n'a pas de salaire
    minimum legal. Ici, ce qui manque reste manquant.
    """
    ids: list[str] = doc.get("id") or []
    sizes: list[int] = [int(s) for s in (doc.get("size") or [])]
    if not ids or len(ids) != len(sizes):
        raise SourceError("Reponse Eurostat inattendue : 'id' et 'size' ne concordent pas.")

    codes: dict[str, list[str]] = {}
    labels: dict[str, dict[str, str]] = {}
    for name in ids:
        cat = ((doc.get("dimension") or {}).get(name) or {}).get("category") or {}
        index = cat.get("index") or {}
        if isinstance(index, dict):
            ordered = sorted(index, key=lambda c: int(index[c]))
        else:
            ordered = list(index)
        codes[name] = ordered
        labels[name] = cat.get("label") or {}

    strides = [1] * len(ids)
    for i in range(len(ids) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]

    values = doc.get("value") or {}
    statuses = doc.get("status") or {}
    if isinstance(values, list):
        values = {str(i): v for i, v in enumerate(values) if v is not None}

    points: list[dict[str, Any]] = []
    for flat, value in values.items():
        if value is None:
            continue
        try:
            rest = int(flat)
        except (TypeError, ValueError):
            continue
        point: dict[str, Any] = {}
        for pos, name in enumerate(ids):
            which = rest // strides[pos]
            rest -= which * strides[pos]
            liste = codes[name]
            code = liste[which] if 0 <= which < len(liste) else "?"
            point[name] = code
            point[f"{name}_label"] = labels[name].get(code, code)
        point["valeur"] = value
        flag = statuses.get(str(flat)) if isinstance(statuses, dict) else None
        point["statut"] = flag or ""
        points.append(point)
    return points


def _eurostat_data(
    cle: str, flow: str, filtres: dict[str, str], force: bool = False
) -> dict[str, Any]:
    entry = None if force else _cache_read(cle)
    if entry is not None:
        return entry
    depuis = _env("INDICES_DEPUIS", DEFAULT_DEPUIS)
    url = _eurostat_url(flow, filtres, depuis)
    try:
        doc = _http_get(url).json()
    except httpx.HTTPError as exc:
        raise SourceError(f"Eurostat injoignable ({flow}) : {exc}") from exc
    except ValueError as exc:
        raise SourceError(f"Eurostat n'a pas rendu du JSON ({flow}) : {exc}") from exc
    points = _jsonstat_points(doc)
    entry = {
        "source": url,
        "flow": flow,
        "millesime": doc.get("updated") or "",
        "points": points,
    }
    _cache_write(cle, entry)
    _log(f"{flow} : {len(points)} points, millesime {entry['millesime']}")
    return entry


# --------------------------------------------------------------------------
# Les periodes : une chaine humaine, deux bornes
# --------------------------------------------------------------------------

_RE_ANNEE = re.compile(r"^(\d{4})$")
_RE_MOIS = re.compile(r"^(\d{4})[-/](\d{1,2})$")
_RE_JOUR = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
_RE_SEM = re.compile(r"^(\d{4})[-/]?S([12])$", re.IGNORECASE)
_RE_TRIM = re.compile(r"^(\d{4})[-/]?[QT]([1-4])$", re.IGNORECASE)


def _fin_de_mois(annee: int, mois: int) -> dt.date:
    if mois == 12:
        return dt.date(annee, 12, 31)
    return dt.date(annee, mois + 1, 1) - dt.timedelta(days=1)


def _bornes(texte: str) -> tuple[dt.date, dt.date]:
    """Traduit '2024', '2024-06', '2024-S1', '2024-06-15' en debut et fin.

    Une borne se donne comme on la dit. Exiger une date ISO complete pour
    repondre a « depuis 2024 » n'apporte rien et fait rater la question.
    """
    raw = (texte or "").strip()
    if not raw:
        raise ConfigError("Periode vide.")
    m = _RE_JOUR.match(raw)
    if m:
        jour = dt.date(int(m[1]), int(m[2]), int(m[3]))
        return jour, jour
    m = _RE_MOIS.match(raw)
    if m:
        return dt.date(int(m[1]), int(m[2]), 1), _fin_de_mois(int(m[1]), int(m[2]))
    m = _RE_SEM.match(raw)
    if m:
        annee, semestre = int(m[1]), int(m[2])
        return (
            dt.date(annee, 1 if semestre == 1 else 7, 1),
            dt.date(annee, 6, 30) if semestre == 1 else dt.date(annee, 12, 31),
        )
    m = _RE_TRIM.match(raw)
    if m:
        annee, trimestre = int(m[1]), int(m[2])
        premier = 3 * (trimestre - 1) + 1
        return dt.date(annee, premier, 1), _fin_de_mois(annee, premier + 2)
    m = _RE_ANNEE.match(raw)
    if m:
        return dt.date(int(m[1]), 1, 1), dt.date(int(m[1]), 12, 31)
    raise ConfigError(
        f"Periode incomprise : '{raw}'. Formes acceptees : 2024, 2024-06, "
        "2024-S1, 2024-Q3, 2024-06-15."
    )


def _periode_eurostat_date(code: str) -> dt.date | None:
    """Le premier jour de la periode Eurostat '2024-06' ou '2024-S1'."""
    try:
        return _bornes(code)[0]
    except ConfigError:
        return None


# --------------------------------------------------------------------------
# Le lexique : ce qui fait qu'une question en francais trouve son code
# --------------------------------------------------------------------------
#
# Il vit dans le serveur, pas dans la skill : une skill est lue une fois en
# debut de session et le modele ne la relit pas avant chaque appel, donc le
# vocabulaire y est une intention. Ici c'est le serveur qui traduit, et il dit
# dans l'en-tete ce qu'il a traduit.
#
# Le cas d'ecole de ce connecteur : la Grece. Eurostat la code EL, le bulletin
# petrolier la code GR. Une question sur la Grece qui porte le mauvais code ne
# leve aucune erreur - elle rend zero ligne, ce qui se lit comme "pas de
# donnee". Les deux codes sont acceptes en entree, et le rendu porte le code de
# la source interrogee.

_LEXIQUE: dict[str, Any] | None = None
_LEXIQUE_ERROR = ""


def _norm(text: Any) -> str:
    """Forme comparable d'un libelle : sans accent, sans casse, sans ponctuation."""
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()


def _lexique() -> dict[str, Any]:
    """Le lexique livre avec le connecteur. Ne leve jamais."""
    global _LEXIQUE, _LEXIQUE_ERROR
    if _LEXIQUE is not None:
        return _LEXIQUE
    try:
        _LEXIQUE = json.loads(LEXIQUE_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        _LEXIQUE_ERROR = f"{LEXIQUE_FILE.name} illisible : {exc}"
        _LEXIQUE = {"pays": {}, "alias": {}, "produits": {}, "gasoil_codes": {}}
    return _LEXIQUE


def _pays_labels() -> dict[str, str]:
    return _lexique().get("pays") or {}


def _resoudre_pays(raw: str, source: str) -> tuple[str, str]:
    """Rend (code interne, code de la source) pour un pays donne en francais.

    Le code interne est celui d'Eurostat - EL pour la Grece, EA pour la zone
    euro. Le code de la source est ce qu'il faut aller chercher : GR et EUR dans
    le bulletin petrolier.
    """
    lex = _lexique()
    voulu = (raw or "").strip()
    if not voulu:
        raise ConfigError("Pays vide.")
    code = voulu.upper()
    if code not in _pays_labels():
        cle = _norm(voulu)
        alias = {_norm(k): v for k, v in (lex.get("alias") or {}).items()}
        if cle in alias:
            code = alias[cle]
        else:
            for pays_code, label in _pays_labels().items():
                if _norm(label) == cle:
                    code = pays_code
                    break
            else:
                raise ConfigError(
                    f"Pays inconnu : '{voulu}'. Donne un code ISO (FR, DE, EL) "
                    "ou un nom en francais. La liste complete est dans "
                    "indices_pays()."
                )
    if source == "gasoil":
        return code, (lex.get("gasoil_codes") or {}).get(code, code)
    return code, code


def _liste_pays(raw: str, source: str, defaut: list[str]) -> list[tuple[str, str]]:
    """Traduit 'France, Allemagne' ou 'FR,DE' en couples de codes."""
    voulu = (raw or "").strip()
    if not voulu or voulu == "*":
        lex = _lexique()
        return [
            (c, (lex.get("gasoil_codes") or {}).get(c, c) if source == "gasoil" else c)
            for c in defaut
        ]
    out: list[tuple[str, str]] = []
    for morceau in re.split(r"[;,]", voulu):
        morceau = morceau.strip()
        if not morceau:
            continue
        couple = _resoudre_pays(morceau, source)
        if couple not in out:
            out.append(couple)
    if not out:
        raise ConfigError(f"Aucun pays exploitable dans '{voulu}'.")
    return out


def _pays_defaut(source: str) -> list[str]:
    """Les pays rendus quand la question n'en nomme aucun.

    Ce sont les pays de livraison de Vente-unique, pas les vingt-sept : une
    reponse de vingt-sept lignes par periode ne se lit pas, et personne ne
    demande la Bulgarie quand il demande « le gazole ».
    """
    brut = _env("INDICES_PAYS_DEFAUT") or ",".join(
        _lexique().get("pays_defaut") or ["FR"]
    )
    codes = [c.strip().upper() for c in re.split(r"[;,]", brut) if c.strip()]
    return codes or ["FR"]


# --------------------------------------------------------------------------
# Construction des series
# --------------------------------------------------------------------------

def _normalise_taxes(taxes: str) -> str:
    taxes = (taxes or "ttc").strip().lower()
    if taxes in {"ht", "hors taxes", "hors-taxes", "wo", "sans taxes"}:
        return "ht"
    if taxes in {"ttc", "with", "avec taxes", "toutes taxes"}:
        return "ttc"
    raise ConfigError(f"Valeur de 'taxes' inconnue : '{taxes}'. Attendu : ttc ou ht.")


def _normalise_produit(produit: str) -> str:
    produit = (produit or "diesel").strip().lower()
    produit = (_lexique().get("produits") or {}).get(_norm(produit), produit)
    if produit not in WOB_PRODUITS:
        raise ConfigError(
            f"Produit inconnu : '{produit}'. Au choix : " + ", ".join(WOB_PRODUITS) + "."
        )
    return produit


# Ce que chaque source du gazole publie. Le tableau est la pour que les ecarts
# soient DECLARES et rendus dans chaque ligne, pas decouverts en comparant deux
# chiffres qui n'ont pas la meme unite.
GASOIL_SOURCES = {
    "europe": {
        "nom": "Weekly Oil Bulletin (Commission europeenne)",
        "frequence": "hebdomadaire",
        "produits": tuple(WOB_PRODUITS),
        "taxes": ("ttc", "ht"),
        "tolerance_j": 35,
    },
    "desnz": {
        "nom": "DESNZ, Weekly road fuel prices (gov.uk)",
        "frequence": "hebdomadaire",
        "produits": tuple(DESNZ_PRODUITS),
        "taxes": ("ttc", "ht"),
        "tolerance_j": 21,
    },
    "ssb": {
        "nom": "Statistisk sentralbyra, table 09654 (data.ssb.no)",
        "frequence": "mensuelle",
        "produits": tuple(SSB_PRODUITS.values()),
        "taxes": ("ttc",),
        "tolerance_j": 75,
    },
}


def _plan_sources_gasoil(
    demandes: list[tuple[str, str]], source: str
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Quelle source repond pour quel pays, et ce qui reste sans reponse.

    En mode `auto`, un pays qui a une source nationale passe par elle - et c'est
    le point important pour le ROYAUME-UNI : il est encore PRESENT dans le
    bulletin europeen, mais fige au 2020-12-21. Le laisser sur le bulletin
    rendrait un prix de 2020 a cote de chiffres de la semaine derniere. En auto,
    il vient donc de DESNZ, qui est a jour.

    `europe` force le bulletin, ce qui reste legitime pour comparer 27 pays dans
    une seule unite - avec l'avertissement de gel qui repart alors tout seul.
    """
    source = (source or "auto").strip().lower()
    alias = {
        "": "auto", "auto": "auto", "automatique": "auto",
        "europe": "europe", "eu": "europe", "bulletin": "europe",
        "national": "national", "nationale": "national", "nationales": "national",
    }
    source = alias.get(source)
    if source is None:
        raise ConfigError(
            "Valeur de 'source' inconnue. Au choix : auto (defaut - la source "
            "nationale quand elle existe, sinon le bulletin europeen), europe "
            "(le bulletin seul), national (les sources nationales seules)."
        )
    plan: dict[str, tuple[str, str]] = {}
    absents: list[str] = []
    for interne, code_source in demandes:
        nationale = GASOIL_SOURCES_NATIONALES.get(interne)
        if source == "europe":
            retenue = "europe"
        elif source == "national":
            retenue = nationale
        else:
            retenue = nationale or "europe"
        if not retenue:
            absents.append(
                f"{interne} (aucune source nationale de prix du carburant : "
                "voir indices_pays)"
            )
            continue
        plan[interne] = (retenue, code_source)
    return plan, absents


def _serie_gasoil(
    pays: str,
    depuis: str,
    jusqu_a: str,
    produit: str,
    taxes: str,
    source: str = "auto",
    force: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    produit = _normalise_produit(produit)
    taxes = _normalise_taxes(taxes)
    demandes = _liste_pays(pays, "gasoil", _pays_defaut("gasoil"))
    plan, absents = _plan_sources_gasoil(demandes, source)

    debut = _bornes(depuis)[0] if depuis else dt.date(1900, 1, 1)
    fin = _bornes(jusqu_a)[1] if jusqu_a else dt.date(2999, 12, 31)
    libelle, unite_eu = WOB_PRODUITS[produit]

    chargeurs = {"europe": _wob_data, "desnz": _desnz_data, "ssb": _ssb_data}
    charges: dict[str, dict[str, Any]] = {}
    lignes: list[dict[str, Any]] = []
    notes: list[str] = []
    sources_citees: list[str] = []
    provenances: list[str] = []
    millesimes: list[str] = []
    # Le gel se mesure source par source : chacune a sa propre derniere
    # publication, et une tolerance qui depend de sa frequence.
    dernier_par_source: dict[str, dict[str, str]] = {}
    mixte: set[str] = set()

    for interne, (nom_source, code_source) in plan.items():
        spec = GASOIL_SOURCES[nom_source]
        if produit not in spec["produits"]:
            absents.append(
                f"{interne} (le produit '{produit}' n'est pas publie par "
                f"{spec['nom']} - elle ne porte que : "
                + ", ".join(spec["produits"]) + ")"
            )
            continue
        if taxes not in spec["taxes"]:
            absents.append(
                f"{interne} (cette source ne publie pas le hors taxes : "
                f"{spec['nom']} donne le prix a la pompe seulement)"
            )
            continue
        if nom_source not in charges:
            charges[nom_source] = chargeurs[nom_source](force=force)
        entry = charges[nom_source]
        bloc = (entry.get("data") or {}).get(taxes) or {}
        dates: list[str] = bloc.get("dates") or []
        # Le bulletin europeen indexe par son propre code pays (GR pour la
        # Grece) ; les sources nationales n'en portent qu'un, le code interne.
        cle = code_source if nom_source == "europe" else interne
        valeurs = ((bloc.get("series") or {}).get(cle) or {}).get(produit)
        if valeurs is None:
            hors_perimetre = nom_source == "europe" and interne not in set(
                _lexique().get("bulletin_petrolier_pays") or []
            )
            absents.append(
                f"{interne} (hors bulletin petrolier : il ne couvre que l'Union)"
                if hors_perimetre
                else f"{interne} (cherche sous '{cle}' chez {spec['nom']}, "
                "colonne introuvable)"
            )
            continue

        if nom_source not in [x.split(" | ")[0] for x in sources_citees]:
            sources_citees.append(f"{nom_source} | {entry.get('source', '')}")
            if entry.get("provenance"):
                provenances.append(f"{spec['nom']} : {entry['provenance']}")
        if dates:
            millesimes.append(max(d for d in dates if d))
        if nom_source != "europe":
            mixte.add(nom_source)

        unite = unite_eu if nom_source == "europe" else (
            DESNZ_UNITE if nom_source == "desnz" else SSB_UNITE
        )
        mensuelle = spec["frequence"] == "mensuelle"

        # Le dernier releve du pays se cherche sur TOUTE la serie, pas sur la
        # fenetre demandee : c'est ce qui permet de dire "ce pays n'est plus
        # publie" meme quand la question porte sur 2019.
        dispo = [
            d
            for pos, d in enumerate(dates)
            if pos < len(valeurs) and valeurs[pos] is not None and d
        ]
        if dispo:
            dernier_par_source.setdefault(nom_source, {})[interne] = max(dispo)
        for pos, jour_iso in enumerate(dates):
            if not jour_iso:
                continue
            jour = dt.date.fromisoformat(jour_iso)
            if jour < debut or jour > fin:
                continue
            valeur = valeurs[pos] if pos < len(valeurs) else None
            if valeur is None:
                continue
            lignes.append(
                {
                    "indice": "gazole",
                    "code_pays": interne,
                    "pays": _pays_labels().get(interne, interne),
                    "periode": jour_iso[:7] if mensuelle else jour_iso,
                    "date": jour_iso,
                    "valeur": round(float(valeur), 2),
                    "unite": unite,
                    "produit": produit,
                    "taxes": taxes,
                    "frequence": spec["frequence"],
                    "source": nom_source,
                }
            )

    lignes.sort(key=lambda r: (r["date"], r["code_pays"]), reverse=True)

    for nom_source, par_pays in dernier_par_source.items():
        spec = GASOIL_SOURCES[nom_source]
        entry = charges.get(nom_source) or {}
        dates = ((entry.get("data") or {}).get(taxes) or {}).get("dates") or []
        fin_source = max((d for d in dates if d), default="")
        notes.extend(
            _note_arret(par_pays, fin_source, int(spec["tolerance_j"]), "un prix")
        )

    if mixte:
        unites = sorted({r["unite"] for r in lignes})
        freqs = sorted({r["frequence"] for r in lignes})
        if len(unites) > 1 or len(freqs) > 1:
            notes.append(
                "PLUSIEURS SOURCES DANS CE TABLEAU, et elles n'ont ni la meme "
                "unite ni forcement la meme frequence : "
                + ", ".join(unites)
                + " / " + ", ".join(freqs)
                + ". La colonne 'source' dit d'ou vient chaque ligne. NE COMPARE "
                "PAS deux valeurs absolues d'unites differentes - il faudrait un "
                "taux de change a la date, que ce connecteur ne fait pas. Ce qui "
                "se compare d'un pays a l'autre, c'est la VARIATION en "
                "pourcentage : elle est sans unite. Voir indices_variation."
            )
        if "desnz" in mixte:
            notes.append(
                "Royaume-Uni : la valeur vient de DESNZ (gov.uk), pas du "
                "bulletin europeen, ou il est fige au 2020-12-21. C'est "
                "volontaire, et c'est ce qui rend le chiffre courant."
            )

    meta = {
        "source": " ; ".join(x.split(" | ", 1)[1] for x in sources_citees),
        "provenance": " ; ".join(provenances),
        "rapatrie": (charges.get("europe") or next(iter(charges.values()), {})).get(
            "fetched_at", ""
        ),
        "millesime": max(millesimes) if millesimes else "",
        "libelle": libelle,
        "unite": " / ".join(sorted({r["unite"] for r in lignes})) or unite_eu,
        "absents": absents,
        "notes": notes,
    }
    return lignes, meta

def _serie_inflation(
    pays: str,
    depuis: str,
    jusqu_a: str,
    mesure: str,
    poste: str = "total",
    force: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    mesure = (mesure or "annuel").strip().lower().replace("-", "_")
    alias = {
        "annuelle": "annuel",
        "glissante": "moyenne_12m",
        "12m": "moyenne_12m",
        "moyenne": "moyenne_12m",
        "ipch": "annuel",
        "hicp": "annuel",
    }
    mesure = alias.get(mesure, mesure)
    if mesure not in HICP_MESURES:
        raise ConfigError(
            f"Mesure inconnue : '{mesure}'. Au choix : " + ", ".join(HICP_MESURES) + "."
        )
    unit, unite_label, a_quoi = HICP_MESURES[mesure]

    poste = (poste or "total").strip().lower()
    poste = {
        "": "total",
        "tout": "total",
        "tous postes": "total",
        "ensemble": "total",
        "carburant": "carburants",
        "gazole": "carburants",
        "gasoil": "carburants",
        "diesel": "carburants",
        "essence": "carburants",
        "cp0722": "carburants",
    }.get(poste, poste)
    if poste not in HICP_POSTES:
        raise ConfigError(
            f"Poste inconnu : '{poste}'. Au choix : " + ", ".join(HICP_POSTES) + "."
        )
    coicop, poste_label, poste_a_quoi = HICP_POSTES[poste]

    entry = _eurostat_data(
        f"inflation_{unit}_{coicop}",
        HICP_FLOW,
        {"freq": "M", "unit": unit, "coicop18": coicop},
        force=force,
    )
    voulus = {interne for interne, _ in _liste_pays(pays, "eurostat", _pays_defaut("eurostat"))}
    debut = _bornes(depuis)[0] if depuis else dt.date(1900, 1, 1)
    fin = _bornes(jusqu_a)[1] if jusqu_a else dt.date(2999, 12, 31)

    couverture, dernier_flux = _couverture_points(entry.get("points") or [], voulus)
    lignes: list[dict[str, Any]] = []
    for point in entry.get("points") or []:
        code = point.get("geo")
        if code not in voulus:
            continue
        jour = _periode_eurostat_date(point.get("time", ""))
        if jour is None or jour < debut or jour > fin:
            continue
        lignes.append(
            {
                "indice": "inflation",
                "code_pays": code,
                "pays": _pays_labels().get(code, point.get("geo_label", code)),
                "periode": point.get("time", ""),
                "date": jour.isoformat(),
                "valeur": point.get("valeur"),
                "unite": unite_label,
                "mesure": mesure,
                "poste": poste,
                "statut": point.get("statut", ""),
            }
        )
    lignes.sort(key=lambda r: (r["date"], r["code_pays"]), reverse=True)

    notes = _note_arret(couverture, dernier_flux, 70, "une inflation")
    if poste == "carburants":
        # Dit une fois, dans l'en-tete, plutot que suppose lu : cet indice est
        # la SEULE mesure carburant disponible pour la Suisse, et il ne se
        # compare pas a un prix au litre.
        notes.append(
            "CECI EST UN INDICE, PAS UN PRIX. Le poste 'carburants' de l'IPCH "
            "mesure un panier de consommation - essence, gazole et lubrifiants "
            "ponderes ensemble - en base mensuelle. Il repond a « de combien a "
            "bouge le carburant », ce qui suffit a une clause d'indexation. Il "
            "ne repond pas a « combien coute le gazole », et il NE SE COMPARE "
            "PAS a un prix au litre du bulletin petrolier ou de DESNZ. C'est en "
            "revanche la seule mesure carburant disponible pour la SUISSE."
        )
    meta = {
        "source": entry.get("source", ""),
        "flow": HICP_FLOW,
        "coicop": coicop,
        "poste": poste,
        "millesime": entry.get("millesime", ""),
        "rapatrie": entry.get("fetched_at", ""),
        "libelle": (
            f"IPCH, {unite_label} - {a_quoi}"
            if poste == "total"
            else f"IPCH {poste_label}, {unite_label} - {poste_a_quoi}"
        ),
        "unite": unite_label,
        "absents": sorted(voulus - {r["code_pays"] for r in lignes}),
        # 70 jours : deux mois. Les pays hors UE (CH, NO, IS) publient leur IPCH
        # avec un mois de retard sur les Etats membres, c'est normal et ca ne
        # merite pas un avertissement.
        "notes": notes,
    }
    return lignes, meta


def _serie_salaire_uk(
    debut: dt.date, fin: dt.date, force: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Le salaire minimum britannique, tel que gov.uk le publie : par HEURE.

    Eurostat porte encore le Royaume-Uni, mais fige a 2020-S2. Cette source le
    remplace, au prix de deux ecarts que le connecteur NE COMBLE PAS :

    - le taux est HORAIRE, la ou Eurostat publie un montant MENSUEL. Convertir
      supposerait une duree de travail hebdomadaire - donc un chiffre invente,
      qui partirait ensuite dans une comparaison de cout de main-d'oeuvre ;
    - il prend effet en AVRIL, la ou Eurostat decoupe en semestres (S1 en
      janvier, S2 en juillet). La periode rendue est le mois de prise d'effet.

    La tranche d'age est rendue telle quelle parce qu'elle a CHANGE : « 25 and
    over » avant 2021, puis « 23 and over », puis « 21 and over ». Sans elle, on
    suivrait une seule courbe en croyant qu'elle porte sur la meme population.
    """
    entry = _nmw_data(force=force)
    lignes: list[dict[str, Any]] = []
    for brut in entry.get("lignes") or []:
        jour = dt.date.fromisoformat(brut["debut"])
        if jour < debut or jour > fin:
            continue
        lignes.append(
            {
                "indice": "salaire_minimum",
                "code_pays": "UK",
                "pays": _pays_labels().get("UK", "Royaume-Uni"),
                "periode": brut["debut"][:7],
                "date": brut["debut"],
                "valeur": brut["valeur"],
                "unite": NMW_UNITE,
                "devise": "GBP",
                "categorie": brut["tranche"],
                "statut": "",
                "source": "gov.uk",
            }
        )
    return lignes, entry


def _serie_salaire(
    pays: str,
    depuis: str,
    jusqu_a: str,
    devise: str,
    force: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    devise = (devise or "EUR").strip().upper()
    if devise not in MW_DEVISES:
        raise ConfigError(
            f"Devise inconnue : '{devise}'. Au choix : " + ", ".join(MW_DEVISES) + "."
        )
    entry = _eurostat_data(
        f"salaire_minimum_{devise}", MW_FLOW, {"freq": "S", "currency": devise}, force=force
    )
    voulus = {interne for interne, _ in _liste_pays(pays, "eurostat", _pays_defaut("eurostat"))}
    debut = _bornes(depuis)[0] if depuis else dt.date(1900, 1, 1)
    fin = _bornes(jusqu_a)[1] if jusqu_a else dt.date(2999, 12, 31)

    couverture, dernier_flux = _couverture_points(entry.get("points") or [], voulus)
    lignes: list[dict[str, Any]] = []
    for point in entry.get("points") or []:
        code = point.get("geo")
        if code not in voulus:
            continue
        jour = _periode_eurostat_date(point.get("time", ""))
        if jour is None or jour < debut or jour > fin:
            continue
        lignes.append(
            {
                "indice": "salaire_minimum",
                "code_pays": code,
                "pays": _pays_labels().get(code, point.get("geo_label", code)),
                "periode": point.get("time", ""),
                "date": jour.isoformat(),
                "valeur": point.get("valeur"),
                "unite": MW_DEVISES[devise] + " par mois",
                "devise": devise,
                "categorie": "salaire minimum legal national",
                "statut": point.get("statut", ""),
                "source": "eurostat",
            }
        )
    # Le Royaume-Uni : Eurostat le porte encore mais fige a 2020-S2. On lui
    # substitue la source nationale, et on le DIT - un tableau qui melange un
    # montant mensuel en euros et un taux horaire en livres doit s'annoncer.
    notes: list[str] = []
    meta_uk: dict[str, Any] = {}
    if "UK" in voulus:
        lignes = [r for r in lignes if r["code_pays"] != "UK"]
        lignes_uk, meta_uk = _serie_salaire_uk(debut, fin, force=force)
        lignes.extend(lignes_uk)
        if lignes_uk:
            # La substitution a marche : le chiffre rendu pour le Royaume-Uni
            # est courant. Laisser partir l'avertissement de gel serait alors
            # FAUX - il designerait une valeur d'epoque qui n'est plus rendue.
            # On retire donc le pays du controle de couverture Eurostat.
            couverture.pop("UK", None)
            notes.append(
                "Royaume-Uni : le montant vient de gov.uk, pas d'Eurostat, ou il "
                "est fige a 2020-S2. Il est donc en LIVRES PAR HEURE quand les "
                "autres pays sont en " + MW_DEVISES[devise].lower() + " PAR "
                "MOIS, et il prend effet en avril quand Eurostat decoupe en "
                "semestres. Les deux NE SE COMPARENT PAS ligne a ligne, et ce "
                "connecteur ne les convertit pas : il faudrait une duree de "
                "travail hebdomadaire, c'est-a-dire fabriquer un chiffre. La "
                "colonne 'categorie' porte la tranche d'age, qui a change dans "
                "le temps."
            )

    # 200 jours : un peu plus d'un semestre. Calcule APRES la substitution, pour
    # ne pas avertir d'un gel sur un pays dont on rend desormais la valeur
    # courante depuis une autre source.
    notes = _note_arret(couverture, dernier_flux, 200, "un montant") + notes

    lignes.sort(key=lambda r: (r["date"], r["code_pays"]), reverse=True)
    rendus = {r["code_pays"] for r in lignes}
    manquants = sorted(voulus - rendus)
    sans_smic = [c for c in manquants if c in SANS_SALAIRE_MINIMUM_LEGAL]
    if sans_smic:
        notes.append(
            "Aucune valeur pour " + ", ".join(sans_smic) + " : ces pays n'ont pas "
            "de salaire minimum legal national, le plancher y est fixe par "
            "convention collective, branche par branche. Ce n'est PAS une "
            "donnee manquante, et ca ne se remplace pas par zero."
        )
    sources = [entry.get("source", "")]
    if meta_uk.get("source"):
        sources.append(meta_uk["source"])
    # Les deux millesimes ne sont PAS du meme format : Eurostat rend un horodatage
    # ISO complet, gov.uk une date de prise d'effet. Les passer a max() les
    # comparerait caractere par caractere - « 2026-07-31T11:00 » gagnerait
    # toujours sur « 2026-04-01 », y compris si gov.uk etait plus recent. On les
    # garde donc separes et nommes, plutot que d'en elire un faussement.
    meta = {
        "source": " ; ".join(x for x in sources if x),
        "flow": MW_FLOW,
        "millesime": entry.get("millesime", ""),
        "millesime_uk": meta_uk.get("millesime", ""),
        "rapatrie": entry.get("fetched_at", ""),
        "provenance": meta_uk.get("provenance", ""),
        "libelle": f"Salaire minimum legal national, {MW_DEVISES[devise]}",
        "unite": " / ".join(sorted({r["unite"] for r in lignes})) or MW_DEVISES[devise],
        "absents": manquants,
        "notes": notes,
    }
    return lignes, meta


def _couverture_points(
    points: list[dict[str, Any]], voulus: set[str]
) -> tuple[dict[str, str], str]:
    """Derniere periode publiee par pays demande, et derniere periode du flux.

    Balaye TOUS les points du cache, pas seulement la fenetre filtree : c'est ce
    qui permet de dire "ce pays n'est plus publie" meme quand la question porte
    sur une periode ancienne.
    """
    par_pays: dict[str, str] = {}
    dernier = ""
    for point in points:
        periode = point.get("time", "")
        if periode > dernier:
            dernier = periode
        code = point.get("geo")
        if code in voulus and periode > par_pays.get(code, ""):
            par_pays[code] = periode
    return par_pays, dernier


def _note_arret(
    dernier_par_pays: dict[str, str],
    dernier_source: str,
    tolerance_jours: int,
    quoi: str,
) -> list[str]:
    """Signale les pays que la source a CESSE de publier.

    C'est le piege le plus vicieux de ces trois series, parce qu'il ne produit
    ni erreur ni case vide : il produit une VALEUR, simplement perimee. Le
    Royaume-Uni sort des trois sources fin 2020 - le bulletin petrolier ne
    couvre que les Etats membres, et l'IPCH britannique n'est plus transmis a
    Eurostat. Une question sur le gazole au Royaume-Uni rend donc, sans un mot,
    un prix de decembre 2020 en tete de tableau, la ou tout le reste est de la
    semaine derniere.

    La tolerance evite de crier a chaque semaine manquante : une source saute un
    releve de temps en temps sans avoir cesse de publier.
    """
    if not dernier_source:
        return []
    fin_source = _periode_eurostat_date(dernier_source)
    if fin_source is None:
        return []
    notes: list[str] = []
    for code, dernier in sorted(dernier_par_pays.items()):
        fin_pays = _periode_eurostat_date(dernier)
        if fin_pays is None:
            continue
        retard = (fin_source - fin_pays).days
        if retard > tolerance_jours:
            notes.append(
                f"ATTENTION : {_pays_labels().get(code, code)} ({code}) n'est plus "
                f"publie depuis {dernier}, alors que la source va jusqu'a "
                f"{dernier_source}. Toute valeur rendue ici pour ce pays est "
                f"donc {quoi} d'epoque, pas une valeur courante - ne la compare "
                "pas a un chiffre recent sans le dire."
            )
    return notes


INDICES = ("gazole", "inflation", "salaire_minimum")


def _serie(
    indice: str,
    pays: str = "",
    depuis: str = "",
    jusqu_a: str = "",
    produit: str = "diesel",
    taxes: str = "ttc",
    mesure: str = "annuel",
    devise: str = "EUR",
    source: str = "auto",
    poste: str = "total",
    force: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aiguillage unique : c'est ce qui permet a l'export et au calcul d'ecart
    de couvrir les trois indices sans dupliquer les filtres.

    Les appels sont ecrits en MOTS-CLES. En positionnel, l'ajout de `source` et
    de `poste` decalait `force` d'un cran : l'export forcait un rapatriement a
    chaque appel et une source explicite etait ignoree, sans qu'aucune erreur ne
    le signale. C'est exactement le genre de panne muette que ce connecteur est
    cense empecher, elle n'a pas sa place dans son propre aiguillage.
    """
    cle = (indice or "").strip().lower()
    cle = {"gasoil": "gazole", "diesel": "gazole", "carburant": "gazole",
           "smic": "salaire_minimum", "salaire": "salaire_minimum",
           "ipch": "inflation", "hicp": "inflation"}.get(cle, cle)
    if cle == "gazole":
        return _serie_gasoil(
            pays, depuis, jusqu_a, produit, taxes, source=source, force=force
        )
    if cle == "inflation":
        return _serie_inflation(
            pays, depuis, jusqu_a, mesure, poste=poste, force=force
        )
    if cle == "salaire_minimum":
        return _serie_salaire(pays, depuis, jusqu_a, devise, force=force)
    raise ConfigError(
        f"Indice inconnu : '{indice}'. Au choix : " + ", ".join(INDICES) + "."
    )


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------

def _colonnes(lignes: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for ligne in lignes:
        for cle in ligne:
            seen.setdefault(cle, None)
    return list(seen)


def _csv_text(lignes: list[dict[str, Any]], delimiter: str = ";") -> str:
    if not lignes:
        return ""
    cols = _colonnes(lignes)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=cols, delimiter=delimiter, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    for ligne in lignes:
        writer.writerow({c: ligne.get(c, "") for c in cols})
    return buf.getvalue()


def _entete(meta: dict[str, Any], titre: str) -> list[str]:
    lignes = [titre]
    if meta.get("libelle"):
        lignes.append(f"Mesure     : {meta['libelle']}")
    if meta.get("millesime"):
        lignes.append(f"Millesime  : derniere periode ou publication source {meta['millesime']}")
    if meta.get("millesime_uk"):
        lignes.append(
            f"Millesime UK : {meta['millesime_uk']} (gov.uk, prise d'effet - un "
            "autre format et une autre source que la ligne ci-dessus)"
        )
    if meta.get("rapatrie"):
        lignes.append(f"Rapatrie   : {meta['rapatrie']} (cache local)")
    if meta.get("source"):
        lignes.append(f"Source     : {meta['source']}")
    provenance = meta.get("provenance") or ""
    if "CHAINE-SEULE" in provenance:
        lignes.append(
            "ATTENTION : ce classeur a ete telecharge avec le NOM D'HOTE NON "
            "VERIFIE (INDICES_GASOIL_TLS=chaine-seule). La chaine de "
            "certification et l'appartenance a europa.eu ont ete controlees, "
            "mais dis-le si ce chiffre part dans un echange contractuel."
        )
    elif "fichier local" in provenance:
        # `startswith` ne marche plus depuis que la provenance est prefixee du
        # nom de la source ("Weekly Oil Bulletin (...) : fichier local ..."). Un
        # avertissement qui ne part plus est pire que pas d'avertissement.
        lignes.append(f"ATTENTION : lecture d'un {provenance}. Verifie sa date.")
    for note in meta.get("notes") or []:
        lignes.append(note)
    if meta.get("absents"):
        lignes.append(
            "Sans donnee sur ce perimetre : " + ", ".join(str(a) for a in meta["absents"]) + "."
        )
    return lignes


def _rendu(
    lignes: list[dict[str, Any]], meta: dict[str, Any], titre: str, max_lignes: int
) -> str:
    tete = _entete(meta, titre)
    total = len(lignes)
    coupe = lignes[: max(1, max_lignes)]
    tete.append(f"{total} ligne(s) sur le perimetre ; {len(coupe)} affichee(s).")
    if total > len(coupe):
        tete.append(
            f"ATTENTION : rendu TRONQUE. Le compte ci-dessus ({total}) est le "
            "total, pas le nombre de lignes lues ci-dessous. Releve max_lignes, "
            "resserre la periode, ou demande un export CSV."
        )
    tete.append("")
    texte = "\n".join(tete) + _csv_text(coupe)
    if len(texte) > RENDER_MAX_CHARS:
        texte = texte[:RENDER_MAX_CHARS] + "\n... (rendu coupe au budget d'affichage)"
    return texte


def _guard(fn):
    """Rend les erreurs de configuration et de source comme message lisible.

    functools.wraps n'est pas cosmetique ici : il pose __wrapped__, que
    inspect.signature suit. Sans lui, FastMCP lit la signature du wrapper et
    publie chaque outil avec deux parametres (args, kwargs) au lieu des vrais -
    les outils sortent alors du handshake, mais sont INAPPELABLES. Le bug est
    deja passe une fois chez shiptify, il ne repasse pas ici.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ConfigError, SourceError) as exc:
            return f"ERREUR : {exc}"

    return wrapper


# --------------------------------------------------------------------------
# Les outils
# --------------------------------------------------------------------------

SERVER_INSTRUCTIONS = """Les trois indices publics europeens qui font bouger un tarif de transport :
le prix hebdomadaire des carburants (Weekly Oil Bulletin de la Commission),
l'inflation harmonisee IPCH et le salaire minimum legal (Eurostat). Lecture
seule, donnees ouvertes, aucune cle d'API.

Trois choses a savoir avant de citer un chiffre rendu ici.

1. CE CONNECTEUR NE CALCULE AUCUNE SURCHARGE ET N'APPLIQUE AUCUNE CLAUSE. Il rend
   la donnee et son millesime. La formule de revision - indice de reference, part
   carburant de l'assiette, periodicite, seuil, arrondi - est dans le contrat du
   transporteur, sous 02_TRANSPORTEURS/<NOM>/02_tarif/, et deux transporteurs
   n'ont pas la meme. Ne tranche jamais la regle a la place du contrat.

2. LA COUVERTURE N'EST PAS LA MEME SUR LES TROIS SERIES. Le Royaume-Uni n'a pas
   disparu des fichiers, il s'y est FIGE en 2020 - une valeur, pas une case vide.
   La Suisse, la Norvege et l'Islande sont hors bulletin petrolier. Huit pays
   n'ont pas de salaire minimum legal, et cette absence ne se remplace pas par
   zero. Le serveur le dit dans l'en-tete de chaque rendu concerne : ne resume
   jamais une reponse en coupant cet avertissement.

3. UN INDICE SE CITE AVEC SON MILLESIME. Chaque rendu porte la derniere periode
   publiee par la source et la date de rapatriement du cache.

Commence par indices_sources quand tu ne sais pas quelle serie repond.
indices_variation est la forme utile pour une revision tarifaire : il compare la
MOYENNE des releves de deux periodes, pas deux points isoles. indices_pays donne
les codes acceptes et les ecarts de code entre sources - la Grece est EL chez
Eurostat et GR dans le bulletin petrolier. N'exporte en CSV que si l'utilisateur
a demande un export.
"""


def _hints(titre: str, monde_ouvert: bool, idempotent: bool = True) -> ToolAnnotations:
    """Les annotations de comportement de la specification MCP.

    `readOnlyHint` a vrai partout : aucun outil n'a de chemin d'ecriture vers la
    source. Ce ne sont que des indications - la specification demande aux clients
    de ne pas leur faire confiance - ce qui garantit la lecture seule reste le
    code : des GET sur trois URL publiques, et rien d'autre.

    `openWorldHint` distingue ce qui lit le referentiel embarque ou le cache
    (ensemble ferme, reponse reproductible) de ce qui peut rappeler la source.

    indices_export_csv est annonce en lecture seule comme les autres : il lit
    l'API et ecrit un fichier LOCAL, il n'ecrit rien vers la source. Il n'est en
    revanche pas idempotent - il pose un fichier de plus a chaque appel.
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

mcp = FastMCP("indices", instructions=SERVER_INSTRUCTIONS)


@mcp.tool(annotations=_hints("Les trois series, leur source et leur fraicheur", SUR_LA_SOURCE))
@_guard
def indices_sources() -> str:
    """Les trois indices exposes : ce qu'ils mesurent, leur source, leur fraicheur.

    A appeler en premier quand on ne sait pas lequel repond a la question posee,
    ou quand un chiffre doit etre date avant d'etre cite.
    """
    lignes = [
        "Trois indices, SIX sources publiques, aucune authentification.",
        "",
        "gazole            TROIS sources selon le pays, et elles n'ont pas la meme",
        "                  unite ni la meme frequence. Chaque ligne porte la sienne,",
        "                  et le connecteur ne convertit rien : convertir supposerait",
        "                  un taux de change a la date, donc un chiffre fabrique.",
        "",
        "  Union           Weekly Oil Bulletin, Commission europeenne (DG ENER).",
        "                  HEBDOMADAIRE, releve le lundi, TTC et hors taxes, 29 pays",
        "                  + moyennes UE et zone euro, depuis 2005.",
        "                  Six produits : " + ", ".join(WOB_PRODUITS) + ".",
        "                  Unites : EUR/1000 l, sauf fioul lourd en EUR/t.",
        f"                  Cache local : {_cache_stamp('gasoil')}",
        "",
        "  Royaume-Uni     DESNZ, Weekly road fuel prices (gov.uk).",
        "                  HEBDOMADAIRE, depuis 2003 - donc plus long que le bulletin.",
        "                  Unite : GBp/l (pence par litre). Diesel et euro95 seulement.",
        "                  Le HORS TAXES est reconstitue depuis l'accise et le taux de",
        "                  TVA que le releve publie, tous deux variables dans le temps.",
        "                  Utilise PAR DEFAUT pour le Royaume-Uni : le bulletin europeen",
        "                  l'a encore, mais FIGE au 2020-12-21.",
        f"                  Cache local : {_cache_stamp('gasoil_uk')}",
        "",
        "  Norvege         Statistisk sentralbyra, table 09654 (data.ssb.no).",
        "                  MENSUEL, depuis 1986. Unite : NOK/l. Diesel et euro95.",
        "                  Prix a la pompe SEULEMENT : pas de hors taxes.",
        f"                  Cache local : {_cache_stamp('gasoil_no')}",
        "",
        "  Suisse          AUCUN PRIX. Il n'existe pas d'API federale suisse de prix",
        "                  du carburant (verifie le 2026-09-02 : la division prix est",
        "                  absente de l'API PX-Web de l'OFS, energiedashboard.admin.ch",
        "                  est une application web sans API, et opendata.swiss n'en",
        "                  porte qu'une republication cantonale). Sa seule mesure est",
        "                  l'indice : indices_inflation(pays='CH', poste='carburants').",
        "",
        "inflation         Eurostat " + HICP_FLOW + " - IPCH harmonise, MENSUEL.",
        "                  Mesures : " + ", ".join(HICP_MESURES) + ".",
        "                  Postes  : " + ", ".join(HICP_POSTES) + ".",
        "                  Le poste 'carburants' (CP0722) est un INDICE de panier de",
        "                  consommation, pas un prix - et c'est la seule mesure",
        "                  carburant disponible pour la SUISSE.",
        "                  Le Royaume-Uni est sorti de l'IPCH fin 2020 : il ne rend",
        "                  rien, ni sur le total ni sur les carburants.",
        "                  Piege : prc_hicp_manr et prc_hicp_midx repondent encore mais",
        "                  sont FIGES depuis le 2026-02-06 (derniere periode 2025-12).",
        "                  Ce connecteur n'interroge que " + HICP_FLOW + ", qui est a jour",
        "                  et porte toutes les unites, taux annuel compris.",
        f"                  Cache local : {_cache_stamp('inflation_RCH_A')}",
        "",
        "salaire_minimum   Eurostat " + MW_FLOW + " - salaire minimum legal national,",
        "                  SEMESTRIEL (S1 = janvier, S2 = juillet).",
        "                  Devises : " + ", ".join(f"{k} ({v})" for k, v in MW_DEVISES.items()) + ".",
        "                  Huit pays n'ont pas de salaire minimum legal ("
        + ", ".join(SANS_SALAIRE_MINIMUM_LEGAL) + ") :",
        "                  ils sortent SANS valeur, et une valeur absente ne vaut pas zero.",
        f"                  Cache local : {_cache_stamp('salaire_minimum_EUR')}",
        "",
        "  Royaume-Uni     gov.uk, National Minimum Wage and National Living Wage.",
        "                  Substitue a Eurostat, qui le porte encore mais FIGE a",
        "                  2020-S2. Deux ecarts que le connecteur NE COMBLE PAS : le",
        "                  taux est HORAIRE (GBP/heure) et non mensuel, et il prend",
        "                  effet en AVRIL et non par semestre. La tranche d'age est",
        "                  rendue telle quelle - elle a change trois fois depuis 2020.",
        "                  Source : un TABLEAU HTML, il n'existe pas d'API de serie.",
        f"                  Cache local : {_cache_stamp('salaire_minimum_uk')}",
        "",
        f"Duree de cache   : {_ttl_hours():.0f} h. indices_refresh() force le rapatriement.",
        f"Dossier de cache : {_cache_dir()}",
        f"Dossier d'export : {_export_dir()}",
        "",
        "Ce connecteur ne calcule aucune surcharge et n'applique aucune clause :",
        "la formule est dans le contrat du transporteur, et deux transporteurs",
        "n'ont pas la meme.",
    ]
    return "\n".join(lignes)


@mcp.tool(annotations=_hints("Codes pays acceptes, et les ecarts entre sources", HORS_LIGNE))
@_guard
def indices_pays() -> str:
    """Le referentiel des pays : codes, noms francais acceptes, couverture par indice.

    Utile avant une comparaison multi-pays, et indispensable pour la Grece :
    Eurostat la code EL, le bulletin petrolier la code GR.
    """
    lex = _lexique()
    labels = _pays_labels()
    gasoil_codes = lex.get("gasoil_codes") or {}
    lignes = []
    if _LEXIQUE_ERROR:
        lignes.append(f"ATTENTION : {_LEXIQUE_ERROR}")
    couverts = set(lex.get("bulletin_petrolier_pays") or [])
    lignes.append(
        "code;pays;code_bulletin_petrolier;source_gazole;prix_au_litre;"
        "indice_carburant;source_salaire_minimum;remarque"
    )
    for code in sorted(labels):
        remarques: list[str] = []
        if code in gasoil_codes:
            remarques.append("code different entre Eurostat et le bulletin petrolier")

        # D'ou vient son prix du carburant, en mode auto.
        nationale = GASOIL_SOURCES_NATIONALES.get(code)
        if nationale:
            source_gaz = nationale
            prix = "oui"
            remarques.append(
                f"prix du carburant par la source nationale {nationale}, "
                "unite et frequence differentes du bulletin"
            )
        elif code in couverts:
            source_gaz = "europe"
            prix = "oui"
        else:
            source_gaz = "-"
            prix = "non"
            remarques.append(
                "aucun prix du carburant : hors bulletin europeen et sans source "
                "nationale branchee"
            )

        # L'indice carburant IPCH couvre tout ce qu'Eurostat couvre, donc tout
        # sauf le Royaume-Uni, sorti de l'IPCH fin 2020.
        indice_carb = "non" if code == "UK" else "oui"
        if code == "UK":
            remarques.append("sorti de l'IPCH fin 2020 : aucun indice, ni total ni carburant")

        if code == "UK":
            source_smic = "gov.uk"
            remarques.append("salaire minimum en GBP/HEURE, non comparable au mensuel Eurostat")
        elif code in SANS_SALAIRE_MINIMUM_LEGAL:
            source_smic = "-"
            remarques.append("pas de salaire minimum legal")
        else:
            source_smic = "eurostat"

        lignes.append(
            f"{code};{labels[code]};{gasoil_codes.get(code, code)};"
            f"{source_gaz};{prix};{indice_carb};{source_smic};"
            f"{' ; '.join(remarques)}"
        )
    lignes.append("")
    lignes.append(
        "Noms francais acceptes en entree : "
        + ", ".join(sorted((lex.get("alias") or {}).keys()))
    )
    lignes.append("")
    lignes.append(
        "Pays rendus quand la question n'en nomme aucun : "
        + ", ".join(_pays_defaut("eurostat"))
        + " (reglable par INDICES_PAYS_DEFAUT)."
    )
    return "\n".join(lignes)


@mcp.tool(annotations=_hints("Prix hebdomadaires des carburants", SUR_LA_SOURCE))
@_guard
def indices_gasoil(
    pays: str = "",
    depuis: str = "",
    jusqu_a: str = "",
    produit: str = "diesel",
    taxes: str = "ttc",
    source: str = "auto",
    max_lignes: int = 0,
) -> str:
    """Prix des carburants a la pompe, par pays - trois sources publiques.

    pays      : 'France', 'FR', 'FR,DE,ES', 'zone euro', vide = pays de livraison VU.
    depuis    : '2024', '2024-06', '2024-06-15'. Vide = tout l'historique.
    jusqu_a   : idem, borne haute incluse.
    produit   : diesel (defaut), euro95, heating_oil, fuel_oil_1, fuel_oil_2, LPG.
                Hors de l'Union, seuls diesel et euro95 existent.
    taxes     : 'ttc' (defaut) ou 'ht'. Le HT est ce qui sert a une comparaison
                inter-pays : la fiscalite ecrase tout le reste dans le TTC.
                La Norvege ne publie pas de HT, et le connecteur le dit.
    source    : 'auto' (defaut), 'europe' ou 'national'.

    TROIS SOURCES, ET CE N'EST PAS UN DETAIL. Les Etats membres viennent du
    Weekly Oil Bulletin (hebdomadaire, EUR/1000 l, TTC et HT). Le ROYAUME-UNI
    vient de DESNZ / gov.uk (hebdomadaire, pence/litre, HT reconstitue depuis
    l'accise et la TVA publiees). La NORVEGE vient de SSB (mensuel, NOK/litre,
    pompe seulement).

    En 'auto', le Royaume-Uni passe donc par DESNZ et NON par le bulletin
    europeen, ou il est FIGE au 2020-12-21. C'est le point de tout ceci : le
    bulletin y rendait un prix de 2020 sans le dire.

    'europe' force le bulletin - utile pour comparer 27 pays dans une seule
    unite, l'avertissement de gel repart alors.

    LA SUISSE N'A PAS DE PRIX ICI : aucune API federale ne le publie (verifie le
    2026-09-02). Sa seule mesure carburant est un indice :
    indices_inflation(pays='CH', poste='carburants').

    Chaque ligne porte son unite, sa frequence et sa source. NE COMPARE PAS deux
    valeurs absolues d'unites differentes - il faudrait un taux de change a la
    date, que ce connecteur ne fait pas. La VARIATION en pourcentage, elle, est
    sans unite : c'est indices_variation.
    """
    lignes, meta = _serie_gasoil(pays, depuis, jusqu_a, produit, taxes, source)
    titre = (
        f"Gazole / carburants, {'TTC' if (taxes or 'ttc').lower().startswith('t') else 'hors taxes'}"
        f" - produit '{produit}'."
    )
    return _rendu(lignes, meta, titre, max_lignes or _max_lignes_defaut())


@mcp.tool(annotations=_hints("Inflation harmonisee IPCH", SUR_LA_SOURCE))
@_guard
def indices_inflation(
    pays: str = "",
    depuis: str = "",
    jusqu_a: str = "",
    mesure: str = "annuel",
    poste: str = "total",
    max_lignes: int = 0,
) -> str:
    """Inflation harmonisee IPCH par pays et par mois (Eurostat prc_hicp_minr).

    mesure : 'annuel' (defaut, l'inflation au sens courant), 'mensuel',
             'moyenne_12m' (ce que visent les clauses d'indexation), 'indice'
             (base 2025 = 100), 'indice_2015'.
    poste  : 'total' (defaut, tous postes) ou 'carburants'.

    Un taux et un indice ne se comparent pas : pour un ecart entre deux dates,
    c'est l'indice qu'il faut, pas la somme des taux mensuels.

    poste='carburants' EST LA REPONSE POUR LA SUISSE. Le bulletin petrolier ne
    couvre que l'Union et aucune API federale suisse ne publie de prix du
    carburant, mais Eurostat publie l'IPCH suisse, sous-position carburants
    comprise. C'est un INDICE de panier de consommation, mensuel - pas un prix
    au litre, et il ne se compare pas a un prix. Il repond a « de combien a
    bouge le carburant », ce qui suffit a une clause d'indexation.

    Le Royaume-Uni est sorti de l'IPCH fin 2020 : sur ce poste comme sur le
    total, il ne rend rien. Son equivalent est le CPI de l'ONS, qui n'est PAS
    l'IPCH - methodologie differente - et n'est pas branche ici.
    """
    lignes, meta = _serie_inflation(pays, depuis, jusqu_a, mesure, poste)
    titre = "Inflation IPCH." if (poste or "total") == "total" else (
        "Indice IPCH du poste carburants - un INDICE, pas un prix."
    )
    return _rendu(lignes, meta, titre, max_lignes or _max_lignes_defaut())


@mcp.tool(annotations=_hints("Salaire minimum legal national", SUR_LA_SOURCE))
@_guard
def indices_salaire_minimum(
    pays: str = "",
    depuis: str = "",
    jusqu_a: str = "",
    devise: str = "EUR",
    max_lignes: int = 0,
) -> str:
    """Salaire minimum legal national, semestriel, par pays (Eurostat earn_mw_cur).

    devise : EUR (defaut), NAC (monnaie nationale), PPS (pouvoir d'achat).
    Periodes : S1 = au 1er janvier, S2 = au 1er juillet.

    Huit pays de l'echantillon n'ont pas de salaire minimum legal national - le
    plancher y est conventionnel. Ils sortent SANS ligne, et l'en-tete le dit.
    Une case vide n'est pas un zero.
    """
    lignes, meta = _serie_salaire(pays, depuis, jusqu_a, devise)
    return _rendu(
        lignes, meta, "Salaire minimum legal national.", max_lignes or _max_lignes_defaut()
    )


@mcp.tool(annotations=_hints("Ecart d'un indice entre deux periodes", SUR_LA_SOURCE))
@_guard
def indices_variation(
    indice: str,
    reference: str,
    courant: str,
    pays: str = "",
    produit: str = "diesel",
    taxes: str = "ttc",
    mesure: str = "indice",
    devise: str = "EUR",
    source: str = "auto",
    poste: str = "total",
) -> str:
    """Ecart d'un indice entre une periode de reference et une periode courante.

    C'est la forme dont on a besoin pour une revision tarifaire : « de combien a
    bouge le gazole en France entre juin 2024 et aout 2026 ». La moyenne des
    releves de chaque periode est comparee, pas un point isole : une semaine
    prise seule est du bruit.

    reference / courant : '2024-06', '2024', '2024-S1', ou une date precise.
    indice : gazole, inflation, salaire_minimum.

    Sur l'inflation, la mesure par defaut est 'indice' : c'est la seule qui se
    compare entre deux dates. Comparer deux taux annuels ne donne pas une
    evolution de prix, ca donne une evolution de rythme.

    Cet outil rend un ECART CONSTATE. Il n'applique aucune clause : la formule
    de revision, son indice de reference et sa part carburant sont dans le
    contrat du transporteur.

    C'EST L'OUTIL A UTILISER QUAND LES PAYS N'ONT PAS LA MEME SOURCE. Le gazole
    britannique est en pence par litre, le norvegien en couronnes, celui de
    l'Union en euros par 1000 litres : les valeurs absolues ne se comparent pas,
    mais l'ecart en POURCENTAGE est sans unite. La colonne `unite` est rendue
    par pays, pas globalement.
    """
    lignes_ref, meta = _serie(
        indice, pays, reference, reference, produit, taxes, mesure, devise,
        source=source, poste=poste,
    )
    lignes_cur, _ = _serie(
        indice, pays, courant, courant, produit, taxes, mesure, devise,
        source=source, poste=poste,
    )
    # L'unite se lit sur les lignes, pas dans la meta : depuis que le gazole a
    # trois sources, meta['unite'] est la liste de TOUTES les unites du tableau.
    # La recopier sur chaque pays afficherait "EUR/1000 l / GBp/l / NOK/l" en
    # face de la France.
    unites: dict[str, str] = {}
    for ligne in lignes_cur + lignes_ref:
        unites.setdefault(ligne["code_pays"], str(ligne.get("unite") or ""))

    def moyenne(lignes: list[dict[str, Any]]) -> dict[str, tuple[float, int]]:
        cumul: dict[str, list[float]] = {}
        for ligne in lignes:
            valeur = ligne.get("valeur")
            if isinstance(valeur, (int, float)):
                cumul.setdefault(ligne["code_pays"], []).append(float(valeur))
        return {k: (sum(v) / len(v), len(v)) for k, v in cumul.items() if v}

    ref = moyenne(lignes_ref)
    cur = moyenne(lignes_cur)
    codes = sorted(set(ref) | set(cur))
    if not codes:
        return (
            "Aucune donnee sur les deux periodes demandees. Verifie les bornes : "
            f"reference='{reference}', courant='{courant}'. Le gazole est "
            "hebdomadaire, l'inflation mensuelle, le salaire minimum semestriel - "
            "une borne trop fine peut ne rien attraper."
        )

    tete = _entete(meta, f"Variation de '{indice}' entre {reference} et {courant}.")
    tete.append(
        "Moyenne des releves de chaque periode, pas un point isole. "
        "Ecart constate, aucune clause appliquee."
    )
    # Un pays present sur une seule des deux periodes rend un ecart VIDE, pas un
    # ecart nul. La cause est presque toujours la frequence : demander un mois a
    # une source mensuelle qui ne l'a pas encore publie n'attrape rien.
    boiteux = sorted((set(ref) | set(cur)) - (set(ref) & set(cur)))
    if boiteux:
        tete.append(
            "Ecart non calculable pour " + ", ".join(boiteux) + " : ces pays n'ont "
            "de releve que sur UNE des deux periodes. Regarde les colonnes "
            "releves_reference et releves_courant. Cause la plus frequente : une "
            "source mensuelle interrogee sur un mois qu'elle n'a pas encore publie."
        )
    tete.append("")
    sortie: list[dict[str, Any]] = []
    for code in codes:
        base, n_base = ref.get(code, (None, 0))
        fin, n_fin = cur.get(code, (None, 0))
        ecart = pct = None
        if base is not None and fin is not None:
            ecart = round(fin - base, 3)
            pct = round((fin - base) / base * 100, 2) if base else None
        sortie.append(
            {
                "code_pays": code,
                "pays": _pays_labels().get(code, code),
                "reference": reference,
                "valeur_reference": round(base, 3) if base is not None else "",
                "releves_reference": n_base,
                "courant": courant,
                "valeur_courante": round(fin, 3) if fin is not None else "",
                "releves_courant": n_fin,
                "ecart": ecart if ecart is not None else "",
                "ecart_pct": pct if pct is not None else "",
                "unite": unites.get(code, meta.get("unite", "")),
            }
        )
    return "\n".join(tete) + _csv_text(sortie)


@mcp.tool(annotations=_hints("Exporter une serie complete en CSV", SUR_LA_SOURCE, idempotent=False))
@_guard
def indices_export_csv(
    indice: str,
    pays: str = "",
    depuis: str = "",
    jusqu_a: str = "",
    produit: str = "diesel",
    taxes: str = "ttc",
    mesure: str = "annuel",
    devise: str = "EUR",
    source: str = "auto",
    poste: str = "total",
    filename: str = "",
) -> str:
    """Exporte une serie complete en CSV. SUR DEMANDE EXPLICITE seulement.

    Meme perimetre que les outils de lecture, mais SANS plafond d'affichage :
    c'est la sortie a produire pour une etude de revision tarifaire ou un
    controle de surcharge, qui se traitent sur un fichier.

    CSV point-virgule, UTF-8 avec BOM : il s'ouvre dans Excel FR sans assistant
    d'import. Le fichier atterrit dans le dossier local du connecteur, JAMAIS
    dans la bibliotheque d'equipe - un CSV depose dans un dossier synchronise
    part chez tout le monde, et la base de connaissance ne porte pas de donnees.
    """
    lignes, meta = _serie(
        indice, pays, depuis, jusqu_a, produit, taxes, mesure, devise,
        source=source, poste=poste,
    )
    if not lignes:
        return (
            "Rien a exporter : aucune ligne sur ce perimetre. Aucun fichier "
            "n'a ete ecrit. " + ("Sans donnee : " + ", ".join(str(a) for a in meta.get("absents") or []) if meta.get("absents") else "")
        )
    cible_dir = _export_dir()
    try:
        cible_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Dossier d'export inutilisable ({cible_dir}) : {exc}") from exc
    nom = (filename or "").strip() or (
        f"indices_{_norm(indice).replace(' ', '_')}_"
        + dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        + ".csv"
    )
    if not nom.lower().endswith(".csv"):
        nom += ".csv"
    cible = cible_dir / pathlib.Path(nom).name
    cols = _colonnes(lignes)
    try:
        with cible.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=cols, delimiter=";", extrasaction="ignore"
            )
            writer.writeheader()
            for ligne in lignes:
                writer.writerow({c: ligne.get(c, "") for c in cols})
    except OSError as exc:
        raise ConfigError(f"Ecriture impossible dans {cible} : {exc}") from exc

    sortie = [
        f"Export termine : {cible}",
        f"{len(lignes)} ligne(s), {len(cols)} colonne(s). Serie COMPLETE sur le "
        "perimetre demande, aucune troncature.",
    ]
    sortie.extend(_entete(meta, "Perimetre exporte :")[1:])
    return "\n".join(sortie)


@mcp.tool(annotations=_hints("Rappeler les sources maintenant", SUR_LA_SOURCE, idempotent=False))
@_guard
def indices_refresh(indice: str = "") -> str:
    """Force le rapatriement d'une source, sans attendre l'expiration du cache.

    A appeler le jour ou le bulletin vient de paraitre, ou apres une correction
    de l'URL. Vide = les trois. Le bulletin petrolier pese 4,4 Mo : c'est
    quelques secondes.
    """
    cible = (indice or "").strip().lower()
    faits: list[str] = []
    if cible in {"", "gazole", "gasoil", "diesel", "carburant"}:
        for nom, charge in (
            ("bulletin europeen", _wob_data),
            ("Royaume-Uni (DESNZ)", _desnz_data),
            ("Norvege (SSB)", _ssb_data),
        ):
            entry = charge(force=True)
            dates = ((entry.get("data") or {}).get("ttc") or {}).get("dates") or []
            reels = [d for d in dates if d]
            faits.append(
                f"gazole / {nom} : {len(reels)} periodes, du "
                f"{min(reels) if reels else '?'} au {max(reels) if reels else '?'}"
                f" - {entry.get('provenance', '')}"
            )
    if cible in {"", "inflation", "ipch", "hicp"}:
        for mesure, (unit, _, _) in HICP_MESURES.items():
            for poste, (coicop, _, _) in HICP_POSTES.items():
                entry = _eurostat_data(
                    f"inflation_{unit}_{coicop}",
                    HICP_FLOW,
                    {"freq": "M", "unit": unit, "coicop18": coicop},
                    force=True,
                )
                faits.append(
                    f"inflation/{mesure}/{poste} : "
                    f"{len(entry.get('points') or [])} points, "
                    f"millesime {entry.get('millesime', '?')}"
                )
    if cible in {"", "salaire_minimum", "smic", "salaire"}:
        for devise in MW_DEVISES:
            entry = _eurostat_data(
                f"salaire_minimum_{devise}", MW_FLOW, {"freq": "S", "currency": devise}, force=True
            )
            faits.append(
                f"salaire_minimum/{devise} : {len(entry.get('points') or [])} points, "
                f"millesime {entry.get('millesime', '?')}"
            )
        entry = _nmw_data(force=True)
        faits.append(
            f"salaire_minimum / Royaume-Uni (gov.uk) : "
            f"{len(entry.get('lignes') or [])} periodes, la plus recente "
            f"{entry.get('millesime', '?')}"
        )
    if not faits:
        raise ConfigError(
            f"Indice inconnu : '{indice}'. Au choix : " + ", ".join(INDICES) + ", ou vide pour les trois."
        )
    return "Rapatriement termine.\n" + "\n".join(faits)


@mcp.tool(annotations=_hints("Diagnostic : configuration, sources, cache", SUR_LA_SOURCE))
@_guard
def indices_doctor() -> str:
    """Diagnostic : interpreteur, chemins, configuration, et joignabilite des sources.

    C'est ici qu'un ecart de plateforme se voit avant d'etre un incident, et
    c'est ici que se lit l'etat du certificat du bulletin petrolier.
    """
    lignes = [
        "MCP indices-eu - diagnostic",
        "",
        f"Interpreteur         : {sys.executable}",
        f"Python               : {sys.version.split()[0]} sur {sys.platform}",
        f"Racine locale        : {_local_root()}",
        f"Dossier de cache     : {_cache_dir()}",
        f"Dossier d'export     : {_export_dir()}",
        f"Duree de cache       : {_ttl_hours():.1f} h",
        f"Mode TLS gazole      : {_tls_mode()}",
    ]
    _load_env_file()
    lignes.append(
        f"Config du poste      : {_ENV_LOADED_FROM or 'aucune (defauts du serveur)'}"
    )
    if _ENV_LOAD_ERROR:
        lignes.append(f"  ATTENTION          : {_ENV_LOAD_ERROR}")
    shared = _load_shared_env()
    if _SHARED_LOADED_FROM:
        lignes.append(f"Config d'equipe      : {_SHARED_LOADED_FROM}")
        lignes.append(
            "  reglages repris    : " + (", ".join(sorted(shared)) if shared else "(aucun)")
        )
    else:
        lignes.append("Config d'equipe      : aucune (valeurs par defaut du serveur)")
        for path in _shared_env_candidates():
            lignes.append(f"    absent  {path}")
    if _SHARED_LOCAL_SEEN:
        lignes.append(
            "  ATTENTION : cles LOCALES par nature trouvees dans le fichier "
            "d'equipe, ignorees : " + ", ".join(sorted(set(_SHARED_LOCAL_SEEN)))
        )
    if _SHARED_REJECTED:
        lignes.append(
            "  ATTENTION : cles inconnues dans le fichier d'equipe, IGNOREES : "
            + ", ".join(sorted(set(_SHARED_REJECTED)))
            + ". Dans la quasi-totalite des cas, c'est une faute de frappe - "
            "compare avec indices.env.example. Une cle ignoree ne produit "
            "aucune erreur ailleurs."
        )
    if _LEXIQUE_ERROR:
        lignes.append(f"Lexique              : {_LEXIQUE_ERROR}")
    else:
        lignes.append(
            f"Lexique              : {len(_pays_labels())} pays, "
            f"{len(_lexique().get('alias') or {})} alias"
        )
    lignes.append("")
    lignes.append("Sources :")

    verdicts: list[bool] = []
    # Une source par ligne, chacune sondee sur le pays qu'elle sert : sinon un
    # gazole "OK" sur la France masquerait un DESNZ hors service.
    for nom, pays_test, appel in (
        ("gazole / Union", "FR", lambda: _serie_gasoil("FR", "", "", "diesel", "ttc")),
        (
            "gazole / UK (DESNZ)",
            "UK",
            lambda: _serie_gasoil("UK", "", "", "diesel", "ttc"),
        ),
        ("gazole / NO (SSB)", "NO", lambda: _serie_gasoil("NO", "", "", "diesel", "ttc")),
    ):
        try:
            lignes_g, meta_g = appel()
            dernier = lignes_g[0] if lignes_g else None
            verdicts.append(bool(dernier))
            lignes.append(
                f"  {nom:22s} : OK | "
                + (
                    f"{pays_test} au {dernier['periode']} = {dernier['valeur']} "
                    f"{dernier['unite']}"
                    if dernier
                    else "aucune ligne"
                )
            )
            if meta_g.get("provenance"):
                lignes.append(f"      provenance         : {meta_g['provenance'][:150]}")
        except (ConfigError, SourceError) as exc:
            verdicts.append(False)
            lignes.append(f"  {nom:22s} : ECHEC | {exc}")

    for nom, appel in (
        ("inflation", lambda: _serie_inflation("FR", "", "", "annuel")),
        (
            "indice carburant CH",
            lambda: _serie_inflation("CH", "", "", "annuel", poste="carburants"),
        ),
        ("salaire minimum", lambda: _serie_salaire("FR", "", "", "EUR")),
        ("salaire min. UK", lambda: _serie_salaire("UK", "", "", "EUR")),
    ):
        try:
            lignes_x, meta_x = appel()
            dernier = lignes_x[0] if lignes_x else None
            verdicts.append(bool(dernier))
            lignes.append(
                f"  {nom:22s} : OK | "
                + (
                    f"{dernier['code_pays']} en {dernier['periode']} = "
                    f"{dernier['valeur']} ({dernier.get('unite', '')})"
                    if dernier
                    else "aucune ligne"
                )
                + f" | millesime {meta_x.get('millesime', '?')}"
            )
        except (ConfigError, SourceError) as exc:
            verdicts.append(False)
            lignes.append(f"  {nom:22s} : ECHEC | {exc}")

    # Les etats de cache se lisent APRES les controles : avant, ils diraient
    # "absent" sur une source que le controle vient de rapatrier avec succes.
    lignes.append("")
    lignes.append("Cache local, apres ces controles :")
    for cle, libelle in (
        ("gasoil", "gazole / bulletin petrolier europeen"),
        ("gasoil_uk", "gazole / DESNZ (gov.uk)"),
        ("gasoil_no", "gazole / SSB (data.ssb.no)"),
        ("inflation_RCH_A_TOTAL", "inflation / Eurostat " + HICP_FLOW + " TOTAL"),
        ("inflation_RCH_A_CP0722", "indice carburant / Eurostat CP0722"),
        ("salaire_minimum_EUR", "salaire minimum / Eurostat " + MW_FLOW),
        ("salaire_minimum_uk", "salaire minimum / gov.uk (GBP par heure)"),
    ):
        lignes.append(f"  {libelle:44s} : {_cache_stamp(cle)}")

    lignes.append("")
    lignes.append(
        "Verdict : "
        + (
            f"les {len(verdicts)} sources repondent."
            if all(verdicts)
            else "au moins une source ne repond pas, voir ci-dessus."
        )
    )
    return "\n".join(lignes)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

HELP = """\
MCP indices-eu - gazole, inflation et salaire minimum en Europe, en lecture seule.

  python server.py            mode serveur MCP (stdio), lance par Claude Code
  python server.py doctor     diagnostic de configuration et de sources
  python server.py --help     cette aide

Configuration, en deux couches, et aucun secret nulle part :
  - l'equipe : 08_ENGINE/04_mcp/00_config/indices.shared.env - URL du bulletin
    petrolier, plafonds, duree de cache ;
  - le poste : indices.env hors du vault, par defaut ~/.indices-mcp/indices.env.
    Modele : indices.env.example.
Priorite : configuration du plugin > indices.env du poste > fichier d'equipe >
defaut du serveur.
"""


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if arg == "doctor":
        rapport = indices_doctor()
        print(rapport)
        # Le verdict porte le NOMBRE de sources, qui change quand on en branche
        # une. Chercher « les trois sources repondent » rendait donc un code
        # d'echec alors que tout allait bien, des l'ajout de la quatrieme - une
        # suite d'integration se serait mise au rouge sans rien de casse. On
        # cherche l'echec, qui est la seule formulation stable.
        return 1 if "au moins une source ne repond pas" in rapport else 0
    if arg:
        print(f"Argument inconnu : {arg}\n")
        print(HELP)
        return 2
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
