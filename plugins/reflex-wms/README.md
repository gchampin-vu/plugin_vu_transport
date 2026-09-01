---
updated: 2026-09-01
updated_by: Guillaume_Champin
---

# reflex-wms — interroger le WMS Reflex en lecture seule

Poser une question en français sur l'entrepôt, obtenir une réponse structurée,
et un export CSV quand on le demande.

Ce que ce connecteur apporte, et qui n'existait nulle part : **le MLD Hardis
des 1 985 tables Reflex est embarqué dedans**, sous forme d'un catalogue
compressé de 536 Ko — 48 534 colonnes avec leur libellé, leur type, leur
longueur et leur rang dans la clé. Les outils de schéma répondent **hors ligne,
en quelques millisecondes**, sans requête de découverte sur la production.

C'est ce qui permet de partir d'une question en français et d'arriver à une
requête T-SQL juste : sur Reflex, un nom de colonne ne se devine pas
(`PECDPO`, `PETSOL`, `RPTRPS`), il se lit.

## Installer

```bash
claude plugin install reflex-wms@vu-transport
```

Rien d'autre. Au premier démarrage, `bootstrap.py` crée un environnement Python
dans `%LOCALAPPDATA%\reflex-mcp\venv` et y installe `mcp` et `pyodbc`.

**Aucun logiciel ne s'installe sur la machine.** Le pilote ODBC utilisé est
`SQL Server`, livré avec Windows depuis toujours — c'est un choix, pas un
défaut : un pilote plus récent serait meilleur, mais il faudrait l'installer.
Si un `ODBC Driver 17/18` est présent sur le poste, le connecteur le prend
automatiquement.

Puis `/reflex-setup` pour vérifier, ou l'outil `reflex_doctor`.

## L'accès à la base — à régler avant de s'en servir

| | |
|---|---|
| Serveur | `172.17.151.114`, réseau interne — **bureau ou VPN obligatoire** |
| Base courante | `RFXCAFPRDDAT` |
| Base d'épuration | `RFXCAFPRDEPU` — même schéma, l'historique sorti de la courante |
| Ordre d'interrogation | **épuration d'abord, courante ensuite** — voir plus bas |
| Schéma | `reflex` (minuscules), jamais `dbo` |
| Dialecte | T-SQL, instance **≤ 2016** |

**L'instance refuse l'authentification Windows des comptes du domaine CAFOM**
(erreur 18456, constatée le 28/08/2026). L'accès passe donc par un compte SQL,
et il n'y en a qu'un pour l'équipe : le compte de service `query`, en lecture.

**Il est dans la configuration d'équipe, il n'y a rien à saisir.** Un poste qui
synchronise la bibliothèque récupère serveur, bases, garde-fous et identifiant
au premier démarrage. Le mode `sql` y est déjà posé.

Deux cas où il faut intervenir :

- **la bibliothèque n'est pas synchronisée sur ce poste** — pointer le fichier
  avec `REFLEX_SHARED_ENV`, ou saisir compte et mot de passe dans
  `/plugin > reflex-wms` ;
- **ce poste doit utiliser un accès différent** (compte nominatif, recette) —
  le saisir dans `/plugin`, qui passe **devant** la configuration d'équipe.

En attendant l'accès, **tout ce qui est hors ligne fonctionne** :
`reflex_guide`, `reflex_tables`, `reflex_columns`, `reflex_find_column`,
`reflex_prefix`, `reflex_recipes`. Une requête peut être préparée et relue sans
accès.

> Le jour où l'IT ouvrira la lecture aux comptes Windows, il suffira de
> repasser `REFLEX_AUTH=trusted` dans le fichier d'équipe : un accès nominatif
> se trace et se révoque, un compte de service partagé non. Rien d'autre à
> changer dans le connecteur.


## Les onze outils

### Le mémo, à lire en premier

| Outil | Ce qu'il fait |
|---|---|
| `reflex_guide(sujet)` | Conventions, pièges, carte des tables. Sujets : `domaines`, `preparations`, `receptions`, `expeditions`, `stock`, `sql`, `ecarts` |

Il porte les deux pièges qui produisent des résultats faux **sans lever
d'erreur** : les dates éclatées en cinq colonnes, et les tops qui valent
`'1'`/`'0'` chez Vente-unique alors que la doc Hardis annonce `'O'`/`'N'`.

