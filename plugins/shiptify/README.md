---
updated: 2026-08-28
updated_by: Guillaume_Champin
type: process
---

# Plugin `shiptify` — connecteur MCP en lecture seule

Serveur MCP qui donne a Claude Code un acces **en lecture** a la base Shiptify :
les envois, les demandes de transport, les commandes, les lieux, les
transporteurs actifs, les factures et leurs lignes, le journal d'evenements et
les referentiels.

C'est le portage du script Power Query qui ramenait `/shipments` dans Power BI,
avec trois differences qui comptent : la cle d'API n'est plus ecrite dans la
requete, les 91 chemins de lecture de l'API sont atteignables et pas seulement
un, et le resultat est interrogeable en session au lieu d'attendre un
rafraichissement de modele.

Shiptify porte **les demandes de transport middle mile vers les agences**
(cf. le vocabulaire maison). Ce serveur est donc l'entree naturelle pour une
question d'acheminement, et un complement du controle de facturation cote
lignes de facture.

## Ce que ca sait faire

| Outil | Ce qu'il rend |
| --- | --- |
| `shiptify_setup_status` | ou en est la mise en service : cle saisie, origine, rangee ou non, quoi faire ensuite |
| `shiptify_save_key` | range sur la machine la cle deja saisie dans l'interface. Aucun argument |
| `shiptify_forget_key` | supprime la cle rangee sur la machine |
| `shiptify_doctor` | configuration lue, cle presente (masquee), et deux vrais appels a l'API. A appeler quand un appel echoue |
| `shiptify_list_paths` | les 91 chemins GET avec leurs filtres et leurs valeurs permises. A lire avant de deviner un nom de parametre |
| `shiptify_accounts` | les comptes Shiptify autorises pour la cle |
| `shiptify_list_shipments` | **l'outil principal** : les envois, filtres par dates de creation, de depart, d'arrivee, par lieu, par expediteur, par demande |
| `shiptify_get_shipment` | un envoi, et au choix ses points de suivi, contenus, pieces jointes, metadonnees, SSCC |
| `shiptify_list_shipment_requests` | les demandes de transport |
| `shiptify_get_shipment_request` | une demande par identifiant **ou par reference interne**, avec ses envois, contenus, facturation, lignes de facture |
| `shiptify_list_invoices` / `shiptify_get_invoice` | les factures transporteur, par mois comptable, statut, transporteur |
| `shiptify_list_invoice_lines` | **le detail ligne a ligne**, filtre par transporteur, dates d'enlevement, de livraison ou comptables |
| `shiptify_list_orders` | les commandes, par date de depart estimee ou par envoi |
| `shiptify_list_locations` | les lieux : agences, entrepots, points de livraison. Recherche plein texte |
| `shiptify_list_carriers` | les transporteurs actifs du compte |
| `shiptify_list_events` | le journal d'evenements (creation, annulation, replanification, prix) |
| `shiptify_dictionary` | les referentiels : modes, causes, incidents, litiges, services, tags, unites de fret |
| `shiptify_export_csv` | **le remplacant du script Power Query** : pagine une collection entiere, aplatit les objets imbriques, ecrit un CSV Excel-compatible |
| `shiptify_get` | echappatoire vers n'importe lequel des 91 chemins GET : visites, creneaux, points de suivi, pieces jointes, chemins `carrier` et `galaxy` |

## Ce que ca ne sait pas faire, et pourquoi

- **Aucune ecriture. Par construction, pas par convention.** L'API expose 277
  operations, dont 186 en ecriture : creer un envoi, annuler, confirmer un
  enlevement, poster un message dans un chat Shiptify. Le serveur n'en expose
  **aucune**, et `shiptify_get` valide le chemin demande contre la liste blanche
  des 91 chemins GET du contrat avant d'emettre quoi que ce soit. Une tentative
  sur `/shipments/{id}/cancel` est refusee avec un message, pas envoyee.

  C'est la regle du vault appliquee au transport : **l'envoi est un geste
  humain**. Un `POST` sur Shiptify engage un transporteur.

