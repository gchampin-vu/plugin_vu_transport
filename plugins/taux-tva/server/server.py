#!/usr/bin/env python3
"""
MCP tva : les taux de TVA des 27 Etats membres, en LECTURE SEULE.

Trois usages :
    python server.py            # mode serveur MCP (stdio) - lance par Claude Code
    python server.py doctor     # diagnostic de configuration et de connexion
    python server.py --help

Ce connecteur remplace le script Power Query qui appelait api.vatcomply.com et
n'en gardait que trois colonnes (country_code, standard_rate, currency). Il ne
passe plus par cet intermediaire : il interroge TEDB, la base officielle de la
Commission europeenne, et un jeu de donnees public pour les juridictions hors
Union. Ce qu'on y gagne, ce sont les categories de biens, les CODES DE
NOMENCLATURE DOUANIERE qui relient une categorie a un produit reel, les
commentaires officiels et la date d'effet de chaque taux. C'est
cette derniere colonne qui repond aux questions qu'on se pose reellement -
« quel taux sur le transport de personnes en Italie », « pourquoi 5,5 % en
France » - et le script d'origine la jetait.

CE QUE CE CONNECTEUR N'EST PAS. Ce n'est pas une reference fiscale. VAT Comply
est une source publique, gratuite, sans engagement de mise a jour. Pour une
facture, une declaration ou un parametrage d'ERP, la reference est
l'administration fiscale du pays. Ce connecteur sert a COMPARER 27 pays d'un
coup, et a REPERER qu'un taux a bouge. Chaque rendu porte la date de releve et
le rappel : c'est volontaire, un taux cite sans sa date est un taux faux en
puissance.

CE QUE LA SOURCE NE COUVRE PAS, et c'est le piege principal :
  - les 27 Etats membres, et EUX SEULS. Pas la Suisse, pas la Norvege, pas le
    Royaume-Uni - trois pays ou Vente-unique vend. Une question sur CH ou NO
    n'a pas de reponse ici, et le serveur le dit au lieu de rendre une ligne
    vide qu'on lirait comme un zero.
  - la Grece porte le code EL, pas GR. C'est la nomenclature TVA de l'Union,
    pas l'ISO 3166. `pays="GR"` est traduit, mais un rapprochement fait a la
    main sur un fichier ISO perdrait la Grece en silence.
  - les regimes territoriaux : Canaries, Madere, Acores, Corse, DOM, Aland,
    Livigno. La source rend UN taux par pays ; le taux national ne s'y applique
    pas. Le referentiel local (pays.json) porte ces cas et le serveur previent.

LECTURE SEULE par construction : la source n'expose que des GET, et ce serveur
n'appelle qu'un seul chemin, /vat_rates. Rien n'est envoye a l'extremite hormis
cette requete sans parametre - aucun identifiant, aucun numero de TVA, aucune
donnee de Vente-unique.

CONFIGURATION, EN DEUX COUCHES. Ce qui est identique sur tous les postes -
racine d'API, duree de fraicheur du cache, delai, et le perimetre des pays ou
l'equipe travaille - vit dans 08_ENGINE/04_mcp/00_config/tva.shared.env. Ce qui
est propre a un poste vit dans tva.env, hors du vault. Ordre de priorite :
configuration du plugin > tva.env du poste > fichier d'equipe > defaut du
serveur. Le poste passe devant l'equipe. Voir README.md.

Aucune cle d'API : la source est publique et anonyme. Ce connecteur est donc le
seul de l'equipe qui n'a rien a mettre en service.
"""

from __future__ import annotations

import csv
import datetime as dt
import functools
import io
import json
import os
import pathlib
import re
import sys
import time
import unicodedata
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import sources

HERE = pathlib.Path(__file__).resolve().parent
PAYS_FILE = HERE / "pays.json"

# Les deux sources vivent dans sources.py : c'est le seul module qui connait
# le SOAP de TEDB et la forme du jeu vatnode. Ici on ne manipule que la forme
# de ligne normalisee.
DEFAULT_BASE_URL = sources.TEDB_ENDPOINT
RATES_PATH = ""

DEFAULT_TIMEOUT_S = 30.0

# Duree pendant laquelle le cache local repond sans rappeler la source. Un taux
# de TVA change par une loi de finances, pas dans la journee : 24 h est large
# et evite de marteler une API publique gratuite. tva_rafraichir force le
# rappel quand on veut la valeur de l'instant.
DEFAULT_TTL_HOURS = 24.0

# Au-dela, un cache n'est plus « un peu vieux », il est PERIME : le rendu le dit
# en tete, en majuscules. Un taux de six semaines cite comme courant est
# exactement le chiffre faux qui part dans un mail.
STALE_DAYS = 30.0

# On rejoue une panne passagere, pas un 429. Un 429 sur une API publique veut
# dire « ralentis » : insister est impoli et inutile, le cache repond a la
# place.
RETRY_ATTEMPTS = 3
RETRY_STATUSES = (500, 502, 503, 504)
RETRY_BACKOFF_S = 1.5

RENDER_MAX_CHARS = 24_000

# Dossiers synchronises : rien de local n'y vit. Il n'y a pas de secret ici,
# mais un cache pose sur le drive partage partirait chez vingt-six personnes et
# vieillirait pour tout le monde a la fois.
SYNCED_MARKERS = ("/onedrive", "cafom", "sharepoint", "dropbox", "google drive")

# Les colonnes du rendu court, dans l'ordre. `reduced_rates` est aplati en
# texte : c'est une liste, et une liste dans une cellule CSV ne se lit pas.
BRIEF_COLUMNS = (
    "code",
    "pays",
    "taux_normal",
    "taux_reduits",
    "taux_super_reduit",
    "taux_parking",
    "devise",
    "provenance",
)

# La provenance est une COLONNE, pas une note de bas de page. Les deux moities
# du perimetre n'ont pas la meme autorite : les 27 et XI viennent de la base
# officielle de la Commission, les 17 autres d'un jeu tenu a la main. Melanger
# les deux dans un tableau sans le dire, c'est laisser citer un taux suisse
# avec la confiance qu'on accorde a un taux francais.
LEGAL_NOTE = (
    "Sources : TEDB (Commission europeenne, officielle) pour les 27 Etats "
    "membres et XI ; vatnode (licence MIT, tenue a la main) pour les 17 "
    "juridictions hors Union et pour les devises. Pour une facture, une "
    "declaration ou un parametrage d'ERP, la reference reste l'administration "
    "fiscale du pays - ce connecteur sert a comparer des juridictions et a "
    "reperer un changement, pas a etablir un taux."
)


class ConfigError(RuntimeError):
    pass


class TvaError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Le referentiel local : ce que l'API ne dit pas
# --------------------------------------------------------------------------

_REF: dict[str, Any] | None = None


def _ref() -> dict[str, Any]:
    """Charge pays.json. Le nom du fichier est ecrit au caractere pres.

    La casse compte sous macOS et pas sous Windows : un `Pays.json` marcherait
    sur le poste de celui qui developpe et casserait chez le collegue sur Mac.
    """
    global _REF
    if _REF is None:
        try:
            _REF = json.loads(PAYS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"referentiel des pays illisible ({PAYS_FILE}) : {exc}. "
                "Installation du plugin incomplete."
            ) from exc
    return _REF


def _norm(text: Any) -> str:
    """Majuscules, sans accent, underscores. « Grece », « GRÈCE », « grece » -> GRECE."""
    flat = unicodedata.normalize("NFKD", str(text or ""))
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", "_", flat.upper()).strip("_")


# --------------------------------------------------------------------------
# Configuration : le poste, puis l'equipe, puis le defaut
# --------------------------------------------------------------------------

_ENV_LOADED_FROM: str = ""
_ENV_LOAD_ERROR: str = ""
_ENV_DONE: bool = False


def _is_synced(path: pathlib.Path) -> bool:
    flat = str(path).replace("\\", "/").lower()
    return any(marker in flat for marker in SYNCED_MARKERS)


