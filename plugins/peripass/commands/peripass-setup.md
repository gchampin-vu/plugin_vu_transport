---
description: Met en service le connecteur Peripass, etape par etape : ce que l'equipe fournit deja, ce qu'il reste a saisir, ou le saisir, comment verifier que ca marche.
---

Tu accompagnes un collegue de l'equipe Logistique & Transport dans la mise en
service du connecteur Peripass. Il n'est pas forcement technique, et il n'a
peut-etre jamais ouvert `/plugin`.

## Comment tu conduis ce guide

**Une etape a la fois.** Tu poses l'etape, tu attends qu'il te dise que c'est
fait, tu verifies avec un outil, puis tu passes a la suivante. Ne deroule
jamais les six etapes d'un bloc : il en sautera une, et le diagnostic sera
faux.

**Tu verifies, tu ne crois pas sur parole.** Apres chaque etape qui se verifie,
tu appelles l'outil correspondant et tu dis ce qu'il rend. « C'est bon » d'un
utilisateur n'est pas une verification.

**Tu ne touches jamais au secret.** Une cle d'API ne se colle pas dans la
conversation, ne se passe en parametre d'aucun outil, et ne s'ecrit dans aucun
fichier de la bibliotheque d'equipe autre que celui prevu pour ca. Elle se
saisit dans le champ de configuration du plugin, ou elle reste hors du contexte
du modele et hors de la transcription. S'il la colle quand meme dans le chat :
dis-lui de la faire regenerer cote Peripass, l'ancienne est a considerer comme
divulguee.

**Tu numerotes ce qu'il doit faire, et tu es litteral.** « Tape `/plugin` » et
non « va dans les parametres du plugin ». Un clic decrit approximativement est
un aller-retour de plus.

**La particularite de ce connecteur : Peripass a une base par site.** Il faut
donc **deux cles**, une pour AUV (Moulins) et une pour AMB (Amblainville). Une
seule cle donne un connecteur qui repond et qui ne couvre que la moitie du
perimetre - c'est exactement ce qui produit un chiffre faux sans prevenir. Tu
ne conclus pas la mise en service tant que les deux sites ne repondent pas.

---

## Etape 0 - Ce qu'il doit avoir en main

Avant de lancer quoi que ce soit, dis-lui, en une fois :

> **Peut-etre rien.** Le reglage du connecteur - les deux sites, leurs racines
> d'API, leurs tenants, les plafonds - est deja pose une fois pour toute
> l'equipe dans `08_ENGINE/04_mcp/00_config/peripass.shared.env`. Selon la
> decision d'equipe, les cles d'API y sont aussi.
>
> S'il te manque quelque chose, ce sera **une cle d'API par site** : une pour
> AUV, une pour AMB, differentes. Elles se demandent a Peripass, ou a la
> personne qui administre le compte Peripass chez nous. Le plugin n'en fournit
> aucune.

Ne lui fais pas chercher ses cles avant l'etape 1 : elles ne sont peut-etre pas
necessaires.

---

## Etape 1 - Etat des lieux

Appelle `peripass_setup_status`.

Lis la sortie et **dis-lui en une phrase ou il en est** avant de continuer :

| Ce que rend l'outil | Ou il en est | La suite |
|---|---|---|
| `RIEN a saisir pour AUV, AMB` ou aucun site en `A FAIRE` | l'equipe fournit tout | etape 4 |
| un ou deux sites en `A FAIRE` | il manque une cle | etape 2 |
| cles actives, `Cle rangee : non` | saisies, pas rangees | etape 3 |

Deux lignes de cette sortie meritent d'etre lues avant tout le reste.

**`Config d'equipe : aucune`** - la bibliotheque SharePoint « Transport BtoC »
n'est pas atteignable depuis son poste. Ce n'est pas un blocage, mais il perd
tout ce que l'equipe a deja regle et il devra tout saisir a la main. Deux
sorties, dans cet ordre : synchroniser la bibliotheque, ou renseigner le champ
**« Chemin du fichier de configuration d'equipe »** dans `/plugin` avec le
chemin complet de `peripass.shared.env`. Traite ca **avant** de lui faire
saisir quoi que ce soit : dans une bonne partie des cas, il n'aura plus rien a
faire ensuite.

**Une ligne `ATTENTION ... IGNOREES`** - quelqu'un a pose dans le fichier
partage une variable que le serveur ne reprend pas. C'est presque toujours une
faute de frappe, et elle ne se voit nulle part ailleurs. Si c'est une cle mal
orthographiee, elle est a considerer comme divulguee et a faire revoquer :
elle est en clair sur le drive de l'equipe sans que rien ne la lise. Signale-le
a Guillaume, ne corrige pas le fichier partage de ta propre initiative - une
ecriture la-bas vaut pour les vingt-six postes.

---

## Etape 2 - Saisir les cles manquantes

C'est la seule etape que tu ne peux pas faire a sa place. **Nomme les sites qui
manquent**, et ne lui fais pas ressaisir ceux que l'equipe couvre deja.

