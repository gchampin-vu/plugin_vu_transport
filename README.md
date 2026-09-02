---
updated: 2026-09-02
updated_by: Guillaume_Champin
type: process
---

# Marketplace `vu-transport`

Le catalogue de plugins Claude Code de l'equipe Logistique & Transport. Un
collegue ajoute le catalogue une fois, installe ce dont il a besoin, et se met a
jour tout seul ensuite.

| Plugin | Ce qu'il apporte | Etat |
| --- | --- | --- |
| `shiptify` | Serveur MCP en lecture seule sur la base Shiptify (17 outils) + une skill qui sait s'en servir | recette passee contre la production le 2026-08-27 |
| `yooz-factures` | Serveur MCP en lecture seule sur la base de factures Yooz (20 outils) + une skill. **Recherche filtree en direct sur la grille du portail**, historique dans un cache local interrogeable en SQL, et requetes directes pour le reste | teste hors reseau le 2026-08-28 (56 controles, 3 echecs connus d'isolation du test). **Chemin direct confronte au vrai Yooz le 2026-08-28** : rapproche du cache sur 3 303 documents, aucun ecart de montant |
| `peripass` | Serveur MCP en lecture seule sur le yard management Peripass (20 outils) + une skill. **Multi-tenant** : une cle par site, AUV et AMB interroges ensemble, une colonne `site` sur chaque resultat | teste hors reseau le 2026-08-28 (82 controles), handshake MCP et chaine HTTP verifies contre les deux hotes de production. **Pas encore confronte a un tenant avec une cle valide** : les cles du script Power Query sont a faire tourner d'abord |
| `gisco` | Serveur MCP en lecture seule sur le referentiel geographique europeen d'Eurostat (20 outils) + une skill. Pays, NUTS, 98 000 communes, 830 000 codes postaux avec commune, NUTS3 et degre d'urbanisation. Rattachement d'un point a sa commune **hors ligne**, sans GDAL ni geopandas. Versant britannique charge depuis l'ONS dans les memes tables. Millesimes **decouverts chez Eurostat**, pas ecrits en dur, et couches du cache en retard signalees. **Aucune cle** : service public ouvert | monte le 2026-09-01. 98 controles hors ligne verts au 2026-09-02 - realignes sur la decouverte des millesimes, contre laquelle trois d'entre eux ne compilaient plus. Handshake MCP re-verifie : 20 outils annotes `readOnlyHint` et instructions de serveur servies au client. **Un defaut corrige** : la reprise de schema n'existait que sur le chemin d'ecriture, donc `gisco_couches` echouait sur un cache monte par une version precedente. 5 couches GISCO et 2 couches ONS rapatriees et interrogees contre la production. **`uk_onspd` valide sur 4 000 codes postaux, pas sur les 1,8 million.** Portee macOS non verifiee |
| `trustpilot` | Serveur MCP en lecture seule sur les avis Trustpilot des 18 domaines du groupe (22 outils) + une skill. Avis, notes, TrustScore, reponses. **Interprete les commentaires** : themes dans les 11 langues, transporteurs cites desambiguises par pays, verbatims selectionnes. Le `referenceId` est notre numero de commande - la jointure vers Reflex et Shiptify, ouverte par le chemin **prive** (cle + secret). Cache local SQLite pour l'historique. **Aucune ecriture** : ni reponse a un avis, ni tag, ni invitation | monte le 2026-09-02. 188 controles hors ligne verts, dont la purete du flux stdio et la non-divulgation des secrets. **Aucune recette contre le vrai compte Trustpilot** : la liste blanche des chemins est ecrite a la main, faute de contrat OpenAPI publie, et n'est pas confirmee. `trustpilot.shared.env` pas encore depose |
| `jira` | Serveur MCP sur les **deux** instances Atlassian du groupe (33 outils) + une skill et six commandes. `vuproject` (le metier : SUPPLY, VUD) et `webfacto` (les developpements), deux referentiels sans rien en commun, une colonne `tenant` sur chaque resultat. Le **contexte JIRA est embarque** - 12 projets, decodeur des titres VUD, epics par chantier, recettes JQL - donc une question en francais devient un JQL juste sans appel de decouverte, et le JQL construit est affiche. Agregation cote serveur pour compter, l'API Cloud ne rendant aucun total. **Ecriture bornee a quatre gestes** depuis le 2026-09-02 : creer, mettre a jour, commenter, franchir une transition - liste blanche ou la cle porte le verbe, aucun DELETE expose, rien ne part sans `confirmer=True` apres affichage du corps exact et du compte qui signera | 188 controles hors ligne verts au 2026-09-02, dont le refus de `DELETE` sur le meme chemin qu'un `PUT` autorise et un mouchard qui echoue si une ecriture part sans confirmation. **Rien n'a encore ete confronte a un jeton valide** : ni la lecture, ni a plus forte raison l'ecriture, dont la premiere doit se faire sur un ticket d'essai. Le referentiel de `webfacto` reste a relever. **L'ouverture de l'ecriture n'est pas arbitree en equipe** - le `CLAUDE.md` collectif annonce encore des connecteurs en lecture seule par construction |

> **Ce tableau est encore en retard sur le catalogue.** `marketplace.json` porte
> aussi `reflex-wms`, `taux-tva`, `taux-de-change` et `indices-eu`, qui n'ont pas
> de ligne ici. A reprendre par leurs porteurs — on ne decrit pas l'etat de
> recette du connecteur d'un autre.

## Installer, cote collegue

Deux commandes dans Claude Code. La source depend de la ou le catalogue est
publie.

**Depuis le depot Git de l'equipe** (la cible, voir « Publier » plus bas) :

```bash
/plugin marketplace add <organisation>/<depot>
```

**Depuis la bibliotheque SharePoint synchronisee**, en attendant le depot :

```bash
/plugin marketplace add "C:\Users\<toi>\CAFOM\Transport BtoC - Documents\08_ENGINE\03_plugins\plugin_vu_transport"
```

Puis, dans les deux cas :

```bash
/plugin install shiptify@vu-transport
/plugin install yooz-factures@vu-transport
/plugin install peripass@vu-transport
/plugin install gisco@vu-transport
```

### La saisie peut attendre

Aucun identifiant n'est **obligatoire** a l'installation : on peut installer
d'abord et configurer a la premiere utilisation. Dans une session, une commande
par connecteur guide de bout en bout :

```bash
/shiptify-setup
```

```bash
/peripass-setup
```

Elles regardent ou on en est, disent exactement ou saisir la cle si elle
manque, la rangent sur la machine, testent la connexion et font un appel de
demonstration.

`gisco` fait exception : il n'a **aucun identifiant**, GISCO etant un service
public ouvert. Sa commande de mise en service, `/gisco-cache`, ne demande pas de
cle : elle rapatrie les couches geographiques, une fois, en annoncant leur poids
avant de telecharger quoi que ce soit.

Cote Peripass, cette commande a un role de plus : **une seule cle saisie donne
un connecteur qui repond mais ne couvre que la moitie du perimetre.** Elle le
verifie, et elle montre du meme geste si les deux sites portent les memes
champs personnalises.

Pour saisir ou corriger un identifiant a la main, a tout moment : `/plugin` >
le plugin > configuration. Le serveur reprend la valeur au demarrage suivant de
la session.

**La saisie se fait toujours dans l'interface de Claude Code, jamais dans la
conversation.** C'est ce que le champ `sensitive` garantit : la valeur n'entre
ni dans le contexte du modele, ni dans la transcription. Aucun outil des trois
plugins n'accepte un secret en parametre - `shiptify_save_key` et
`peripass_save_key` ne prennent aucun argument, ils rangent la cle deja saisie.

Verifier que tout repond, dans une session :

> lance shiptify_doctor

> lance yooz_status

> lance peripass_doctor

Ils disent d'ou vient la configuration, si l'API repond, et ce que le serveur a
lu. C'est le premier outil a appeler quand quelque chose coince. Cote Yooz, il
faut ensuite un premier rapatriement, une fois :

> fais un yooz_sync complet sur les deux societes


## Publier, cote equipe

Le contenu de ce dossier **est** le depot : `.claude-plugin/marketplace.json` a
la racine, les plugins sous `plugins/`. Pour le publier :

```bash
git init
git add .
git commit -m "Marketplace vu-transport : plugin shiptify"
git remote add origin <url-du-depot-prive>
git push -u origin main
```

Ensuite chaque collegue fait `/plugin marketplace add <organisation>/<depot>`, et
`/plugin marketplace update vu-transport` recupere les corrections.

**Ce dossier vit dans une bibliotheque SharePoint synchronisee.** L'ajout local
fonctionne pour depanner, mais ce n'est pas la cible : SharePoint duplique les
fichiers en cas d'ecriture concurrente (c'est un point ouvert connu du
`CLAUDE.md` d'equipe), et un `.git` dans un dossier synchronise se corrompt.
**Le depot Git est la forme de partage, pas le drive.**

Une seule chose ne doit jamais partir dans le depot : un fichier `.env`. Le
`.gitignore` de ce dossier l'exclut, et le serveur **refuse** de lire un `.env`
situe dans un dossier synchronise.

> **Jalon Webfacto.** Construire ce plugin et l'essayer entre nous est du
> prototypage, libre. **Le deployer a l'echelle de l'equipe ne l'est pas** : un
> depot GitHub d'organisation, un connecteur GitHub pour chaque utilisateur ou
> une distribution par la console d'administration Claude touchent au SI et aux
> acces. Avant tout demarrage en developpement ou integration au SI, ce cas
> d'usage doit etre valide par la Webfacto (cadrage besoin, faisabilite,
> securite, priorisation).
>
> C'est exactement le `[À TRANCHER]` « plugin d'equipe » du `CLAUDE.md`
> collectif : ce dossier en est la premiere brique concrete, pas la decision.