def _packaged_python() -> str:
    """Detecte un interpreteur Windows empaquete. Rend la raison, ou "".

    Un Python livre par le Microsoft Store ou par le Python Manager donne a ses
    processus enfants une vue VIRTUALISEE de %LOCALAPPDATA% : le plugin ecrit
    d'un cote, le terminal lit de l'autre, sans erreur. D'ou la racine locale
    de ce connecteur, sous le profil utilisateur, qui n'est pas virtualise. On
    garde la detection pour pouvoir NOMMER le symptome dans le diagnostic.
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
    return pathlib.Path.home() / ".tva-mcp"


def _candidate_env_files() -> list[pathlib.Path]:
    """Emplacements du fichier de configuration du poste, dans l'ordre.

    Le fichier s'appelle `tva.env`, pas `.env` : un fichier nomme `.env` a deja
    ete ignore en silence chez Yooz par un serveur qui cherchait `yooz.env`. On
    nomme le fichier d'apres le connecteur, partout.
    """
    out: list[pathlib.Path] = []
    explicit = (os.environ.get("TVA_ENV_FILE") or "").strip()
    if explicit:
        out.append(pathlib.Path(explicit))
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        out.append(pathlib.Path(plugin_data) / "tva.env")
    out.append(_local_root() / "tva.env")
    out.append(HERE / "tva.env")
    return out


def _parse_env(text: str) -> dict[str, str]:
    """Parseur .env minimal : KEY=VALUE, # en commentaire, guillemets optionnels."""
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

    Le « non vide » n'est pas un detail : en mode plugin la configuration arrive
    par l'environnement, et un champ userConfig laisse vide pose une variable
    VIDE. Un setdefault la prendrait pour un reglage et ignorerait le fichier
    du poste en silence.

    Lu en utf-8-sig : un BOM non consomme colle trois octets invisibles devant
    le premier nom de variable, qui devient introuvable.
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
                f"synchronise ({path}) et n'a PAS ete lu. Deplace-le vers "
                f"{_local_root() / 'tva.env'}, ou pointe un emplacement local "
                "avec TVA_ENV_FILE."
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
        _ENV_LOADED_FROM = str(path)
        return


# --- Le fichier d'equipe de 08_ENGINE ------------------------------------
#
# Il n'y a aucun secret a partager ici : la source est publique. Ce qui se
# partage, c'est une DECISION - le perimetre de pays sur lequel l'equipe
# travaille, et la duree de fraicheur qu'on accepte. Le faire ressaisir
# vingt-six fois, ce sont vingt-six occasions de diverger.
#
# La liste blanche n'est pas une barriere de securite, c'est un garde-fou
# contre la faute de frappe : une variable mal orthographiee dans le fichier
# partage serait sinon ignoree en silence sur les vingt-six postes a la fois.

SHARED_FILE_NAME = "tva.shared.env"
ENGINE_DIR_NAME = "08_ENGINE"
SHARED_SUBPATH = ("04_mcp", "00_config")

SHARED_ALLOWED = frozenset(
    {
        "TVA_BASE_URL",
        "TVA_VATNODE_URL",
        "TVA_TIMEOUT_S",
        "TVA_TTL_HOURS",
        "TVA_PAYS_VU",
    }
)

# Locales par nature : un chemin valable sur un poste n'existe pas sur les
# autres. Posees dans le fichier d'equipe, elles sont vues et signalees.
SHARED_LOCAL_ONLY = frozenset({"TVA_EXPORT_DIR", "TVA_CACHE_FILE", "TVA_ENV_FILE"})

_SHARED_CACHE: dict[str, str] | None = None
_SHARED_LOADED_FROM: str = ""
_SHARED_REJECTED: list[str] = []
_SHARED_LOCAL_SEEN: list[str] = []


def _engine_roots() -> list[pathlib.Path]:
    """Racines `08_ENGINE` plausibles, dans l'ordre de preference.

    Le plugin est installe depuis `08_ENGINE/03_plugins/...`, mais Claude Code
    en fait une copie sous `~/.claude/plugins/`. Remonter depuis le code ne
    suffit donc pas : on remonte quand meme - cas de l'installation directe -
    et on complete par le profil utilisateur, ou OneDrive pose la bibliotheque.
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
    """Emplacements du fichier d'equipe. Un chemin explicite est EXCLUSIF.

    Exclusif, et pas seulement prioritaire : sinon pointer un fichier precis ne
    desactive pas le fichier d'equipe reel, il le met en second. C'est ce qui
    faisait tourner la suite de controles hors-ligne des autres connecteurs
    avec la vraie configuration, sur la seule machine ou elle est lancee.
    """
    explicit = (os.environ.get("TVA_SHARED_ENV") or "").strip()
    if explicit:
        return [pathlib.Path(explicit)]
    return [root.joinpath(*SHARED_SUBPATH, SHARED_FILE_NAME) for root in _engine_roots()]


def _load_shared_env() -> dict[str, str]:
    """Lit le premier fichier d'equipe trouve, filtre par la liste blanche.

    Ne leve jamais : un fichier absent, illisible ou mal rempli ne doit pas
    empecher le connecteur de tourner sur la configuration du poste. Ce qui a
    ete refuse est garde de cote pour que le diagnostic le dise.
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
            if upper in SHARED_LOCAL_ONLY:
                local_seen.append(upper)
            elif upper in SHARED_ALLOWED:
                if val.strip():
                    values[upper] = val.strip()
            else:
                rejected.append(upper)
        _SHARED_LOADED_FROM = str(path)
        break
    _SHARED_REJECTED = rejected
    _SHARED_LOCAL_SEEN = local_seen
    _SHARED_CACHE = values
    return values


def _shared_report() -> list[str]:
    """Les lignes que le diagnostic affiche sur le fichier d'equipe."""
    shared = _load_shared_env()
    lines: list[str] = []
    if _SHARED_LOADED_FROM:
        lines.append(f"Config d'equipe          : {_SHARED_LOADED_FROM}")
        lines.append(
            "  reglages repris        : "
            + (", ".join(sorted(shared)) if shared else "(aucun)")
        )
    else:
        lines.append("Config d'equipe          : aucune (defauts du serveur)")
        lines.append(
            "  Ce n'est pas un probleme ici : ce connecteur n'a ni cle ni "
            "racine a recevoir pour fonctionner. Seul le perimetre de pays "
            "TVA_PAYS_VU serait a reprendre."
        )
        for path in _shared_env_candidates():
            lines.append(f"    absent  {path}")
    if _SHARED_LOCAL_SEEN:
        lines.append("")
        lines.append(
            "  ATTENTION : le fichier d'equipe porte des cles LOCALES par "
            "nature, ignorees : " + ", ".join(sorted(set(_SHARED_LOCAL_SEEN)))
            + ". Un chemin valable sur un poste n'existe pas sur les autres."
        )
    if _SHARED_REJECTED:
        lines.append("")
        lines.append(
            "  ATTENTION : le fichier d'equipe porte des cles inconnues de ce "
            "serveur, IGNOREES : " + ", ".join(sorted(set(_SHARED_REJECTED)))
            + ". Dans la quasi-totalite des cas c'est une faute de frappe : "
            "compare avec tva.shared.env.example. Une cle ignoree ne produit "
            "aucune erreur ailleurs - c'est ici, et seulement ici, que ca se voit."
        )
    return lines


def _env(name: str, default: str = "") -> str:
    """La valeur retenue, dans un ordre de priorite fixe.

    1. l'environnement du processus - configuration du plugin, et le tva.env du
       poste que _load_env_file y a deja verse ;
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


def _base_url() -> str:
    """L'endpoint SOAP de TEDB.

    On ne coupe PAS le slash final ici, contrairement a l'usage : ce n'est pas
    une racine a laquelle on ajoute un chemin, c'est l'adresse a laquelle on
    POSTe. Le service la sert avec son slash, et le retirer ajoute une
    redirection a chaque appel - au mieux.
    """
    brut = (_env("TVA_BASE_URL") or DEFAULT_BASE_URL).strip()
    return brut if brut.endswith("/") else brut + "/"


def _vatnode_url() -> str:
    """L'URL du jeu de donnees tenu a la main, si elle est forcee.

    Rend une chaine VIDE quand rien n'est configure, et c'est volontaire : le
    module sources essaie alors le CDN puis le depot brut. Rendre l'URL par
    defaut ici supprimerait ce repli sans que personne ne s'en apercoive.
    """
    return _env("TVA_VATNODE_URL").strip()


def _timeout() -> float:
    try:
        return float(_env("TVA_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _ttl_hours() -> float:
    try:
        return max(0.0, float(_env("TVA_TTL_HOURS") or DEFAULT_TTL_HOURS))
    except ValueError:
        return DEFAULT_TTL_HOURS


def _cache_file() -> pathlib.Path:
    raw = _env("TVA_CACHE_FILE")
    return pathlib.Path(raw) if raw else _local_root() / "vat_rates.json"


def _previous_file() -> pathlib.Path:
    """Le releve precedent, garde pour pouvoir DIRE ce qui a change.

    C'est tout l'interet d'un cache ici : sans lui, « le taux a-t-il bouge » n'a
    pas de reponse - on ne compare pas un taux a un souvenir.
    """
    current = _cache_file()
    return current.with_name(current.stem + ".precedent" + current.suffix)


def _export_dir() -> pathlib.Path:
    """Ou atterrissent les CSV. Jamais dans la bibliotheque d'equipe."""
    raw = _env("TVA_EXPORT_DIR")
    if raw:
        return pathlib.Path(raw)
    return _local_root() / "exports"


