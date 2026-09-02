---
updated: 2026-09-02
updated_by: Guillaume_Champin
---

# Connecteur JIRA — deux instances, lecture large et écriture bornée

Interroge en langage naturel les **deux instances Atlassian** de Vente-unique :

| Tenant | Site | Ce qu'elle porte |
|---|---|---|
| **PROJET** | `vuproject.atlassian.net` | le métier — `SUPPLY` (les chantiers), `VUD` (les incidents d'expédition), et dix autres projets hors périmètre L&T |
| **WF** | `webfacto.atlassian.net` | le travail des développeurs internes |

Deux commandes dédiées, une par instance : **`/jira-projet`** et **`/jira-wf`**.
Deux exports : **`/jira-projet-export`** et **`/jira-wf-export`** (plus
`/jira-export`, générique, qui demande l'instance). Et **`/jira-setup`** pour la
mise en service.

**33 outils. 29 lisent, 4 écrivent.** Le connecteur a été en lecture seule
jusqu'au 2026-09-02 ; il écrit depuis, et le principe n'a pas bougé : aucun
appel ne part vers un chemin qui n'est pas nommé dans un fichier versionné à
côté du serveur. Ce qui a changé, c'est qu'il y a désormais **deux** listes
blanches — `rest_get_paths.json` pour la lecture, `rest_write_paths.json` pour
l'écriture, où **la clé porte le verbe**. Voir « Écrire » plus bas : un statut
qui bouge reste un geste humain, mais un geste humain **vérifiable** plutôt
qu'un geste refait à la main dans l'interface.

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

## Écrire : quatre gestes, trois garde-fous

Quatre gestes, et quatre seulement : **créer** un ticket, **mettre à jour** ses
champs, **commenter**, **franchir une transition**. Ce sont les quatre entrées de
`rest_write_paths.json`, chacune avec son verbe et son pourquoi ; les gestes
volontairement absents y sont listés aussi, avec la raison de leur absence.

**Aucun DELETE n'est exposé**, ni sur un ticket ni sur un commentaire, et ce
n'est pas un oubli : un ticket qui n'a pas lieu d'être se ferme par une
transition, ce qui garde la trace. Un commentaire faux se corrige par un
commentaire qui rectifie, pas par un effacement silencieux.

La clé de la liste blanche porte **le verbe**, et c'est ce qui fait tout :
`PUT /issue/{clé}` met à jour, `DELETE /issue/{clé}` détruit — le même chemin,
deux gestes sans rapport. Autoriser un chemin sans son verbe, ce serait
autoriser la suppression en croyant autoriser la mise à jour.

### Les trois garde-fous

1. **Rien ne part sans `confirmer=True`.** Un appel sans confirmation ne touche
   pas JIRA : il affiche le **corps exact** qui serait envoyé, l'instance visée
   et **le compte sous lequel l'action sera tracée**, puis s'arrête. C'est la
   règle « l'envoi est un geste humain » du cerveau d'équipe, rendue
   vérifiable — on ne demande pas de croire un résumé.
2. **Aucune écriture n'est rejouée.** Le serveur rejoue un GET sur une coupure
   réseau, un 429 ou un 5xx ; il ne rejoue **jamais** un POST, pas même sur un
   429 où ce serait pourtant sans risque. La règle n'a pas d'exception parce
   qu'une règle sans exception se tient. Un `POST /issue` rejoué après un délai
   d'attente, c'est un doublon dans le référentiel, et le serveur ne peut pas
   savoir si le premier appel a abouti : il le dit, et laisse vérifier.
3. **Rien n'est deviné.** Le type de ticket, la priorité, la transition et la
   personne sont relus sur l'instance et comparés **à l'identique**. Un libellé
   approchant est **refusé** avec la liste des valeurs réelles — jamais remplacé
   par le plus proche. « Tâche » quand le projet déclare « Task » crée un ticket
   du mauvais type, et JIRA ne s'en plaint pas. Une transition est résolue sur
   celles réellement franchissables pour **ce** ticket, relues juste avant.

### Ce que le connecteur refuse avant même d'appeler

Une clé mal formée, un titre vide ou sur deux lignes, un titre de plus de 255
caractères, un projet désigné par un nom au lieu de sa clé, une étiquette avec
un espace, une échéance en langage naturel, un statut posé comme un champ, une
mise à jour qui ne change rien, une étiquette à la fois ajoutée et retirée. Tous
refusés **sans un seul appel réseau** — un refus qui ne coûte rien est un refus
qu'on peut se permettre de rendre strict.

Deux cas méritent leur explication :

- **une échéance ne s'écrit qu'en `AAAA-MM-JJ`.** En lecture, le connecteur
  traduit « la semaine prochaine » ; en écriture, il refuse. C'est la règle « on
  n'invente jamais une date » du cerveau d'équipe : une date fausse dans un
  ticket devient une date fausse dans un mail à un transporteur.
- **un statut ne se pose pas comme un champ**, il se franchit. C'est aussi la
  seule façon dont JIRA l'accepte, mais l'erreur est fréquente et le message le
  dit plutôt que de laisser JIRA rendre un 400 opaque.

### Qui signe l'écriture

Avec les identifiants d'équipe, c'est le **compte de service** qui apparaît dans
l'historique du ticket, pas la personne. Acceptable pour lire, discutable pour
écrire : chaque aperçu affiche donc le compte tracé et son origine avant de
confirmer, et rappelle qu'un **jeton nominatif posé sur le poste passe devant
celui de l'équipe**. On ne retire pas la ligne partagée, on la surcharge chez
soi.

### La mise à jour écrase

Un champ posé dans `fields` **remplace** la valeur existante — sur une
description, tout l'existant part, et JIRA n'en garde que l'historique.
L'aperçu chiffre donc ce qui serait écrasé (« ECRASEMENT de 1 240 caractère(s)
existant(s) ») avant de confirmer. Les étiquettes, elles, sont **incrémentales** :
`etiquettes_ajout` et `etiquettes_retrait` ne touchent que celles nommées.

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

Cette requête-là n'a rien à voir avec les quatre gestes d'écriture : elle va au
service d'authentification, pas à l'API Jira, et ne touche aucune donnée. Elle
n'est donc soumise à aucune confirmation — elle ne change rien chez personne.

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

## Les 33 outils

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

**Résoudre avant d'écrire, en lecture** — `jira_types_ticket` (les types réels
d'un projet, avec leur id), `jira_transitions` (les transitions franchissables
pour ce ticket, maintenant).

