---
updated: 2026-08-28
updated_by: Guillaume_Champin
type: process
---

# Plugin `peripass` — connecteur MCP en lecture seule

Serveur MCP qui donne a Claude Code un acces **en lecture** au yard management
Peripass : les visiteurs (creneaux, arrivees, check-in, departs, temps
d'attente et de rotation), les assets sur la cour, les taches, les profils, les
tableaux de dispatch et les bornes d'accueil.

C'est le portage du script Power Query qui ramenait `/visitors` des deux sites
dans Power BI, avec quatre differences qui comptent : les cles d'API ne sont
plus ecrites dans la requete, les 18 chemins de lecture de l'API sont
atteignables et pas seulement un, les champs personnalises sont decouverts au
lieu d'etre listes en dur, et le resultat est interrogeable en session au lieu
d'attendre un rafraichissement de modele.

Peripass porte **ce qui se passe entre l'arrivee d'un camion sur le site et son
depart**. Ce serveur est donc l'entree naturelle pour une question de respect
de creneau, de temps d'immobilisation ou de rotation sur site — et le chainon
qui manque quand un transporteur conteste un retard, puisque Peripass a l'heure
du creneau et l'heure d'arrivee reelle.

## La particularite : deux bases, pas une

**Peripass n'a pas une base, il en a une par site.** AUV (Moulins /
Montbeugny) et AMB (Amblainville) sont deux tenants distincts, avec deux cles
d'API et deux jeux d'identifiants. Rien n'est partage entre eux.

C'est la difference structurante avec les autres connecteurs de l'equipe, et
elle se retrouve partout :

| Consequence | Ce que fait le serveur |
| --- | --- |
| Un id n'existe que dans son site | Tout outil qui prend un id **exige** `site`. Le visiteur 4218 existe des deux cotes et designe deux camions differents |
| Un chiffre doit couvrir les deux sites | Les outils de liste interrogent **les deux par defaut** et posent une colonne `site` en tete — ce que faisait le `Table.Combine` du script d'origine |
| `max_rows` sur deux sites | Il s'applique **par site**, pas au total. L'entete de chaque rendu le rappelle |
| Un site qui tombe | Le rendu dit **PARTIEL** en majuscules. Un chiffre sur un seul site n'est pas le chiffre de l'entrepot |
| Les referentiels peuvent differer | Rien ne garantit que les profils, les quais et les champs personnalises portent les memes noms des deux cotes. `peripass_fields` est la pour le constater avant de comparer |

## Ce que ca sait faire

| Outil | Ce qu'il rend |
| --- | --- |
| `peripass_setup_status` | ou en est la mise en service, **site par site** : cle saisie, origine, rangee ou non, quoi faire ensuite |
| `peripass_save_key` | range sur la machine les cles deja saisies dans l'interface. Aucun argument |
| `peripass_forget_key` | supprime la cle rangee d'un site, ou de tous |
| `peripass_doctor` | configuration lue, cles presentes (masquees), et deux vrais appels par site. A appeler quand un appel echoue |
| `peripass_sites` | les sites configures, leur racine d'API, leur etat, leur quota restant |
| `peripass_list_paths` | les 18 chemins GET avec leurs filtres et leurs valeurs permises. A lire avant de deviner un nom de parametre |
| `peripass_list_visitors` | **l'outil principal** : les visiteurs, filtres par periode de creneau, d'arrivee, de check-in ou de depart, par statut, par profil, par champ personnalise |
| `peripass_get_visitor` | un visiteur par reference — `id:10`, ou `tms_id:90` sur un champ personnalise. Cherche sur les deux sites et etiquette les reponses |
| `peripass_visitor_history` | l'historique d'un visiteur : les changements d'etat, horodates |
| `peripass_visitor_detail` | le detail d'un visiteur et, au choix, son dispatch, ses profils, ses pieces jointes, ses types de document autorises |
| `peripass_visitor_attachments` | les pieces jointes d'un visiteur (les metadonnees, pas le contenu) |
| `peripass_fields` | **les champs personnalises reellement presents**, avec leur taux de remplissage et des exemples. A appeler avant toute recherche par champ |
| `peripass_list_assets` / `peripass_get_asset` | les remorques, caisses et contenants sur la cour, et leur statut |
| `peripass_list_tasks` / `peripass_get_task` | les taches de cour : ce qui est demande aux operateurs, et ou ca en est |
| `peripass_list_certified_persons` | les habilitations. **Donnees nominatives** : a lire, pas a recopier |
| `peripass_referential` | les profils de visiteur, les tableaux de dispatch, les bornes d'accueil |
| `peripass_export_csv` | **le remplacant du script Power Query** : pagine une collection entiere sur les deux sites, aplatit les objets imbriques, ecrit un CSV Excel-compatible |
| `peripass_get` | echappatoire vers n'importe lequel des 18 chemins GET |