- **Pas de total.** L'API ne renvoie jamais de compte : ni `total`, ni
  `X-Total-Count`. La seule fin de collection fiable est une page vide, et c'est
  ce que fait la pagination. Consequence a garder en tete : **le nombre de
  lignes rendu par un outil de liste n'est pas un volume**. Quand le resultat
  est tronque, le serveur le dit en majuscules. Pour un chiffre citable, passe
  par `shiptify_export_csv`, qui pagine tout le perimetre.

- **Pas d'agregation cote serveur.** Pas de `group by`, pas de somme. Shiptify
  rend des lignes. Un cout moyen par zone se calcule apres export, ou dans Power
  BI.

## Les limites de l'API, a connaitre avant de se cogner dedans

| Limite | Detail |
| --- | --- |
| Taille de page | **100 lignes maximum**, sauf `/visits` qui accepte 200. Impose par l'API, pas par le serveur |
| Pagination | `limit` / `offset`. **Tous les endpoints ne l'acceptent pas** : `/carriers/active` et les referentiels rendent un `HTTP 400 "limit is not allowed"` si on les envoie. Le serveur lit le contrat pour savoir lesquels paginer |
| `/events` | le parametre `event` est **obligatoire** : pas de journal global, on interroge un type d'evenement a la fois |
| `X-Account-ID` | accepte sur presque tous les appels. Non necessaire sur ce compte a ce jour, a renseigner si un appel rend un `HTTP 403` |
| Authentification | en-tete `Authorization: Api-Key <cle>`. Le prefixe est configurable (`SHIPTIFY_AUTH_PREFIX`) au cas ou Shiptify bascule sur `Bearer` |

## Mettre en service : une seule valeur a saisir

La configuration se lit sur **deux niveaux**, et c'est ce qui rend la mise en
service courte :

| Niveau | Ce qu'il porte | Ou |
| --- | --- | --- |
| **Equipe** | racine de l'API, prefixe d'authentification, plafond de pagination, delai d'attente | `08_ENGINE/04_mcp/00_config/shiptify.shared.env` — deja rempli, rien a y faire |
| **Poste** | **la cle d'API**, et elle seule | `/plugin` > shiptify > configuration |

Le poste est prioritaire sur l'equipe : un reglage d'equipe est un point de
depart commun, pas une contrainte.

**Aucun secret dans le fichier d'equipe, et ce n'est pas qu'une consigne** : le
serveur lit ce fichier a travers une **liste blanche**. Un `SHIPTIFY_API_KEY`
pose la-bas est ignore, et signale par `/shiptify-setup`. Le detail est dans
`08_ENGINE/04_mcp/00_config/README.md`.

**La saisie de la cle se fait dans l'interface de Claude Code**, jamais dans la
conversation. Deux moments possibles, au choix de l'utilisateur :

- **a l'installation** — Claude Code propose le champ « Cle d'API Shiptify » ;
- **plus tard, a la premiere utilisation** — le champ n'est pas obligatoire, on
  peut installer d'abord et configurer ensuite.

Dans une session, la commande guide **pas a pas**, une etape a la fois :

```bash
/shiptify-setup
```

Sept etapes : ce qu'il faut avoir en main, l'etat des lieux, la saisie de la
cle (les quatre clics, litteralement), le rangement sur la machine, le test de
connexion avec un tableau code HTTP -> cause reelle -> geste, un appel de
demonstration, et la cloture. Elle ne deroule jamais tout d'un bloc : elle
verifie avec un outil avant de passer a l'etape suivante.

Pour saisir ou corriger la cle a la main : `/plugin` > **shiptify** >
configuration > **Cle d'API Shiptify**. Le serveur reprend la cle au demarrage
suivant de la session.

### Trois outils pour le cycle de vie