Donne-lui ces quatre lignes, exactement :

> 1. tape **`/plugin`**
> 2. choisis **peripass** (marketplace `vu-transport`)
> 3. ouvre **Configure** / la configuration du plugin, et colle ta cle dans le
>    champ **« Cle d'API Peripass - AUV »** et/ou **« ... - AMB »**
> 4. **redemarre la session** Claude Code - le serveur ne relit sa configuration
>    qu'au demarrage

Trois precisions a donner dans la foulee, elles evitent chacune un
aller-retour :

- **Les champs sont marques « sensible » :** ce qu'il y tape n'apparait ni dans
  la conversation, ni dans la transcription. C'est pour ca que la saisie se
  fait la et pas dans le chat.
- **Les autres champs restent vides.** Racines d'API, tenants, liste des sites :
  l'equipe les a deja poses. Les remplir ici ne fait que figer sur ce poste une
  valeur qui ne suivra plus les corrections de l'equipe.
- **L'etape 4 n'est pas optionnelle.** Sans redemarrage, `peripass_setup_status`
  rendra encore `AUCUNE`, et il croira s'etre trompe de champ.

Termine ton tour ici. Quand il revient, reprends a l'etape 1.

---

## Etape 3 - Ranger les cles sur la machine

Appelle `peripass_save_key`. Il ne prend aucun argument : il range les cles
deja saisies, sans qu'elles passent par la conversation.

Rapporte le chemin du magasin et les droits appliques, et dis en une phrase a
quoi ca sert : **les cles survivent a une reinstallation du plugin**, et une
installation directe (hors plugin) les retrouve au meme endroit.

Deux cas ou cette etape se saute :

- les cles viennent de la configuration d'equipe et il n'a pas besoin d'etre
  autonome de la bibliotheque synchronisee - `setup_status` le dit lui-meme ;
- il travaille sur un poste partage.

---

## Etape 4 - Verifier la connexion

Appelle `peripass_doctor`. Attendu : `OK` sur `GET /profiles` **et** sur
`GET /visitors`, **pour chaque site**.

**Si un site rend `HTTP 403`, ne conclus pas trop vite.** Mesure le 2026-08-28
contre les deux tenants : Peripass rend un 403 a corps vide aussi bien pour une
cle absente que pour une cle fausse ou sans droits - sa documentation annonce
pourtant un 401 pour l'en-tete manquante. Regarde la ligne `Origine` du site :

| Ce que dit `Origine` | Ce que c'est vraiment | Ce qu'il doit faire |
|---|---|---|
| `(aucune)` | la saisie n'est pas arrivee jusqu'au serveur | retour a l'etape 2, et verifier qu'il a redemarre la session |
| `config d'equipe (...)` | la cle partagee est erronee, revoquee ou sans droits | ca concerne **tout le monde** : signale-le a Guillaume, la correction se fait dans `peripass.shared.env`, pas sur ce poste |
| `configuration du plugin` ou `fichier ...` | sa cle a lui est erronee, revoquee, ou mal collee (espace en trop) | reprendre l'etape 2 avec la bonne cle |
| `404` sur les deux chemins | la racine d'API est fausse | reglage d'equipe : `PERIPASS_<SITE>_BASE_URL` dans `peripass.shared.env` |
| delai depasse | reseau ou VPN | reessayer, puis voir avec l'IT |

**Ne conclus jamais que le connecteur fonctionne sur autre chose qu'un `OK`.**

---

## Etape 5 - Montrer que ca marche

Deux appels de demonstration, courts :

1. `peripass_referential` avec `name="profiles"` - les profils de visiteur des
   deux sites doivent sortir, avec la colonne `site`.
2. `peripass_fields` avec `collection="visitors"` et `sample=100` - il rend les
   champs personnalises reellement presents sur chaque site.

Le second compte autant que le premier : **c'est la premiere fois que l'on voit
si AUV et AMB portent les memes champs**. S'ils different, dis-le tout de suite :
c'est ce qui rendra une comparaison entre sites fausse plus tard.

---

## Etape 6 - Clore, en quatre lignes

Pas plus de quatre lignes :

1. quelles cles sont en place et d'ou elles viennent - equipe ou poste - et si
   le fichier de configuration d'equipe est lu ;
2. deux exemples de ce qu'il peut demander maintenant, en langage normal :
   « les camions arrives a AMB cette semaine », « les creneaux non honores sur
   AUV en aout », « exporte les visiteurs du mois en CSV » ;
3. le connecteur est en **lecture seule** - il ne cree rien, ne bloque rien et
   ne fait avancer aucun statut dans Peripass ;
4. il n'a **pas encore ete confronte a un tenant avec une cle valide**. Les
   premiers resultats sont a regarder comme une recette, pas comme une mesure
   etablie. S'ils sont coherents, dis-le, et propose de le noter dans le README
   du plugin.
