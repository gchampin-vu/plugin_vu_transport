---
name: trustpilot
description: Interroger les avis clients Trustpilot des dix-huit domaines du groupe (Vente-unique, Kauf-unique, Habitat) en lecture seule - note et TrustScore, avis a une etoile, themes de plainte, verbatims a lire, transporteurs cites, reponses de l'entreprise. Active des qu'une question porte sur la note Trustpilot d'un pays ou d'une enseigne, sur ce que disent ou reprochent les clients, sur la satisfaction ou la qualite de service percue, sur un avis relie a une commande, ou des qu'il faut extraire des avis en CSV. Declenche aussi sur "trustpilot", "avis client", "avis", "note", "trustscore", "etoiles", "1 etoile", "verbatim", "commentaire client", "satisfaction", "reclamation", "ce que disent les clients", "de quoi se plaignent-ils", "avis de la commande", "export des avis".
---

# Trustpilot : ce que le client dit lui-meme de la livraison

Servi par le serveur MCP `trustpilot` du meme plugin, **en lecture seule** : ni
reponse a un avis, ni tag, ni invitation. Ces gestes engagent la marque devant
un client sur une page publique - ils ne sont pas exposes du tout.

Trustpilot n'est pas un outil de transport. C'est la **seule source ou le client
raconte lui-meme comment la livraison s'est passee**, dans sa langue, avec une
note. Le Scorecard mesure ce que le transporteur declare ; Trustpilot mesure ce
que le client a vecu. **L'ecart entre les deux est le sujet.**

| | |
|---|---|
| Perimetre | 18 domaines : `vente-unique.com` + 14 pays, `kauf-unique.de` et `.at`, `habitat.fr`, `habitat-design.com` |
| Langues | fr, de, nl, it, es, pt, pl, da, sv, nb/no, en |
| Historique | depuis le `TRUSTPILOT_START_DATE` d'equipe (2023-01-01) |
| Regimes | chemin **public** (avis, notes) et chemin **prive** (dates cote serveur, `referenceId`) |

## La chose a ne jamais faire : accuser un transporteur sur un verbatim

`trustpilot_transporteurs` rend les transporteurs **cites par les clients**.
C'est un **indice**, pas une mesure :

- le client nomme qui il a vu a sa porte, pas qui a fait le trajet - un
  sous-traitant, un dernier kilometre affrete, une agence VUD sont invisibles ;
- il ne nomme personne quand tout va bien, donc la detection est **biaisee vers
  le negatif** ;
- la detection est **lexicale** : elle ne voit que les avis ou le nom est ecrit,
  donc elle sous-compte toujours.

**La chaine fiable est l'autre** : `referenceId` -> notre numero de commande ->
Reflex ou Shiptify -> le transporteur qui a reellement livre. Si la question est
« quel transporteur cause ces avis », passe par `trustpilot_find_review` et la
commande, et dis-le. Un nom cite dans un verbatim ne part pas dans une revue
transporteur comme un chiffre.

## Choisir le bon outil — c'est la que ca se joue

| La question | L'outil |
|---|---|
| « quelle est la note de l'Italie ce mois-ci », « combien d'avis a 1 etoile », « par pays, par mois » | **`trustpilot_summary`** — agrege **cote serveur** |
| « la note Trustpilot affichee », « la repartition par etoile » | **`trustpilot_profile`** — le TrustScore publie |
| « de quoi se plaignent les clients espagnols » | **`trustpilot_themes`** d'abord, **`trustpilot_verbatims`** ensuite |
| « montre-moi les avis a 1 etoile de la semaine » | **`trustpilot_list_reviews`** |
| « la commande 1234567 a-t-elle laisse un avis » | **`trustpilot_find_review`** (chemin prive) |
| « est-ce que les clients citent AIT » | **`trustpilot_transporteurs`**, avec les trois avertissements |
| « quels domaines repondent », « l'identifiant de la BU » | **`trustpilot_business_units`** |
| douze mois, les dix-huit domaines, un croisement | **`trustpilot_sync`** puis **`trustpilot_sql`** |
| un fichier, un export, un CSV | **`trustpilot_export_csv`** ou **`trustpilot_export_sql`** — **sur demande explicite** |
| rien ne repond | **`trustpilot_doctor`**, puis `trustpilot_setup_status` |