| Outil | Ce qu'il fait |
| --- | --- |
| `shiptify_setup_status` | la cle est-elle saisie, d'ou vient-elle, est-elle rangee sur la machine, que faire ensuite. Ne rend jamais la cle en clair |
| `shiptify_save_key` | range sur la machine la cle **deja saisie dans l'interface**. Aucun argument |
| `shiptify_forget_key` | supprime la cle rangee. Ne touche pas a la configuration du plugin |

### Pourquoi aucun outil ne prend la cle en parametre

Un `shiptify_set_api_key("...")` serait plus simple a expliquer. Il ferait
passer le secret **par le fil de la conversation** : la cle entrerait dans le
contexte du modele et dans la transcription de la session. C'est precisement ce
que le champ `sensitive` du manifeste existe pour eviter — Claude Code collecte
la valeur lui-meme et la transmet au serveur par l'environnement.

`shiptify_save_key` ne prend donc **aucun argument** : il range la cle deja
saisie. Et la regle de l'equipe reste la regle — on ne demande jamais a
quelqu'un de coller une cle d'API dans un message.

### Ou la cle est rangee

**Pas dans le vault.** Cette bibliotheque est synchronisee SharePoint avec toute
l'equipe L&T : un `.env` pose ici partirait sur le drive partage. Le serveur
**refuse** d'ecrire ou de lire un `.env` situe dans un dossier synchronise.

Le magasin est, dans l'ordre : `%CLAUDE_PLUGIN_DATA%\.env` en mode plugin,
`%LOCALAPPDATA%\shiptify-mcp\.env` en installation directe. Droits NTFS
restreints au seul utilisateur courant, verifie a l'ecriture.

Ranger la cle n'est pas obligatoire — la configuration du plugin suffit a faire
fonctionner le connecteur. Ca sert a deux choses : la cle survit a une
reinstallation du plugin, et l'installation directe la trouve aussi.

**Le serveur refuse de lire un `.env` situe dans un dossier synchronise.** Ce
n'est pas une recommandation dans un README, c'est verifie a l'execution, et
`shiptify_doctor` le dit.

Emplacements essayes, dans l'ordre :

1. `SHIPTIFY_ENV_FILE` si la variable est posee ;
2. `%CLAUDE_PLUGIN_DATA%\.env` — **le bon endroit en mode plugin** (voir le
   piege Windows ci-dessous) ;
3. `%LOCALAPPDATA%\shiptify-mcp\.env` — le bon endroit en installation directe ;
4. `~/.shiptify/.env` ;
5. a cote de `server.py` — refuse ici, puisque le vault est synchronise.

Le modele des variables est dans `.env.example`, qui ne porte aucune valeur. Le
`.gitignore` du marketplace exclut tout `.env` : ceinture et bretelles si un
fichier atterrit la par accident.

### Le piege Windows de l'interpreteur empaquete

Si `python` resout vers un interpreteur **empaquete** (Microsoft Store, ou Python
Manager), ses processus enfants heritent d'une **vue virtualisee de
`%LOCALAPPDATA%`**. Mesure sur ce poste : le meme interpreteur de venv voit
`['.env', 'venv']` lance directement, et `['venv']` seulement lance par le
Python du Store — `LOCALAPPDATA` valant pourtant la meme chaine.

Donc **un `.env` pose sous `%LOCALAPPDATA%` peut etre invisible au serveur lance
par le plugin**, sans aucune erreur. En mode plugin, prefere la configuration du
plugin (immunisee) ou un `.env` dans `%CLAUDE_PLUGIN_DATA%`. `shiptify_doctor`
detecte le cas et le nomme, au lieu de rapporter « absent » et d'envoyer
chercher un fichier qui est bien la.

### Les variables

