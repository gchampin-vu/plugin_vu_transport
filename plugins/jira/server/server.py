#!/usr/bin/env python3
"""
MCP jira : interrogation et ecriture bornee sur DEUX instances Atlassian.

Trois usages :
    python server.py            # mode serveur MCP (stdio) - lance par Claude Code
    python server.py doctor     # diagnostic de configuration et de connexion
    python server.py --help

MULTI-TENANT, et c'est la raison d'etre de ce connecteur. Vente-unique parle a
deux instances Jira Cloud qui n'ont RIEN en commun :

    PROJET  https://vuproject.atlassian.net   le metier - SUPPLY, VUD, ...
    WF      https://webfacto.atlassian.net    la WebFacto - le developpement

Deux jeux d'identifiants, deux referentiels de projets, deux numerotations de
tickets. Chaque outil prend donc un `tenant`, et le rendu porte toujours la
colonne `tenant` : sans elle, deux cles de meme numero venues des deux
instances sont indiscernables.

DEUX SURFACES, DEUX LISTES BLANCHES. Le connecteur a ete en lecture seule
jusqu'au 2026-09-02 ; il ecrit depuis. Ce qui n'a PAS change, c'est le principe :
aucun appel n'est emis vers un chemin qui n'est pas nomme dans un fichier
versionne a cote de ce serveur.

    rest_get_paths.json     les chemins GET. Lecture.
    rest_write_paths.json   les chemins d'ecriture, avec leur VERBE. Quatre
                            gestes, et quatre seulement : creer un ticket,
                            mettre a jour ses champs, ajouter un commentaire,
                            franchir une transition.

Le verbe fait partie de l'autorisation : PUT /issue/{k} met a jour, DELETE
/issue/{k} detruit - meme chemin, deux gestes sans rapport. AUCUN DELETE N'EST
EXPOSE, ni sur un ticket ni sur un commentaire, et ce n'est pas un oubli : un
ticket qui n'a pas lieu d'etre se ferme par une transition, ce qui garde la
trace. La liste des gestes volontairement absents, et leur pourquoi, sont dans
la section non_exposes de rest_write_paths.json.

TROIS GARDE-FOUS SUR L'ECRITURE, parce qu'un ticket faux dans JIRA est vu par
toute l'equipe et declenche des automatisations :

  1. **Rien ne part sans `confirmer=True`.** Un appel sans confirmation ne
     touche pas Jira : il affiche le corps EXACT qui serait envoye, l'instance
     visee et le compte sous lequel l'action sera tracee. C'est la regle « le
     geste est humain » du cerveau d'equipe, rendue verifiable.
  2. **Aucune ecriture n'est rejouee.** Le serveur rejoue un GET sur une
     coupure reseau ; il ne rejoue JAMAIS un POST. Un POST /issue rejoue apres
     un delai d'attente, c'est un doublon dans le referentiel, et le connecteur
     ne peut pas savoir si le premier appel a abouti. Il le dit, et laisse
     verifier.
  3. **Rien n'est devine.** Un type de ticket est resolu sur l'instance avant
     l'appel (jira_types_ticket), une transition est resolue sur les
     transitions reellement disponibles pour CE ticket, une personne est
     resolue en accountId. Un nom approche est REFUSE avec la liste des valeurs
     reelles - il n'est jamais remplace par le plus proche.

La quatrieme requete non-GET du serveur n'a rien a voir avec ces gestes : c'est
le renouvellement d'un jeton d'acces OAuth aupres de
https://auth.atlassian.com/oauth/token, si le tenant est en mode `oauth`. Elle
ne touche aucune donnee Jira - voir _oauth_access_token.

QUI SIGNE L'ECRITURE. Avec les identifiants d'equipe, c'est le compte de
SERVICE qui apparait dans l'historique du ticket, pas la personne. Acceptable
pour lire, discutable pour ecrire : chaque outil d'ecriture affiche donc le
compte tracé avant de confirmer, et rappelle qu'un jeton nominatif pose sur le
poste passe devant celui de l'equipe. Voir _credential.

AUTHENTIFICATION, TROIS MODES, du plus simple au plus autonome :

    basic   (defaut)  courriel + jeton d'API personnel. Deux lignes a poser,
                      rien a inscrire cote Atlassian. Le jeton se cree a la
                      main sur id.atlassian.com : ATLASSIAN NE PUBLIE AUCUNE
                      API POUR EN CREER UN, c'est une action d'interface, et
                      aucun connecteur ne peut la contourner.
    bearer            un jeton d'acces deja obtenu ailleurs, envoye tel quel.
    oauth             client_id + client_secret + refresh_token. Le serveur
                      renouvelle alors le jeton d'acces TOUT SEUL, avant
                      expiration, et suit la rotation du refresh token. C'est
                      le seul mode ou plus rien n'expire a la main - il demande
                      en revanche une application inscrite cote Atlassian,
                      donc un cadrage Webfacto.

CONFIGURATION, EN DEUX COUCHES. Ce qui est identique sur tous les postes -
liste des tenants, racines de site, plafonds - vit dans le fichier partage
08_ENGINE/04_mcp/00_config/jira.shared.env. Ce qui est propre a une personne
vit dans jira.env, hors du vault. Ordre de priorite : configuration du plugin >
jira.env du poste > fichier d'equipe > defaut du serveur.

**LES IDENTIFIANTS SONT DES IDENTIFIANTS D'EQUIPE.** Decision du
2026-09-01 : comme chez shiptify et yooz, le couple courriel + jeton vit dans
le fichier partage 08_ENGINE/04_mcp/00_config/jira.shared.env. C'est un compte
de SERVICE, pas le compte d'une personne, et le faire ressaisir vingt-six fois
n'ajouterait aucune protection - seulement vingt-six mises en service qui
echouent et une rotation impossible a propager.

Ce que ca implique, et qu'il faut assumer : cette bibliotheque est lisible par
toute l'equipe L&T, donc le jeton l'est aussi. Un jeton Jira autorise
l'ECRITURE, meme si ce serveur ne s'en sert pas : le compte de service doit
donc etre provisionne avec les droits les plus faibles qui repondent, et les
actions faites avec lui seront tracees sous SON nom, pas sous celui de la
personne. Le jour ou un acces doit etre nominatif - audit, prestataire externe,
tracabilite d'une action - la reponse est la configuration du plugin sur le
poste, qui passe DEVANT le fichier d'equipe (voir _env), pas le retrait de la
ligne.

Deux secrets restent LOCAUX, et pour une raison technique et non de politique :
_CLIENT_SECRET et _REFRESH_TOKEN du mode oauth. Atlassian fait TOURNER le
refresh token a chaque renouvellement et invalide le precedent : deux postes
qui le partagent se le cassent mutuellement, le second appel echouant
definitivement. Un refresh token n'a qu'un detenteur, par construction.
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

HERE = pathlib.Path(__file__).resolve().parent

# Les deux instances de Vente-unique. Un tenant = une instance = un jeu
# d'identifiants. Les alias en langage courant vivent dans contexte_jira.json.
DEFAULT_TENANTS: dict[str, str] = {
    "PROJET": "https://vuproject.atlassian.net",
    "WF": "https://webfacto.atlassian.net",
}
DEFAULT_TENANT_ORDER = "PROJET,WF"

# Racine de l'API plateforme. La v3 est celle qui rend les descriptions en ADF.
API_ROOT = "/rest/api/3"

# Racine passerelle, utilisee en mode oauth : un jeton OAuth ne s'envoie pas au
# site, il s'envoie a api.atlassian.com avec le cloudId de l'instance.
GATEWAY = "https://api.atlassian.com/ex/jira"
OAUTH_TOKEN_URL = "https://auth.atlassian.com/oauth/token"

# Plafond impose par l'API de recherche : maxResults ne depasse pas 100.
PAGE_SIZE_MAX = 100

DEFAULT_TIMEOUT_S = 60.0
# Garde-fou de pagination, PAR TENANT : 50 pages de 100 = 5 000 tickets.
DEFAULT_MAX_PAGES = 50

# Plafond de caracteres rendus dans la conversation par un outil de liste.
RENDER_MAX_CHARS = 24_000

# Plafond de caracteres pour une description ou un commentaire rendu en texte.
TEXT_MAX_CHARS = 6_000

RETRY_ATTEMPTS = 3
RETRY_STATUSES = (429, 500, 502, 503, 504)

# Projection par defaut. Sans projection, un ticket Jira rend TOUS ses champs,
# personnalises compris : mesure sur SUPPLY, 40 a 70 Ko par ticket, dont
# l'essentiel est vide. La projection courte tient en 200 caracteres par ligne.
ISSUE_FIELDS_BRIEF = (
    "summary,status,issuetype,priority,assignee,reporter,"
    "created,updated,resolutiondate,duedate,parent,labels,resolution"
)

# Colonnes numeriques du cache : sans ce typage, AVG() moyenne des chaines.
NUMERIC_COLUMNS = {"age_jours", "jours_depuis_maj", "commentaires"}

CONTEXTE_FILE = HERE / "contexte_jira.json"
SPEC_FILE = HERE / "rest_get_paths.json"
WRITE_SPEC_FILE = HERE / "rest_write_paths.json"

# Les verbes d'ecriture que le serveur sait emettre. PATCH et DELETE n'y sont
# pas : rien dans rest_write_paths.json ne les demande, et une methode qu'on
# n'emet pas est une methode qui ne peut pas fuir par une faute de frappe.
WRITE_METHODS = ("POST", "PUT")

# Dossiers synchronises : un secret n'y vit pas.
SYNCED_MARKERS = ("/onedrive", "cafom", "sharepoint", "dropbox", "google drive")

# L'ordre de tri accepte par les outils, traduit en clause ORDER BY.
ORDRES = {
    "recent": "updated DESC",
    "maj": "updated DESC",
    "ancien": "updated ASC",
    "cree": "created DESC",
    "creation": "created DESC",
    "plus ancien": "created ASC",
    "cle": "key ASC",
    "priorite": "priority DESC",
    "echeance": "duedate ASC",
    "resolution": "resolutiondate DESC",
}


class ConfigError(RuntimeError):
    pass


class JiraError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Liste blanche des chemins GET
# --------------------------------------------------------------------------

_SPEC: dict[str, Any] | None = None


def _spec() -> dict[str, Any]:
    global _SPEC
    if _SPEC is None:
        try:
            _SPEC = json.loads(SPEC_FILE.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(
                f"Liste des chemins GET introuvable ({SPEC_FILE}) : {exc}. Ce "
                "fichier fait partie du serveur, il est versionne a cote de "
                "server.py."
            ) from exc
    return _SPEC


def _get_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Les gabarits de chemin de la liste blanche, compiles une fois."""
    out: list[tuple[str, re.Pattern[str]]] = []
    for template in _spec()["paths"]:
        body = re.escape(template)
        body = re.sub(r"\\\{[a-zA-Z0-9_]+\\\}", r"[^/]+", body)
        out.append((template, re.compile(r"^" + body + r"$")))
    # Le plus specifique d'abord : /issue/{k}/comment avant /issue/{k}.
    out.sort(key=lambda pair: -len(pair[0]))
    return out


_PATTERNS: list[tuple[str, re.Pattern[str]]] | None = None


def _clean_api_path(path: str) -> str:
    """Normalise un chemin d'API : sans URL, sans racine, sans filtres.

    Partage par les deux listes blanches. Une normalisation ecrite deux fois
    est une normalisation qui diverge, et ici elle decide de ce qui passe : un
    chemin d'ecriture qui echapperait au nettoyage echapperait a la liste.
    """
    clean = (path or "").strip()
    if not clean:
        raise JiraError("Chemin vide. Appelle jira_list_paths pour la liste.")
    if clean.startswith("http://") or clean.startswith("https://"):
        raise JiraError(
            "Passe un chemin d'API, pas une URL complete : le serveur ajoute "
            f"lui-meme la racine du tenant et {API_ROOT}. Exemple : /myself."
        )
    if not clean.startswith("/"):
        clean = "/" + clean
    # On accepte qu'on lui passe la racine, par confort, et on la retire.
    if clean.startswith(API_ROOT):
        clean = clean[len(API_ROOT):] or "/"
    if "?" in clean:
        raise JiraError(
            "Ne mets pas les filtres dans le chemin : passe-les dans "
            "query_json. Le serveur les encode lui-meme."
        )
    return clean


def _check_get_path(path: str) -> tuple[str, str]:
    """Valide un chemin contre la liste blanche des GET. Rend (chemin, gabarit).

    Un chemin inconnu est refuse. Cet outil-ci n'emet jamais autre chose qu'un
    GET : l'ecriture a sa propre liste et ses propres outils, elle ne passe
    jamais par l'echappatoire generique.
    """
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = _get_patterns()
    clean = _clean_api_path(path)
    blocked = _spec().get("non_exposes", {})
    if clean in blocked:
        raise JiraError(f"Chemin volontairement non expose : {blocked[clean]}")
    for template, pattern in _PATTERNS:
        if pattern.match(clean):
            return clean, template
    raise JiraError(
        f"Chemin refuse : {clean}. Il n'est pas dans la liste blanche des "
        "chemins GET de ce connecteur. jira_list_paths donne les chemins "
        "atteignables. Si un chemin manque et qu'il est en lecture, il "
        "s'ajoute dans rest_get_paths.json - ce n'est pas une limite d'API."
    )


# --------------------------------------------------------------------------
# Liste blanche des chemins d'ECRITURE
# --------------------------------------------------------------------------
#
# Meme mecanique que pour la lecture, avec une difference qui decide de tout :
# la cle porte le VERBE. PUT /issue/{k} met a jour, DELETE /issue/{k} detruit -
# le meme chemin, deux gestes sans rapport. Autoriser un chemin sans son verbe,
# ce serait autoriser la suppression en croyant autoriser la mise a jour.
#
# Pas de fichier, pas d'ecriture : si rest_write_paths.json manque, le serveur
# refuse d'ecrire plutot que de se replier sur une liste ecrite dans le code.
# Une liste blanche qui vit dans le code est une liste blanche qu'on ne relit
# pas.

_WRITE_SPEC: dict[str, Any] | None = None


