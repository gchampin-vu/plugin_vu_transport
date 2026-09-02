---
description: Met en service le connecteur Trustpilot, etape par etape : ce qu'il faut avoir en main, ou saisir la cle et le secret, ce que le secret ouvre, comment verifier que ca marche.
---

Tu accompagnes un collegue de l'equipe Logistique & Transport dans la mise en
service du connecteur Trustpilot. Il n'est pas forcement technique, et il n'a
peut-etre jamais ouvert `/plugin`.

## Comment tu conduis ce guide

**Une etape a la fois.** Tu poses l'etape, tu attends qu'il te dise que c'est
fait, tu verifies avec un outil, puis tu passes a la suivante. Ne deroule jamais
les six etapes d'un bloc : il en sautera une, et le diagnostic sera faux.

**Tu verifies, tu ne crois pas sur parole.** Apres chaque etape qui se verifie,
tu appelles l'outil correspondant et tu dis ce qu'il rend. « C'est bon » d'un
utilisateur n'est pas une verification.

**Tu ne touches jamais au secret.** Ni la cle ni le secret d'API ne se collent
dans la conversation, ne se passent en parametre d'un outil - aucun ne
l'accepte -, ni ne s'ecrivent dans un fichier de la bibliotheque d'equipe. Ils
se saisissent dans les champs de configuration du plugin, ou ils restent hors du
contexte du modele et hors de la transcription. **S'il les colle quand meme dans
le chat : dis-lui de les faire regenerer cote Trustpilot, les anciens sont a
considerer comme divulgues.**

**Tu numerotes ce qu'il doit faire, et tu es litteral.** « Tape `/plugin` » et
non « va dans les parametres du plugin ».

---

## Etape 0 - Ce qu'il doit avoir en main

Avant de lancer quoi que ce soit, dis-lui, en une fois :

> **Dans le cas normal : rien.** La cle et le secret de l'application
> Trustpilot sont deja poses pour toute l'equipe dans le fichier de
> configuration commun (`08_ENGINE/04_mcp/00_config/trustpilot.shared.env`).
>
> Tu n'as quelque chose a saisir que dans trois cas : la bibliotheque
> SharePoint `Transport BtoC` n'est pas synchronisee sur ton poste, tu veux un
> acces nominatif, ou tu travailles sur un environnement de recette.
>
> Si tu es dans un de ces cas, il te faut **deux valeurs**, qui se lisent dans
> **Trustpilot Business > Integrations > API applications** : le « API Key »
> (aussi appele Client ID) et le « API Secret ». Elles s'obtiennent aupres de
> la personne qui administre le compte Trustpilot chez nous.

---

## Etape 1 - Etat des lieux

Appelle `trustpilot_setup_status`.

Cet outil ne dit pas seulement si la cle est la : il dit **ce que la
configuration actuelle ouvre et ce qu'elle ferme**. Lis ce bloc et
**resume-le-lui en une phrase** avant de continuer.

| Ce que rend l'outil | Ou il en est | La suite |
|---|---|---|
| `Cle d'API : AUCUNE` | rien n'est atteignable | etape 2 |
| cle presente, `Secret d'API : AUCUN` | le public marche, le prive est ferme | etape 3 — et lis-lui ce que ca ferme |
| cle et secret presents | tout est ouvert | etape 4 |

Si la ligne `Config d'equipe` dit `aucune`, signale-le **sans en faire un
blocage** : ca veut dire que la bibliotheque SharePoint n'est pas synchronisee
au meme endroit sur son poste. Il peut la synchroniser, ou pointer le fichier
avec la variable d'environnement `TRUSTPILOT_SHARED_ENV`. Mais il faudra alors
saisir cle et secret a la main.

Si la ligne `ATTENTION` signale des cles ignorees dans le fichier d'equipe,
**traite-la avant tout le reste**. Deux cas, et un seul est benin :

- une **faute de frappe** dans le nom d'une variable : elle est ignoree en
  silence partout ailleurs, c'est ici et seulement ici que ca se voit ;
- **`TRUSTPILOT_REFRESH_TOKEN`**, qui est refuse volontairement — voir l'etape 3.

---

## Etape 2 - Saisir la cle

Donne-lui ces quatre lignes, exactement :

> 1. tape **`/plugin`**
> 2. choisis **trustpilot** (marketplace `vu-transport`)
> 3. ouvre **Configure** / la configuration du plugin, et colle ta cle dans le
>    champ **« Cle d'API Trustpilot »**
> 4. **redemarre la session** Claude Code — le serveur ne relit sa
>    configuration qu'au demarrage

Trois precisions a donner dans la foulee, elles evitent chacune un
aller-retour :

- **Les champs sont marques « sensible » :** ce qu'il y tape n'apparait ni dans
  la conversation, ni dans la transcription. C'est pour ca que la saisie se fait
  la et pas dans le chat.
- **Le champ « Refresh token » se laisse VIDE.** C'est un piege, pas une
  option — voir l'etape 3.
- **L'etape 4 n'est pas optionnelle.** Sans redemarrage,
  `trustpilot_setup_status` rendra encore `AUCUNE`, et il croira s'etre trompe
  de champ.

Termine ton tour ici. Quand il revient, reprends a l'etape 1.

---

## Etape 3 - Le secret : ce qu'il ouvre, et le piege a cote

C'est l'etape ou tu apportes quelque chose qu'il ne devinera pas seul.

**Dis-lui ce que le secret ouvre, concretement :**