| Variable | Defaut | Role |
| --- | --- | --- |
| `SHIPTIFY_API_KEY` | — | **obligatoire**, et **jamais dans le fichier d'equipe** (liste blanche). Mettre entre guillemets doubles : une cle contient souvent `%`, `*`, `/` |
| `SHIPTIFY_BASE_URL` | `https://api.shiptify.com` | racine de l'API |
| `SHIPTIFY_AUTH_PREFIX` | `Api-Key` | prefixe de l'en-tete `Authorization` |
| `SHIPTIFY_ACCOUNT_ID` | vide | en-tete `X-Account-ID`, si un appel rend un 403 |
| `SHIPTIFY_MAX_PAGES` | `60` | garde-fou : 60 pages de 100 = 6000 lignes par requete. A relever pour un export annuel |
| `SHIPTIFY_TIMEOUT_S` | `60` | delai par appel HTTP |
| `SHIPTIFY_EXPORT_DIR` | voir ci-dessous | dossier des exports CSV. Par defaut : `%CLAUDE_PLUGIN_DATA%\exports` en mode plugin, `<vault>/Assets/shiptify` en installation dans le vault, sinon `<dossier courant>/shiptify-exports` |
| `SHIPTIFY_SHARED_ENV` | vide | chemin explicite du fichier d'equipe, si la bibliotheque SharePoint n'est pas synchronisee a l'endroit attendu |
| `VU_ENGINE_DIR` | vide | racine `08_ENGINE` explicite, meme usage |

**Ou chaque variable est lue, dans l'ordre** : environnement du processus (la
configuration du plugin) > `.env` local hors du vault > fichier d'equipe
`08_ENGINE/04_mcp/00_config/shiptify.shared.env` > defaut du serveur. Seules
`SHIPTIFY_BASE_URL`, `SHIPTIFY_AUTH_PREFIX`, `SHIPTIFY_ACCOUNT_ID`,
`SHIPTIFY_MAX_PAGES`, `SHIPTIFY_TIMEOUT_S` et `SHIPTIFY_EXPORT_DIR` peuvent
venir du fichier d'equipe.

## Installation

### Par le plugin — la voie normale, et celle qui se partage

Ce connecteur est empaquete dans le marketplace `vu-transport`. Deux commandes,
detaillees dans le [README du marketplace](../../README.md) :

```bash
/plugin marketplace add <organisation>/<depot>
```

```bash
/plugin install shiptify@vu-transport
```

Claude Code demande alors la cle d'API. **Les dependances Python s'installent
toutes seules au premier demarrage** : `server/bootstrap.py` cree
l'environnement, installe `mcp` et `httpx`, puis passe la main au serveur. Rien
a faire a la main, et rien a expliquer a un collegue.

### En installation directe — sans passer par le plugin

Utile pour mettre au point, ou sur un poste ou le plugin n'est pas voulu.
L'environnement virtuel est cree **hors du vault**, dans
`%LOCALAPPDATA%\shiptify-mcp\venv` : on ne synchronise pas quelques milliers de fichiers
de dependances.

```bash
powershell -ExecutionPolicy Bypass -File "08_ENGINE/03_plugins/plugin_vu_transport/plugins/shiptify/server/install.ps1"
```