def _write_spec() -> dict[str, Any]:
    global _WRITE_SPEC
    if _WRITE_SPEC is None:
        try:
            _WRITE_SPEC = json.loads(WRITE_SPEC_FILE.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(
                f"Liste des chemins d'ecriture introuvable ({WRITE_SPEC_FILE}) : "
                f"{exc}. Ce fichier fait partie du serveur, il est versionne a "
                "cote de server.py. Sans lui, le connecteur reste en lecture "
                "seule - c'est le repli voulu."
            ) from exc
    return _WRITE_SPEC


def _path_pattern(template: str) -> re.Pattern[str]:
    """Compile un gabarit de chemin : {ceci} vaut un segment, et un seul.

    `[^/]+` et non `.+` : sans l'exclusion du separateur, le gabarit
    /issue/{k} accepterait /issue/X/attachments, c'est-a-dire exactement ce
    que la liste blanche est censee arreter.
    """
    morceaux = [re.escape(m) for m in re.split(r"\{[a-zA-Z0-9_]+\}", template)]
    return re.compile("^" + "[^/]+".join(morceaux) + "$")


_WRITE_PATTERNS: list[tuple[str, str, re.Pattern[str]]] | None = None
_WRITE_BLOCKED: list[tuple[str, str, re.Pattern[str]]] | None = None


def _compile_write(entries: Any) -> list[tuple[str, str, re.Pattern[str]]]:
    """Compile des cles « VERBE /chemin » en (verbe, gabarit, motif)."""
    out: list[tuple[str, str, re.Pattern[str]]] = []
    for entry in entries:
        if entry.startswith("_"):
            continue
        verbe, _, template = entry.partition(" ")
        verbe = verbe.strip().upper()
        template = template.strip()
        if not verbe or not template.startswith("/"):
            raise ConfigError(
                f"Entree de liste blanche d'ecriture mal formee : « {entry} ». "
                "La forme attendue est « VERBE /chemin » - le verbe fait "
                "partie de l'autorisation, il n'est pas optionnel."
            )
        out.append((verbe, template, _path_pattern(template)))
    # Le plus specifique d'abord : /issue/{k}/comment avant /issue/{k}.
    out.sort(key=lambda triple: -len(triple[1]))
    return out


def _check_write_path(method: str, path: str) -> tuple[str, str, dict[str, Any]]:
    """Valide un couple (verbe, chemin) d'ecriture. Rend (chemin, cle, fiche).

    Point de passage unique de toute ecriture, y compris depuis les outils
    dedies : un outil qui se tromperait de chemin est arrete ici.
    """
    global _WRITE_PATTERNS, _WRITE_BLOCKED
    if _WRITE_PATTERNS is None:
        _WRITE_PATTERNS = _compile_write(_write_spec()["paths"])
    if _WRITE_BLOCKED is None:
        _WRITE_BLOCKED = _compile_write(_write_spec().get("non_exposes") or {})
    verbe = (method or "").strip().upper()
    clean = _clean_api_path(path)

    # D'abord le refus documente : il porte une raison, et une raison vaut
    # mieux qu'un « chemin inconnu » sur un geste que quelqu'un a deja tranche.
    for bverbe, btemplate, bpattern in _WRITE_BLOCKED:
        if bverbe == verbe and bpattern.match(clean):
            raison = (_write_spec().get("non_exposes") or {}).get(
                f"{bverbe} {btemplate}", ""
            )
            raise JiraError(
                f"Geste volontairement non expose : {verbe} {clean}. {raison}\n"
                "Ce n'est pas une limite de l'API Atlassian, c'est une "
                "decision : elle se rediscute, et elle s'inscrit dans "
                "rest_write_paths.json."
            )
    if verbe not in WRITE_METHODS:
        raise JiraError(
            f"Verbe refuse : {verbe or '(vide)'}. Ce serveur n'emet que "
            f"{', '.join(WRITE_METHODS)} en ecriture, et GET en lecture. "
            "DELETE et PATCH ne sont pas implementes du tout - une methode "
            "qu'on n'emet pas ne peut pas fuir par une faute de frappe."
        )
    for wverbe, wtemplate, wpattern in _WRITE_PATTERNS:
        if wverbe == verbe and wpattern.match(clean):
            cle = f"{wverbe} {wtemplate}"
            return clean, cle, _write_spec()["paths"][cle]
    # Le chemin existe peut-etre, mais pas avec ce verbe : le dire, parce que
    # les deux erreurs ne se corrigent pas de la meme facon.
    autres = sorted({v for v, _t, p in _WRITE_PATTERNS if p.match(clean)})
    if autres:
        raise JiraError(
            f"Verbe refuse sur ce chemin : {verbe} {clean}. Ce chemin n'est "
            f"autorise qu'en {', '.join(autres)}. Le verbe fait partie de "
            "l'autorisation."
        )
    raise JiraError(
        f"Chemin d'ecriture refuse : {verbe} {clean}. Il n'est pas dans "
        "rest_write_paths.json. jira_list_paths(ecriture=True) donne les "
        "gestes exposes, et ceux qui ne le sont pas avec leur pourquoi."
    )


# --------------------------------------------------------------------------
# Configuration : le fichier jira.env
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
    un conteneur d'application : ses processus enfants heritent d'une vue
    VIRTUALISEE de %LOCALAPPDATA%. Un fichier de configuration pose la peut
    donc etre invisible pour ce serveur, sans aucune erreur. D'ou la racine
    locale de ce connecteur : ~/.jira-mcp, dans le profil utilisateur, qui
    n'est pas virtualise. Lecon mesuree en empaquetant Yooz le 2026-08-27.
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
    return pathlib.Path.home() / ".jira-mcp"


def _candidate_env_files() -> list[pathlib.Path]:
    """Emplacements du fichier de configuration, essayes dans l'ordre.

    Le fichier s'appelle `jira.env`, pas `.env` : c'est la lecon de Yooz, ou un
    fichier nomme `.env` a ete ignore en silence par un connecteur qui
    cherchait `yooz.env`. Le nom du connecteur est dans le nom du fichier, et
    il est le meme a tous les emplacements.
    """
    explicit = (os.environ.get("JIRA_ENV_FILE") or "").strip()
    if explicit:
        # **Un chemin explicite est EXCLUSIF**, comme pour le fichier d'equipe.
        # Le mettre simplement en tete de liste laissait la recherche continuer
        # sous le profil utilisateur : pointer un fichier precis ne desactivait
        # donc pas le fichier reel, il le mettait en second. Consequence
        # mesuree le 2026-09-01, et elle n'est pas theorique - la suite de
        # controles hors-ligne pointe un fichier inexistant pour simuler un
        # poste sans configuration, et elle a fait un VRAI appel a
        # vuproject.atlassian.net des que ~/.jira-mcp/jira.env a existe. Un
        # controle hors-ligne qui sort sur le reseau ne controle plus rien.
        # Le meme piege vaut en exploitation : qui pointe un fichier de recette
        # attend ce fichier, pas un repli silencieux sur la production.
        return [pathlib.Path(explicit)]
    out: list[pathlib.Path] = []
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        out.append(pathlib.Path(plugin_data) / "jira.env")
    out.append(_local_root() / "jira.env")
    # Voisin du script : refuse s'il est synchronise, mais on le regarde quand
    # meme pour pouvoir le DIRE plutot que rester muet.
    out.append(HERE / "jira.env")
    return out


def _parse_env(text: str) -> dict[str, str]:
    """Parseur .env minimal : KEY=VALUE, # en commentaire, guillemets optionnels.

    Un jeton d'API peut contenir n'importe quoi (=, %, #, +). La valeur n'est
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
    par l'environnement : JIRA_PROJET_TOKEN=${user_config.projet_token}. Si le
    champ n'est pas renseigne, la substitution pose une variable VIDE, et un
    setdefault la considererait comme renseignee - le fichier du poste serait
    alors ignore en silence.

    Lu en utf-8-sig : un BOM UTF-8 non consomme colle trois octets invisibles
    devant le premier nom de variable, qui devient introuvable. C'est ce qui a
    fait perdre une cle chez Yooz.
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
                f"synchronise ({path}) et n'a PAS ete lu : il porterait un "
                "jeton d'API nominatif sur le drive partage de l'equipe. "
                f"Deplace-le vers {_local_root() / 'jira.env'}, ou pointe un "
                "emplacement local avec JIRA_ENV_FILE."
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
# Il porte ce qui est identique d'un poste a l'autre : la liste des tenants, la
# racine de chaque site, les plafonds de pagination - et, decision du
# 2026-09-01, LE COURRIEL ET LE JETON DU COMPTE DE SERVICE. Un reglage ressaisi
# vingt-six fois, ce sont vingt-six occasions de diverger et une correction qui
# ne se propage jamais. C'est ce qui a fait rater la mise en service de Yooz, ou
# l'applicationId etait confondu avec le client_id sur un poste et juste sur un
# autre.
#
# LES IDENTIFIANTS SONT ADMIS ICI, comme chez shiptify depuis le 2026-08-28,
# parce que ce sont ceux d'un compte de SERVICE : ils n'identifient personne,
# ils valent pour l'equipe. Ce que ca implique et qu'il faut assumer : la
# bibliotheque est lisible par toute l'equipe L&T, donc le jeton l'est aussi, et
# un jeton Jira autorise l'ECRITURE. Deux consequences a tenir cote
# provisionnement du compte, pas cote code : les droits les plus faibles qui
# repondent, et la conscience que toute action faite avec ce jeton sera tracee
# sous SON nom. Le jour ou un acces doit etre nominatif, la reponse est la
# configuration du plugin sur le poste, qui passe DEVANT ce fichier - pas le
# retrait de la ligne.
#
# DEUX SECRETS RESTENT REFUSES ICI, et c'est technique et non politique :
# _CLIENT_SECRET et _REFRESH_TOKEN du mode oauth. Atlassian fait TOURNER le
# refresh token a chaque renouvellement et invalide le precedent : deux postes
# qui le partagent se le cassent mutuellement, et le second echoue
# definitivement - il faudrait refaire le consentement a la main. Un refresh
# token n'a qu'un detenteur, par construction.
#
# Une valeur lue ici n'est JAMAIS versee dans os.environ : elle reste dans le
# cache ci-dessous, et c'est _env qui va l'y chercher en dernier recours.

SHARED_FILE_NAME = "jira.shared.env"
ENGINE_DIR_NAME = "08_ENGINE"
SHARED_SUBPATH = ("04_mcp", "00_config")

SHARED_ALLOWED_GLOBALS = frozenset(
    {
        "JIRA_TENANTS",
        "JIRA_PAGE_SIZE",
        "JIRA_MAX_PAGES",
        "JIRA_TIMEOUT_S",
        "JIRA_DEFAULT_TENANT",
        # Le couple SANS prefixe de tenant, admis ici depuis le 2026-09-02.
        # Il vaut pour TOUTES les instances : c'est la reponse au cas reel, ou
        # le meme compte de service repond sur les deux sites et ou le couple
        # etait donc ecrit deux fois, a deux endroits qui pouvaient diverger.
        # Une ligne par instance reste possible et passe devant.
        "JIRA_EMAIL",
        "JIRA_TOKEN",
        "JIRA_AUTH",
        # Le repli d'un tenant sur les identifiants du tenant par defaut.
        # Se coupe ici pour toute l'equipe, ou sur un poste, avec la valeur 0.
        "JIRA_CREDS_FALLBACK",
    }
)

# Par tenant, ces suffixes-la. Le nom du tenant n'est pas connu d'avance :
# JIRA_TENANTS peut lui-meme venir du fichier d'equipe, et une troisieme
# instance s'ajoutera sans toucher a ce code.
#
# EMAIL et TOKEN y sont, depuis le 2026-09-01 : ce sont les identifiants du
# compte de service de l'equipe. La liste blanche n'est donc PAS une barriere
# anti-secret - elle laisse passer le jeton - c'est un garde-fou contre la faute
# de frappe : une variable mal orthographiee posee dans le fichier partage
# serait sinon ignoree en silence, et on chercherait longtemps pourquoi le
# reglage d'equipe ne prend pas.
SHARED_TENANT_SUFFIXES = (
    "BASE_URL",
    "LABEL",
    "AUTH",
    "CLOUD_ID",
    "CLIENT_ID",
    "EMAIL",
    "TOKEN",
)
_SHARED_TENANT_RE = re.compile(
    r"^JIRA_([A-Z0-9_]+?)_(" + "|".join(SHARED_TENANT_SUFFIXES) + r")$"
)

# Refusees a la lecture du fichier partage, et signalees : un chemin valable sur
# un poste n'existe pas sur les vingt-cinq autres.
#
# JIRA_EMAIL et JIRA_TOKEN, sans prefixe de tenant, ETAIENT ici - refuses comme
# « ambigus des qu'il y a deux instances ». Ils en sont sortis le 2026-09-02,
# parce que l'ambiguite etait theorique et la duplication reelle : le meme
# compte de service repondait sur les deux sites, et le couple etait ecrit deux
# fois dans le meme fichier. Deux copies d'un secret, c'est une rotation qui en
# oublie une. Le couple sans prefixe s'applique donc a TOUTES les instances, et
# une ligne prefixee le surcharge instance par instance.
SHARED_LOCAL_ONLY = frozenset(
    {
        "JIRA_EXPORT_DIR",
        "JIRA_ENV_FILE",
        "JIRA_SHARED_ENV",
        "JIRA_CACHE_DB",
        "JIRA_MCP_VENV",
        "VU_ENGINE_DIR",
    }
)
# Les deux secrets du mode oauth restent refuses dans le fichier partage. La
# raison est TECHNIQUE : le refresh token tourne a chaque renouvellement et
# invalide le precedent, donc deux postes qui le partagent se le cassent
# mutuellement. Il n'a qu'un detenteur possible.
_SHARED_SECRET_RE = re.compile(
    r"^JIRA_[A-Z0-9_]+_(CLIENT_SECRET|REFRESH_TOKEN|PASSWORD)$"
)

_SHARED_CACHE: dict[str, str] | None = None
_SHARED_LOADED_FROM: str = ""
_SHARED_REJECTED: list[str] = []
_SHARED_LOCAL_SEEN: list[str] = []
_SHARED_SECRETS_SEEN: list[str] = []


def _is_secret_key(name: str) -> bool:
    """Une cle dont la VALEUR ne s'affiche jamais, meme dans un diagnostic.

    Le NOM, lui, se dit : c'est ce qui permet de savoir d'ou vient la valeur
    active sans la reveler.
    """
    return bool(re.search(r"(TOKEN|SECRET|PASSWORD)$", name))


def _shared_allows(name: str) -> bool:
    if _SHARED_SECRET_RE.match(name) or name in SHARED_LOCAL_ONLY:
        return False
    if name in SHARED_ALLOWED_GLOBALS:
        return True
    return bool(_SHARED_TENANT_RE.match(name))


def _engine_roots() -> list[pathlib.Path]:
    """Racines `08_ENGINE` plausibles, dans l'ordre de preference.

    Le plugin est installe depuis `08_ENGINE/03_plugins/...`, mais Claude Code
    en fait une copie dans `~/.claude/plugins/`. On ne peut donc pas se
    contenter de remonter depuis le code : on remonte quand meme (installation
    directe depuis la bibliotheque), et on complete par le profil utilisateur,
    ou OneDrive synchronise la bibliotheque d'equipe. Les motifs sont des globs
    et non des chemins en dur, parce que le point de montage differe entre
    Windows (CAFOM/) et macOS (Library/CloudStorage/).
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
    patterns = (
        "CAFOM/*/" + ENGINE_DIR_NAME,
        "*/CAFOM/*/" + ENGINE_DIR_NAME,
        "Library/CloudStorage/*/" + ENGINE_DIR_NAME,
        "Library/CloudStorage/*/*/" + ENGINE_DIR_NAME,
    )
    for pattern in patterns:
        try:
            for hit in sorted(home.glob(pattern)):
                if hit.is_dir():
                    add(hit)
        except OSError:
            continue
    return out


def _shared_env_candidates() -> list[pathlib.Path]:
    """Emplacements du fichier d'equipe essayes, dans l'ordre.

    **Un chemin explicite est EXCLUSIF.** Pointer un fichier precis doit
    DESACTIVER la recherche, pas le mettre en tete : sinon un fichier de
    recette inexistant retombe en silence sur la configuration de production,
    et la suite de controles hors-ligne tourne avec les vrais identifiants sans
    que personne ne le voie. Le piege a ete mesure chez peripass.
    """
    explicit = (os.environ.get("JIRA_SHARED_ENV") or "").strip()
    if explicit:
        return [pathlib.Path(explicit)]
    return [
        root.joinpath(*SHARED_SUBPATH, SHARED_FILE_NAME) for root in _engine_roots()
    ]


def _load_shared_env() -> dict[str, str]:
    """Lit le premier fichier d'equipe trouve, filtre par la liste blanche.

    Ne leve jamais : un fichier d'equipe absent, illisible ou mal rempli ne
    doit pas empecher le connecteur de tourner sur la configuration du poste.
    Ce qui a ete refuse est garde de cote pour que setup_status le dise - un
    reglage ignore en silence, c'est une demi-journee de recherche.
    """
    global _SHARED_CACHE, _SHARED_LOADED_FROM
    global _SHARED_REJECTED, _SHARED_LOCAL_SEEN, _SHARED_SECRETS_SEEN
    if _SHARED_CACHE is not None:
        return _SHARED_CACHE
    values: dict[str, str] = {}
    rejected: list[str] = []
    local_seen: list[str] = []
    secrets_seen: list[str] = []
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
            elif _SHARED_SECRET_RE.match(upper):
                # Les secrets du mode oauth : refuses pour une raison
                # mecanique, pas de politique. JIRA_TOKEN et JIRA_EMAIL sans
                # prefixe de tenant, eux, ne sont pas des secrets refuses mais
                # des cles AMBIGUES - ils tombent donc dans SHARED_LOCAL_ONLY
                # juste en dessous, avec le bon message.
                secrets_seen.append(upper)
            elif upper in SHARED_LOCAL_ONLY:
                local_seen.append(upper)
            else:
                rejected.append(upper)
        _SHARED_LOADED_FROM = str(path)
        break
    _SHARED_REJECTED = rejected
    _SHARED_LOCAL_SEEN = local_seen
    _SHARED_SECRETS_SEEN = secrets_seen
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
    else:
        lines.append("Config d'equipe      : aucune (valeurs par defaut du serveur)")
        for path in _shared_env_candidates():
            lines.append(f"    absent  {path}")
        secrets = sorted(k for k in shared if _is_secret_key(k))
        if secrets:
            lines.append(
                "  dont secrets       : "
                + ", ".join(secrets)
                + " (valeur jamais affichee ; retenue seulement si ce poste "
                "n'en definit pas)"
            )
    if _SHARED_SECRETS_SEEN:
        lines.append("")
        lines.append(
            "  ATTENTION : le fichier d'equipe porte des secrets qui ne peuvent "
            "PAS etre partages, ils ont ete IGNORES : "
            + ", ".join(sorted(set(_SHARED_SECRETS_SEEN)))
            + "."
        )
        lines.append(
            "  Ce n'est pas une question de confiance mais de mecanique : "
            "Atlassian fait TOURNER le refresh token a chaque renouvellement et "
            "invalide le precedent. Deux postes qui le partagent se le cassent "
            "mutuellement, et le second echoue definitivement - il faut alors "
            "refaire le consentement a la main. Ces deux valeurs se posent dans "
            "/plugin ou dans le jira.env du poste, sur UN seul poste."
        )
    if _SHARED_LOCAL_SEEN:
        lines.append("")
        lines.append(
            "  ATTENTION : le fichier d'equipe porte des cles LOCALES par "
            "nature, elles ont ete ignorees : "
            + ", ".join(sorted(set(_SHARED_LOCAL_SEEN)))
            + ". Un chemin valable sur un poste n'existe pas sur les autres : "
            "ces valeurs se posent dans /plugin."
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
            "  Dans la quasi-totalite des cas c'est une faute de frappe dans le "
            "nom de la variable : compare avec jira.shared.env.example. Une cle "
            "ignoree ne produit aucune erreur ailleurs - c'est ici, et seulement "
            "ici, que ca se voit."
        )
    return lines


def _env(name: str, default: str = "") -> str:
    """La valeur retenue pour un reglage, dans un ordre de priorite fixe.

    1. l'environnement du processus - la configuration du plugin, et le
       jira.env local que _load_env_file y a deja verse : ce que CE POSTE a
       decide ;
    2. le fichier d'equipe de 08_ENGINE - ce que l'EQUIPE a decide ;
    3. la valeur par defaut du serveur.

    Le poste passe donc devant l'equipe. C'est ce qui permet de pointer une
    instance de recette, ou d'utiliser son propre jeton, sans toucher au
    fichier partage - donc sans casser les vingt-cinq autres postes.
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
        return float(_env("JIRA_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _max_pages() -> int:
    try:
        return max(1, int(_env("JIRA_MAX_PAGES") or DEFAULT_MAX_PAGES))
    except ValueError:
        return DEFAULT_MAX_PAGES


def _page_size() -> int:
    try:
        want = int(_env("JIRA_PAGE_SIZE") or PAGE_SIZE_MAX)
    except ValueError:
        return PAGE_SIZE_MAX
    return max(1, min(PAGE_SIZE_MAX, want))


def _cache_path() -> pathlib.Path:
    raw = _env("JIRA_CACHE_DB")
    return pathlib.Path(raw) if raw else _local_root() / "jira_cache.sqlite"


def _export_dir() -> pathlib.Path:
    """Ou atterrissent les CSV.

    Le reglage explicite gagne ; sinon la racine locale, qui est la meme quel
    que soit le mode de lancement. **Jamais dans la bibliotheque d'equipe** :
    un CSV depose dans un dossier synchronise part chez tout le monde, et la
    base de connaissance ne porte pas de donnees.
    """
    raw = _env("JIRA_EXPORT_DIR")
    if raw:
        return pathlib.Path(raw)
    return _local_root() / "exports"


# --------------------------------------------------------------------------
# Le contexte JIRA embarque : projets, conventions de titre, chantiers, JQL
# --------------------------------------------------------------------------
#
# Pourquoi dans le serveur et pas seulement dans la skill : une skill est lue
# une fois, en debut de session, et le modele ne la relit pas avant chaque
# appel. Le vocabulaire maison y est une intention, pas une garantie. Ici c'est
# le serveur qui traduit - « les chantiers ouverts » en JQL, « Moulins » en code
# d'agence, « en cours » en statusCategory - et il DIT dans l'entete ce qu'il a
# traduit, ce qui rend la traduction verifiable.
#
# Le cas d'ecole : personne ne dit « statusCategory = \"In Progress\" ». On dit
# « ce qui est en cours ». Et un filtre sur un libelle de statut inexistant ne
# rend pas moins de lignes, il fait ECHOUER la requete.

_CONTEXTE: dict[str, Any] | None = None
_CONTEXTE_ERROR: str = ""


def _norm(text: Any) -> str:
    """Forme comparable d'un libelle : sans accent, sans casse, sans ponctuation."""
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()


def _contexte() -> dict[str, Any]:
    """Le contexte livre avec le connecteur. Ne leve jamais.

    Un contexte absent degrade la traduction des questions en francais ; il ne
    doit pas empecher le connecteur d'interroger JIRA.
    """
    global _CONTEXTE, _CONTEXTE_ERROR
    if _CONTEXTE is None:
        try:
            _CONTEXTE = json.loads(CONTEXTE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _CONTEXTE_ERROR = f"{CONTEXTE_FILE} illisible : {exc}"
            _CONTEXTE = {}
    return _CONTEXTE


def _contexte_tenants() -> dict[str, Any]:
    table = _contexte().get("tenants") or {}
    return {k: v for k, v in table.items() if not k.startswith("_")}


# --------------------------------------------------------------------------
# Les tenants : la specificite de ce connecteur
# --------------------------------------------------------------------------

def _tenant_code_raw(raw: str) -> str:
    """Normalisation brute, sans traduction : evite la recursion des alias."""
    flat = unicodedata.normalize("NFKD", str(raw or ""))
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", "_", flat.strip().upper()).strip("_")


def _tenant_code(raw: str) -> str:
    """Normalise un code de tenant, en acceptant les noms courants.

    « webfacto », « wf », « les devs » designent la meme instance, et personne
    ne dit « PROJET » pour vuproject. Avant cette traduction, tenant='webfacto'
    echouait sur un tenant inconnu, ce qui est la mauvaise reponse a une
    question juste. Les alias vivent dans contexte_jira.json, pas en dur ici.
    """
    code = _tenant_code_raw(raw)
    if not code:
        return code
    for canonical, entry in _contexte_tenants().items():
        if not isinstance(entry, dict):
            continue
        if code == canonical.upper():
            return canonical
        for alias in entry.get("alias") or []:
            if code == _tenant_code_raw(alias):
                return canonical
    return code


class Tenant:
    __slots__ = (
        "code",
        "site",
        "label",
        "auth",
        "email",
        "token",
        "cloud_id",
        "client_id",
        "client_secret",
        "refresh_token",
        "problem",
        "email_origine",
        "token_origine",
    )

    def __init__(
        self,
        code: str,
        site: str,
        label: str = "",
        auth: str = "basic",
        email: str = "",
        token: str = "",
        cloud_id: str = "",
        client_id: str = "",
        client_secret: str = "",
        refresh_token: str = "",
        problem: str = "",
        email_origine: str = "",
        token_origine: str = "",
    ) -> None:
        self.code = code
        self.site = site
        self.label = label
        self.auth = auth
        self.email = email
        self.token = token
        self.cloud_id = cloud_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.problem = problem
        # D'ou vient chaque moitie du couple. Sert a distinguer un jeton absent
        # d'un jeton repris de l'autre instance : les deux rendent un 401.
        self.email_origine = email_origine
        self.token_origine = token_origine

    @property
    def identifiants_repris(self) -> bool:
        """Le couple vient-il, en tout ou partie, d'une autre instance ?"""
        return "REPRIS" in self.email_origine or "REPRIS" in self.token_origine

    @property
    def ready(self) -> bool:
        return not self.problem

    @property
    def api_base(self) -> str:
        """La racine a laquelle on parle.

        En mode oauth, un jeton ne s'envoie PAS au site : Atlassian le refuse.
        Il s'envoie a la passerelle api.atlassian.com, avec le cloudId de
        l'instance dans le chemin. C'est la cause n° 1 des 401 en mode OAuth.
        """
        if self.auth == "oauth":
            return f"{GATEWAY}/{self.cloud_id}{API_ROOT}"
        return f"{self.site}{API_ROOT}"

    def browse(self, key: str) -> str:
        """L'URL cliquable d'un ticket. Le site, jamais la passerelle."""
        return f"{self.site}/browse/{key}"


def _declared_tenants() -> list[str]:
    raw = _env("JIRA_TENANTS") or DEFAULT_TENANT_ORDER
    codes = [_tenant_code(c) for c in raw.split(",")]
    return [c for c in codes if c]


# --------------------------------------------------------------------------
# Les identifiants d'un tenant : trois etages, et un repli assume
# --------------------------------------------------------------------------
#
# Le probleme reel, constate le 2026-09-02 sur jira.shared.env : le meme compte
# de service repond sur vuproject ET sur webfacto, et le couple courriel + jeton
# y etait donc ecrit DEUX FOIS, prefixe une fois par instance. Deux copies d'un
# secret, ce n'est pas deux fois plus sur - c'est une rotation qui en oublie
# une, et un poste qui marche sur une instance et pas sur l'autre sans que
# personne ne comprenne pourquoi.
#
# D'ou la chaine ci-dessous, essayee dans cet ordre pour chaque champ :
#
#   1. JIRA_<TENANT>_EMAIL / _TOKEN   l'instance decide pour elle-meme
#   2. JIRA_EMAIL / JIRA_TOKEN        un seul couple pour TOUTES les instances
#   3. JIRA_<DEFAUT>_EMAIL / _TOKEN   le repli : le tenant par defaut fait foi
#
# Chaque etage traverse lui-meme les trois niveaux de _env : configuration du
# plugin, puis jira.env du poste, puis fichier d'equipe. Un jeton NOMINATIF pose
# sur le poste bat donc toujours le compte de service de l'equipe, y compris
# instance par instance - c'est la sortie pour tracer une ecriture sous son
# propre nom, et elle est verifiee dans test_offline.py.
#
# LE REPLI SE VOIT. Un jeton emis pour vuproject n'a aucune raison d'etre
# accepte par webfacto : un compte doit avoir ete invite sur chaque instance.
# Reprendre le jeton du tenant par defaut est donc un PARI, pas une garantie -
# il paie quand c'est un compte de service present sur les deux sites, il rend
# 401 sinon. Le connecteur ne le fait donc jamais en silence : l'origine de
# chaque champ est portee par le Tenant, dite par jira_setup_status et
# jira_doctor, et rappelee dans le message du 401. Un 401 dont on ignore quel
# jeton a ete envoye est un 401 qu'on ne corrige pas.
#
# POUR LE COUPER : JIRA_CREDS_FALLBACK=0, sur le poste ou pour l'equipe. Le
# tenant non renseigne redevient alors proprement « non configure » plutot que
# de rendre un 401 - c'est ce qu'on veut quand on sait que le compte n'existe
# pas sur la seconde instance.

FAUX = {"0", "non", "no", "false", "off", "aucun"}


def _creds_fallback() -> bool:
    """Le repli d'un tenant sur les identifiants du tenant par defaut est-il actif ?"""
    return (_env("JIRA_CREDS_FALLBACK") or "1").strip().lower() not in FAUX


def _default_tenant_code() -> str:
    """Le tenant qui sert de repli. JIRA_DEFAULT_TENANT, sinon le premier declare.

    « Premier declare » et non « PROJET » en dur : une troisieme instance, ou un
    poste qui n'a acces qu'a webfacto, ne doivent pas dependre d'un nom ecrit
    dans le code.
    """
    explicite = _tenant_code(_env("JIRA_DEFAULT_TENANT"))
    if explicite:
        return explicite
    declares = _declared_tenants()
    return declares[0] if declares else ""


def _credential(code: str, suffixe: str) -> tuple[str, str]:
    """Un champ d'identification d'un tenant. Rend (valeur, origine lisible).

    L'origine n'est pas cosmetique : c'est ce qui distingue « le jeton est
    absent » de « le jeton est celui de l'autre instance, repris par defaut »,
    et ces deux situations rendent le meme 401.
    """
    propre = _env(f"JIRA_{code}_{suffixe}")
    if propre:
        return propre, f"propre a {code}"
    commun = _env(f"JIRA_{suffixe}")
    if commun:
        return commun, f"commun a toutes les instances (JIRA_{suffixe})"
    defaut = _default_tenant_code()
    if _creds_fallback() and defaut and defaut != code:
        herite = _env(f"JIRA_{defaut}_{suffixe}")
        if herite:
            return herite, f"REPRIS de {defaut}, faute de valeur propre a {code}"
    return "", "(aucune)"


def _describe_tenant(code: str) -> Tenant:
    """Resout la configuration d'un tenant. Ne leve pas : il porte son probleme."""
    ctx = _contexte_tenants().get(code) or {}
    site = _env(f"JIRA_{code}_BASE_URL") or DEFAULT_TENANTS.get(code) or str(
        ctx.get("site") or ""
    )
    label = _env(f"JIRA_{code}_LABEL") or str(ctx.get("label") or "")
    # JIRA_AUTH sans prefixe vaut pour toutes les instances : deux sites du
    # meme groupe s'authentifient de la meme facon dans le cas courant.
    auth = (
        _env(f"JIRA_{code}_AUTH") or _env("JIRA_AUTH") or "basic"
    ).strip().lower()
    cloud_id = _env(f"JIRA_{code}_CLOUD_ID") or str(ctx.get("cloud_id") or "")
    client_id = _env(f"JIRA_{code}_CLIENT_ID")
    client_secret = _env(f"JIRA_{code}_CLIENT_SECRET")
    refresh = _env(f"JIRA_{code}_REFRESH_TOKEN")

    # Le couple courriel + jeton, par la chaine a trois etages de _credential :
    # valeur propre au tenant, sinon couple commun, sinon celui du tenant par
    # defaut. Les deux moities sont resolues SEPAREMENT : un poste peut avoir
    # son propre courriel sur une instance et reprendre le jeton commun.
    #
    # Le mode oauth est HORS de cette chaine, et ce n'est pas un oubli : ses
    # deux secrets tournent a chaque renouvellement et n'ont qu'un detenteur
    # possible. Les reprendre d'une autre instance les casserait des deux
    # cotes. Un tenant en oauth se configure entierement pour lui-meme.
    if auth == "oauth":
        email, email_origine = _env(f"JIRA_{code}_EMAIL"), f"propre a {code}"
        token, token_origine = _env(f"JIRA_{code}_TOKEN"), f"propre a {code}"
    else:
        email, email_origine = _credential(code, "EMAIL")
        token, token_origine = _credential(code, "TOKEN")

    problem = ""
    if auth not in {"basic", "bearer", "oauth"}:
        problem = (
            f"mode d'authentification inconnu : « {auth} ». Valeurs permises : "
            "basic (courriel + jeton d'API, le plus simple), bearer (jeton "
            "d'acces fourni), oauth (renouvellement automatique)."
        )
    elif not site:
        problem = (
            f"aucune racine de site connue pour {code}. Renseigne "
            f"JIRA_{code}_BASE_URL, par exemple https://exemple.atlassian.net."
        )
    elif auth == "basic" and not email:
        problem = (
            f"courriel absent. En mode basic, Jira Cloud attend le COUPLE "
            "courriel + jeton d'API : le jeton seul rend 401. Trois facons de "
            f"le poser, de la plus precise a la plus large : JIRA_{code}_EMAIL "
            "pour cette instance seule, JIRA_EMAIL pour toutes les instances, "
            f"ou celui de {_default_tenant_code() or 'defaut'} "
            "qui serait repris par defaut"
            + ("" if _creds_fallback() else " - repli desactive par JIRA_CREDS_FALLBACK=0")
            + "."
        )
    elif auth == "basic" and not token:
        problem = (
            f"jeton d'API absent (JIRA_{code}_TOKEN pour cette instance, ou "
            "JIRA_TOKEN pour toutes)"
        )
    elif auth == "bearer" and not token:
        problem = (
            f"jeton d'acces absent (JIRA_{code}_TOKEN pour cette instance, ou "
            "JIRA_TOKEN pour toutes)"
        )
    elif auth == "oauth" and not (client_id and client_secret and refresh):
        manque = [
            nom
            for nom, val in (
                (f"JIRA_{code}_CLIENT_ID", client_id),
                (f"JIRA_{code}_CLIENT_SECRET", client_secret),
                (f"JIRA_{code}_REFRESH_TOKEN", refresh),
            )
            if not val
        ]
        problem = (
            "mode oauth incomplet, il manque : "
            + ", ".join(manque)
            + ". Le mode oauth suppose une application inscrite cote Atlassian "
            "(console developpeur) et un premier consentement obtenu a la "
            "main. Tant que ce n'est pas fait, reste en mode basic."
        )
    elif auth == "oauth" and not cloud_id:
        problem = (
            f"mode oauth sans cloudId (JIRA_{code}_CLOUD_ID). Un jeton OAuth "
            "s'envoie a la passerelle api.atlassian.com, qui a besoin de "
            "l'identifiant d'instance. Il se lit sur "
            "https://api.atlassian.com/oauth/token/accessible-resources."
        )
    return Tenant(
        code,
        site.rstrip("/"),
        label,
        auth,
        email,
        token,
        cloud_id,
        client_id,
        client_secret,
        refresh,
        problem,
        email_origine,
        token_origine,
    )


def _all_tenants() -> list[Tenant]:
    return [_describe_tenant(code) for code in _declared_tenants()]


def _tenants(selector: str = "", exiger_unique: bool = False) -> list[Tenant]:
    """Les tenants vises par un appel. Vide / '*' / 'TOUS' = tous ceux qui repondent.

    Un tenant declare mais non configure n'est pas une erreur silencieuse : s'il
    est explicitement demande, on refuse ; s'il fait partie du lot par defaut,
    l'appel continue sur les autres et le rendu le dit. Un chiffre calcule sur
    une seule instance alors que l'utilisateur en attendait deux est exactement
    le genre de faux qu'on ne veut pas citer.
    """
    everything = _all_tenants()
    want = (selector or "").strip()
    if want and want.upper() not in {"*", "ALL", "TOUS", "TOUTES", "LES_DEUX"}:
        codes = [_tenant_code(c) for c in want.split(",") if c.strip()]
        known = {t.code: t for t in everything}
        out: list[Tenant] = []
        for code in codes:
            tenant = known.get(code)
            if tenant is None:
                candidate = _describe_tenant(code)
                if candidate.ready:
                    out.append(candidate)
                    continue
                raise ConfigError(
                    f"Tenant inconnu : {code}. Tenants declares : "
                    f"{', '.join(t.code for t in everything) or '(aucun)'}. "
                    "Appelle jira_tenants pour l'etat de la configuration. "
                    "PROJET vaut vuproject, WF vaut webfacto."
                )
            if not tenant.ready:
                raise ConfigError(
                    f"Tenant {code} non configure : {tenant.problem}. "
                    "Appelle jira_setup_status pour la marche a suivre."
                )
            out.append(tenant)
        if exiger_unique and len(out) != 1:
            raise ConfigError(
                "Cet outil porte sur UN seul tenant : une cle de ticket n'a de "
                "sens que dans son instance. Passe tenant=\"PROJET\" ou "
                "tenant=\"WF\", pas les deux."
            )
        return out

    ready = [t for t in everything if t.ready]
    if exiger_unique:
        if len(ready) == 1:
            return ready
        raise ConfigError(
            "Cet outil porte sur UN seul tenant, et tu n'as pas dit lequel. "
            "Une cle de ticket n'a de sens que dans son instance : SUPPLY-3527 "
            "existe sur PROJET, il n'a aucun rapport avec un ticket de meme "
            "numero cote WF. Passe tenant=\"PROJET\" ou tenant=\"WF\"."
        )
    if not ready:
        details = "\n".join(f"  - {t.code} : {t.problem}" for t in everything)
        packaged = _packaged_python()
        raise ConfigError(
            "Aucun tenant JIRA configure.\n\n"
            + (details or "  (aucun tenant declare dans JIRA_TENANTS)")
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
            )
            + "\n\nEn resume : appelle jira_setup_status, qui donne la marche a "
            "suivre pour ce poste. La saisie se fait dans /plugin, jamais dans "
            "la conversation."
        )
    return ready


def _missing_tenants() -> list[Tenant]:
    return [t for t in _all_tenants() if not t.ready]


# --------------------------------------------------------------------------
# Le magasin d'identifiants, sur la machine
# --------------------------------------------------------------------------

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


def _key_store_path() -> pathlib.Path:
    """Ou les identifiants sont ranges sur la machine, quand on les range.

    Le meme fichier que le serveur relit au demarrage : on reutilise la chaine
    de recherche plutot que d'inventer un second format. On n'ecrit jamais dans
    un dossier synchronise.
    """
    explicit = (os.environ.get("JIRA_ENV_FILE") or "").strip()
    if explicit:
        return pathlib.Path(explicit)
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        return pathlib.Path(plugin_data) / "jira.env"
    return _local_root() / "jira.env"


def _stored_values() -> dict[str, str]:
    path = _key_store_path()
    try:
        if not path.is_file():
            return {}
        return _parse_env(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}


def _value_source(name: str) -> str:
    """Quelle COUCHE fournit la variable `name`. Rend "" si personne ne la pose."""
    _load_env_file()
    if os.environ.get(name):
        if name in _ENV_FILE_KEYS:
            return f"fichier {_ENV_LOADED_FROM}"
        if os.environ.get("CLAUDE_PLUGIN_ROOT"):
            return "configuration du plugin (userConfig)"
        return "environnement du processus"
    if _load_shared_env().get(name):
        return f"config d'equipe ({_SHARED_LOADED_FROM})"
    return ""


def _credential_names(code: str, suffixe: str) -> list[str]:
    """Les variables essayees pour un champ d'identification, dans l'ordre."""
    noms = [f"JIRA_{code}_{suffixe}", f"JIRA_{suffixe}"]
    defaut = _default_tenant_code()
    if _creds_fallback() and defaut and defaut != code:
        noms.append(f"JIRA_{defaut}_{suffixe}")
    return noms


def _key_source(code: str, suffixe: str = "TOKEN") -> str:
    """D'ou sort le jeton d'un tenant : la premiere question quand ca ne marche pas.

    Rend la VARIABLE qui a repondu et la COUCHE qui la porte. Les deux comptent :
    savoir que le jeton vient du fichier d'equipe ne suffit pas s'il vient de la
    ligne de l'AUTRE instance, reprise par defaut - c'est la cause d'un 401 qui,
    sans cette precision, ressemble a un mauvais jeton.
    """
    for name in _credential_names(code, suffixe):
        source = _value_source(name)
        if source:
            marque = "" if name.startswith(f"JIRA_{code}_") else "  <- repli"
            return f"{name} ({source}){marque}"
    return "(aucun)"


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
    preserve - il peut porter d'autres reglages, et l'autre tenant.
    """
    path = _key_store_path()
    if _is_synced(path):
        raise ConfigError(
            f"Refus d'ecrire des identifiants dans un dossier synchronise "
            f"({path}) : ils partiraient sur le drive partage de l'equipe, et "
            "un jeton Jira est nominatif. Definis JIRA_ENV_FILE sur un chemin "
            "local."
        )
    lines: list[str] = []
    try:
        if path.is_file():
            drop = re.compile(
                r"\s*(" + "|".join(re.escape(k) for k in values) + r")\s*="
            )
            lines = [
                line
                for line in path.read_text(encoding="utf-8-sig").splitlines()
                if not drop.match(line)
            ]
    except OSError as exc:
        raise ConfigError(f"Lecture impossible de {path} : {exc}") from exc

    if not lines:
        lines = [
            "# Configuration du serveur MCP jira.",
            "# Fichier LOCAL, hors du vault synchronise : un jeton d'API Jira",
            "# est nominatif et autorise l'ecriture. Il ne se partage pas.",
            f"# Ecrit le {dt.date.today().isoformat()} par jira_save_key.",
            "",
        ]
    for key in sorted(values):
        lines.append(f'{key}="{values[key]}"')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Sans BOM : trois octets invisibles devant la premiere variable la
        # rendraient introuvable.
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Ecriture impossible dans {path} : {exc}") from exc
    return path, _restrict_permissions(path)


# --------------------------------------------------------------------------
# OAuth : le seul endroit ou ce serveur emet autre chose qu'un GET
# --------------------------------------------------------------------------
#
# C'est la reponse a « peut-on generer les jetons par l'API ». Il faut
# distinguer deux choses que le langage courant confond :
#
#   - le JETON D'API PERSONNEL (mode basic) NE PEUT PAS etre cree par une API.
#     Atlassian n'expose aucun endpoint pour ca : c'est une action d'interface
#     sur id.atlassian.com, volontairement, parce que le jeton porte l'identite
#     de la personne. Aucun connecteur ne contourne ce point.
#   - le JETON D'ACCES OAUTH, lui, se renouvelle par API - c'est meme sa raison
#     d'etre : il vit une heure, et un refresh token le regenere sans personne.
#     C'est ce que fait la fonction ci-dessous.
#
# La requete est un POST, mais vers le service d'authentification Atlassian, pas
# vers l'API Jira : elle ne cree, ne modifie et ne supprime aucune donnee. Elle
# ne passe donc ni par la liste blanche d'ecriture, ni par la confirmation - il
# n'y a rien a confirmer, rien ne change chez personne.
#
# ROTATION. Atlassian rend souvent un NOUVEAU refresh token a chaque
# renouvellement, et invalide le precedent. Ne pas le persister, c'est marcher
# une heure puis casser definitivement - il faudrait refaire le consentement a
# la main. On l'ecrit donc dans le magasin local, tout de suite.

_TOKENS: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()
# Marge avant expiration : on renouvelle une minute avant, pour ne pas perdre
# un appel sur une expiration survenue entre l'obtention et l'envoi.
TOKEN_MARGIN_S = 60.0


def _oauth_access_token(tenant: Tenant) -> str:
    """Rend un jeton d'acces OAuth valide, en le renouvelant au besoin."""
    now = time.time()
    with _TOKEN_LOCK:
        cached = _TOKENS.get(tenant.code)
        if cached and cached[1] - TOKEN_MARGIN_S > now:
            return cached[0]
        payload = {
            "grant_type": "refresh_token",
            "client_id": tenant.client_id,
            "client_secret": tenant.client_secret,
            "refresh_token": tenant.refresh_token,
        }
        try:
            resp = httpx.post(
                OAUTH_TOKEN_URL,
                json=payload,
                timeout=_timeout(),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise JiraError(
                f"[{tenant.code}] renouvellement du jeton OAuth impossible : "
                f"{exc}. Si le poste est derriere un proxy, il faut le "
                "configurer (HTTPS_PROXY)."
            ) from exc
        if resp.status_code >= 400:
            body = (resp.text or "")[:400]
            raise JiraError(
                f"[{tenant.code}] renouvellement du jeton OAuth refuse "
                f"(HTTP {resp.status_code}) | {body}\n"
                "Les deux causes courantes : le refresh token a ete consomme "
                "par un autre poste (Atlassian le fait tourner, un seul "
                "detenteur a la fois), ou le consentement a ete revoque. Dans "
                "les deux cas il faut refaire le premier consentement a la "
                "main - ce n'est pas rattrapable par une nouvelle tentative."
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise JiraError(
                f"[{tenant.code}] reponse non JSON du service de jetons."
            ) from exc
        access = str(data.get("access_token") or "")
        if not access:
            raise JiraError(
                f"[{tenant.code}] le service de jetons n'a rendu aucun "
                "access_token."
            )
        try:
            ttl = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600.0
        _TOKENS[tenant.code] = (access, now + ttl)

        rotated = str(data.get("refresh_token") or "")
        if rotated and rotated != tenant.refresh_token:
            # A ecrire MAINTENANT : l'ancien vient d'etre invalide.
            name = f"JIRA_{tenant.code}_REFRESH_TOKEN"
            os.environ[name] = rotated
            tenant.refresh_token = rotated
            try:
                _write_key_store({name: rotated})
            except ConfigError:
                # On ne casse pas l'appel en cours : le jeton d'acces est bon
                # pour une heure. Mais la prochaine session echouera, et
                # jira_doctor le dira.
                pass
        return access


# --------------------------------------------------------------------------
# Couche HTTP
# --------------------------------------------------------------------------

_CLIENT: httpx.Client | None = None
_CLIENT_LOCK = threading.Lock()
# Dernieres informations de quota rendues par l'API, par tenant.
_RATE_LIMIT: dict[str, str] = {}


def _client() -> httpx.Client:
    """Le client HTTP du processus, partage et garde ouvert.

    Un httpx.get par appel rouvre la connexion TLS a chaque page. Sur une
    recherche qui pagine cinquante fois et sur deux instances, le gain se voit.
    httpx.Client est sur en usage concurrent : c'est ce qui permet d'interroger
    les deux tenants de front.
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


def _clean_query(query: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in (query or {}).items():
        if val is None or val == "":
            continue
        if isinstance(val, bool):
            out[key] = "true" if val else "false"
        elif isinstance(val, (list, tuple)):
            out[key] = ",".join(str(v) for v in val)
        else:
            out[key] = str(val)
    return out


def _auth_kwargs(tenant: Tenant) -> dict[str, Any]:
    """Les elements d'authentification a passer a httpx, selon le mode."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "myrddin-mcp-jira/1.0",
    }
    if tenant.auth == "basic":
        # Le couple courriel + jeton, encode par httpx. Jira Cloud refuse le
        # jeton seul : c'est la cause n° 1 des 401 en mode basic.
        return {"headers": headers, "auth": (tenant.email, tenant.token)}
    if tenant.auth == "bearer":
        headers["Authorization"] = f"Bearer {tenant.token}"
        return {"headers": headers}
    headers["Authorization"] = f"Bearer {_oauth_access_token(tenant)}"
    return {"headers": headers}


def _jira_errors(resp: httpx.Response) -> str:
    """Le message d'erreur que Jira met dans le corps, remis a plat.

    Jira est bavard et precis quand il refuse un JQL : « The value 'X' does not
    exist for the field 'status' ». Ce message vaut dix fois le code HTTP, et il
    est dans le corps, pas dans le statut.
    """
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or "")[:400]
    bits: list[str] = []
    if isinstance(data, dict):
        for msg in data.get("errorMessages") or []:
            bits.append(str(msg))
        errors = data.get("errors")
        if isinstance(errors, dict):
            for field, msg in errors.items():
                bits.append(f"{field} : {msg}")
        if not bits and data.get("message"):
            bits.append(str(data["message"]))
    return " | ".join(bits) or (resp.text or "")[:400]


def _request(
    tenant: Tenant,
    path: str,
    query: dict[str, Any] | None = None,
    method: str = "GET",
    body: Any = None,
) -> Any:
    """Un appel sur l'API du tenant. GET par defaut, POST et PUT bornes.

    DEUX REGIMES DE REPRISE, et c'est le garde-fou n° 2 du connecteur :

      - un GET est REJOUE sur une coupure reseau, un 429 ou un 5xx. Le rejouer
        ne change rien a l'etat de Jira, donc c'est gratuit.
      - une ECRITURE n'est JAMAIS rejouee, pas meme sur un 429 ou elle serait
        pourtant sans risque. La regle n'a pas d'exception parce qu'une regle
        sans exception se tient : un POST /issue rejoue apres un delai
        d'attente, c'est un doublon dans le referentiel, et le serveur ne peut
        pas savoir si le premier appel a abouti - Jira n'expose pas de cle
        d'idempotence sur ces chemins. Il rend l'erreur en le disant, et
        laisse verifier.
    """
    if not tenant.ready:
        raise ConfigError(f"Tenant {tenant.code} non configure : {tenant.problem}")
    verbe = (method or "GET").strip().upper()
    ecriture = verbe != "GET"
    if ecriture:
        # Defense en profondeur : meme appele par un outil dedie qui a deja
        # verifie, un chemin d'ecriture repasse par la liste blanche. Un outil
        # qui se tromperait de chemin ne peut pas sortir de la liste.
        _check_write_path(verbe, path)
    params = _clean_query(query)
    url = tenant.api_base + path
    kwargs = _auth_kwargs(tenant)
    if ecriture:
        kwargs["json"] = {} if body is None else body
    resp: httpx.Response | None = None
    last_error = ""
    tentatives = 1 if ecriture else RETRY_ATTEMPTS
    for attempt in range(tentatives):
        try:
            resp = _client().request(verbe, url, params=params, **kwargs)
        except httpx.HTTPError as exc:
            last_error = str(exc)
            if ecriture:
                raise JiraError(
                    f"[{tenant.code}] {verbe} {path} : la requete n'a pas "
                    f"abouti ({exc}).\nCE N'EST PAS UNE GARANTIE QUE RIEN N'A "
                    "ETE ECRIT : la coupure peut avoir eu lieu apres que Jira "
                    "a traite l'appel. Le serveur ne rejoue pas une ecriture, "
                    "il creerait un doublon. Va verifier dans Jira - "
                    "jira_issue, jira_comments ou jira_recherche - avant de "
                    "relancer."
                ) from exc
            if attempt >= tentatives - 1:
                raise JiraError(
                    f"[{tenant.code}] appel {path} impossible : {exc}"
                ) from exc
            time.sleep(0.5 * (attempt + 1))
            continue
        if (
            not ecriture
            and resp.status_code in RETRY_STATUSES
            and attempt < tentatives - 1
        ):
            # Jira rend un Retry-After sur 429 : on l'honore, borne a 10 s.
            wait = 0.5 * (2 ** attempt)
            if resp.status_code == 429:
                try:
                    wait = min(10.0, float(resp.headers.get("Retry-After", wait)))
                except (TypeError, ValueError):
                    pass
            time.sleep(wait)
            continue
        break
    if resp is None:
        raise JiraError(f"[{tenant.code}] appel {path} impossible : {last_error}")

    for name in ("X-RateLimit-Remaining", "X-Ratelimit-Remaining"):
        if name in resp.headers:
            _RATE_LIMIT[tenant.code] = f"{resp.headers[name]} appel(s) restant(s)"
            break

    code = resp.status_code
    if code == 400 and ecriture:
        raise JiraError(
            f"[{tenant.code}] HTTP 400 sur {verbe} {path} : corps refuse par "
            "Jira. RIEN N'A ETE ECRIT.\n"
            f"Ce que Jira repond : {_jira_errors(resp)}\n"
            "Jira nomme le champ fautif, et c'est presque toujours l'une de "
            "ces quatre causes : un champ qui n'existe pas sur l'ecran de "
            "creation de CE projet, une valeur hors de sa liste, un champ "
            "obligatoire absent, ou un type de ticket qui n'appartient pas au "
            "projet. jira_types_ticket donne les types reels du projet, "
            "jira_fields les champs de l'instance."
        )
    if code == 400:
        raise JiraError(
            f"[{tenant.code}] HTTP 400 sur {path} : requete refusee par Jira.\n"
            f"Ce que Jira repond : {_jira_errors(resp)}\n"
            "Sur une recherche, c'est presque toujours le JQL : un nom de champ "
            "inexistant, un libelle de statut qui n'existe pas dans cette "
            "instance, ou une personne designee par son nom au lieu de son "
            "accountId. jira_fields et jira_statuses donnent les valeurs "
            "reelles, jira_user_lookup l'accountId."
        )
    if code == 401:
        detail = {
            "basic": (
                f"Verifie le COUPLE JIRA_{tenant.code}_EMAIL + "
                f"JIRA_{tenant.code}_TOKEN. Trois causes, dans l'ordre de "
                "frequence : le jeton a expire (un jeton d'API Atlassian a une "
                "duree de vie, il n'est pas eternel), le courriel n'est pas "
                "celui du compte Atlassian, ou le jeton a ete cree sur l'AUTRE "
                "instance - un jeton est propre a un compte, pas a un site."
            ),
            "bearer": (
                f"Le jeton JIRA_{tenant.code}_TOKEN est refuse : expire, "
                "revoque, ou emis pour une autre instance."
            ),
            "oauth": (
                "Le jeton d'acces a ete refuse alors qu'il vient d'etre "
                "renouvele. La cause habituelle : la racine appelee. Un jeton "
                "OAuth ne s'envoie PAS au site atlassian.net, il s'envoie a "
                f"{GATEWAY}/<cloudId>. Verifie JIRA_{tenant.code}_CLOUD_ID."
            ),
        }.get(tenant.auth, "")
        raise JiraError(
            f"[{tenant.code}] HTTP 401 : authentification refusee. {detail}\n"
            "jira_doctor dit d'ou vient le jeton actuellement utilise."
        )
    if code == 403:
        nuance = ""
        if ecriture:
            nuance = (
                "\nRIEN N'A ETE ECRIT. Sur une ecriture, la cause la plus "
                "frequente n'est pas une faute de configuration : le compte "
                "peut LIRE ce projet sans avoir le droit d'y ecrire. C'est "
                "meme le provisionnement recommande pour le compte de service "
                "de l'equipe. Pour ecrire - et pour que Jira trace sous ton "
                "nom - pose un jeton nominatif dans la configuration du "
                "plugin : il passe DEVANT le compte d'equipe."
            )
        raise JiraError(
            f"[{tenant.code}] HTTP 403 sur {verbe} {path} : authentifie, mais "
            "sans droit sur cette ressource.\n"
            f"Ce que Jira repond : {_jira_errors(resp)}{nuance}\n"
            "A distinguer du 401 : ici le compte est reconnu. Soit le projet "
            "n'est pas visible pour ce compte, soit l'instance demande une "
            "re-authentification par le navigateur (Jira le fait apres "
            "plusieurs echecs). Se connecter une fois sur le site le leve."
        )
    if code == 404:
        raise JiraError(
            f"[{tenant.code}] HTTP 404 sur {path} : ressource inexistante sur "
            "CETTE instance. Une cle de ticket ou de projet n'a de sens que "
            "dans son instance - une cle valide sur PROJET n'existe pas sur WF. "
            f"Ce que Jira repond : {_jira_errors(resp)}"
        )
    if code == 410:
        raise JiraError(
            f"[{tenant.code}] HTTP 410 sur {path} : endpoint retire par "
            "Atlassian. C'est le cas de l'ancienne recherche /search sur les "
            "instances Cloud recentes ; le serveur bascule alors sur "
            "/search/jql. Si le message apparait ailleurs, la liste blanche de "
            "rest_get_paths.json est a mettre a jour."
        )
    if code == 429 and ecriture:
        raise JiraError(
            f"[{tenant.code}] HTTP 429 sur {verbe} {path} : quota d'appels "
            "atteint. RIEN N'A ETE ECRIT - Jira refuse avant de traiter. Le "
            "serveur ne rejoue pas, meme ici ou ce serait sans risque : la "
            "regle « une ecriture ne se rejoue pas » n'a pas d'exception, "
            "c'est ce qui la rend tenable. Relance l'appel toi-meme dans "
            f"{resp.headers.get('Retry-After', 'quelques')} s. Le quota est "
            "compte par COMPTE : avec le compte de service, il est partage "
            "entre tous les postes de l'equipe."
        )
    if code == 429:
        raise JiraError(
            f"[{tenant.code}] HTTP 429 : quota d'appels atteint. Jira Cloud "
            "limite le debit par compte et par instance. Resserre le perimetre "
            "(une periode, un projet), baisse max_rows, ou rapatrie une fois "
            "avec jira_sync puis interroge le cache local en SQL - il se relit "
            "sans quota. Attente conseillee : "
            f"{resp.headers.get('Retry-After', 'non precisee')} s."
        )
    if code >= 400:
        doute = ""
        if ecriture and code >= 500:
            doute = (
                "\nUN 5xx SUR UNE ECRITURE EST AMBIGU : Jira a peut-etre "
                "traite l'appel avant de tomber. Verifie dans Jira avant de "
                "relancer - le serveur ne rejoue pas une ecriture."
            )
        raise JiraError(
            f"[{tenant.code}] HTTP {code} sur {verbe} {path} | "
            f"{_jira_errors(resp)}{doute}"
        )

    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise JiraError(
            f"[{tenant.code}] reponse non JSON sur {path} : "
            f"{(resp.text or '')[:300]}"
        ) from exc


# --------------------------------------------------------------------------
# La recherche : pagination par jeton, et le repli
# --------------------------------------------------------------------------
#
# Atlassian a RETIRE l'ancien endpoint /search des instances Cloud en 2025. Le
# moderne est /search/jql, et il change deux choses qui se voient dans tous les
# rendus de ce connecteur :
#
#   1. il pagine PAR JETON (nextPageToken), pas par startAt ;
#   2. il ne rend AUCUN total.
#
# Consequence a assumer : « combien de tickets » n'est pas une question a un
# appel. On parcourt les pages en ne demandant que la cle, et on dit toujours si
# le plafond a ete atteint. Un compte annonce sans cette mention n'est pas
# citable - c'est exactement le genre de chiffre qui part dans un mail.

_SEARCH_MODE: dict[str, str] = {}


def _search_page(
    tenant: Tenant,
    jql: str,
    fields: str,
    page_size: int,
    cursor: str | int,
) -> tuple[list[dict[str, Any]], str | int | None]:
    """Une page de resultats. Rend (tickets, curseur_suivant ou None)."""
    mode = _SEARCH_MODE.get(tenant.code, "jql")
    if mode == "jql":
        query: dict[str, Any] = {
            "jql": jql,
            "maxResults": page_size,
            "fields": fields or ISSUE_FIELDS_BRIEF,
        }
        if cursor:
            query["nextPageToken"] = cursor
        try:
            payload = _request(tenant, "/search/jql", query)
        except JiraError as exc:
            texte = str(exc)
            if "HTTP 404" in texte or "HTTP 410" in texte:
                # Instance ancienne ou Data Center : on bascule une fois, et on
                # s'en souvient pour ne pas payer l'aller-retour a chaque page.
                _SEARCH_MODE[tenant.code] = "legacy"
                return _search_page(tenant, jql, fields, page_size, 0)
            raise
        issues = [i for i in (payload.get("issues") or []) if isinstance(i, dict)]
        token = payload.get("nextPageToken")
        # La fin de collection se lit a l'absence de jeton. isLast, quand il est
        # rendu, dit la meme chose : on honore les deux.
        if payload.get("isLast") is True:
            token = None
        return issues, (str(token) if token else None)

    start = int(cursor or 0)
    payload = _request(
        tenant,
        "/search",
        {
            "jql": jql,
            "startAt": start,
            "maxResults": page_size,
            "fields": fields or ISSUE_FIELDS_BRIEF,
        },
    )
    issues = [i for i in (payload.get("issues") or []) if isinstance(i, dict)]
    total = payload.get("total")
    nxt: int | None = start + len(issues)
    if not issues or (isinstance(total, int) and nxt >= total):
        nxt = None
    return issues, nxt


def _search(
    tenant: Tenant,
    jql: str,
    fields: str = "",
    max_rows: int = 200,
) -> tuple[list[dict[str, Any]], str]:
    """Parcourt les pages jusqu'a max_rows. Rend (tickets, raison_arret).

    La raison est vide quand la collection a ete lue en ENTIER, et dit sinon
    pourquoi on s'est arrete : les deux arrets n'appellent pas la meme
    correction. 'max_rows' veut dire qu'il restait des lignes ; 'max_pages' que
    le garde-fou de pagination du serveur a mordu.
    """
    out: list[dict[str, Any]] = []
    cursor: str | int = 0
    size = min(_page_size(), max(1, int(max_rows)))
    pages = 0
    cap = _max_pages()
    while True:
        issues, cursor_next = _search_page(tenant, jql, fields, size, cursor)
        pages += 1
        out.extend(issues)
        if len(out) >= max_rows:
            return out[:max_rows], "max_rows" if cursor_next else ""
        if not cursor_next:
            return out, ""
        if pages >= cap:
            return out, "max_pages"
        cursor = cursor_next


def _collect(
    tenants: list[Tenant],
    jql: str,
    fields: str = "",
    max_rows: int = 200,
) -> tuple[list[tuple[Tenant, dict[str, Any]]], list[str], list[str]]:
    """Interroge chaque tenant et empile les tickets. Rend (paires, tronques, notes).

    max_rows s'applique PAR TENANT, pas au total : deux instances a 200 lignes
    rendent donc jusqu'a 400 lignes, et le rendu le dit.
    """
    if not tenants:
        return [], [], ["aucun tenant configure"]

    def fetch(tenant: Tenant):
        try:
            issues, stop = _search(tenant, jql, fields, max_rows)
        except (ConfigError, JiraError) as exc:
            return tenant, [], "", str(exc)
        return tenant, issues, stop, ""

    if len(tenants) == 1:
        results = [fetch(tenants[0])]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tenants)) as pool:
            results = list(pool.map(fetch, tenants))

    pairs: list[tuple[Tenant, dict[str, Any]]] = []
    truncated: list[str] = []
    notes: list[str] = []
    for tenant, issues, stop, error in results:
        if error:
            notes.append(f"[{tenant.code}] ECHEC : {error}")
            continue
        if stop:
            truncated.append(f"{tenant.code} ({stop})")
        for issue in issues:
            pairs.append((tenant, issue))
        notes.append(f"[{tenant.code}] {len(issues)} ticket(s)")
    return pairs, truncated, notes