### Le schéma — hors ligne, instantané

| Outil | Ce qu'il fait |
|---|---|
| `reflex_tables(recherche, prefixe)` | Cherche une table par nom, entité logique ou description française. Accents ignorés |
| `reflex_columns(table, recherche)` | Les colonnes exactes, avec libellé, type, longueur, rang dans la clé |
| `reflex_find_column(terme)` | Dans quelle(s) table(s) vit une colonne |
| `reflex_prefix(prefixe)` | De quelle table sort une colonne, d'après ses deux premières lettres |

### Les requêtes

| Outil | Ce qu'il fait |
|---|---|
| `reflex_query(sql, max_rows, base)` | Exécute une lecture T-SQL bornée |
| `reflex_peek(table, colonnes, where, depot)` | Un coup d'œil sur une table, sans écrire de SQL |
| `reflex_recipes(nom)` | Sept requêtes déjà validées, à lire puis adapter |
| `reflex_export_csv(sql, nom_fichier)` | Export CSV, **sur demande explicite uniquement** |

### La mise en service

| Outil | Ce qu'il fait |
|---|---|
| `reflex_setup_status()` | D'où vient chaque réglage, quel pilote, quel catalogue. Ne se connecte pas |
| `reflex_doctor()` | Diagnostic complet, connexion comprise |

## Lecture seule — quatre verrous, pas une convention

1. **Aucun outil n'écrit.** Il n'y a ni `INSERT`, ni `UPDATE`, ni procédure
   stockée.
2. **L'analyseur refuse** tout ce qui n'est pas un `SELECT` ou une CTE.
   Commentaires et littéraux sont retirés avant analyse, pour qu'un mot-clé
   caché dans une chaîne ne serve pas de passe-droit. `INTO` est refusé
   (`SELECT … INTO` crée une table), les procédures `sp_`/`xp_` aussi, et le
   point-virgule qui sépare deux instructions également.
3. **La session tourne en `READ UNCOMMITTED`** : aucun verrou n'est posé sur la
   production, même si la requête oublie `WITH (NOLOCK)`.
4. **Le compte SQL est en lecture.** C'est le dernier filet, et le seul que le
   connecteur ne contrôle pas lui-même : même si les trois premiers verrous
   tombaient, la base refuserait l'écriture.

Débloquer un stock, solder une préparation, corriger un emplacement passe par
Reflex, par l'entrepôt, ou par la Webfacto — jamais par un agent.

Une conséquence du compte de service partagé, à connaître : **côté base, toutes
les requêtes de l'équipe portent le même compte.** Ce qui les distingue, c'est
`APP=reflex-mcp` dans la chaîne de connexion — un DBA qui voit une requête
longue sait qu'elle vient d'ici, mais pas de qui. Le jour où la traçabilité
nominative devient un besoin, c'est l'argument pour demander l'ouverture des
comptes Windows.

## Les garde-fous — pour ne pas bloquer la production

Une base de WMS se lit à plusieurs dizaines de millions de lignes par table.
Une requête mal bornée n'y rend pas un mauvais résultat : elle occupe le
serveur, fait attendre l'entrepôt, et le poste reste bloqué dessus.

| Garde-fou | Défaut | Ce qu'il fait |
|---|---|---|
| **Gouverneur de coût** | 5000 | SQL Server **estime** le coût du plan et **refuse de démarrer** au-delà. Le seul qui agit avant que la première ligne ne soit lue |
| **Délai d'exécution** | 60 s | Posé sur le pilote ODBC, plafonné à 600 s en dur |
| **Chien de garde** | +5 s | Annule le curseur si le pilote n'honore pas le délai. Le pilote livré avec Windows date de 2000, on ne lui fait pas une confiance aveugle |
| **Une requête à la fois** | — | Un agent qui enchaîne cinq questions ne lance pas cinq balayages simultanés. Refus immédiat et explicite, pas d'attente muette |
| **`WITH (NOLOCK)` exigé** | oui | Une requête qui l'oublie est **refusée**, la table fautive est nommée |
| **Jointure sans `ON`** | refusée | Y compris la jointure implicite par virgule dans le `FROM` |
| **Plafond de lignes** | 200 / 100 000 | Un résultat tronqué est signalé comme tel |