def _perimetre_equipe() -> list[str]:
    """Les pays que l'equipe suit, si elle l'a decide. Vide sinon.

    Volontairement VIDE par defaut. Le perimetre de vente de Vente-unique ne se
    devine pas depuis une liste de boutiques : etre present sur un marche et y
    etre immatricule a la TVA sont deux choses differentes, et inventer cette
    liste ici serait un fait faux ecrit dans du code. Elle se pose dans le
    fichier d'equipe, par quelqu'un qui la connait.
    """
    raw = _env("TVA_PAYS_VU")
    if not raw:
        return []
    out: list[str] = []
    for part in re.split(r"[,;\s]+", raw):
        code = _norm(part)
        if code and code not in out:
            out.append(code)
    return out


# --------------------------------------------------------------------------
# L'appel a la source, et le cache
# --------------------------------------------------------------------------

def _fetch_live() -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Les deux sources, en lecture. Rien de Vente-unique ne sort d'ici.

    Rend (lignes, compte-rendu des sources, incidents). Les noms de pays sont
    poses ici depuis le referentiel local : TEDB rend un code, pas un nom
    francais, et « Grece » doit s'afficher pour EL.
    """
    try:
        blob = sources.fetch_tout(
            _timeout(), tedb_endpoint=_base_url(), vatnode_url=_vatnode_url()
        )
    except sources.SourceError as exc:
        raise TvaError(str(exc)) from exc

    noms = _ref().get("codes", {})
    membres = {_norm(c) for c in _ref().get("etats_membres", [])}
    for ligne in blob["rows"]:
        code = _norm(ligne.get("country_code"))
        nom = noms.get(code)
        if nom:
            ligne["country_name"] = nom
        # `member_state` sert a trier et a repondre a « UE ». On le recale sur
        # le referentiel : XI est servie par TEDB mais n'est PAS un Etat
        # membre, et la confondre avec un membre fausserait la reponse a
        # « les 27 ».
        ligne["member_state"] = code in membres
    return blob["rows"], blob.get("sources", {}), blob.get("incidents", [])


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Normalise la reponse en liste d'enregistrements.

    La source rend une liste. On tolere l'enveloppe {"...": [...]} au cas ou
    elle en poserait une, plutot que de casser sur un changement cosmetique.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("rates", "vat_rates", "data", "results"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
        # Forme {"FR": {...}, "DE": {...}} : on la remet a plat.
        out: list[dict[str, Any]] = []
        for code, value in payload.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("country_code", code)
                out.append(row)
        return out
    return []


def _read_cache(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or not _rows(blob.get("rows")):
        return None
    return blob


def _write_cache(
    rows: list[dict[str, Any]],
    compte_rendu: dict[str, Any] | None = None,
    incidents: list[str] | None = None,
) -> dict[str, Any]:
    """Ecrit le releve du jour, apres avoir mis l'ancien de cote s'il differe."""
    blob = {
        "releve": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": _base_url(),
        "sources": compte_rendu or {},
        "incidents": incidents or [],
        "rows": rows,
    }
    target = _cache_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        ancien = _read_cache(target)
        if ancien and ancien.get("rows") != rows:
            _previous_file().write_text(
                json.dumps(ancien, ensure_ascii=False), encoding="utf-8"
            )
        target.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        # Un cache non ecrit degrade le confort, pas la reponse : on continue
        # avec les lignes en memoire et on le dit dans le diagnostic.
        blob["cache_error"] = f"{target} : {exc}"
    return blob


def _age_hours(iso: str) -> float | None:
    try:
        stamp = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    delta = dt.datetime.now(dt.timezone.utc) - stamp
    return delta.total_seconds() / 3600.0


_MEMO: dict[str, Any] | None = None


def _payload(force: bool = False) -> dict[str, Any]:
    """Les taux, et TOUJOURS l'origine et la date du releve.

    Ordre : memoire du process, cache frais, appel a la source, cache perime en
    dernier recours. Un cache perime n'est jamais rendu en silence : la cle
    `avertissement` remonte jusqu'a l'en-tete de chaque rendu.
    """
    global _MEMO
    if not force and _MEMO is not None:
        age = _age_hours(_MEMO.get("releve", ""))
        if age is not None and age < _ttl_hours():
            return _MEMO

    cache = _read_cache(_cache_file())
    if not force and cache:
        age = _age_hours(cache.get("releve", ""))
        if age is not None and age < _ttl_hours():
            cache["origine"] = "cache local"
            cache["age_h"] = age
            _MEMO = cache
            return cache

    try:
        rows, compte_rendu, incidents = _fetch_live()
    except TvaError as exc:
        if cache:
            age = _age_hours(cache.get("releve", "")) or 0.0
            cache["origine"] = "cache local (source injoignable)"
            cache["age_h"] = age
            cache["avertissement"] = (
                f"La source n'a pas repondu ({exc}). Les taux ci-dessous sont "
                f"ceux du releve du {cache.get('releve', '?')}, soit "
                f"{age / 24:.1f} jour(s). Ce ne sont PAS forcement les taux du "
                "jour : ne les cite pas comme courants sans le dire."
            )
            _MEMO = cache
            return cache
        raise TvaError(
            f"{exc}\nAucun cache local a {_cache_file()} : il n'y a rien a "
            "rendre. Ce serveur n'affiche pas un taux qu'il n'a pas lu."
        ) from exc

    blob = _write_cache(rows, compte_rendu, incidents)
    blob["origine"] = "appel aux sources"
    blob["age_h"] = 0.0
    _MEMO = blob
    return blob


# --------------------------------------------------------------------------
# Resolution des pays : le coeur du garde-fou
# --------------------------------------------------------------------------

def _index(blob: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in _rows(blob.get("rows")):
        code = _norm(row.get("country_code"))
        if code:
            out[code] = row
    return out


def _resolve(pays: str, connus: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Traduit une demande en codes couverts par la source. Rend (codes, alertes).

    Le point important : ce qui n'est pas couvert n'est jamais ecarte en
    silence. Une question sur la Suisse ressort en alerte explicite, avec ou
    chercher la reponse. Un pays qu'on laisse tomber sans rien dire, c'est une
    ligne manquante dans un tableau qu'on lira comme complete.
    """
    ref = _ref()
    alias: dict[str, str] = {_norm(k): v for k, v in ref.get("alias", {}).items()}
    admin = {_norm(k): v for k, v in ref.get("administrations", {}).items()}
    territoires = {_norm(k): v for k, v in ref.get("territoires", {}).items()}
    membres = [_norm(c) for c in ref.get("etats_membres", [])]

    demande = [p for p in re.split(r"[,;/|]+", pays or "") if p.strip()]
    if not demande:
        perimetre = _perimetre_equipe()
        if perimetre:
            demande = perimetre
        else:
            # On repasse par le chemin normal : c'est lui qui pose la reserve
            # de provenance. Sortir ici rendait les 17 juridictions tenues a la
            # main sans jamais le dire.
            #
            # « TOUS » et non « * » : _norm efface les caracteres non
            # alphanumeriques, donc « * » ressort vide et la demande se perd en
            # silence. On passe par l'alias, qui traverse la normalisation.
            demande = ["TOUS"]

    codes: list[str] = []
    alertes: list[str] = []

    def ajoute(code: str) -> None:
        if code in connus and code not in codes:
            codes.append(code)

    for brut in demande:
        cle = _norm(brut)
        if not cle:
            continue
        cible = alias.get(cle, cle)

        if cible == "*":
            for code in sorted(connus):
                ajoute(code)
            continue
        if cible == "@UE":
            # « UE » ne veut PAS dire « tout ce que le connecteur sait ». Depuis
            # que le perimetre depasse l'Union, confondre les deux rendrait la
            # Suisse dans une reponse sur les Etats membres.
            for code in membres:
                ajoute(code)
            manquants = [c for c in membres if c not in connus]
            if manquants:
                alertes.append(
                    "RELEVE INCOMPLET - Etats membres absents : "
                    + ", ".join(manquants)
                    + ". Ce n'est pas « pas de TVA » : c'est un releve incomplet."
                )
            continue
        if cible == "@VU":
            perimetre = _perimetre_equipe()
            if not perimetre:
                alertes.append(
                    "PERIMETRE - « VU » demande, mais TVA_PAYS_VU n'est pas "
                    "renseigne : la liste des pays ou l'equipe travaille est "
                    "une decision, elle n'est pas devinee par ce serveur. A "
                    "poser dans 08_ENGINE/04_mcp/00_config/tva.shared.env. "
                    "Toutes les juridictions couvertes sont rendues a la place."
                )
                for code in sorted(connus):
                    ajoute(code)
            else:
                for part in perimetre:
                    sub, sub_alertes = _resolve(part, connus)
                    for code in sub:
                        ajoute(code)
                    alertes.extend(sub_alertes)
            continue

        if cible in connus:
            ajoute(cible)
            continue

        if cible in admin:
            # Le code est connu du referentiel mais absent du releve : c'est
            # vatnode qui n'a pas repondu, pas le pays qui n'a pas de TVA.
            info = admin[cible]
            alertes.append(
                f"ABSENT DU RELEVE - {info.get('nom', cible)} ({cible}) est "
                f"reconnu mais absent. {info.get('regime', '')} A verifier aupres de "
                f"{str(info.get('administration') or 'son administration fiscale').rstrip('.')}."
            )
            continue

        if cible in territoires:
            info = territoires[cible]
            alertes.append(
                f"NON COUVERT - {cible} : territoire a regime particulier, rattache a "
                f"{info.get('pays', '?')}. {info.get('regime', '')} "
                f"{info.get('consequence', '')}"
            )
            continue

        alertes.append(
            f"INCONNU - « {brut.strip()} » : ni un code, ni un nom reconnu. Le perimetre couvre les "
            "27 Etats membres, XI (Irlande du Nord) et 17 juridictions hors "
            "Union - et la Grece y porte le code EL, pas GR. tva_pays liste ce "
            "qui est reconnu, et ce qui ne l'est pas."
        )

    # La reserve de provenance, une fois pour toutes les lignes concernees.
    # Ligne par ligne elle serait du bruit ; absente, elle laisserait citer un
    # taux suisse avec la confiance d'un taux francais.
    a_la_main = [
        c for c in codes if _norm((connus.get(c) or {}).get("source")) != "TEDB"
    ]
    if a_la_main:
        ou = []
        for c in a_la_main:
            info = admin.get(c) or {}
            ou.append(f"{c} ({info.get('administration', 'administration nationale')})")
        seul = len(a_la_main) == 1
        alertes.append(
            "PROVENANCE - "
            + ", ".join(a_la_main)
            + (" ne vient PAS" if seul else " ne viennent PAS")
            + " de la base officielle de la Commission : "
            + ("c'est un taux tenu" if seul else "ce sont des taux tenus")
            + " a la main dans un jeu de donnees public. "
            + ("Bon" if seul else "Bons")
            + " pour comparer, a verifier avant de facturer, aupres de : "
            + ", ".join(ou)
            + "."
        )

    # Dedoublonnage, en gardant l'ordre. La branche « VU » rappelle _resolve
    # pour chaque element du perimetre : chaque sous-appel pose sa propre
    # reserve de provenance, et l'appel englobant en pose une de plus. Sans ce
    # filtre, le meme avertissement s'affiche deux fois - et un avertissement
    # repete finit par ne plus etre lu.
    uniques: list[str] = []
    for alerte in alertes:
        if alerte not in uniques:
            uniques.append(alerte)

    return codes, uniques