Le script cree aussi le `.env` s'il manque. Pour y poser la cle du meme geste
(l'operation est idempotente, elle peut etre rejouee) :

```bash
powershell -ExecutionPolicy Bypass -File "08_ENGINE/03_plugins/plugin_vu_transport/plugins/shiptify/server/install.ps1" -ApiKey "<cle>"
```

Verifier :

```bash
& "$env:LOCALAPPDATA\shiptify-mcp\venv\Scripts\python.exe" "08_ENGINE\03_plugins\plugin_vu_transport\plugins\shiptify\server\server.py" doctor
```

Enregistrer dans Claude Code — **la cle ne passe pas par la ligne de commande**,
le serveur lit son `.env` :

```bash
claude mcp add shiptify --scope user -- "%LOCALAPPDATA%\shiptify-mcp\venv\Scripts\python.exe" "<chemin absolu>\08_ENGINE\03_plugins\plugin_vu_transport\plugins\shiptify\server\server.py"
```

**N'installe pas les deux en meme temps** : le plugin et un enregistrement
manuel exposeraient deux fois les memes outils.

## Comment l'utiliser en session

L'enchainement qui marche :

1. `shiptify_list_paths` ou `shiptify_dictionary` -> les noms exacts des filtres
   et des valeurs. **Sauter cette etape, c'est deviner un nom de parametre** et
   se prendre un 400.
2. `shiptify_list_carriers` / `shiptify_list_locations` -> les identifiants a
   mettre dans les filtres. Un nom Shiptify n'est pas le nom maison du
   transporteur : `Prévoté I Meru` cote Shiptify, prospect messagerie cote
   vault. Croise avec `01_CONTEXTE/PORTEFEUILLE_ET_ZONES.md`.
3. `shiptify_list_shipments` avec un perimetre de dates serre, pour regarder.
4. `shiptify_export_csv` des que le perimetre depasse quelques centaines de
   lignes, ou des qu'un chiffre doit etre cite.

### La projection de colonnes, et pourquoi elle existe

Un envoi Shiptify aplati porte **114 colonnes**. Les rendre toutes dans la
conversation la sature pour rien. Les outils de liste acceptent donc `fields`,
une liste de colonnes en notation pointee :

```
fields="id,status,carrier.name,address_dest.city,address_dest.zipcode,price"
```

`fields="*"` rend tout. Par defaut, les envois sortent avec une projection
courte des colonnes utiles. Les autres collections sortent completes.

### L'export CSV : ce que faisait le script Power Query

`shiptify_export_csv` reprend exactement la mecanique du script d'origine :
meme pagination `limit`/`offset` jusqu'a page vide, et **meme aplatissement**
des objets imbriques en colonnes pointees — `address_dest.city`,
`carrier.name`, `shipment_mode.name`. Ce que faisait
`Table.ExpandRecordColumn`, en une fois et sans lister les champs a la main.

Les tableaux (`contents`, `carrier_galaxy_services`) sortent en JSON compact
dans leur colonne : ils n'ont pas de forme de colonne stable, et les eclater
multiplierait les lignes.

CSV point-virgule, UTF-8 avec BOM : il s'ouvre dans Excel FR sans passer par
l'assistant d'import.

```
shiptify_export_csv(
  path="/shipments/",
  query_json='{"created_date_from": "2026-01-01"}',
  filename="shipments_2026.csv"
)
```

**Le message d'export dit s'il a ete tronque.** Un fichier tronque qui passe
pour complet, c'est un volume faux dans une revue transporteur.

## Etat de la verification

Recette passee sur ce poste le 2026-08-27, **contre l'API de production**, sur
`mcp 1.29.1` / `httpx 0.28.1` / Python 3.14.7 :

- handshake MCP stdio complet, **20 outils** exposes avec leurs schemas ;
- `doctor` : `.env` lu hors du vault, cle masquee a l'affichage,
  `GET /` et `GET /accounts/` en `HTTP 200`, code de sortie 0 ;
- **purete du flux stdio verifiee** : les journaux `httpx` partent sur la sortie
  d'erreur, stdout ne porte que du JSON-RPC. Un journal egare sur stdout
  casserait le protocole MCP en silence ;
- donnees reelles rendues : **119 transporteurs actifs**, 10 modes de transport,
  les lieux (Amblainville, Coslada, Zibido San Giacomo), les envois du jour avec
  leur transporteur et leur prix, les tags, une facture avec ses rattachements ;
- `shiptify_get_shipment` avec `include=tracking-points,contents,sscc` : les
  trois sous-ressources remontent, et un include inconnu rend un message qui
  liste les valeurs possibles au lieu d'echouer ;
- export CSV : **104 envois sur 114 colonnes**, relu depuis le disque, BOM UTF-8
  present, colonnes pointees conformes au script Power Query, tableaux en JSON ;
- garde-fous verifies, chacun refuse avec un message et **sans appel reseau** :
  chemin d'ecriture (`/shipments/12/cancel`), chemin inconnu, parametres colles
  dans le chemin, `query_json` invalide, referentiel inconnu, demande de
  transport sans identifiant ni reference.

**Deux bugs trouves par la recette et corriges** — ils sont notes ici parce
qu'ils se reproduiront chez le prochain serveur MCP de ce vault :

1. **Le decorateur d'erreurs effacait la signature des outils.** FastMCP lit la
   signature de la fonction pour publier le schema : sans `functools.wraps`,
   les 16 outils decores sortaient avec deux parametres `(args, kwargs)` et
   etaient **inappelables**. Le handshake, lui, passait tres bien. Un test qui
   se contente de lister les outils ne voit rien.
2. **L'export envoyait `limit`/`offset` a des endpoints qui les refusent.**
   `/carriers/active` rend un `HTTP 400 "limit is not allowed"`. Le serveur lit
   maintenant le contrat pour savoir quels endpoints paginer.

### Ce qui reste a verifier en usage reel

- **`GET /accounts/` rend une liste vide** alors que tous les autres appels
  fonctionnent. Sans consequence a ce jour, `X-Account-ID` n'etant pas
  necessaire. A regarder si un appel se met a rendre un 403.
- **La surface facturation est pauvre sur cette cle** : `/invoices` rend une
  seule facture, de 2024, et `/galaxy/invoice-lines` rien sur juin-aout 2026.
  Soit la facturation Shiptify n'est pas alimentee pour ce compte, soit elle
  demande une portee que la cle n'a pas. **A trancher avant de compter sur ce
  serveur pour un controle de facture** : c'est une question a poser a Shiptify,
  pas un bug du serveur.
- La pagination s'arrete sur une **page vide**, jamais sur une page partielle.
  C'est un appel de plus par collection, et c'est volontaire : s'arreter sur une
  page partielle sous-compterait en silence si l'API filtrait apres avoir
  applique la limite.

## Tenir la liste des chemins a jour

`openapi_get_paths.json` est la liste blanche : les 91 chemins GET, leurs
resumes et leurs parametres, extraits du contrat OpenAPI le 2026-08-27. Il est
versionne a cote du code, et le serveur refuse de demarrer sans lui.

Quand Shiptify fait evoluer son API, le fichier se regenere depuis
`https://api-docs.shiptify.com/shiptify-public-api.openapi.json` — la page de
documentation est une application Swagger UI, le contrat est le JSON qu'elle
charge. Un chemin qui n'est pas dans ce fichier n'est pas atteignable : c'est
exactement l'effet voulu.

> **Jalon Webfacto.** Ce serveur est un outil de poste, en lecture seule, sur
> une cle existante : c'est du prototypage individuel, libre. **Le brancher a un
> flux du SI, l'exposer a plusieurs utilisateurs, ou en faire une automatisation
> qui tourne sans personne devant depasse ce cadre** : avant tout demarrage en
> developpement ou integration au SI, ce cas d'usage doit etre valide par la
> Webfacto (cadrage besoin, faisabilite, securite, priorisation).

## Rotation de la cle

La cle utilisee pour la recette a circule en clair : elle etait ecrite dans le
script Power Query, et elle a ete collee dans une conversation. **Elle est a
considerer comme compromise et a faire tourner cote Shiptify.**

Le geste, une fois la nouvelle cle en main :

```bash
powershell -ExecutionPolicy Bypass -File "08_ENGINE/03_plugins/plugin_vu_transport/plugins/shiptify/server/install.ps1" -ApiKey "<nouvelle cle>"
```

Et dans le meme mouvement, sortir la cle du script Power Query : dans Power BI,
elle a sa place dans un **parametre** de type texte, pas dans le corps de la
requete — le script d'origine le notait deja en commentaire sans le faire.

_Ariane, 2026-08-27._
