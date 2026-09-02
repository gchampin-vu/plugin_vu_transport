#!/usr/bin/env python3
"""
MCP taux-de-change : les taux de change de chancellerie DGFiP, en LECTURE SEULE.

Trois usages :
    python server.py            # mode serveur MCP (stdio) - lance par Claude Code
    python server.py doctor     # diagnostic de configuration et de connexion
    python server.py --help

Source : le jeu de donnees public `dgfip-taux-de-change` du portail open data
du ministere de l'Economie (https://data.economie.gouv.fr), API Opendatasoft
Explore v2.1. Licence Ouverte v2.0 (Etalab), mise a jour mensuelle. **Aucune
cle d'API** : rien a saisir, rien a ranger, aucun secret dans ce connecteur.

Lecture seule par construction : l'API Explore n'expose que des GET, et ce
serveur n'emet que des GET. Il n'y a aucune operation d'ecriture a exposer.

C'est le portage du script Power Query qui ramenait ce jeu de donnees dans
Power BI, avec quatre differences qui comptent :

1. Le script paginait par `offset` de 10 en 10. L'API refuse `offset + limit`
   au-dela de 10 000 : la pagination par offset **ne peut pas** lire les
   24 103 lignes du jeu. Ici, la lecture complete passe par `/exports/json`,
   qui n'a pas ce plafond.
2. Le script rendait la table brute. Or un taux n'est **republie que quand il
   change** : filtrer sur le mois courant rend 44 devises sur 189. Ce serveur
   resout le taux **en vigueur a une date** - le dernier publie a cette date -
   ce qui est la seule lecture juste du jeu de donnees.
3. Le jeu porte une ligne par couple (devise, pays) : 24 103 lignes pour
   262 couples et 189 devises seulement. Compter les lignes ou moyenner une
   colonne sans dedoublonner rend un chiffre faux. Ce serveur dedoublonne et
   dit combien de pays portaient la devise.
4. Le sens du taux est rappele dans chaque reponse : `taux` est le nombre
   d'euros que vaut UNE unite de la devise.

Releve du jeu de donnees le 2026-09-01 : 24 103 lignes, 189 devises,
262 couples devise/pays, 659 dates de publication, du 1990-01-01 au 2026-09-01.

Configuration : rien d'obligatoire. Le reglage d'equipe optionnel est dans
08_ENGINE/04_mcp/00_config/taux.shared.env. Voir README.md.
"""

from __future__ import annotations

import csv
import datetime as dt
import decimal
import functools
import io
import json
import os
import pathlib
import re
import sys
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

DEFAULT_BASE_URL = "https://data.economie.gouv.fr"
DEFAULT_DATASET = "dgfip-taux-de-change"
API_ROOT = "/api/explore/v2.1/catalog/datasets"

# Plafonds imposes par l'API Explore v2.1, mesures le 2026-09-01. Ils ne sont
# pas negociables, et le second n'est pas cosmetique : c'est lui qui rend la
# pagination par offset inutilisable sur ce jeu, et qui justifie que la lecture
# complete passe par /exports.
PAGE_LIMIT_MAX = 100
OFFSET_PLUS_LIMIT_MAX = 10_000

DEFAULT_TIMEOUT_S = 30.0
# Duree de vie de l'instantane en memoire. Le jeu est mensuel : une demi-journee
# est large, et evite de retelecharger 1,3 Mo a chaque question.
DEFAULT_CACHE_TTL_S = 21_600

# Plafond de caracteres rendus dans la conversation par un outil de liste.
RENDER_MAX_CHARS = 24_000

# La date avant laquelle ce jeu de donnees ne couvre presque rien, et ou une
# reponse vide n'est PAS une anomalie du connecteur.
#
# La plage annoncee par le portail commence au 1990-01-01, et c'est trompeur :
# 83 lignes seulement sont anterieures a 1999, sur neuf devises obscures
# (AOR, BGL, CUI, GWP, KPW, KYD, SZL, TJR, TOP). Les grandes devises arrivent
# toutes au printemps et a l'ete 2005 - GBP le 2005-04-16, PLN le 2005-05-01,
# USD le 2005-06-01, CHF le 2005-08-01, la derniere. Mesure du 2026-09-01 : le
# nombre de devises ayant un taux applicable passe de 12 au 2000-06-30 a 162 au
# 2005-06-30, puis 189 aujourd'hui.
#
# Consequence pratique : une question sur 2003 rend legitimement zero ligne pour
# le dollar. Sans ce reperage, ce vide se lit comme une panne du connecteur.
PREMIERE_DATE_UTILE = "2005-08-01"

# Le sens du taux, ecrit une fois et cite partout. Une inversion de sens est
# l'erreur la plus couteuse possible sur ce jeu de donnees, et la plus facile a
# commettre : elle ne leve aucune erreur, elle rend un montant plausible.
SENS_DU_TAUX = "taux = nombre d'EUROS que vaut UNE unite de la devise (1 devise = taux EUR)"

CHAMPS = (
    "monnaie_source",
    "nom_monnaie_source",
    "pays_principal",
    "code_pays",
    "date",
    "taux",
    "monnaievigueur",
)

ENGINE_DIR_NAME = "08_ENGINE"
SHARED_SUBPATH = ("04_mcp", "00_config")
SHARED_FILE_NAME = "taux.shared.env"

# Liste blanche du fichier d'equipe. **Aucune cle secrete ici, et il n'y en
# aura pas** : le jeu de donnees est public. Un connecteur sans secret n'a besoin
# ni d'un champ `sensitive`, ni d'un outil pour ranger une cle - c'est une bonne
# nouvelle a dire au collegue qui l'installe.
SHARED_ALLOWED_KEYS = frozenset(
    {
        "TAUX_BASE_URL",
        "TAUX_DATASET",
        "TAUX_TIMEOUT_S",
        "TAUX_CACHE_TTL_S",
        "TAUX_EXPORT_DIR",
    }
)

HELP = """MCP taux-de-change : taux de change de chancellerie DGFiP, en lecture seule.

    python server.py            mode serveur MCP (stdio), lance par Claude Code
    python server.py doctor     diagnostic de configuration et de connexion
    python server.py --help     cet ecran

Jeu de donnees public, aucune cle d'API. Reglage d'equipe optionnel dans
08_ENGINE/04_mcp/00_config/taux.shared.env.
"""


class ConfigError(RuntimeError):
    pass


class TauxError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Configuration : le fichier d'equipe, puis le poste
# --------------------------------------------------------------------------

_SHARED_CACHE: dict[str, str] | None = None
_SHARED_LOADED_FROM: str = ""
_SHARED_REJECTED: list[str] = []

_ENV_LOADED_FROM: str = ""
_ENV_DONE: bool = False