# --------------------------------------------------------------------------
# Mise en forme
# --------------------------------------------------------------------------

def _num(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "oui" if value else "non"
    if isinstance(value, (int, float)):
        # Le separateur decimal reste le point : c'est un CSV relu par une
        # machine autant que par un humain, et une virgule dans un CSV
        # point-virgule se lit, mais se retype mal.
        return f"{value:g}"
    return str(value)


def _liste_num(value: Any) -> str:
    if not isinstance(value, list):
        return _num(value)
    return " | ".join(_num(v) for v in value if v is not None)


def _provenance(row: dict[str, Any]) -> str:
    """Deux mots dans une cellule, pour que la colonne reste lisible.

    « TEDB » se lit comme officiel, « main » comme a verifier. Le detail est
    dans LEGAL_NOTE et dans tva_pays ; ici on n'a que la largeur d'une colonne.
    """
    src = str(row.get("source") or "")
    if src == "TEDB":
        return "TEDB"
    if src:
        return f"{src} (main)"
    return ""


def _vue(row: dict[str, Any], categorie: str = "") -> dict[str, str]:
    out = {
        "code": _norm(row.get("country_code")),
        "pays": str(row.get("country_name") or ""),
        "taux_normal": _num(row.get("standard_rate")),
        "taux_reduits": _liste_num(row.get("reduced_rates")),
        "taux_super_reduit": _num(row.get("super_reduced_rate")),
        "taux_parking": _num(row.get("parking_rate")),
        "devise": str(row.get("currency") or ""),
        "provenance": _provenance(row),
        "effet": str(row.get("effective_on") or ""),
    }
    if categorie:
        cats = row.get("rate_categories")
        cats = cats if isinstance(cats, dict) else {}
        trouve = ""
        for name, rates in cats.items():
            if _norm(name) == _norm(categorie):
                trouve = _liste_num(rates)
                break
        out[f"taux_{_norm(categorie).lower()}"] = trouve
    return out


def _to_csv_text(rows: list[dict[str, Any]], cols: list[str]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=cols,
        delimiter=";",
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in cols})
    return buf.getvalue()


def _entete(blob: dict[str, Any], titre: str) -> list[str]:
    """Les lignes de fraicheur. Elles precedent TOUJOURS les taux.

    Un taux sans sa date de releve est un taux faux en puissance : c'est la
    regle 3 du CLAUDE.md de l'equipe appliquee a une source externe.
    """
    lines = [titre]
    releve = str(blob.get("releve") or "?")
    age = blob.get("age_h")
    age_txt = ""
    if isinstance(age, (int, float)):
        age_txt = f" (il y a {age:.0f} h)" if age < 48 else f" (il y a {age / 24:.0f} jours)"
    lines.append(f"Releve du {releve}{age_txt} - origine : {blob.get('origine', '?')}")
    if blob.get("avertissement"):
        lines.append(f"ATTENTION : {blob['avertissement']}")
    # Un incident de source ne fausse pas les lignes rendues, mais il change ce
    # qui MANQUE. Une devise absente ou tout le hors-Union disparu se lit comme
    # une reponse complete si on ne le dit pas.
    for incident in blob.get("incidents") or []:
        lines.append(f"ATTENTION : {incident}")
    if isinstance(age, (int, float)) and age > STALE_DAYS * 24:
        lines.append(
            f"ATTENTION : ce releve a plus de {STALE_DAYS:.0f} jours. Un taux "
            "peut avoir change depuis - appelle tva_rafraichir avant de citer "
            "ces valeurs."
        )
    return lines


def _rendu(
    blob: dict[str, Any],
    titre: str,
    vues: list[dict[str, str]],
    cols: list[str],
    alertes: list[str],
    notes: list[str] | None = None,
) -> str:
    lines = _entete(blob, titre)
    # Les messages arrivent deja qualifies par _resolve (NON COUVERT,
    # ABSENT DU RELEVE, PROVENANCE, INCONNU...) : les prefixer en aveugle
    # ferait passer une reserve de provenance pour une absence de taux.
    for alerte in alertes:
        lines.append(alerte)
    for note in notes or []:
        lines.append(note)
    lines.append(f"{len(vues)} pays.")
    if not vues:
        lines.append("(aucune ligne)")
        lines.append("")
        lines.append(LEGAL_NOTE)
        return "\n".join(lines)
    body = _to_csv_text(vues, cols)
    if len(body) > RENDER_MAX_CHARS:
        body = body[:RENDER_MAX_CHARS] + "\n... (rendu tronque)"
    lines.append("")
    lines.append(body)
    lines.append(LEGAL_NOTE)
    return "\n".join(lines)


