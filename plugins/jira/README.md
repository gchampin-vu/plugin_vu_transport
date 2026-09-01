---
updated: 2026-09-01
updated_by: Guillaume_Champin
---

# Connecteur JIRA — deux instances, en lecture seule

Interroge en langage naturel les **deux instances Atlassian** de Vente-unique :

| Tenant | Site | Ce qu'elle porte |
|---|---|---|
| **PROJET** | `vuproject.atlassian.net` | le métier — `SUPPLY` (les chantiers), `VUD` (les incidents d'expédition), et dix autres projets hors périmètre L&T |
| **WF** | `webfacto.atlassian.net` | le travail des développeurs internes |

Deux commandes dédiées, une par instance : **`/jira-projet`** et **`/jira-wf`**.
Deux exports : **`/jira-projet-export`** et **`/jira-wf-export`** (plus
`/jira-export`, générique, qui demande l'instance). Et **`/jira-setup`** pour la
mise en service.

**27 outils, tous en lecture.** Créer un ticket, commenter, changer un statut ou
affecter quelqu'un ne se font pas ici : un statut qui bouge dans JIRA engage
l'équipe, ça reste un geste humain dans l'interface.

## Ce qui fait la différence : le contexte est embarqué

Un connecteur JIRA générique sait exécuter du JQL. Il ne sait pas que `TIL` veut
dire *Tracking Information Log*, que `SUPPLY-3527` est le cadrage d'Atlas, que
`VUD7113` est l'agence de Marseille, ni que le titre d'un ticket `VUD` est une
**référence d'expédition** et pas une phrase. Il cherche donc le bon mot dans le
mauvais projet.

`server/contexte_jira.json` porte ce qui ne se devine pas, relevé le 2026-09-01
sur l'API de `vuproject` et dans `01_CONTEXTE/JIRA_ET_CHANTIERS.md` :

- les **12 projets** de vuproject, avec leur type et leur périmètre ;
- le **décodeur des titres VUD** — préfixe d'enseigne, date, séquence, numéro
  d'expédition, code de prestation — et les préfixes de titre de `SUPPLY` ;
- les **épics par chantier** : la généalogie d'Atlas (SCALIA Log → Atlas), TIL et
  son tiering transporteur, les lancements pays et leurs transporteurs, les
  études de rentabilité d'agence, la facturation, les intégrations SI ;
- les **codes d'agence VUD** ;
- les **recettes JQL** qui marchent, et les **pièges** qui rendent un chiffre
  faux.

C'est exposé par l'outil `jira_contexte`, qui **répond sans appeler JIRA**. Il
ne porte **aucun avancement**, volontairement : JIRA fait foi sur les statuts et
les dates.

## La traduction français → JQL, et pourquoi elle est visible

`jira_recherche` prend des filtres en français et **affiche le JQL construit** :

```
jira_recherche(tenant="PROJET", projet="SUPPLY", statut="ouvert",
               type_ticket="Epic", maj_depuis="cette semaine")
→ JQL : project = SUPPLY AND issuetype = "Epic" AND statusCategory != Done
        AND updated >= startOfWeek() ORDER BY updated DESC
  Traduit : statut « ouvert » traduit en statusCategory != Done ;
            updated « cette semaine » traduit en startOfWeek()
```

Afficher le JQL n'est pas un détail d'ergonomie : c'est ce qui rend la traduction
**vérifiable**, donc ce qui permet de voir quand elle a mal compris.

**Le serveur refuse ce qu'il ne comprend pas.** Une période qu'il ne sait pas
traduire, un nom de personne au lieu d'un accountId, une recherche sans aucun
filtre : refusés, avec la marche à suivre. La raison est simple — un filtre
ignoré ne rend pas moins de lignes, il rend **toute la base** avec l'air d'avoir
compris.

## Compter n'est pas lister

**L'API de recherche moderne de Jira Cloud ne rend aucun total.** Atlassian a
retiré l'ancien `/search` des instances Cloud en 2025 ; `/search/jql` pagine par
jeton et ne compte rien.

Conséquence assumée : un compte s'obtient en parcourant les pages. C'est le
travail de **`jira_summary`**, qui ne demande que les champs à grouper, agrège
côté serveur, et **dit toujours si le plafond a mordu**. Un compte issu d'une
lecture tronquée est un **plancher**, jamais un volume — et le rendu l'écrit en
majuscules, parce que c'est exactement le chiffre qui finit dans un mail.

Pour un historique long, `jira_sync` rapatrie une fois dans un cache SQLite local
et `jira_sql` l'interroge en SQL, instantanément et sans quota.

## L'authentification : trois modes, et une vérité à dire

| Mode | Ce qu'il faut | Renouvellement |
|---|---|---|
| **`basic`** (défaut) | courriel + jeton d'API — du compte de service de l'équipe, ou personnel | **manuel** — le jeton expire, on en recrée un |
| `bearer` | un jeton d'accès obtenu ailleurs | manuel |
| `oauth` | client_id + client_secret + refresh_token | **automatique**, par le serveur |

**Le mode `basic` est le plus simple : deux lignes par instance, rien à inscrire
chez Atlassian.** Le jeton se crée sur
`https://id.atlassian.com/manage-profile/security/api-tokens`.

**Sur la génération automatique de jetons, la réponse honnête est en deux
parties :**

1. **Un jeton d'API personnel ne peut PAS être créé par une API.** Atlassian
   n'expose aucun endpoint pour ça, volontairement : le jeton porte l'identité de
   la personne. Aucun connecteur ne contourne ce point, et une réponse qui le
   promettrait serait fausse.
2. **Un jeton d'ACCÈS OAuth, lui, se renouvelle par API** — c'est sa raison
   d'être. Le mode `oauth` du connecteur le fait : il appelle
   `auth.atlassian.com/oauth/token` avant expiration, et **suit la rotation du
   refresh token** en réécrivant la nouvelle valeur dans le magasin local (sans
   quoi ça marche une heure puis casse définitivement).

Le mode `oauth` suppose une **application inscrite dans la console développeur
Atlassian** sur le domaine de l'entreprise. Ce n'est pas un geste individuel :
**il passe par un cadrage Webfacto**. Tant que ce n'est pas fait, `basic` est le
bon choix.

C'est aussi la seule requête non-GET du serveur, et elle va au service
d'authentification, pas à l'API Jira : la règle de lecture seule reste entière.

## La configuration, en deux couches

Comme Yooz et Shiptify, et pour la même raison : un réglage ressaisi vingt-six
fois, ce sont vingt-six occasions de diverger et une correction qui ne se
propage jamais.

| Couche | Où | Ce qu'elle porte |
|---|---|---|
| **Équipe** | `08_ENGINE/04_mcp/00_config/jira.shared.env` | tenants, racines de site, cloudId, plafonds, **et le couple courriel + jeton du compte de service** |
| **Poste** | `~/.jira-mcp/jira.env`, ou `/plugin` | ce qui surcharge l'équipe, les secrets OAuth, les chemins locaux |

Priorité : **configuration du plugin > `jira.env` du poste > fichier d'équipe >
défaut du serveur**. Le poste passe **devant** l'équipe, et non l'inverse : un
réglage d'équipe est un point de départ commun, pas une contrainte.

**Les identifiants sont ceux d'un compte de service, décision du 2026-09-01.**
Ils vivent donc dans le fichier partagé : ils n'identifient personne, ils valent
pour l'équipe, et les faire ressaisir vingt-six fois n'ajouterait aucune
protection — seulement vingt-six mises en service qui échouent et une rotation
impossible à propager.

**Ce que ça implique, et qui est assumé plutôt que contourné :**

1. cette bibliothèque est lisible par toute l'équipe L&T, donc le jeton l'est
   aussi. C'est le prix du réglage partagé ;
2. un jeton Jira **autorise l'écriture**, même si ce connecteur n'émet que des
   GET. Le compte de service doit donc être provisionné avec **les droits les
   plus faibles qui répondent** — une lecture sur SUPPLY et VUD, pas un
   administrateur. C'est la seule vraie protection, et elle est côté
   provisionnement, pas côté code ;
3. ce qui serait fait avec ce jeton serait tracé sous **son** nom. Acceptable
   pour de la lecture, pas pour une action qui engage quelqu'un ;
4. le quota de débit de Jira Cloud est compté **par compte** : un compte de
   service partagé par vingt-six postes partage aussi son quota. C'est la
   contrepartie à surveiller, et `jira_doctor` affiche le quota restant ;
5. le jour où un accès doit être **nominatif** — audit, prestataire externe,
   traçabilité d'une action — la réponse est la configuration du plugin sur le
   poste, qui passe devant le fichier d'équipe. **On ne retire pas la ligne, on
   la surcharge chez soi.**

**Deux secrets restent refusés dans le fichier partagé**, et la raison est
mécanique et non politique : `JIRA_*_CLIENT_SECRET` et `JIRA_*_REFRESH_TOKEN` du
mode OAuth. Atlassian fait **tourner** le refresh token à chaque renouvellement
et invalide le précédent : deux postes qui le partagent se le cassent
mutuellement, et le second échoue définitivement — il faudrait refaire le
consentement à la main. Un refresh token n'a qu'un détenteur possible, donc le
mode OAuth se configure sur **un** poste.

Le serveur les ignore et le **signale** dans `/jira-setup`, avec cette raison.

## Les 27 outils

**Mise en service et diagnostic** — `jira_setup_status`, `jira_save_key`,
`jira_forget_key`, `jira_doctor`, `jira_tenants`.

**Contexte et référentiels** — `jira_contexte` (hors ligne), `jira_projects`,
`jira_fields`, `jira_statuses`, `jira_referentiel`, `jira_myself`,
`jira_user_lookup`, `jira_list_paths`.

**Interroger** — `jira_recherche` (filtres en français, l'outil principal),
`jira_search` (JQL brut), `jira_summary` (compter, répartir), `jira_issue`
(détail, description en texte lisible), `jira_comments`, `jira_changelog`
(depuis quand dans ce statut), `jira_children` (ce que porte un épic),
`jira_get` (échappatoire, liste blanche de chemins GET).

**Exporter et historiser** — `jira_export_csv`, `jira_sync`, `jira_tables`,
`jira_columns`, `jira_sql`, `jira_export_sql`.

## Installation

**Par le plugin, le chemin nominal :**

```
/plugin  →  marketplace vu-transport  →  jira  →  Install
/plugin  →  jira  →  Configure  →  courriel + jeton, par instance
```

puis **redémarrer la session** : le serveur ne relit sa configuration qu'au
démarrage. `bootstrap.py` crée l'environnement et installe `mcp` et `httpx` au
premier lancement, sur Mac comme sur Windows.

Ensuite **`/jira-setup`**, qui vérifie et guide pas à pas.

**En direct, sur un poste Windows** (confort, pas le chemin nominal) :

```powershell
.\server\install.ps1 -ProjetEmail 'p.nom@vente-unique.com' -ProjetToken '<jeton>'
```

## Vérifier

```bash
python server/test_offline.py
```

**114 contrôles hors ligne, aucun appel réseau.** Ils couvrent la traduction
français → JQL, **les refus** (une période incompréhensible, un nom au lieu d'un
accountId, une recherche sans filtre, un ordre qui n'est pas un tri), la liste
blanche des chemins GET, le rendu de l'ADF, le refus des identifiants dans le
fichier d'équipe, la lecture d'un `.env` avec BOM, et la signature des outils MCP
— le bug `functools.wraps` qui publie des outils inappelables sans erreur au
démarrage.

```bash
python server/server.py doctor
```

Diagnostic de connexion : d'où vient chaque jeton, quelle racine d'API est
appelée, et un vrai appel `GET /myself` plus une recherche, **par instance**.

## Ce qu'il reste à faire

- **Le référentiel de `webfacto` n'est pas relevé.** Personne n'avait d'accès API
  à cette instance au moment de la construction : le contexte embarqué n'en
  connaît que le site. `jira_projects(tenant="WF")` le découvre, et ce qui sera
  trouvé a vocation à monter dans `01_CONTEXTE/JIRA_ET_CHANTIERS.md` — une case à
  cocher y attend déjà.
- **Le connecteur n'a pas encore été confronté à un jeton valide.** Les 114
  contrôles hors ligne passent, le serveur démarre, publie ses 27 outils et rend
  ses erreurs proprement. Les premiers résultats réels sont à regarder comme une
  recette, pas comme une mesure établie.
- **Le mode `oauth` est écrit et non éprouvé** : il attend une application
  inscrite côté Atlassian, donc le jalon Webfacto.

## Le jalon Webfacto

Le prototypage et l'usage individuel de ce connecteur sont libres. **Deux choses
demandent un cadrage Webfacto préalable** (besoin, faisabilité, sécurité,
priorisation) : l'inscription d'une application OAuth sur le domaine Atlassian de
l'entreprise, et toute diffusion du connecteur à l'échelle de l'équipe qui
supposerait un compte de service ou un dépôt Git d'organisation.
