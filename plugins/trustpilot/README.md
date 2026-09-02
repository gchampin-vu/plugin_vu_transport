---
updated: 2026-09-02
updated_by: Guillaume_Champin
---

# `trustpilot` — les avis clients des dix-huit domaines, en lecture seule

Trustpilot n'est pas un outil de transport. C'est la **seule source où le client
raconte lui-même comment la livraison s'est passée**, dans sa langue, avec une
note. Le Scorecard Transporteur mesure ce que le transporteur déclare ;
Trustpilot mesure ce que le client a vécu. **L'écart entre les deux est le
sujet.**

| | |
|---|---|
| Périmètre | 18 domaines : `vente-unique.com` et ses 14 déclinaisons pays, `kauf-unique.de` et `.at`, `habitat.fr`, `habitat-design.com` |
| Langues | fr, de, nl, it, es, pt, pl, da, sv, nb/no, en |
| Historique | depuis `TRUSTPILOT_START_DATE` (2023-01-01, repris du script Power Query d'origine) |
| Outils | 22, annotés selon la spécification MCP : 17 en lecture seule, 5 qui écrivent sur le disque **du poste** — aucun n'écrit chez Trustpilot |
| Écriture | **aucune** — ni réponse à un avis, ni tag, ni invitation |

## Ce qu'il ne fait pas, et pourquoi ce n'est pas une option

Répondre à un avis, poser un tag, contacter un auteur : ces gestes **engagent la
marque devant un client sur une page publique**. Les chemins correspondants ne
sont pas derrière un garde-fou, ils **ne sont pas exposés du tout** — la liste
blanche `server/api_paths.json` ne contient que des `GET`, et `trustpilot_get`
n'émet que cette méthode. C'est la règle n° 4 du `CLAUDE.md` d'équipe —
l'envoi est un geste humain — appliquée à une API qui parle au client final.

Il ne remplace pas non plus le Scorecard : un avis est un ressenti **déclaratif
et volontaire**, pas une mesure de flux. Le nombre d'avis d'un pays ne dit rien
de son volume d'expédition.

## Ce que ce connecteur apporte, et que l'export Trustpilot ne donne pas

**1. Il interprète les commentaires.** Un classement thématique en treize thèmes,
dans les onze langues du périmètre, et une détection des transporteurs cités
**désambiguïsée par pays** — c'est ce qui distingue Rhenus Italie de Rhenus
Pologne, et Bring (Suède) de Posten Bring (Norvège), et ce qui évite de compter
le mot norvégien « posten », qui veut aussi dire « la poste ».

**2. Il agrège côté serveur.** `trustpilot_summary` pagine, calcule et ne rend
que le résultat : nombre d'avis, note moyenne, part de 1-2 étoiles, part de 4-5,
taux de réponse de l'entreprise, délai de réponse médian. Sans lui, répondre à
« quelle note en Italie ce trimestre » ferait remonter des milliers d'avis dans
la conversation pour les compter à la main.

**3. Il relie l'avis au SI.** Le `referenceId` d'un avis **est notre numéro de
commande**. C'est la jointure vers le back-office, Reflex et Shiptify — donc
vers le transporteur réel. Un avis à une étoile devient une commande, donc une
tournée, donc un transporteur.

**4. Il couvre les dix-huit domaines.** La note Trustpilot est suivie chaque
semaine par le service client, sur FR et Habitat seulement. Ici, c'est le
périmètre entier, d'un seul appel.

## Le chemin public et le chemin privé

C'est la distinction structurante du connecteur, et la seule chose à comprendre
avant de s'en servir.

| | Public (clé seule) | Privé (clé **et** secret) |
|---|---|---|
| Avis, notes, TrustScore | oui | oui |
| Filtre de date **côté serveur** | non | oui |
| `referenceId` (notre n° de commande) | **non** | oui |
| Retrouver l'avis d'une commande | **impossible** | oui |

Sans le secret, une question sur une période fait lire du plus récent au plus
ancien jusqu'à la borne : c'est long, et l'API publique ne remonte pas
indéfiniment. Surtout, **la moitié de l'intérêt pour l'équipe Transport tombe** :
sans `referenceId`, pas de lien avis → commande → transporteur. Il n'existe
aucun contournement public. `trustpilot_setup_status` et `trustpilot_doctor`
disent lequel des deux régimes est ouvert, séparément — un poste peut très bien
avoir un public qui répond et un privé qui échoue.

**Le refresh token est un piège, pas une option.** Avec la clé et le secret, le
connecteur s'authentifie par `client_credentials` : rien ne tourne, rien
n'expire. Trustpilot fait **tourner** un refresh token à chaque échange — deux
postes qui le partagent se le cassent mutuellement, et rendent un
`invalid_grant` que personne ne rattache à la cause. Le champ existe dans la
configuration du plugin pour le cas où une application l'imposerait ; il se
laisse vide, et il est **refusé** dans le fichier d'équipe.

## Les 22 outils

| Famille | Outils |
|---|---|
| Mise en service | `trustpilot_setup_status`, `trustpilot_save_key`, `trustpilot_forget_key`, `trustpilot_doctor` |
| Repères | `trustpilot_list_paths`, `trustpilot_lexique`, `trustpilot_business_units`, `trustpilot_profile` |
| Lire les avis | `trustpilot_list_reviews`, `trustpilot_get_review`, `trustpilot_find_review` |
| Comprendre | `trustpilot_summary`, `trustpilot_themes`, `trustpilot_verbatims`, `trustpilot_transporteurs` |
| Sortir un fichier | `trustpilot_export_csv`, `trustpilot_export_sql` |
| Échappatoire | `trustpilot_get` (liste blanche, `GET` seulement) |
| Cache local | `trustpilot_sync`, `trustpilot_tables`, `trustpilot_columns`, `trustpilot_sql` |

La compétence `trustpilot` (`skills/trustpilot/SKILL.md`) sait lequel appeler
pour quelle question, et porte les pièges ci-dessous sous une forme utilisable
en session.

**Les annotations MCP disent la vérité, pas la promesse commerciale.**
`readOnlyHint` est pris au sens de la spécification — l'outil ne modifie pas son
environnement — donc il est à **faux** sur les cinq outils qui écrivent sur le
disque du poste : `save_key`, `forget_key`, `sync`, `export_csv`, `export_sql`.
Aucun n'écrit chez Trustpilot. Les annoncer en lecture seule ferait approuver
sans regard un export de verbatims clients. Les dix-sept autres sont en lecture
seule, et `openWorldHint` distingue ce qui interroge Trustpilot de ce qui lit le
lexique embarqué ou le cache local.

## Les pièges, et ce que le connecteur en fait

| Le piège | Ce qui se passerait sans lui | Ce que fait le connecteur |
|---|---|---|
| **TrustScore ≠ note moyenne** | une note « qui baisse » dans un compte rendu alors que rien n'a bougé | deux outils distincts, et chacun dit lequel il rend |
| **Le nombre d'avis n'est pas un volume** | comparer deux pays sur l'effectif d'avis, qui mesure surtout le taux de réponse aux invitations | l'effectif est toujours rendu **à côté** de la note, jamais seul |
| **Petits effectifs** (LU, IE, DK) | une note de 3,6 sur huit avis lue comme un signal | l'effectif accompagne chaque note ; la compétence impose de le citer |
| **Thèmes lexicaux** | une détection de mots prise pour une compréhension du texte — « aucun retard » compte dans le thème délai | l'outil rend son **taux de non-classés** et le dit dans son en-tête |
| **La casse n'est pas toujours le transport** | 77 % des scans de casse UK sont constatés **à réception**, donc avant le transporteur | le rappel est dans le lexique et dans les avertissements de l'outil |
| **`habitat-design.com` sert six locales** | un pays attribué à tort au domaine | aucun pays unique ne lui est attribué ; le pays se lit dans la langue et `consumer.displayLocation` |
| **Le norvégien est codé `nb` ou `no`** | la moitié du pays perdue par un filtre de langue | les deux valeurs sont traitées ensemble |
| **`createdAt` ≠ `experiencedAt`** | une période décalée entre publication et vécu | les filtres portent sur `createdAt`, et le rendu le dit |
| **Le quota est compté par application** | un `429` interprété comme une clé invalide | le message nomme la cause : le quota est partagé par l'équipe |
| **Transporteur cité ≠ transporteur réel** | un nom lu dans un verbatim porté en revue transporteur | trois avertissements rendus par l'outil, et la chaîne fiable rappelée : `referenceId` → commande → Reflex / Shiptify |

## Les données personnelles

Un avis Trustpilot porte un nom d'affichage, parfois une ville, et — par le
chemin privé — l'adresse mail du client invité.

- `referralEmail` est **retiré par défaut** des exports ;
- `trustpilot_find_review` accepte une adresse pour **filtrer**, et ne la
  **restitue jamais** ;
- `inclure_donnees_personnelles` est faux par défaut, et son passage à vrai se
  signale à l'utilisateur ;
- les exports vont dans `~/.trustpilot-mcp/exports/`, **jamais dans la
  bibliothèque d'équipe** : un CSV de verbatims déposé dans un dossier
  synchronisé part chez les vingt-six, et la base de connaissance ne porte pas
  de données — elle dit **où** elles sont.

## Installation

Le catalogue `vu-transport` est déjà connu de ceux qui ont un autre connecteur
de l'équipe. Sinon, ajoute-le une fois, puis :

```
/plugin install trustpilot@vu-transport
```

Les dépendances (`mcp`, `httpx`) s'installent seules au premier démarrage, dans
`~/.trustpilot-mcp/venv`. Puis `/trustpilot-setup`, qui guide pas à pas et
vérifie à chaque étape.

## Configuration

L'ordre de priorité est celui de tous les connecteurs de l'équipe :
**configuration du plugin > `.env` du poste > fichier d'équipe > défaut du
serveur**. Le poste passe devant l'équipe — c'est la sortie pour un accès
nominatif ou une recette, sans toucher au fichier partagé.

| Réglage | Où il a sa place | À quoi il sert |
|---|---|---|
| `TRUSTPILOT_API_KEY` | **fichier d'équipe** (ou plugin) | la clé de l'application — « API Key » / Client ID |
| `TRUSTPILOT_API_SECRET` | **fichier d'équipe** (ou plugin) | ce qui ouvre le chemin privé |
| `TRUSTPILOT_REFRESH_TOKEN` | **poste uniquement** | refusé dans le fichier d'équipe, et signalé |
| `TRUSTPILOT_BASE_URL` | équipe | racine de l'API |
| `TRUSTPILOT_DOMAINS` | équipe | les dix-huit domaines interrogés par défaut |
| `TRUSTPILOT_START_DATE` | équipe | début de l'historique (2023-01-01) |
| `TRUSTPILOT_PER_PAGE`, `TRUSTPILOT_MAX_PAGES`, `TRUSTPILOT_TIMEOUT_S` | équipe | pagination et garde-fous |
| `TRUSTPILOT_EXPORT_DIR` | **poste uniquement** | un chemin valable ici n'existe pas sur les vingt-cinq autres postes |

Le fichier d'équipe est `08_ENGINE/04_mcp/00_config/trustpilot.shared.env`.
**Il n'existe pas encore** — le connecteur tourne sur ses valeurs par défaut et
le dit dans `trustpilot_setup_status`. Modèle versionné à côté du serveur :
`server/trustpilot.shared.env.example`. Le déposer est une **écriture dans le
collectif** : elle prend effet sur tous les postes au prochain démarrage de
session, et une valeur fausse casse les vingt-six en même temps.

Modèle du fichier de poste : `server/.env.example`.

Le serveur applique une **liste blanche** à la lecture du fichier d'équipe : une
clé qu'il ne connaît pas est ignorée **et signalée** par `/trustpilot-setup`. Ce
n'est pas une barrière anti-secret, c'est un garde-fou contre la faute de
frappe, qui sinon passerait inaperçue sur les vingt-six postes.

## Le cache local

`trustpilot_sync` rapatrie les avis dans un SQLite sous `~/.trustpilot-mcp/`,
**hors de tout dossier synchronisé**. C'est le chemin de l'historique : il n'est
pas soumis au garde-fou `TRUSTPILOT_MAX_PAGES` qui plafonne le direct à 6 000
avis par domaine — plafond qui se dépasse en quelques mois sur
`vente-unique.com`.

Le classement thématique et la détection de transporteur sont calculés **à la
synchro** et rangés en colonnes : `trustpilot_sql` peut donc filtrer dessus,
instantanément, sans reconsommer le quota partagé. **Si le lexique change, il
faut resynchroniser** pour que les colonnes suivent.

## Contrôles

```
python server/test_offline.py
```

**188 contrôles, sans réseau**, verts le 2026-09-02 : résolution des domaines et
de leurs alias, classement thématique dans les onze langues, désambiguïsation
des transporteurs par pays, agrégations, écriture CSV, exclusivité du chemin de
configuration d'équipe, dégradation propre sans secret, **pureté du flux stdio**
et non-divulgation des secrets dans `setup_status` et `doctor`.

Le contrôle en ligne est `python server/bootstrap.py doctor`.

**Ce que ces contrôles ne prouvent pas :** ils ne touchent pas l'API Trustpilot.
La liste blanche des chemins a été écrite à la main, faute de contrat OpenAPI
publié — **elle n'est confirmée que par une recette contre le vrai compte, avec
une clé valide. Cette recette n'est pas passée.**

## Portabilité

Le connecteur suit les conventions de
[`08_ENGINE/04_mcp/README.md`](../../../04_mcp/README.md) : `pathlib` partout,
aucun chemin absolu en dur, racine locale sous le profil utilisateur (**pas**
`%LOCALAPPDATA%`, qu'un Python empaqueté virtualise), `sys.executable` comme
seul interpréteur nommé, `subprocess.run` avec une liste d'arguments — le chemin
du plugin traverse `Transport BtoC - Documents`, qui contient des espaces et un
tiret —, encodage explicite partout, et **rien sur `stdout`** : le journal part
sur `stderr`. `install.ps1` est un confort Windows, jamais le chemin nominal :
le point d'entrée supposé est `bootstrap.py`, sinon le plugin ne serait pas
installable sur Mac.

## Ce qu'il reste à faire

1. **La recette contre le vrai compte Trustpilot** — c'est le seul contrôle qui
   confirme la liste blanche des chemins et le régime privé.
2. **Déposer `trustpilot.shared.env`** dans `08_ENGINE/04_mcp/00_config/`, avec
   la clé et le secret de l'application. Décision d'équipe : cette bibliothèque
   est lisible par toute l'équipe L&T, donc les secrets qu'on y dépose le sont
   aussi. Tant que le fichier n'existe pas, chacun saisit dans `/plugin`.
3. **La jointure `referenceId` → Reflex / Shiptify**, aujourd'hui faite à la
   main. C'est la suite naturelle, et elle mérite d'être écrite une fois.

## Le jalon Webfacto

Le prototypage et l'usage individuel de ce connecteur sont libres. **Dès qu'il
s'agit d'industrialiser** — brancher les avis sur un tableau de bord, alimenter
un suivi qualité automatisé, croiser avec le SI en production ou publier hors
SharePoint — le cas d'usage passe par un cadrage Webfacto préalable : besoin,
faisabilité, sécurité, priorisation.