def _guard(fn):
    """Rend les erreurs comme message lisible plutot qu'en exception.

    functools.wraps n'est pas cosmetique : il pose __wrapped__, que
    inspect.signature suit. Sans lui, FastMCP lit la signature du wrapper et
    publie chaque outil avec (args, kwargs) au lieu des vrais parametres - les
    outils sortent du handshake mais sont INAPPELABLES. Le bug est deja passe
    une fois chez shiptify.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ConfigError, TvaError) as exc:
            return f"ERREUR : {exc}"

    return wrapper


# --------------------------------------------------------------------------
# Outils MCP
# --------------------------------------------------------------------------

SERVER_INSTRUCTIONS = """\
Connecteur en LECTURE SEULE sur les taux de TVA de 45 juridictions europeennes,
depuis DEUX sources publiques qui n'ont pas la meme autorite.

Quatre choses a savoir avant de citer un taux :

1. DEUX SOURCES, DEUX NIVEAUX DE CONFIANCE. Les 27 Etats membres et XI
   (Irlande du Nord) viennent de TEDB, la base officielle de la Commission
   europeenne, alimentee par les Etats membres. Les 17 autres juridictions -
   dont la Suisse, la Norvege et le Royaume-Uni - viennent d'un jeu de donnees
   public tenu A LA MAIN. La colonne `provenance` le dit sur chaque ligne. Ne
   presente jamais un taux suisse avec l'assurance d'un taux francais.

2. « UE » N'EST PAS « TOUT ». « UE » designe les 27 Etats membres ; « Europe »
   ou « tous » les 45 juridictions. XI n'est PAS un Etat membre, et une
   livraison a Belfast ne se traite pas comme une livraison en Grande-Bretagne.

3. LA GRECE PORTE LE CODE EL, pas GR. C'est la nomenclature TVA de l'Union.
   Un rapprochement fait sur des codes ISO perd la Grece en silence.

4. UN TAUX SE CITE AVEC SA DATE DE RELEVE. Chaque rendu la porte en tete, et
   les lignes TEDB portent en plus la date d'effet du taux. Pour une facture,
   une declaration ou un parametrage d'ERP, la reference reste l'administration
   fiscale du pays.

Ce qu'AUCUNE des deux sources ne couvre : les territoires a regime particulier
- Canaries, Ceuta-Melilla, Madere, Acores, Corse, DOM, Aland, Busingen,
Livigno, Mont Athos. Un taux national existe et se cite par reflexe : c'est la
premiere cause d'erreur de facturation. tva_pays les nomme.

Commence par tva_taux. tva_detail donne, pour une juridiction TEDB, les
categories de biens, les CODES DE NOMENCLATURE DOUANIERE (CN) qui relient une
categorie a un produit reel, les commentaires officiels et les exemptions.
tva_changements dit si un taux a bouge depuis le releve precedent. N'exporte en
CSV que si l'utilisateur a demande un export.
"""


def _hints(titre: str, monde_ouvert: bool, idempotent: bool = True) -> ToolAnnotations:
    """Les annotations de comportement de la specification MCP.

    `readOnlyHint` a vrai partout : aucun outil n'a de chemin d'ecriture vers
    la source. Ce ne sont que des indications - la specification demande aux
    clients de ne pas leur faire confiance - ce qui garantit la lecture seule
    reste le code : un seul chemin appele, en GET, sans parametre.

    `openWorldHint` distingue ce qui lit le referentiel embarque (ensemble
    ferme, reponse reproductible) de ce qui peut rappeler la source.
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

mcp = FastMCP("tva", instructions=SERVER_INSTRUCTIONS)


@mcp.tool(annotations=_hints("Taux de TVA par pays", SUR_LA_SOURCE))
@_guard
def tva_taux(pays: str = "", categorie: str = "") -> str:
    """Les taux de TVA des pays demandes : normal, reduits, super-reduit, parking.

    C'est l'outil a appeler par defaut. Il remplace le script Power Query, en
    rendant les colonnes que celui-ci jetait.

    pays : codes ou noms separes par des virgules - « FR,DE,IT », « France,
        Allemagne », « GR » (traduit en EL), « Suisse », « UK ». « UE » pour
        les 27 Etats membres SEULEMENT ; « Europe » ou « tous » pour les 45
        juridictions couvertes. « VU » pour le perimetre d'equipe, s'il a ete
        pose dans le fichier partage. Vide = le perimetre d'equipe s'il
        existe, sinon tout le perimetre couvert.
    categorie : ajoute une colonne avec le taux applicable a cette categorie de
        biens - « transport_passengers », « foodstuffs », « restaurant ». La
        liste exacte se lit avec tva_categories.

    La colonne `provenance` dit d'ou vient chaque ligne : « TEDB » pour la
    base officielle de la Commission, « vatnode (main) » pour un taux tenu a
    la main. Les deux ne s'invoquent pas avec la meme assurance.

    Ce qui n'est pas couvert ressort en tete, nomme, avec ou chercher la
    reponse : jamais une ligne manquante en silence.
    """
    blob = _payload()
    connus = _index(blob)
    codes, alertes = _resolve(pays, connus)
    vues = [_vue(connus[c], categorie) for c in codes]
    cols = list(BRIEF_COLUMNS)
    notes: list[str] = []
    if categorie:
        col = f"taux_{_norm(categorie).lower()}"
        cols.append(col)
        vides = [v["code"] for v in vues if not v.get(col)]
        if vides:
            notes.append(
                f"Categorie « {categorie} » : aucune valeur pour "
                + ", ".join(vides)
                + ". Une cellule vide veut dire « pas de taux reduit declare "
                "pour cette categorie dans cette source », PAS « exonere » et "
                "pas « zero » - la lecture par defaut est alors le taux normal, "
                "a verifier."
            )
    titre = "Taux de TVA" + (f" - categorie {categorie}" if categorie else "")
    return _rendu(blob, titre, vues, cols, alertes, notes)


