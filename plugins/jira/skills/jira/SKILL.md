---
name: jira
description: Interroger les deux instances JIRA de Vente-unique, et y ecrire quatre gestes bornes sous confirmation - vuproject (le metier : chantiers SUPPLY, incidents d'expedition VUD) et webfacto (les developpements de la WebFacto). Active des qu'une question porte sur un ticket, un epic, une cle de type SUPPLY-1234 ou VUD-1234, un chantier ou un projet suivi dans JIRA, l'avancement d'un lancement pays, Atlas, TIL, une demande faite a la WebFacto, un incident d'expedition, ou des qu'il faut extraire des tickets en CSV. Declenche aussi sur "jira", "ticket", "epic", "sprint", "backlog", "JQL", "SUPPLY", "webfacto", "vuproject", "ou en est", "qui porte", "quels chantiers", "export des tickets". Active aussi pour ECRIRE dans JIRA : "cree un ticket", "ouvre un ticket SUPPLY", "commente ce ticket", "passe-le en cours", "change le statut", "affecte-le a", "mets a jour la description" - le connecteur montre alors ce qui partirait et attend une confirmation explicite.
---

# Interroger JIRA

JIRA est la **verite de l'avancement** chez Vente-unique. La bibliotheque
d'equipe porte le pourquoi, le contexte et les decisions ; JIRA porte l'etat
reel : ce qui est ouvert, ce qui a bouge, qui porte quoi.

Les outils sont exposes par le serveur MCP `jira` du meme plugin. **29 lisent,
4 ecrivent.** L'ecriture est bornee a quatre gestes - creer, mettre a jour,
commenter, faire franchir une transition - et **rien ne part sans une
confirmation explicite** : voir « Ecrire » plus bas. Tant que l'utilisateur n'a
pas confirme, tu n'as rien change dans JIRA, et tu ne dis pas le contraire.

## La chose a ne jamais oublier : il y a DEUX instances

| Tenant | Site | Ce qu'elle porte |
|---|---|---|
| **PROJET** | `vuproject.atlassian.net` | le metier : `SUPPLY` (les chantiers), `VUD` (les incidents d'expedition), et dix autres projets hors perimetre |
| **WF** | `webfacto.atlassian.net` | le travail des developpeurs internes |

Elles n'ont **rien** en commun : deux jeux d'identifiants, deux referentiels de
projets, deux numerotations. Trois consequences, dans l'ordre ou elles mordent :

1. **Une cle de ticket n'existe que dans son instance.** Tout outil qui prend
   une cle exige donc `tenant` : `jira_issue`, `jira_comments`,
   `jira_changelog`, `jira_children`, `jira_statuses`, `jira_user_lookup`.
2. **Par defaut, les outils de LISTE interrogent les deux** et posent une
   colonne `tenant` en tete. Ne retire jamais cette colonne d'un tableau que tu
   restitues : sans elle, deux lignes de deux instances se confondent.
3. **`max_rows` s'applique PAR instance**, pas au total.

`jira_tenants` dit lesquelles repondent. Si une seule est configuree, dis-le
avant de citer un chiffre.

**Le faux ami previsible :** les epics `WF - TICKET SPOT <MOIS>` sont dans le
projet `SUPPLY` de l'instance **PROJET**. Le prefixe `WF` designe le
destinataire de la demande, pas l'instance. Une question sur « les tickets spot »
releve de PROJET.

## Commence par le contexte embarque, pas par une requete

**`jira_contexte` repond sans appeler JIRA.** Il porte ce qui ne se devine pas,
releve sur l'instance et dans `01_CONTEXTE/JIRA_ET_CHANTIERS.md` :

- les **12 projets** de vuproject, et lesquels sont a nous ;
- le **decodeur des titres VUD** : `HF2607050041-736648-LM` n'est pas une
  phrase, c'est une reference d'expedition, prefixe d'enseigne + date +
  sequence + numero + code de prestation ;
- les **epics par chantier** : Atlas et sa genealogie (SCALIA Log → Atlas,
  `SUPPLY-3527` etant le cadrage), TIL et son tiering transporteur, les
  lancements pays et leurs transporteurs, les etudes de rentabilite d'agence ;
- les **recettes JQL** qui marchent, et les **pieges** qui rendent un chiffre
  faux.

Sans ce detour, on cherche le bon mot dans le mauvais projet. `TIL` ne veut rien
dire pour qui ne sait pas que c'est *Tracking Information Log*.

**Le contexte ne porte AUCUN avancement, volontairement.** Une cle citee la est
un point de depart ; son statut se lit dans JIRA, jamais dans le contexte.

## Compter n'est pas lister - et ici ce n'est pas un detail

**L'API de recherche moderne de Jira Cloud ne rend aucun total.** Elle pagine par
jeton. Un compte s'obtient donc en parcourant les pages, et c'est
**`jira_summary`** qui le fait : il ne demande que les champs a grouper, agrege
cote serveur, et rend la repartition avec les parts.

Utilise-le pour « combien », « comment se repartit », « qui porte quoi », «
l'evolution mois par mois ». Utilise `jira_recherche` quand la question porte sur
des **lignes** : « montre-moi les chantiers ouverts », « quels tickets sur cet
epic ».

**Et lis toujours la ligne de troncature.** Si le rendu dit que la lecture est
incomplete, le compte est un **plancher**, pas un volume. Deux sorties :
resserrer le perimetre jusqu'a une lecture complete, ou rapatrier une fois avec
`jira_sync` et compter en SQL avec `jira_sql`.

## Parle-lui en francais : il traduit, et il montre sa traduction

`jira_recherche` prend des filtres en francais et **affiche le JQL qu'il a
construit**. C'est ce qui rend la traduction verifiable :

- `statut="ouvert"` / `"en cours"` / `"termine"` / `"a faire"` → passe par la
  **categorie** de statut, universelle sur Jira Cloud ;
- `cree_depuis="cette semaine"`, `maj_depuis="30 jours"`, `cree_jusqua="2026-08-01"` ;
- `assigne="moi"` → `currentUser()` ;
- `titre="HF"` cherche dans le **titre seul** - c'est ce qu'il faut sur VUD ;
  `texte="..."` cherche partout, commentaires compris.

**Le serveur REFUSE ce qu'il ne comprend pas**, au lieu de l'ignorer. C'est
voulu : un filtre ignore ne rend pas moins de lignes, il rend toute la base avec
l'air d'avoir compris. Si un filtre est refuse, ne contourne pas en le retirant -
c'est exactement le cas ou le resultat serait faux.

## Les trois pieges qui rendent une reponse fausse

1. **Un filtre par personne exige un accountId.** Depuis la mise en conformite
   RGPD d'Atlassian, `assignee = "Prenom Nom"` ne marche plus sur Cloud.
   `jira_user_lookup` donne l'accountId ; `assigne="moi"` evite la question.
2. **Ne devine jamais un libelle de statut.** Ils sont propres a chaque projet.
   Filtre sur la categorie, ou lis les libelles reels avec `jira_statuses`.
3. **Ne devine jamais un nom de champ personnalise.** `jira_fields` donne l'id
   `customfield_NNNNN` **et** le nom utilisable en JQL.

## Ce qui vaut la peine d'etre lu en entier

`jira_issue` rend la **description en texte lisible** : le serveur aplatit l'ADF
(l'arbre JSON dans lequel Jira stocke les descriptions). C'est ce qui rend
exploitable un cadrage comme `SUPPLY-3527`.

Deux options a connaitre : `commentaires=True` - sur un ticket VUD, l'essentiel
de l'information est dans les echanges, pas dans le titre - et
`historique=True`, qui donne **depuis quand** le ticket est dans son statut. Un
ticket ouvert depuis trois mois dont le statut n'a pas bouge depuis dix semaines,
ce n'est pas la meme conversation qu'un ticket qui vient de changer d'etat.

## L'export : sur demande explicite, et jamais dans la bibliotheque

`jira_export_csv` prend les **memes filtres** que `jira_recherche` : le perimetre
de la question, pagine en entier - pas les lignes affichees. CSV point-virgule,
UTF-8 avec BOM, il s'ouvre dans Excel FR sans assistant d'import.

**Ne l'appelle que si l'utilisateur a demande un fichier.** Il ecrit sur le
disque, dans le dossier local du connecteur - jamais dans la bibliotheque
d'equipe, ou un CSV partirait chez vingt-six personnes.

Les commandes `/jira-projet-export` et `/jira-wf-export` sont la porte d'entree
directe.

## Ecrire : montre d'abord, confirme ensuite

**Quatre gestes, et quatre seulement** : `jira_creer_ticket`, `jira_maj_ticket`,
`jira_commenter_ticket`, `jira_transition_ticket`. Supprimer un ticket ou un
commentaire n'est pas expose, et ce n'est pas un oubli - un ticket sans objet se
ferme par une transition, ce qui garde la trace.

**La regle de conduite, en une phrase : appelle TOUJOURS sans `confirmer`
d'abord.** L'outil rend alors l'apercu - le corps exact, l'instance, le compte
qui signera - sans rien ecrire. Restitue cet apercu et **demande le feu vert**.
Ne rappelle avec `confirmer=True` que sur un « oui » explicite portant sur CE
geste-la. Un accord donne pour un ticket ne vaut pas pour le suivant.

**Ce qu'il faut resoudre avant, jamais deviner :**

- le **type de ticket** avec `jira_types_ticket(tenant, projet)` - les types
  varient d'un projet a l'autre, et « Tache » n'est pas « Task » ;
- la **transition** avec `jira_transitions(tenant, cle)` - elle depend du
  workflow, du statut courant et des droits du compte ;
- la **personne** avec `jira_user_lookup` - Atlassian exige l'accountId, y
  compris pour affecter. `assigne="moi"` evite la question.

Le serveur refuse un libelle approchant plutot que de prendre le plus proche.
**Ne contourne pas ce refus** : c'est exactement le cas ou le geste partirait au
mauvais endroit.

**Trois choses a dire a l'utilisateur, dans l'apercu comme apres coup :**

1. **sous quel compte le geste sera trace.** Avec les identifiants d'equipe,
   c'est le compte de SERVICE qui apparait dans l'historique, pas la personne.
   Si ca compte pour ce geste-la, dis-le : un jeton nominatif pose dans la
   configuration du plugin passe devant celui de l'equipe.
2. **ce qui serait ecrase.** Une description posee remplace l'existante. L'apercu
   en donne la taille ; s'il annonce un ecrasement, propose de relire l'existant
   avec `jira_issue` avant de confirmer.
3. **qu'un commentaire et une transition sont vus de toute l'equipe**, partent en
   notification et peuvent declencher des automatisations. Ce ne sont pas des
   notes privees.

**Si un appel confirme tombe sur une coupure reseau, ne le relance pas.** Le
serveur ne rejoue jamais une ecriture, et il ne peut pas savoir si le premier
appel a abouti. Va verifier - `jira_issue`, `jira_comments` - puis decide.

**Ce qui ne s'ecrit pas par ce connecteur** : un statut pose comme un champ (il
se franchit), une echeance en langage naturel (`AAAA-MM-JJ` seulement, on
n'invente pas une date), une piece jointe, un lien entre tickets, une creation
en masse. Ces gestes-la se font dans l'interface JIRA, ou s'ajoutent apres
discussion.

## Restituer

- **Le resultat d'abord**, la methode ensuite.
- **Le JQL utilise, toujours** : c'est ce qui rend le chiffre verifiable et
  rejouable.
- **Une cle avec son instance et son URL.**
- **La troncature avant le chiffre**, si le rendu la signale.
- **Ce qui a ete ecrit, mot pour mot** : la cle creee et son URL, le statut
  d'avant et d'apres, le compte sous lequel c'est trace. Et si rien n'a ete
  confirme, dis-le clairement : « rien n'a ete ecrit dans JIRA ».
- **Aucun avancement recopie dans la base de connaissance d'equipe.** Si une
  reponse merite d'etre conservee, c'est le *pourquoi* qui monte, pas le statut -
  et sur feu vert de Guillaume, jamais de ta propre initiative.