**Ne compte jamais a la main.** Faire remonter des milliers d'avis pour les
compter dans la conversation est l'erreur la plus couteuse : `summary` pagine,
agrege et ne rend que le resultat - nombre d'avis, note moyenne, part de 1-2
etoiles, part de 4-5, taux de reponse, delai de reponse median.

**Themes avant verbatims, jamais l'inverse.** `themes` dit **ou regarder** ;
`verbatims` sert a **lire**. Ouvrir directement les verbatims, c'est choisir un
echantillon sans savoir de quoi il est representatif.

## TrustScore et note moyenne ne sont pas le meme chiffre

- **`trustpilot_profile`** rend le **TrustScore** : ce que Trustpilot calcule et
  affiche - pondere, glissant, sur la vie entiere de la business unit.
- **`trustpilot_summary`** rend la **note moyenne des avis de la periode**.

Les deux ne coincident pas, et c'est normal. **Ne les melange pas dans un meme
tableau** sans dire lequel est lequel : c'est ainsi qu'une note « qui a baisse »
apparait dans un compte rendu alors que rien n'a bouge.

## Le chemin public et le chemin prive

| | Public (cle seule) | Prive (cle **et** secret) |
|---|---|---|
| Avis, notes, TrustScore | oui | oui |
| Filtre de date **cote serveur** | non | oui |
| `referenceId` (notre numero de commande) | **non** | oui |
| Retrouver l'avis d'une commande | **impossible** | oui |

Sans le secret, une question sur une periode fait lire du plus recent au plus
ancien jusqu'a la borne : c'est long, et l'API publique ne remonte pas
indefiniment. **Si un outil dit que le secret manque, recopie-le** - ce n'est pas
un incident, c'est une capacite fermee, et il n'y a **aucun contournement**
public pour le `referenceId`.

## Les pieges qui rendent un chiffre faux sans lever d'erreur

**1. Le nombre d'avis n'est pas un volume.** Un pays a plus d'avis parce qu'il
vend plus **et** parce qu'il repond plus aux invitations. Comparer deux pays sur
le nombre d'avis ne compare rien. **La note se compare, l'effectif se cite a
cote.**

**2. Les petits effectifs.** Sur LU, IE, DK, un mois peut ne compter que quelques
avis. Une note qui passe de 4,2 a 3,6 sur huit avis **n'est pas un signal**. Cite
toujours l'effectif avec la note, et dis-le quand il est trop faible pour
conclure.

**3. Les themes sont lexicaux.** C'est une detection de **mots**, dans les onze
langues, sur treize themes definis dans `lexique.json`. Elle **ignore la
negation** - « aucun retard » compte dans le theme delai. Elle oriente la
lecture, elle ne produit pas un chiffre a citer seul. L'outil rend son **taux de
non-classes** : lis-le avant de conclure. Une case vide veut dire « aucun mot du
lexique », pas « rien a signaler ».

**4. La casse n'est pas toujours le transport.** Sur le Royaume-Uni, 77 % des
scans de casse sont constates **a reception**, donc avant le transporteur. Un
theme « produit abime » en hausse **ouvre une question**, il n'accuse personne.

**5. `habitat-design.com` sert six locales.** Ne lui attribue aucun pays unique :
le pays se lit dans la langue de l'avis et dans `consumer.displayLocation`.
« Habitat » est ambigu - `habitat.fr` pour la France, `habitat-design.com` pour
l'international. Demande lequel.

**6. Le norvegien est code `nb` ou `no` selon l'avis.** Filtrer sur une seule des
deux valeurs perd la moitie du pays. Le connecteur traite les deux ; ne
reintroduis pas le filtre a la main.