def _parse_env(text: str) -> dict[str, str]:
    """Lit un fichier .env simple : CLE=valeur, # en commentaire."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key.strip()] = val
    return out


def _local_root() -> pathlib.Path:
    """Racine locale de l'outil : instantane de repli et exports.

    Sous le profil utilisateur, pas sous %LOCALAPPDATA% ni sous le dossier de
    donnees du plugin. Un Python empaquete (Microsoft Store, Python Manager)
    donne a ses processus enfants une vue VIRTUALISEE de %LOCALAPPDATA% : le
    serveur lance par le plugin et le meme serveur lance en ligne de commande ne
    verraient pas le meme dossier, sans aucune erreur. Et le dossier de donnees
    du plugin depend de l'identifiant d'installation.

    os.environ et non _env : cette fonction est appelee pendant le chargement du
    fichier .env local, et passer par _env ferait un aller-retour.
    """
    raw = (os.environ.get("TAUX_HOME") or "").strip()
    if raw:
        return pathlib.Path(raw)
    return pathlib.Path.home() / ".taux-de-change-mcp"


def _load_env_file() -> None:
    """Verse dans l'environnement le .env local, s'il y en a un.

    Il n'est pas necessaire : ce connecteur n'a pas de secret. Il reste lu pour
    qu'un poste puisse fixer un reglage - un miroir de l'API, un dossier
    d'export - sans passer par l'interface du plugin.
    """
    global _ENV_DONE, _ENV_LOADED_FROM
    if _ENV_DONE:
        return
    _ENV_DONE = True
    explicit = (os.environ.get("TAUX_ENV_FILE") or "").strip()
    path = pathlib.Path(explicit) if explicit else _local_root() / ".env"
    try:
        if not path.is_file():
            return
        values = _parse_env(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return
    for key, val in values.items():
        # Le poste ne surcharge pas ce que le plugin a deja pose : ce qui vient
        # de l'environnement gagne.
        if val.strip() and not os.environ.get(key):
            os.environ[key] = val.strip()
    _ENV_LOADED_FROM = str(path)


def _engine_roots() -> list[pathlib.Path]:
    """Racines `08_ENGINE` plausibles, dans l'ordre de preference.

    Le plugin est installe depuis `08_ENGINE/03_plugins/...`, mais Claude Code
    en fait une copie dans `~/.claude/plugins/`. On ne peut donc pas se
    contenter de remonter depuis le code : on remonte quand meme (installation
    directe depuis la bibliotheque), et on complete par le profil utilisateur,
    ou OneDrive synchronise la bibliotheque d'equipe. Le point de montage
    differe selon l'OS - on ne le devine pas, on le cherche.
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
    """Emplacements du fichier d'equipe essayes, dans l'ordre.

    **Un chemin explicite est EXCLUSIF.** Pointer un fichier precis desactive la
    recherche sous le profil, au lieu de le mettre en premier et de garder le
    fichier reel en second. C'est ce qui permet a la suite de controles
    hors-ligne de simuler un poste sans configuration d'equipe - un garde-fou
    qui tourne en fait avec la vraie configuration finit par etre ignore.
    """
    explicit = (os.environ.get("TAUX_SHARED_ENV") or "").strip()
    if explicit:
        return [pathlib.Path(explicit)]
    return [root.joinpath(*SHARED_SUBPATH, SHARED_FILE_NAME) for root in _engine_roots()]


