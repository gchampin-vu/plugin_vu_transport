#!/usr/bin/env python3
"""
Controles hors-ligne du connecteur taux-de-change.

    python test_offline.py

**Aucun appel reseau.** L'instantane est remplace par un jeu d'essai qui
reproduit exactement les pieges du jeu de donnees reel :

- un taux qui n'est republie que quand il change (GBP dormant depuis fevrier) ;
- une ligne par couple (devise, pays), donc USD en double a chaque publication ;
- deux taux qui divergent a la 10e decimale sur la meme date (le franc CFA) ;
- une devise qui n'a plus cours (monnaievigueur=0) ;
- une devise dont la premiere publication est posterieure a la date demandee.

C'est la raison d'etre de l'instantane en memoire : ces cas se testent sans
reseau, donc ils se testent vraiment. Un garde-fou qui ne tourne que quand
data.economie.gouv.fr repond n'est pas un garde-fou.

Le fichier d'equipe est neutralise par TAUX_SHARED_ENV pointant un chemin
inexistant : la variable est EXCLUSIVE, donc la suite tourne bien comme sur un
poste sans configuration d'equipe - et pas, en silence, avec celle du
mainteneur.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

# Doit etre pose AVANT l'import du serveur : c'est ce qui garantit qu'aucune
# valeur d'equipe ne fausse les controles.
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="taux-test-"))
os.environ["TAUX_SHARED_ENV"] = str(_TMP / "inexistant.env")
os.environ["TAUX_ENV_FILE"] = str(_TMP / "inexistant.env")
os.environ["TAUX_HOME"] = str(_TMP / "home")
os.environ["TAUX_EXPORT_DIR"] = str(_TMP / "exports")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import server as s  # noqa: E402

ECHECS: list[str] = []
PASSES = 0


def verifie(nom: str, condition: bool, detail: str = "") -> None:
    global PASSES
    if condition:
        PASSES += 1
        print(f"  OK   {nom}")
    else:
        ECHECS.append(f"{nom} - {detail}")
        print(f"  ECHEC {nom} - {detail}")


# --------------------------------------------------------------------------
# Le jeu d'essai
# --------------------------------------------------------------------------

def ligne(code, nom, pays, cp, date, taux, vigueur=1):
    return s._normalise(
        {
            "monnaie_source": code,
            "nom_monnaie_source": nom,
            "pays_principal": pays,
            "code_pays": cp,
            "date": date,
            "taux": taux,
            "monnaievigueur": vigueur,
        }
    )


JEU = [
    # USD : deux pays, donc deux lignes par publication. Deux publications.
    ligne("USD", "DOLLAR DES ETATS-UNIS", "ETATS-UNIS", "US", "2026-09-01", 0.8589),
    ligne("USD", "DOLLAR DES ETATS-UNIS", "EQUATEUR", "EC", "2026-09-01", 0.8589),
    ligne("USD", "DOLLAR DES ETATS-UNIS", "ETATS-UNIS", "US", "2026-06-01", 0.87),
    ligne("USD", "DOLLAR DES ETATS-UNIS", "EQUATEUR", "EC", "2026-06-01", 0.87),
    # GBP : rien depuis fevrier. C'est LE piege du jeu.
    ligne("GBP", "LIVRE STERLING", "ROYAUME-UNI", "GB", "2026-02-01", 1.154),
    # AFA : n'a plus cours, dernier taux ancien.
    ligne("AFA", "AFGHANI", "AFGHANISTAN", "AF", "2003-10-16", 0.0175, vigueur=0),
    # XOF : parite fixe, arrondie differemment selon le pays, meme date.
    ligne("XOF", "FRANC C.F.A.", "SENEGAL", "SN", "2002-01-01", 0.0015244904),
    ligne("XOF", "FRANC C.F.A.", "MALI", "ML", "2002-01-01", 0.0015244902),
    # TRY : premiere publication tardive, pour tester "pas encore de taux".
    ligne("TRY", "NOUVELLE LIVRE TURQUE", "TURQUIE", "TR", "2026-09-01", 0.0178),
]

s._SNAP = JEU
s._SNAP_AT = float("inf")  # jamais perime : aucun appel reseau ne sera tente
s._SNAP_ORIGIN = "JEU D'ESSAI (test_offline.py)"


def appel_reseau_interdit(*_args, **_kwargs):
    raise AssertionError(
        "un controle hors-ligne a tente un appel reseau : le test est faux, "
        "ou le code a cesse de passer par l'instantane"
    )


s._get = appel_reseau_interdit  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Les controles
# --------------------------------------------------------------------------

def test_en_vigueur() -> None:
    print("\nLe taux en vigueur a une date - ce que ce connecteur existe pour faire")

    lignes = s._en_vigueur("2026-09-01", None, vigueur_seulement=True)
    par_code = {r["monnaie_source"]: r for r in lignes}

    verifie(
        "GBP est rendu alors qu'il n'a rien publie ce mois-la",
        "GBP" in par_code,
        "c'est exactement le piege : un filtre d'egalite sur la date l'aurait perdu",
    )
    verifie(
        "la date d'effet de GBP est celle de sa derniere publication",
        par_code.get("GBP", {}).get("date_effet") == "2026-02-01",
        str(par_code.get("GBP")),
    )
    verifie(
        "USD est dedoublonne : une ligne, pas une par pays",
        len([r for r in lignes if r["monnaie_source"] == "USD"]) == 1,
    )
    verifie(
        "et nb_pays porte le compte des pays",
        par_code.get("USD", {}).get("nb_pays") == 2,
        str(par_code.get("USD", {}).get("nb_pays")),
    )
    verifie(
        "USD retient la publication la plus recente, pas la plus ancienne",
        par_code.get("USD", {}).get("taux") == 0.8589,
        str(par_code.get("USD", {}).get("taux")),
    )
    verifie(
        "la devise sans cours est ecartee par defaut",
        "AFA" not in par_code,
    )
    verifie(
        "et rendue quand on la demande explicitement",
        "AFA" in {
            r["monnaie_source"]
            for r in s._en_vigueur("2026-09-01", None, vigueur_seulement=False)
        },
    )
    verifie(
        "les taux divergents entre pays sont signales, pas choisis en silence",
        par_code.get("XOF", {}).get("taux_divergents") is True,
        str(par_code.get("XOF")),
    )

    # Une date anterieure a la publication : le taux ancien, pas le recent.
    juin = {r["monnaie_source"]: r for r in s._en_vigueur("2026-07-15")}
    verifie(
        "au 2026-07-15, USD rend le taux de juin et pas celui de septembre",
        juin.get("USD", {}).get("taux") == 0.87
        and juin.get("USD", {}).get("date_effet") == "2026-06-01",
        str(juin.get("USD")),
    )
    verifie(
        "au 2026-07-15, TRY n'a pas encore de taux",
        "TRY" not in juin,
    )


def test_devise_ciblee() -> None:
    print("\nLa resolution d'une devise nommee")

    entry = s._une_devise("2026-09-01", "usd")
    verifie("le code est normalise en majuscules", entry["monnaie_source"] == "USD")

    entry = s._une_devise("2026-09-01", "AFA")
    verifie(
        "une devise sans cours reste resolue quand elle est nommee",
        entry["taux"] == 0.0175,
        "repondre 'inconnue' serait faux : elle a un taux, avec sa date d'effet",
    )

    try:
        s._une_devise("2026-09-01", "XYZ")
        verifie("un code inconnu leve une erreur", False, "aucune erreur levee")
    except s.TauxError as exc:
        verifie(
            "un code inconnu leve une erreur qui dit quoi faire",
            "taux_devises" in str(exc) and "ne devine pas" in str(exc),
            str(exc),
        )

    try:
        s._une_devise("2020-01-01", "TRY")
        verifie("une date trop ancienne leve une erreur", False, "aucune erreur levee")
    except s.TauxError as exc:
        verifie(
            "une date anterieure a la premiere publication dit laquelle",
            "2026-09-01" in str(exc),
            str(exc),
        )


def test_dates() -> None:
    print("\nLes formats de date acceptes")

    verifie("AAAA-MM-JJ passe tel quel", s._parse_date("2026-03-15")[0] == "2026-03-15")
    verifie(
        "AAAA-MM est resolu au 1er, et c'est annonce",
        s._parse_date("2026-03") == ("2026-03-01", "2026-03 resolu au 1er du mois (2026-03-01)"),
        str(s._parse_date("2026-03")),
    )
    verifie("AAAA est resolu au 31 decembre", s._parse_date("2026")[0] == "2026-12-31")
    verifie("un mois en francais est comprise", s._parse_date("septembre 2026")[0] == "2026-09-01")
    verifie("vide vaut aujourd'hui", s._parse_date("")[0] == s._today())
    for mauvais in ("15/03/2026", "n'importe quoi", "2026-13-45"):
        try:
            s._parse_date(mauvais)
            verifie(f"date refusee : {mauvais}", False, "acceptee a tort")
        except s.TauxError:
            verifie(f"date refusee : {mauvais}", True)


def test_devises_spec() -> None:
    print("\nLa liste de devises demandee")
    verifie("virgules", s._devises("usd,gbp") == ["USD", "GBP"])
    verifie("espaces et point-virgules", s._devises("usd; gbp  pln") == ["USD", "GBP", "PLN"])
    verifie("vide = toutes", s._devises("") == [])


def test_conversion() -> None:
    print("\nLa conversion, et son sens")

    out = s.taux_convertir(1000, "USD", "vers_eur", "2026-09-01")
    verifie(
        "devise -> euro multiplie par le taux",
        "858.90 EUR" in out,
        out.splitlines()[-1] if out else "",
    )
    verifie("le taux utilise est cite", "0.8589" in out)
    verifie("la date d'effet du taux est citee", "2026-09-01" in out)
    verifie(
        "le sens du taux est rappele dans la reponse",
        "nombre d'EUROS que vaut UNE unite" in out,
    )

    out = s.taux_convertir(1000, "USD", "depuis_eur", "2026-09-01")
    verifie(
        "euro -> devise divise par le taux",
        "1164.28 USD" in out,
        [l for l in out.splitlines() if "Resultat  " in l],
    )

    out = s.taux_convertir(100, "GBP", "vers_eur", "2026-09-01")
    verifie(
        "un taux dormant est signale comme datant d'un autre mois",
        "un autre mois que la date demandee" in out,
    )

    out = s.taux_convertir(100, "AFA", "vers_eur", "2026-09-01")
    verifie(
        "une devise sans cours est signalee dans la conversion",
        "n'ayant plus cours" in out,
    )

    out = s.taux_convertir(100, "USD", "n'importe quoi")
    verifie("un sens non compris est refuse", out.startswith("ERREUR"), out[:120])


def test_historique() -> None:
    print("\nL'historique d'une devise")

    out = s.taux_historique("USD")
    verifie(
        "une ligne par publication, pas une par pays",
        out.count("\nUSD;") == 0 and out.count("2026-09-01;USD") == 1,
        out,
    )
    verifie("les publications sont du plus recent au plus ancien", out.index("2026-09-01") < out.index("2026-06-01"))
    verifie("le min et le max du perimetre sont donnes", "min 0.8589" in out and "max 0.87" in out, out)

    out = s.taux_historique("USD", "2026-08-01")
    verifie(
        "la borne basse filtre",
        "2026-06-01" not in out and "2026-09-01" in out,
    )
    out = s.taux_historique("XYZ")
    verifie("un code inconnu est refuse", out.startswith("ERREUR"), out[:120])


def test_garde_fous_records() -> None:
    print("\nLes plafonds de l'API, dits avant l'appel plutot qu'apres l'erreur")

    out = s.taux_records(limit=500)
    verifie(
        "limit au-dela de 100 est refuse cote serveur",
        out.startswith("ERREUR") and "100" in out,
        out[:150],
    )
    out = s.taux_records(limit=100, offset=9950)
    verifie(
        "offset + limit au-dela de 10 000 est refuse, avec la raison",
        out.startswith("ERREUR") and "taux_export_csv" in out,
        out[:200],
    )
    out = s.taux_records(limit=0)
    verifie("limit nul est refuse", out.startswith("ERREUR"), out[:120])


def test_csv() -> None:
    print("\nLe format des fichiers ecrits")

    texte = s._to_csv_text([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])
    verifie("point-virgule comme separateur", texte.splitlines()[0] == "a;b", texte)
    verifie("fins de ligne LF, jamais CRLF", "\r" not in texte)

    verifie(
        "le taux n'est pas arrondi par le connecteur",
        s._fmt_taux(9.763e-05) == "9.763e-05",
        s._fmt_taux(9.763e-05),
    )

    out = s.taux_export_csv(mode="en_vigueur", date="2026-09-01")
    chemin = pathlib.Path(out.splitlines()[0].split("Export termine : ", 1)[1])
    verifie("le chemin exact du fichier est rendu", chemin.is_file(), str(chemin))
    verifie(
        "l'export atterrit dans le dossier local, jamais dans la bibliotheque",
        "CAFOM" not in str(chemin) and "SharePoint" not in str(chemin),
        str(chemin),
    )
    brut = chemin.read_bytes()
    verifie("un seul BOM, pour qu'Excel FR ouvre sans assistant", brut[:3] == b"\xef\xbb\xbf" and brut[3:6] != b"\xef\xbb\xbf")
    verifie("fins de ligne LF dans le fichier ecrit", b"\r\n" not in brut)
    verifie(
        "l'export en_vigueur porte les devises sans cours, et le dit",
        b"AFA" in brut and "PRESENTE dans l'export" in out,
    )
    verifie("l'absence de troncature est affirmee", "Troncature           : aucune" in out)

    out = s.taux_export_csv(mode="xxx")
    verifie("un mode inconnu est refuse", out.startswith("ERREUR"), out[:120])


def test_pieges_dits() -> None:
    print("\nCe que les reponses disent d'elles-memes")

    guide = s.taux_guide()
    for attendu in (
        "republie QUE quand il change",
        "une ligne par couple (devise, pays)",
        "sens du taux",
        "en vigueur AUJOURD'HUI",
        s.PREMIERE_DATE_UTILE,
        "taux de CHANCELLERIE",
    ):
        verifie(f"le guide porte : {attendu}", attendu.lower() in guide.lower())

    out = s.taux_a_la_date("2003-05-01", "USD")
    verifie(
        "une date anterieure a la couverture reelle est signalee",
        "DATE ANTERIEURE" in out,
        out[:300],
    )
    out = s.taux_a_la_date("2026-09-01", "USD,ZZZ")
    verifie(
        "une devise demandee et non rendue est nommee, avec les trois causes",
        "ZZZ" in out and "sans avoir distingue les trois" in out,
    )
    out = s.taux_a_la_date("2020-01-01", "USD")
    verifie(
        "sur une date passee, la portee du flag monnaievigueur est rappelee",
        "pas \"a cette date\"" in out,
        out[:400],
    )


def test_config() -> None:
    print("\nLa configuration : origine de chaque reglage, et exclusivite du chemin explicite")

    verifie(
        "un TAUX_SHARED_ENV explicite est EXCLUSIF : aucun repli sur le fichier reel",
        s._shared_env_candidates() == [pathlib.Path(os.environ["TAUX_SHARED_ENV"])],
        str(s._shared_env_candidates()),
    )
    verifie(
        "sans fichier d'equipe, la racine API est celle du serveur",
        s._base_url() == s.DEFAULT_BASE_URL and s._origine_reglage("TAUX_BASE_URL") == "defaut serveur",
    )
    verifie(
        "un reglage pose par le poste est annonce comme venant du poste",
        s._origine_reglage("TAUX_EXPORT_DIR") == "poste",
    )
    verifie(
        "la racine locale suit la convention ~/.taux-de-change-mcp",
        "taux-de-change-mcp" in str(pathlib.Path.home() / ".taux-de-change-mcp"),
    )
    valeurs = s._parse_env('# commentaire\nTAUX_BASE_URL="https://exemple"\nexport TAUX_DATASET=jeu\nvide\n')
    verifie(
        "le lecteur de .env gere commentaires, guillemets et prefixe export",
        valeurs == {"TAUX_BASE_URL": "https://exemple", "TAUX_DATASET": "jeu"},
        str(valeurs),
    )


def main() -> int:
    print("Controles hors-ligne du connecteur taux-de-change")
    print(f"Jeu d'essai : {len(JEU)} lignes, aucun appel reseau autorise.")
    for fn in (
        test_en_vigueur,
        test_devise_ciblee,
        test_dates,
        test_devises_spec,
        test_conversion,
        test_historique,
        test_garde_fous_records,
        test_csv,
        test_pieges_dits,
        test_config,
    ):
        fn()
    print("\n" + "-" * 70)
    if ECHECS:
        print(f"{PASSES} controle(s) OK, {len(ECHECS)} ECHEC(S) :")
        for echec in ECHECS:
            print(f"  - {echec}")
        return 1
    print(f"{PASSES} controle(s) OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
