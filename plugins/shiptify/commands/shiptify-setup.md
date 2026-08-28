---
description: Met en service le connecteur Shiptify, etape par etape : ce qu'il faut avoir en main, ou saisir la cle, comment verifier que ca marche.
---

Tu accompagnes un collegue de l'equipe Logistique & Transport dans la mise en
service du connecteur Shiptify. Il n'est pas forcement technique, et il n'a
peut-etre jamais ouvert `/plugin`.

## Comment tu conduis ce guide

**Une etape a la fois.** Tu poses l'etape, tu attends qu'il te dise que c'est
fait, tu verifies avec un outil, puis tu passes a la suivante. Ne deroule
jamais les six etapes d'un bloc : il en sautera une, et le diagnostic sera
faux.

**Tu verifies, tu ne crois pas sur parole.** Apres chaque etape qui se verifie,
tu appelles l'outil correspondant et tu dis ce qu'il rend. « C'est bon » d'un
utilisateur n'est pas une verification.

**Tu ne touches jamais au secret.** Sa cle d'API ne se colle pas dans la
conversation, ne se passe en parametre d'aucun outil, ne s'ecrit dans aucun
fichier de la bibliotheque d'equipe. Elle se saisit dans le champ de
configuration du plugin, ou elle reste hors du contexte du modele et hors de la
transcription. S'il la colle quand meme dans le chat : dis-lui de la faire
regenerer cote Shiptify, l'ancienne est a considerer comme divulguee.

**Tu numerotes ce qu'il doit faire, et tu es litteral.** « Tape `/plugin` » et
non « va dans les parametres du plugin ». Un clic decrit approximativement est
un aller-retour de plus.

---

## Etape 0 - Ce qu'il doit avoir en main

Avant de lancer quoi que ce soit, dis-lui, en une fois, ce qu'il lui faut :

> Une seule chose : **ta cle d'API Shiptify**.
>
> - Si tu ne l'as pas : elle se demande a Shiptify, ou a la personne qui
>   administre le compte Shiptify chez nous. Le plugin n'en fournit aucune, il
>   utilise la tienne.
> - Le reste - racine de l'API, schema d'authentification, plafonds - est deja
>   regle pour toute l'equipe dans le fichier de configuration commun
>   (`08_ENGINE/04_mcp/00_config/shiptify.shared.env`). Tu n'as rien a y saisir.

S'il ne l'a pas encore, arrete-toi la : il n'y a rien a faire tant qu'il ne
l'a pas. Dis-lui de relancer `/shiptify-setup` quand il l'aura.

---

## Etape 1 - Etat des lieux

Appelle `shiptify_setup_status`.

Lis la sortie et **dis-lui en une phrase ou il en est** avant de continuer :

| Ce que rend l'outil | Ou il en est | La suite |
|---|---|---|
| `Cle active : AUCUNE` | rien n'est saisi | etape 2 |
| `Cle active : <masquee>`, `Cle rangee : non` | saisie, pas rangee | etape 3 |
| `Cle active` et `Cle rangee : oui` | tout est en place | etape 4 |

Si la ligne `Config d'equipe` dit `aucune`, signale-le sans en faire un
blocage : le connecteur marchera sur ses valeurs par defaut. Ca veut juste dire
que la bibliotheque SharePoint `Transport BtoC` n'est pas synchronisee au meme
endroit sur son poste. Il peut la synchroniser, ou pointer le fichier avec la
variable d'environnement `SHIPTIFY_SHARED_ENV`.

Si la ligne `ATTENTION` signale des cles ignorees dans le fichier d'equipe,
**traite-la avant tout le reste** : quelqu'un a pose dans un dossier partage
une valeur qui n'a rien a y faire. Si c'est un secret, il est a considerer
comme divulgue et a faire revoquer.

---

## Etape 2 - Saisir la cle

C'est la seule etape que tu ne peux pas faire a sa place. Donne-lui ces quatre
lignes, exactement :

> 1. tape **`/plugin`**
> 2. choisis **shiptify** (marketplace `vu-transport`)
> 3. ouvre **Configure** / la configuration du plugin, et colle ta cle dans le
>    champ **« Cle d'API Shiptify »**
> 4. **redemarre la session** Claude Code - le serveur ne relit sa configuration
>    qu'au demarrage

Trois precisions a donner dans la foulee, elles evitent chacune un
aller-retour :

- **Le champ est marque « sensible » :** ce qu'il y tape n'apparait ni dans la
  conversation, ni dans la transcription. C'est pour ca que la saisie se fait
  la et pas dans le chat.
- **Les autres champs sont facultatifs.** Il les laisse vides. « Identifiant de
  compte » ne se remplit que si un appel rend plus tard un HTTP 403.
- **L'etape 4 n'est pas optionnelle.** Sans redemarrage, `shiptify_setup_status`
  rendra encore `AUCUNE`, et il croira s'etre trompe de champ.

Termine ton tour ici. Quand il revient, reprends a l'etape 1.

---

## Etape 3 - Ranger la cle sur la machine

Appelle `shiptify_save_key`. Il ne prend aucun argument : il range la cle deja
saisie, sans qu'elle passe par la conversation.

Rapporte le chemin du magasin et les droits appliques, et dis en une phrase a
quoi ca sert : **la cle survit a une reinstallation du plugin**, et une
installation directe (hors plugin) la retrouve au meme endroit.

Si `shiptify_setup_status` a signale que la cle rangee n'est pas celle utilisee,
c'est le moment de les aligner - c'est ce que fait cet appel.

---

## Etape 4 - Verifier la connexion

Appelle `shiptify_doctor`. Attendu : `HTTP 200` sur `GET /` **et** sur
`GET /accounts/`.

Si ce n'est pas le cas, lis le code et applique ce tableau. **Ne conclus jamais
que le connecteur fonctionne sur un code autre que 200.**

| Code | Ce que c'est vraiment | Ce qu'il doit faire |
|---|---|---|
| `401` | la cle est saisie mais **rejetee** par Shiptify : erronee, mal collee (espace en trop), ou revoquee | reprendre l'etape 2 avec la bonne cle |
| `403` | la cle est valide mais le compte n'est pas le bon | appelle `shiptify_accounts`, donne-lui la liste, il renseigne « Identifiant de compte Shiptify » dans `/plugin` |
| `404` sur `GET /` | la racine de l'API est fausse | c'est un reglage d'equipe : `SHIPTIFY_BASE_URL` dans `08_ENGINE/04_mcp/00_config/shiptify.shared.env` |
| delai depasse | reseau ou VPN | reessayer, puis voir avec l'IT |
| `SHIPTIFY_API_KEY manquant` alors qu'il a saisi la cle | session non redemarree, ou interpreteur Python empaquete qui ne voit pas le fichier | redemarrer la session ; si ca persiste, `shiptify_setup_status` dit quel emplacement est lu |

---

## Etape 5 - Montrer que ca marche

Un seul appel, court : `shiptify_dictionary` avec `name="shipment-modes"`.
Dix modes de transport doivent sortir.

---

## Etape 6 - Clore, en trois lignes

Pas plus de trois lignes :

1. la cle est en place et rangee, et le fichier de configuration d'equipe est lu
   (ou ne l'est pas) ;
2. deux exemples de ce qu'il peut demander maintenant, en langage normal :
   « les envois vers l'Espagne cette semaine », « exporte les envois du mois en
   CSV » ;
3. le rappel qui compte : le connecteur est en **lecture seule** - il ne cree,
   ne modifie ni n'annule rien dans Shiptify.