def _load_shared_env() -> dict[str, str]:
    """Lit le premier fichier d'equipe trouve, filtre par la liste blanche.

    Ne leve jamais : un fichier absent, illisible ou mal rempli ne doit pas
    empecher le connecteur de tourner sur ses valeurs par defaut. Ce qui a ete
    refuse est garde de cote pour que le doctor le dise - une cle mal
    orthographiee ne produit aucune erreur ailleurs.
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


def _env(name: str, default: str = "") -> str:
    """La valeur retenue pour un reglage, dans un ordre de priorite fixe.

    1. l'environnement du processus - la configuration du plugin, et le .env
       local que _load_env_file y a deja verse : ce que ce poste a decide ;
    2. le fichier d'equipe de 08_ENGINE ;
    3. la valeur par defaut du serveur.

    Le poste avant l'equipe : qui a surcharge un reglage chez lui attend que ca
    tienne.
    """
    _load_env_file()
    val = (os.environ.get(name) or "").strip()
    if val:
        return val
    val = (_load_shared_env().get(name) or "").strip()
    if val:
        return val
    return default


def _origine_reglage(name: str) -> str:
    """D'ou sort un reglage : poste, equipe, ou defaut du serveur.

    La premiere question quand une reponse surprend. Un reglage d'equipe se
    corrige dans 08_ENGINE, pour tout le monde ; un reglage de poste se corrige
    dans /plugin, pour soi seul.
    """
    _load_env_file()
    if (os.environ.get(name) or "").strip():
        return "poste"
    if (_load_shared_env().get(name) or "").strip():
        return "equipe"
    return "defaut serveur"


def _base_url() -> str:
    return _env("TAUX_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _dataset() -> str:
    return _env("TAUX_DATASET", DEFAULT_DATASET)


def _timeout() -> float:
    try:
        return float(_env("TAUX_TIMEOUT_S", str(DEFAULT_TIMEOUT_S)))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _cache_ttl() -> float:
    try:
        return float(_env("TAUX_CACHE_TTL_S", str(DEFAULT_CACHE_TTL_S)))
    except ValueError:
        return DEFAULT_CACHE_TTL_S


def _export_dir() -> pathlib.Path:
    raw = _env("TAUX_EXPORT_DIR")
    if raw:
        return pathlib.Path(raw)
    return _local_root() / "exports"


def _snapshot_path() -> pathlib.Path:
    return _local_root() / "instantane.json"


# --------------------------------------------------------------------------
# L'appel HTTP : GET seulement
# --------------------------------------------------------------------------

def _dataset_url(suffix: str = "") -> str:
    return f"{_base_url()}{API_ROOT}/{_dataset()}{suffix}"


def _get(suffix: str, query: dict[str, Any] | None = None) -> httpx.Response:
    """Un GET sur le jeu de donnees. Le seul verbe que ce serveur sait emettre."""
    params = {k: str(v) for k, v in (query or {}).items() if v not in (None, "")}
    url = _dataset_url(suffix)
    try:
        with httpx.Client(timeout=_timeout(), follow_redirects=True) as client:
            resp = client.get(url, params=params, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise TauxError(
            f"Appel impossible ({url}) : {exc}. Si le poste est derriere un "
            "proxy, verifie HTTP_PROXY / HTTPS_PROXY."
        ) from exc
    if resp.status_code >= 400:
        raise TauxError(
            f"HTTP {resp.status_code} sur {url} (parametres : "
            f"{json.dumps(params, ensure_ascii=False)}) : {resp.text[:400]}"
        )
    return resp


def _json(suffix: str, query: dict[str, Any] | None = None) -> Any:
    resp = _get(suffix, query)
    try:
        return resp.json()
    except ValueError as exc:
        raise TauxError(f"Reponse non JSON sur {suffix} : {resp.text[:200]}") from exc


# --------------------------------------------------------------------------
# L'instantane : le jeu entier, en memoire, avec repli local
# --------------------------------------------------------------------------
#
# Pourquoi le jeu entier plutot que des appels filtres. Trois raisons, et la
# troisieme est la vraie.
#
# 1. Il est petit : 24 103 lignes, 1,3 Mo en JSON. Le telecharger coute un appel
#    et une seconde.
# 2. Presque toute question utile est une question "en vigueur a une date", et y
#    repondre demande, par devise, la derniere publication anterieure a cette
#    date. Cela ne s'exprime pas en un filtre ODSQL : il faudrait un appel par
#    devise, soit 189 appels.
# 3. Une fois l'instantane en memoire, une reponse fausse devient une erreur de
#    code, visible en test, au lieu d'un filtre approximatif qui rend des lignes
#    plausibles. C'est ce qui permet a test_offline.py de tout verifier sans
#    reseau.

_SNAP: list[dict[str, Any]] | None = None
_SNAP_AT: float = 0.0
_SNAP_ORIGIN: str = ""
_SNAP_FETCHED_AT: str = ""


def _normalise(row: dict[str, Any]) -> dict[str, Any]:
    """Une ligne du jeu : colonnes connues, date en AAAA-MM-JJ, taux en float.

    L'API rend `date` en AAAA-MM-JJ sur /records et /exports/json, mais en
    horodatage ISO complet (`2026-09-01T00:00:00+00:00`) des qu'une agregation
    passe par `max(date)`. On tronque : une date de mise en vigueur n'a pas
    d'heure, et deux formats dans la meme colonne casseraient la comparaison de
    chaines dont depend toute la logique "en vigueur a une date".
    """
    out: dict[str, Any] = {champ: row.get(champ) for champ in CHAMPS}
    date = str(out.get("date") or "")
    out["date"] = date[:10] if len(date) > 10 else date
    try:
        out["taux"] = float(out["taux"]) if out["taux"] not in (None, "") else None
    except (TypeError, ValueError):
        out["taux"] = None
    try:
        out["monnaievigueur"] = int(out["monnaievigueur"])
    except (TypeError, ValueError):
        out["monnaievigueur"] = None
    return out


def _write_snapshot(rows: list[dict[str, Any]]) -> None:
    """Depose l'instantane sur le disque, pour le jour ou l'API ne repond pas.

    Un portail open data tombe, ou le poste est hors reseau. Un taux de
    chancellerie du mois dernier reste exploitable **si on dit qu'il date** :
    c'est tout l'objet du champ `recupere_le`, qui est cite dans chaque reponse
    servie depuis le repli.
    """
    payload = {
        "source": _dataset_url(),
        "recupere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "lignes": rows,
    }
    try:
        path = _snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except OSError:
        # Un repli qu'on n'arrive pas a ecrire n'est pas une raison de faire
        # echouer la lecture qui vient de reussir.
        pass


def _read_snapshot() -> tuple[list[dict[str, Any]], str]:
    path = _snapshot_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TauxError(f"aucun instantane local exploitable ({path}) : {exc}") from exc
    rows = [_normalise(r) for r in payload.get("lignes") or []]
    if not rows:
        raise TauxError(f"instantane local vide ({path})")
    return rows, str(payload.get("recupere_le") or "date inconnue")


def _snapshot(force: bool = False) -> list[dict[str, Any]]:
    """Le jeu de donnees entier, mis en cache, avec repli sur le disque."""
    global _SNAP, _SNAP_AT, _SNAP_ORIGIN, _SNAP_FETCHED_AT
    if not force and _SNAP is not None and (time.time() - _SNAP_AT) < _cache_ttl():
        return _SNAP

    try:
        # /exports/json et PAS /records : /records refuse offset + limit au-dela
        # de 10 000, il ne peut donc pas rendre les 24 103 lignes du jeu.
        payload = _json("/exports/json", {"limit": -1})
        if not isinstance(payload, list):
            raise TauxError("/exports/json n'a pas rendu une liste JSON")
        rows = [_normalise(r) for r in payload]
        if not rows:
            raise TauxError("/exports/json a rendu zero ligne")
    except TauxError as exc:
        erreur = str(exc)
        try:
            rows, quand = _read_snapshot()
        except TauxError as repli:
            raise TauxError(
                f"{erreur}\nEt le repli local a echoue : {repli}\n"
                "Aucune donnee disponible. Ce connecteur ne rend pas un taux de "
                "memoire : il n'y a pas de reponse a cette question pour "
                "l'instant."
            ) from None
        _SNAP, _SNAP_AT = rows, time.time()
        _SNAP_FETCHED_AT = quand
        _SNAP_ORIGIN = (
            f"REPLI LOCAL {_snapshot_path()} (recupere le {quand}) - "
            "l'API n'a pas repondu"
        )
        return rows

    _SNAP, _SNAP_AT = rows, time.time()
    _SNAP_FETCHED_AT = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    _SNAP_ORIGIN = f"API {_dataset_url()} (lu le {_SNAP_FETCHED_AT})"
    _write_snapshot(rows)
    return rows


def _origine() -> str:
    """La ligne d'origine des donnees, citee dans chaque reponse.

    Elle n'est pas decorative : c'est la difference entre un taux du jour et un
    taux servi depuis un repli vieux de trois semaines.
    """
    return _SNAP_ORIGIN or "instantane non encore charge"


# --------------------------------------------------------------------------
# Dates et devises : ce que l'utilisateur ecrit, ce que le jeu comprend
# --------------------------------------------------------------------------

_MOIS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12,
}


def _today() -> str:
    return dt.date.today().isoformat()


def _parse_date(value: str, quoi: str = "date") -> tuple[str, str]:
    """Rend (date AAAA-MM-JJ, note a afficher).

    Accepte AAAA-MM-JJ, AAAA-MM, une chaine vide et "aujourd'hui".

    **AAAA-MM est resolu au 1er du mois, et c'est dit dans la reponse.** Les
    taux de chancellerie prennent effet le 1er dans la periode recente, mais le
    jeu porte aussi des mises en vigueur en cours de mois (2009-06-16,
    2003-10-16) : sur ces mois-la, "septembre" et "le 1er septembre" ne
    designent pas le meme taux. On ne devine pas a la place de l'utilisateur, on
    resout et on annonce ce qu'on a resolu.
    """
    raw = (value or "").strip().lower()
    if not raw or raw in {"aujourd'hui", "aujourdhui", "today", "now", "maintenant"}:
        return _today(), "date resolue a aujourd'hui"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            dt.date.fromisoformat(raw)
        except ValueError as exc:
            raise TauxError(f"{quoi} invalide : {value} ({exc})") from exc
        return raw, ""
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return f"{raw}-01", f"{value} resolu au 1er du mois ({raw}-01)"
    if re.fullmatch(r"\d{4}", raw):
        return f"{raw}-12-31", f"{value} resolu au 31 decembre ({raw}-12-31)"
    mois = re.fullmatch(r"([a-z]+)\s+(\d{4})", raw)
    if mois and mois.group(1) in _MOIS:
        stamp = f"{mois.group(2)}-{_MOIS[mois.group(1)]:02d}-01"
        return stamp, f"{value} resolu au 1er du mois ({stamp})"
    raise TauxError(
        f"{quoi} non comprise : {value}. Formats acceptes : AAAA-MM-JJ, AAAA-MM, "
        "AAAA, 'septembre 2026', ou vide pour aujourd'hui."
    )


def _devises(spec: str) -> list[str]:
    """La liste de codes ISO demandee. Vide = toutes."""
    if not (spec or "").strip():
        return []
    parts = re.split(r"[,;\s]+", spec.strip().upper())
    return [p for p in parts if p]


# --------------------------------------------------------------------------
# Le coeur : le taux en vigueur a une date
# --------------------------------------------------------------------------

def _en_vigueur(
    date: str,
    codes: list[str] | None = None,
    vigueur_seulement: bool = True,
) -> list[dict[str, Any]]:
    """Le taux en vigueur a `date`, une ligne par devise.

    C'EST LA FONCTION QUI JUSTIFIE CE CONNECTEUR. Le jeu de donnees ne porte pas
    un taux par devise et par mois : il porte une ligne **le jour ou le taux
    change**. Au 2026-09-01, 44 devises seulement ont une ligne datee de ce
    jour, alors que 189 ont un taux applicable - GBP n'avait pas bouge depuis
    fevrier, PLN depuis avril. Un `where date = '2026-09-01'` rend donc 44
    devises sur 189, et l'absence de GBP se lit comme "pas de taux pour la livre".

    Pour chaque devise, on retient donc la derniere publication de date
    inferieure ou egale a la date demandee, et on rend aussi cette date d'effet :
    un taux sans sa date d'effet n'est pas verifiable.

    Le dedoublonnage par pays est l'autre moitie du travail : le jeu porte une
    ligne par couple (devise, pays), donc USD apparait plus de dix fois a chaque
    publication, avec le meme taux. On garde une ligne et on compte les pays.
    """
    voulus = {c.upper() for c in (codes or [])}
    par_devise: dict[str, dict[str, Any]] = {}
    for row in _snapshot():
        code = str(row.get("monnaie_source") or "").upper()
        if not code or (voulus and code not in voulus):
            continue
        jour = str(row.get("date") or "")
        if not jour or jour > date:
            continue
        cur = par_devise.get(code)
        if cur is None or jour > cur["date_effet"]:
            par_devise[code] = {
                "monnaie_source": code,
                "nom_monnaie_source": row.get("nom_monnaie_source"),
                "date_effet": jour,
                "taux": row.get("taux"),
                "monnaievigueur": row.get("monnaievigueur"),
                "pays": [row.get("pays_principal")],
                "taux_vus": [row.get("taux")],
            }
        elif jour == cur["date_effet"]:
            cur["pays"].append(row.get("pays_principal"))
            cur["taux_vus"].append(row.get("taux"))

    out: list[dict[str, Any]] = []
    for code in sorted(par_devise):
        entry = par_devise[code]
        if vigueur_seulement and entry["monnaievigueur"] != 1:
            continue
        pays = [p for p in entry["pays"] if p]
        taux_vus = {t for t in entry["taux_vus"] if t is not None}
        entry["nb_pays"] = len(set(pays))
        entry["pays"] = " | ".join(sorted(set(pays)))
        # Deux devises portent des taux qui divergent a la 10e decimale sur une
        # meme date (XOF et XAF au 2002-01-01 : la parite fixe du franc CFA
        # arrondie pays par pays). On le dit plutot que de choisir en silence.
        entry["taux_divergents"] = len(taux_vus) > 1
        if entry["taux_divergents"]:
            entry["taux_min"] = min(taux_vus)
            entry["taux_max"] = max(taux_vus)
        entry.pop("taux_vus", None)
        out.append(entry)
    return out


def _une_devise(date: str, code: str) -> dict[str, Any]:
    """Le taux en vigueur a `date` pour une devise, ou une erreur qui dit quoi faire."""
    code = code.strip().upper()
    if not code:
        raise TauxError("devise manquante : donne un code ISO, par exemple USD ou PLN.")
    # vigueur_seulement=False : sur une question ciblee, l'utilisateur a nomme la
    # devise. Lui repondre "inconnue" parce qu'elle n'a plus cours serait faux -
    # elle a un taux, et sa date d'effet le dira.
    trouve = _en_vigueur(date, [code], vigueur_seulement=False)
    if trouve:
        return trouve[0]

    connues = {str(r.get("monnaie_source") or "").upper() for r in _snapshot()}
    if code not in connues:
        proches = sorted(c for c in connues if c.startswith(code[:1]))[:12]
        raise TauxError(
            f"devise inconnue dans le jeu de donnees : {code}. Le jeu porte "
            f"{len(connues)} devises. Codes commencant par {code[:1]} : "
            f"{', '.join(proches) or '(aucun)'}. Appelle taux_devises pour la "
            "liste complete - ne devine pas un code ISO."
        )
    premiere = min(
        (str(r.get("date")) for r in _snapshot() if str(r.get("monnaie_source", "")).upper() == code),
        default="?",
    )
    raise TauxError(
        f"{code} existe dans le jeu, mais aucune publication n'est anterieure ou "
        f"egale au {date} : sa premiere publication est du {premiere}. Il n'y a "
        "pas de taux a rendre pour cette date."
    )


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------

def _fmt_taux(val: Any) -> str:
    """Le taux, sans arrondi de notre fait.

    repr() d'un float Python rend la representation la plus courte qui reconverge
    vers la meme valeur : on n'ajoute ni ne retire de precision. Un taux arrondi
    par le connecteur est un chiffre invente, et la regle du vault est explicite
    la-dessus.
    """
    if val is None:
        return ""
    if isinstance(val, float):
        return repr(val)
    return str(val)


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    """Union des colonnes, dans l'ordre de premiere apparition."""
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    return list(seen)


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
        writer.writerow({c: _fmt_taux(row.get(c, "")) for c in cols})
    return buf.getvalue()


