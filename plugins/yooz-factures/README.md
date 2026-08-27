---
updated: 2026-08-27
updated_by: Guillaume_Champin
type: process
---

# Plugin `yooz-factures`

Plugin du catalogue [`vu-transport`](../../README.md) : un serveur MCP en **lecture
seule** sur la base Yooz des deux societes du perimetre (DistriService et
Vente-Unique), par l'API publique Yooz Rising v2, region `eu1`, plus une skill qui
sait s'en servir.

Il s'installe en une commande, prepare son environnement Python tout seul, et chaque
collegue saisit ses propres identifiants dans l'interface du plugin.

Ce que ca change par rapport au script Power Query : la donnee n'est plus captive
d'un classeur. Une question du type *"quelles factures VIR sont bloquees, pour
combien, et depuis quand"* se pose en une phrase, et la reponse sort avec son
perimetre. C'est la brique qui manquait sous [[controle-facture]] : le montant
facture, cote Yooz, en face de l'estimation du back-office.

Lie a [[Connecteurs_Recommandes]] et a [[04_FACTURATION/README|04_FACTURATION]].

## Le point de depart : ce que l'API Yooz sait faire, et ce qu'elle ne sait pas

La surface complete de l'API v2 a ete relevee dans la **collection Postman publique
de l'API Yooz Rising** (222 requetes, 98 GET). Deux constats commandent toute la
conception du connecteur :

1. **Il n'existe aucun endpoint de recherche de documents.** Le seul GET qui touche
   les documents est la liste des *types* de document. `POST /documents` existe,
   mais c'est un **import** - il n'est deliberement pas expose ici.
2. **La donnee facture ne sort que par un data report**, sans filtre serveur : ni
   fournisseur, ni periode, ni montant. Seuls `pageOffset`, `pageSize` (1 000 max)
   et `lastExecutionDatetime` sont acceptes.

D'ou **deux chemins d'acces**, exposes separement, parce qu'ils n'ont ni le meme
cout ni le meme usage.

### Chemin 1 - l'historique, par le cache

`yooz_sync` rapatrie un data report page par page dans un **cache SQLite local**
(hors du vault), puis `yooz_sql`, `yooz_invoices`, `yooz_summary` et
`yooz_export_csv` l'interrogent instantanement. Sans ce cache, "total par
fournisseur sur juillet" impliquerait de retelecharger tout l'historique a chaque
question.

Le cache reprend exactement le traitement du Power Query : `source_app` et
`keyToInvoiceLines` (`orgUnitCode & "-" & yoozNumber`) ajoutees, `YZ_INVOICE_LINE`
ecartee, montants **types en numerique a l'ecriture** (y compris quand Yooz les rend
en texte a virgule decimale - sinon un `SUM()` additionne des chaines).

**Un rapport = un jeu de donnees = une table.** `yooz_sync(dataset="lignes",
report_id=...)` range un autre rapport a cote, sans melanger deux structures.
`yooz_tables` dit ce qui est en cache et de quel rapport ca vient.

Trois modes de rapatriement :

| mode | ce qu'il envoie | quand |
| --- | --- | --- |
| `full` (defaut) | `lastExecutionDatetime` au plancher (2023-01-01) | toujours juste, jamais de doublon (cache en `INSERT OR REPLACE`) |
| `delta` | la date de la derniere synchro reussie, par societe | rafraichissement quotidien |
| `brut` | rien du tout, le rapport decide | quand on veut le comportement natif du rapport |

**La reserve a connaitre sur `delta`.** La documentation Yooz est explicite :
`lastExecutionDatetime` ne filtre **que si le data report porte lui-meme un filtre
sur cette date**. Si le rapport factures n'en a pas, `delta` rend le meme volume que
`full` - sans perte de donnee, mais sans gain. A verifier au premier vrai
rapatriement, en comparant le nombre de lignes des deux modes.

### Chemin 2 - les petites requetes, en direct

Ce que l'API rend vite et court, sans cache :

- `yooz_reports` - **la liste des data reports** (identifiant, nom, createur). Un
  appel. C'est par la qu'on trouve un autre rapport que celui configure, au lieu
  d'aller chercher un UUID dans un classeur.
- `yooz_report_peek` - une page courte d'un rapport (5 lignes par defaut), avec
  choix des colonnes. Pour verifier la fraicheur, ou decouvrir la structure d'un
  rapport avant de le rapatrier.