# --------------------------------------------------------------------------
# ADF : rendre lisible une description Jira
# --------------------------------------------------------------------------

def _adf_text(node: Any, depth: int = 0) -> str:
    """Aplatit un document ADF en texte lisible.

    La v3 de l'API stocke les descriptions et les commentaires en Atlassian
    Document Format : un arbre JSON. Rendu brut, un cadrage comme SUPPLY-3527
    pese des dizaines de milliers de caracteres de structure pour quelques
    milliers de texte utile. On garde le texte, les titres, les puces et les
    lignes de tableau, on jette la mise en forme.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(n, depth) for n in node)
    if not isinstance(node, dict):
        return str(node)

    kind = node.get("type") or ""
    content = node.get("content")

    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return "@" + str((node.get("attrs") or {}).get("text") or "").lstrip("@")
    if kind == "emoji":
        return str((node.get("attrs") or {}).get("shortName") or "")
    if kind == "inlineCard":
        return str((node.get("attrs") or {}).get("url") or "")
    if kind == "heading":
        niveau = int((node.get("attrs") or {}).get("level") or 1)
        return "\n" + "#" * niveau + " " + _adf_text(content, depth).strip() + "\n"
    if kind == "paragraph":
        return _adf_text(content, depth).strip() + "\n"
    if kind in {"bulletList", "orderedList"}:
        lignes = []
        for index, item in enumerate(content or [], start=1):
            puce = "- " if kind == "bulletList" else f"{index}. "
            texte = _adf_text(item, depth + 1).strip()
            prefixe = "  " * depth
            lignes.append(prefixe + puce + texte.replace("\n", " "))
        return "\n".join(lignes) + "\n"
    if kind == "listItem":
        return _adf_text(content, depth)
    if kind == "codeBlock":
        return "\n```\n" + _adf_text(content, depth).strip() + "\n```\n"
    if kind == "blockquote":
        return "> " + _adf_text(content, depth).strip().replace("\n", "\n> ") + "\n"
    if kind == "rule":
        return "\n---\n"
    if kind == "table":
        lignes = []
        for row in content or []:
            cellules = [
                _adf_text(cell.get("content"), depth).strip().replace("\n", " ")
                for cell in (row.get("content") or [])
                if isinstance(cell, dict)
            ]
            lignes.append(" | ".join(cellules))
        return "\n".join(lignes) + "\n"
    if kind == "mediaSingle" or kind == "mediaGroup":
        return "[piece jointe]\n"
    return _adf_text(content, depth)


def _texte_champ(value: Any) -> str:
    """Rend un champ texte, qu'il soit en ADF ou en texte simple."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("type") == "doc":
        return _adf_text(value)
    return _adf_text(value)


