#!/usr/bin/env python3
"""Acquisition des taux de TVA : deux sources, une seule forme de ligne.

Ce module ne rend AUCUN taux de lui-meme. Il appelle deux sources publiques et
les normalise vers la forme de ligne que le reste du serveur sait afficher.

    TEDB     les 27 Etats membres + XI (Irlande du Nord).
             Base officielle de la Commission europeenne, alimentee par les
             Etats membres eux-memes. Service SOAP. C'est LA reference.
             Rend les categories de biens, les codes de nomenclature douaniere
             (CN), les commentaires officiels et la date d'effet de chaque taux.

    vatnode  les 18 juridictions hors Union (CH, GB, NO, IS, LI, TR, et les
             Balkans), plus LA DEVISE de tout le monde - TEDB n'en rend aucune.
             Depot GitHub sous licence MIT, servi par CDN, sans cle.

Pourquoi deux sources et pas une. Aucune des deux ne suffit :

  - TEDB s'arrete aux Etats membres. La Suisse, la Norvege et le Royaume-Uni,
    ou le groupe vend, n'y sont pas et n'y seront jamais.
  - vatnode couvre 45 juridictions mais ne porte ni categorie, ni code CN, ni
    commentaire, ni date d'effet. S'en servir pour les 27 serait une
    REGRESSION par rapport a ce que le connecteur savait deja faire.

Et surtout : la provenance des deux moities n'est pas la meme, donc le rendu
ne doit pas les presenter de la meme facon. Pour les 27, TEDB est une source
officielle. Pour les 18 autres, vatnode annonce lui-meme des taux « maintained
manually from national sources » - la saisie d'un mainteneur, pas un flux
officiel. Chaque ligne porte donc sa `provenance`, et le rendu la montre.

DEUX PIEGES DE CODE, traites ici et nulle part ailleurs.

1. La Grece. TEDB la code EL, vatnode la code GR. Un rapprochement fait sans
   traduction produit deux pays la ou il y en a un, ou en perd un en silence.
   `_VATNODE_VERS_TEDB` traduit vers EL, la convention de l'Union, qui est
   aussi celle du referentiel local du connecteur.

2. La devise n'est pas une constante. La Bulgarie est passee a l'euro le
   1er janvier 2026. Graver les devises dans le referentiel aurait fabrique un
   fait faux au premier changement suivant : elles viennent de vatnode, qui
   est date et verifie chaque jour.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any
from xml.etree import ElementTree as ET

import httpx

# --------------------------------------------------------------------------
# TEDB - Taxes in Europe Database, Commission europeenne
# --------------------------------------------------------------------------

TEDB_ENDPOINT = "https://ec.europa.eu/taxation_customs/tedb/ws/"
TEDB_WSDL = "https://ec.europa.eu/taxation_customs/tedb/ws/VatRetrievalService.wsdl"
TEDB_ACTION = (
    "urn:ec.europa.eu:taxud:tedb:services:v1:VatRetrievalService/RetrieveVatRates"
)
_NS = "urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService"
_NST = _NS + ":types"
_T = "{" + _NST + "}"

# Les 27 Etats membres, plus XI. XI n'est pas un Etat membre : c'est l'Irlande
# du Nord, qui reste dans le champ TVA de l'Union pour les biens depuis le
# protocole nord-irlandais. TEDB la sert, et une livraison a Belfast ne se
# traite pas comme une livraison en Grande-Bretagne - d'ou sa presence ici.
TEDB_JURIDICTIONS = (
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
    "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO",
    "SE", "SI", "SK", "XI",
)

# --------------------------------------------------------------------------
# vatnode - depot MIT, servi par CDN
# --------------------------------------------------------------------------

VATNODE_URL = (
    "https://cdn.jsdelivr.net/gh/vatnode/eu-vat-rates-data@main/"
    "data/eu-vat-rates-data.json"
)
VATNODE_MIROIR = (
    "https://raw.githubusercontent.com/vatnode/eu-vat-rates-data/main/"
    "data/eu-vat-rates-data.json"
)

# Piege n° 1 : vatnode normalise la Grece en GR, TEDB et l'Union utilisent EL.
_VATNODE_VERS_TEDB = {"GR": "EL"}

RETRY_ATTEMPTS = 3
RETRY_STATUSES = (500, 502, 503, 504)
RETRY_BACKOFF_S = 1.5


class SourceError(RuntimeError):
    """Une source n'a pas repondu, ou a repondu autre chose qu'attendu."""


# --------------------------------------------------------------------------
# Outillage commun
# --------------------------------------------------------------------------

def _http(method: str, url: str, timeout: float, **kw) -> httpx.Response:
    """Un appel, rejoue sur panne passagere seulement.

    Un 429 veut dire « ralentis » : insister est impoli et inutile. Le cache du
    serveur repond a la place, et le dit.
    """
    last = ""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = httpx.request(
                method, url, timeout=timeout, follow_redirects=True, **kw
            )
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__} : {exc}"
        else:
            if r.status_code in RETRY_STATUSES:
                last = f"HTTP {r.status_code}"
            elif r.status_code == 429:
                raise SourceError(
                    f"{url} repond 429 (trop de requetes). On ne rejoue pas : "
                    "la source demande a etre laissee tranquille."
                )
            elif r.status_code >= 400:
                raise SourceError(
                    f"{method} {url} : HTTP {r.status_code}. {r.text[:300]}"
                )
            else:
                return r
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_S * attempt)
    raise SourceError(f"{method} {url} injoignable apres {RETRY_ATTEMPTS} tentatives : {last}")


def _jour(valeur: Any) -> str:
    """TEDB rend « 2025-01-01+01:00 » : une date avec un decalage horaire.

    date.fromisoformat ne l'avale pas. On garde les dix premiers caracteres
    plutot que de fabriquer une date fausse en tentant de parser le reste.
    """
    texte = str(valeur or "")
    return texte[:10] if len(texte) >= 10 else ""


# --------------------------------------------------------------------------
# TEDB : l'appel et le depouillement
# --------------------------------------------------------------------------

def enveloppe_tedb(codes: tuple[str, ...] | list[str], situation: str) -> str:
    """Le corps SOAP. Document/literal, elements qualifies dans le namespace
    « types » - c'est ce que dit VatRetrievalServiceType.xsd."""
    iso = "".join(f"<t:isoCode>{c}</t:isoCode>" for c in codes)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:n="{_NS}" xmlns:t="{_NST}">'
        "<s:Body><n:retrieveVatRatesReqMsg>"
        f"<t:memberStates>{iso}</t:memberStates>"
        f"<t:situationOn>{situation}</t:situationOn>"
        "</n:retrieveVatRatesReqMsg></s:Body></s:Envelope>"
    )


