---
name: peripass
description: Interroger Peripass, le yard management des sites AUV (Moulins) et AMB (Amblainville) - visiteurs et creneaux de quai, arrivees, check-in et departs, temps d'attente et de rotation, assets sur la cour, taches, profils et tableaux de dispatch. Active des qu'une question porte sur Peripass, sur un creneau ou un rendez-vous transporteur, sur une arrivee ou un depart de camion, sur un temps d'attente sur site, sur une remorque ou une caisse sur la cour, ou des qu'il faut extraire des visiteurs en CSV. Declenche aussi sur "peripass", "yard", "cour", "creneau de quai", "RDV transporteur", "temps d'attente camion", "turnaround", "rotation", "check-in", "AUV", "AMB", "export des visiteurs".
---

# Interroger Peripass

Peripass est le **yard management** de Vente-unique : ce qui se passe entre
l'arrivee d'un camion sur le site et son depart. Les creneaux planifies, les
arrivees reelles, les check-in, les temps d'attente, les quais, les remorques
sur la cour.

C'est la source pour une question de **respect de creneau**, de **temps
d'immobilisation** ou de **rotation sur site**. C'est aussi le chainon qui
manque quand un transporteur conteste un retard : Peripass a l'heure du creneau
et l'heure d'arrivee reelle.

Les outils sont exposes par le serveur MCP `peripass` du meme plugin. Ils sont
**tous en lecture**. Creer un visiteur, changer un statut, deplacer un asset ou
poser une tache ne se font pas ici : un changement de statut dans Peripass fait
bouger un camion sur une cour, ca reste un geste humain dans l'interface.

## La chose a ne jamais oublier : il y a DEUX bases

Peripass n'a pas une base, il en a une par site. **AUV (Moulins / Montbeugny)
et AMB (Amblainville) sont deux tenants distincts**, avec deux cles d'API,
deux jeux d'identifiants, et rien de partage entre eux.

Trois consequences, dans l'ordre ou elles mordent :

1. **Un id n'existe que dans son site.** Le visiteur 4218 existe des deux
   cotes et designe deux camions differents. Tout outil qui prend un id exige
   donc `site` : `peripass_visitor_history`, `peripass_visitor_detail`,
   `peripass_get_asset`, `peripass_get_task`.
2. **Par defaut, les outils de liste interrogent les DEUX sites** et posent une
   colonne `site` en tete. C'est ce que faisait le `Table.Combine` du script
   Power Query. Ne retire jamais cette colonne d'un tableau que tu restitues :
   sans elle, deux lignes de deux sites se confondent.
3. **`max_rows` s'applique PAR SITE, pas au total.** `max_rows=300` sur deux
   sites peut rendre 600 lignes. L'entete de chaque rendu le rappelle.

`peripass_sites` dit quels sites repondent. Si un seul est configure, dis-le
avant de citer un chiffre : un volume calcule sur AUV seul n'est pas le volume
de l'entrepot.

## Le piege qui rend un chiffre faux sans prevenir

**Peripass ignore en SILENCE un filtre mal forme.** C'est ecrit dans son
contrat : « If format is incorrect we will ignore the filter ». Une date mal
ecrite, un statut mal orthographie, un filtre de periode sans champ de
reference : l'API ne rend pas d'erreur, elle rend **toutes les lignes**. Le
chiffre parait plausible et il est faux.

Le serveur pose donc des garde-fous, avant tout appel :

- un `timestamp_start` sans `timestamp_field` est **refuse** ;
- un `status` ou un `search_object` hors liste est **refuse**, avec les valeurs
  permises ;
- un `search_text` sans `search_object` est **refuse**.

Si un outil te refuse un filtre, ne contourne pas en retirant le filtre : c'est
exactement le cas ou le resultat serait faux.

## L'ordre qui marche

1. **`peripass_sites`** — quels sites repondent, et sous quel code.
2. **`peripass_referential`** (`profiles`, `dispatchdashboards`) — les noms
   reels des profils et des quais. **Rien ne garantit qu'AUV et AMB portent
   les memes libelles** : c'est la premiere cause d'un chiffre qui ne se
   compare pas d'un site a l'autre.
