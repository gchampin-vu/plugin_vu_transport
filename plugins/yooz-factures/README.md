---
updated: 2026-08-28
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

**Depuis la version 1.2.0, le chemin par defaut est le direct.** La grille de
recherche du portail Yooz accepte des filtres serveur : une question sur des
factures se repond en un appel, sur l'etat courant de Yooz, sans rien rapatrier.
Le cache reste la, pour l'historique large, le SQL libre et les exports.

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

1. **L'API publique v2 n'a aucun endpoint de recherche de documents.** Le seul GET
   qui touche les documents est la liste des *types* de document. `POST /documents`
   existe, mais c'est un **import** - il n'est deliberement pas expose ici.
   *Nuance ajoutee le 2026-08-28 : l'API **interne du portail**, elle, en a une - la
   grille de recherche. Voir le chemin 3.*
2. **La donnee facture ne sort que par un data report**, sans filtre serveur : ni
   fournisseur, ni periode, ni montant. Seuls `pageOffset`, `pageSize` (1 000 max)
   et `lastExecutionDatetime` sont acceptes.

D'ou **trois chemins d'acces**, exposes separement, parce qu'ils n'ont ni le meme
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

### Chemin 3 - la grille de recherche du portail, en direct et filtree

**Ajoute le 2026-08-28, et c'est desormais le chemin par defaut pour une question
sur des factures.** L'interface Yooz a bien une recherche filtree : sa grille poste
ses criteres sur `POST /yooz/v1/core/grid/-18/data`. Ce n'est pas l'API v2 publique,
c'est l'**API interne du portail**, relevee dans les appels du navigateur.

Le point qui rend la chose exploitable : **le jeton du connecteur y est accepte**.
Rejoue avec le client API des deux societes - et non le client `yooz-stats` du
navigateur - l'appel repond 200. L'en-tete `siloid` du navigateur ne sert a rien, et
les cookies de session non plus.

- `yooz_live_summary` - montants et volumes agreges, par mois, par tiers, par unite,
  par cause de blocage. **La reponse en un appel a "combien ce fournisseur nous a
  facture sur la periode".**
- `yooz_live_invoices` - les lignes, avec choix des colonnes parmi les 97 de la
  grille.
- `yooz_live_columns` - la liste de ces colonnes, lue sur
  `POST /yooz/v1/core/grid/-18/settings`.

Filtres serveur verifies un a un, en controlant que les lignes rendues respectent
bien le filtre : tiers par code de referentiel (`eq` / `in`), date de facture et
date d'echeance (`gte` / `lte`), montant total, numero de piece en egalite stricte,
et blocage. `pageOffset` est un **numero de page qui commence a 1** (0 rend un 400),
`pageSize` n'a pas de plafond apparent - le connecteur pagine par 5 000 et plafonne
a 20 000 lignes.

**Ce chemin rend le meme chiffre que le cache.** Controle du 2026-08-28 sur les
3 303 documents DistriService de 2026 joints sur `yoozNumber` : montants identiques
en valeur absolue, regle de signe en desaccord sur zero. Les deux seuls ecarts
etaient deux dates de facture qui avaient bouge dans Yooz depuis la synchro de la
veille - c'est exactement l'interet du direct.

#### Les quatre pieges de cette API, et ce que le connecteur en fait

1. **Un filtre qu'elle ne sait pas appliquer est IGNORE EN SILENCE.** Un `like` sur
   `thirdPartyName` rend la grille entiere avec un HTTP 200 : le total serait faux
   sans aucun signal. Le connecteur **refuse** ces operateurs (`like`, `contains`,
   `startsWith`, `search`...) plutot que de laisser sortir le chiffre. Ceux qui
   passent : `eq`, `neq`, `in`, `gte`, `lte`, `gt`, `lt`, `contextual`. Corollaire :
   la recherche d'un tiers par son nom passe par `yooz_referential` pour obtenir son
   code, puis par `third_code`.
2. **Un avoir sort en POSITIF**, le sens etant porte par le type de document et non
   par le signe - a l'inverse du data report. Le connecteur ajoute `amountSigned` et
   `totalAmountSigned`, et n'additionne que celles-la.
3. **`blocked=false` ne veut pas dire "non bloque".** Le filtre que poste
   l'interface selectionne les documents qui portent un bloc de blocage : sur
   145 documents GLS tous non bloques, il n'en rend que 8. `blocked="oui"` reste un
   filtre serveur ; `blocked="non"` est applique cote client.