def _entete(titre: str, notes: list[str], origine: str = "") -> list[str]:
    """L'entete commune a toute reponse : titre, origine des donnees, sens du taux.

    `origine` est explicite pour les outils qui n'utilisent pas l'instantane -
    taux_records interroge l'API en direct, et afficher "instantane non encore
    charge" laisserait croire que sa reponse vient d'un cache.
    """
    source = origine or _origine()
    lignes = [titre, f"Source               : {source}", f"Sens du taux         : {SENS_DU_TAUX}"]
    if "REPLI LOCAL" in source:
        lignes.append(
            "ATTENTION : donnees servies depuis le repli local, l'API n'a pas "
            "repondu. Verifie la date de recuperation ci-dessus avant de citer "
            "un taux dans un document ou un mail."
        )
    lignes.extend(f"Note                 : {n}" for n in notes if n)
    return lignes


def _render_table(
    rows: list[dict[str, Any]], titre: str, notes: list[str], origine: str = ""
) -> str:
    lignes = _entete(titre, notes, origine)
    lignes.append(f"{len(rows)} ligne(s).")
    if not rows:
        lignes.append("(aucune ligne)")
        return "\n".join(lignes)
    body = _to_csv_text(rows)
    if len(body) > RENDER_MAX_CHARS:
        gardees: list[str] = []
        taille = 0
        for line in body.splitlines():
            if taille + len(line) > RENDER_MAX_CHARS:
                break
            gardees.append(line)
            taille += len(line) + 1
        body = "\n".join(gardees)
        lignes.append(
            f"Rendu limite a {max(0, len(gardees) - 1)} ligne(s) sur {len(rows)}, "
            "pour ne pas saturer la conversation. Resserre le perimetre "
            "(parametre devises=...) ou demande un export si tu as besoin de "
            "toutes les lignes."
        )
    lignes.append("")
    lignes.append(body)
    return "\n".join(lignes)


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
        except (ConfigError, TauxError) as exc:
            return f"ERREUR : {exc}"

    return wrapper


# --------------------------------------------------------------------------
# Les outils
# --------------------------------------------------------------------------

mcp = FastMCP("taux")