@mcp.tool(annotations=_hints("Fiche complete d'un pays", SUR_LA_SOURCE))
@_guard
def tva_detail(pays: str, max_commentaires: int = 6) -> str:
    """Tout ce que la source dit d'UN pays : categories de biens et commentaires.

    C'est ici qu'on repond a « pourquoi 5,5 % » ou « quel taux sur le transport
    de personnes » : `rate_categories` associe chaque taux reduit aux categories
    de biens et services concernees, et `rate_comments` porte le texte officiel.

    max_commentaires : nombre de taux dont on affiche le commentaire. Les
        commentaires sont volumineux (plusieurs milliers de caracteres par
        pays) : on les borne pour ne pas saturer la conversation.
    """
    blob = _payload()
    connus = _index(blob)
    codes, alertes = _resolve(pays, connus)
    if not codes:
        lines = _entete(blob, f"Detail TVA : {pays}")
        lines.extend(alertes)
        lines.append("Aucun pays couvert dans cette demande : rien a afficher.")
        lines.append("")
        lines.append(LEGAL_NOTE)
        return "\n".join(lines)
    if len(codes) > 1:
        alertes.append(
            f"{len(codes)} pays demandes, seul {codes[0]} est detaille. Pour "
            "comparer plusieurs pays, utilise tva_taux."
        )
    row = connus[codes[0]]

    lines = _entete(blob, f"Detail TVA - {row.get('country_name')} ({codes[0]})")
    for alerte in alertes:
        lines.append(alerte)
    lines.append("")
    lines.append(f"Taux normal        : {_num(row.get('standard_rate'))}")
    lines.append(f"Taux reduits       : {_liste_num(row.get('reduced_rates')) or '(aucun)'}")
    lines.append(f"Super-reduit       : {_num(row.get('super_reduced_rate')) or '(aucun)'}")
    lines.append(f"Taux parking       : {_num(row.get('parking_rate')) or '(aucun)'}")
    lines.append(f"Devise             : {row.get('currency') or '(inconnue)'}")
    lines.append(f"Etat membre        : {_num(row.get('member_state'))}")
    lines.append(f"Provenance         : {_provenance(row)}")
    if row.get("effective_on"):
        lines.append(f"Taux normal en vigueur depuis le {row['effective_on']}")
    if _norm(row.get("source")) != "TEDB":
        info = _ref().get("administrations", {}).get(codes[0], {})
        lines.append("")
        lines.append(
            "ATTENTION - cette juridiction ne vient PAS de la base officielle "
            "de la Commission. Le taux est tenu a la main dans un jeu de "
            "donnees public : il n'y a ni categorie de biens, ni code de "
            "nomenclature, ni commentaire officiel, ni date d'effet."
        )
        if info.get("regime"):
            lines.append(f"  Regime : {info['regime']}")
        if info.get("administration"):
            lines.append(f"  A verifier aupres de : {info['administration']}")
        if info.get("attention"):
            lines.append(f"  {info['attention']}")

    cats = row.get("rate_categories")
    cats = cats if isinstance(cats, dict) else {}
    lines.append("")
    cn = row.get("cn_codes")
    cn = cn if isinstance(cn, dict) else {}
    lines.append(f"Categories de biens et services ({len(cats)}) : taux applicable")
    if not cats:
        lines.append("  (aucune declaree pour cette juridiction)")
    for name in sorted(cats):
        # Les codes de nomenclature douaniere sont ce qui permet de rattacher
        # la categorie a un produit reel du catalogue. On en montre quelques-uns
        # : la liste complete part dans l'export.
        codes_cn = cn.get(name) or []
        suffixe = ""
        if codes_cn:
            apercu = " ".join(codes_cn[:6])
            reste = f" +{len(codes_cn) - 6}" if len(codes_cn) > 6 else ""
            suffixe = f"   [CN {apercu}{reste}]"
        lines.append(f"  {name} : {_liste_num(cats[name])}{suffixe}")

    exemptions = row.get("exemptions")
    if isinstance(exemptions, list) and exemptions:
        lines.append("")
        lines.append(
            f"Categories exemptees ou hors champ ({len(exemptions)}) - "
            "attention, « exempte » n'est pas « taux zero » sur une facture :"
        )
        lines.append("  " + ", ".join(exemptions))

    comments = row.get("rate_comments")
    comments = comments if isinstance(comments, dict) else {}
    if comments:
        garde = sorted(comments, key=lambda k: -len(str(comments[k])))[
            : max(0, int(max_commentaires))
        ]
        lines.append("")
        lines.append(
            f"Commentaires officiels ({len(garde)} taux sur {len(comments)}, "
            "tronques) :"
        )
        for taux in garde:
            texte = comments[taux]
            items = texte if isinstance(texte, list) else [texte]
            lines.append(f"  --- taux {taux} ---")
            for item in items[:8]:
                flat = " ".join(str(item).split())
                lines.append(f"    - {flat[:400]}" + ("..." if len(flat) > 400 else ""))

    territoires = [
        (nom, info)
        for nom, info in _ref().get("territoires", {}).items()
        if _norm(info.get("pays")) == codes[0]
    ]
    if territoires:
        lines.append("")
        lines.append("ATTENTION - territoires a regime particulier rattaches a ce pays :")
        for nom, info in territoires:
            lines.append(f"  {nom} : {info.get('regime')} {info.get('consequence')}")
        lines.append(
            "  La source rend UN taux par pays. Pour une livraison sur l'un de "
            "ces territoires, le taux ci-dessus ne repond pas."
        )

    lines.append("")
    lines.append(LEGAL_NOTE)
    out = "\n".join(lines)
    return out if len(out) <= RENDER_MAX_CHARS else out[:RENDER_MAX_CHARS] + "\n... (tronque)"


@mcp.tool(annotations=_hints("Categories de biens disponibles", SUR_LA_SOURCE))
@_guard
def tva_categories(pays: str = "") -> str:
    """Les categories de biens et services que la source sait qualifier.

    A appeler avant de passer `categorie` a tva_taux : les noms sont techniques
    et anglais (`transport_passengers`, `foodstuffs`, `private_dwellings`), et
    un nom approche ne rend pas une erreur, il rend une colonne vide.

    pays : restreint le decompte a ces pays. Vide = tous.
    """
    blob = _payload()
    connus = _index(blob)
    codes, alertes = _resolve(pays, connus)
    compte: dict[str, int] = {}
    for code in codes:
        cats = connus[code].get("rate_categories")
        if isinstance(cats, dict):
            for name in cats:
                compte[name] = compte.get(name, 0) + 1

    lines = _entete(blob, "Categories de biens et services")
    for alerte in alertes:
        lines.append(alerte)
    lines.append(
        f"{len(compte)} categorie(s) declaree(s), sur {len(codes)} pays "
        "interroge(s). Le nombre entre parentheses est le nombre de pays qui "
        "declarent un taux reduit pour cette categorie."
    )
    lines.append("")
    for name in sorted(compte, key=lambda n: (-compte[n], n)):
        lines.append(f"  {name} ({compte[name]})")
    lines.append("")
    lines.append(
        "Une categorie absente d'un pays ne veut pas dire « exonere » : elle "
        "veut dire qu'aucun taux reduit n'est declare, donc que le taux normal "
        "s'applique a priori - a verifier avant de facturer."
    )
    lines.append(LEGAL_NOTE)
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Codes pays reconnus, et ce qui n'est pas couvert", HORS_LIGNE))
@_guard
def tva_pays(recherche: str = "") -> str:
    """Ce que le connecteur couvre, avec quelle AUTORITE, et ce qu'il ne couvre pas.

    Repond depuis le referentiel local, sans appel aux sources. C'est l'outil a
    lire avant de conclure qu'un pays « n'a pas de TVA » : la bonne reponse est
    souvent « ce n'est pas la meme source qui le dit », ce qui n'est pas la
    meme chose.

    Le partage qui compte n'est plus couvert / non couvert - depuis le passage
    a TEDB, presque toute l'Europe est couverte. C'est OFFICIEL / TENU A LA
    MAIN : les 27 et XI viennent de la base de la Commission, les 17 autres
    d'un jeu de donnees public entretenu manuellement.

    recherche : filtre sur un code, un nom ou un territoire.
    """
    ref = _ref()
    besoin = _norm(recherche)

    def garde(*champs: Any) -> bool:
        if not besoin:
            return True
        return any(besoin in _norm(c) for c in champs)

    codes = ref.get("codes", {})
    membres = [_norm(c) for c in ref.get("etats_membres", [])]
    admin = ref.get("administrations", {})

    lines = ["Referentiel des juridictions - connecteur tva", ""]
    lines.append(
        f"SOURCE OFFICIELLE - TEDB, Commission europeenne ({len(membres)} Etats "
        "membres + XI)"
    )
    lines.append(
        "  Taux, categories de biens, codes de nomenclature douaniere, "
        "commentaires officiels et date d'effet."
    )
    officiels = [c for c in membres + ["XI"] if garde(c, codes.get(c))]
    for code in officiels:
        marque = ""
        if code == "EL":
            marque = "  <- code TVA de l'Union, PAS le code ISO GR"
        elif code == "XI":
            marque = "  <- Irlande du Nord : dans le champ TVA de l'Union pour les biens, distincte de GB"
        lines.append(f"  {code}  {codes.get(code, '?')}{marque}")
    if not officiels:
        lines.append("  (aucun code ne correspond a la recherche)")

    lines.append("")
    hors_ue = [c for c in sorted(codes) if c not in membres and c != "XI"]
    lines.append(
        f"SOURCE TENUE A LA MAIN - vatnode, licence MIT ({len(hors_ue)} "
        "juridictions hors Union)"
    )
    lines.append(
        "  Taux et devise seulement. Ni categorie, ni code de nomenclature, ni "
        "commentaire, ni date d'effet. A verifier avant de facturer."
    )
    trouves_hors = [
        c for c in hors_ue if garde(c, codes.get(c), (admin.get(c) or {}).get("regime"))
    ]
    for code in trouves_hors:
        info = admin.get(code) or {}
        lines.append(f"  {code}  {codes.get(code, '?')} - {info.get('regime', '')}")
        if info.get("administration"):
            lines.append(f"        A verifier aupres de : {info['administration']}")
        if info.get("attention"):
            lines.append(f"        {info['attention']}")
    if not trouves_hors:
        lines.append("  (aucun ne correspond a la recherche)")

    lines.append("")
    lines.append(
        "NON COUVERTS PAR AUCUNE DES DEUX SOURCES - territoires a regime "
        "particulier : le taux national ne s'y applique pas"
    )
    terr = ref.get("territoires", {})
    trouves_terr = [
        (n, i) for n, i in sorted(terr.items()) if garde(n, i.get("pays"), i.get("regime"))
    ]
    for nom, info in trouves_terr:
        lines.append(f"  {nom} (rattache a {info.get('pays')}) - {info.get('regime')}")
        lines.append(f"        {info.get('consequence', '')}")
    if not trouves_terr:
        lines.append("  (aucun ne correspond a la recherche)")
    if ref.get("territoires_commentaire"):
        lines.append(f"  {ref['territoires_commentaire']}")

    perimetre = _perimetre_equipe()
    lines.append("")
    if perimetre:
        lines.append("Perimetre d'equipe TVA_PAYS_VU : " + ", ".join(perimetre))
    else:
        lines.append(
            "Perimetre d'equipe TVA_PAYS_VU : non renseigne. « VU » et un appel "
            "sans pays rendent donc TOUTES les juridictions couvertes. La "
            "liste des pays ou l'equipe "
            "travaille est une decision : elle se pose dans "
            "08_ENGINE/04_mcp/00_config/tva.shared.env, pas dans ce code."
        )
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Ce qui a change depuis le releve precedent", HORS_LIGNE))
@_guard
def tva_changements() -> str:
    """Compare le releve courant au precedent, et dit quels taux ont bouge.

    C'est la raison d'etre du cache : « le taux a-t-il change » n'a pas de
    reponse sans un point de comparaison. Le releve precedent n'est conserve
    que lorsque la source a effectivement rendu autre chose.

    Un rendu « aucune comparaison possible » n'est pas « rien n'a change » :
    c'est qu'il n'y a qu'un seul releve sur ce poste.
    """
    courant = _read_cache(_cache_file())
    if not courant:
        return (
            "Aucun releve en cache sur ce poste : appelle tva_taux ou "
            "tva_rafraichir une premiere fois, puis reviens. Un seul releve ne "
            "permet aucune comparaison."
        )
    precedent = _read_cache(_previous_file())
    if not precedent:
        return (
            f"Un seul releve en cache ({courant.get('releve')}). Aucune "
            "comparaison possible - ce n'est PAS « aucun changement ». Le "
            "releve precedent n'est conserve qu'a partir du moment ou la source "
            "rend une valeur differente."
        )

    avant = _index(precedent)
    apres = _index(courant)
    champs = (
        ("standard_rate", "taux normal"),
        ("reduced_rates", "taux reduits"),
        ("super_reduced_rate", "super-reduit"),
        ("parking_rate", "parking"),
        ("currency", "devise"),
    )

    lignes: list[dict[str, str]] = []
    for code in sorted(set(avant) | set(apres)):
        if code not in avant:
            lignes.append({"code": code, "champ": "(pays)", "avant": "absent", "apres": "present"})
            continue
        if code not in apres:
            lignes.append({"code": code, "champ": "(pays)", "avant": "present", "apres": "absent"})
            continue
        for cle, label in champs:
            a, b = avant[code].get(cle), apres[code].get(cle)
            if a != b:
                lignes.append(
                    {
                        "code": code,
                        "champ": label,
                        "avant": _liste_num(a) or "(vide)",
                        "apres": _liste_num(b) or "(vide)",
                    }
                )

    out = [
        "Changements de taux entre deux releves",
        f"Releve precedent : {precedent.get('releve')}",
        f"Releve courant   : {courant.get('releve')}",
        "",
    ]
    if not lignes:
        out.append(
            "Aucun ecart sur les taux entre ces deux releves. Un ecart existe "
            "peut-etre sur les categories ou les commentaires : ils ne sont pas "
            "compares ici, seuls les taux le sont."
        )
    else:
        out.append(f"{len(lignes)} ecart(s) :")
        out.append("")
        out.append(_to_csv_text(lignes, ["code", "champ", "avant", "apres"]))
        out.append(
            "Un ecart n'est pas forcement un changement de loi : ce peut etre "
            "une correction de la source. A confirmer aupres de l'administration "
            "fiscale du pays avant d'en tirer une consequence de facturation."
        )
    out.append(LEGAL_NOTE)
    return "\n".join(out)