> Sans le secret, le connecteur lit les avis, les notes et les TrustScore — de
> quoi suivre une note. Mais il passe par le chemin **public**, qui :
>
> - **ne filtre pas les dates cote serveur.** Une question sur mars dernier fait
>   remonter les avis du plus recent au plus ancien jusqu'a mars : c'est long, et
>   sur une longue periode l'API refuse d'aller plus loin.
> - **ne rend pas le `referenceId`.** C'est notre **numero de commande**. Sans
>   lui, on ne peut ni relier un avis a une commande, ni retrouver l'avis d'une
>   commande, **ni donc savoir quel transporteur a livre le client qui se
>   plaint**. Pour l'equipe Transport, c'est la moitie de l'interet du
>   connecteur.
>
> Le secret est dans **Trustpilot Business > Integrations > API applications**,
> a cote de la cle. Meme champ dans `/plugin` : « Secret d'API Trustpilot ».

**Et previens-le sur le refresh token, avant qu'il ne remplisse le champ :**

> Laisse **« Refresh token OAuth » vide**. Avec la cle et le secret, le
> connecteur s'authentifie par `client_credentials` : rien ne tourne, rien
> n'expire au bout de trente jours.
>
> Trustpilot fait **tourner** un refresh token a chaque echange : le nouveau est
> range en local, l'ancien peut etre invalide cote Trustpilot. Un refresh token
> partage entre deux postes se casse donc mutuellement, et rend une erreur
> `invalid_grant` que personne ne rattache a la cause.

---

## Etape 4 - Ranger sur la machine

Appelle `trustpilot_save_key`. Il ne prend aucun argument : il range ce qui est
deja saisi, sans qu'aucun secret passe par la conversation.

Rapporte le chemin du magasin et les droits appliques, et dis en une phrase a
quoi ca sert : **les valeurs survivent a une reinstallation du plugin**, et une
installation directe (hors plugin) les retrouve au meme endroit.

C'est utile surtout a celui qui n'a pas la bibliotheque synchronisee : ca rend
son poste autonome.

---

## Etape 5 - Verifier la connexion

Appelle `trustpilot_doctor`. Il verifie **les deux regimes separement**, et
c'est le point important : un poste peut avoir un public qui repond et un prive
qui echoue. Attendu :

- `PUBLIC  find www.vente-unique.com : OK` avec un identifiant et un TrustScore ;
- `PUBLIC  avis : OK` ;
- `PRIVE   avis : OK` **si le secret est renseigne**, sinon la ligne dit
  explicitement qu'elle n'a pas ete tentee — ce n'est pas une erreur.

Si ce n'est pas le cas, lis le code et applique ce tableau. **Ne conclus jamais
que le connecteur fonctionne sur autre chose qu'un OK.**

| Symptome | Ce que c'est vraiment | Ce qu'il doit faire |
|---|---|---|
| `401` sur le public | la cle est saisie mais **rejetee** : erronee, mal collee (espace en trop), ou revoquee | reprendre l'etape 2 avec la bonne cle |
| `401` sur le prive seulement | le jeton OAuth a ete refuse ou revoque | relancer une fois — le connecteur jette le jeton en cache et en redemande un ; si ca persiste, verifier que cle et secret viennent de la **meme** application |
| erreur sur la demande de jeton, `invalid_grant` | c'est **le refresh token** | vider le champ « Refresh token » dans `/plugin` pour basculer sur `client_credentials` |
| erreur de jeton avec `grant password` | grant **deprecie** par Trustpilot, inoperant avec un MFA | vider `TRUSTPILOT_USERNAME` / `TRUSTPILOT_PASSWORD` |
| `403` sur un domaine, pas sur les autres | l'application Trustpilot n'a pas de droit sur **cette** business unit | appelle `trustpilot_business_units` : il dit lesquels repondent. C'est un droit a demander cote Trustpilot |
| `404` sur `find` | le domaine est mal orthographie | Trustpilot enregistre la forme **avec `www.`** chez nous |
| `429` | quota atteint — et il est compte **par application**, donc partage par toute l'equipe | quelqu'un synchronise peut-etre en meme temps : reessayer plus tard |
| `TRUSTPILOT_API_KEY manquant` alors qu'il a saisi la cle | session non redemarree, ou interpreteur Python empaquete qui ne voit pas le fichier | redemarrer la session ; `trustpilot_setup_status` dit quel emplacement est lu |

---

## Etape 6 - Montrer que ca marche

Deux appels courts, dans cet ordre :

1. `trustpilot_business_units` avec `domaine="fr"` — un identifiant, un
   TrustScore et un nombre d'avis doivent sortir. Les identifiants sont ranges
   en local au passage : ils ne seront plus redemandes.
2. `trustpilot_summary` avec `domaine="fr"`, `group_by="mois"` — la note des
   trois derniers mois.

Puis, **si le secret est en place**, montre-lui ce que ca debloque :
`trustpilot_themes` avec `domaine="fr"` — la repartition thematique des
plaintes, avec la note de chaque theme. C'est ce qu'il viendra chercher.

---

## Etape 7 - Clore, en quatre lignes

Pas plus de quatre lignes :

1. ce qui est en place, et si le chemin prive est ouvert ou non ;
2. trois exemples de ce qu'il peut demander maintenant, en langage normal :
   « la note Trustpilot de l'Italie ce trimestre », « de quoi se plaignent les
   clients allemands », « la commande 1234567 a-t-elle laisse un avis » ;
3. le rappel qui compte : le connecteur est en **lecture seule** — il ne
   repond a aucun avis, ne pose aucun tag, n'envoie aucune invitation.
   Repondre a un client publiquement reste un geste humain ;
4. et le reflexe a garder : **un avis n'accuse pas un transporteur.** Le nom
   cite dans un verbatim est un indice ; la chaine fiable est
   `referenceId` -> commande -> Reflex ou Shiptify -> transporteur reel.
