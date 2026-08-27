---
updated: 2026-08-27
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

Les autres connecteurs de `10_ENGINE/` (`mcp_powerbi`, `mcp_yooz`, `mcp_teams`)
ne sont **pas** encore empaquetes. Le catalogue est fait pour les accueillir :
un dossier sous `plugins/`, une entree dans `marketplace.json`.

## Pourquoi un plugin plutot qu'un `claude mcp add`

`claude mcp add` marche tres bien pour soi. Il ne se partage pas : chacun doit
recopier un chemin absolu, installer les dependances a la main, et personne ne
recoit les corrections. Le plugin repond aux trois :

- **une commande pour installer**, pas un mode operatoire ;
- **les dependances s'installent au premier demarrage** (voir l'amorce plus
  bas) ;
- **chacun saisit sa propre cle** dans l'interface du plugin. Aucune cle ne
  circule, ni dans le catalogue, ni dans un fichier partage, ni dans un message.

## Installer, cote collegue

Deux commandes dans Claude Code. La source depend de la ou le catalogue est
publie.

**Depuis le depot Git de l'equipe** (la cible, voir « Publier » plus bas) :

```bash
/plugin marketplace add <organisation>/<depot>
```

**Depuis la bibliotheque SharePoint synchronisee**, en attendant le depot :

```bash
/plugin marketplace add "C:\Users\<toi>\CAFOM\Transport BtoC - Documents\Projets Claude\08_Guillaume_Champin\myrddin\10_ENGINE\plugin_vu_transport"
```

Puis, dans les deux cas :

```bash
/plugin install shiptify@vu-transport
```

Claude Code demande alors la **cle d'API Shiptify**. Elle reste sur le poste du
collegue : elle n'est ni versionnee, ni partagee, ni ecrite dans le drive
d'equipe.

Verifier que tout repond, dans une session :

> lance shiptify_doctor

Il dit d'ou vient la cle, si l'API repond, et ce que le serveur a lu comme
configuration. C'est le premier outil a appeler quand quelque chose coince.

## Ce qu'un collegue doit obtenir de Shiptify

Le plugin ne donne aucun acces : il utilise **la cle du collegue**. Sans cle
Shiptify, le plugin s'installe et ne repond rien d'utile. La demande de cle se
fait aupres de Shiptify, par le canal habituel.

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
│   └── shiptify/
│       ├── .claude-plugin/
│       │   └── plugin.json       manifeste : metadonnees + userConfig (la cle)
│       ├── .mcp.json             declaration du serveur MCP
│       ├── server/               le code du connecteur
│       │   ├── bootstrap.py      amorce : prepare l'environnement Python
│       │   ├── server.py         le serveur MCP, 17 outils, lecture seule
│       │   ├── openapi_get_paths.json   liste blanche des 91 chemins GET
│       │   ├── requirements.txt
│       │   ├── install.ps1       installation directe, hors plugin
│       │   └── .env.example
│       ├── skills/shiptify/SKILL.md   quand et comment interroger Shiptify
│       └── README.md             la doc du connecteur
├── .gitignore
└── README.md                     ce fichier
```

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