- `yooz_referential` - **la requete la plus efficace du connecteur** : la fiche d'un
  fournisseur par son code, un appel, un objet. Ou le simple comptage. Ou une page
  de 100 lignes (plafond de l'API).
- `yooz_org_units` - relier un `orgUnitCode` de facture (7000, 7005...) a l'entite
  qui la porte.
- `yooz_document_types`, `yooz_exports`, `yooz_export_download`.

## Ce que ca sait faire

| Outil | Ce qu'il rend | Chemin |
| --- | --- | --- |
| `yooz_status` | configuration lue, societes, jeton, appel API de test, etat du cache. **Le premier a appeler quand ca coince.** N'affiche aucun secret | - |
| `yooz_sync` | rapatrie un rapport dans le cache (`company`, `dataset`, `mode`, `since`, `report_id`) | historique |
| `yooz_tables` | jeux de donnees en cache, rapport d'origine, date de synchro | historique |
| `yooz_columns` | colonnes d'un jeu de donnees avec leur taux de remplissage | historique |
| `yooz_invoices` | recherche par tiers, unite, numero, periode, montant, statut bloque - sans SQL | historique |
| `yooz_invoice` | tous les champs non vides d'une facture | historique |
| `yooz_summary` | montants et volumes par tiers, societe, mois, cause de blocage | historique |
| `yooz_sql` | SQL libre en **lecture seule** sur le cache | historique |
| `yooz_export_csv` | ecrit le resultat d'une requete dans un CSV | historique |
| `yooz_reports` | la liste des data reports de l'application | direct |
| `yooz_report_peek` | une page courte d'un rapport, colonnes au choix | direct |
| `yooz_referential` | un fournisseur par son code, un comptage, ou une page de 100 | direct |
| `yooz_org_units` | la liste des unites, ou la fiche d'une unite | direct |
| `yooz_document_types` | les types de document de l'application | direct |
| `yooz_exports` | les fichiers d'export comptable generes | direct |
| `yooz_export_download` | telecharge un export **sans le marquer comme lu** | direct |
| `yooz_api_get` | GET brut sur un chemin de l'API, pour le reste | direct |

## La surface de l'API v2, pour etendre sans deviner

Relevee dans la collection Postman publique. Les chemins sont prefixes de
`/yooz/v2/api`.

| Endpoint | Parametres utiles | Note |
| --- | --- | --- |
| `GET /dataReports` | - | identifiant + nom + createur de chaque rapport |
| `GET /dataReports/data/{reportId}` | `pageOffset` (numero de page), `pageSize` (**1 000 max**), `lastExecutionDatetime` (UTC, `2023-01-01T00:00:00.000Z`), `language` | **la seule sortie de donnee facture** |
| `GET /{TYPE}/referentials` | - | liste des referentiels d'une famille |
| `GET /{TYPE}/referentials/{code}/count` | - | nombre d'elements |
| `GET /{TYPE}/referentials/{code}/data` | `limit` (**100 max**), `offset` | page de donnees |
| `GET /{TYPE}/referentials/{code}/data/{element}` | - | **un element, un appel** |
| `GET /orgUnits`, `GET /orgUnits/{code}` | - | unites d'organisation |
| `GET /orgUnits/{code}/dimensionSet` | - | jeu de dimensions (centres de cout) |
| `GET /dimensionSets/{id}/dimensions/{nom}/data` | `limit` (100 max), `offset` | valeurs d'une dimension |
| `GET /documentTypes` | `tags` (separes par des virgules) | types de document |
| `GET /exportResults` | `limit`, `offset`, `alreadyDownloaded` | exports consolides |
| `GET /orgUnits/{code}/exportResults` | - | exports d'une unite |
| `GET /exportResults/{generatedFileId}` | `ignoreMarkAsDownloaded` | **voir le piege ci-dessous** |
| `GET /users`, `/users/{login}`, `/userGroups/...` | - | utilisateurs, roles, groupes |
| `GET /elementLinks`, `/exports`, `/imports/configurations` | - | liens d'elements, configurations |

Les familles `{TYPE}` de referentiel, avec leur nom dans l'outil : `fournisseur`
(`YZ_SUPPLIER`), `client` (`YZ_CUSTOMER`), `compte` (`YZ_ACCOUNT`), `cause_blocage`
(`YZ_BLOCKING_CAUSE`), `cause_refus`, `cause_suppression`, `categorie_facture`,
`categorie_speciale`, `devise`, `mode_paiement`, `profil_tva`, `journal`,
`periode_comptable`, `article`, `immobilisation`, `unite_mesure`, `adresse`,
`compte_bancaire`.

**Le piege du telechargement d'export.** Par defaut, `GET /exportResults/{id}`
**marque le fichier comme telecharge** cote Yooz, ce qui peut le faire sauter a
l'integration comptable qui le consomme. Le serveur envoie donc
`ignoreMarkAsDownloaded=true` par defaut, et il faut passer explicitement
`mark_as_downloaded=True` pour consommer le fichier. C'est le seul appel du
connecteur qui puisse avoir un effet dans Yooz.

## Ce que ca ne sait pas faire, et pourquoi

- **Aucune ecriture dans Yooz.** Le seul POST du serveur est l'echange du refresh
  token. `POST /documents`, les imports de referentiel, les `PUT` et les `DELETE`
  de l'API ne sont pas exposes. Valider, bloquer ou comptabiliser une facture reste
  un geste humain dans Yooz.
- **Pas de recherche de facture cote serveur** : l'API ne le propose pas. Tout
  filtre fin passe par le cache. C'est une contrainte de Yooz, pas un choix.
- **Pas de lignes de facture par defaut.** `YZ_INVOICE_LINE` est ecartee du cache
  (comme dans le Power Query). Deux voies si le detail devient necessaire : un data
  report dedie rapatrie dans son propre `dataset`, ou lever `YOOZ_DROP_COLUMNS`.
- **La documentation publique de l'application n'est pas lisible sans jeton** :
  `publicApiDoc/?applicationId=...` est une page dynamique, et l'API repond
  `401 INVALID_USER_CONTEXT` sur tous les chemins de definition. La surface
  ci-dessus vient donc de la collection Postman publique de Yooz, pas d'une
  supposition.

## Ou vivent les secrets

**Jamais dans le vault, jamais dans le catalogue de plugins.** Deux voies, et la
premiere est celle du plugin.

### En mode plugin : la configuration du plugin

A l'installation, Claude Code demande les identifiants. Les quatre champs de secret
(`client_secret` et `refresh token` de chaque societe) sont declares `sensitive` :
ils vont dans le **coffre du poste**, pas dans un `settings.json`, et sont passes au
serveur par son environnement. Rien a copier, rien a synchroniser, rien qui circule.

C'est aussi le seul chemin immunise contre un piege Windows mesure sur ce poste :
un `python` lance par l'alias du Microsoft Store donne a toute sa descendance une
**vue virtualisee de `%LOCALAPPDATA%`**. Mesure du 2026-08-27 dans
`%LOCALAPPDATA%\yooz-mcp` : PowerShell et l'interpreteur du venv lance directement
voient `['venv', 'yooz.env']` ; le meme interpreteur lance par l'alias du Store ne
voit que `['venv']`. Un fichier de configuration pose la est donc **invisible sans
erreur**. C'est pour cette raison que la racine locale de l'outil est
`~/.yooz-mcp`, et non `%LOCALAPPDATA%` : le profil utilisateur n'est pas virtualise,
donc le serveur lance par le plugin et le meme serveur lance a la main voient le
meme cache. `yooz_status` signale le cas s'il le rencontre.

### En mode direct : un fichier hors du vault

Pour le diagnostic, une synchro planifiee ou une session hors Claude Code. Le
fichier est cherche dans cet ordre :

1. `YOOZ_ENV_FILE` (chemin explicite) ;
2. `%CLAUDE_PLUGIN_DATA%\yooz.env` (sous `~/.claude/`, fiable en mode plugin) ;
3. `~/.yooz-mcp/yooz.env` - l'emplacement que cree `server/install.ps1` ;
4. `%LOCALAPPDATA%\yooz-mcp\yooz.env` - ancien emplacement, encore lu, jamais ecrit.

Une variable d'environnement **non vide** gagne toujours sur le fichier : un champ
de plugin laisse vide ne masque donc pas la valeur du fichier.

Le serveur **refuse** un fichier de secrets dont le chemin contient `onedrive`,
`sharepoint`, `cafom`, `transport btoc`, `dropbox` ou `google drive` - en silence
pour un emplacement par defaut, avec une erreur si le chemin a ete demande
explicitement par `YOOZ_ENV_FILE`.

Le refresh token Yooz est un jeton *offline*. S'il est renouvele par Yooz lors d'un
echange, le serveur conserve le nouveau dans `~/.yooz-mcp/refresh_tokens.json`
(droits 600) et l'utilise ensuite en priorite : c'est ce qui evite que la
configuration se perime en silence.

## Installer

### Le chemin normal : le plugin

Depuis Claude Code, une fois le catalogue `vu-transport` ajoute (voir le README du
catalogue) :

```bash
/plugin install yooz-factures@vu-transport
```

Claude Code demande alors les identifiants Yooz, puis demarre le serveur.
**Aucune installation Python a faire** : l'amorce `server/bootstrap.py` cherche un
environnement utilisable, le cree si besoin, installe `mcp` et `httpx`, et passe la
main. Les fois suivantes, elle ne fait que passer la main.

Verifier, dans une session :

> lance yooz_status

Il dit d'ou vient la configuration, si le jeton s'obtient, si l'API repond, et ou en
est le cache. C'est le premier outil a appeler quand quelque chose coince.

Puis le premier rapatriement, une fois :

> fais un yooz_sync complet sur les deux societes

### Ce qu'un collegue doit obtenir avant

Le plugin ne donne aucun acces : il utilise **les identifiants du collegue**. Pour
chaque societe, quatre valeurs a demander par le canal habituel (Yooz, ou la
personne qui administre l'application) :

| Valeur | Ce que c'est |
| --- | --- |
| `applicationId` | identifiant de l'application Yooz, **en-tete HTTP** |
| `client_id` | identifiant du client API |
| `client_secret` | secret du client API |
| refresh token | jeton *offline* genere dans Yooz |

La distinction qui fait perdre du temps : **`applicationId` n'est pas le
`client_id`**. Un `applicationId` errone rend un `403 EMPTY_OR_BAD_APPLICATION_ID`,
ce qui se lit a tort comme une erreur d'authentification.

Sans identifiants, le plugin s'installe et `yooz_status` dit precisement ce qui
manque.

### Le chemin direct, hors plugin

Pour une synchro planifiee ou un diagnostic en terminal :

```powershell
.\server\install.ps1
```

L'environnement virtuel, le cache et les exports vont dans `~/.yooz-mcp` (rien dans
le vault). Le script cree aussi le `yooz.env` vide, puis affiche les commandes
`doctor` et `sync`.

## Comment l'utiliser en session

**Une question courte, sans rien rapatrier.**

> La fiche du fournisseur T-VIR dans Yooz.

`yooz_referential(kind="fournisseur")` pour trouver le referentiel, puis
`yooz_referential(kind="fournisseur", referential="<code>", code="T-VIR")`. Deux
appels, deux objets.

**Ce qui est bloque, et ce que ca pese.**

`yooz_summary(group_by="blockingCause")` puis `yooz_invoices(blocked="oui")`.

**Un fournisseur sur une periode.**

```sql
SELECT source_app, SUBSTR(YZ_DATE_YZ_COMMONS,1,7) AS mois,
       COUNT(*) AS nb, ROUND(SUM(totalAmount),2) AS total
FROM factures
WHERE thirdPartyName LIKE '%VIR%'
  AND YZ_DATE_YZ_COMMONS BETWEEN '2026-07-01' AND '2026-08-31'
GROUP BY 1, 2 ORDER BY 2
```

**Le point de depart d'un controle de facture.**

`yooz_invoice("FAC-2026-001")` rend tous les champs cote Yooz - montant, tiers,
unite d'organisation, ecart (`YZ_DISCREPANCY_AMOUNT_YZ_INVOICE`), cause de blocage.
La comparaison avec l'estimation du back-office reste le travail de
[[controle-facture]] ; ce serveur fournit le cote Yooz, cle `keyToInvoiceLines`
comprise.

**Regle de citation.** Un chiffre sorti d'ici se cite avec son perimetre et sa date
de synchro : "3 912 factures, DistriService + Vente-Unique, synchro du 2026-08-27".
`yooz_status` donne la date du dernier ecrit par societe.

## Etat de la verification

**Teste le 2026-08-27, hors reseau Yooz.** `server/test_offline.py` remplace les
deux fonctions d'appel API par un faux Yooz qui repond selon le chemin, et verifie
les deux chemins du connecteur plus le mode plugin. **31 controles, tout passe.**

```powershell
python .\server\bootstrap.py --help    # prepare l'environnement, puis :
& "$HOME\.yooz-mcp\venv\Scripts\python.exe" .\server\test_offline.py .\server\server.py
```

Sont verifies en particulier : pas de doublon apres trois synchros, deux jeux de
donnees separes dans le cache, `YZ_INVOICE_LINE` ecartee, `"1000,50"` converti en
`1000.5`, `keyToInvoiceLines` calculee, les trois modes de synchro (plancher pour
`full`, borne repositionnee pour `delta`, aucune borne pour `brut`), les plafonds de
l'API respectes (1 000 lignes par page de rapport, 100 par page de referentiel),
l'aplatissement des objets imbriques de referentiel, le telechargement d'export qui
**n'envoie pas** de marquage, et les refus attendus (SQL en ecriture, famille de
referentiel inconnue, identifiant d'export non numerique, fichier de secrets designe
explicitement dans un dossier synchronise).