3. **`peripass_fields`** — les champs personnalises reellement presents, avec
   leur taux de remplissage. Obligatoire avant toute recherche par champ
   personnalise : `search_field` attend le **nom technique exact, sensible a
   la casse**, et l'API n'expose aucun schema.
4. **`peripass_list_visitors`** avec une periode serree, pour regarder.
5. **`peripass_export_csv`** uniquement si l'utilisateur a demande un fichier —
   voir la section ci-dessous.

Si quelque chose coince, **`peripass_doctor`** avant tout : il dit d'ou vient
chaque cle, si chaque site repond, et ce que le serveur a lu comme
configuration.

## Le filtre de periode : `timestamp_field` d'abord

C'est le filtre central, et il faut choisir **sur quoi** on filtre :

| `timestamp_field` | Ce qu'il date | Quand l'utiliser |
| --- | --- | --- |
| `SlotStart` | le creneau **planifie** | « combien de RDV etaient prevus cette semaine » |
| `Arrived` | l'arrivee **reelle** sur site | « combien de camions se sont presentes » |
| `CheckedIn` | l'entree en operation | analyse de quai |
| `Departed` | le depart | analyse de rotation complete |

**Planifie et reel ne se comptent pas ensemble.** Un ecart entre le nombre de
`SlotStart` et le nombre de `Arrived` sur la meme periode, c'est le taux de
non-presentation : c'est un resultat, pas une incoherence a lisser.

Les dates s'ecrivent en ISO 8601 **avec fuseau** : `2026-08-01T00:00:00Z` ou
`2026-08-01T00:00:00+02:00`. Peripass rend ses horodatages dans l'heure du
tenant avec le decalage — sur un tenant ancien le decalage peut manquer, et une
comparaison entre AUV et AMB devient alors approximative. Dis-le si c'est le
cas plutot que de conclure sur dix minutes d'ecart.

`updated_since` n'est **pas** un filtre de periode : il rend ce qui a bouge
depuis un instant, quelle que soit la date du creneau. Il sert a un
rafraichissement incremental, pas a une analyse.

## L'export est sur demande, jamais par defaut

**La reponse par defaut va dans la session, pas dans un fichier.** N'appelle
`peripass_export_csv` que si l'utilisateur a demande un fichier, un export, un
CSV ou un classeur : cet outil ecrit sur le disque, ce n'est pas une etape de
routine.

Un perimetre trop gros pour la session ne justifie pas un export de ta propre
initiative : resserre la periode, filtre sur un site ou un statut, ou dis a
l'utilisateur que le perimetre demande un export et laisse-le decider.

## Si une cle manque

Un outil qui repond « Aucun site Peripass configure » veut dire que le
connecteur n'est pas encore configure sur ce poste. Appelle
`peripass_setup_status` : il donne la marche a suivre, site par site. La
commande `/peripass-setup` fait le tour complet.

**Regarde la ligne `Config d'equipe` avant d'envoyer saisir quoi que ce soit.**
Le reglage du connecteur — sites, racines d'API, tenants, plafonds, et selon la
decision d'equipe les cles — vit dans
`08_ENGINE/04_mcp/00_config/peripass.shared.env`, pose une fois pour tout le
monde. Si cette ligne dit `aucune`, la bibliotheque SharePoint n'est pas
atteignable depuis ce poste : c'est ca qu'il faut corriger d'abord, pas les
cles. Dans le cas inverse, il n'y a peut-etre rien a saisir du tout.

**Ne demande jamais a l'utilisateur de coller une cle dans la conversation**,
et n'appelle aucun outil en lui passant une cle en parametre — aucun ne
l'accepte. La saisie se fait dans l'interface de Claude Code (`/plugin` >
peripass > configuration), ou la cle reste hors du contexte du modele.

## Le quota : Peripass compte les lignes, pas les requetes

Le quota d'appels de Peripass est calcule sur le **nombre de ressources lues** :
ramener 6000 visiteurs coute 6000 unites, pas 60. Un `HTTP 429` n'est donc pas
un signe qu'on appelle trop souvent, mais qu'on ramene trop large.

