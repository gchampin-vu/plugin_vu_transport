"""Test a blanc du serveur MCP yooz-factures : aucun appel reseau.

On remplace les deux fonctions d'appel API par un faux Yooz qui repond selon le
chemin demande, et on verifie les deux chemins du connecteur :

  - l'historique  : sync (full / delta / brut), cache multi-jeux, colonnes,
                    recherches, agregations, SQL, export CSV ;
  - les petites requetes : liste des rapports, page courte, referentiels
                    (liste / count / un element), unites d'organisation, types
                    de document, exports et telechargement.

Plus les garde-fous : SQL en ecriture refuse, secrets en dossier synchronise
refuses, telechargement d'export qui ne marque rien par defaut.

Lancement :
    python test_offline.py <chemin de server.py>
"""
import importlib.util
import os
import pathlib
import shutil
import sys

SANDBOX = pathlib.Path(os.environ["TEMP"]) / "yooz-mcp-test"
shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True, exist_ok=True)
os.environ["YOOZ_HOME"] = str(SANDBOX)

(SANDBOX / "yooz.env").write_text(
    "YOOZ_COMPANIES=distriservice,vente_unique\n"
    "YOOZ_DEFAULT_REPORT_ID=report-factures\n"
    "YOOZ_DISTRISERVICE_LABEL=DistriService\n"
    "YOOZ_DISTRISERVICE_CLIENT_ID=cid-1\n"
    "YOOZ_DISTRISERVICE_CLIENT_SECRET=sec-1\n"
    "YOOZ_DISTRISERVICE_REFRESH_TOKEN=rt-1\n"
    "YOOZ_DISTRISERVICE_APPLICATION_ID=app-1\n"
    "YOOZ_VENTE_UNIQUE_LABEL=Vente-Unique\n"
    "YOOZ_VENTE_UNIQUE_CLIENT_ID=cid-2\n"
    "YOOZ_VENTE_UNIQUE_CLIENT_SECRET=sec-2\n"
    "YOOZ_VENTE_UNIQUE_REFRESH_TOKEN=rt-2\n"
    "YOOZ_VENTE_UNIQUE_APPLICATION_ID=app-2\n",
    encoding="utf-8",
)

SERVER = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "server.py")
spec = importlib.util.spec_from_file_location("yooz_server", SERVER)
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# --- faux Yooz ------------------------------------------------------------
FACTURES = {
    "DistriService": [
        {
            "id": "d1", "yoozNumber": "D-0001", "YZ_NUMBER_YZ_COMMONS": "FAC-2026-001",
            "orgUnitCode": "7000", "orgUnitName": "Fosse", "thirdPartyName": "VIR",
            "thirdPartyCode": "T-VIR", "YZ_DATE_YZ_COMMONS": "2026-07-03",
            "YZ_DUE_DATE_YZ_COMMONS": "2026-08-31", "currency": "EUR",
            "amount": "1000,50", "taxAmount": "200.10", "totalAmount": "1200.60",
            "blockedBoolean": "true", "blockingCause": "Ecart de prix",
            "YZ_PORTAL_STATUS": "A valider",
            "YZ_INVOICE_LINE": [{"label": "traction", "amount": 1000.5}],
        },
        {
            "id": "d2", "yoozNumber": "D-0002", "YZ_NUMBER_YZ_COMMONS": "FAC-2026-002",
            "orgUnitCode": "7005", "orgUnitName": "Moulins", "thirdPartyName": "TAMDIS",
            "thirdPartyCode": "T-TAM", "YZ_DATE_YZ_COMMONS": "2026-08-12",
            "currency": "EUR", "amount": 500, "totalAmount": 600,
            "blockedBoolean": "false", "YZ_PORTAL_STATUS": "Comptabilise",
        },
    ],
    "Vente-Unique": [
        {
            "id": "v1", "yoozNumber": "V-0100", "YZ_NUMBER_YZ_COMMONS": "INV-77",
            "orgUnitCode": "1000", "orgUnitName": "VU Siege", "thirdPartyName": "VIR",
            "YZ_DATE_YZ_COMMONS": "2026-08-01", "currency": "EUR",
            "amount": "2500", "totalAmount": "3000", "blockedBoolean": "true",
            "blockingCause": "Bon de commande absent",
            "CUSTOM_1_YZ_INVOICE": "transport",
        },
    ],
}
LIGNES = {
    "DistriService": [
        {"id": "l1", "yoozNumber": "D-0001", "orgUnitCode": "7000",
         "lineLabel": "traction", "lineAmount": "1000,50"},
    ],
    "Vente-Unique": [],
}
FOURNISSEUR = {
    "data": {"dataBlocks": {
        "YZ_THIRD_COMMONS": {"YZ_CODE": {"value": "T-VIR"}, "YZ_NAME": {"value": "VIR"}},
        "YZ_ADDRESS": {"YZ_CITY": {"value": "Fosse"}, "YZ_COUNTRY": {"value": "FR"}},
        "YZ_BANK": {"YZ_IBAN": {"value": ""}},
    }}
}
CALLS = []