# --------------------------------------------------------------------------
# Mise en ligne d'un ticket, et rendu
# --------------------------------------------------------------------------

def _nested(source: Any, *keys: str) -> str:
    """Descend une suite de cles sans jamais lever. Rend "" si le chemin casse."""
    cur = source
    for key in keys:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    if cur is None:
        return ""
    if isinstance(cur, (dict, list)):
        return json.dumps(cur, ensure_ascii=False, default=str)
    return str(cur)


def _short_date(raw: Any) -> str:
    """Une date Jira ramenee a « AAAA-MM-JJ hh:mm ».

    Jira rend « 2026-08-27T10:12:33.123+0200 ». Les millisecondes et le fuseau
    prennent la moitie de la colonne et n'ont jamais servi a decider.
    """
    text = str(raw or "")
    if len(text) >= 16 and text[10] in "T ":
        return text[:10] + " " + text[11:16]
    return text[:10]


def _days_since(raw: Any) -> Any:
    """Nombre de jours entiers depuis une date Jira. "" si illisible."""
    text = str(raw or "")[:19]
    if len(text) < 10:
        return ""
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return ""
    return (dt.datetime.now() - moment).days


def _issue_row(
    tenant: Tenant, issue: dict[str, Any], extra: str = ""
) -> dict[str, Any]:
    """Un ticket ramene a une ligne de tableau.

    C'est ici que le connecteur gagne l'essentiel de sa sobriete : un ticket
    Jira brut porte des dizaines de champs imbriques, la ligne ci-dessous en
    porte quinze, a plat, nommes en francais. La description n'y est PAS -
    volontairement : elle pese a elle seule plus que tout le reste, et elle se
    demande ticket par ticket avec jira_issue.
    """
    fields = issue.get("fields") or {}
    key = str(issue.get("key") or "")
    row: dict[str, Any] = {
        "tenant": tenant.code,
        "cle": key,
        "projet": key.split("-")[0] if "-" in key else "",
        "type": _nested(fields, "issuetype", "name"),
        "statut": _nested(fields, "status", "name"),
        "categorie": _nested(fields, "status", "statusCategory", "name"),
        "priorite": _nested(fields, "priority", "name"),
        "resume": str(fields.get("summary") or ""),
        "assigne": _nested(fields, "assignee", "displayName"),
        "rapporteur": _nested(fields, "reporter", "displayName"),
        "cree": _short_date(fields.get("created")),
        "maj": _short_date(fields.get("updated")),
        "resolu": _short_date(fields.get("resolutiondate")),
        "echeance": _short_date(fields.get("duedate")),
        "resolution": _nested(fields, "resolution", "name"),
        "parent": _nested(fields, "parent", "key"),
        "etiquettes": ",".join(str(l) for l in (fields.get("labels") or [])),
        "age_jours": _days_since(fields.get("created")),
        "jours_depuis_maj": _days_since(fields.get("updated")),
        "url": tenant.browse(key) if key else "",
    }
    for name in [f.strip() for f in (extra or "").split(",") if f.strip()]:
        if name in row:
            continue
        value = fields.get(name)
        if isinstance(value, dict):
            row[name] = (
                value.get("name")
                or value.get("value")
                or value.get("displayName")
                or _texte_champ(value)[:300]
            )
        elif isinstance(value, list):
            morceaux = []
            for item in value:
                if isinstance(item, dict):
                    morceaux.append(
                        str(item.get("name") or item.get("value") or item.get("key") or "")
                    )
                else:
                    morceaux.append(str(item))
            row[name] = ",".join(m for m in morceaux if m)
        elif value is None:
            row[name] = ""
        else:
            row[name] = value
    return row