## Ce que ca ne sait pas faire, et pourquoi

- **Aucune ecriture. Par construction, pas par convention.** Le contrat expose
  60 operations, dont 42 en ecriture : creer ou mettre a jour un visiteur, le
  passer en `checkedin`, `blocked` ou `departed`, deplacer un asset, poser une
  tache, activer une borne. Le serveur n'en expose **aucune**, et
  `peripass_get` valide le chemin demande contre la liste blanche des 18
  chemins GET du contrat avant d'emettre quoi que ce soit. Une tentative sur
  `/visitors/12/status/checkedin` est refusee avec un message, pas envoyee.

  C'est la regle du vault appliquee a la cour : **l'envoi est un geste humain**.
  Un changement de statut dans Peripass fait bouger un camion.

- **Deux chemins GET sont volontairement injoignables.**
  `/visitors/validateeticket` attend la cle d'API **en parametre d'URL** : un
  secret dans une query string finit dans les journaux des proxies, et c'est
  de toute facon une operation de borne d'accueil. Le telechargement de piece
  jointe rend un **binaire**, il n'a rien a faire dans une conversation.

- **Pas de total.** L'API ne renvoie jamais de compte. La seule fin de
  collection fiable est une page vide, et c'est ce que fait la pagination —
  la meme logique que le `List.Generate ... each [Cnt] > 0` du script d'origine.
  Consequence a garder en tete : **le nombre de lignes rendu par un outil de
  liste n'est pas un volume**. Quand le resultat est tronque, le serveur le dit
  en majuscules, et il nomme le site tronque.

- **Pas d'agregation cote serveur.** Pas de `group by`, pas de somme. Peripass
  rend des lignes. Un temps d'attente moyen par transporteur se calcule apres
  export, ou dans Power BI.

## Les limites de l'API, a connaitre avant de se cogner dedans

| Limite | Detail |
| --- | --- |
| Taille de page | **100 lignes maximum**. Impose par l'API |
| Pagination | `pageNumber` **zero-based** + `pageSize`, tous deux **requis** quand ils existent. Tous les endpoints ne paginent pas : le serveur lit le contrat pour savoir lesquels |
| **Filtre mal forme** | **Peripass l'ignore en SILENCE** : « If format is incorrect we will ignore the filter ». Une date mal ecrite ne rend pas une erreur, elle rend **toutes les lignes**. Le serveur valide donc les enums et refuse une periode sans champ de reference, avant d'emettre |
| Recherche | `searchText` est **exacte et sensible a la casse**. Pas de recherche partielle, pas de « contient » |
| Champs personnalises | Aucun schema expose. Les cles de l'objet `fields` dependent de la configuration du tenant, et `searchField` attend le **nom technique exact**. D'ou `peripass_fields` |
| Quota | Calcule sur le **nombre de ressources lues**, pas de requetes : ramener 6000 visiteurs coute 6000 unites. Un `HTTP 429` veut dire « trop large », pas « trop souvent ». En-tetes `X-RateLimit-*` remontes par `peripass_sites` |
| **Codes d'erreur** | **Mesure le 2026-08-28 : Peripass rend un `403` a corps vide pour une cle absente, une cle fausse ET une cle sans droits.** Sa documentation annonce un `401` pour l'en-tete manquante : ce n'est pas ce qui se passe. Le message d'erreur du serveur enonce les trois cas plutot que d'en designer un au hasard |
| Fuseaux | Les horodatages sont rendus dans l'heure du tenant avec le decalage UTC. **Sur un tenant ancien le decalage peut manquer** — une comparaison AUV / AMB devient alors approximative, et ca se dit |
| Authentification | En-tete `X-API-Key`, une cle par tenant |