def fake_api_get(company, path, params=None):
    params = dict(params or {})
    CALLS.append((company.label, path, params))
    if path.endswith("/dataReports"):
        return [
            {"reportId": "report-factures", "name": "Factures fournisseurs",
             "isMultiTenant": False, "creator": "admin"},
            {"reportId": "report-lignes", "name": "Lignes de facture",
             "isMultiTenant": False, "creator": "admin"},
        ]
    if "/dataReports/data/report-lignes" in path:
        page = int(params.get("pageOffset", "0"))
        return LIGNES[company.label] if page == 0 else []
    if "/dataReports/data/" in path:
        page = int(params.get("pageOffset", "0"))
        return FACTURES[company.label] if page == 0 else []
    if path.endswith("/YZ_SUPPLIER/referentials"):
        return ["REF_FOURNISSEURS", "REF_FOURNISSEURS_BIS"]
    if path.endswith("/count"):
        return {"count": "312"}
    if "/referentials/REF_FOURNISSEURS/data/T-VIR" in path:
        return FOURNISSEUR
    if path.endswith("/referentials/REF_FOURNISSEURS/data"):
        return [FOURNISSEUR, FOURNISSEUR]
    if path.endswith("/orgUnits"):
        return {"orgUnits": [{"code": {"value": "7000"}}, {"code": {"value": "7005"}}],
                "accessAllOrgUnits": False}
    if path.endswith("/orgUnits/7000"):
        return {"data": {"dataBlocks": {"YZ_ORGANIZATIONAL_UNIT_COMMONS":
                {"YZ_CODE": {"value": "7000"}, "YZ_NAME": {"value": "Hub Fosse"}}}}}
    if path.endswith("/documentTypes"):
        return [{"data": {"code": {"value": "YZ_INVOICE"}, "name": {"value": "Facture"},
                          "tags": ["invoice"]}}]
    if path.endswith("/exportResults"):
        return [{"generatedFileId": 4211, "name": "EXPORT_202608.csv",
                 "alreadyDownloaded": False, "exportCode": "COMPTA"}]
    raise srv.YoozError(f"chemin non simule dans le test : {path}")


def fake_api_get_bytes(company, path, params=None):
    CALLS.append((company.label, path, dict(params or {})))
    return b"col1;col2\n1;2\n"


srv._api_get = fake_api_get
srv._api_get_bytes = fake_api_get_bytes


def show(title, value):
    print("\n" + "=" * 78)
    print("### " + title)
    print("=" * 78)
    print(value)


ok = True


def expect(label, condition):
    global ok
    print(("  OK   " if condition else "  ECHEC") + f" {label}")
    ok = ok and bool(condition)


# --- 1. l'historique ------------------------------------------------------
show("yooz_sync(all, full)", srv.yooz_sync(company="all", mode="full"))
show("yooz_sync(distriservice, delta)", srv.yooz_sync(company="distriservice", mode="delta"))
show("yooz_sync(distriservice, brut)", srv.yooz_sync(company="distriservice", mode="brut"))
show(
    "yooz_sync(lignes, autre rapport)",
    srv.yooz_sync(company="all", dataset="lignes", report_id="report-lignes", mode="full"),
)
show("yooz_tables", srv.yooz_tables())
show("yooz_columns(factures)", srv.yooz_columns())
show("yooz_columns(lignes)", srv.yooz_columns(dataset="lignes"))
show("yooz_invoices(third_party=vir, blocked=oui)", srv.yooz_invoices(third_party="vir", blocked="oui"))
show("yooz_invoices(date_from=2026-08-01)", srv.yooz_invoices(date_from="2026-08-01"))
show("yooz_invoice(FAC-2026-001)", srv.yooz_invoice("FAC-2026-001"))
show("yooz_summary(thirdPartyName)", srv.yooz_summary())
show("yooz_summary(mois)", srv.yooz_summary(group_by="mois"))
show("yooz_sql(lignes)", srv.yooz_sql("SELECT source_app, lineLabel, lineAmount FROM lignes"))
show("yooz_export_csv", srv.yooz_export_csv("SELECT * FROM factures", "test_export.csv"))