@mcp.tool()
@_guard
def taux_guide() -> str:
    """Les quatre pieges de ce jeu de donnees. A lire AVANT d'ecrire un filtre.

    Repond hors ligne, sans toucher l'API. Trois de ces pieges produisent un
    resultat faux sans lever d'erreur : c'est pour cela qu'ils sont ici et pas
    dans un README que personne ne relit avant un appel.
    """
    return f"""GUIDE - taux de change de chancellerie DGFiP

Jeu de donnees : {_dataset()} sur {_base_url()}
Licence Ouverte v2.0 (Etalab), publication mensuelle, aucune cle d'API.
Releve du 2026-09-01 : 24 103 lignes, 189 devises, 262 couples devise/pays,
659 dates de publication, du 1990-01-01 au 2026-09-01.

PIEGE 1 - un taux n'est republie QUE quand il change.
  Il n'y a pas une ligne par devise et par mois. Au 2026-09-01, 44 devises
  seulement portent une ligne datee de ce jour, alors que 189 ont un taux
  applicable : GBP n'avait pas bouge depuis fevrier, PLN depuis avril.
  Un `where date = '2026-09-01'` rend donc 44 devises sur 189, et l'absence de
  GBP se lit comme "il n'y a pas de taux pour la livre".
  -> N'ecris JAMAIS un filtre d'egalite sur la date. Utilise taux_du_jour ou
     taux_a_la_date : ils retiennent, par devise, la derniere publication
     anterieure ou egale a la date, et rendent cette date d'effet.

PIEGE 2 - une ligne par couple (devise, pays), pas par devise.
  USD apparait plus de dix fois a chaque publication - Etats-Unis, Equateur,
  Salvador, Panama... - avec le meme taux. D'ou 24 103 lignes pour 189 devises.
  -> Compter les lignes ne compte pas des taux. Moyenner une colonne moyenne
     des doublons. Les outils de ce serveur dedoublonnent et rendent nb_pays.

PIEGE 3 - le sens du taux.
  {SENS_DU_TAUX}
  Exemple releve : USD au 2026-09-01 vaut 0.8589, donc 1 USD = 0,8589 EUR, et
  1 EUR vaut environ 1,164 USD.
  -> devise vers euro : montant x taux. Euro vers devise : montant / taux.
     Une inversion ne leve aucune erreur, elle rend un montant plausible.
     Utilise taux_convertir plutot que de multiplier a la main.

PIEGE 4 - monnaievigueur dit "en vigueur AUJOURD'HUI", pas "a la date demandee".
  C'est une propriete de la devise, constante sur toutes ses lignes : 46 des
  189 devises sont a 0, elles n'ont plus cours. Le flag ne raconte donc rien de
  l'etat de la devise a une date passee.
  -> Sur une date ancienne, vigueur_seulement=True ecarte des devises qui
     avaient cours a cette date. Les outils le signalent quand la date demandee
     n'est pas du mois courant.

PIEGE 5 - "historique complet depuis 1990" est trompeur.
  La plage annoncee va du 1990-01-01 a aujourd'hui, mais 83 lignes seulement
  sont anterieures a 1999, sur neuf devises obscures (AOR, BGL, CUI, GWP, KPW,
  KYD, SZL, TJR, TOP). Les grandes devises n'entrent qu'en 2005 : GBP le
  2005-04-16, PLN le 2005-05-01, USD le 2005-06-01, CHF le 2005-08-01.
  Le nombre de devises ayant un taux applicable passe de 12 au 2000-06-30 a
  162 au 2005-06-30, puis 189 aujourd'hui.
  -> L'historique reellement exploitable commence a {PREMIERE_DATE_UTILE}.
     Avant, une reponse vide n'est pas une panne : la donnee n'existe pas, et
     ca ne dit rien du taux qui s'appliquait alors.

CE QUE CE JEU N'EST PAS
  Ce sont les taux de CHANCELLERIE : une reference administrative mensuelle,
  pas un cours de marche, pas un taux de banque, pas le taux de conversion
  d'un reglement. Le taux qui s'applique a une facture transporteur depend du
  contrat - a lire dans 02_TRANSPORTEURS/<NOM>/01_contrat/, pas ici.
  Le jeu ne porte pas non plus les monnaies pre-euro : ni FRF, ni DEM, malgre
  des dates qui remontent a 1990.

PLAFONDS DE L'API
  /records : limit <= {PAGE_LIMIT_MAX}, et offset + limit <= {OFFSET_PLUS_LIMIT_MAX}.
  Ce second plafond rend la pagination par offset incapable de lire les 24 103
  lignes : c'est /exports qui sert la lecture complete, et c'est ce que fait
  taux_export_csv.
"""


@mcp.tool()
@_guard
def taux_devises(contient: str = "", vigueur_seulement: bool = True) -> str:
    """Le referentiel des devises : code ISO, libelle, derniere publication, taux courant.

    A appeler avant tout filtre sur une devise : un code ISO devine rend zero
    ligne, et zero ligne se lit comme "il n'y a pas de taux".

    contient : filtre sur le code ou le libelle, sans accent et sans casse
    (par exemple "zloty", "PL", "franc"). vigueur_seulement : ecarte les 46
    devises qui n'ont plus cours aujourd'hui.
    """
    aujourdhui = _today()
    lignes = _en_vigueur(aujourdhui, None, vigueur_seulement)
    besoin = (contient or "").strip().lower()
    if besoin:
        lignes = [
            r
            for r in lignes
            if besoin in str(r["monnaie_source"]).lower()
            or besoin in str(r["nom_monnaie_source"] or "").lower()
            or besoin in str(r["pays"] or "").lower()
        ]
    rows = [
        {
            "monnaie_source": r["monnaie_source"],
            "nom_monnaie_source": r["nom_monnaie_source"],
            "taux": r["taux"],
            "date_effet": r["date_effet"],
            "monnaievigueur": r["monnaievigueur"],
            "nb_pays": r["nb_pays"],
            "pays": r["pays"],
        }
        for r in lignes
    ]
    notes = [
        "taux et date_effet sont ceux en vigueur AUJOURD'HUI, "
        f"soit la derniere publication anterieure ou egale au {aujourdhui}.",
    ]
    if not vigueur_seulement:
        notes.append(
            "vigueur_seulement=False : les devises qui n'ont plus cours sont "
            "incluses (monnaievigueur=0), avec leur dernier taux publie."
        )
    titre = "Devises du jeu de donnees"
    if besoin:
        titre += f" contenant '{contient}'"
    return _render_table(rows, titre, notes)


@mcp.tool()
@_guard
def taux_du_jour(devises: str = "", vigueur_seulement: bool = True) -> str:
    """Les taux en vigueur AUJOURD'HUI, avec la date d'effet de chacun.

    C'est le raccourci de taux_a_la_date sur la date du jour. La date d'effet
    rendue n'est pas celle d'aujourd'hui : c'est celle de la derniere
    publication du taux, qui peut avoir plusieurs mois.

    devises : codes ISO separes par des virgules ("USD,GBP,PLN"), ou vide pour
    toutes. vigueur_seulement : ecarte les devises qui n'ont plus cours.
    """
    return _rendu_a_la_date(_today(), devises, vigueur_seulement)


@mcp.tool()
@_guard
def taux_a_la_date(date: str, devises: str = "", vigueur_seulement: bool = True) -> str:
    """Les taux EN VIGUEUR a une date : la derniere publication anterieure ou egale.

    C'est l'outil central de ce connecteur, et la seule lecture juste du jeu de
    donnees. Un filtre d'egalite sur la date rendrait 44 devises sur 189, parce
    qu'un taux n'est republie que quand il change (voir taux_guide, piege 1).

    date : AAAA-MM-JJ, AAAA-MM (resolu au 1er du mois), AAAA (au 31 decembre),
    "septembre 2026", ou vide pour aujourd'hui. devises : codes ISO separes par
    des virgules, ou vide pour toutes.
    """
    return _rendu_a_la_date(date, devises, vigueur_seulement)