def depouille_tedb(xml: bytes) -> list[dict[str, Any]]:
    """Depouille la reponse SOAP vers la forme de ligne canonique.

    TEDB rend UNE LIGNE PAR TAUX ET PAR CATEGORIE - 1 125 lignes pour 28
    juridictions. Le serveur, lui, affiche un pays par ligne. Le
    regroupement se fait donc ici, et c'est le seul endroit qui connait la
    forme SOAP.

    Le type de taux se lit dans `rate/type`, pas dans `type` : `type` ne dit
    que STANDARD ou REDUCED, alors que `rate/type` distingue le taux normal,
    le reduit, le super-reduit, le parking et les exemptions.
    """
    try:
        racine = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SourceError(
            f"TEDB : reponse illisible, ce n'est pas du XML valide ({exc}). "
            "Le service a peut-etre change de forme."
        ) from exc

    fautes = [
        (e.findtext(_T + "code", ""), e.findtext(_T + "description", ""))
        for e in racine.iter(_T + "error")
    ]
    if fautes:
        detail = " ; ".join(f"{c} {d}" for c, d in fautes)
        raise SourceError(f"TEDB a repondu une erreur : {detail}")

    par_pays: dict[str, dict[str, Any]] = {}
    for res in racine.iter(_T + "vatRateResults"):
        code = (res.findtext(_T + "memberState") or "").strip().upper()
        if not code:
            continue
        bloc = res.find(_T + "rate")
        if bloc is None:
            continue
        genre = (bloc.findtext(_T + "type") or "").strip()
        brut = bloc.findtext(_T + "value")
        try:
            valeur = float(brut) if brut not in (None, "") else None
        except ValueError:
            valeur = None
        effet = _jour(res.findtext(_T + "situationOn"))

        p = par_pays.setdefault(
            code,
            {
                "country_code": code,
                "standard_rate": None,
                "reduced_set": set(),
                "super_reduced_rate": None,
                "parking_rate": None,
                "rate_categories": {},
                "rate_comments": {},
                "cn_codes": {},
                "exemptions": set(),
                "effective_on": "",
                "lignes_tedb": 0,
            },
        )
        p["lignes_tedb"] += 1

        if genre == "DEFAULT" and valeur is not None:
            p["standard_rate"] = valeur
            p["effective_on"] = effet
        elif genre == "REDUCED_RATE" and valeur is not None:
            p["reduced_set"].add(valeur)
        elif genre == "SUPER_REDUCED_RATE" and valeur is not None:
            p["super_reduced_rate"] = valeur
        elif genre == "PARKING_RATE" and valeur is not None:
            p["parking_rate"] = valeur

        cat = res.find(_T + "category")
        if cat is not None:
            ident = (cat.findtext(_T + "identifier") or "").strip()
            if ident:
                # Une categorie peut porter plusieurs taux dans un meme pays
                # (taux different selon le sous-produit) : on garde la liste,
                # pas la derniere valeur lue.
                if genre in ("EXEMPTED", "OUT_OF_SCOPE", "NOT_APPLICABLE"):
                    p["exemptions"].add(ident)
                if valeur is not None:
                    p["rate_categories"].setdefault(ident, set()).add(valeur)
                codes_cn = [
                    (c.findtext(_T + "value") or "").strip()
                    for c in res.iter(_T + "code")
                ]
                codes_cn = [c for c in codes_cn if c]
                if codes_cn:
                    p["cn_codes"].setdefault(ident, set()).update(codes_cn)

        note = res.findtext(_T + "comment")
        if note and note.strip():
            cle = f"{valeur:g}" if valeur is not None else genre
            p["rate_comments"].setdefault(cle, [])
            plat = " ".join(note.split())
            if plat not in p["rate_comments"][cle]:
                p["rate_comments"][cle].append(plat)

    lignes: list[dict[str, Any]] = []
    for code, p in sorted(par_pays.items()):
        lignes.append(
            {
                "country_code": code,
                "country_name": "",  # rempli par le referentiel local
                "standard_rate": p["standard_rate"],
                "reduced_rates": sorted(p["reduced_set"]),
                "super_reduced_rate": p["super_reduced_rate"],
                "parking_rate": p["parking_rate"],
                "currency": "",  # TEDB n'en rend aucune - voir vatnode
                "member_state": code != "XI",
                "rate_categories": {
                    k: sorted(v) for k, v in sorted(p["rate_categories"].items())
                },
                "rate_comments": p["rate_comments"],
                "cn_codes": {
                    k: sorted(v) for k, v in sorted(p["cn_codes"].items())
                },
                "exemptions": sorted(p["exemptions"]),
                "effective_on": p["effective_on"],
                "source": "TEDB",
                "provenance": "officielle",
                "lignes_source": p["lignes_tedb"],
            }
        )
    return lignes