En pratique : resserre la periode avant d'augmenter `max_rows`, et ne fais pas
un export complet pour repondre a une question qui tient sur une semaine.

## Les quatre pieges de restitution

**Le nombre de lignes rendu n'est pas un volume.** L'API ne renvoie aucun
total : la pagination s'arrete sur une page vide. Les outils de liste
s'arretent a `max_rows` et le disent en majuscules quand c'est tronque. **Un
chiffre ne se cite que depuis un rendu non tronque** : si la liste annonce une
troncature, resserre le perimetre jusqu'a ce qu'il tienne, ou propose un export
et attends la reponse.

**Un site qui ne repond pas rend le resultat PARTIEL.** Le rendu le dit. Ne
cite pas un chiffre issu d'un rendu partiel comme s'il couvrait les deux sites.

**Les champs personnalises ne sont pas garantis identiques entre sites.**
`fields.Transporteur` peut exister sur AUV et pas sur AMB, ou s'y appeler
autrement. Verifie avec `peripass_fields` avant de comparer.

**Le nom du transporteur dans Peripass n'est pas le nom maison.** Croise avec
l'annuaire du contexte d'equipe (`01_CONTEXTE/PORTEFEUILLE_ET_ZONES.md`) avant
de conclure sur le portefeuille : « Rhenus » designe quatre entites, « Posten
Bring » et « Bring » sont deux prestataires distincts, et VIR est l'ancien nom
de JP Home.

## Les personnes : a lire, pas a recopier

`peripass_list_certified_persons` porte des donnees **nominatives** : nom,
prenom, telephone, date de naissance, numero d'identification. Elle sert a
verifier qu'une habilitation existe et qu'elle est valide.

Ne recopie pas ces lignes dans une note du vault ni dans un livrable. La regle
du cerveau s'applique telle quelle : une personne se designe par son role, un
dossier par son numero. Meme prudence sur `currentHost` et sur les valeurs
d'exemple rendues par `peripass_fields`.

## Le resultat d'abord, la methode ensuite

**L'ordre de restitution n'est pas libre.** Une reponse Peripass se rend dans
cet ordre, toujours :

1. **Le resultat.** Le chiffre, le tableau, le graphique. En premier, sans
   preambule. Pas de « je vais regarder », pas d'annonce de ce que tu
   t'apprêtes a faire, pas de recit des etapes suivies pour y arriver.
2. **La methode, apres.** Une fois le resultat pose : les sites interroges, le
   `timestamp_field` retenu, la periode exacte, le champ sur lequel tu as
   compte, le nombre de lignes obtenues par site.
3. **Les risques que tu as identifies.** Ce qui peut rendre le chiffre faux :
   une troncature, un site muet, un champ personnalise vide, un statut annule
   inclus ou exclu, un decalage horaire absent, un creneau planifie compte
   comme une arrivee reelle. Nomme-les, ne les sous-entends pas.

Ce qui est interdit : commenter ta demarche **avant** d'avoir produit le
resultat. Si une piste s'est revelee fausse en route, ca se dit dans la partie
methode, pas en ouverture.

## Citer un chiffre Peripass

La regle de l'equipe s'applique sans changement : **un chiffre se cite avec son
perimetre et ses hypotheses.** Pas « 312 camions », mais « 312 visiteurs AMB
arrives du 01 au 07/08/2026, filtre `timestampFilterField=Arrived`, rendu non
tronque, AUV non interroge ». Chaque outil rappelle en tete la requete exacte
qu'il a jouee et le compte par site : recopie-les.

Et la regle qui ne bouge pas : **on n'invente jamais un chiffre.** Si l'API rend
une liste vide, la reponse est « aucune ligne sur ce perimetre », pas une
estimation.

## Ce qui n'est pas encore verifie

Le connecteur a passe 82 controles hors reseau et son handshake MCP, et la
chaine HTTP a ete verifiee contre les deux hotes de production. **Il n'a pas
encore ete confronte a un tenant avec une cle valide** : les volumes reels, les
noms des champs personnalises et le comportement du quota restent a constater.

Tant que ce n'est pas fait, dis a l'utilisateur ce que tu as reellement obtenu,
et ne presente pas un premier resultat comme une mesure etablie.