def _rendu_a_la_date(date: str, devises: str, vigueur_seulement: bool) -> str:
    """Le corps commun de taux_du_jour et taux_a_la_date.

    Un helper plutot qu'un outil qui appelle l'autre : `@mcp.tool()` rend la
    fonction telle quelle aujourd'hui, mais s'appuyer sur ce detail
    d'implementation pour recuperer la fonction non decoree ferait dependre le
    connecteur d'une version du SDK.
    """
    jour, note = _parse_date(date, "date")
    codes = _devises(devises)
    lignes = _en_vigueur(jour, codes, vigueur_seulement)
    rows = [
        {
            "monnaie_source": r["monnaie_source"],
            "nom_monnaie_source": r["nom_monnaie_source"],
            "taux": r["taux"],
            "date_effet": r["date_effet"],
            "monnaievigueur": r["monnaievigueur"],
            "nb_pays": r["nb_pays"],
        }
        for r in lignes
    ]
    notes = [note] if note else []
    notes.append(
        "date_effet = date de mise en vigueur du taux rendu, PAS la date "
        "demandee. Un taux peut dater de plusieurs mois : c'est normal, il n'est "
        "republie que quand il change."
    )
    if codes:
        manquants = sorted(set(codes) - {r["monnaie_source"] for r in lignes})
        if manquants:
            notes.append(
                "AUCUN taux rendu pour : "
                + ", ".join(manquants)
                + ". Soit le code n'existe pas dans le jeu (verifie avec "
                "taux_devises), soit la devise n'avait pas encore de publication "
                "a cette date, soit elle n'a plus cours et vigueur_seulement=True "
                "l'a ecartee. Ne conclus pas a l'absence de taux sans avoir "
                "distingue les trois."
            )
    if jour < PREMIERE_DATE_UTILE:
        notes.append(
            f"DATE ANTERIEURE AU {PREMIERE_DATE_UTILE} : sur cette periode, le jeu "
            "ne couvre presque rien. Les grandes devises n'y entrent qu'au "
            "printemps et a l'ete 2005 - GBP le 2005-04-16, USD le 2005-06-01, "
            "CHF le 2005-08-01. Une reponse vide ou tres courte ici n'est pas une "
            "panne du connecteur : la donnee n'existe pas. Ne conclus pas non plus "
            "que le taux etait different - il n'a simplement pas ete publie dans "
            "ce jeu."
        )
    if jour[:7] != _today()[:7] and vigueur_seulement:
        notes.append(
            "La date demandee n'est pas du mois courant, et monnaievigueur dit "
            "\"en vigueur aujourd'hui\", pas \"a cette date\" : "
            "vigueur_seulement=True peut ecarter des devises qui avaient cours "
            "alors. Relance avec vigueur_seulement=False pour une lecture "
            "historique."
        )
    divergents = [r["monnaie_source"] for r in lignes if r.get("taux_divergents")]
    if divergents:
        notes.append(
            "Taux divergents entre pays sur la meme date pour : "
            + ", ".join(divergents)
            + ". C'est l'arrondi de la parite fixe du franc CFA, pays par pays "
            "(ecart au-dela de la 9e decimale). Le taux rendu est celui du "
            "premier pays lu."
        )
    return _render_table(rows, f"Taux en vigueur au {jour}", notes)


@mcp.tool()
@_guard
def taux_historique(devise: str, depuis: str = "", jusqu_a: str = "") -> str:
    """La suite des taux publies pour une devise, du plus recent au plus ancien.

    Une ligne par publication, donc par changement de taux - pas une ligne par
    mois. C'est ce qu'il faut pour une serie, un graphique, ou pour repondre a
    "de combien le zloty a bouge depuis janvier".

    devise : un code ISO. depuis / jusqu_a : bornes de date optionnelles, memes
    formats que taux_a_la_date.
    """
    code = (devise or "").strip().upper()
    if not code:
        raise TauxError("devise manquante : donne un code ISO, par exemple PLN.")
    borne_bas = _parse_date(depuis, "depuis")[0] if (depuis or "").strip() else ""
    borne_haut = _parse_date(jusqu_a, "jusqu_a")[0] if (jusqu_a or "").strip() else ""

    par_date: dict[str, dict[str, Any]] = {}
    for row in _snapshot():
        if str(row.get("monnaie_source") or "").upper() != code:
            continue
        jour = str(row.get("date") or "")
        if not jour or (borne_bas and jour < borne_bas) or (borne_haut and jour > borne_haut):
            continue
        # Dedoublonnage par date : le jeu porte une ligne par pays.
        cur = par_date.setdefault(
            jour,
            {
                "date_effet": jour,
                "monnaie_source": code,
                "nom_monnaie_source": row.get("nom_monnaie_source"),
                "taux": row.get("taux"),
                "monnaievigueur": row.get("monnaievigueur"),
                "nb_pays": 0,
            },
        )
        cur["nb_pays"] += 1

    if not par_date:
        # On reutilise la resolution de _une_devise pour dire POURQUOI c'est vide.
        _une_devise(borne_haut or _today(), code)
        bas = borne_bas or "le debut"
        haut = borne_haut or "aujourd'hui"
        raise TauxError(f"aucune publication pour {code} entre {bas} et {haut}.")

    rows = [par_date[d] for d in sorted(par_date, reverse=True)]
    taux = [r["taux"] for r in rows if r["taux"] is not None]
    notes = [
        "Une ligne par PUBLICATION, pas par mois : le taux ne change pas tous "
        "les mois. Entre deux lignes, c'est le taux de la ligne la plus ancienne "
        "des deux qui s'applique.",
        f"nb_pays = nombre de lignes du jeu portant ce couple devise/date "
        f"(le jeu duplique par pays).",
    ]
    if taux:
        notes.append(
            f"Sur ce perimetre : min {_fmt_taux(min(taux))}, "
            f"max {_fmt_taux(max(taux))}, dernier {_fmt_taux(rows[0]['taux'])} "
            f"au {rows[0]['date_effet']}."
        )
    titre = (
        f"Historique {code}"
        + (f" du {borne_bas}" if borne_bas else "")
        + (f" au {borne_haut}" if borne_haut else "")
    )
    return _render_table(rows, titre, notes)


@mcp.tool()
@_guard
def taux_convertir(
    montant: float,
    devise: str,
    sens: str = "vers_eur",
    date: str = "",
) -> str:
    """Convertit un montant au taux de chancellerie en vigueur a une date.

    Rend le taux utilise ET sa date d'effet : un montant converti sans son taux
    n'est pas verifiable, et c'est ce montant-la qui part dans un mail.

    montant : la somme a convertir. devise : code ISO. sens : "vers_eur" (le
    montant est en devise, on rend des euros) ou "depuis_eur" (le montant est en
    euros, on rend la devise). date : vide pour aujourd'hui, sinon memes formats
    que taux_a_la_date.
    """
    jour, note = _parse_date(date, "date")
    sens_norm = (sens or "vers_eur").strip().lower().replace("-", "_")
    if sens_norm in {"vers_eur", "eur", "en_eur", "devise_vers_eur"}:
        vers_eur = True
    elif sens_norm in {"depuis_eur", "eur_vers_devise", "en_devise", "vers_devise"}:
        vers_eur = False
    else:
        raise TauxError(
            f"sens non compris : {sens}. Valeurs acceptees : \"vers_eur\" (le "
            "montant est en devise) ou \"depuis_eur\" (le montant est en euros)."
        )

    entry = _une_devise(jour, devise)
    taux = entry.get("taux")
    if taux in (None, 0):
        raise TauxError(
            f"le taux publie pour {entry['monnaie_source']} au "
            f"{entry['date_effet']} est vide ou nul : aucune conversion possible."
        )

    # Decimal et pas float pour le montant rendu : une multiplication en float
    # ajoute des decimales qui n'existent pas, et un montant est cite tel quel
    # dans un controle de facture.
    d_montant = decimal.Decimal(str(montant))
    d_taux = decimal.Decimal(repr(taux))
    brut = d_montant * d_taux if vers_eur else d_montant / d_taux
    arrondi = brut.quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)

    code = entry["monnaie_source"]
    src, dst = (code, "EUR") if vers_eur else ("EUR", code)
    formule = f"{montant} {src} x {_fmt_taux(taux)}" if vers_eur else f"{montant} {src} / {_fmt_taux(taux)}"

    lignes = _entete(f"Conversion {src} -> {dst} au taux en vigueur du {jour}", [note])
    lignes += [
        "",
        f"Resultat             : {arrondi} {dst}",
        f"Resultat non arrondi : {brut.normalize()} {dst}",
        f"Calcul               : {formule}",
        f"Taux utilise         : {_fmt_taux(taux)} ({code} -> EUR)",
        f"Date d'effet du taux : {entry['date_effet']}",
        f"Devise               : {code} - {entry['nom_monnaie_source']}",
        "",
        "L'arrondi ci-dessus est a 2 decimales, au plus proche. Si le controle "
        "de facture ou l'ecriture comptable impose une autre regle d'arrondi, "
        "pars du resultat non arrondi.",
        "Taux de CHANCELLERIE : reference administrative mensuelle, pas un cours "
        "de marche. Le taux qui s'applique a un reglement transporteur depend du "
        "contrat - a lire dans 02_TRANSPORTEURS/<NOM>/01_contrat/.",
    ]
    if entry.get("monnaievigueur") != 1:
        lignes.append(
            f"ATTENTION : {code} est marquee comme n'ayant plus cours "
            "(monnaievigueur=0). Le taux rendu est son dernier taux publie."
        )
    if entry["date_effet"][:7] != jour[:7]:
        lignes.append(
            f"Le taux date du {entry['date_effet']}, soit un autre mois que la "
            "date demandee : normal, il n'est republie que quand il change."
        )
    return "\n".join(lignes)