@mcp.tool(annotations=_hints("Rappeler la source maintenant", SUR_LA_SOURCE, idempotent=False))
@_guard
def tva_rafraichir() -> str:
    """Force un appel a la source, sans attendre l'expiration du cache.

    A appeler quand la question porte sur l'instant - « le taux a-t-il change
    cette semaine » - ou apres un rendu signale comme perime. Le releve
    precedent est mis de cote s'il differe, ce qui rend tva_changements
    utilisable juste apres.
    """
    avant = _read_cache(_cache_file())
    blob = _payload(force=True)
    lines = _entete(blob, "Releve rafraichi")
    if blob.get("cache_error"):
        lines.append(
            f"ATTENTION : le cache n'a pas pu etre ecrit ({blob['cache_error']}). "
            "Les taux sont a jour dans cette session, mais la comparaison avec "
            "le prochain releve ne sera pas possible."
        )
    lines.append(f"{len(_index(blob))} pays dans le releve.")
    if avant and _index(avant) and avant.get("rows") != blob.get("rows"):
        lines.append(
            "La source rend autre chose que le releve precedent : appelle "
            "tva_changements pour voir quoi exactement."
        )
    elif avant:
        lines.append("Identique au releve precedent, aux taux comme au reste.")
    lines.append(LEGAL_NOTE)
    return "\n".join(lines)


@mcp.tool(annotations=_hints("Exporter en CSV (sur demande explicite)", SUR_LA_SOURCE, idempotent=False))
@_guard
def tva_export_csv(
    pays: str = "",
    forme: str = "pays",
    filename: str = "",
) -> str:
    """Ecrit les taux dans un CSV. SUR DEMANDE EXPLICITE de l'utilisateur.

    N'appelle cet outil QUE si un fichier, un export ou un CSV a ete demande :
    il ecrit sur le disque, ce n'est pas une etape de routine.

    forme :
      « pays »       une ligne par pays, les taux en colonnes. Le remplacant
                     direct du script Power Query, en plus complet.
      « categories » une ligne par pays x categorie x taux. C'est la forme a
                     charger dans Power BI pour croiser un taux avec une
                     famille de produits.

    CSV point-virgule, UTF-8 avec BOM : il s'ouvre dans Excel FR sans passer par
    l'assistant d'import. Il atterrit dans le dossier local du connecteur,
    jamais dans la bibliotheque d'equipe - un CSV pose dans un dossier
    synchronise part chez tout le monde.
    """
    blob = _payload()
    connus = _index(blob)
    codes, alertes = _resolve(pays, connus)
    releve = str(blob.get("releve") or "")

    choix = _norm(forme)
    if choix in {"PAYS", "", "COURT"}:
        cols = list(BRIEF_COLUMNS) + ["effet", "etat_membre", "releve"]
        rows: list[dict[str, Any]] = []
        for code in codes:
            vue = dict(_vue(connus[code]))
            vue["etat_membre"] = _num(connus[code].get("member_state"))
            vue["releve"] = releve
            rows.append(vue)
    elif choix in {"CATEGORIES", "CATEGORIE", "LONG"}:
        cols = [
            "code", "pays", "taux_normal", "categorie", "taux_categorie",
            "codes_cn", "devise", "provenance", "releve",
        ]
        rows = []
        for code in codes:
            row = connus[code]
            cats = row.get("rate_categories")
            cats = cats if isinstance(cats, dict) else {}
            cn = row.get("cn_codes")
            cn = cn if isinstance(cn, dict) else {}
            base = {
                "code": code,
                "pays": str(row.get("country_name") or ""),
                "taux_normal": _num(row.get("standard_rate")),
                "devise": str(row.get("currency") or ""),
                "provenance": _provenance(row),
                "releve": releve,
            }
            if not cats:
                rows.append({**base, "categorie": "", "taux_categorie": ""})
                continue
            for name in sorted(cats):
                valeurs = cats[name]
                valeurs = valeurs if isinstance(valeurs, list) else [valeurs]
                for valeur in valeurs:
                    rows.append(
                        {
                            **base,
                            "categorie": name,
                            "taux_categorie": _num(valeur),
                            # Les codes de nomenclature douaniere sont ce qui
                            # relie une categorie a un produit reel du
                            # catalogue. Ils n'existent que sur les lignes
                            # TEDB.
                            "codes_cn": " ".join(cn.get(name, [])),
                        }
                    )
    else:
        raise TvaError(
            f"forme inconnue : « {forme} ». Valeurs permises : « pays » (une "
            "ligne par pays) ou « categories » (une ligne par pays x categorie)."
        )

    target_dir = _export_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TvaError(f"Dossier d'export inutilisable ({target_dir}) : {exc}") from exc

    if filename.strip():
        name = filename.strip()
        if not name.lower().endswith(".csv"):
            name += ".csv"
    else:
        name = f"tva_{choix.lower() or 'pays'}_{dt.date.today().isoformat()}.csv"
    target = target_dir / name

    try:
        # newline="" plutot que write_text : sans lui, l'ecriture traduit les
        # fins de ligne en CRLF sous Windows et les laisse en LF sous macOS. Le
        # meme code produirait deux fichiers differents selon le poste, ce que
        # la convention de construction interdit (08_ENGINE/04_mcp/README.md).
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            handle.write(_to_csv_text(rows, cols))
    except OSError as exc:
        raise TvaError(f"Ecriture impossible dans {target} : {exc}") from exc

    out = [
        f"Export termine : {target}",
        f"{len(rows)} ligne(s), {len(cols)} colonne(s), {len(codes)} pays.",
        f"Forme : {choix.lower()} | releve du {releve} | origine : {blob.get('origine')}",
        "La colonne `releve` porte la date du releve sur chaque ligne : un "
        "fichier de taux qui circule sans sa date est un fichier dangereux.",
    ]
    if blob.get("avertissement"):
        out.insert(1, f"ATTENTION : {blob['avertissement']}")
    for alerte in alertes:
        out.append(alerte)
    out.append("")
    out.append("Colonnes : " + ", ".join(cols))
    out.append(LEGAL_NOTE)
    return "\n".join(out)