4. **Un filtre vide rend un 404 `NO_DATA_FOUND`**, pas une liste vide. Traduit en
   zero ligne, pas en panne.

Deux differences de perimetre avec le data report : la grille porte **tous** les
documents (`Autre document`, `Devis - Proforma`), d'ou `doc_kind="facture"`, et elle
n'a pas le plancher d'historique du rapport.

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
| `yooz_live_summary` | **montants et volumes agreges, en direct** - par mois, tiers, unite, cause de blocage. Avoirs comptes en negatif | grille |
| `yooz_live_invoices` | **recherche filtree en direct** : tiers, periode, echeance, montant, numero, blocage, type de document | grille |
| `yooz_live_columns` | les 97 colonnes de la grille, avec leur code | grille |
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

**La grille du portail**, hors `/yooz/v2/api` (relevee le 2026-08-28, non
documentee publiquement) :

| Endpoint | Corps utile | Note |
| --- | --- | --- |
| `POST /yooz/v1/core/grid/-18/data` | `filters`, `columns`, `pageOffset` (**commence a 1**), `pageSize` | la recherche filtree de documents |
| `POST /yooz/v1/core/grid/-18/settings` | `{}` | les 97 colonnes, les actions, le tri par defaut |

Les autres identifiants de grille repondent `NOT_RIGHT_ROLE_ON_COMPONENT` ou une
erreur interne : `-18` est la grille des documents, et c'est la seule utile ici.

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
- **Pas de recherche de facture sur l'API publique v2** : elle ne le propose pas.
  C'est la grille du portail qui la fournit (chemin 3), et le cache qui prend le
  relais pour le SQL libre et l'historique large.
- **Pas de recherche partielle de texte, meme sur la grille** : ni nom de tiers, ni
  numero de piece. Ces filtres existent dans le protocole mais Yooz les ignore en
  silence, donc le connecteur les refuse. Le nom se resout par le referentiel.
- **Pas de lignes de facture par defaut.** `YZ_INVOICE_LINE` est ecartee du cache
  (comme dans le Power Query). Deux voies si le detail devient necessaire : un data
  report dedie rapatrie dans son propre `dataset`, ou lever `YOOZ_DROP_COLUMNS`.
- **La documentation publique de l'application n'est pas lisible sans jeton** :
  `publicApiDoc/?applicationId=...` est une page dynamique, et l'API repond
  `401 INVALID_USER_CONTEXT` sur tous les chemins de definition. La surface
  ci-dessus vient donc de la collection Postman publique de Yooz, pas d'une
  supposition.

## Ou vit quoi : le reglage d'un cote, les secrets de l'autre

La configuration se lit sur **trois niveaux**, et c'est ce qui rend la mise en
service courte : sur les quatre valeurs par societe, **deux seulement** sont a
saisir sur le poste.

| Niveau | Ce qu'il porte | Ou |
| --- | --- | --- |
| **Equipe** | region Yooz, societes, identifiant du data report, `applicationId` et `client_id` de chaque societe, colonnes ecartees | `08_ENGINE/04_mcp/00_config/yooz.shared.env` — deja rempli, rien a y faire |
| **Poste** | **`client_secret` et refresh token** de chaque societe | `/plugin` > yooz-factures > configuration |
| **Poste, mode direct** | les memes secrets, plus les chemins locaux | `~\.yooz-mcp\yooz.env`, hors du vault |

Le poste est prioritaire sur l'equipe : un reglage d'equipe est un point de
depart commun, pas une contrainte.

Ce partage n'est pas cosmetique. La faute la plus couteuse de la mise en service
est de confondre `applicationId` et `client_id` — elle rend un
`403 EMPTY_OR_BAD_APPLICATION_ID` qui se lit a tort comme une erreur
d'authentification. La poser **une fois pour toute l'equipe** supprime la faute
au lieu de la documenter.

Deux variables d'environnement permettent de pointer le fichier d'equipe quand
la bibliotheque SharePoint n'est pas synchronisee a l'endroit attendu :
`YOOZ_SHARED_ENV` (le fichier) ou `VU_ENGINE_DIR` (la racine `08_ENGINE`).

### Les secrets : jamais dans le vault, jamais dans le catalogue de plugins

Deux voies, et la premiere est celle du plugin.

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