def fetch_tedb(
    timeout: float, situation: str = "", endpoint: str = ""
) -> list[dict[str, Any]]:
    """Les 27 + XI, en un appel. 1,5 Mo, environ 4 s.

    `endpoint` permet de pointer un miroir ou un bouchon de test. Il est
    branche jusqu'ici depuis TVA_BASE_URL : un reglage affiche par le
    diagnostic et sans effet reel serait pire que pas de reglage du tout.
    """
    jour = situation or dt.date.today().isoformat()
    corps = enveloppe_tedb(TEDB_JURIDICTIONS, jour)
    r = _http(
        "POST",
        endpoint or TEDB_ENDPOINT,
        timeout,
        content=corps.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": TEDB_ACTION,
        },
    )
    lignes = depouille_tedb(r.content)
    if not lignes:
        raise SourceError(
            "TEDB : aucune juridiction dans la reponse. Une reponse vide n'est "
            "pas « aucun taux » : c'est une source qui ne repond plus ce qu'on "
            "attend. Le cache n'est pas remplace."
        )
    return lignes


# --------------------------------------------------------------------------
# vatnode : le hors-Union, et la devise de tout le monde
# --------------------------------------------------------------------------

def depouille_vatnode(payload: Any) -> tuple[list[dict[str, Any]], dict[str, str], str]:
    """Rend (lignes hors Union, devises par code, version du jeu).

    Les 27 ne ressortent PAS en lignes : TEDB les dit mieux. On ne garde de
    vatnode, pour eux, que la devise - la seule chose que TEDB ne rend pas.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("rates"), dict):
        raise SourceError(
            "vatnode : forme inattendue, la cle « rates » manque ou n'est pas "
            "un objet. Le jeu de donnees a peut-etre change de structure."
        )
    version = str(payload.get("version") or "")
    rates: dict[str, Any] = payload["rates"]

    devises: dict[str, str] = {}
    hors_union: list[dict[str, Any]] = []
    for brut, info in rates.items():
        if not isinstance(info, dict):
            continue
        code = _VATNODE_VERS_TEDB.get(brut.strip().upper(), brut.strip().upper())
        devise = str(info.get("currency") or "")
        if devise:
            devises[code] = devise
        if info.get("eu_member") or code in TEDB_JURIDICTIONS:
            continue
        reduits = info.get("reduced")
        reduits = [r for r in reduits if r is not None] if isinstance(reduits, list) else []
        hors_union.append(
            {
                "country_code": code,
                "country_name": str(info.get("country") or ""),
                "standard_rate": info.get("standard"),
                "reduced_rates": sorted(reduits),
                "super_reduced_rate": info.get("super_reduced"),
                "parking_rate": info.get("parking"),
                "currency": devise,
                "member_state": False,
                "rate_categories": {},
                "rate_comments": {},
                "cn_codes": {},
                "exemptions": [],
                "effective_on": "",
                "vat_name": str(info.get("vat_name") or ""),
                "vat_abbr": str(info.get("vat_abbr") or ""),
                "source": "vatnode",
                "provenance": "tenue a la main",
                "lignes_source": 1,
            }
        )
    hors_union.sort(key=lambda r: r["country_code"])
    return hors_union, devises, version


def fetch_vatnode(
    timeout: float, url_base: str = ""
) -> tuple[list[dict[str, Any]], dict[str, str], str]:
    """Le CDN d'abord, le depot brut en repli - meme fichier, deux chemins."""
    derniere = ""
    chemins = (url_base,) if url_base else (VATNODE_URL, VATNODE_MIROIR)
    for url in chemins:
        try:
            r = _http("GET", url, timeout, headers={"Accept": "application/json"})
            return depouille_vatnode(r.json())
        except (SourceError, ValueError) as exc:
            derniere = f"{url} : {exc}"
    raise SourceError(f"vatnode injoignable par ses deux chemins. {derniere}")