## La configuration, en deux couches

Depuis la v1.1.0, ce connecteur suit la convention d'equipe des connecteurs MCP
— celle de `shiptify` et de `yooz-factures`. Ce qui est **identique sur tous
les postes** ne se ressaisit plus vingt-six fois :

| Couche | Ou | Ce qu'elle porte |
| --- | --- | --- |
| **l'equipe** | `08_ENGINE/04_mcp/00_config/peripass.shared.env` | les sites, leurs racines d'API, leurs tenants, les plafonds de pagination — et, selon la decision d'equipe, les cles d'API de tenant |
| **le poste** | `/plugin > peripass > configuration`, ou `peripass.env` hors du vault | ce qui est propre a ce poste : une cle nominative, un tenant de recette, un dossier d'export |

**Ordre de priorite, du plus fort au plus faible :** configuration du plugin >
`peripass.env` du poste > fichier d'equipe > defaut du serveur. **Le poste
passe devant l'equipe**, et non l'inverse : un reglage d'equipe est un point de
depart commun, pas une contrainte. C'est ce qui permet de tester un tenant de
recette sans toucher au fichier partage — donc sans casser les vingt-cinq
autres postes.

### Ce que le fichier d'equipe a le droit de porter

Le serveur applique une **liste blanche a la lecture**. Passent :
`PERIPASS_SITES`, `PERIPASS_BASE_URL`, `PERIPASS_PAGE_SIZE`,
`PERIPASS_MAX_PAGES`, `PERIPASS_TIMEOUT_S`, et par site les suffixes
`_API_KEY`, `_BASE_URL`, `_TENANT`, `_LABEL`. Le nom du site n'est pas code en
dur : un troisieme yard s'ajoutera sans toucher au serveur.

Tout le reste est **ignore et signale** par `/peripass-setup`. Ce n'est pas une
barriere anti-secret — les cles de tenant y sont admises — c'est un **garde-fou
contre la faute de frappe** : une variable mal orthographiee dans un fichier
partage serait sinon ignoree en silence sur vingt-six postes a la fois.

Deux categories sont refusees explicitement, avec un message a part :

- **les chemins locaux** (`PERIPASS_EXPORT_DIR`, `PERIPASS_ENV_FILE`) — un
  chemin valable ici n'existe pas sur les autres postes ;
- **`PERIPASS_API_KEY` sans prefixe de site** — ambigu des qu'il y a deux yards.

### Les cles d'API dans le fichier d'equipe : une decision, pas un defaut

Le serveur les **accepte**, comme `shiptify` depuis le 2026-08-28 : une cle
Peripass est une cle de **tenant**, pas de personne. Elle n'identifie personne,
et la faire ressaisir vingt-six fois n'ajoute aucune protection — seulement
vingt-six mises en service qui echouent et une rotation impossible a propager.

Ce que ca implique et qu'il faut assumer : **la bibliotheque est lisible par
toute l'equipe L&T, donc la cle l'est aussi.** Tant que les lignes restent
commentees dans `peripass.shared.env`, chacun saisit sa cle dans `/plugin` et
le connecteur marche exactement pareil — il demande juste une saisie par poste.

Le jour ou une cle doit cesser d'etre partagee — cle nominative, prestataire
externe, audit — la reponse est la configuration du plugin sur le poste, qui
passe **devant** le fichier d'equipe. On ne retire pas la ligne, on la
surcharge chez soi.

Une valeur lue dans le fichier d'equipe **n'est jamais versee dans
`os.environ`** : elle reste dans un cache interne, et n'est donc pas heritee
par un sous-processus. Son nom est affiche par les diagnostics, jamais sa
valeur.

## Mettre en service

**La saisie d'une cle se fait dans l'interface de Claude Code**, jamais dans la
conversation. Mais commence par lancer `/peripass-setup` : **si l'equipe
fournit deja les cles, il n'y a rien a saisir du tout.**