@mcp.tool(annotations=_hints("Diagnostic complet, appel a la source compris", SUR_LA_SOURCE, idempotent=False))
def tva_doctor() -> str:
    """Diagnostic : interpreteur, chemins, configuration lue, et vrai appel.

    Le premier outil a appeler quand quelque chose coince. Il n'y a aucune cle
    a masquer ici : la source est publique et anonyme.
    """
    _load_env_file()
    lines = ["Configuration du serveur MCP tva", ""]
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    lines.append(
        "Mode                     : "
        + (f"plugin ({plugin_root})" if plugin_root else "installation directe")
    )
    lines.append(f"Interpreteur             : {sys.executable}")
    lines.append(f"Racine locale            : {_local_root()}")
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
            "  Les processus enfants d'un interpreteur empaquete voient une vue "
            "virtualisee de %LOCALAPPDATA%. Ce connecteur range donc tout sous "
            f"{_local_root()}, qui n'est pas virtualise."
        )
    lines.append("")
    lines.extend(_shared_report())
    lines.append("")
    lines.append(
        "Ordre de priorite        : configuration du plugin > tva.env du poste "
        "> config d'equipe > defaut du serveur"
    )
    lines.append("")
    lines.append(f"TVA_BASE_URL             : {_base_url()}")
    lines.append(
        "TVA_VATNODE_URL          : "
        + (_vatnode_url() or f"{sources.VATNODE_URL} (defaut, avec repli)")
    )
    lines.append(f"TVA_TIMEOUT_S            : {_timeout()}")
    lines.append(f"TVA_TTL_HOURS            : {_ttl_hours()}")
    lines.append(
        "TVA_PAYS_VU              : "
        + (
            ", ".join(_perimetre_equipe())
            or "(non renseigne - tout le perimetre couvert par defaut)"
        )
    )
    lines.append(f"Dossier d'export         : {_export_dir()}")

    cache = _read_cache(_cache_file())
    if cache:
        age = _age_hours(cache.get("releve", ""))
        age_txt = f"{age:.1f} h" if age is not None else "age inconnu"
        lines.append(
            f"Cache                    : {_cache_file()} - releve "
            f"{cache.get('releve')} ({age_txt}), {len(_index(cache))} pays"
        )
    else:
        lines.append(f"Cache                    : {_cache_file()} (aucun)")
    precedent = _read_cache(_previous_file())
    lines.append(
        "Releve precedent         : "
        + (f"{_previous_file()} - {precedent.get('releve')}" if precedent else "(aucun)")
    )

    try:
        ref = _ref()
        membres = ref.get("etats_membres", [])
        codes_ref = ref.get("codes", {})
        lines.append(
            f"Referentiel local        : {len(codes_ref)} juridictions "
            f"({len(membres)} Etats membres + XI en officiel, "
            f"{len(codes_ref) - len(membres) - 1} tenues a la main), "
            f"{len(ref.get('territoires', {}))} territoires a regime particulier"
        )
    except ConfigError as exc:
        lines.append(f"Referentiel local        : ERREUR - {exc}")

    lines.append("")
    lines.append("Appels de verification - les deux sources, separement")
    # Separement, et c'est le point : une panne de vatnode n'a pas les memes
    # consequences qu'une panne de TEDB, et un diagnostic qui les confond ne
    # dit pas quoi reparer.
    try:
        lignes_tedb = sources.fetch_tedb(_timeout(), endpoint=_base_url())
        codes_tedb = sorted(_norm(r.get("country_code")) for r in lignes_tedb)
        avec_cat = sum(1 for r in lignes_tedb if r.get("rate_categories"))
        lines.append(
            f"  [officiel] POST {_base_url()} : OK | "
            f"{len(lignes_tedb)} juridictions, {avec_cat} avec categories | "
            + ", ".join(codes_tedb)
        )
    except sources.SourceError as exc:
        lines.append(f"  [officiel] POST {_base_url()} : ECHEC - {exc}")
        lines.append(
            "    Consequence : AUCUNE reponse possible pour les Etats membres. "
            "TEDB est la source qui ne se degrade pas."
        )

    try:
        hors, devises, version = sources.fetch_vatnode(
            _timeout(), url_base=_vatnode_url()
        )
        lines.append(
            f"  [tenu a la main] GET {_vatnode_url() or sources.VATNODE_URL}"
            f" : OK | jeu du "
            f"{version or '?'} | {len(hors)} juridictions hors Union, "
            f"{len(devises)} devises"
        )
    except sources.SourceError as exc:
        lines.append(
            f"  [tenu a la main] GET "
            f"{_vatnode_url() or sources.VATNODE_URL} : ECHEC - {exc}"
        )
        lines.append(
            "    Consequence : ni devise, ni juridiction hors Union - CH, GB et "
            "NO seraient absents. Ce n'est PAS « ils n'ont pas de TVA »."
        )

    try:
        rows, _cr, incidents = _fetch_live()
        codes = sorted(_norm(r.get("country_code")) for r in rows)
        lines.append(
            f"  [fusion] {len(rows)} juridictions | " + ", ".join(codes)
        )
        for incident in incidents:
            lines.append(f"    INCIDENT : {incident}")
        # Ces quatre-la sont desormais ATTENDUS. Leur absence n'est plus
        # « normal, la source ne les couvre pas » : c'est un defaut a reparer.
        manquants = [c for c in ("CH", "NO", "GB", "XI") if c not in codes]
        if manquants:
            lines.append(
                "  ANOMALIE : " + ", ".join(manquants) + " devraient etre "
                "couverts et sont absents. XI vient de TEDB, CH/NO/GB de "
                "vatnode : regarde lequel des deux appels ci-dessus a echoue."
            )
        else:
            lines.append(
                "  CH, NO, GB et XI sont bien presents - c'est ce que le "
                "passage a deux sources devait apporter."
            )
    except (ConfigError, TvaError) as exc:
        lines.append(f"  [fusion] ECHEC | {exc}")
        if cache:
            lines.append(
                "  Le cache local permet quand meme de repondre, avec son age "
                "annonce en tete de chaque rendu."
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

HELP = """\
MCP tva - les taux de TVA de 45 juridictions europeennes, en lecture seule.

  python server.py            mode serveur MCP (stdio), lance par Claude Code
  python server.py doctor     diagnostic de configuration et de connexion
  python server.py --help     cette aide

Aucune cle d'API : les deux sources (TEDB, vatnode) sont publiques et anonymes.

Configuration, en deux couches, et toutes deux optionnelles :
  - l'equipe : 08_ENGINE/04_mcp/00_config/tva.shared.env - racine d'API, duree
    de fraicheur, et le perimetre de pays TVA_PAYS_VU ;
  - le poste : tva.env hors du vault, par defaut ~/.tva-mcp/tva.env. Modele :
    tva.env.example.
Priorite : configuration du plugin > tva.env du poste > fichier d'equipe >
defaut du serveur. Voir README.md.
"""


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if arg == "doctor":
        report = tva_doctor()
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