Pourquoi `WITH (NOLOCK)` est **refusé** et pas seulement signalé, alors que la
session est déjà en `READ UNCOMMITTED` : une requête écrite ici finit toujours
recopiée ailleurs — dans SSMS, dans une source Power BI, dans un script
d'équipe — où ce réglage de session n'existe pas. Elle y posera des verrous sur
la production. On refuse donc à la source.

Un garde-fou qui se déclenche reste du temps perdu. Le mémo dit comment écrire
la requête bornée du premier coup : filtrer sur le dépôt d'abord, borner la
période sur les colonnes de date **brutes** (jamais sur
`RFX_DHB_DATE2DATETIME(…)`, qui interdit l'usage des index), joindre sur la clé
complète, compter avant de lister.

## L'ordre des deux bases : épuration d'abord

Par défaut (`base='auto'`), une lecture interroge **`RFXCAFPRDEPU` puis
`RFXCAFPRDDAT`**, et s'arrête à la première qui rend des lignes.

C'est contre-intuitif — on s'attendrait à interroger d'abord la base vivante —
et c'est le bon ordre pour ce qu'on demande à Reflex : les questions portent
presque toujours sur quelque chose qui **a déjà eu lieu** (une préparation
partie, un conteneur reçu, un chargement de la semaine dernière). Reflex épure
en continu, et le moment où un dossier bascule n'est pas connu de celui qui
pose la question. Chercher d'abord dans l'épuration trouve le cas ancien du
premier coup, et ne paie la seconde lecture que pour le cas récent.

**La base qui a répondu est toujours indiquée**, sur la lecture comme sur
l'export. Un chiffre dont on ignore de quelle base il sort n'est pas citable :
les deux ne couvrent pas la même période.

Trois choses à savoir :

- **un référentiel se lit sur `base='prod'`, toujours.** L'épuration en porte
  une photo ancienne : mesuré le 01/09/2026 sur `HLDEPPP`, elle rend `001`,
  `AMB` et `MOR` (Moreuil, site fermé) et **pas `AUV`**, là où la courante rend
  `AMB` et `AUV`. Le référentiel répond, donc la cascade s'y arrête, et la
  liste est fausse sans qu'aucune erreur ne soit levée. Le connecteur le
  signale dès qu'une réponse vient de l'épuration. La cascade est faite pour
  l'**historique d'un dossier**, pas pour une table de codes ;
- quand l'épuration ne rend rien, **la requête est exécutée deux fois**.
  Négligeable sur une lecture bornée ; sur une extraction lourde dont on sait
  qu'elle porte sur du récent, poser `base='prod'` évite le premier passage ;
- **la cascade se déclenche sur zéro ligne.** Un `COUNT(*)` sans `GROUP BY`
  rend toujours une ligne, même quand il ne compte rien : il ne bascule donc
  jamais et s'arrête sur le compte de l'épuration. Pour compter sur les deux,
  poser la question sur chaque base explicitement.

Quand aucune des deux ne rend rien, le connecteur le dit — le vide est constaté
des deux côtés, pas seulement sur la courante.

## Conformité MCP

| Convention | Où |
|---|---|
| Manifeste `.claude-plugin/plugin.json`, serveur déclaré par `"mcpServers": "./.mcp.json"` | référence explicite plutôt que découverte implicite |
| `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}` | chemin du serveur, venv et exports |
| `${user_config.KEY}` → variables d'environnement | 11 champs `userConfig`, `sensitive: true` sur le mot de passe |
| **Annotations de comportement** sur les 11 outils | `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint`, `openWorldHint` |
| `title` lisible par outil | affiché par le client à la place du nom technique |
| `instructions` de serveur | dit par quoi commencer, avant le premier appel |
| `skills/<nom>/SKILL.md`, `commands/*.md` | disposition standard |

`openWorldHint` distingue les deux familles : **faux** pour ce qui lit le
catalogue embarqué (ensemble fermé, réponse reproductible), **vrai** pour ce
qui interroge la base, dont le contenu bouge à chaque mouvement d'entrepôt.

La spécification demande aux clients de considérer les annotations comme des
**indications non fiables**. Ce qui garantit la lecture seule reste le code :
aucun outil d'écriture, l'analyseur, et la session en `READ UNCOMMITTED`. Les
annotations disent au client ce que le code tient déjà.

Les tools rendent du **texte**, pas de `structuredContent` : le rendu CSV est
calibré pour être lu dans la conversation, et un `outputSchema` n'apporterait
rien sur un résultat dont les colonnes changent à chaque requête.

## La configuration

Priorité, du plus fort au plus faible :

1. la configuration du plugin sur le poste (`/plugin`) ;
2. le fichier local hors du vault (`%LOCALAPPDATA%\reflex-mcp\reflex.env`) ;
3. `08_ENGINE/04_mcp/00_config/reflex.shared.env` — la décision d'équipe ;
4. la valeur par défaut du serveur.

**Le fichier d'équipe porte l'identifiant de service**, depuis le 01/09/2026 et
sur décision explicite. Deux raisons, et la première ne laisse pas le choix :
l'authentification Windows est refusée par l'instance, donc un compte SQL est
obligatoire ; et il n'y en a qu'un, en lecture, que toute l'équipe a déjà. Le
faire ressaisir vingt-six fois n'ajouterait aucune protection — seulement
vingt-six mises en service qui échouent et une rotation impossible à propager.
Même raisonnement que la clé de service dans `shiptify.shared.env`.

Contrepartie assumée : la bibliothèque est lisible par les 26 espaces de
`07_EQUIPE`, donc le mot de passe l'est aussi. En échange, le remplacer se fait
**une fois** et se propage à tous au prochain démarrage de session.

**La valeur du mot de passe ne s'affiche jamais** — ni dans `/reflex-setup`, ni
dans `reflex_doctor`, ni dans la chaîne de connexion rendue au diagnostic, qui
montre `PWD=********`. Seul le *nom* de la clé est cité, pour qu'on sache d'où
vient la valeur active. Un test le vérifie à chaque exécution de la suite.

Ce que le fichier d'équipe refuse toujours : `REFLEX_EXPORT_DIR`. Un chemin
valable sur un poste n'existe pas sur les vingt-cinq autres, et le serveur le
signale plutôt que de l'ignorer.

## Un écart connu, non arbitré

En construisant le catalogue, **trois définitions de la compétence d'équipe
`reflex-mld` se sont révélées contredites par le MLD Hardis**. Elles changent
le résultat sans lever d'erreur — c'est ce qui les rend coûteuses.

| Colonne | Ce que dit la compétence | Ce que dit le MLD |
|---|---|---|
| `PESLEF` | date de « lancement effectif » | **Date livraison effectuée** — siècle. Même famille : `PEULEF` « Code utilisateur livraison effectuée », `PETLEF` « Top livraison effectuée » |
| `P1TSLO` / `P1TSOR` | tops soldage / sortie de stock | **Top substitution sur ligne odp** / **Top service obligatoire sur référence réservation**. Le top de ligne qui existe est `P1TVLP` |
| `PENCOM` | numéro de commande | **Numéro de commentaire** |

Conséquence sur le filtre canonique de la compétence,
`PESLEF > 0 AND PESSOL = 0 AND PESSOR = 0` : il se lit « livraison effectuée ET
ni soldée ni sortie de stock » — un état contradictoire, pas une préparation en
cours.

Les recettes de ce connecteur sont écrites sur le MLD, et chacune porte l'écart
en tête. `reflex_guide('ecarts')` le documente. **L'arbitrage appartient à
Jimmy Mieuzet**, porteur de la compétence. Si un chiffre issu d'ici ne recoupe
pas un chiffre Power BI existant, c'est la première chose à regarder.

## Régénérer le catalogue MLD

Le catalogue embarqué vient du MLD HTML livré par Hardis (v9.14, mise à jour
éditeur du 18/11/2019). Le jour où une version supérieure est livrée :

```bash
python server/tools/build_catalog.py "<racine du MLD Hardis>"
```

Le script gère un piège : les fichiers annoncent tous `charset=iso-8859-1` dans
leur `<meta>`, et **l'annonce est fausse pour une partie d'entre eux**, qui sont
en UTF-8. Décoder tout le corpus en latin-1 corrompt les libellés.

`reflex_setup_status` affiche la version du MLD et la date de construction du
catalogue.

## Jalon Webfacto

Le prototypage et l'usage individuel sont libres. **Brancher ce connecteur à
une automatisation, à un flux, à un outil partagé, ou en faire une source de
chiffres diffusés** demande un cadrage Webfacto préalable — besoin,
faisabilité, sécurité, priorisation. Il lit la base de production du WMS.