Dans une session, la commande guide de bout en bout :

```bash
/peripass-setup
```

Elle regarde ou on en est site par site, dit ce que l'equipe fournit deja et ce
qu'il reste a saisir, range les cles sur la machine, teste la connexion, et
fait deux appels de demonstration — dont `peripass_fields`, qui montre du meme
geste si les deux sites portent les memes champs personnalises.

Pour saisir ou corriger une cle a la main : `/plugin` > **peripass** >
configuration. Le serveur reprend les valeurs au demarrage suivant de la
session.

**Si `/peripass-setup` affiche `Config d'equipe : aucune`**, la bibliotheque
SharePoint « Transport BtoC » n'est pas atteignable depuis ce poste. Le
connecteur tourne quand meme sur ses valeurs par defaut, mais tout ce que
l'equipe a regle est perdu. Deux sorties : synchroniser la bibliotheque, ou
renseigner le champ **« Chemin du fichier de configuration d'equipe »** dans
`/plugin` (variable `PERIPASS_SHARED_ENV`). Le connecteur cherche seul, dans
l'ordre : `PERIPASS_SHARED_ENV`, `VU_ENGINE_DIR`, une remontee depuis son
propre code, puis `<profil>\CAFOM\<bibliotheque>\08_ENGINE`.

### Une seule cle n'est pas une mise en service

Le connecteur fonctionne avec un seul site configure, et c'est un piege : il
repond, les chiffres ont l'air normaux, et ils ne couvrent que la moitie du
perimetre. `peripass_sites` et l'entete de chaque rendu nomment les sites
interroges — mais la vraie reponse est de saisir les deux cles.

### Pourquoi aucun outil ne prend une cle en parametre

Un `peripass_set_api_key("...")` serait plus simple a expliquer. Il ferait
passer le secret **par le fil de la conversation** : la cle entrerait dans le
contexte du modele et dans la transcription de la session. C'est precisement ce
que le champ `sensitive` du manifeste existe pour eviter — Claude Code collecte
la valeur lui-meme et la transmet au serveur par l'environnement.

`peripass_save_key` ne prend donc **aucun argument** : il range les cles deja
saisies. Et la regle de l'equipe reste la regle — on ne demande jamais a
quelqu'un de coller une cle d'API dans un message.

### Ou les cles sont rangees

**Pas dans le vault.** Cette bibliotheque est synchronisee SharePoint avec
toute l'equipe L&T : un fichier de configuration pose ici partirait sur le
drive partage. Le serveur **refuse** d'ecrire ou de lire une configuration
situee dans un dossier synchronise, et `peripass_doctor` le dit.

Le magasin est `~/.peripass-mcp/peripass.env`, ou `%CLAUDE_PLUGIN_DATA%\peripass.env`
en mode plugin. Droits NTFS restreints au seul utilisateur courant, verifie a
l'ecriture.

Emplacements essayes, dans l'ordre :

1. `PERIPASS_ENV_FILE` si la variable est posee ;
2. `%CLAUDE_PLUGIN_DATA%\peripass.env` — le bon endroit en mode plugin ;
3. `~/.peripass-mcp/peripass.env` — le bon endroit en installation directe ;
4. a cote de `server.py` — refuse ici, puisque le vault est synchronise.

**Le fichier d'equipe, lui, vit bien dans le drive partage** — c'est son objet.
Il ne porte que ce que l'equipe a decide de partager, et le serveur ne reprend
d'ici que les variables de la liste blanche. `peripass_save_key` recopie en
local les cles actives, y compris celles venues de l'equipe : c'est ce qui rend
un poste autonome de la bibliotheque synchronisee. En revanche il **ne recopie
pas** une racine d'API ou un tenant fournis par l'equipe — les figer en local
reviendrait a se rendre sourd a leur prochaine correction, puisque le poste
passe devant l'equipe.

Ranger les cles n'est pas obligatoire — la configuration du plugin suffit a
faire fonctionner le connecteur. Ca sert a deux choses : les cles survivent a
une reinstallation du plugin, et l'installation directe les trouve aussi.