## Comment c'est fabrique

```
plugin_vu_transport/
├── .claude-plugin/
│   └── marketplace.json          le catalogue : nom, proprietaire, liste des plugins
├── plugins/
│   ├── shiptify/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json       manifeste : metadonnees + userConfig (la cle)
│   │   ├── .mcp.json             declaration du serveur MCP
│   │   ├── server/               le code du connecteur
│   │   │   ├── bootstrap.py      amorce : prepare l'environnement Python
│   │   │   ├── server.py         le serveur MCP, 17 outils, lecture seule
│   │   │   ├── openapi_get_paths.json   liste blanche des 91 chemins GET
│   │   │   ├── requirements.txt
│   │   │   ├── install.ps1       installation directe, hors plugin
│   │   │   └── .env.example
│   │   ├── skills/shiptify/SKILL.md   quand et comment interroger Shiptify
│   │   └── README.md             la doc du connecteur
│   ├── yooz-factures/
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json       manifeste + userConfig (4 secrets par societe)
│   │   ├── .mcp.json             declaration du serveur MCP
│   │   ├── server/               le code du connecteur
│   │   │   ├── bootstrap.py      amorce : meme structure que celle de shiptify
│   │   │   ├── server.py         le serveur MCP, 17 outils, lecture seule
│   │   │   ├── test_offline.py   31 controles, sans reseau
│   │   │   ├── requirements.txt
│   │   │   ├── install.ps1       installation directe, hors plugin
│   │   │   └── .env.example
│   │   ├── skills/factures-yooz/SKILL.md   quand et comment interroger Yooz
│   │   └── README.md             la doc du connecteur
│   └── peripass/
│       ├── .claude-plugin/
│       │   └── plugin.json       manifeste + userConfig (une cle PAR SITE)
│       ├── .mcp.json             declaration du serveur MCP
│       ├── commands/peripass-setup.md      la mise en service guidee
│       ├── server/               le code du connecteur
│       │   ├── bootstrap.py      amorce : meme structure que celle de shiptify
│       │   ├── server.py         le serveur MCP, 20 outils, lecture seule
│       │   ├── openapi_get_paths.json   liste blanche des 18 chemins GET
│       │   ├── test_offline.py   82 controles, sans reseau ni cle
│       │   ├── requirements.txt
│       │   ├── install.ps1       installation directe, hors plugin
│       │   └── peripass.env.example
│       ├── skills/peripass/SKILL.md   quand et comment interroger Peripass
│       └── README.md             la doc du connecteur
├── .gitignore
└── README.md                     ce fichier
```