**Le mode plugin est teste specifiquement** : configuration lue depuis
l'environnement, champ laisse vide ignore, **substitution `${user_config.*}` non
resolue traitee comme absente** (sans ce filtre, Yooz recevrait la chaine litterale
comme secret et repondrait un 401 incomprehensible), societe incomplete signalee, et
racine locale hors de `%LOCALAPPDATA%`.

**L'amorce est verifiee sur ce poste** : lancee par l'alias Python du Microsoft
Store, elle a reutilise l'environnement existant et rendu la main au serveur, qui a
repondu son diagnostic. C'est aussi la ou la virtualisation de `%LOCALAPPDATA%` a
ete mesuree, et ou `yooz_status` la signale desormais.

**Reste a verifier avec de vrais identifiants** (non fait ici, volontairement : les
identifiants transmis en conversation sont a considerer comme compromis et a
regenerer avant tout usage) :

1. `yooz_status` : jeton OK et appel API OK sur les deux societes.
2. `yooz_reports` : la liste reelle des rapports. Elle dira notamment s'il existe un
   rapport de lignes de facture exploitable pour le controle ligne a ligne.
3. **La semantique de `lastExecutionDatetime`** : comparer le nombre de lignes en
   `mode="full"` et en `mode="delta"`. Si les deux sont egaux, le rapport ne porte
   pas de filtre sur cette date et `delta` n'apporte rien.