Le plugin ne donne aucun acces : il utilise **les identifiants du collegue**.
Quatre valeurs par societe, mais **deux seulement sont a sa charge** :

| Valeur | Ce que c'est | Qui la fournit |
| --- | --- | --- |
| `applicationId` | identifiant de l'application Yooz, **en-tete HTTP** | le fichier d'equipe |
| `client_id` | identifiant du client API | le fichier d'equipe |
| `client_secret` | secret du client API | **lui**, dans `/plugin` |
| refresh token | jeton *offline* genere dans Yooz | **lui**, dans `/plugin` |

Les deux dernieres se demandent par le canal habituel : Yooz, ou la personne qui
administre l'application.

La distinction qui faisait perdre du temps : **`applicationId` n'est pas le
`client_id`**. Un `applicationId` errone rend un `403 EMPTY_OR_BAD_APPLICATION_ID`,
ce qui se lit a tort comme une erreur d'authentification. Depuis que la valeur
est posee une fois pour l'equipe, le collegue n'a plus l'occasion de se tromper —
sauf si `/yooz-setup` lui dit que le fichier d'equipe est introuvable, auquel cas
il devra la saisir.

Sans identifiants, le plugin s'installe et `yooz_status` dit precisement ce qui
manque. La commande `/yooz-setup` conduit la mise en service **pas a pas**, une
etape a la fois, avec un tableau erreur -> cause reelle -> geste.

### Le chemin direct, hors plugin

Pour une synchro planifiee ou un diagnostic en terminal :

```powershell
.\server\install.ps1
```

L'environnement virtuel, le cache et les exports vont dans `~/.yooz-mcp` (rien dans
le vault). Le script cree aussi le `yooz.env` vide, puis affiche les commandes
`doctor` et `sync`.

## Comment l'utiliser en session

**Combien ce fournisseur nous a facture, en un appel.**

> Le montant des factures GLS depuis le debut de l'annee.

`yooz_referential(kind="fournisseur")` pour le code du tiers (`GLS` -> `FGLS`),
puis :

```
yooz_live_summary(group_by="mois", third_code="FGLS", date_from="2026-01-01")
```

Ni synchro, ni SQL. Les avoirs sont comptes en negatif et leur part est isolee.

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

**Regle de citation.** Un chiffre sorti d'ici se cite avec son perimetre et sa
source. Sur le cache, la source est la date de synchro que donne `yooz_status` ;
sur la grille, c'est l'heure de lecture, que le connecteur met en tete de chaque
reponse : "72 documents GLS, DistriService / VUL, du 2026-01-01 au 2026-08-28,
2 529 242,97 EUR TTC dont 12 avoirs pour -4 960,95 - Yooz lu en direct le
2026-08-28 a 14h43".

## Etat de la verification

**Teste le 2026-08-28, hors reseau Yooz.** `server/test_offline.py` remplace les
fonctions d'appel API - `_api_get`, `_api_get_bytes` et desormais `_api_post` - par
un faux Yooz qui repond selon le chemin, et verifie les trois chemins du connecteur
plus le mode plugin. **56 controles.**

> **Trois echecs connus, anterieurs a la version 1.2.0 et non lies a elle** :
> `substitution non resolue ignoree`, `champ vide ignore` et `societe incomplete
> signalee`. Ils viennent de l'**isolation du test** : le fichier de reglages
> d'equipe `08_ENGINE/04_mcp/00_config/yooz.shared.env` est trouve pendant le test
> et fournit de vraies valeurs la ou le test attend du vide. A corriger dans le
> harnais de test, pas dans le serveur.

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

**Le chemin direct est verifie avec de vrais identifiants** (2026-08-28, societe
DistriService et Vente-Unique) : les filtres un a un en controlant que les lignes
rendues les respectent, le rapprochement des 3 303 documents de 2026 avec le cache,
et le refus des operateurs ignores en silence. Hors ligne, sont verifies en plus :
le signe des avoirs, `doc_kind`, le depart de `pageOffset` a 1, la traduction du
404 `NO_DATA_FOUND` en zero ligne, et le fait qu'aucune colonne calculee par le
connecteur ne soit demandee a Yooz.

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
4. ~~Le comportement de `pageOffset` au-dela de la premiere page sur un vrai
   volume.~~ **Fait le 2026-08-28 sur la grille** : `pageOffset` y est un numero de
   page qui commence a 1, et la pagination a ete deroulee sur 19 269 documents.
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
