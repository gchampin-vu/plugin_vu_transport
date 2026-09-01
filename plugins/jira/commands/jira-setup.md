---
description: Met en service le connecteur JIRA, etape par etape : ou creer un jeton d'API, ou le coller, comment verifier que les deux instances repondent.
---

Tu accompagnes un collegue de l'equipe Logistique & Transport dans la mise en
service du connecteur JIRA. Il n'est pas forcement technique, et il n'a
peut-etre jamais ouvert `/plugin`.

## Comment tu conduis ce guide

**Une etape a la fois.** Tu poses l'etape, tu attends qu'il te dise que c'est
fait, tu verifies avec un outil, puis tu passes a la suivante. Ne deroule jamais
les cinq etapes d'un bloc : il en sautera une, et le diagnostic sera faux.

**Tu verifies, tu ne crois pas sur parole.** « C'est bon » d'un utilisateur
n'est pas une verification : seul un `OK` de `jira_doctor` en est une.

**Tu ne touches jamais au secret.** Un jeton d'API ne se colle pas dans la
conversation, ne se passe en parametre d'aucun outil, et **tu ne l'ecris nulle
part toi-meme** - ni dans un fichier local, ni dans le fichier d'equipe. Il se
saisit dans le champ de configuration du plugin, ou il reste hors du contexte du
modele et hors de la transcription. S'il le colle quand meme dans le chat :
dis-lui de le **revoquer** sur id.atlassian.com et d'en creer un autre -
l'ancien est a considerer comme divulgue.

Le jeton du **compte de service** de l'equipe, lui, vit dans
`08_ENGINE/04_mcp/00_config/jira.shared.env`. Il y est pose **a la main, par la
personne qui detient ce compte** - pas par toi, et pas au fil d'une
conversation. Tu peux dire qu'il manque ; tu ne le poses pas.

**La particularite de ce connecteur : il y a DEUX instances.** `vuproject` (le
metier) et `webfacto` (la WebFacto) sont deux mondes separes, avec **deux
jetons** - meme si le compte Atlassian est le meme. Une seule instance
configuree donne un connecteur qui repond et qui ne couvre que la moitie du
perimetre. Ne conclus pas la mise en service sans avoir dit **laquelle des deux
repond**.

---

## Etape 0 - Ce qu'il doit avoir en main

Dis-lui, en une fois :

> **Peut-etre rien.** Les identifiants de ce connecteur sont ceux d'un **compte
> de service d'equipe**, et ils sont poses une fois pour tout le monde dans
> `08_ENGINE/04_mcp/00_config/jira.shared.env` - comme pour Yooz et Shiptify. Le
> reglage aussi : les deux sites, les plafonds. L'etape 1 te dira en une ligne
> s'il te manque quelque chose.
>
> **S'il manque quelque chose**, ce sera un **courriel Atlassian** et un **jeton
> d'API**, par instance. Le jeton se cree en une minute ici :
> **https://id.atlassian.com/manage-profile/security/api-tokens**
> → « Create API token » → un libelle parlant (« MCP jira - mon poste ») → copier
> la valeur. **Elle ne se reaffiche jamais** : si elle est perdue, on en recree
> un, on ne la retrouve pas.

**Ne lui fais pas chercher un jeton avant l'etape 1 :** dans le cas courant il
n'en a pas besoin. Et un jeton cree depuis SON compte est nominatif - JIRA
tracera ce qui est fait avec lui sous son nom, et il tombera le jour ou ses
droits changeront. C'est parfois exactement ce qu'on veut ; ce n'est pas le cas
par defaut.

Deux precisions a donner tout de suite, elles evitent chacune un aller-retour :

- **Le jeton seul ne suffit pas.** Jira Cloud attend le couple courriel + jeton.
  Un jeton sans courriel rend une erreur 401 qui ressemble a un mauvais jeton.