# --------------------------------------------------------------------------
# La fusion
# --------------------------------------------------------------------------

def fetch_tout(
    timeout: float,
    situation: str = "",
    tedb_endpoint: str = "",
    vatnode_url: str = "",
) -> dict[str, Any]:
    """Les deux sources, fusionnees, avec le compte-rendu de chacune.

    TEDB est la source qui ne se degrade pas : s'il ne repond pas, on ne rend
    rien. Perdre les 27 pour ne garder que les 18 juridictions tenues a la
    main serait servir la moins bonne moitie en silence.

    vatnode, lui, peut manquer sans que la reponse soit fausse : on perd le
    hors-Union et les devises, et le rendu le dit. Un taux FR sans sa devise
    reste un taux FR juste.
    """
    incidents: list[str] = []

    lignes = fetch_tedb(timeout, situation, tedb_endpoint)

    try:
        hors_union, devises, version = fetch_vatnode(timeout, vatnode_url)
    except SourceError as exc:
        hors_union, devises, version = [], {}, ""
        incidents.append(
            f"vatnode n'a pas repondu ({exc}). Consequence : aucune devise, et "
            "les juridictions hors Union (CH, GB, NO et les autres) sont "
            "absentes de ce releve - ce n'est PAS « elles n'ont pas de TVA »."
        )

    for ligne in lignes:
        devise = devises.get(ligne["country_code"])
        if devise:
            ligne["currency"] = devise
    lignes.extend(hors_union)
    lignes.sort(key=lambda r: (not r["member_state"], r["country_code"]))

    sans_devise = [l["country_code"] for l in lignes if not l["currency"]]
    if sans_devise and not incidents:
        incidents.append(
            "Devise absente pour : " + ", ".join(sans_devise)
            + ". vatnode ne la donne pas pour ces codes."
        )

    return {
        "rows": lignes,
        "sources": {
            "tedb": {
                "endpoint": tedb_endpoint or TEDB_ENDPOINT,
                "juridictions": len([l for l in lignes if l["source"] == "TEDB"]),
                "provenance": "officielle - Commission europeenne, alimentee par les Etats membres",
            },
            "vatnode": {
                "url": vatnode_url or VATNODE_URL,
                "version": version,
                "juridictions": len(hors_union),
                "provenance": "tenue a la main a partir de sources nationales - licence MIT",
            },
        },
        "incidents": incidents,
    }


if __name__ == "__main__":  # pragma: no cover - mise au point a la main
    blob = fetch_tout(60.0)
    print(f"{len(blob['rows'])} juridictions")
    print(json.dumps(blob["sources"], ensure_ascii=False, indent=2))
    for inc in blob["incidents"]:
        print("INCIDENT :", inc)
    for l in blob["rows"]:
        print(
            f"  {l['country_code']:3s} {str(l['standard_rate']):>5s} "
            f"{l['currency']:4s} {l['source']:8s} cat={len(l['rate_categories']):3d} "
            f"cn={len(l['cn_codes']):3d} eff={l['effective_on']}"
        )