def _flatten(row: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Aplatit les objets imbriques en notation pointee."""
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
    """Projette des colonnes. Accepte le joker de prefixe `prefixe.*`."""
    wanted = [f.strip() for f in (fields or "").split(",") if f.strip()]
    if not wanted or wanted == ["*"]:
        return rows
    available = _columns(rows)
    resolved: list[str] = []
    for token in wanted:
        if token == "*":
            resolved.extend(available)
        elif token.endswith(".*"):
            prefix = token[:-1]
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
    """Rend une collection en CSV point-virgule, borne en taille.

    Ce que l'entete doit dire, et qu'il dit : sur quels tenants on a lu, combien
    de lignes, et si la lecture est COMPLETE. Un compte issu d'une lecture
    tronquee n'est pas un volume, et c'est exactement le chiffre qui finit cite
    dans un mail a un transporteur.
    """
    flat = _select([_flatten(r) for r in rows], fields)
    lines = [header]
    if notes:
        lines.append("Par tenant : " + " | ".join(notes))
    lines.append(f"{len(rows)} ligne(s) ramenees, tous tenants confondus.")
    if truncated:
        lines.append(
            "ATTENTION : lecture INCOMPLETE sur "
            + ", ".join(truncated)
            + ". Le compte ci-dessus n'est PAS un total : ne le cite pas comme "
            "un volume. Pour COMPTER sur un large perimetre, utilise "
            "jira_summary ; pour un historique, jira_sync puis jira_sql."
        )
    else:
        lines.append(
            "Lecture complete sur ce perimetre : ce compte est citable, avec "
            "ses filtres."
        )
    if any("ECHEC" in n for n in notes):
        lines.append(
            "ATTENTION : au moins un tenant n'a pas repondu. Le resultat est "
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
            f"Rendu limite a {max(0, len(kept) - 1)} ligne(s) sur {len(flat)} "
            "ramenees, pour ne pas saturer la conversation. Restreins avec "
            "champs=... ou resserre les filtres. N'exporte en CSV que si "
            "l'utilisateur l'a demande."
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
    publie chaque outil avec deux parametres (args, kwargs) au lieu des vrais -
    les outils sortent alors du handshake mais sont INAPPELABLES. Le bug est
    passe une fois chez shiptify, il ne repasse pas ici.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ConfigError, JiraError) as exc:
            return f"ERREUR : {exc}"

    return wrapper


# --------------------------------------------------------------------------
# La traduction francais -> JQL
# --------------------------------------------------------------------------

_JQL_INTERDIT = re.compile(r"[\r\n\x00]")


def _quote_jql(value: str) -> str:
    """Echappe une valeur pour une chaine JQL entre guillemets.

    JQL echappe avec des antislashs, comme le JSON. Sans ca, un titre contenant
    un guillemet - il y en a - casse la requete, et une valeur bien choisie
    pourrait etendre le filtre. On refuse aussi les sauts de ligne : ils n'ont
    aucun sens dans un filtre et servent surtout a en cacher un second.
    """
    text = str(value or "")
    if _JQL_INTERDIT.search(text):
        raise JiraError(
            "Une valeur de filtre contient un saut de ligne ou un caractere de "
            "controle. Retire-le : un filtre tient sur une ligne."
        )
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _statut_jql(statut: str) -> tuple[str, str]:
    """Traduit un mot de statut en clause JQL. Rend (clause, note).

    Le mot courant passe par la table du contexte et devient une CATEGORIE de
    statut - universelle sur Jira Cloud. Un libelle qui n'est pas dans la table
    est passe tel quel a Jira, avec une note : c'est peut-etre un vrai libelle
    de cette instance, et Jira refusera clairement s'il ne l'est pas.
    """
    brut = (statut or "").strip()
    if not brut:
        return "", ""
    table = (_contexte().get("statuts") or {}).get("traduction") or {}
    index = {_norm(k): v for k, v in table.items() if not k.startswith("_")}
    trouve = index.get(_norm(brut))
    if trouve is not None:
        if not trouve:
            return "", f"statut « {brut} » : aucun filtre de statut applique"
        return trouve, f"statut « {brut} » traduit en {trouve}"
    if "," in brut:
        valeurs = ", ".join(f'"{_quote_jql(v.strip())}"' for v in brut.split(",") if v.strip())
        return (
            f"status IN ({valeurs})",
            f"statut « {brut} » pris comme une liste de libelles reels",
        )
    return (
        f'status = "{_quote_jql(brut)}"',
        f"statut « {brut} » pris comme un libelle reel de l'instance - "
        "jira_statuses donne les libelles existants si Jira le refuse",
    )


def _periode_jql(valeur: str, champ: str) -> tuple[str, str]:
    """Traduit une periode en litteral JQL. Rend (litteral, note). Leve si inconnu.

    On REFUSE une periode qu'on ne sait pas traduire, au lieu de l'ignorer :
    une periode ignoree ne rend pas une erreur, elle rend TOUTE la base, et le
    chiffre parait plausible.
    """
    brut = (valeur or "").strip()
    if not brut:
        return "", ""
    # Deja du JQL : une date relative, ou une fonction.
    if re.fullmatch(r"-?\d+[dwmMhy]", brut) or brut.endswith(")"):
        return brut, ""
    # Une date absolue, avec ou sans heure.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?", brut):
        return f'"{brut}"', ""
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", brut):
        jour, mois, annee = brut.split("/")
        return f'"{annee}-{mois}-{jour}"', f"{champ} : date lue au format francais"
    table = _contexte().get("periodes") or {}
    index = {_norm(k): v for k, v in table.items() if not k.startswith("_")}
    trouve = index.get(_norm(brut))
    if trouve:
        return trouve, f"{champ} « {brut} » traduit en {trouve}"
    connus = ", ".join(sorted(k for k in table if not k.startswith("_")))
    raise JiraError(
        f"Periode non comprise pour {champ} : « {brut} ». Ecris une date "
        "(2026-08-01), une duree relative JQL (-30d, -4w), ou l'un de ces "
        f"mots : {connus}.\n"
        "Le serveur refuse plutot que d'ignorer le filtre : une periode "
        "ignoree rendrait TOUTE la base, et le chiffre aurait l'air juste."
    )


def _assigne_jql(valeur: str, champ: str = "assignee") -> tuple[str, str]:
    """Traduit une personne en clause JQL. Rend (clause, note).

    Sur Jira Cloud, un filtre par NOM D'AFFICHAGE ne marche pas : depuis la
    mise en conformite RGPD d'Atlassian, il faut l'accountId. On accepte donc
    « moi », un accountId, ou un identifiant de compte, et on REFUSE un nom -
    en disant ou le resoudre. Refuser vaut mieux que produire un 400 opaque.
    """
    brut = (valeur or "").strip()
    if not brut:
        return "", ""
    if _norm(brut) in {"moi", "me", "currentuser", "mes", "moi meme", "guillaume"}:
        return f"{champ} = currentUser()", f"{champ} : moi, via currentUser()"
    if brut.lower() in {"vide", "personne", "non assigne", "aucun"}:
        return f"{champ} IS EMPTY", f"{champ} : non assigne"
    # Un accountId Atlassian : hexadecimal, souvent prefixe de 5..: ou 712020:.
    if re.fullmatch(r"[0-9a-fA-F]{24}", brut) or re.match(r"^[0-9a-z]+:[0-9a-f-]{8,}", brut):
        return f'{champ} = "{_quote_jql(brut)}"', f"{champ} : accountId"
    raise JiraError(
        f"Personne non resolue pour {champ} : « {brut} ». Sur Jira Cloud, un "
        "filtre par nom d'affichage ou par courriel ne fonctionne pas : "
        "Atlassian exige l'accountId. Appelle jira_user_lookup avec ce nom, "
        "puis repasse l'accountId ici. Pour soi-meme, passe simplement « moi »."
    )


def _projet_jql(projet: str) -> tuple[str, str]:
    """Traduit un projet - cle, nom, ou mot courant - en clause JQL."""
    brut = (projet or "").strip()
    if not brut:
        return "", ""
    connus = {
        k: v
        for k, v in (_contexte().get("projets") or {}).items()
        if not k.startswith("_")
    }
    codes: list[str] = []
    notes: list[str] = []
    for morceau in brut.split(","):
        mot = morceau.strip()
        if not mot:
            continue
        cle = re.sub(r"[^A-Z0-9_]+", "", mot.upper())
        if cle in connus:
            codes.append(cle)
            continue
        # Un nom de projet, ou un mot courant : on cherche dans le contexte.
        trouve = ""
        for code, info in connus.items():
            if not isinstance(info, dict):
                continue
            if _norm(mot) == _norm(info.get("nom")):
                trouve = code
                break
        if trouve:
            codes.append(trouve)
            notes.append(f"projet « {mot} » reconnu comme {trouve}")
        else:
            codes.append(cle or mot.upper())
            notes.append(
                f"projet « {mot} » pris tel quel : il n'est pas dans le "
                "contexte embarque. jira_projects donne les cles reelles"
            )
    if not codes:
        return "", ""
    if len(codes) == 1:
        return f"project = {codes[0]}", "; ".join(notes)
    return f"project IN ({', '.join(codes)})", "; ".join(notes)


def _ordre_jql(ordre: str) -> tuple[str, str]:
    brut = (ordre or "").strip()
    if not brut:
        return "updated DESC", ""
    index = {_norm(k): v for k, v in ORDRES.items()}
    trouve = index.get(_norm(brut))
    if trouve:
        return trouve, ""
    # On accepte un ORDER BY ecrit a la main - mais seulement s'il a la FORME
    # d'un ORDER BY : un ou plusieurs champs, chacun eventuellement suivi de ASC
    # ou DESC, un champ a plusieurs mots devant etre entre guillemets. Sans
    # cette exigence de forme, une phrase en francais (« par ordre
    # d'importance ») passait pour un tri et partait telle quelle dans le JQL,
    # ou Jira la refusait avec un message incomprehensible.
    _morceau = r'(?:"[^"]+"|[A-Za-z0-9_\.]+)(?:\s+(?:ASC|DESC))?'
    if re.fullmatch(rf"{_morceau}(?:\s*,\s*{_morceau})*", brut, re.I):
        return brut, f"ordre « {brut} » pris tel quel"
    raise JiraError(
        f"Ordre non compris : « {brut} ». Valeurs connues : "
        + ", ".join(sorted(ORDRES))
        + "."
    )


def _build_jql(
    projet: str = "",
    texte: str = "",
    titre: str = "",
    type_ticket: str = "",
    statut: str = "",
    assigne: str = "",
    rapporteur: str = "",
    epic: str = "",
    etiquette: str = "",
    priorite: str = "",
    cree_depuis: str = "",
    cree_jusqua: str = "",
    maj_depuis: str = "",
    resolu_depuis: str = "",
    jql_en_plus: str = "",
    ordre: str = "",
) -> tuple[str, list[str]]:
    """Assemble un JQL a partir de filtres en francais. Rend (jql, traductions).

    Les traductions sont rendues avec le JQL et affichees dans l'entete de
    chaque resultat. C'est ce qui rend la traduction verifiable : on voit ce que
    le serveur a compris, donc on voit quand il a mal compris.
    """
    clauses: list[str] = []
    notes: list[str] = []

    def ajoute(clause: str, note: str = "") -> None:
        if clause:
            clauses.append(clause)
        if note:
            notes.append(note)

    ajoute(*_projet_jql(projet))

    if texte.strip():
        ajoute(
            f'text ~ "{_quote_jql(texte.strip())}"',
            "texte cherche dans tous les champs textuels (titre, description, "
            "commentaires) via l'operateur ~",
        )
    if titre.strip():
        ajoute(
            f'summary ~ "{_quote_jql(titre.strip())}"',
            "recherche limitee au TITRE - c'est ce qu'il faut sur VUD, ou le "
            "titre porte la reference d'expedition",
        )
    if type_ticket.strip():
        valeurs = [t.strip() for t in type_ticket.split(",") if t.strip()]
        if len(valeurs) == 1:
            ajoute(f'issuetype = "{_quote_jql(valeurs[0])}"')
        else:
            liste = ", ".join(f'"{_quote_jql(v)}"' for v in valeurs)
            ajoute(f"issuetype IN ({liste})")
    ajoute(*_statut_jql(statut))
    if assigne.strip():
        ajoute(*_assigne_jql(assigne, "assignee"))
    if rapporteur.strip():
        ajoute(*_assigne_jql(rapporteur, "reporter"))
    if epic.strip():
        cle = epic.strip().upper()
        ajoute(
            f"parent = {cle}",
            f"epic {cle} : filtre sur parent. Si le resultat est vide alors que "
            "l'epic a des tickets, l'instance utilise peut-etre l'ancien champ "
            "« Epic Link » - jira_children essaie les deux",
        )
    if etiquette.strip():
        valeurs = [t.strip() for t in etiquette.split(",") if t.strip()]
        liste = ", ".join(f'"{_quote_jql(v)}"' for v in valeurs)
        ajoute(f"labels IN ({liste})")
    if priorite.strip():
        ajoute(f'priority = "{_quote_jql(priorite.strip())}"')

    for valeur, champ, operateur in (
        (cree_depuis, "created", ">="),
        (cree_jusqua, "created", "<="),
        (maj_depuis, "updated", ">="),
        (resolu_depuis, "resolutiondate", ">="),
    ):
        if valeur.strip():
            litteral, note = _periode_jql(valeur, champ)
            ajoute(f"{champ} {operateur} {litteral}", note)

    if jql_en_plus.strip():
        ajoute(
            f"({jql_en_plus.strip()})",
            "clause JQL ajoutee telle quelle : elle n'est pas verifiee par le "
            "serveur, c'est Jira qui la refusera si elle est fausse",
        )

    tri, note_tri = _ordre_jql(ordre)
    if note_tri:
        notes.append(note_tri)

    corps = " AND ".join(clauses)
    if not corps:
        raise JiraError(
            "Aucun filtre : refuse. Une recherche sans filtre parcourt "
            "l'instance entiere - des dizaines de milliers de tickets - et rend "
            "les cent premiers par date, ce qui ne repond a aucune question. "
            "Donne au minimum un projet, un texte ou une periode. Si tu veux "
            "vraiment tout, passe par jira_search avec un JQL explicite."
        )
    return f"{corps} ORDER BY {tri}", notes


# --------------------------------------------------------------------------
# Outils MCP
# --------------------------------------------------------------------------

mcp = FastMCP("jira")

# --- Mise en service : les identifiants ---------------------------------
#
# La saisie se fait dans l'interface de Claude Code, par les champs userConfig
# declares dans plugin.json (marques `sensitive`). Claude Code les collecte
# lui-meme et les passe au serveur par l'environnement : ils ne traversent
# jamais la conversation, donc ils n'entrent ni dans le contexte du modele, ni
# dans la transcription.
#
# C'est pour cette raison qu'aucun outil ici n'accepte un jeton en parametre.
# Un jira_set_token("...") serait plus direct a expliquer, mais il ferait
# passer le secret par le fil de la conversation - exactement ce que le champ
# `sensitive` existe pour eviter. jira_save_key ne prend donc aucun argument :
# il range ce qui est deja saisi.


@mcp.tool()
@_guard
def jira_setup_status() -> str:
    """Ou en est la mise en service : quels identifiants sont saisis, sont-ils ranges ?

    A appeler en premier apres l'installation du plugin, et chaque fois qu'un
    outil repond qu'aucun tenant n'est configure. Ne rend jamais un jeton en
    clair.
    """
    _load_env_file()
    tenants = _all_tenants()
    stored = _stored_values()
    store = _key_store_path()
    plugin_mode = bool(os.environ.get("CLAUDE_PLUGIN_ROOT"))

    lines = ["Mise en service du connecteur JIRA", ""]
    lines.append(
        f"Tenants declares     : {', '.join(t.code for t in tenants) or '(aucun)'}"
    )
    lines.append(f"Magasin sur machine  : {store}")
    lines.extend(_shared_report())
    lines.append("")
    for tenant in tenants:
        lines.append(f"Tenant {tenant.code}  ({tenant.label or 'sans libelle'})")
        lines.append(f"  Site           : {tenant.site or '(inconnu)'}")
        lines.append(f"  Mode d'authent.: {tenant.auth}")
        if tenant.auth == "basic":
            lines.append(f"  Courriel       : {tenant.email or 'AUCUN'}")
            lines.append(
                f"  Jeton actif    : {_mask(tenant.token) if tenant.token else 'AUCUN'}"
            )
        elif tenant.auth == "bearer":
            lines.append(
                f"  Jeton actif    : {_mask(tenant.token) if tenant.token else 'AUCUN'}"
            )
        else:
            lines.append(f"  cloudId        : {tenant.cloud_id or 'AUCUN'}")
            lines.append(f"  client_id      : {tenant.client_id or 'AUCUN'}")
            lines.append(
                f"  refresh token  : {_mask(tenant.refresh_token) if tenant.refresh_token else 'AUCUN'}"
            )
        lines.append(f"  Origine        : {_key_source(tenant.code)}")
        on_disk = stored.get(f"JIRA_{tenant.code}_TOKEN", "")
        lines.append(
            f"  Jeton range    : {'oui, ' + _mask(on_disk) if on_disk else 'non'}"
        )
        if tenant.problem:
            lines.append(f"  A FAIRE        : {tenant.problem}")
        if (
            tenant.token
            and on_disk
            and _fingerprint(tenant.token) != _fingerprint(on_disk)
        ):
            lines.append(
                "  NOTE           : le jeton range sur la machine n'est PAS "
                "celui utilise. La configuration du plugin est prioritaire sur "
                "le fichier. jira_save_key aligne le fichier."
            )
        lines.append("")

    missing = [t for t in tenants if not t.ready]
    if missing:
        # Ne JAMAIS faire ressaisir ce que l'equipe fournit deja : c'est le
        # premier reflexe a donner, sinon on fait creer un jeton nominatif pour
        # rien et on se retrouve avec deux sources de verite sur le poste.
        couverts = [
            t
            for t in tenants
            if t.ready
            and _load_shared_env().get(f"JIRA_{t.code}_TOKEN")
            and not os.environ.get(f"JIRA_{t.code}_TOKEN")
        ]
        if couverts:
            lines.append(
                "RIEN a saisir pour "
                + ", ".join(t.code for t in couverts)
                + " : les identifiants viennent de la configuration d'equipe. "
                "Ne les ressaisis pas, il n'y a que les instances ci-dessous a "
                "traiter."
            )
            lines.append("")
        if not _SHARED_LOADED_FROM:
            lines.append(
                "Avant de saisir quoi que ce soit : la ligne « Config "
                "d'equipe » ci-dessus dit « aucune ». La bibliotheque "
                "SharePoint « Transport BtoC » n'est donc pas atteignable "
                "depuis ce poste, ou elle est posee ailleurs - et tout ce que "
                "l'equipe a deja regle, identifiants du compte de service "
                "compris, est perdu. Synchronise-la, ou pointe-la avec "
                "VU_ENGINE_DIR ou JIRA_SHARED_ENV. C'est la correction dans "
                "une bonne partie des cas."
            )
            lines.append("")
        lines.append("A FAIRE - dans l'interface de Claude Code :")
        lines.append("")
        if plugin_mode:
            lines.append("  1. tape /plugin")
            lines.append("  2. choisis le plugin « jira » (marketplace vu-transport)")
            lines.append("  3. ouvre sa configuration et renseigne, pour chaque tenant :")
            for tenant in missing:
                lines.append(
                    f"       - « Courriel Atlassian - {tenant.code} » et "
                    f"« Jeton d'API Atlassian - {tenant.code} »"
                )
            lines.append("  4. redemarre la session pour que le serveur reprenne tout")
        else:
            lines.append(
                "  Ce serveur ne tourne PAS comme plugin : il n'y a donc pas de "
                "champ de configuration. Deux options :"
            )
            lines.append("  - installer le plugin (voir le README du marketplace), ou")
            lines.append(f"  - poser les identifiants dans {store}.")
        lines.append("")
        lines.append("OU TROUVER UN JETON D'API ATLASSIAN")
        lines.append("")
        lines.append(
            "  D'ABORD : demande a Guillaume si le compte de SERVICE de "
            "l'equipe couvre cette instance. Les identifiants d'equipe vivent "
            "dans 08_ENGINE/04_mcp/00_config/jira.shared.env, et s'ils y sont, "
            "il n'y a rien a creer - c'est la ligne « Config d'equipe » "
            "ci-dessus qui le dit."
        )
        lines.append("")
        lines.append(
            "  SINON, un jeton se cree a la main, une fois par instance, sur "
            "https://id.atlassian.com/manage-profile/security/api-tokens : "
            "« Create API token », un libelle parlant (par exemple "
            "« MCP jira - poste Guillaume »), puis copier la valeur - elle ne "
            "se reaffiche jamais. Un jeton cree depuis TON compte est "
            "nominatif : JIRA tracera ce qui est fait avec lui sous ton nom, "
            "et il tombera le jour ou tes droits changeront."
        )
        lines.append("")
        lines.append(
            "  ATLASSIAN N'EXPOSE AUCUNE API POUR CREER CE JETON. C'est "
            "volontaire de leur part : le jeton porte l'identite de la "
            "personne. Aucun connecteur ne peut donc s'en fabriquer un tout "
            "seul, et une reponse qui le promettrait serait fausse. Ce qui EST "
            "automatisable, c'est le renouvellement d'un jeton d'ACCES OAuth - "
            "voir le mode oauth dans le README, qui demande une application "
            "inscrite cote Atlassian, donc un cadrage Webfacto."
        )
        lines.append("")
        lines.append(
            "  Le meme compte Atlassian peut avoir acces aux deux instances, "
            "mais LE JETON N'EST PAS LE MEME : un jeton est lie au compte, et "
            "le compte doit etre invite sur chaque instance. Si le jeton de "
            "vuproject rend 401 sur webfacto, ce n'est pas une erreur de "
            "saisie, c'est qu'il faut un acces a webfacto."
        )
        lines.append("")
        lines.append(
            "  Le jeton ne se colle pas dans la conversation : le champ de "
            "configuration le garde hors du contexte du modele et hors de la "
            "transcription. S'il a ete colle dans le chat, il est a considerer "
            "comme divulgue - le revoquer et en creer un autre."
        )
        return "\n".join(lines)

    shared = _load_shared_env()
    from_team = [
        t
        for t in tenants
        if shared.get(f"JIRA_{t.code}_TOKEN")
        and not os.environ.get(f"JIRA_{t.code}_TOKEN")
    ]
    unsaved = [
        t
        for t in tenants
        if t.token
        and t not in from_team
        and not stored.get(f"JIRA_{t.code}_TOKEN")
    ]
    if from_team:
        lines.append(
            "Identifiants fournis par l'equipe pour "
            + ", ".join(t.code for t in from_team)
            + ", lus dans 08_ENGINE. Il n'y a RIEN a saisir sur ce poste pour "
            "ces instances : verifie la connexion avec jira_doctor."
        )
        lines.append("")
        lines.append(
            "  Deux cas ou l'on saisirait quand meme quelque chose dans "
            "/plugin : un acces NOMINATIF - pour que JIRA trace ce qu'on fait "
            "sous son propre nom et non sous celui du compte de service - ou "
            "une instance de recette. Ce que le poste definit passe devant "
            "l'equipe, sans toucher au fichier partage, donc sans casser les "
            "vingt-cinq autres postes."
        )
        lines.append("")
        lines.append(
            "  jira_save_key n'est utile que pour rendre ce poste autonome de "
            "la bibliotheque synchronisee : il recopie en local les valeurs "
            "actives."
        )
        lines.append("")
    if unsaved:
        lines.append(
            "Les identifiants de "
            + ", ".join(t.code for t in unsaved)
            + " sont saisis mais ne sont PAS ranges sur la machine. Appelle "
            "jira_save_key : ils survivront alors a une reinstallation du "
            "plugin."
        )
    elif not from_team:
        lines.append("Tout est en place. Verifie la connexion avec jira_doctor.")
    return "\n".join(lines)


@mcp.tool()
@_guard
def jira_save_key() -> str:
    """Range sur la machine les identifiants deja saisis dans l'interface de Claude Code.

    Ne prend aucun argument, **volontairement** : les identifiants viennent de
    la configuration du plugin, pas de la conversation. Ils n'ont donc pas a
    etre recopies dans un message pour etre ranges.

    Le fichier est ecrit hors de tout dossier synchronise, et ses droits sont
    restreints a l'utilisateur courant. Un jeton Jira est nominatif : il ne
    part jamais dans la bibliotheque d'equipe.
    """
    tenants = [t for t in _all_tenants() if t.token or t.refresh_token]
    if not tenants:
        raise ConfigError(
            "Aucun identifiant a ranger : aucun tenant n'a de jeton saisi. "
            "Appelle jira_setup_status pour la marche a suivre."
        )
    values: dict[str, str] = {}
    for tenant in tenants:
        if tenant.email:
            values[f"JIRA_{tenant.code}_EMAIL"] = tenant.email
        if tenant.token:
            values[f"JIRA_{tenant.code}_TOKEN"] = tenant.token
        if tenant.auth != "basic":
            values[f"JIRA_{tenant.code}_AUTH"] = tenant.auth
        if tenant.client_id:
            values[f"JIRA_{tenant.code}_CLIENT_ID"] = tenant.client_id
        if tenant.client_secret:
            values[f"JIRA_{tenant.code}_CLIENT_SECRET"] = tenant.client_secret
        if tenant.refresh_token:
            values[f"JIRA_{tenant.code}_REFRESH_TOKEN"] = tenant.refresh_token
        # Le site n'est recopie que s'il s'ecarte du defaut du serveur : figer
        # ici une valeur d'equipe reviendrait a se rendre sourd a sa prochaine
        # correction, puisque le poste passe devant l'equipe.
        if tenant.site and tenant.site != DEFAULT_TENANTS.get(tenant.code):
            values[f"JIRA_{tenant.code}_BASE_URL"] = tenant.site
    path, permissions = _write_key_store(values)
    lines = [f"Identifiants ranges sur la machine : {path}", ""]
    for tenant in tenants:
        secret = tenant.token or tenant.refresh_token
        lines.append(
            f"  {tenant.code} : empreinte {_fingerprint(secret)} "
            "(les 12 premiers caracteres du SHA-256, pour comparer sans "
            "afficher le jeton)"
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
    lines.append("Pour l'effacer : jira_forget_key.")
    return "\n".join(lines)


@mcp.tool()
@_guard
def jira_forget_key(tenant: str = "") -> str:
    """Supprime du magasin local les identifiants d'un tenant, ou de tous si vide.

    Ne touche pas a la configuration du plugin : si un jeton y est saisi, il
    continuera d'etre utilise. Pour le retirer completement, vider aussi le
    champ correspondant dans /plugin - et le revoquer sur id.atlassian.com si
    c'est une fuite qu'on traite, parce qu'un jeton efface ici reste valide
    chez Atlassian.
    """
    path = _key_store_path()
    codes = (
        [_tenant_code(c) for c in tenant.split(",") if c.strip()]
        if tenant.strip()
        else [t.code for t in _all_tenants()]
    )
    if not codes:
        raise ConfigError(
            "Aucun tenant a oublier : JIRA_TENANTS est vide et aucun tenant "
            "n'a ete passe en argument."
        )
    suffixes = (
        "EMAIL",
        "TOKEN",
        "AUTH",
        "CLIENT_ID",
        "CLIENT_SECRET",
        "REFRESH_TOKEN",
    )
    try:
        if not path.is_file():
            return f"Aucun identifiant range a supprimer ({path} n'existe pas)."
        motifs = "|".join(
            f"JIRA_{code}_{suffix}" for code in codes for suffix in suffixes
        )
        drop = re.compile(r"\s*(" + motifs + r")\s*=")
        original = path.read_text(encoding="utf-8-sig").splitlines()
        lines = [line for line in original if not drop.match(line)]
        removed = len(original) - len(lines)
        if any(l.strip() and not l.strip().startswith("#") for l in lines):
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            note = (
                f"{removed} ligne(s) retiree(s) de {path} (le reste du fichier "
                "est conserve)."
            )
        else:
            path.unlink()
            note = f"Fichier supprime : {path}"
    except OSError as exc:
        raise ConfigError(f"Suppression impossible dans {path} : {exc}") from exc

    still = [t.code for t in _all_tenants() if t.token or t.refresh_token]
    return "\n".join(
        [
            note,
            "",
            "Tenants qui gardent un identifiant actif pour cette session : "
            + (", ".join(still) if still else "aucun")
            + ". Il vient alors de la configuration du plugin, qui n'est pas "
            "touchee ici.",
            "",
            "RAPPEL : effacer un jeton ici ne le REVOQUE pas. S'il a fuite, "
            "va le revoquer sur "
            "https://id.atlassian.com/manage-profile/security/api-tokens.",
        ]
    )


@mcp.tool()
def jira_doctor() -> str:
    """Diagnostic : configuration lue, identifiants presents, et vrai appel par tenant.

    Le premier outil a appeler quand quelque chose coince. Ne rend jamais un
    jeton en clair.
    """
    _load_env_file()
    lines = ["Configuration du serveur MCP jira", ""]
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
            "virtualisee de %LOCALAPPDATA%. Ce connecteur range donc sa "
            f"configuration dans {_local_root()}, qui n'est pas virtualise."
        )
    lines.append("")
    lines.extend(_shared_report())
    lines.append("")
    lines.append(
        "Ordre de priorite        : configuration du plugin > jira.env du "
        "poste > config d'equipe > defaut du serveur"
    )
    lines.append("")
    lines.append(f"JIRA_TENANTS             : {', '.join(_declared_tenants())}")
    lines.append(
        f"JIRA_PAGE_SIZE           : {_page_size()} (plafond API : {PAGE_SIZE_MAX})"
    )
    lines.append(f"JIRA_MAX_PAGES           : {_max_pages()} (par tenant)")
    lines.append(f"JIRA_TIMEOUT_S           : {_timeout()}")
    lines.append(f"Dossier d'export         : {_export_dir()}")
    lines.append(f"Cache local              : {_cache_path()}")
    if _CONTEXTE_ERROR:
        lines.append(f"Contexte JIRA embarque   : ERREUR - {_CONTEXTE_ERROR}")
    else:
        ctx = _contexte()
        lines.append(
            f"Contexte JIRA embarque   : {len(_contexte_tenants())} tenant(s), "
            f"{len([k for k in (ctx.get('projets') or {}) if not k.startswith('_')])} "
            f"projet(s), {len(ctx.get('jql') or [])} recette(s) JQL "
            f"(releve {ctx.get('releve', '?')})"
        )
    try:
        spec = _spec()
        lines.append(
            f"Chemins GET autorises    : {len(spec['paths'])} "
            f"(releve {spec.get('released')})"
        )
    except ConfigError as exc:
        lines.append(f"Chemins GET autorises    : ERREUR - {exc}")

    lines.append("")
    lines.append("Tenants")
    for tenant in _all_tenants():
        lines.append(f"  {tenant.code}  {tenant.label}")
        lines.append(f"    Site         : {tenant.site or '(inconnu)'}")
        lines.append(f"    Racine API   : {tenant.api_base if tenant.ready else '(n/a)'}")
        lines.append(f"    Mode         : {tenant.auth}")
        if tenant.auth == "basic":
            lines.append(f"    Courriel     : {tenant.email or '(aucun)'}")
        lines.append(f"    Jeton        : {_mask(tenant.token or tenant.refresh_token)}")
        lines.append(f"    Origine      : {_key_source(tenant.code)}")
        if tenant.problem:
            lines.append(f"    PROBLEME     : {tenant.problem}")

    lines.append("")
    lines.append("Appel de verification")
    ready = [t for t in _all_tenants() if t.ready]
    if not ready:
        lines.append("  Aucun tenant configure : appel non tente.")
        return "\n".join(lines)
    for tenant in ready:
        try:
            moi = _request(tenant, "/myself")
            lines.append(
                f"  [{tenant.code}] GET /myself : OK | "
                f"{moi.get('displayName', '?')} <{moi.get('emailAddress', 'courriel masque')}>"
                f" | compte {moi.get('accountId', '?')}"
            )
        except (ConfigError, JiraError) as exc:
            lines.append(f"  [{tenant.code}] GET /myself : ECHEC | {exc}")
            continue
        try:
            issues, _ = _search(tenant, "ORDER BY created DESC", "summary", 1)
            if issues:
                lines.append(
                    f"  [{tenant.code}] recherche ({_SEARCH_MODE.get(tenant.code, 'jql')}) "
                    f": OK | dernier ticket vu {issues[0].get('key')}"
                )
            else:
                lines.append(
                    f"  [{tenant.code}] recherche : OK | aucun ticket visible "
                    "pour ce compte"
                )
        except (ConfigError, JiraError) as exc:
            lines.append(f"  [{tenant.code}] recherche : ECHEC | {exc}")
        if tenant.code in _RATE_LIMIT:
            lines.append(f"  [{tenant.code}] quota : {_RATE_LIMIT[tenant.code]}")
    return "\n".join(lines)


@mcp.tool()
@_guard
def jira_tenants() -> str:
    """Les deux instances JIRA configurees, leur site et leur etat.

    A appeler avant toute question qui porte sur une seule instance : le
    parametre `tenant` des autres outils attend l'un de ces codes. « vuproject »,
    « webfacto », « wf », « les devs » sont traduits automatiquement.
    """
    tenants = _all_tenants()
    lines = [
        "Instances JIRA declarees (JIRA_TENANTS)",
        "",
        "Un tenant = une instance Atlassian = un referentiel de projets et une "
        "numerotation de tickets qui lui sont propres. Rien n'est partage entre "
        "les deux : SUPPLY-3527 existe sur PROJET et n'a aucun rapport avec un "
        "ticket de meme numero cote WF.",
        "",
    ]
    for tenant in tenants:
        state = "pret" if tenant.ready else f"NON CONFIGURE - {tenant.problem}"
        lines.append(f"{tenant.code} : {state}")
        lines.append(f"    libelle : {tenant.label or '(aucun)'}")
        lines.append(f"    site    : {tenant.site or '(inconnu)'}")
        lines.append(f"    authent.: {tenant.auth}")
        ctx = _contexte_tenants().get(tenant.code) or {}
        if ctx.get("porte"):
            lines.append(f"    porte   : {ctx['porte']}")
        if tenant.code in _RATE_LIMIT:
            lines.append(f"    quota   : {_RATE_LIMIT[tenant.code]}")
    lines.append("")
    lines.append(
        "Les outils de LISTE interrogent par defaut tous les tenants prets et "
        "posent une colonne `tenant`. Les outils qui prennent une CLE de ticket "
        "exigent un tenant unique : une cle n'a de sens que dans son instance."
    )
    return "\n".join(lines)


@mcp.tool()
@_guard
def jira_contexte(sujet: str = "") -> str:
    """Le contexte JIRA embarque : projets, conventions de titre, chantiers, JQL.

    A lire AVANT de construire une requete sur un sujet qu'on ne connait pas.
    C'est ce qui evite de deviner : quel projet porte le sujet, comment se lit
    un titre de ticket VUD, quels epics portent Atlassien ou le tracking, et
    quelles recettes JQL marchent deja.

    `sujet` filtre : tenants, projets, titres, chantiers, statuts, periodes,
    jql, pieges. Vide, rend le sommaire et les recettes.
    """
    ctx = _contexte()
    if not ctx:
        raise JiraError(
            f"Contexte embarque illisible : {_CONTEXTE_ERROR or CONTEXTE_FILE}. "
            "Le connecteur fonctionne quand meme, mais sans traduction des "
            "questions en francais."
        )
    besoin = _norm(sujet)
    sections = {
        "tenants": "tenants",
        "instances": "tenants",
        "projets": "projets",
        "projet": "projets",
        "titres": "conventions_titres",
        "titre": "conventions_titres",
        "conventions": "conventions_titres",
        "chantiers": "chantiers",
        "chantier": "chantiers",
        "epics": "chantiers",
        "statuts": "statuts",
        "statut": "statuts",
        "periodes": "periodes",
        "periode": "periodes",
        "jql": "jql",
        "recettes": "jql",
        "pieges": "pieges",
        "piege": "pieges",
    }
    if besoin:
        cle = sections.get(besoin)
        if not cle:
            # Recherche libre dans tout le contexte : on rend les branches qui
            # contiennent le terme. Utile pour « Atlas », « TIL », « VUD7113 ».
            trouve: dict[str, Any] = {}
            for nom, branche in ctx.items():
                if nom.startswith("_"):
                    continue
                texte = json.dumps(branche, ensure_ascii=False, default=str)
                if besoin in _norm(texte):
                    trouve[nom] = branche
            if not trouve:
                raise JiraError(
                    f"Rien sur « {sujet} » dans le contexte embarque. Sections : "
                    + ", ".join(sorted(set(sections.values())))
                    + ". Le contexte n'est pas exhaustif : ce qui n'y est pas se "
                    "demande a JIRA (jira_projects, jira_fields, jira_search)."
                )
            return _render_json(
                trouve, f"Contexte JIRA - recherche « {sujet} » (releve {ctx.get('releve')})"
            )
        return _render_json(
            ctx.get(cle), f"Contexte JIRA - {cle} (releve {ctx.get('releve')})"
        )

    lines = [
        f"Contexte JIRA embarque, releve le {ctx.get('releve')}",
        "",
        "Il ne porte AUCUN avancement : JIRA fait foi sur les statuts et les "
        "dates. Ce qu'il porte, c'est ce qui ne se devine pas - dans quel "
        "projet vit un sujet, comment se lit un titre, quel epic porte quel "
        "chantier.",
        "",
        "Sections (passe le mot en argument) :",
        "  tenants     les deux instances et leurs alias",
        "  projets     les 12 projets de vuproject, et lesquels sont a nous",
        "  titres      le decodeur des titres VUD et des prefixes SUPPLY",
        "  chantiers   les epics par theme : lancements pays, Atlas, TIL, ...",
        "  statuts     pourquoi on filtre sur statusCategory et pas sur un libelle",
        "  periodes    les mots de periode traduits en JQL",
        "  jql         les recettes pretes a l'emploi",
        "  pieges      ce qui rend un chiffre faux sans prevenir",
        "",
        "Tout autre mot est cherche dans l'ensemble du contexte (essaie "
        "« Atlas », « TIL », « Habitat », « VUD7113 »).",
        "",
        "Recettes JQL :",
    ]
    for recette in ctx.get("jql") or []:
        lines.append("")
        lines.append(f"  {recette.get('intention')}")
        lines.append(f"    {recette.get('jql')}")
        if recette.get("note"):
            lines.append(f"    note : {recette['note']}")
    return "\n".join(lines)


@mcp.tool()
@_guard
def jira_list_paths(contains: str = "", ecriture: bool = False) -> str:
    """Liste les chemins de l'API que ce connecteur accepte d'appeler.

    Par defaut, les chemins GET atteignables par jira_get. `ecriture=True`
    donne l'autre liste : les quatre gestes d'ecriture exposes, avec leur
    VERBE, et ceux qui ne le sont PAS avec leur pourquoi.

    A appeler avant jira_get, pour ne pas deviner un chemin ou un nom de
    filtre. `contains` filtre sur le chemin ou le resume.

    Un chemin absent de ces listes est refuse. Ce n'est pas une limite de
    l'API Atlassian, c'est le choix de n'exposer que ce dont on a besoin.
    """
    needle = contains.strip().lower()
    if ecriture:
        wspec = _write_spec()
        lines = [
            f"Chemins d'ECRITURE autorises ({len(wspec['paths'])} gestes, "
            f"releve {wspec.get('released')})",
            f"Source : {wspec.get('source')}",
            f"Racine ajoutee par le serveur : {wspec.get('racine')}",
            "",
            "LA CLE PORTE LE VERBE, et ce n'est pas cosmetique : PUT "
            "/issue/{k} met a jour, DELETE /issue/{k} detruit - le meme "
            "chemin, deux gestes sans rapport. AUCUN DELETE N'EST EXPOSE.",
            "",
            "Aucune ecriture ne part sans confirmer=True : l'appel sans "
            "confirmation affiche le corps exact et s'arrete.",
            "",
        ]
        montres = 0
        for cle in sorted(wspec["paths"]):
            info = wspec["paths"][cle]
            resume = info.get("summary", "")
            if needle and needle not in cle.lower() and needle not in resume.lower():
                continue
            montres += 1
            lines.append(f"{cle}")
            lines.append(f"    outil     : {info.get('outil', '(aucun)')}")
            lines.append(f"    ce qu'il fait : {resume}")
            if info.get("corps"):
                lines.append(f"    corps     : {info['corps']}")
            if info.get("attention"):
                lines.append(f"    ATTENTION : {info['attention']}")
        if not montres:
            lines.append(f"(aucun geste ne correspond a '{contains}')")
        lines.append("")
        lines.append("Volontairement NON exposes :")
        for cle, raison in (wspec.get("non_exposes") or {}).items():
            if cle.startswith("_"):
                continue
            lines.append(f"  {cle} : {raison}")
        return "\n".join(lines)

    spec = _spec()
    lines = [
        f"Chemins GET autorises ({len(spec['paths'])} au total, releve "
        f"{spec.get('released')})",
        f"Source : {spec.get('source')}",
        f"Racine ajoutee par le serveur : {spec.get('racine')}",
        "",
        "jira_get n'emet QUE des GET : aucune ecriture ne passe par "
        "l'echappatoire generique. Les gestes d'ecriture ont leur propre "
        "liste et leurs propres outils - jira_list_paths(ecriture=True).",
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
            bits = [f"{param['in']} {param['name']}", str(param.get("type") or "?")]
            if param.get("required"):
                bits.append("requis")
            line = "    " + " | ".join(bits)
            if param.get("description"):
                line += f" | {param['description'][:160]}"
            lines.append(line)
    if not shown:
        lines.append(f"(aucun chemin ne correspond a '{contains}')")
    lines.append("")
    lines.append("Volontairement NON exposes :")
    for path, raison in (spec.get("non_exposes") or {}).items():
        if path.startswith("_"):
            continue
        lines.append(f"  {path} : {raison}")
    return "\n".join(lines)


# --- Referentiels : ce qui existe dans l'instance ------------------------


def _paginate_offset(
    tenant: Tenant,
    path: str,
    query: dict[str, Any],
    key: str,
    max_rows: int = 500,
) -> tuple[list[Any], bool]:
    """Pagination startAt/maxResults, pour les endpoints qui la pratiquent encore.

    Rend (lignes, il_en_reste). Le drapeau compte autant que les lignes : sans
    lui, un rendu tronque annonce un nombre qui n'est pas un total.
    """
    out: list[Any] = []
    start = 0
    size = min(_page_size(), max(1, int(max_rows)))
    pages = 0
    while True:
        payload = _request(
            tenant, path, {**query, "startAt": start, "maxResults": size}
        )
        if isinstance(payload, list):
            batch = payload
            total = None
            is_last = True
        else:
            batch = payload.get(key) or []
            total = payload.get("total")
            is_last = payload.get("isLast")
        out.extend(batch)
        pages += 1
        start += len(batch)
        if len(out) >= max_rows:
            reste = bool(batch) and (
                is_last is not True
                and (not isinstance(total, int) or start < total)
            )
            return out[:max_rows], reste
        if not batch or is_last is True:
            return out, False
        if isinstance(total, int) and start >= total:
            return out, False
        if pages >= _max_pages():
            return out, True


@mcp.tool()
@_guard
def jira_projects(tenant: str = "", contains: str = "", max_rows: int = 100) -> str:
    """Les projets visibles, par instance, avec ce que le contexte en sait.

    Le premier appel a faire sur une instance qu'on ne connait pas - typiquement
    WF, dont le referentiel n'a pas encore ete releve. Sur PROJET, la colonne
    `perimetre` vient du contexte embarque : elle dit lesquels sont a nous.
    """
    cibles = _tenants(tenant)
    connus = {
        k: v
        for k, v in (_contexte().get("projets") or {}).items()
        if not k.startswith("_")
    }
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    truncated: list[str] = []
    for cible in cibles:
        try:
            valeurs, reste = _paginate_offset(
                cible,
                "/project/search",
                {"query": contains.strip(), "orderBy": "key"},
                "values",
                max_rows=max(1, int(max_rows)),
            )
        except (ConfigError, JiraError) as exc:
            notes.append(f"[{cible.code}] ECHEC : {exc}")
            continue
        if reste:
            truncated.append(f"{cible.code} (max_rows)")
        for projet in valeurs:
            if not isinstance(projet, dict):
                continue
            cle = str(projet.get("key") or "")
            info = connus.get(cle) if cible.code == "PROJET" else None
            rows.append(
                {
                    "tenant": cible.code,
                    "cle": cle,
                    "nom": projet.get("name"),
                    "type": projet.get("projectTypeKey"),
                    "style": projet.get("style"),
                    "perimetre": (info or {}).get("perimetre", ""),
                    "url": f"{cible.site}/browse/{cle}" if cle else "",
                }
            )
        notes.append(f"[{cible.code}] {len(valeurs)} projet(s)")
    header = "Projets visibles" + (f" contenant « {contains} »" if contains else "")
    sortie = _render_table(rows, header, truncated, notes)
    if any(r["tenant"] == "PROJET" for r in rows):
        sortie += (
            "\n\nRappel du contexte : sur PROJET, deux projets seulement portent "
            "le perimetre Logistique & Transport, et il ne faut pas les "
            "confondre. SUPPLY porte les CHANTIERS (epics, etudes, lancements). "
            "VUD est un service desk d'EXPLOITATION : une expedition en "
            "incident vaut un ticket, et son titre est une reference "
            "d'expedition, pas une phrase. jira_contexte titres donne le "
            "decodeur."
        )
    return sortie


@mcp.tool()
@_guard
def jira_fields(tenant: str = "", contains: str = "", max_rows: int = 400) -> str:
    """Les champs de l'instance, systeme et personnalises, avec leur nom JQL.

    A lire AVANT d'ecrire un JQL sur un champ maison. Un champ personnalise
    s'appelle `customfield_10234` dans l'API et porte un autre nom en JQL : la
    colonne `noms_jql` donne les libelles utilisables. Filtrer sur un nom qui
    n'existe pas ne rend pas moins de lignes, ca fait echouer la requete.
    """
    cibles = _tenants(tenant)
    needle = contains.strip().lower()
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for cible in cibles:
        try:
            champs = _request(cible, "/field")
        except (ConfigError, JiraError) as exc:
            notes.append(f"[{cible.code}] ECHEC : {exc}")
            continue
        gardes = 0
        for champ in champs if isinstance(champs, list) else []:
            if not isinstance(champ, dict):
                continue
            nom = str(champ.get("name") or "")
            ident = str(champ.get("id") or "")
            clauses = ", ".join(str(c) for c in (champ.get("clauseNames") or []))
            if needle and needle not in nom.lower() and needle not in ident.lower():
                continue
            gardes += 1
            if len(rows) >= max_rows:
                continue
            rows.append(
                {
                    "tenant": cible.code,
                    "id": ident,
                    "nom": nom,
                    "personnalise": "oui" if champ.get("custom") else "non",
                    "cherchable": "oui" if champ.get("searchable") else "non",
                    "type": _nested(champ, "schema", "type"),
                    "noms_jql": clauses,
                }
            )
        notes.append(f"[{cible.code}] {gardes} champ(s) retenus")
    return _render_table(
        rows,
        "Champs de l'instance" + (f" contenant « {contains} »" if contains else ""),
        ["rendu limite a max_rows"] if len(rows) >= max_rows else [],
        notes,
    )


@mcp.tool()
@_guard
def jira_statuses(tenant: str, projet: str = "") -> str:
    """Les statuts REELS, par projet et par type de ticket.

    A appeler des que la question porte sur un statut nomme - « combien sont en
    recette », « qu'est-ce qui est en attente client ». Les libelles sont
    propres a chaque projet : filtrer sur un libelle devine fait echouer la
    requete, et filtrer sur statusCategory repond parfois moins finement que
    demande.
    """
    cible = _tenants(tenant, exiger_unique=True)[0]
    if projet.strip():
        cle = re.sub(r"[^A-Za-z0-9_]+", "", projet.strip()).upper()
        payload = _request(cible, f"/project/{cle}/statuses")
        lines = [f"[{cible.code}] statuts du projet {cle}", ""]
        for bloc in payload if isinstance(payload, list) else []:
            if not isinstance(bloc, dict):
                continue
            lines.append(f"Type de ticket : {bloc.get('name')}")
            for statut in bloc.get("statuses") or []:
                if not isinstance(statut, dict):
                    continue
                lines.append(
                    f"    {statut.get('name'):<32} categorie "
                    f"{_nested(statut, 'statusCategory', 'name')}"
                )
            lines.append("")
        lines.append(
            "En JQL : status = \"Libelle exact\" pour un libelle, ou "
            "statusCategory = \"In Progress\" pour la categorie. La categorie "
            "est la meme partout, le libelle non."
        )
        return "\n".join(lines)

    payload = _request(cible, "/status")
    rows = []
    for statut in payload if isinstance(payload, list) else []:
        if not isinstance(statut, dict):
            continue
        rows.append(
            {
                "tenant": cible.code,
                "nom": statut.get("name"),
                "categorie": _nested(statut, "statusCategory", "name"),
                "portee": _nested(statut, "scope", "type") or "globale",
                "projet": _nested(statut, "scope", "project", "id"),
            }
        )
    return _render_table(
        rows,
        f"[{cible.code}] tous les statuts de l'instance",
        [],
        [f"[{cible.code}] {len(rows)} statut(s)"],
    ) + (
        "\n\nCette liste est celle de l'INSTANCE. Pour savoir lesquels "
        "s'appliquent a un projet, passe projet=\"SUPPLY\"."
    )


@mcp.tool()
@_guard
def jira_referentiel(tenant: str, nom: str, max_rows: int = 300) -> str:
    """Un referentiel de l'instance : priorites, resolutions, types, liens, etiquettes.

    `nom` : priorites, resolutions, types, liens, etiquettes, categories.
    A appeler avant de filtrer sur l'une de ces valeurs.
    """
    cible = _tenants(tenant, exiger_unique=True)[0]
    table = {
        "priorites": ("/priority", ""),
        "priorite": ("/priority", ""),
        "resolutions": ("/resolution", ""),
        "resolution": ("/resolution", ""),
        "types": ("/issuetype", ""),
        "type": ("/issuetype", ""),
        "liens": ("/issueLinkType", "issueLinkTypes"),
        "lien": ("/issueLinkType", "issueLinkTypes"),
        "etiquettes": ("/label", "values"),
        "etiquette": ("/label", "values"),
        "labels": ("/label", "values"),
        "categories": ("/statuscategory", ""),
        "categorie": ("/statuscategory", ""),
    }
    choix = table.get(_norm(nom).replace(" ", ""))
    if not choix:
        raise JiraError(
            f"Referentiel inconnu : « {nom} ». Valeurs acceptees : "
            + ", ".join(sorted(set(table)))
            + "."
        )
    path, enveloppe = choix
    if enveloppe == "values":
        valeurs, reste = _paginate_offset(cible, path, {}, "values", max_rows)
        lines = [f"[{cible.code}] {nom} ({len(valeurs)})", ""]
        lines.extend("  " + str(v) for v in valeurs)
        if reste:
            lines.append("")
            lines.append(
                f"ATTENTION : plus de {max_rows} valeurs, la liste est tronquee."
            )
        return "\n".join(lines)
    payload = _request(cible, path)
    if enveloppe and isinstance(payload, dict):
        payload = payload.get(enveloppe) or []
    rows = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "tenant": cible.code,
                "nom": item.get("name"),
                "id": item.get("id"),
                "description": str(item.get("description") or "")[:120],
                "inward": item.get("inward", ""),
                "outward": item.get("outward", ""),
            }
        )
    return _render_table(
        rows, f"[{cible.code}] referentiel {nom}", [], [f"{len(rows)} valeur(s)"]
    )


@mcp.tool()
@_guard
def jira_myself(tenant: str = "") -> str:
    """Qui est le compte derriere le jeton, sur chaque instance.

    Utile pour deux choses : verifier qu'on parle a la bonne instance avec le
    bon compte, et recuperer son accountId - celui que `assignee = currentUser()`
    utilise implicitement.
    """
    lines = []
    for cible in _tenants(tenant):
        try:
            moi = _request(cible, "/myself")
        except (ConfigError, JiraError) as exc:
            lines.append(f"[{cible.code}] ECHEC : {exc}")
            continue
        lines.append(f"[{cible.code}] {cible.site}")
        lines.append(f"    nom        : {moi.get('displayName')}")
        lines.append(f"    courriel   : {moi.get('emailAddress', '(masque par Jira)')}")
        lines.append(f"    accountId  : {moi.get('accountId')}")
        lines.append(f"    fuseau     : {moi.get('timeZone')}")
        lines.append(f"    actif      : {moi.get('active')}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
@_guard
def jira_user_lookup(tenant: str, terme: str, max_rows: int = 20) -> str:
    """Cherche une personne et rend son accountId.

    C'est le passage OBLIGE pour filtrer par personne : depuis la mise en
    conformite RGPD d'Atlassian, `assignee = "Prenom Nom"` ne fonctionne plus
    sur Jira Cloud, il faut l'accountId. Cherche par nom d'affichage ou par
    courriel.
    """
    cible = _tenants(tenant, exiger_unique=True)[0]
    if not terme.strip():
        raise JiraError("Donne un nom ou un courriel a chercher.")
    payload = _request(
        cible,
        "/user/search",
        {"query": terme.strip(), "maxResults": max(1, min(50, int(max_rows)))},
    )
    rows = []
    for personne in payload if isinstance(payload, list) else []:
        if not isinstance(personne, dict):
            continue
        rows.append(
            {
                "tenant": cible.code,
                "nom": personne.get("displayName"),
                "accountId": personne.get("accountId"),
                "type": personne.get("accountType"),
                "actif": personne.get("active"),
                "courriel": personne.get("emailAddress", ""),
            }
        )
    if not rows:
        return (
            f"[{cible.code}] personne ne correspond a « {terme} ». Deux causes "
            "habituelles : la personne n'a pas de compte sur CETTE instance "
            "(les deux instances ont des annuaires distincts), ou la recherche "
            "par courriel est desactivee par la politique de confidentialite de "
            "l'instance - essaie alors le nom d'affichage."
        )
    return _render_table(
        rows,
        f"[{cible.code}] personnes correspondant a « {terme} »",
        [],
        [f"{len(rows)} resultat(s)"],
    ) + (
        "\n\nPour filtrer : passe l'accountId dans le parametre `assigne` ou "
        "`rapporteur`. Pour soi-meme, « moi » suffit."
    )


# --- Le coeur : chercher des tickets ------------------------------------


@mcp.tool()
@_guard
def jira_recherche(
    tenant: str = "",
    projet: str = "",
    texte: str = "",
    titre: str = "",
    type_ticket: str = "",
    statut: str = "",
    assigne: str = "",
    rapporteur: str = "",
    epic: str = "",
    etiquette: str = "",
    priorite: str = "",
    cree_depuis: str = "",
    cree_jusqua: str = "",
    maj_depuis: str = "",
    resolu_depuis: str = "",
    jql_en_plus: str = "",
    ordre: str = "",
    max_rows: int = 100,
    champs: str = "",
) -> str:
    """Cherche des tickets a partir de filtres en francais. L'OUTIL PRINCIPAL.

    C'est celui a preferer pour une question posee en langage naturel : le
    serveur assemble le JQL, le montre dans l'entete, et refuse un filtre qu'il
    ne sait pas traduire plutot que de l'ignorer. Un filtre ignore ne rend pas
    moins de lignes, il rend TOUTE la base avec l'air d'avoir compris.

    Ce que les parametres acceptent, en francais :
      statut       « ouvert », « en cours », « termine », « a faire », ou un
                   libelle reel de l'instance
      periodes     « cette semaine », « ce mois », « 30 jours », une date
                   2026-08-01, ou une duree JQL -30d
      assigne      « moi », « personne », ou un accountId (jira_user_lookup)
      projet       SUPPLY, VUD, ou un nom de projet
      texte        cherche partout (titre, description, commentaires)
      titre        cherche dans le TITRE seul - c'est ce qu'il faut sur VUD, ou
                   le titre porte la reference d'expedition
      epic         la cle d'un epic, pour ses tickets enfants
      champs       colonnes a afficher, ou des champs Jira supplementaires

    `tenant` vide interroge les DEUX instances et pose une colonne `tenant` ;
    max_rows s'applique alors PAR instance.
    """
    jql, traductions = _build_jql(
        projet=projet,
        texte=texte,
        titre=titre,
        type_ticket=type_ticket,
        statut=statut,
        assigne=assigne,
        rapporteur=rapporteur,
        epic=epic,
        etiquette=etiquette,
        priorite=priorite,
        cree_depuis=cree_depuis,
        cree_jusqua=cree_jusqua,
        maj_depuis=maj_depuis,
        resolu_depuis=resolu_depuis,
        jql_en_plus=jql_en_plus,
        ordre=ordre,
    )
    cibles = _tenants(tenant)
    extra = ",".join(
        c.strip()
        for c in (champs or "").split(",")
        if c.strip().startswith("customfield_") or c.strip() in {"description", "comment"}
    )
    demande = ISSUE_FIELDS_BRIEF + ("," + extra if extra else "")
    pairs, truncated, notes = _collect(cibles, jql, demande, max(1, int(max_rows)))
    rows = [_issue_row(t, issue, extra) for t, issue in pairs]

    header = (
        "JQL : " + jql + "\nInstances : " + ", ".join(c.code for c in cibles)
    )
    if traductions:
        header += "\nTraduit : " + " ; ".join(traductions)
    projection = champs if champs and not extra else ""
    return _render_table(rows, header, truncated, notes, projection)


@mcp.tool()
@_guard
def jira_search(
    tenant: str = "",
    jql: str = "",
    max_rows: int = 100,
    champs: str = "",
) -> str:
    """Execute un JQL ecrit a la main. L'echappatoire, quand la traduction ne suffit pas.

    A utiliser pour ce que jira_recherche ne sait pas exprimer : un OR, une
    fonction JQL (membersOf, sprint), un ORDER BY multiple, un champ
    personnalise. jira_contexte jql donne des recettes qui marchent, jira_fields
    les noms de champs reels.

    Le JQL est passe TEL QUEL a Jira : c'est lui qui le refusera s'il est faux,
    et son message est remonte en clair - il est precis.
    """
    if not jql.strip():
        raise JiraError(
            "Donne un JQL. Exemples dans jira_contexte jql. Pour une question "
            "posee en francais, jira_recherche est plus sur : il traduit et il "
            "refuse ce qu'il ne comprend pas."
        )
    cibles = _tenants(tenant)
    extra = ",".join(
        c.strip()
        for c in (champs or "").split(",")
        if c.strip().startswith("customfield_")
    )
    demande = ISSUE_FIELDS_BRIEF + ("," + extra if extra else "")
    pairs, truncated, notes = _collect(
        cibles, jql.strip(), demande, max(1, int(max_rows))
    )
    rows = [_issue_row(t, issue, extra) for t, issue in pairs]
    header = "JQL : " + jql.strip() + "\nInstances : " + ", ".join(
        c.code for c in cibles
    )
    return _render_table(rows, header, truncated, notes, champs if not extra else "")


@mcp.tool()
@_guard
def jira_issue(
    tenant: str,
    cle: str,
    commentaires: bool = False,
    historique: bool = False,
    champs: str = "",
) -> str:
    """Un ticket en detail, description comprise, rendue en texte lisible.

    La description est stockee en ADF (un arbre JSON) : le serveur l'aplatit en
    texte. C'est ce qui rend lisible un cadrage comme SUPPLY-3527, le document
    le plus dense du corpus.

    `commentaires=True` ajoute les echanges - sur un ticket VUD, c'est la que
    vit l'information. `historique=True` ajoute qui a change quoi, et depuis
    quand le ticket est dans son statut.
    """
    cible = _tenants(tenant, exiger_unique=True)[0]
    reference = cle.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", reference):
        raise JiraError(
            f"« {cle} » n'est pas une cle de ticket. Une cle s'ecrit "
            "PROJET-NUMERO, par exemple SUPPLY-3527."
        )
    demande = ISSUE_FIELDS_BRIEF + ",description,comment,issuelinks,subtasks,components,fixVersions"
    if champs.strip():
        demande += "," + champs.strip()
    issue = _request(
        cible,
        f"/issue/{reference}",
        {"fields": demande, "expand": "changelog" if historique else ""},
    )
    fields = issue.get("fields") or {}
    row = _issue_row(cible, issue, champs)
    lines = [f"[{cible.code}] {reference} - {row['resume']}", ""]
    for etiquette, valeur in (
        ("Projet", row["projet"]),
        ("Type", row["type"]),
        ("Statut", f"{row['statut']} (categorie {row['categorie']})"),
        ("Priorite", row["priorite"]),
        ("Assigne", row["assigne"] or "(personne)"),
        ("Rapporteur", row["rapporteur"]),
        ("Cree", f"{row['cree']} - il y a {row['age_jours']} jour(s)"),
        ("Mis a jour", f"{row['maj']} - il y a {row['jours_depuis_maj']} jour(s)"),
        ("Resolu", row["resolu"] or "(non resolu)"),
        ("Resolution", row["resolution"]),
        ("Echeance", row["echeance"]),
        ("Parent", row["parent"]),
        ("Etiquettes", row["etiquettes"]),
        ("Composants", ",".join(
            str(c.get("name")) for c in (fields.get("components") or [])
            if isinstance(c, dict)
        )),
        ("URL", row["url"]),
    ):
        if valeur not in ("", None):
            lines.append(f"{etiquette:<12}: {valeur}")

    liens = fields.get("issuelinks") or []
    if liens:
        lines.append("")
        lines.append("Liens")
        for lien in liens:
            if not isinstance(lien, dict):
                continue
            for sens, mot in (("inwardIssue", "inward"), ("outwardIssue", "outward")):
                autre = lien.get(sens)
                if isinstance(autre, dict):
                    lines.append(
                        f"    {_nested(lien, 'type', mot)} {autre.get('key')} - "
                        f"{_nested(autre, 'fields', 'summary')[:80]}"
                    )
    sous = fields.get("subtasks") or []
    if sous:
        lines.append("")
        lines.append(f"Sous-taches ({len(sous)})")
        for tache in sous[:30]:
            if isinstance(tache, dict):
                lines.append(
                    f"    {tache.get('key')} [{_nested(tache, 'fields', 'status', 'name')}] "
                    f"{_nested(tache, 'fields', 'summary')[:80]}"
                )

    description = _texte_champ(fields.get("description")).strip()
    lines.append("")
    lines.append("Description")
    if not description:
        lines.append("    (vide)")
    elif len(description) > TEXT_MAX_CHARS:
        lines.append(description[:TEXT_MAX_CHARS])
        lines.append("")
        lines.append(
            f"... TRONQUE : la description fait {len(description)} caracteres, "
            f"{TEXT_MAX_CHARS} sont affiches. Ouvre le ticket dans JIRA pour la "
            "suite, ou dis ce que tu cherches et je relirai la partie utile."
        )
    else:
        lines.append(description)

    if commentaires:
        lines.append("")
        lines.append(jira_comments(cible.code, reference, max_rows=20))
    if historique:
        lines.append("")
        lines.append(jira_changelog(cible.code, reference, max_rows=40))
    return "\n".join(lines)


@mcp.tool()
@_guard
def jira_comments(
    tenant: str, cle: str, max_rows: int = 30, ordre: str = "-created"
) -> str:
    """Les commentaires d'un ticket, en texte lisible.

    Sur un ticket VUD, l'essentiel de l'information n'est pas dans le titre ni
    dans la description : il est dans les commentaires. Par defaut du plus
    recent au plus ancien.
    """
    cible = _tenants(tenant, exiger_unique=True)[0]
    reference = cle.strip().upper()
    valeurs, reste = _paginate_offset(
        cible,
        f"/issue/{reference}/comment",
        {"orderBy": ordre.strip() or "-created"},
        "comments",
        max_rows=max(1, int(max_rows)),
    )
    lines = [f"[{cible.code}] {reference} - {len(valeurs)} commentaire(s) lus", ""]
    if reste:
        lines.append(
            f"ATTENTION : le ticket porte PLUS de {max_rows} commentaires, seuls "
            "les premiers du tri sont lus. Releve max_rows si l'historique "
            "complet compte."
        )
        lines.append("")
    for commentaire in valeurs:
        if not isinstance(commentaire, dict):
            continue
        auteur = _nested(commentaire, "author", "displayName")
        quand = _short_date(commentaire.get("created"))
        corps = _texte_champ(commentaire.get("body")).strip()
        if len(corps) > 1500:
            corps = corps[:1500] + " ... (tronque)"
        lines.append(f"--- {quand} - {auteur}")
        lines.append(corps or "(vide)")
        lines.append("")
    if not valeurs:
        lines.append("(aucun commentaire)")
    return "\n".join(lines)


@mcp.tool()
@_guard
def jira_changelog(tenant: str, cle: str, max_rows: int = 60) -> str:
    """L'historique d'un ticket : qui a change quoi, et quand.

    La source pour « depuis quand ce ticket est-il bloque » : le dernier
    changement de statut est calcule et affiche en tete. Un ticket ouvert depuis
    trois mois dont le statut n'a pas bouge depuis dix semaines, ce n'est pas la
    meme conversation qu'un ticket qui vient de changer d'etat.
    """
    cible = _tenants(tenant, exiger_unique=True)[0]
    reference = cle.strip().upper()
    valeurs, reste = _paginate_offset(
        cible,
        f"/issue/{reference}/changelog",
        {},
        "values",
        max_rows=max(1, int(max_rows)),
    )
    entrees: list[dict[str, Any]] = []
    dernier_statut = ""
    for entree in valeurs:
        if not isinstance(entree, dict):
            continue
        quand = _short_date(entree.get("created"))
        auteur = _nested(entree, "author", "displayName")
        for item in entree.get("items") or []:
            if not isinstance(item, dict):
                continue
            champ = str(item.get("field") or "")
            entrees.append(
                {
                    "tenant": cible.code,
                    "quand": quand,
                    "qui": auteur,
                    "champ": champ,
                    "de": str(item.get("fromString") or "")[:60],
                    "vers": str(item.get("toString") or "")[:60],
                }
            )
            if champ.lower() == "status":
                if not dernier_statut or quand > dernier_statut:
                    dernier_statut = quand
    lines = [f"[{cible.code}] {reference} - historique"]
    if dernier_statut:
        jours = _days_since(dernier_statut.replace(" ", "T"))
        lines.append(
            f"Dernier changement de STATUT : {dernier_statut}"
            + (f" - il y a {jours} jour(s)" if jours != "" else "")
        )
    else:
        lines.append(
            "Aucun changement de statut dans l'historique lu : le ticket n'a "
            "jamais change d'etat, ou la lecture est partielle."
        )
    lines.append("")
    return "\n".join(lines) + _render_table(
        entrees,
        "",
        ["max_rows"] if reste else [],
        [f"{len(entrees)} changement(s)"],
    )


@mcp.tool()
@_guard
def jira_children(tenant: str, cle: str, max_rows: int = 200, champs: str = "") -> str:
    """Les tickets rattaches a un epic ou a une tache.

    Essaie `parent`, puis l'ancien champ « Epic Link » si le premier ne rend
    rien : les projets classiques anciens rattachent encore par la, et une liste
    vide serait ici une reponse fausse a une question juste.
    """
    cible = _tenants(tenant, exiger_unique=True)[0]
    reference = cle.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", reference):
        raise JiraError(f"« {cle} » n'est pas une cle de ticket (PROJET-NUMERO).")

    tentatives = [
        (f"parent = {reference} ORDER BY created ASC", "champ parent"),
        (f'"Epic Link" = {reference} ORDER BY created ASC', "ancien champ Epic Link"),
    ]
    essais: list[str] = []
    for jql, libelle in tentatives:
        try:
            issues, stop = _search(
                cible, jql, ISSUE_FIELDS_BRIEF, max(1, int(max_rows))
            )
        except JiraError as exc:
            essais.append(f"{libelle} : refuse par Jira ({str(exc)[:120]})")
            continue
        if issues:
            rows = [_issue_row(cible, issue) for issue in issues]
            header = (
                f"[{cible.code}] tickets rattaches a {reference}, via {libelle}\n"
                f"JQL : {jql}"
            )
            if essais:
                header += "\nEssais precedents : " + " | ".join(essais)
            return _render_table(
                rows,
                header,
                [f"{cible.code} ({stop})"] if stop else [],
                [f"[{cible.code}] {len(issues)} ticket(s)"],
            )
        essais.append(f"{libelle} : aucun ticket")
    return (
        f"[{cible.code}] aucun ticket rattache a {reference}.\n"
        + "\n".join("  " + e for e in essais)
        + "\n\nTrois lectures possibles, dans cet ordre de probabilite : l'epic "
        "n'a pas encore de tickets ; ce n'est pas un epic mais une tache sans "
        "sous-tache ; le rattachement se fait par un champ personnalise propre "
        "a l'instance, que jira_fields permet de trouver."
    )


# --------------------------------------------------------------------------
# Agregation : compter, repartir
# --------------------------------------------------------------------------
#
# Le coeur d'une question de portefeuille est un COMPTE, pas une liste :
# « combien de chantiers ouverts », « comment se repartissent les incidents du
# mois », « qui porte quoi ». Sans agregation cote serveur, y repondre exige de
# rapatrier toutes les lignes dans la conversation.
#
# Et il y a une contrainte propre a Jira Cloud : l'API de recherche moderne ne
# rend AUCUN total. Un compte s'obtient donc en parcourant les pages. On le fait
# en ne demandant que les champs a grouper - une page de 100 tickets projetee
# sur trois champs pese quelques kilo-octets - et on DIT si le plafond a mordu.

GROUPES = {
    "statut": ("statut", "le libelle de statut, propre au projet"),
    "categorie": ("categorie", "la categorie de statut : To Do, In Progress, Done"),
    "type": ("type", "le type de ticket"),
    "priorite": ("priorite", "la priorite"),
    "assigne": ("assigne", "la personne affectee"),
    "rapporteur": ("rapporteur", "la personne qui a cree le ticket"),
    "projet": ("projet", "le projet"),
    "epic": ("parent", "l'epic ou la tache parente"),
    "parent": ("parent", "l'epic ou la tache parente"),
    "resolution": ("resolution", "la resolution"),
    "etiquette": ("etiquettes", "les etiquettes, telles qu'elles sont posees"),
    "mois_creation": ("cree", "le mois de creation"),
    "mois_maj": ("maj", "le mois de derniere mise a jour"),
    "tenant": ("tenant", "l'instance"),
    "aucun": ("", "aucun regroupement : rend juste le compte"),
}


@mcp.tool()
@_guard
def jira_summary(
    tenant: str = "",
    grouper_par: str = "statut",
    projet: str = "",
    texte: str = "",
    titre: str = "",
    type_ticket: str = "",
    statut: str = "",
    assigne: str = "",
    epic: str = "",
    cree_depuis: str = "",
    cree_jusqua: str = "",
    maj_depuis: str = "",
    jql: str = "",
    max_scan: int = 3000,
) -> str:
    """Compte et repartit des tickets, cote serveur. A PREFERER a une liste pour compter.

    « Combien de chantiers ouverts », « la repartition des incidents du mois par
    prestation », « qui porte quoi » : c'est cet outil, pas jira_recherche.
    Compter en listant coute la fenetre de conversation pour rien.

    `grouper_par` : statut, categorie, type, priorite, assigne, rapporteur,
    projet, epic, resolution, etiquette, mois_creation, mois_maj, tenant, aucun.

    Les filtres sont les memes que jira_recherche. `jql` remplace tous les
    filtres si tu preferes l'ecrire a la main.
    """
    choix = GROUPES.get(_norm(grouper_par).replace(" ", "_"))
    if not choix:
        raise JiraError(
            f"Regroupement inconnu : « {grouper_par} ». Valeurs acceptees : "
            + ", ".join(sorted(GROUPES))
            + "."
        )
    colonne, explication = choix

    traductions: list[str] = []
    if jql.strip():
        requete = jql.strip()
    else:
        requete, traductions = _build_jql(
            projet=projet,
            texte=texte,
            titre=titre,
            type_ticket=type_ticket,
            statut=statut,
            assigne=assigne,
            epic=epic,
            cree_depuis=cree_depuis,
            cree_jusqua=cree_jusqua,
            maj_depuis=maj_depuis,
            ordre="cle",
        )

    # On ne demande QUE ce qu'il faut pour grouper. C'est ce qui rend le compte
    # abordable : 3 000 tickets projetes sur deux champs, contre 3 000 tickets
    # complets qui ne tiendraient pas dans la conversation.
    besoin = {
        "statut": "status",
        "categorie": "status",
        "type": "issuetype",
        "priorite": "priority",
        "assigne": "assignee",
        "rapporteur": "reporter",
        "resolution": "resolution",
        "etiquettes": "labels",
        "parent": "parent",
        "cree": "created",
        "maj": "updated",
    }.get(colonne, "")
    demande = ",".join(filter(None, ["summary", besoin])) or "summary"

    cibles = _tenants(tenant)
    pairs, truncated, notes = _collect(
        cibles, requete, demande, max(1, int(max_scan))
    )
    total = len(pairs)

    lines = [
        "JQL : " + requete,
        "Instances : " + ", ".join(c.code for c in cibles),
    ]
    if traductions:
        lines.append("Traduit : " + " ; ".join(traductions))
    lines.append("Par instance : " + " | ".join(notes))
    lines.append("")
    if truncated:
        lines.append(
            "ATTENTION : le compte ci-dessous est un PLANCHER, pas un total. La "
            "lecture s'est arretee sur "
            + ", ".join(truncated)
            + " : il restait des tickets. NE CITE PAS ce chiffre comme un "
            "volume. Deux sorties : resserrer le perimetre (une periode, un "
            "projet) jusqu'a ce que la lecture soit complete, ou rapatrier une "
            "fois avec jira_sync et compter en SQL."
        )
    else:
        lines.append(
            f"Lecture COMPLETE du perimetre : {total} ticket(s). Ce compte est "
            "citable, avec ses filtres."
        )
    if any("ECHEC" in n for n in notes):
        lines.append(
            "ATTENTION : au moins une instance n'a pas repondu - le compte ne "
            "couvre pas le perimetre demande."
        )
    lines.append("")

    if not colonne:
        return "\n".join(lines)

    compte: dict[str, int] = {}
    vides = 0
    for cible, issue in pairs:
        row = _issue_row(cible, issue)
        valeur = str(row.get(colonne) or "")
        if colonne in {"cree", "maj"}:
            valeur = valeur[:7]
        if colonne == "etiquettes" and valeur:
            for etiq in valeur.split(","):
                if etiq.strip():
                    compte[etiq.strip()] = compte.get(etiq.strip(), 0) + 1
            continue
        if not valeur:
            vides += 1
            valeur = "(vide)"
        compte[valeur] = compte.get(valeur, 0) + 1

    lines.append(f"Reparti par {grouper_par} - {explication}")
    lines.append("")
    rows = []
    for valeur, nb in sorted(compte.items(), key=lambda kv: (-kv[1], kv[0])):
        rows.append(
            {
                "valeur": valeur,
                "tickets": nb,
                "part": f"{100 * nb / total:.1f} %" if total else "",
            }
        )
    lines.append(_to_csv_text(rows))
    if vides:
        lines.append(
            f"NOTE : {vides} ticket(s) n'ont aucune valeur pour ce champ, "
            f"comptes en « (vide) ». Sur {total}, cela fait "
            f"{100 * vides / total:.0f} % - a dire si l'on cite la repartition."
        )
    if colonne == "etiquettes":
        lines.append(
            "NOTE : un ticket peut porter plusieurs etiquettes, la somme des "
            "lignes depasse donc le nombre de tickets."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# ECRIRE : quatre gestes, trois garde-fous
# --------------------------------------------------------------------------
#
# Ce qui suit est la seule partie du connecteur qui CHANGE quelque chose chez
# quelqu'un d'autre. Un ticket cree, un commentaire pose, un statut qui bouge :
# c'est vu par toute l'equipe, ca part en notification, et ca peut declencher
# une automatisation Jira. D'ou la forme de chaque outil, identique aux quatre :
#
#   1. VALIDER EN LOCAL D'ABORD. Une cle mal formee, un titre vide, une
#      etiquette avec un espace, un champ interdit : refuses avant le moindre
#      appel reseau. Un refus qui ne coute pas d'appel est un refus qu'on peut
#      se permettre de rendre strict.
#   2. RESOUDRE, JAMAIS DEVINER. Le type de ticket, la priorite, la transition
#      et la personne sont relus sur l'instance et compares a l'identique. Un
#      nom approchant est REFUSE avec la liste des valeurs reelles - il n'est
#      jamais remplace par le plus proche. « Tache » au lieu de « Task » cree
#      un ticket du mauvais type sans lever d'erreur.
#   3. MONTRER, PUIS CONFIRMER. Sans confirmer=True, l'outil affiche le corps
#      EXACT qui partirait, l'instance visee et le compte qui signera, et
#      s'arrete. C'est la regle « l'envoi est un geste humain » du cerveau
#      d'equipe, rendue verifiable : on ne demande pas de croire un resume.


def _adf_depuis_texte(texte: str, quoi: str = "le texte") -> dict[str, Any]:
    """Construit un document ADF a partir de texte simple.

    Jira Cloud v3 n'accepte plus une description ni un commentaire en texte
    brut : le corps est de l'ADF, un arbre JSON. Et le piege est silencieux -
    une chaine posee dans le champ ne leve pas toujours d'erreur, elle
    enregistre un contenu vide. La conversion se fait donc ici, une seule
    fois, pour les deux outils qui en ont besoin.

    Une ligne vide separe deux paragraphes ; un simple retour a la ligne
    devient un hardBreak, comme dans l'editeur Jira.
    """
    brut = (texte or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not brut:
        raise JiraError(f"{quoi} est vide : il n'y a rien a ecrire.")
    contenu: list[dict[str, Any]] = []
    for para in re.split(r"\n\s*\n", brut):
        noeuds: list[dict[str, Any]] = []
        for i, ligne in enumerate(para.split("\n")):
            if i:
                noeuds.append({"type": "hardBreak"})
            if ligne:
                noeuds.append({"type": "text", "text": ligne})
        if noeuds:
            contenu.append({"type": "paragraph", "content": noeuds})
    if not contenu:
        raise JiraError(f"{quoi} ne contient que des espaces.")
    return {"type": "doc", "version": 1, "content": contenu}


def _cle_ticket(cle: str) -> str:
    """Valide une cle de ticket. PROJET-NUMERO, rien d'autre."""
    reference = (cle or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", reference):
        raise JiraError(
            f"« {cle} » n'est pas une cle de ticket. Une cle s'ecrit "
            "PROJET-NUMERO, par exemple SUPPLY-3527. Rappel : une cle n'a de "
            "sens que dans SON instance."
        )
    return reference


def _cle_projet(projet: str) -> str:
    """Valide une CLE de projet. Pour ecrire, un nom approchant ne suffit pas.

    En lecture, _projet_jql accepte « les incidents » ou « supply » et traduit.
    Ici non : creer un ticket dans le mauvais projet ne se corrige pas d'un
    filtre, ca laisse un ticket a deplacer a la main. On exige donc la cle.
    """
    brut = (projet or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", brut):
        raise JiraError(
            f"« {projet} » n'est pas une cle de projet. Pour ECRIRE, le "
            "connecteur exige la cle exacte - SUPPLY, VUD - et non un nom "
            "approchant : un ticket cree dans le mauvais projet se deplace a "
            "la main. jira_projects donne les cles reelles de l'instance."
        )
    return brut


def _etiquettes(valeur: str, quoi: str = "etiquettes") -> list[str]:
    """Decoupe une liste d'etiquettes, et refuse celles que Jira refusera.

    Jira interdit l'espace dans une etiquette et rend un 400 peu clair. Le
    dire ici coute un appel de moins et une minute de moins.
    """
    out: list[str] = []
    for morceau in (valeur or "").replace(";", ",").split(","):
        etiquette = morceau.strip()
        if not etiquette:
            continue
        if re.search(r"\s", etiquette):
            raise JiraError(
                f"Etiquette refusee : « {etiquette} ». Jira n'accepte pas "
                f"d'espace dans une etiquette ({quoi}) - utilise un tiret ou "
                "un souligne."
            )
        out.append(etiquette)
    return out


# Les champs qui ont leur propre parametre, et ne se posent donc pas dans
# champs_json : deux chemins pour la meme valeur, c'est une valeur qui gagne
# sans qu'on sache laquelle.
CHAMPS_RESERVES = {
    "project": "le parametre projet",
    "issuetype": "le parametre type_ticket",
    "summary": "le parametre titre",
    "description": "le parametre description",
    "assignee": "le parametre assigne",
    "priority": "le parametre priorite",
    "labels": "le parametre etiquettes",
    "parent": "le parametre parent",
    "duedate": "le parametre echeance",
}


def _champs_libres(champs_json: str) -> dict[str, Any]:
    """Les champs personnalises passes en JSON. Valide, et refuse le statut."""
    try:
        extra = json.loads(champs_json or "{}")
    except ValueError as exc:
        raise JiraError(f"champs_json n'est pas du JSON valide : {exc}") from exc
    if not isinstance(extra, dict):
        raise JiraError(
            "champs_json doit etre un objet JSON, de la forme "
            '{"customfield_10010": "valeur"}.'
        )
    for nom in extra:
        court = str(nom).strip().lower()
        if court in {"status", "statut", "resolution"}:
            raise JiraError(
                f"« {nom} » ne se pose pas comme un champ : un statut ne "
                "s'ecrit pas, il se FRANCHIT. Passe par "
                "jira_transition_ticket, qui resout la transition sur celles "
                "reellement disponibles pour ce ticket. C'est aussi la seule "
                "facon dont Jira l'accepte."
            )
        if court in CHAMPS_RESERVES:
            raise JiraError(
                f"« {nom} » a son propre parametre : utilise "
                f"{CHAMPS_RESERVES[court]}. Deux chemins pour la meme valeur, "
                "c'est une valeur qui gagne sans qu'on sache laquelle."
            )
    return extra


def _signataire(tenant: Tenant) -> str:
    """Sous quel compte l'ecriture sera tracee, et d'ou vient ce compte.

    Ce n'est pas une precaution decorative : avec les identifiants d'equipe,
    l'historique du ticket porte le nom du compte de SERVICE, pas celui de la
    personne. Acceptable pour lire, discutable pour ecrire - donc affiche
    avant chaque confirmation, avec la sortie.
    """
    if tenant.auth == "basic":
        qui = tenant.email or "(courriel absent)"
        return f"{qui}  [{tenant.email_origine or 'origine inconnue'}]"
    return (
        f"compte porte par le jeton {tenant.auth} - "
        f'jira_myself(tenant="{tenant.code}") dit lequel'
    )


def _apercu_ecriture(
    tenant: Tenant,
    verbe: str,
    chemin: str,
    corps: dict[str, Any],
    geste: str,
    notes: list[str] | None = None,
) -> str:
    """Le rendu d'un appel NON confirme. Rien n'est parti, et on montre quoi."""
    lignes = [
        "A CONFIRMER - RIEN N'A ETE ECRIT DANS JIRA.",
        "",
        f"Geste       : {geste}",
        f"Instance    : {tenant.code} ({tenant.site})",
        f"Appel       : {verbe} {API_ROOT}{chemin}",
        f"Trace sous  : {_signataire(tenant)}",
    ]
    for note in notes or []:
        lignes.append(f"ATTENTION   : {note}")
    lignes += [
        "",
        "Corps EXACT qui serait envoye :",
        json.dumps(corps, ensure_ascii=False, indent=2),
        "",
        "Pour l'executer : rappelle le meme outil, memes parametres, avec "
        "confirmer=True.",
        "Si le compte affiche ci-dessus n'est pas le tien et que ce geste doit "
        "etre trace a ton nom, pose ton jeton nominatif dans la configuration "
        "du plugin - il passe devant le compte d'equipe - puis relance la "
        "session.",
    ]
    return "\n".join(lignes)


# --- Resoudre, jamais deviner -------------------------------------------


def _types_projet(tenant: Tenant, cle_projet: str) -> list[dict[str, Any]]:
    """Les types de ticket reels d'un projet, tels que l'instance les declare."""
    payload = _request(tenant, f"/project/{cle_projet}")
    types = payload.get("issueTypes") or []
    if not types:
        raise JiraError(
            f"[{tenant.code}] le projet {cle_projet} ne declare aucun type de "
            "ticket exploitable. Verifie la cle du projet avec jira_projects."
        )
    return [t for t in types if isinstance(t, dict)]


def _resolve_type_ticket(
    tenant: Tenant, cle_projet: str, demande: str
) -> dict[str, Any]:
    """Un type de ticket -> sa fiche. Comparaison a l'identique, ou refus.

    Aucun rapprochement approximatif : « Tache » quand le projet declare
    « Task » cree un ticket du mauvais type, et Jira ne s'en plaint pas.
    """
    brut = (demande or "").strip()
    if not brut:
        raise JiraError(
            "type_ticket est vide. jira_types_ticket(tenant, projet) donne "
            "les types reels du projet - ils varient d'un projet a l'autre, "
            "surtout sur un projet gere par l'equipe."
        )
    types = _types_projet(tenant, cle_projet)
    for fiche in types:
        if str(fiche.get("id")) == brut:
            return fiche
    vise = _norm(brut)
    for fiche in types:
        if _norm(fiche.get("name")) == vise:
            return fiche
    reels = ", ".join(
        f"{t.get('name')} (id {t.get('id')})" for t in types
    )
    raise JiraError(
        f"Type de ticket inconnu dans {cle_projet} : « {brut} ». Le "
        f"connecteur ne prend PAS le plus proche - il refuse. Types reels de "
        f"ce projet : {reels}."
    )


def _resolve_priorite(tenant: Tenant, demande: str) -> dict[str, Any]:
    """Une priorite -> sa fiche, lue sur l'instance."""
    brut = (demande or "").strip()
    priorites = _request(tenant, "/priority")
    fiches = [p for p in (priorites or []) if isinstance(p, dict)]
    for fiche in fiches:
        if str(fiche.get("id")) == brut or _norm(fiche.get("name")) == _norm(brut):
            return fiche
    reelles = ", ".join(f"{p.get('name')} (id {p.get('id')})" for p in fiches)
    raise JiraError(
        f"Priorite inconnue sur {tenant.code} : « {brut} ». Priorites de "
        f"l'instance : {reelles}."
    )


def _resolve_compte(tenant: Tenant, valeur: str) -> tuple[str, str]:
    """Une personne -> (accountId, libelle). « moi » est resolu par /myself.

    Comme en lecture : un nom d'affichage est REFUSE, pas rapproche. Affecter
    un ticket a la mauvaise personne, c'est une notification a quelqu'un qui
    n'est pas concerne et un ticket qui dort.
    """
    brut = (valeur or "").strip()
    if not brut:
        return "", ""
    if _norm(brut) in {"moi", "me", "currentuser", "mes", "moi meme"}:
        moi = _request(tenant, "/myself")
        compte = str(moi.get("accountId") or "")
        if not compte:
            raise JiraError(
                f"[{tenant.code}] /myself ne rend pas d'accountId : "
                "impossible de resoudre « moi »."
            )
        return compte, f"{moi.get('displayName') or 'moi'} (via /myself)"
    if re.fullmatch(r"[0-9a-fA-F]{24}", brut) or re.match(
        r"^[0-9a-z]+:[0-9a-f-]{8,}", brut
    ):
        return brut, "accountId fourni"
    raise JiraError(
        f"Personne non resolue : « {brut} ». Atlassian exige l'accountId, y "
        "compris pour affecter un ticket - un nom d'affichage ou un courriel "
        "ne suffit pas. Appelle jira_user_lookup avec ce nom, puis repasse "
        "l'accountId ici. Pour soi-meme, passe simplement « moi »."
    )


def _transitions_dispo(tenant: Tenant, cle: str) -> list[dict[str, Any]]:
    """Les transitions reellement disponibles pour CE ticket, maintenant.

    « Pour ce ticket » et non « pour ce projet » : le workflow, les conditions
    et les droits font qu'une transition existe sans etre franchissable ici et
    maintenant. La liste est donc relue juste avant, jamais mise en cache.
    """
    payload = _request(tenant, f"/issue/{cle}/transitions")
    return [t for t in (payload.get("transitions") or []) if isinstance(t, dict)]


def _resolve_transition(
    tenant: Tenant, cle: str, demande: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Une transition -> (fiche, liste complete). A l'identique, ou refus."""
    brut = (demande or "").strip()
    dispo = _transitions_dispo(tenant, cle)
    if not dispo:
        raise JiraError(
            f"[{tenant.code}] {cle} n'offre aucune transition franchissable "
            "pour ce compte. Soit le ticket est dans un statut terminal, soit "
            "le compte n'a pas le droit de le faire avancer."
        )
    if not brut:
        offre = " | ".join(
            f"{t.get('name')} -> {_nested(t, 'to', 'name')} (id {t.get('id')})"
            for t in dispo
        )
        raise JiraError(
            f"transition est vide. Transitions franchissables sur {cle} "
            f"maintenant : {offre}."
        )
    for fiche in dispo:
        if str(fiche.get("id")) == brut:
            return fiche, dispo
    vise = _norm(brut)
    for fiche in dispo:
        if _norm(fiche.get("name")) == vise:
            return fiche, dispo
    # Un statut CIBLE plutot qu'un nom de transition : c'est la confusion la
    # plus frequente, et elle se rattrape sans deviner - le statut cible est
    # une donnee, pas une approximation.
    cibles = [f for f in dispo if _norm(_nested(f, "to", "name")) == vise]
    if len(cibles) == 1:
        return cibles[0], dispo
    offre = " | ".join(
        f"{t.get('name')} -> {_nested(t, 'to', 'name')} (id {t.get('id')})"
        for t in dispo
    )
    raise JiraError(
        f"Transition inconnue sur {cle} : « {brut} ». Le connecteur ne prend "
        f"pas la plus proche. Transitions franchissables maintenant : {offre}."
    )


# --- Les outils de resolution, exposes -----------------------------------


@mcp.tool()
@_guard
def jira_types_ticket(tenant: str, projet: str) -> str:
    """Les types de ticket REELS d'un projet, avec leur id.

    A appeler avant jira_creer_ticket. Un type de ticket est un id resolu sur
    l'instance, jamais un nom devine : les types varient d'un projet a
    l'autre, et un projet gere par l'equipe (team-managed) a les siens.

    Rend aussi la colonne `sous_tache` : une sous-tache exige un parent, et le
    connecteur refuse de la creer sans lui.
    """
    cible = _tenants(tenant, exiger_unique=True)[0]
    cle_projet = _cle_projet(projet)
    fiches = _types_projet(cible, cle_projet)
    rows = [
        {
            "tenant": cible.code,
            "projet": cle_projet,
            "id": fiche.get("id"),
            "nom": fiche.get("name"),
            "sous_tache": "oui" if fiche.get("subtask") else "non",
            "description": (fiche.get("description") or "")[:120],
        }
        for fiche in fiches
    ]
    return _render_table(
        rows,
        f"[{cible.code}] types de ticket du projet {cle_projet}",
        [],
        [f"{len(rows)} type(s)"],
        "",
    )


@mcp.tool()
@_guard
def jira_transitions(tenant: str, cle: str) -> str:
    """Les transitions franchissables pour CE ticket, maintenant.

    A appeler avant jira_transition_ticket. « Pour ce ticket » et non « pour
    ce projet » : le workflow, ses conditions et les droits du compte font
    qu'une transition peut exister sans etre franchissable ici et maintenant.

    En lecture seule : lister n'engage rien.
    """
    cible = _tenants(tenant, exiger_unique=True)[0]
    reference = _cle_ticket(cle)
    issue = _request(cible, f"/issue/{reference}", {"fields": "summary,status"})
    fields = issue.get("fields") or {}
    dispo = _transitions_dispo(cible, reference)
    rows = [
        {
            "tenant": cible.code,
            "id": fiche.get("id"),
            "transition": fiche.get("name"),
            "vers_statut": _nested(fiche, "to", "name"),
            "ecran": "oui" if fiche.get("hasScreen") else "non",
        }
        for fiche in dispo
    ]
    entete = (
        f"[{cible.code}] {reference} - statut actuel : "
        f"{_nested(fields, 'status', 'name') or '?'} | "
        f"{_texte_champ(fields.get('summary'))[:80]}"
    )
    return _render_table(
        rows,
        entete,
        [],
        [
            f"{len(rows)} transition(s) franchissable(s) maintenant",
            "« ecran = oui » : Jira demande des champs supplementaires dans "
            "l'interface. Le connecteur ne les remplit pas - si la transition "
            "echoue pour cette raison, elle se fait dans Jira.",
        ],
        "",
    )


# --- Les quatre gestes ---------------------------------------------------


@mcp.tool()
@_guard
def jira_creer_ticket(
    tenant: str,
    projet: str,
    type_ticket: str,
    titre: str,
    description: str = "",
    parent: str = "",
    assigne: str = "",
    priorite: str = "",
    etiquettes: str = "",
    champs_json: str = "{}",
    confirmer: bool = False,
) -> str:
    """Cree un ticket. Sans confirmer=True, montre ce qui partirait et s'arrete.

    `projet` est une CLE (SUPPLY, VUD), `type_ticket` un nom ou un id resolu
    sur le projet - jira_types_ticket les donne. `assigne` est un accountId ou
    « moi » ; un nom d'affichage est refuse, jira_user_lookup le resout.
    `parent` est la cle du ticket parent : obligatoire pour une sous-tache,
    accepte pour rattacher a un epic.

    NON IDEMPOTENT : deux appels confirmes creent DEUX tickets. Le serveur ne
    rejoue jamais cet appel, meme sur une coupure reseau - il le dit et laisse
    verifier.
    """
    # 1. Tout ce qui se verifie sans reseau, d'abord.
    cle_projet = _cle_projet(projet)
    sujet = (titre or "").strip()
    if not sujet:
        raise JiraError("titre vide : un ticket sans titre n'a pas d'usage.")
    if "\n" in sujet or "\r" in sujet:
        raise JiraError(
            "Un titre de ticket tient sur une ligne. Le detail va dans "
            "description, qui accepte les retours a la ligne."
        )
    if len(sujet) > 255:
        raise JiraError(
            f"titre trop long : {len(sujet)} caracteres, Jira en accepte 255. "
            "Resserre le titre et mets le detail dans description."
        )
    labels = _etiquettes(etiquettes)
    extra = _champs_libres(champs_json)
    cle_parent = _cle_ticket(parent) if (parent or "").strip() else ""

    # 2. Le reseau : resoudre ce qui ne se devine pas.
    cible = _tenants(tenant, exiger_unique=True)[0]
    fiche_type = _resolve_type_ticket(cible, cle_projet, type_ticket)
    if fiche_type.get("subtask") and not cle_parent:
        raise JiraError(
            f"« {fiche_type.get('name')} » est un type SOUS-TACHE dans "
            f"{cle_projet} : Jira exige un parent. Passe parent=\"CLE\" - la "
            "cle du ticket auquel elle se rattache."
        )
    compte, libelle_compte = _resolve_compte(cible, assigne)
    fiche_prio = _resolve_priorite(cible, priorite) if (priorite or "").strip() else {}

    fields: dict[str, Any] = {
        "project": {"key": cle_projet},
        "issuetype": {"id": str(fiche_type.get("id"))},
        "summary": sujet,
    }
    if (description or "").strip():
        fields["description"] = _adf_depuis_texte(description, "la description")
    if cle_parent:
        fields["parent"] = {"key": cle_parent}
    if compte:
        fields["assignee"] = {"id": compte}
    if fiche_prio:
        fields["priority"] = {"id": str(fiche_prio.get("id"))}
    if labels:
        fields["labels"] = labels
    fields.update(extra)
    corps = {"fields": fields}

    notes = [
        "NON IDEMPOTENT : chaque appel confirme cree un ticket de plus. En "
        "cas de doute apres une coupure, cherche le titre avec "
        "jira_recherche AVANT de relancer.",
        f"Type resolu : {fiche_type.get('name')} (id {fiche_type.get('id')}) "
        f"dans {cle_projet}.",
    ]
    if compte:
        notes.append(f"Affectation resolue : {libelle_compte}. Il sera notifie.")
    if not confirmer:
        return _apercu_ecriture(
            cible,
            "POST",
            "/issue",
            corps,
            f"creer un ticket {fiche_type.get('name')} dans {cle_projet}",
            notes,
        )

    cree = _request(cible, "/issue", None, "POST", corps)
    nouvelle = str(cree.get("key") or "")
    if not nouvelle:
        return _render_json(
            cree, f"[{cible.code}] ticket cree, mais sans cle dans la reponse"
        )
    lignes = [
        f"TICKET CREE : {nouvelle}",
        f"  instance   : {cible.code}",
        f"  projet     : {cle_projet}",
        f"  type       : {fiche_type.get('name')}",
        f"  titre      : {sujet}",
        f"  trace sous : {_signataire(cible)}",
        f"  url        : {cible.browse(nouvelle)}",
    ]
    if compte:
        lignes.append(f"  affecte a  : {libelle_compte}")
    lignes += [
        "",
        "Verifie-le dans Jira : le connecteur a envoye le corps affiche, il "
        "ne relit pas ce que les automatisations du projet ont pu changer "
        "juste apres la creation.",
    ]
    return "\n".join(lignes)


@mcp.tool()
@_guard
def jira_maj_ticket(
    tenant: str,
    cle: str,
    titre: str = "",
    description: str = "",
    assigne: str = "",
    priorite: str = "",
    echeance: str = "",
    etiquettes_ajout: str = "",
    etiquettes_retrait: str = "",
    champs_json: str = "{}",
    confirmer: bool = False,
) -> str:
    """Met a jour les champs d'un ticket. Ne touche PAS son statut.

    Un statut ne s'ecrit pas, il se franchit : jira_transition_ticket.

    ECRASEMENT. Un champ pose ici REMPLACE la valeur existante - sur une
    description, tout l'existant part. L'apercu affiche donc la taille de ce
    qui serait ecrase avant de confirmer. Les etiquettes, elles, sont
    incrementales : `etiquettes_ajout` et `etiquettes_retrait` n'affectent que
    celles nommees, les autres restent.

    `echeance` s'ecrit AAAA-MM-JJ. Une chaine vide laisse le champ tranquille ;
    le mot « vider » l'efface.
    """
    # 1. Local d'abord.
    reference = _cle_ticket(cle)
    extra = _champs_libres(champs_json)
    ajouts = _etiquettes(etiquettes_ajout, "etiquettes_ajout")
    retraits = _etiquettes(etiquettes_retrait, "etiquettes_retrait")
    doublon = sorted(set(ajouts) & set(retraits))
    if doublon:
        raise JiraError(
            f"Ces etiquettes sont a la fois ajoutees et retirees : "
            f"{', '.join(doublon)}. Tranche - Jira appliquerait les deux "
            "gestes dans un ordre qui n'est pas garanti."
        )
    nouveau_titre = (titre or "").strip()
    if nouveau_titre and ("\n" in nouveau_titre or "\r" in nouveau_titre):
        raise JiraError("Un titre de ticket tient sur une ligne.")
    if len(nouveau_titre) > 255:
        raise JiraError(
            f"titre trop long : {len(nouveau_titre)} caracteres, Jira en "
            "accepte 255."
        )
    date_echeance = (echeance or "").strip()
    vider_echeance = _norm(date_echeance) in {"vider", "vide", "aucune", "supprimer"}
    if date_echeance and not vider_echeance:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_echeance):
            raise JiraError(
                f"echeance « {date_echeance} » n'est pas une date. Jira "
                "attend AAAA-MM-JJ. Le connecteur n'interprete pas « la "
                "semaine prochaine » sur une ECRITURE : une date fausse dans "
                "un ticket devient une date fausse dans un mail."
            )
    demande_quelque_chose = any(
        [
            nouveau_titre,
            (description or "").strip(),
            (assigne or "").strip(),
            (priorite or "").strip(),
            date_echeance,
            ajouts,
            retraits,
            extra,
        ]
    )
    if not demande_quelque_chose:
        raise JiraError(
            "Aucune modification demandee : tous les parametres sont vides. "
            "Rien n'a ete appele - un PUT vide serait un appel pour rien."
        )

    # 2. Le reseau : relire l'existant AVANT de proposer de l'ecraser.
    cible = _tenants(tenant, exiger_unique=True)[0]
    actuel = _request(
        cible,
        f"/issue/{reference}",
        {
            "fields": (
                "summary,description,labels,assignee,priority,duedate,status,"
                "issuetype"
            )
        },
    )
    champs_actuels = actuel.get("fields") or {}
    desc_actuelle = _adf_text(champs_actuels.get("description"))
    labels_actuels = [str(x) for x in (champs_actuels.get("labels") or [])]

    compte, libelle_compte = _resolve_compte(cible, assigne)
    fiche_prio = _resolve_priorite(cible, priorite) if (priorite or "").strip() else {}

    fields: dict[str, Any] = {}
    update: dict[str, Any] = {}
    change: list[str] = []
    if nouveau_titre:
        fields["summary"] = nouveau_titre
        change.append(
            f"titre : « {_texte_champ(champs_actuels.get('summary'))[:60]} » -> "
            f"« {nouveau_titre[:60]} »"
        )
    if (description or "").strip():
        fields["description"] = _adf_depuis_texte(description, "la description")
        change.append(
            f"description : ECRASEMENT de {len(desc_actuelle)} caractere(s) "
            f"existant(s) par {len((description or '').strip())}"
        )
    if compte:
        fields["assignee"] = {"id": compte}
        change.append(
            f"affectation : {_nested(champs_actuels, 'assignee', 'displayName') or 'personne'}"
            f" -> {libelle_compte}"
        )
    if fiche_prio:
        fields["priority"] = {"id": str(fiche_prio.get("id"))}
        change.append(
            f"priorite : {_nested(champs_actuels, 'priority', 'name') or '?'} -> "
            f"{fiche_prio.get('name')}"
        )
    if vider_echeance:
        fields["duedate"] = None
        change.append(
            f"echeance : {champs_actuels.get('duedate') or 'aucune'} -> effacee"
        )
    elif date_echeance:
        fields["duedate"] = date_echeance
        change.append(
            f"echeance : {champs_actuels.get('duedate') or 'aucune'} -> {date_echeance}"
        )
    if ajouts or retraits:
        gestes = [{"add": e} for e in ajouts] + [{"remove": e} for e in retraits]
        update["labels"] = gestes
        change.append(
            f"etiquettes : {', '.join(labels_actuels) or 'aucune'} | "
            f"ajout {ajouts or 'aucun'}, retrait {retraits or 'aucun'} "
            "(les autres restent)"
        )
    for nom, valeur in extra.items():
        fields[nom] = valeur
        change.append(f"{nom} : pose a {json.dumps(valeur, ensure_ascii=False)}")

    corps: dict[str, Any] = {}
    if fields:
        corps["fields"] = fields
    if update:
        corps["update"] = update

    entete = (
        f"{reference} - {_texte_champ(champs_actuels.get('summary'))[:70]} "
        f"[{_nested(champs_actuels, 'status', 'name') or '?'}]"
    )
    notes = [f"Ticket vise : {entete}"] + [f"  {c}" for c in change]
    if "description" in fields and desc_actuelle:
        notes.append(
            "La description actuelle sera PERDUE - Jira n'en garde que "
            "l'historique. Relis-la avec jira_issue avant de confirmer si "
            "elle porte du contenu."
        )
    if not confirmer:
        return _apercu_ecriture(
            cible,
            "PUT",
            f"/issue/{reference}",
            corps,
            f"mettre a jour {reference}",
            notes,
        )

    _request(cible, f"/issue/{reference}", None, "PUT", corps)
    lignes = [f"TICKET MIS A JOUR : {reference}"]
    lignes += [f"  {c}" for c in change]
    lignes += [
        f"  trace sous : {_signataire(cible)}",
        f"  url        : {cible.browse(reference)}",
        "",
        "Jira a repondu sans corps (204) : c'est sa reponse normale a une mise "
        "a jour reussie. Relis le ticket avec jira_issue pour voir l'etat "
        "final, automatisations comprises.",
    ]
    return "\n".join(lignes)


@mcp.tool()
@_guard
def jira_commenter_ticket(
    tenant: str, cle: str, texte: str, confirmer: bool = False
) -> str:
    """Ajoute un commentaire a un ticket. Le texte est converti en ADF.

    NON IDEMPOTENT : deux appels confirmes posent DEUX commentaires. Le
    serveur ne rejoue jamais cet appel.

    Un commentaire est visible de tous ceux qui suivent le ticket, et part en
    notification. C'est un message a des gens, pas une note privee.
    """
    reference = _cle_ticket(cle)
    corps_adf = _adf_depuis_texte(texte, "le commentaire")
    corps = {"body": corps_adf}
    cible = _tenants(tenant, exiger_unique=True)[0]
    actuel = _request(cible, f"/issue/{reference}", {"fields": "summary,status"})
    champs_actuels = actuel.get("fields") or {}
    apercu = (texte or "").strip()
    notes = [
        f"Ticket vise : {reference} - "
        f"{_texte_champ(champs_actuels.get('summary'))[:70]} "
        f"[{_nested(champs_actuels, 'status', 'name') or '?'}]",
        "NON IDEMPOTENT : chaque appel confirme pose un commentaire de plus.",
        "Un commentaire part en notification aux personnes qui suivent le "
        "ticket. Il ne se supprime pas par ce connecteur : un commentaire "
        "faux se corrige par un commentaire qui rectifie.",
        f"Texte tel qu'il sera lu ({len(apercu)} caracteres) : "
        f"{apercu[:300]}{'...' if len(apercu) > 300 else ''}",
    ]
    if not confirmer:
        return _apercu_ecriture(
            cible,
            "POST",
            f"/issue/{reference}/comment",
            corps,
            f"commenter {reference}",
            notes,
        )

    pose = _request(cible, f"/issue/{reference}/comment", None, "POST", corps)
    return "\n".join(
        [
            f"COMMENTAIRE POSE sur {reference}",
            f"  id         : {pose.get('id') or '?'}",
            f"  auteur     : {_nested(pose, 'author', 'displayName') or '?'}",
            f"  date       : {_short_date(pose.get('created'))}",
            f"  trace sous : {_signataire(cible)}",
            f"  url        : {cible.browse(reference)}",
        ]
    )


@mcp.tool()
@_guard
def jira_transition_ticket(
    tenant: str,
    cle: str,
    transition: str,
    commentaire: str = "",
    confirmer: bool = False,
) -> str:
    """Fait franchir une transition a un ticket - la SEULE facon de bouger un statut.

    `transition` est un nom de transition, son id, ou le statut cible s'il ne
    correspond qu'a une seule transition. Un libelle approchant est REFUSE
    avec la liste des transitions franchissables : jira_transitions la donne.

    LE GESTE LE PLUS ENGAGEANT DE CE CONNECTEUR. Un statut qui bouge est vu de
    toute l'equipe, peut declencher des automatisations Jira et des
    notifications, et vaut souvent avancement dans un point de suivi.
    """
    reference = _cle_ticket(cle)
    corps_commentaire = (
        _adf_depuis_texte(commentaire, "le commentaire")
        if (commentaire or "").strip()
        else None
    )
    cible = _tenants(tenant, exiger_unique=True)[0]
    actuel = _request(cible, f"/issue/{reference}", {"fields": "summary,status"})
    champs_actuels = actuel.get("fields") or {}
    statut_actuel = _nested(champs_actuels, "status", "name") or "?"
    fiche, dispo = _resolve_transition(cible, reference, transition)

    corps: dict[str, Any] = {"transition": {"id": str(fiche.get("id"))}}
    if corps_commentaire is not None:
        corps["update"] = {"comment": [{"add": {"body": corps_commentaire}}]}

    cible_statut = _nested(fiche, "to", "name") or "?"
    notes = [
        f"Ticket vise : {reference} - "
        f"{_texte_champ(champs_actuels.get('summary'))[:70]}",
        f"Statut      : {statut_actuel}  ->  {cible_statut}",
        f"Transition  : {fiche.get('name')} (id {fiche.get('id')}), resolue "
        "sur les transitions franchissables a l'instant.",
        "Une transition peut declencher des automatisations et des "
        "notifications cote Jira, et elle est visible de toute l'equipe.",
    ]
    if fiche.get("hasScreen"):
        notes.append(
            "Cette transition affiche un ECRAN dans Jira : elle peut exiger "
            "des champs que le connecteur ne remplit pas. Si Jira la refuse "
            "pour cette raison, fais-la dans l'interface."
        )
    if corps_commentaire is not None:
        notes.append("Un commentaire sera pose dans le meme geste.")
    if not confirmer:
        notes.append(
            "Autres transitions possibles : "
            + " | ".join(
                f"{t.get('name')} -> {_nested(t, 'to', 'name')}"
                for t in dispo
                if str(t.get("id")) != str(fiche.get("id"))
            )
        )
        return _apercu_ecriture(
            cible,
            "POST",
            f"/issue/{reference}/transitions",
            corps,
            f"faire passer {reference} de {statut_actuel} a {cible_statut}",
            notes,
        )

    _request(cible, f"/issue/{reference}/transitions", None, "POST", corps)
    relu = _request(cible, f"/issue/{reference}", {"fields": "status"})
    statut_final = _nested(relu.get("fields") or {}, "status", "name") or "?"
    lignes = [
        f"TRANSITION FRANCHIE : {reference}",
        f"  transition : {fiche.get('name')}",
        f"  statut     : {statut_actuel} -> {statut_final}",
        f"  trace sous : {_signataire(cible)}",
        f"  url        : {cible.browse(reference)}",
    ]
    if _norm(statut_final) != _norm(cible_statut):
        lignes += [
            "",
            f"ATTENTION : le statut final ({statut_final}) n'est pas celui "
            f"annonce par la transition ({cible_statut}). Ce n'est pas une "
            "erreur du connecteur - une automatisation du projet a "
            "probablement enchaine. Regarde jira_changelog.",
        ]
    return "\n".join(lignes)


# --- Export et echappatoire ---------------------------------------------


@mcp.tool()
@_guard
def jira_export_csv(
    tenant: str = "",
    jql: str = "",
    projet: str = "",
    texte: str = "",
    titre: str = "",
    type_ticket: str = "",
    statut: str = "",
    assigne: str = "",
    epic: str = "",
    etiquette: str = "",
    cree_depuis: str = "",
    cree_jusqua: str = "",
    maj_depuis: str = "",
    resolu_depuis: str = "",
    ordre: str = "",
    champs: str = "",
    filename: str = "",
    max_rows: int = 20000,
) -> str:
    """Exporte en CSV le perimetre exact de la question posee. SUR DEMANDE EXPLICITE.

    Prend les MEMES filtres que jira_recherche : la question posee en francais
    se traduit une fois, et l'export recoit le meme perimetre - pas seulement
    les lignes affichees dans la conversation. Il pagine la collection entiere.

    N'appelle cet outil QUE si l'utilisateur a demande un fichier, un export ou
    un CSV. Il ecrit sur le disque : ce n'est pas une etape de routine, et un
    perimetre trop gros pour la conversation ne le justifie pas.

    CSV point-virgule, UTF-8 avec BOM : il s'ouvre dans Excel FR sans passer par
    l'assistant d'import. Le fichier atterrit dans le dossier d'export LOCAL,
    jamais dans la bibliotheque d'equipe.
    """
    traductions: list[str] = []
    if jql.strip():
        requete = jql.strip()
    else:
        requete, traductions = _build_jql(
            projet=projet,
            texte=texte,
            titre=titre,
            type_ticket=type_ticket,
            statut=statut,
            assigne=assigne,
            epic=epic,
            etiquette=etiquette,
            cree_depuis=cree_depuis,
            cree_jusqua=cree_jusqua,
            maj_depuis=maj_depuis,
            resolu_depuis=resolu_depuis,
            ordre=ordre or "cle",
        )
    cibles = _tenants(tenant)
    extra = ",".join(
        c.strip()
        for c in (champs or "").split(",")
        if c.strip().startswith("customfield_")
    )
    demande = ISSUE_FIELDS_BRIEF + ("," + extra if extra else "")
    pairs, truncated, notes = _collect(
        cibles, requete, demande, max(1, int(max_rows))
    )
    rows = [_flatten(_issue_row(t, issue, extra)) for t, issue in pairs]
    if champs.strip() and not extra:
        rows = _select(rows, champs)

    target_dir = _export_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JiraError(
            f"Dossier d'export inutilisable ({target_dir}) : {exc}"
        ) from exc

    if filename.strip():
        name = pathlib.Path(filename.strip()).name
        if not name.lower().endswith(".csv"):
            name += ".csv"
    else:
        marque = "_".join(c.code.lower() for c in cibles)
        name = f"jira_{marque}_{dt.datetime.now().strftime('%Y-%m-%d_%H%M%S')}.csv"
    target = target_dir / name

    try:
        target.write_text(_to_csv_text(rows), encoding="utf-8-sig")
    except OSError as exc:
        raise JiraError(f"Ecriture impossible dans {target} : {exc}") from exc

    cols = _columns(rows)
    out = [
        f"Export termine : {target}",
        f"{len(rows)} ligne(s), {len(cols)} colonne(s).",
        f"JQL : {requete}",
        "Instances : " + ", ".join(c.code for c in cibles),
        "Par instance : " + " | ".join(notes),
    ]
    if traductions:
        out.append("Traduit : " + " ; ".join(traductions))
    if truncated:
        out.append(
            "ATTENTION : export TRONQUE sur "
            + ", ".join(truncated)
            + ". Le fichier n'est PAS le perimetre complet : resserre les "
            "dates, ou releve max_rows et JIRA_MAX_PAGES. Un export tronque "
            "sans le dire, c'est un chiffre faux qui part dans un mail."
        )
    else:
        out.append("Perimetre COMPLET : la pagination est allee au bout.")
    if any("ECHEC" in n for n in notes):
        out.append(
            "ATTENTION : au moins une instance n'a pas repondu. Le fichier ne "
            "couvre pas tout le perimetre demande."
        )
    if cols:
        out.append("")
        out.append("Colonnes : " + ", ".join(cols[:60]))
    return "\n".join(out)


@mcp.tool()
@_guard
def jira_get(
    tenant: str,
    path: str,
    query_json: str = "{}",
    max_rows: int = 200,
    champs: str = "",
) -> str:
    """Appelle n'importe quel chemin GET autorise de l'API Jira. Echappatoire.

    Pour ce qui n'a pas d'outil dedie. Appelle jira_list_paths d'abord, pour le
    chemin exact et ses parametres.

    Un chemin absent de la liste blanche est refuse, et seule la methode GET est
    emise : aucune ecriture n'est possible par cet outil.
    """
    cible = _tenants(tenant, exiger_unique=True)[0]
    checked, _template = _check_get_path(path)
    try:
        query = json.loads(query_json or "{}")
    except ValueError as exc:
        raise JiraError(f"query_json n'est pas du JSON valide : {exc}") from exc
    if not isinstance(query, dict):
        raise JiraError("query_json doit etre un objet JSON.")

    payload = _request(cible, checked, query)
    header = (
        f"[{cible.code}] GET {API_ROOT}{checked} | filtres : "
        + json.dumps(_clean_query(query), ensure_ascii=False)
    )
    # Une collection se rend en tableau, un objet en JSON : les deux formes
    # existent selon le chemin, et un tableau se lit dix fois mieux.
    rows: list[dict[str, Any]] | None = None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        rows = [{"tenant": cible.code, **_flatten(r)} for r in payload[:max_rows]]
    elif isinstance(payload, dict):
        for key in ("values", "issues", "comments", "results"):
            valeur = payload.get(key)
            if isinstance(valeur, list) and valeur and isinstance(valeur[0], dict):
                rows = [
                    {"tenant": cible.code, **_flatten(r)} for r in valeur[:max_rows]
                ]
                break
    if rows is not None:
        return _render_table(rows, header, [], [f"{len(rows)} ligne(s)"], champs)
    return _render_json(payload, header)


# --------------------------------------------------------------------------
# Le cache local : l'historique sans reconsommer le quota
# --------------------------------------------------------------------------

def _sync_hint() -> str:
    return (
        "Rapatrie d'abord un perimetre avec jira_sync, par exemple "
        "jira_sync(tenant=\"PROJET\", projet=\"SUPPLY\", cree_depuis=\"2026-01-01\"). "
        "Le cache se relit ensuite en SQL, instantanement et sans quota."
    )


@mcp.tool()
@_guard
def jira_sync(
    tenant: str = "",
    projet: str = "",
    jql: str = "",
    statut: str = "",
    cree_depuis: str = "",
    maj_depuis: str = "",
    table: str = "issues",
    champs: str = "",
    max_rows: int = 20000,
) -> str:
    """Rapatrie un perimetre de tickets dans le cache local SQLite.

    A faire une fois pour un historique qu'on va interroger plusieurs fois :
    « l'evolution des incidents VUD sur douze mois », « le temps de traitement
    par prestation ». Ensuite, jira_sql repond instantanement, sans requete a
    Jira et sans consommer le quota.

    La cle de ligne est tenant + cle de ticket : une resynchronisation est donc
    toujours juste et ne cree jamais de doublon. En cas de doute, relance.
    """
    if jql.strip():
        requete = jql.strip()
        traductions: list[str] = []
    else:
        requete, traductions = _build_jql(
            projet=projet,
            statut=statut,
            cree_depuis=cree_depuis,
            maj_depuis=maj_depuis,
            ordre="cle",
        )
    cibles = _tenants(tenant)
    extra = ",".join(
        c.strip()
        for c in (champs or "").split(",")
        if c.strip().startswith("customfield_")
    )
    demande = ISSUE_FIELDS_BRIEF + ("," + extra if extra else "")
    nom = cache.table_name(table)

    conn = cache.open_rw(_cache_path())
    lines = [f"Cache : {_cache_path()}", f"JQL : {requete}"]
    if traductions:
        lines.append("Traduit : " + " ; ".join(traductions))
    lines.append("")
    total = 0
    try:
        for cible in cibles:
            try:
                issues, stop = _search(
                    cible, requete, demande, max(1, int(max_rows))
                )
            except (ConfigError, JiraError) as exc:
                lines.append(f"[{cible.code}] ECHEC : {exc}")
                continue
            rows = [_flatten(_issue_row(cible, issue, extra)) for issue in issues]
            ecrites = cache.upsert(
                conn,
                nom,
                rows,
                key_of=lambda r: f"{r.get('tenant')}:{r.get('cle')}",
                numeric=NUMERIC_COLUMNS,
                index_on=("tenant", "projet", "statut", "type", "assigne", "cree"),
            )
            cache.meta_set(
                conn,
                f"last_sync:{nom}:{cible.code}",
                dt.datetime.now().isoformat(timespec="seconds"),
            )
            cache.meta_set(conn, f"scope:{nom}:{cible.code}", requete)
            total += ecrites
            lines.append(
                f"[{cible.code}] {ecrites} ticket(s) ecrit(s)"
                + (f" - lecture INCOMPLETE ({stop})" if stop else " - lecture complete")
            )
        conn.commit()
    finally:
        conn.close()

    lines.append("")
    lines.append(f"Total ecrit : {total} ligne(s) dans la table [{nom}].")
    lines.append(
        "Le cache n'est PAS l'etat courant de JIRA : un ticket cree ou deplace "
        "depuis la synchro n'y est pas, et un statut lu ici peut etre perime. "
        "Pour l'etat du jour, utilise jira_recherche."
    )
    lines.append("Ce qu'il contient : jira_tables. Ses colonnes : jira_columns.")
    return "\n".join(lines)


@mcp.tool()
@_guard
def jira_tables() -> str:
    """Ce que le cache local contient : tables, volumes, perimetre et date de synchro."""
    try:
        conn = cache.open_ro(_cache_path(), _sync_hint())
    except cache.CacheError as exc:
        raise JiraError(str(exc)) from exc
    try:
        names = cache.tables(conn)
        if not names:
            return "Cache vide. " + _sync_hint()
        lines = [f"Cache local : {_cache_path()}", ""]
        for table in names:
            total = conn.execute(
                f"SELECT COUNT(*) FROM {cache.quote(table)}"
            ).fetchone()[0]
            lines.append(
                f"[{table}] {total} ligne(s), "
                f"{len(cache.columns(conn, table))} colonne(s)"
            )
            if "tenant" in cache.columns(conn, table):
                for row in conn.execute(
                    f"SELECT tenant, COUNT(*) FROM {cache.quote(table)} GROUP BY 1"
                ):
                    lines.append(f"    tenant {row[0]:<8} {row[1]} ligne(s)")
            for row in conn.execute(
                "SELECT key, value FROM _meta WHERE key LIKE ?",
                (f"last_sync:{table}:%",),
            ):
                lines.append(f"    synchro {row['key'].split(':')[-1]:<8} {row['value']}")
            for row in conn.execute(
                "SELECT key, value FROM _meta WHERE key LIKE ?",
                (f"scope:{table}:%",),
            ):
                lines.append(
                    f"    perimetre {row['key'].split(':')[-1]:<6} {row['value']}"
                )
        lines.append("")
        lines.append(
            "Le perimetre compte autant que le volume : une table synchronisee "
            "sur « SUPPLY depuis janvier » ne repond pas a une question sur VUD, "
            "et elle rendrait zero sans erreur."
        )
        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
@_guard
def jira_columns(table: str = "issues") -> str:
    """Colonnes d'une table du cache, avec leur taux de remplissage.

    A lire avant d'ecrire du SQL. Le taux de remplissage n'est pas un detail :
    grouper par un champ present sur 30 % des lignes rend une repartition qui a
    l'air complete.
    """
    try:
        conn = cache.open_ro(_cache_path(), _sync_hint())
    except cache.CacheError as exc:
        raise JiraError(str(exc)) from exc
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
            lines.append(
                f"  {col:<40} {kind}  {100 * (filled or 0) / total:5.1f}% rempli"
            )
        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
@_guard
def jira_sql(sql: str, max_rows: int = 100) -> str:
    """Interroge le cache local en SQL (SQLite, lecture seule).

    Pour ce que l'API ne sait pas faire : un historique long, un croisement, une
    evolution mois par mois, un delai moyen de resolution. Instantane, et sans
    quota.

    Exemple :
      SELECT tenant, projet, substr(cree,1,7) mois, COUNT(*) nb,
             ROUND(AVG(age_jours),1) age_moyen
      FROM issues GROUP BY 1,2,3 ORDER BY mois DESC
    """
    try:
        clean = cache.guard_sql(sql)
        conn = cache.open_ro(_cache_path(), _sync_hint())
    except cache.CacheError as exc:
        raise JiraError(str(exc)) from exc
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
                "total - c'est celui-ci qui l'est. Releve max_rows, ou agrege "
                "dans la requete."
            )
        lines.append("")
        lines.append(cache.to_csv_text(rows))
        return "\n".join(lines)
    except cache.CacheError as exc:
        raise JiraError(str(exc)) from exc
    finally:
        conn.close()


@mcp.tool()
@_guard
def jira_export_sql(sql: str, filename: str = "", max_rows: int = 500000) -> str:
    """Exporte en CSV le resultat d'une requete sur le cache. SUR DEMANDE.

    C'est l'export sans plafond de pagination et sans quota : il lit le cache,
    pas l'API. A n'appeler que si l'utilisateur a demande un fichier.
    """
    try:
        clean = cache.guard_sql(sql)
        conn = cache.open_ro(_cache_path(), _sync_hint())
    except cache.CacheError as exc:
        raise JiraError(str(exc)) from exc
    try:
        cur = conn.execute(clean)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(int(max_rows))
        reste = cur.fetchone() is not None
    except Exception as exc:
        raise JiraError(f"SQL refuse par SQLite : {exc}") from exc
    finally:
        conn.close()

    target_dir = _export_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JiraError(
            f"Dossier d'export inutilisable ({target_dir}) : {exc}"
        ) from exc
    name = (filename or "").strip() or (
        "jira_sql_" + dt.datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".csv"
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
        raise JiraError(f"Ecriture impossible dans {target} : {exc}") from exc

    out = [
        f"Export termine : {target}",
        f"{len(rows)} ligne(s), {len(cols)} colonne(s).",
    ]
    if reste:
        out.append(
            f"ATTENTION : la requete rendait PLUS de {max_rows} lignes, le "
            "fichier est tronque."
        )
    else:
        out.append("Resultat complet : la requete ne rendait pas plus de lignes.")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

HELP = """\
MCP jira - interrogation des deux instances Atlassian de Vente-unique -
vuproject (metier) et webfacto (developpement) - et ecriture bornee a quatre
gestes, jamais sans confirmer=True.

  python server.py            mode serveur MCP (stdio), lance par Claude Code
  python server.py doctor     diagnostic de configuration et de connexion
  python server.py --help     cette aide

Configuration, en deux couches :
  - l'equipe : 08_ENGINE/04_mcp/00_config/jira.shared.env, sur le drive
    partage. Tenants, sites, plafonds, ET le couple courriel + jeton du compte
    de SERVICE (decision du 2026-09-01). Restent refuses : les deux secrets
    oauth, qui n'ont qu'un detenteur possible.
  - le poste : jira.env hors du vault, par defaut ~/.jira-mcp/jira.env.
    Modele : jira.env.example.
Priorite : configuration du plugin > jira.env du poste > fichier d'equipe >
defaut du serveur. Voir README.md.
"""


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if arg == "doctor":
        report = jira_doctor()
        print(report)
        return 0 if ": OK |" in report else 1
    if arg:
        print(f"Argument inconnu : {arg}\n")
        print(HELP)
        return 2
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