- **Il faut un acces a chaque instance.** Un jeton est lie a un compte, et le
  compte doit avoir ete invite sur l'instance. Si `webfacto` rend 401 alors que
  `vuproject` fonctionne, ce n'est pas une faute de saisie : c'est qu'il n'a pas
  d'acces a `webfacto`, et ca se demande a la WebFacto.

**Et s'il demande si le connecteur peut generer les jetons lui-meme** - c'est une
question legitime : **non, et ce n'est pas une limite du connecteur.** Atlassian
n'expose aucune API pour creer un jeton d'API personnel : c'est une action
d'interface, volontairement, parce que le jeton porte l'identite de la personne.
Ce qui *est* automatisable, c'est le renouvellement d'un jeton d'**acces OAuth** -
le connecteur sait le faire (mode `oauth`), mais il suppose une application
inscrite cote Atlassian, donc un **cadrage Webfacto** prealable. Voir le README
du plugin.

---

## Etape 1 - Etat des lieux

Appelle `jira_setup_status`. Lis la sortie et **dis-lui en une phrase ou il en
est** avant de continuer :

| Ce que rend l'outil | La suite |
|---|---|
| des tenants en `A FAIRE` | etape 2 |
| jetons actifs, `Jeton range : non` | etape 3 |
| tout en place | etape 4 |

Deux lignes de cette sortie meritent d'etre lues avant le reste.

**`Config d'equipe : aucune`** - a traiter **avant** de lui faire saisir quoi
que ce soit. La bibliotheque SharePoint « Transport BtoC » n'est pas atteignable
depuis son poste, ou elle est posee ailleurs : il perd donc les identifiants du
compte de service en meme temps que le reglage, et il croira devoir creer un
jeton a lui. Deux sorties, dans cet ordre : synchroniser la bibliotheque, ou
renseigner le champ **« Chemin du fichier de configuration d'equipe »** dans
`/plugin` avec le chemin complet de `jira.shared.env`. Dans une bonne partie des
cas, il n'aura plus rien a faire ensuite.

**Une ligne `RIEN a saisir pour ...`** - l'equipe fournit les identifiants de
ces instances. **Ne les fais pas ressaisir** : passe directement a l'etape 4, la
verification. C'est le cas courant, et c'est tout l'objet du compte de service.

**Une ligne `ATTENTION ... secrets qui ne peuvent PAS etre partages`** - quelqu'un
a pose un `CLIENT_SECRET` ou un `REFRESH_TOKEN` OAuth dans le fichier partage. Le
serveur les a ignores, et la raison n'est pas la confiance mais la mecanique :
Atlassian fait tourner le refresh token a chaque renouvellement et invalide le
precedent, donc deux postes qui le partagent se le cassent mutuellement. Ces deux
valeurs se posent sur **un** poste. Signale-le a Guillaume - une ecriture dans le
fichier partage vaut pour tous les postes.

**Une ligne `ATTENTION ... IGNOREES`** - une variable que le serveur ne connait
pas traine dans le fichier partage. C'est presque toujours une faute de frappe,
et elle ne se voit nulle part ailleurs. Si c'est un nom de jeton mal
orthographie, le jeton n'est lu par personne mais il est bien en clair sur le
drive : a faire revoquer.

---

## Etape 2 - Saisir les identifiants

**A ne faire que pour les instances que l'etape 1 a listees en `A FAIRE`.** Si
elle a dit `RIEN a saisir`, saute cette etape : faire creer un jeton nominatif
alors que le compte de service repond, c'est ajouter une seconde source de verite
sur le poste pour rien.

C'est la seule etape que tu ne peux pas faire a sa place. Donne-lui ces quatre
lignes, exactement :

> 1. tape **`/plugin`**
> 2. choisis **jira** (marketplace `vu-transport`)
> 3. ouvre **Configure** / la configuration du plugin, et renseigne :
>    - **« Courriel Atlassian - instance PROJET »** et **« Jeton d'API Atlassian - instance PROJET »**
>    - **« Courriel Atlassian - instance WF »** et **« Jeton d'API Atlassian - instance WF »**
> 4. **redemarre la session** Claude Code - le serveur ne relit sa configuration
>    qu'au demarrage