### Ce que le troisieme connecteur a apporte au format

`peripass` est le premier connecteur **multi-tenant** du catalogue : Peripass
n'a pas une base mais une par site. Trois choses ont ete ajoutees au patron
commun, et elles resserviront :

- **un parametre `site` sur chaque outil**, une colonne `site` sur chaque
  resultat, et un `max_rows` qui s'applique **par site**. Un rendu ou un site
  n'a pas repondu se declare **PARTIEL** ;
- **un joker de prefixe dans la projection de colonnes** (`fields.*`). Les
  champs personnalises d'un tenant ne se nomment pas en dur : c'est ce qui
  cassait le script Power Query d'origine des qu'un champ etait renomme ;
- **la validation des enums avant l'appel.** Peripass ignore **en silence** un
  filtre mal forme et rend alors toutes les lignes. Un connecteur qui se
  contente de relayer produirait un chiffre faux d'apparence normale.

### L'amorce, et pourquoi elle existe

Un plugin s'installe en une commande : il n'ouvre pas un terminal pour faire un
`pip install`. `server/bootstrap.py` s'en charge au premier demarrage : il
cherche un environnement Python utilisable, le cree si besoin, installe `mcp` et
`httpx`, puis passe la main au serveur. Les fois suivantes, il ne fait que
passer la main.