### Deux pieges Windows deja payes ailleurs, et evites ici

**La racine locale n'est pas `%LOCALAPPDATA%`, elle est `~/.peripass-mcp`.** Si
`python` resout vers un interpreteur **empaquete** (Microsoft Store, ou Python
Manager), ses processus enfants heritent d'une **vue virtualisee de
`%LOCALAPPDATA%`**. Mesure en empaquetant Yooz le 2026-08-27 : PowerShell et
l'interpreteur du venv lance directement voient `['venv', 'yooz.env']` ; le
meme interpreteur lance par l'alias du Store ne voit que `['venv']`. Le plugin
ecrirait d'un cote, un terminal lirait de l'autre, **sans aucune erreur**. Le
profil utilisateur, lui, n'est pas virtualise. `peripass_doctor` signale quand
meme l'interpreteur empaquete, pour pouvoir nommer le symptome.

**Le fichier s'appelle `peripass.env`, pas `.env`, et il s'ecrit sans BOM.**
Chez Yooz, un fichier nomme `.env` a ete ignore en silence par un connecteur
qui cherchait `yooz.env`, et un BOM UTF-8 non consomme a colle trois octets
invisibles devant le premier nom de variable, qui est devenu introuvable. Le
serveur lit en `utf-8-sig` et ecrit sans BOM.

### Les variables

La colonne **Couche** dit ou la variable a vocation a vivre : `equipe` dans
`peripass.shared.env`, `poste` dans `/plugin` ou `peripass.env`. Une variable
`poste` posee dans le fichier d'equipe est ignoree et signalee.

| Variable | Couche | Defaut | Role |
| --- | --- | --- | --- |
| `PERIPASS_SITES` | equipe | `AUV,AMB` | les sites a interroger, separes par des virgules |
| `PERIPASS_<SITE>_API_KEY` | equipe ou poste | — | **obligatoire par site**. Envoyee en `X-API-Key`. Mettre entre guillemets doubles |
| `PERIPASS_<SITE>_BASE_URL` | equipe | l'hote historique du site | racine d'API. A changer pour passer a la racine partagee |
| `PERIPASS_<SITE>_TENANT` | equipe | vide | nom du tenant, envoye en parametre `tenant`. Necessaire seulement avec la racine partagee |
| `PERIPASS_<SITE>_LABEL` | equipe | vide | libelle lisible du site. Documentaire |
| `PERIPASS_PAGE_SIZE` | equipe | `100` | taille de page. Plafonne a 100 par l'API |
| `PERIPASS_MAX_PAGES` | equipe | `60` | garde-fou : 60 pages de 100 = 6000 lignes **par site**. A relever pour un export annuel, en gardant le quota en tete |
| `PERIPASS_TIMEOUT_S` | equipe | `60` | delai par appel HTTP |
| `PERIPASS_EXPORT_DIR` | **poste seul** | voir ci-dessous | dossier des exports CSV. Par defaut : `%CLAUDE_PLUGIN_DATA%\exports` en mode plugin, `<vault>/Assets/peripass` en installation dans le vault |
| `PERIPASS_ENV_FILE` | **poste seul** | — | emplacement explicite du fichier de configuration local |
| `PERIPASS_SHARED_ENV` | **poste seul** | — | emplacement explicite du fichier d'equipe, si la bibliotheque n'est pas synchronisee a l'endroit attendu |
| `VU_ENGINE_DIR` | **poste seul** | — | racine `08_ENGINE` a utiliser. Alternative a la precedente, commune aux connecteurs de l'equipe |

### Les deux formes d'URL

Le connecteur accepte les deux, et part sur la premiere :

| Forme | URL | Quand |
| --- | --- | --- |
| **historique** (defaut) | `https://vente-unique-logistics-<site>.peripass.app/api/v2` | celle du script Power Query, eprouvee. Peripass la maintient pour les integrations existantes |
| **moderne** | `https://restapiprd.peripass.app/api/v2` + parametre `tenant` | celle que Peripass recommande pour les nouvelles integrations. Demande `PERIPASS_<SITE>_TENANT` |