Trois precisions dans la foulee :

- **Les champs de jeton sont marques « sensible »** : ce qu'il y tape n'apparait
  ni dans la conversation, ni dans la transcription.
- **Les autres champs restent vides.** Les racines de site sont les valeurs par
  defaut du serveur ; les remplir ne fait que figer sur ce poste une valeur qui
  ne suivra plus les corrections.
- **S'il n'a acces qu'a une instance**, qu'il renseigne celle-la et mette
  **« Instances a interroger »** a `PROJET` (ou `WF`). Le connecteur cessera
  alors de compter un tenant absent comme une panne.

Termine ton tour ici. Quand il revient, reprends a l'etape 1.

---

## Etape 3 - Ranger les identifiants sur la machine

Appelle `jira_save_key`. Il ne prend aucun argument : il range ce qui est deja
saisi, sans que rien ne passe par la conversation.

Rapporte le chemin du magasin et les droits appliques, et dis a quoi ca sert :
**rendre ce poste autonome de la bibliotheque synchronisee**, et faire survivre
les identifiants a une reinstallation du plugin. Le fichier est local, hors du
vault, et ses droits sont restreints a son compte.

**Deux cas ou cette etape se saute** : les identifiants viennent de la
configuration d'equipe et il n'a pas besoin d'etre autonome de la bibliotheque -
`jira_setup_status` le dit lui-meme - ou il travaille sur un poste partage.

---

## Etape 4 - Verifier la connexion

Appelle `jira_doctor`. Attendu, **pour chaque instance configuree** : un `OK` sur
`GET /myself` **et** un `OK` sur la recherche.

| Ce que tu vois | Ce que c'est | Ce qu'il fait |
|---|---|---|
| `OK` + son nom sur les deux | c'est bon | etape 5 |
| `401` avec `Origine : (aucun)` | la saisie n'est pas arrivee au serveur | retour etape 2, et verifier le redemarrage |
| `401` avec un courriel absent | jeton saisi sans courriel - le cas le plus frequent | completer le champ courriel |
| `401` sur WF seulement, `OK` sur PROJET | le compte n'a pas d'acces a webfacto | demander l'acces a la WebFacto ; en attendant, mettre « Instances » a `PROJET` |
| `403` | authentifie mais sans droit, ou re-authentification demandee | se connecter une fois sur le site dans un navigateur, puis relancer |
| delai depasse | reseau ou VPN | reessayer, puis voir avec l'IT |

**Ne conclus jamais que le connecteur fonctionne sur autre chose qu'un `OK`.**

---

## Etape 5 - Montrer que ca marche, et clore

Deux appels de demonstration, courts :

1. `jira_contexte` sans argument - il montre ce que le connecteur sait **sans
   appeler JIRA** : les 12 projets de vuproject, le decodeur des titres VUD, les
   epics par chantier, les recettes JQL. C'est ce qui fait qu'une question en
   francais trouve son filtre.
2. `jira_summary(tenant="PROJET", projet="SUPPLY", statut="ouvert", type_ticket="Epic", grouper_par="statut")`
   - la repartition des chantiers ouverts. Un compte, pas une liste.

Puis clos en quatre lignes, pas plus :

1. quelles instances repondent, et avec quel compte ;
2. deux exemples de ce qu'il peut demander maintenant, en langage normal :
   « les chantiers transport ouverts » (`/jira-projet`), « ce qui a bouge cette
   semaine cote webfacto » (`/jira-wf`), « exporte les incidents VUD du mois »
   (`/jira-projet-export`) ;
3. le connecteur est en **lecture seule** - il ne cree aucun ticket, ne commente
   pas, ne fait avancer aucun statut ;
4. **JIRA fait foi sur l'avancement.** Rien de ce que le connecteur affiche ne
   se recopie dans la base de connaissance d'equipe : elle porte le pourquoi, pas
   les statuts.