4. Le comportement de `pageOffset` au-dela de la premiere page sur un vrai volume.
5. Les codes de referentiel reels (`yooz_referential(kind="fournisseur")`), et la
   forme exacte des objets renvoyes - le test simule une structure plausible
   `data.dataBlocks.<BLOC>.<champ>.value`, l'aplatisseur est generique mais ses
   chemins n'ont pas ete confrontes a la vraie reponse.

## Deux points a ne pas oublier

- **Rotation des identifiants.** Les `client_secret` et `refresh_token` des deux
  applications ont circule en clair (script Power Query, conversation). A regenerer
  dans Yooz, et a ne plus jamais coller dans un classeur ni dans un fil.
- **Jalon Webfacto.** Construire ce plugin et l'essayer entre nous est du
  prototypage, libre. Le deployer a l'echelle de l'equipe ne l'est pas, et le jour ou
  il alimente un flux, un rapport partage ou une automatisation branchee au SI, le
  cas d'usage passe par un cadrage Webfacto (besoin, faisabilite, securite,
  priorisation).

## Source de la surface d'API

Collection Postman publique **Yooz Rising Public API of your application**, espace
de travail `TRG-Yooz` (222 requetes, relevee le 2026-08-27) :
`https://www.postman.com/trgwareenan/trg-yooz/documentation/beveb3f/yooz-rising-public-api-of-your-application-certifadmintrgwareenanboonchan`

C'est une collection tierce, pas un document officiel Yooz : elle est coherente avec
l'endpoint prouve par le script Power Query, mais tout endpoint non encore appele
avec de vrais identifiants reste **a confirmer**.