Trois details qui ont demande une correction, et qui valent pour tout plugin
Python de l'equipe :

- **Rien sur la sortie standard.** Le serveur MCP parle JSON-RPC sur stdout :
  une seule ligne de `pip` egaree casse le protocole, en silence. Toute sortie
  de `venv` et de `pip` part sur stderr.
- **`subprocess`, pas `os.execv`.** Sous Windows, `execv` ne met pas les
  arguments entre guillemets. Le chemin du plugin passe par
  `Transport BtoC - Documents` : `execv` coupait le chemin au premier espace.
- **L'environnement existant est reutilise.** Sur le poste ou le connecteur
  avait deja ete installe a la main, l'amorce reprend
  `%LOCALAPPDATA%\shiptify-mcp\venv` au lieu de reinstaller a cote.

### Un piege Windows a connaitre

Si `python` resout vers un interpreteur **empaquete** (Microsoft Store, ou
Python Manager), ses processus enfants heritent d'une **vue virtualisee de
`%LOCALAPPDATA%`**. Mesure sur ce poste : le meme interpreteur de venv voit
`['.env', 'venv']` lance directement, et `['venv']` seulement lance par le
Python du Store - `LOCALAPPDATA` valant pourtant la meme chaine.

Concretement : **un `.env` pose sous `%LOCALAPPDATA%` peut etre invisible pour
le serveur lance par le plugin**, sans aucune erreur. Trois consequences, toutes
prises en compte :

1. En mode plugin, la cle passe par la **configuration du plugin**, pas par un
   fichier. Ce chemin est immunise.
2. Si un fichier est prefere, l'emplacement fiable est `%CLAUDE_PLUGIN_DATA%`
   (sous `~/.claude/`), essaye en premier. `SHIPTIFY_ENV_FILE` permet aussi de
   pointer un chemin explicite.
3. `shiptify_doctor` **detecte l'interpreteur empaquete** et le dit, au lieu de
   rapporter « absent » et d'envoyer chercher un fichier qui est bien la.

**Le cas a ete remesure le 2026-08-27 en empaquetant Yooz, et il va plus loin que
le fichier de configuration.** Dans `%LOCALAPPDATA%\yooz-mcp`, PowerShell et
l'interpreteur du venv lance directement voient `['venv', 'yooz.env']` ; le meme
interpreteur de venv, lance par l'alias du Store, ne voit que `['venv']`. Un
**cache** pose sous `%LOCALAPPDATA%` serait donc different selon qui lance le
serveur - le plugin ecrirait d'un cote, une synchro en terminal de l'autre, sans
erreur. Deux consequences pour tout plugin Python de l'equipe :

- **La racine locale d'un connecteur ne doit pas etre `%LOCALAPPDATA%`.** Yooz
  utilise `~/.yooz-mcp` : le profil utilisateur n'est pas virtualise.
- **La detection par `sys.base_prefix` ne suffit pas**, et l'API Windows
  `GetCurrentPackageFullName` ne repond pas non plus dans le petit-fils du
  processus empaquete. `yooz_status` signale donc le **symptome** (« le dossier
  existe mais aucun fichier n'y est visible ») plutot que de pretendre nommer la
  cause.