Peripass annonce que la forme historique « sera retiree a une date non
precisee ». Le jour ou ca arrive, il n'y a rien a recoder : deux variables
d'environnement suffisent a basculer.

## Installation

### Par le plugin — la voie normale, et celle qui se partage

Ce connecteur est empaquete dans le marketplace `vu-transport`. Deux commandes,
detaillees dans le [README du marketplace](../../README.md) :

```bash
/plugin marketplace add <organisation>/<depot>
```

```bash
/plugin install peripass@vu-transport
```

Claude Code demande alors les deux cles. **Les dependances Python s'installent
toutes seules au premier demarrage** : `server/bootstrap.py` cree
l'environnement, installe `mcp` et `httpx`, puis passe la main au serveur.

### En installation directe — sans passer par le plugin

Utile pour mettre au point, ou sur un poste ou le plugin n'est pas voulu.
L'environnement virtuel est cree **hors du vault**, dans
`~/.peripass-mcp/venv` : on ne synchronise pas quelques milliers de fichiers de
dependances.

```bash
powershell -ExecutionPolicy Bypass -File "08_ENGINE/03_plugins/plugin_vu_transport/plugins/peripass/server/install.ps1"
```

Le script cree aussi le fichier de configuration s'il manque. Pour y poser les
cles du meme geste (l'operation est idempotente, elle peut etre rejouee) :

```bash
powershell -ExecutionPolicy Bypass -File "08_ENGINE/03_plugins/plugin_vu_transport/plugins/peripass/server/install.ps1" -AuvApiKey "<cle AUV>" -AmbApiKey "<cle AMB>"
```

Verifier, dans l'ordre — les controles hors reseau d'abord, ils ne consomment
aucun quota :

```bash
& "$env:USERPROFILE\.peripass-mcp\venv\Scripts\python.exe" "08_ENGINE\03_plugins\plugin_vu_transport\plugins\peripass\server\test_offline.py"
```

```bash
& "$env:USERPROFILE\.peripass-mcp\venv\Scripts\python.exe" "08_ENGINE\03_plugins\plugin_vu_transport\plugins\peripass\server\server.py" doctor
```

Enregistrer dans Claude Code — **les cles ne passent pas par la ligne de
commande**, le serveur lit son fichier :

```bash
claude mcp add peripass --scope user -- "%USERPROFILE%\.peripass-mcp\venv\Scripts\python.exe" "<chemin absolu>\08_ENGINE\03_plugins\plugin_vu_transport\plugins\peripass\server\server.py"
```

**N'installe pas les deux en meme temps** : le plugin et un enregistrement
manuel exposeraient deux fois les memes outils.

## Comment l'utiliser en session

L'enchainement qui marche :

1. `peripass_sites` -> quels sites repondent, et sous quel code.
2. `peripass_referential` (`profiles`, `dispatchdashboards`) -> les noms reels
   des profils et des quais, **sur chaque site**. C'est la premiere cause d'un
   chiffre qui ne se compare pas d'un site a l'autre.
3. `peripass_fields` -> les champs personnalises reellement presents. **Sauter
   cette etape, c'est deviner un nom technique** et se prendre un filtre ignore
   en silence.
4. `peripass_list_visitors` avec une periode serree, pour regarder.
5. `peripass_export_csv` uniquement si l'utilisateur a demande un fichier.

### Choisir le bon `timestamp_field`

C'est le filtre central du connecteur, et il faut choisir **sur quoi** on
filtre :

| Valeur | Ce qu'elle date | Question type |
| --- | --- | --- |
| `SlotStart` | le creneau **planifie** | « combien de RDV etaient prevus » |
| `Arrived` | l'arrivee **reelle** | « combien de camions se sont presentes » |
| `CheckedIn` | l'entree en operation | analyse de quai |
| `Departed` | le depart | rotation complete |

**Planifie et reel ne se comptent pas ensemble.** L'ecart entre le nombre de
`SlotStart` et le nombre de `Arrived` sur la meme periode, c'est le taux de
non-presentation : c'est un resultat, pas une incoherence a lisser.

### La projection de colonnes, et le joker de prefixe

Un visiteur aplati porte une quarantaine de colonnes systeme, plus autant de
champs personnalises. Les rendre toutes sature la conversation pour rien. Les
outils de liste acceptent donc `fields`, une liste de colonnes en notation
pointee :

```
fields="site,id,status,fields.Transporteur,visitorTimeStampArrived"
```

Nouveaute par rapport au connecteur shiptify : **le joker de prefixe**.
`fields.*` prend tous les champs personnalises reellement presents, sans avoir
a les nommer. C'est exactement ce que le script Power Query faisait a la main —
en listant « Numero RDV », « Transporteur », « FRAQ »… — et c'est ce qui le
cassait des qu'un champ etait renomme cote Peripass.

`fields="*"` rend tout. Par defaut, les visiteurs sortent avec une projection
courte plus `fields.*`.

### L'export CSV : ce que faisait le script Power Query

`peripass_export_csv` reprend exactement la mecanique du script d'origine :
meme pagination `pageNumber`/`pageSize` jusqu'a page vide, **meme colonne
`site`** posee avant l'empilement des deux tenants, et **meme aplatissement**
des objets imbriques en colonnes pointees — `fields.Transporteur`,
`currentHost.name`, `category.name`. Ce que faisait
`Table.ExpandRecordColumn`, en une fois et sans lister les champs a la main.

Les tableaux (`profiles`, `certifications`) sortent en JSON compact dans leur
colonne : ils n'ont pas de forme de colonne stable, et les eclater
multiplierait les lignes.

CSV point-virgule, UTF-8 avec BOM : il s'ouvre dans Excel FR sans passer par
l'assistant d'import.

```
peripass_export_csv(
  path="/visitors",
  query_json='{"timestampFilterField": "Arrived", "timestampFilterStart": "2026-08-01T00:00:00Z"}',
  filename="visiteurs_aout_2026.csv"
)
```

**Le message d'export dit s'il a ete tronque, et sur quel site.** Un fichier
tronque qui passe pour complet, c'est un volume faux dans une revue
transporteur.

## Etat de la verification

Recette passee sur ce poste le 2026-08-28, sur `mcp 1.29+` / `httpx 0.28.1` /
Python 3.14.7 :

- **82 controles hors reseau** passes, sans cle et sans appel
  (`server/test_offline.py`) : liste blanche des chemins, refus des chemins
  d'ecriture et des deux chemins bloques, resolution du bon gabarit pour un
  chemin concret, garde-fous des filtres, pagination et multi-site sur donnees
  simulees, aplatissement, joker de prefixe, normalisation des reponses,
  masquage des secrets, refus d'ecrire dans un dossier synchronise, parseur de
  configuration (BOM, `#` dans une valeur, `export`), purete de stdout ;
- **handshake MCP stdio complet**, **20 outils** exposes avec leurs schemas et
  leurs vrais parametres. **Aucune ligne non-JSON sur stdout** : les journaux
  partent sur la sortie d'erreur. Un journal egare sur stdout casserait le
  protocole MCP en silence ;
- **chaine HTTP verifiee contre les deux hotes de production** : les racines
  historiques d'AUV et d'AMB repondent, la requete est bien emise avec ses
  parametres de pagination, la reponse d'erreur est interceptee et rendue
  lisible, la cle est masquee a l'affichage.

**Un ecart entre la documentation et la realite, trouve par cette recette.**
Le contrat Peripass annonce un `401 Unauthorized` quand l'en-tete
d'authentification manque. Mesure contre les deux tenants : **c'est un `403`
a corps vide**, et c'est le meme `403` pour une cle fausse. Un message d'erreur
qui aurait designe « un probleme de droits » aurait envoye chercher au mauvais
endroit ; le serveur enonce donc les trois causes possibles et renvoie vers
`peripass_doctor`, seul capable de dire si une cle a bien ete lue.

### Ce qui reste a verifier en usage reel

**Le connecteur n'a pas encore ete confronte a un tenant avec une cle valide.**
Ce qui reste ouvert, dans l'ordre d'importance :

1. **Les champs personnalises.** Le script Power Query listait onze champs
   (« Numero RDV », « Debut creneau horaire », « Numero de commande »,
   « Activite », « Numero tournee », « Transporteur », « FRAQ », « Envoi SMS »,
   « SMS decroche/accroche », « Decroche/Accroche »). `peripass_fields` dira
   lesquels existent reellement, sur quel site, et avec quel taux de
   remplissage. **A faire au premier appel** : c'est ce qui conditionne toute
   analyse par transporteur.
2. **Le comportement du quota.** Peripass annonce que la limitation « sera
   activee prochainement » et qu'elle compte les ressources lues. A mesurer sur
   un export reel avant de fonder une routine dessus.
3. **Le decalage horaire.** A verifier sur chaque tenant : s'il manque sur
   l'un des deux, une comparaison AUV / AMB a l'heure pres n'est pas fiable.
4. **La forme moderne de l'URL** (`restapiprd.peripass.app` + `tenant`) n'a pas
   ete essayee avec une vraie cle : le nom de tenant exact des deux sites reste
   a confirmer aupres de Peripass.

## Rotation des cles

**Les cles utilisees dans le script Power Query d'origine sont a considerer
comme compromises et a faire tourner cote Peripass.** Elles etaient ecrites en
clair dans le corps de la requete Power Query, et elles ont circule dans une
conversation. Aucune n'est reprise dans ce plugin, ni dans aucun fichier de ce
depot : le connecteur attend que chacun saisisse la sienne sur son poste.

Le geste, une fois les nouvelles cles en main :

```bash
powershell -ExecutionPolicy Bypass -File "08_ENGINE/03_plugins/plugin_vu_transport/plugins/peripass/server/install.ps1" -AuvApiKey "<nouvelle cle AUV>" -AmbApiKey "<nouvelle cle AMB>"
```

Et dans le meme mouvement, sortir les cles du script Power Query : dans Power
BI, elles ont leur place dans un **parametre** de type texte, pas dans le corps
de la requete.

**Si la cle est portee par la configuration d'equipe**, la rotation ne se fait
pas poste par poste : elle se fait une fois dans
`08_ENGINE/04_mcp/00_config/peripass.shared.env`, et prend effet sur tous les
postes au prochain demarrage de session. C'est le principal interet de la
couche d'equipe — et son principal risque : une valeur fausse casse les
vingt-six en meme temps. Trois reflexes avant d'y ecrire :

1. verifier qu'aucun fichier suffixe du nom d'une machine (`-VU-XXXXX`) ne
   traine dans le dossier — SharePoint ne fusionne pas les ecritures
   concurrentes, il **duplique** ;
2. mettre a jour `updated` / `updated_by` sur le `README` de `00_config/` ;
3. verifier avec `/peripass-setup` juste apres.

Un poste qui doit garder l'ancienne cle, ou une cle nominative, la saisit dans
`/plugin` : elle passe devant celle de l'equipe, sans toucher au fichier
partage.

> **Jalon Webfacto.** Ce serveur est un outil de poste, en lecture seule, sur
> des cles existantes : c'est du prototypage individuel, libre. **Le brancher a
> un flux du SI, l'exposer a plusieurs utilisateurs, ou en faire une
> automatisation qui tourne sans personne devant depasse ce cadre** : avant
> tout demarrage en developpement ou integration au SI, ce cas d'usage doit
> etre valide par la Webfacto (cadrage besoin, faisabilite, securite,
> priorisation).

## Tenir la liste des chemins a jour

`openapi_get_paths.json` est la liste blanche : les 18 chemins GET, leurs
resumes, leurs parametres et les enums du contrat, extraits le 2026-08-28. Il
est versionne a cote du code, et le serveur refuse de demarrer sans lui.

Quand Peripass fait evoluer son API, le fichier se regenere depuis
`https://restapiprd.peripass.app/documentation/v2.0/PeripassRestApi.yaml` — la
page de documentation est une application Swagger UI, le contrat est le YAML
qu'elle charge. Un chemin qui n'est pas dans ce fichier n'est pas atteignable :
c'est exactement l'effet voulu.

_Ariane, 2026-08-28._