**7. `createdAt` n'est pas `experiencedAt`.** La premiere est la date de
publication, la seconde la date vecue. Les filtres portent sur `createdAt`.

**8. Le quota est compte par application**, donc partage par toute l'equipe. Un
`429` ne veut pas dire que la cle est mauvaise : quelqu'un synchronise peut-etre
au meme moment.

## Comment tu conduis

**Borne la periode.** Sans date, le connecteur applique quatre-vingt-dix jours.
Pour un fichier ou une revue, demande la periode plutot que de la supposer.

**Resous le domaine, ne le devine pas.** « Les avis allemands » est
`kauf-unique.de`, pas `.at` : deux domaines, meme langue. Le connecteur resout
les alias lui-meme (`domaine='allemagne'`), mais **verifie ce qu'il a retenu**
dans l'en-tete qu'il rend. En cas de doute : `trustpilot_lexique('domaines')`.

**Filtre avec les mots du lexique.** `theme` et `transporteur` ne voient que ce
que le lexique connait. `trustpilot_lexique` avant d'inventer un terme.

**Passe par le cache des que ca depasse quelques milliers d'avis.** Le direct est
plafonne a `TRUSTPILOT_MAX_PAGES` (6 000 avis **par domaine**) : sur
`vente-unique.com`, ca se depasse en quelques mois. `trustpilot_sync` une fois,
puis `trustpilot_sql` autant qu'on veut, sans reconsommer le quota. Le classement
thematique est calcule **a la synchro** : si le lexique change, il faut
resynchroniser.

**Un rendu tronque se dit en premier, pas en dernier.** Le connecteur nomme les
domaines qu'il n'a pas lus jusqu'au bout : recopie cette liste. Un total
incomplet dont on ne dit rien est un chiffre faux qui part dans un mail.

**Un fichier seulement si on t'en a demande un.** `export_csv` et `export_sql`
ecrivent sur le disque. Si la question est « combien », la reponse est `summary`.

**Les donnees personnelles restent dedans.** Le `referralEmail` - la boite du
client invite - est retire par defaut, et `find_review` ne restitue jamais une
adresse. `inclure_donnees_personnelles=True` se justifie et se signale.

## Ce que tu rends

1. **La note avec son effectif et sa periode.** « 3,6 sur 41 avis en aout »,
   jamais « 3,6 ».
2. **Quel chiffre c'est** : note moyenne de la periode, ou TrustScore publie.
3. **Ce qui manque** : rendu tronque, chemin prive ferme, taux de non-classes
   eleve, effectif trop faible. Les outils le disent dans leur en-tete - ne le
   retire pas de ta reponse.
4. **La langue d'origine des verbatims.** Tu peux traduire pour expliquer, en
   donnant le texte original a cote ; tu ne remplaces pas le verbatim par ta
   reformulation.
5. **Le mode d'echantillonnage des verbatims** (`pires`, `recents`, `anciens`,
   `repartis`) : il change le sens de ce qu'on lit.

## Ce que tu ne fais pas

- **Tu ne reponds a aucun avis, tu ne poses aucun tag, tu n'envoies aucune
  invitation.** Ce n'est pas une restriction de prudence : les chemins d'ecriture
  ne sont pas exposes. Repondre a un client est un geste humain.
- **Tu n'ecris jamais dans la bibliotheque d'equipe.** Les exports vont dans le
  dossier local du connecteur. Un CSV de verbatims depose dans un dossier
  synchronise part chez les vingt-six, et la base de connaissance ne porte pas de
  donnees.
- **Tu ne nommes pas une personne.** Ni un livreur cite dans un avis, ni un
  collegue du service client. La regle de la bibliotheque vaut ici aussi : aucune
  appreciation sur une personne nommee.
- **Tu n'inventes ni une note ni une date.** Si un domaine ne repond pas, tu le
  dis. Une note de memoire n'existe pas.
- **Tu ne devines pas un sentiment.** La note en etoiles est une donnee ; un
  sentiment devine sur un texte n'en est pas une. Il n'y a volontairement aucune
  analyse de sentiment dans ce connecteur.