@mcp.tool()
@_guard
def taux_records(
    where: str = "",
    select: str = "",
    group_by: str = "",
    order_by: str = "",
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Requete ODSQL brute sur le jeu, pour ce que les autres outils ne couvrent pas.

    Passe-plat sur /records de l'API Explore v2.1, en GET. Sert a agreger cote
    serveur (count, min, max, avg) ou a poser un filtre exotique.

    N'ECRIS PAS UN FILTRE D'EGALITE SUR LA DATE ici : `where=date="2026-09-01"`
    rend 44 devises sur 189. Pour une question "en vigueur a", c'est
    taux_a_la_date, pas cet outil.

    Champs : monnaie_source, nom_monnaie_source, pays_principal, code_pays,
    date, taux, monnaievigueur. Syntaxe ODSQL, par exemple
    where=monnaie_source="USD" AND date>=date'2020-01-01',
    select=count(*) as n, group_by=monnaie_source.

    limit est plafonne a 100 par l'API, et offset + limit a 10 000 : au-dela,
    passe par taux_export_csv, qui n'a pas ce plafond.
    """
    if limit > PAGE_LIMIT_MAX:
        raise TauxError(
            f"limit={limit} refuse par l'API : le maximum est {PAGE_LIMIT_MAX}. "
            "Pour plus de lignes, agrege avec select/group_by, ou demande un "
            "export (taux_export_csv), qui n'a pas de plafond."
        )
    if limit < 1:
        raise TauxError("limit doit valoir au moins 1.")
    if offset + limit > OFFSET_PLUS_LIMIT_MAX:
        raise TauxError(
            f"offset + limit = {offset + limit} refuse par l'API : le maximum "
            f"est {OFFSET_PLUS_LIMIT_MAX}. C'est ce plafond qui rend la "
            "pagination par offset incapable de lire les 24 103 lignes du jeu. "
            "Passe par taux_export_csv pour une lecture complete."
        )

    query: dict[str, Any] = {"limit": limit, "offset": offset}
    for nom, val in (("where", where), ("select", select), ("group_by", group_by), ("order_by", order_by)):
        if (val or "").strip():
            query[nom] = val.strip()
    payload = _json("/records", query)
    resultats = payload.get("results") or []
    total = payload.get("total_count")

    notes = [
        "Requete : GET /records " + json.dumps(query, ensure_ascii=False),
    ]
    if group_by.strip():
        notes.append(
            "group_by est actif : total_count compte les GROUPES, pas les lignes "
            f"du jeu. total_count = {total}."
        )
    else:
        notes.append(
            f"total_count = {total} lignes correspondant au filtre. Attention : "
            "le jeu porte une ligne par couple (devise, pays), donc ce total "
            "n'est pas un nombre de devises ni un nombre de taux."
        )
    if total is not None and isinstance(total, int) and total > offset + len(resultats):
        notes.append(
            f"Rendu {len(resultats)} ligne(s) sur {total} : resultat TRONQUE par "
            "limit/offset. Ne cite pas ce compte comme un volume."
        )
    return _render_table(
        [_flat_record(r) for r in resultats],
        "Requete ODSQL sur " + _dataset(),
        notes,
        origine=f"API {_dataset_url('/records')} (appel direct, sans cache)",
    )


def _flat_record(row: dict[str, Any]) -> dict[str, Any]:
    """Aplatit une ligne de /records, agregation comprise.

    Une agregation rend des colonnes libres (n, derniere, ...) : on ne peut pas
    projeter sur CHAMPS. On tronque juste les horodatages de date, pour que
    `max(date)` et `date` s'affichent dans le meme format.
    """
    out: dict[str, Any] = {}
    for key, val in row.items():
        if isinstance(val, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", val):
            val = val[:10]
        elif isinstance(val, (dict, list)):
            val = json.dumps(val, ensure_ascii=False, default=str)
        out[key] = val
    return out


@mcp.tool()
@_guard
def taux_export_csv(
    mode: str = "en_vigueur",
    date: str = "",
    devises: str = "",
    where: str = "",
    select: str = "",
    order_by: str = "",
    filename: str = "",
) -> str:
    """Ecrit le perimetre demande en CSV point-virgule, ouvrable dans Excel FR.

    ECRIT UN FICHIER SUR LE DISQUE. A n'appeler que si l'utilisateur a demande
    un fichier, un export, un CSV ou un classeur. Sinon, reponds dans la session
    avec les autres outils.

    Deux modes, et le choix n'est pas neutre :

    - mode="en_vigueur" (defaut) : la table des taux EN VIGUEUR a `date`, une
      ligne par devise, avec la date d'effet. C'est ce qu'on veut pour un
      controle ou un tableau de conversion.
    - mode="brut" : les lignes du jeu telles quelles, filtrees par `where`.
      Une ligne par couple (devise, pays) et par publication. C'est ce qu'on
      veut pour rejouer une serie ou alimenter un modele.

    Aucun plafond de pagination : l'export passe par /exports, pas par /records.
    Le fichier va dans ~/.taux-de-change-mcp/exports, ou dans TAUX_EXPORT_DIR.
    Jamais dans la bibliotheque d'equipe.
    """
    mode_norm = (mode or "en_vigueur").strip().lower()
    entete_note: list[str] = []
    origine_export = ""

    if mode_norm in {"en_vigueur", "vigueur", "asof"}:
        jour, note = _parse_date(date, "date")
        if note:
            entete_note.append(note)
        lignes = _en_vigueur(jour, _devises(devises), vigueur_seulement=False)
        rows = [
            {
                "monnaie_source": r["monnaie_source"],
                "nom_monnaie_source": r["nom_monnaie_source"],
                "taux": r["taux"],
                "date_effet": r["date_effet"],
                "monnaievigueur": r["monnaievigueur"],
                "nb_pays": r["nb_pays"],
                "pays": r["pays"],
                "date_demandee": jour,
            }
            for r in lignes
        ]
        texte = _to_csv_text(rows)
        nb_lignes, nb_cols = len(rows), len(_columns(rows)) if rows else 0
        defaut = f"taux_en_vigueur_{jour}.csv"
        detail = f"mode=en_vigueur, date={jour}, devises={devises or 'toutes'}"
        entete_note.append(
            "Une ligne par devise, taux en vigueur a la date demandee. "
            "monnaievigueur=0 signale une devise qui n'a plus cours : elle est "
            "PRESENTE dans l'export, a toi de la filtrer si tu ne la veux pas."
        )
    elif mode_norm in {"brut", "raw"}:
        query: dict[str, Any] = {"limit": -1, "delimiter": ";", "with_bom": "true"}
        for nom, val in (("where", where), ("select", select), ("order_by", order_by)):
            if (val or "").strip():
                query[nom] = val.strip()
        if (devises or "").strip() and not (where or "").strip():
            codes = _devises(devises)
            liste = ",".join(f'"{c}"' for c in codes)
            query["where"] = f"monnaie_source IN ({liste})"
        resp = _get("/exports/csv", query)
        origine_export = f"API {_dataset_url('/exports/csv')} (appel direct, sans cache)"
        # On renormalise les fins de ligne : selon le poste et le proxy, la
        # meme requete peut rendre du CRLF ou du LF, et la convention d'equipe
        # demande un fichier identique partout.
        # Le BOM demande a l'API est retire ici puis remis a l'ecriture par
        # utf-8-sig : sinon le fichier en porte deux, et Excel affiche le
        # second dans la premiere cellule de l'entete.
        texte = "\n".join(resp.text.lstrip("\ufeff").splitlines()) + "\n"
        premieres = texte.splitlines()
        nb_lignes = max(0, len(premieres) - 1)
        nb_cols = len(premieres[0].split(";")) if premieres else 0
        defaut = f"taux_brut_{dt.date.today().isoformat()}.csv"
        detail = "mode=brut, " + json.dumps(
            {k: v for k, v in query.items() if k not in {"limit", "delimiter", "with_bom"}},
            ensure_ascii=False,
        )
        entete_note.append(
            "Lignes brutes du jeu : une par couple (devise, pays) et par "
            "publication. Ne compte pas les lignes pour compter des devises, et "
            "n'y cherche pas un taux par mois - il n'y en a pas."
        )
    else:
        raise TauxError(
            f"mode non compris : {mode}. Valeurs acceptees : \"en_vigueur\" ou "
            "\"brut\"."
        )

    cible_dir = _export_dir()
    try:
        cible_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TauxError(f"Dossier d'export inutilisable ({cible_dir}) : {exc}") from exc

    nom = (filename or "").strip() or defaut
    if not nom.lower().endswith(".csv"):
        nom += ".csv"
    nom = re.sub(r"[^A-Za-z0-9._-]+", "_", nom)
    cible = cible_dir / nom

    try:
        # newline="" et utf-8-sig : sans newline="", l'ecriture traduit les fins
        # de ligne en CRLF sous Windows et les laisse en LF sous macOS, et le
        # meme code produirait deux fichiers differents selon le poste. Le BOM
        # est ce qui fait qu'Excel FR ouvre le fichier sans assistant d'import.
        with cible.open("w", encoding="utf-8-sig", newline="") as handle:
            handle.write(texte)
    except OSError as exc:
        raise TauxError(f"Ecriture impossible dans {cible} : {exc}") from exc

    lignes_out = _entete(f"Export termine : {cible}", entete_note, origine_export)
    lignes_out += [
        f"{nb_lignes} ligne(s), {nb_cols} colonne(s).",
        f"Perimetre            : {detail}",
        "Format               : CSV point-virgule, UTF-8 avec BOM, fins de ligne LF.",
        "Troncature           : aucune. L'export passe par /exports, qui n'a pas "
        f"le plafond offset + limit <= {OFFSET_PLUS_LIMIT_MAX} de /records.",
    ]
    if nb_lignes == 0:
        lignes_out.append(
            "ATTENTION : le fichier est vide. Ne le livre pas : le filtre ne "
            "correspond a rien. Verifie le code devise avec taux_devises, et "
            "relis taux_guide - un filtre d'egalite sur la date rend presque "
            "toujours moins que ce qu'on croit."
        )
    return "\n".join(lignes_out)


@mcp.tool()
@_guard
def taux_doctor() -> str:
    """Diagnostic : interpreteur, chemins, origine de chaque reglage, connexion.

    A appeler quand une reponse surprend ou qu'un outil echoue. Affiche
    l'interpreteur utilise, la racine locale, le dossier d'export et l'origine
    de chaque reglage - c'est la que se voit un ecart de plateforme avant qu'il
    devienne un incident.
    """
    _load_env_file()
    env_local = _ENV_LOADED_FROM or (
        "aucun, et ce n'est pas un probleme : ce connecteur n'a pas de secret"
    )
    lignes = [
        "DIAGNOSTIC - MCP taux-de-change",
        "",
        f"Interpreteur         : {sys.executable}",
        f"Version Python       : {sys.version.split()[0]}",
        f"Plateforme           : {sys.platform} (os.name={os.name})",
        f"Racine locale        : {_local_root()}",
        f"Dossier d'export     : {_export_dir()}  (origine : {_origine_reglage('TAUX_EXPORT_DIR')})",
        f"Instantane de repli  : {_snapshot_path()}"
        + (" [present]" if _snapshot_path().is_file() else " [absent]"),
        f"Fichier .env local   : {env_local}",
        "",
        f"Racine API           : {_base_url()}  (origine : {_origine_reglage('TAUX_BASE_URL')})",
        f"Jeu de donnees       : {_dataset()}  (origine : {_origine_reglage('TAUX_DATASET')})",
        f"Delai d'appel        : {_timeout()} s  (origine : {_origine_reglage('TAUX_TIMEOUT_S')})",
        f"Duree du cache       : {_cache_ttl()} s  (origine : {_origine_reglage('TAUX_CACHE_TTL_S')})",
        "Authentification     : aucune. Jeu de donnees public, Licence Ouverte "
        "Etalab v2.0. Ce connecteur ne porte aucun secret.",
        "",
    ]

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
    if _SHARED_REJECTED:
        lignes.append("")
        lignes.append(
            "  ATTENTION : le fichier d'equipe porte des cles que ce serveur ne "
            "connait pas, elles ont ete IGNOREES : "
            + ", ".join(sorted(set(_SHARED_REJECTED)))
            + ". Dans la quasi-totalite des cas c'est une faute de frappe : "
            "compare avec .env.example. Une cle ignoree ne produit aucune erreur "
            "ailleurs - c'est ici, et seulement ici, que ca se voit."
        )
    lignes.append("")

    try:
        payload = _json("/records", {"limit": 1, "order_by": "date desc"})
        total = payload.get("total_count")
        derniere = ""
        for row in payload.get("results") or []:
            derniere = str(row.get("date") or "")[:10]
        lignes.append(
            f"Connexion API        : OK | {total} ligne(s) dans le jeu, "
            f"derniere date de publication {derniere or 'inconnue'}"
        )
    except TauxError as exc:
        lignes.append(f"Connexion API        : ECHEC | {exc}")
        if _snapshot_path().is_file():
            lignes.append(
                "  Un instantane local est present : les outils repondront "
                "depuis le repli, en le disant."
            )
        else:
            lignes.append(
                "  Aucun instantane local : les outils ne pourront rien rendre. "
                "Ce connecteur ne rend pas un taux de memoire."
            )

    try:
        rows = _snapshot()
        devises_uniques = {str(r.get("monnaie_source") or "") for r in rows}
        dates = [str(r.get("date") or "") for r in rows if r.get("date")]
        vivantes = {
            str(r.get("monnaie_source") or "")
            for r in rows
            if r.get("monnaievigueur") == 1
        }
        lignes += [
            "",
            f"Instantane charge    : {len(rows)} lignes, {len(devises_uniques)} devises "
            f"(dont {len(vivantes)} en vigueur aujourd'hui)",
            f"Plage de dates       : {min(dates)} -> {max(dates)}",
            f"Origine des donnees  : {_origine()}",
        ]
    except TauxError as exc:
        lignes.append(f"Instantane           : indisponible | {exc}")

    return "\n".join(lignes)


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if arg == "doctor":
        report = taux_doctor()
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