# --- 2. les petites requetes ---------------------------------------------
show("yooz_reports", srv.yooz_reports())
show("yooz_report_peek(3 lignes, 4 colonnes)", srv.yooz_report_peek(
    company="distriservice", page_size=3,
    columns="yoozNumber,thirdPartyName,totalAmount,blockedBoolean"))
show("yooz_referential(liste)", srv.yooz_referential(kind="fournisseur"))
show("yooz_referential(count)", srv.yooz_referential(
    kind="fournisseur", referential="REF_FOURNISSEURS", count=True))
show("yooz_referential(un fournisseur)", srv.yooz_referential(
    kind="fournisseur", referential="REF_FOURNISSEURS", code="T-VIR"))
show("yooz_referential(page)", srv.yooz_referential(
    kind="fournisseur", referential="REF_FOURNISSEURS", limit=500))
show("yooz_org_units(liste)", srv.yooz_org_units())
show("yooz_org_units(7000)", srv.yooz_org_units(code="7000"))
show("yooz_document_types", srv.yooz_document_types())
show("yooz_exports", srv.yooz_exports())
show("yooz_export_download(4211)", srv.yooz_export_download("4211", filename="exp.csv"))
show("yooz_status", srv.yooz_status())

# --- garde-fous -----------------------------------------------------------
print("\n" + "=" * 78)
print("### Garde-fous")
print("=" * 78)
for bad in (
    "DELETE FROM factures",
    "SELECT 1; DROP TABLE factures",
    "PRAGMA table_info(factures)",
    "UPDATE factures SET amount = 0",
):
    expect(f"SQL refuse : {bad[:34]:<34}", srv.yooz_sql(bad).startswith("ECHEC"))

expect("famille de referentiel inconnue refusee",
       srv.yooz_referential(kind="nawak").startswith("ECHEC"))
expect("id d'export non numerique refuse",
       srv.yooz_export_download("../etc/passwd").startswith("ECHEC"))

os.environ["YOOZ_ENV_FILE"] = r"C:\Users\X\CAFOM\Transport BtoC - Documents\yooz.env"
srv._ENV_CACHE = None
try:
    srv._env_file()
    expect("fichier de secrets dans SharePoint refuse", False)
except srv.ConfigError as exc:
    expect("fichier de secrets dans SharePoint refuse", "synchronise" in str(exc))
del os.environ["YOOZ_ENV_FILE"]
srv._ENV_CACHE = None

# --- verifications de fond ------------------------------------------------
print()
conn = srv._open_ro()
tables = sorted(srv._tables(conn))
cols = srv._table_columns(conn, "factures")
total = conn.execute("SELECT COUNT(*) FROM factures").fetchone()[0]
amount = conn.execute("SELECT amount FROM factures WHERE yoozNumber='D-0001'").fetchone()[0]
key = conn.execute("SELECT keyToInvoiceLines FROM factures WHERE yoozNumber='D-0001'").fetchone()[0]
lignes = conn.execute("SELECT COUNT(*) FROM lignes").fetchone()[0]
conn.close()

expect(f"3 factures en cache, pas de doublon apres 3 synchros (vu {total})", total == 3)
expect(f"deux jeux de donnees separes (vu {tables})", tables == ["factures", "lignes"])
expect(f"1 ligne de facture dans son propre jeu (vu {lignes})", lignes == 1)
expect("YZ_INVOICE_LINE ecartee du cache", "YZ_INVOICE_LINE" not in cols)
expect(f"montant '1000,50' converti en nombre (vu {amount!r})", amount == 1000.5)
expect(f"keyToInvoiceLines calculee (vu {key!r})", key == "7000-D-0001")

report_calls = [c for c in CALLS if "/dataReports/data/" in c[1]]
sinces = [c[2].get("lastExecutionDatetime") for c in report_calls]
expect(f"mode full : borne au plancher (vu {sinces[0]!r})", sinces[0] == srv.SINCE_FLOOR)
expect("mode delta : borne repositionnee sur la derniere synchro",
       any(s and s != srv.SINCE_FLOOR for s in sinces))
expect("mode brut : aucun lastExecutionDatetime envoye", any(s is None for s in sinces))
expect("pageSize plafonne a 1000",
       all(int(c[2].get("pageSize", "1")) <= srv.MAX_PAGE_SIZE for c in report_calls))

ref_calls = [c for c in CALLS if "/referentials/REF_FOURNISSEURS/data" in c[1] and c[2]]
expect(f"limite referentiel plafonnee a 100 (vu {[c[2].get('limit') for c in ref_calls]})",
       all(int(c[2].get("limit", "1")) <= srv.MAX_REFERENTIAL_LIMIT for c in ref_calls))