**Écrire, sous `confirmer=True`** — `jira_creer_ticket`, `jira_maj_ticket`,
`jira_commenter_ticket`, `jira_transition_ticket`.

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

**188 contrôles hors ligne, aucun appel réseau.** Ils couvrent la traduction
français → JQL, **les refus** (une période incompréhensible, un nom au lieu d'un
accountId, une recherche sans filtre, un ordre qui n'est pas un tri), les **deux**
listes blanches, le rendu de l'ADF **et sa construction**, le refus des secrets
OAuth dans le fichier d'équipe, la lecture d'un `.env` avec BOM, et la signature
des outils MCP — le bug `functools.wraps` qui publie des outils inappelables sans
erreur au démarrage.

**Sur l'écriture, deux contrôles portent plus que les autres.** Le premier vérifie
que `DELETE /issue/{clé}` est refusé alors que `PUT` sur le **même chemin** passe :
un contrôle qui ne regarderait que le chemin laisserait passer la suppression. Le
second remplace `_request` par un mouchard qui **lève** si un verbe autre que GET
est émis, puis rejoue les quatre gestes sans confirmation : il échoue donc le jour
où un outil écrirait sans `confirmer=True`, ce qu'aucune assertion sur le texte
rendu ne verrait.

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
- **Le connecteur n'a pas encore été confronté à un jeton valide.** Les 188
  contrôles hors ligne passent, le serveur démarre, publie ses 33 outils et rend
  ses erreurs proprement. Les premiers résultats réels sont à regarder comme une
  recette, pas comme une mesure établie.
- **Aucune écriture n'a jamais atteint une vraie instance.** Les quatre gestes
  sont éprouvés hors ligne, aperçu compris, contre une API simulée. La première
  écriture réelle se fait donc **sur un ticket d'essai**, dans un projet où un
  ticket de trop ne dérange personne — et en regardant l'aperçu, pas en
  confirmant d'emblée. Le compte de service doit par ailleurs être provisionné
  **en lecture** tant que l'écriture n'est pas arbitrée : un 403 propre vaut
  mieux qu'un ticket créé sous le nom du compte d'équipe.
- **L'ouverture de l'écriture n'est pas encore arbitrée en équipe.** Le
  `CLAUDE.md` collectif et `08_ENGINE/README.md` écrivent encore que les
  connecteurs de l'équipe sont « en lecture seule par construction, pas par
  convention ». Ce connecteur est le premier à en sortir : c'est un point à
  trancher à quatre, pas une décision à prendre dans un README de plugin.
- **Le mode `oauth` est écrit et non éprouvé** : il attend une application
  inscrite côté Atlassian, donc le jalon Webfacto.

## Le jalon Webfacto

Le prototypage et l'usage individuel de ce connecteur sont libres. **Deux choses
demandent un cadrage Webfacto préalable** (besoin, faisabilité, sécurité,
priorisation) : l'inscription d'une application OAuth sur le domaine Atlassian de
l'entreprise, et toute diffusion du connecteur à l'échelle de l'équipe qui
supposerait un compte de service ou un dépôt Git d'organisation.