dl = [c for c in CALLS if "/exportResults/4211" in c[1]]
expect(f"telechargement d'export sans marquage (vu {dl[0][2] if dl else None})",
       bool(dl) and dl[0][2].get("ignoreMarkAsDownloaded") == "true")
expect("export CSV ecrit", (SANDBOX / "exports" / "test_export.csv").exists())
expect("fichier d'export telecharge", (SANDBOX / "exports" / "exp.csv").exists())

flat = srv._flatten_deep(FOURNISSEUR)
expect(f"objet Yooz aplati lisiblement (vu {sorted(flat)[:2]})",
       flat.get("YZ_THIRD_COMMONS.YZ_CODE") == "T-VIR" and "YZ_BANK.YZ_IBAN" not in flat)
expect(f"source de configuration tracee (vu {srv._ENV_SOURCE})",
       srv._ENV_SOURCE == str(SANDBOX / "yooz.env"))
expect("racine locale hors de %LOCALAPPDATA% par defaut",
       "localappdata" not in str(srv.pathlib.Path.home() / ".yooz-mcp").lower()
       and srv._local_root() == SANDBOX)

# --- mode plugin : la configuration arrive par l'environnement -------------
print("\n" + "=" * 78)
print("### Mode plugin (configuration par l'environnement, sans fichier)")
print("=" * 78)
PLUGIN_DATA = SANDBOX / "plugin-data"
PLUGIN_DATA.mkdir(exist_ok=True)
plugin_env = {
    "CLAUDE_PLUGIN_DATA": str(PLUGIN_DATA),
    "YOOZ_COMPANIES": "distriservice,vente_unique",
    "YOOZ_DEFAULT_REPORT_ID": "report-factures",
    "YOOZ_DISTRISERVICE_LABEL": "DistriService",
    "YOOZ_DISTRISERVICE_APPLICATION_ID": "app-plugin",
    "YOOZ_DISTRISERVICE_CLIENT_ID": "cid-plugin",
    "YOOZ_DISTRISERVICE_CLIENT_SECRET": "sec-plugin",
    "YOOZ_DISTRISERVICE_REFRESH_TOKEN": "rt-plugin",
    # Champ laisse vide par le collegue : Claude Code pose une variable vide.
    "YOOZ_VENTE_UNIQUE_CLIENT_ID": "",
    # Champ absent de la configuration : la substitution n'est pas resolue.
    "YOOZ_VENTE_UNIQUE_CLIENT_SECRET": "${user_config.vente_unique_client_secret}",
    "YOOZ_BASE_URL": "${user_config.base_url}",
}
LEGACY = os.environ.get("LOCALAPPDATA")
os.environ.update(plugin_env)
os.environ["YOOZ_HOME"] = str(PLUGIN_DATA)  # aucun yooz.env a cet endroit
# Ecarte l'ancien emplacement %LOCALAPPDATA%\yooz-mcp\yooz.env du poste, pour
# tester le cas d'un collegue qui n'a jamais installe le connecteur a la main.
os.environ["LOCALAPPDATA"] = str(PLUGIN_DATA)
srv._ENV_CACHE = None
srv._ENV_SOURCE = "(pas encore lu)"

comps = srv._companies()
distri, vu = comps["distriservice"], comps["vente_unique"]
print(srv.yooz_status().split("\n\n")[0])
expect("secret lu depuis l'environnement du plugin", distri.client_secret == "sec-plugin")
expect("applicationId lu depuis l'environnement du plugin", distri.application_id == "app-plugin")
expect("societe complete vue comme configuree", distri.missing == [])
expect(f"substitution non resolue ignoree (vu {vu.client_secret!r})", vu.client_secret == "")
expect("champ vide ignore", vu.client_id == "")
expect(f"societe incomplete signalee (vu {len(vu.missing)} manque(s))", len(vu.missing) == 4)
expect(f"base URL retombe sur le defaut (vu {srv._base_url()})",
       srv._base_url() == srv.DEFAULT_BASE_URL)
expect("aucun fichier de configuration trouve, et c'est dit",
       srv._ENV_SOURCE.startswith("(aucun fichier"))

for key in plugin_env:
    os.environ.pop(key, None)
os.environ["YOOZ_HOME"] = str(SANDBOX)
if LEGACY:
    os.environ["LOCALAPPDATA"] = LEGACY
srv._ENV_CACHE = None

print(f"\n{len(CALLS)} appels API simules")
print("\nRESULTAT : " + ("TOUT PASSE" if ok else "AU MOINS UN ECHEC"))
sys.exit(0 if ok else 1)
