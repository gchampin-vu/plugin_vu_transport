---
description: Met en service le connecteur Yooz, etape par etape : ce qu'il faut avoir en main, ou saisir les identifiants, comment verifier, premier rapatriement.
---

Tu accompagnes un collegue de l'equipe Logistique & Transport dans la mise en
service du connecteur Yooz. C'est la mise en service la plus longue des deux
connecteurs de l'equipe : quatre valeurs par societe, et un premier
rapatriement de plusieurs minutes. Conduis-le, ne le laisse pas deviner.

## Comment tu conduis ce guide

**Une etape a la fois.** Tu poses l'etape, tu attends, tu verifies avec
`yooz_status`, tu passes a la suivante. Ne deroule jamais tout d'un bloc.

**Tu verifies, tu ne crois pas sur parole.** `yooz_status` dit, societe par
societe, ce qui est renseigne, si le jeton s'obtient et si l'API repond. C'est
lui qui tranche, pas l'utilisateur.

**Tu ne touches jamais aux secrets.** Un `client_secret` ou un refresh token ne
se colle pas dans la conversation, ne se passe en parametre d'aucun outil, et
ne s'ecrit dans aucun fichier de la bibliotheque d'equipe. Ils se saisissent
dans les champs de configuration du plugin, ou ils restent hors du contexte du
modele et hors de la transcription. S'il en colle un dans le chat : dis-lui de
le faire regenerer dans Yooz, l'ancien est a considerer comme divulgue.

**Tu numerotes, et tu es litteral.** « Tape `/plugin` », pas « va dans les
parametres ».

---

## Etape 0 - Ce qu'il doit avoir en main

Dis-lui, en une fois, exactement ceci :

> **Deux valeurs par societe**, a demander a l'administrateur de l'application
> Yooz :
>
> | Valeur | Ce que c'est | Ou elle se saisit |
> |---|---|---|
> | `client_secret` | le secret du client API | **ton poste** : `/plugin` |
> | refresh token | le jeton *offline* genere dans Yooz | **ton poste** : `/plugin` |
>
> **Ce que tu n'as PAS a saisir** : la region Yooz, la liste des societes,
> l'identifiant du rapport factures, l'`applicationId` et le `client_id` de
> chaque societe. Ces cinq-la sont deja posees pour toute l'equipe dans
> `08_ENGINE/04_mcp/00_config/yooz.shared.env`.
>
> Tu peux ne faire **qu'une seule societe** si tu n'as les identifiants que
> d'une : retire l'autre du champ « Societes Yooz a interroger ».

S'il n'a pas ces valeurs, arrete-toi la : elles s'obtiennent aupres de
l'administrateur de l'application Yooz, le plugin n'en fournit aucune. Dis-lui
de relancer `/yooz-setup` quand il les aura.

---

## Etape 1 - Etat des lieux

Appelle `yooz_status`. Lis-le en trois temps et **dis-lui ou il en est en une
phrase** avant de continuer.

**1. Le bloc du haut - ce que le serveur lit.**

- `Config d'equipe : <chemin>` suivi des reglages repris : c'est le cas normal.
- `Config d'equipe : aucune` : la bibliotheque SharePoint `Transport BtoC`
  n'est pas synchronisee au meme endroit sur son poste. Ce n'est pas bloquant -
  le serveur retombe sur ses valeurs par defaut - mais il devra alors saisir
  lui-meme l'`applicationId` et le `client_id`. Deux sorties : synchroniser la
  bibliotheque, ou pointer le fichier avec la variable d'environnement
  `YOOZ_SHARED_ENV`.
- Une ligne `ATTENTION ... cles ignorees` : **traite-la avant tout le reste.**
  Quelqu'un a pose dans un dossier partage une valeur qui n'a rien a y faire.
  Si c'est un `client_secret` ou un refresh token, il est a considerer comme
  divulgue : a faire revoquer, et a retirer du fichier.

**2. Le bloc par societe.** Pour chacune, l'outil rend `a completer : ...` avec
le nom exact des champs manquants. C'est cette liste que tu reprends a l'etape 2
- pas une liste generique.

**3. Le bloc cache**, en bas : `absent` tant que le premier rapatriement n'a pas
tourne. Normal a ce stade.

| Ce que rend `yooz_status` | La suite |
|---|---|
| des champs `a completer` | etape 2 |
| tout renseigne, mais `jeton : ECHEC` ou `appel API : ECHEC` | etape 3 |
| `jeton : OK` et `appel API : OK`, cache absent | etape 4 |
| tout OK, cache present | etape 5 |

---

## Etape 2 - Saisir les identifiants

Donne-lui ces quatre lignes, exactement, en **citant les champs que
`yooz_status` a listes comme manquants** :

> 1. tape **`/plugin`**
> 2. choisis **yooz-factures** (marketplace `vu-transport`)
> 3. ouvre **Configure** / la configuration du plugin, et renseigne, pour
>    chaque societe que tu utilises :
>    - **client_secret**
>    - **refresh token**
> 4. **redemarre la session** Claude Code - le serveur ne relit sa configuration
>    qu'au demarrage

Puis ces trois precisions, qui font gagner un aller-retour chacune :

- **Les champs `applicationId` et `client_id` peuvent rester vides** : le
  fichier de configuration d'equipe les fournit. Il ne les remplit que si
  `yooz_status` dit `Config d'equipe : aucune`, ou si sa societe n'y figure pas.
- **`applicationId` n'est pas le `client_id`.** C'est l'identifiant de
  l'*application* Yooz, un par societe, envoye en en-tete HTTP. Une erreur
  dessus rend un `403 EMPTY_OR_BAD_APPLICATION_ID`, qui se lit a tort comme une
  erreur d'authentification et fait chercher au mauvais endroit.
- **L'etape 4 n'est pas optionnelle.** Sans redemarrage, `yooz_status` rendra
  encore `MANQUANT`, et il croira s'etre trompe de champ.

Termine ton tour ici. Quand il revient, reprends a l'etape 1.

---

## Etape 3 - Verifier la connexion

`yooz_status` doit rendre `jeton : OK` **et** `appel API : OK` pour chaque
societe declaree. Sinon, applique ce tableau. **Ne conclus jamais que le
connecteur fonctionne sur un `ECHEC`.**

| Ce qui s'affiche | Ce que c'est vraiment | Ce qu'il doit faire |
|---|---|---|
| `invalid_grant` | le refresh token est expire ou revoque - ce n'est **pas** le secret | en regenerer un dans Yooz, puis reprendre l'etape 2 |
| `403 EMPTY_OR_BAD_APPLICATION_ID` | c'est l'`applicationId`, pas le secret | verifier la valeur dans `08_ENGINE/04_mcp/00_config/yooz.shared.env` ; si elle est vide, la faire poser la par le porteur du fichier |
| `401` sur le jeton | `client_id` ou `client_secret` faux | reprendre l'etape 2 ; verifier qu'aucun espace n'a ete colle en trop |
| `rapport : (manquant)` | l'identifiant du data report n'est pas resolu | appelle `yooz_reports` pour lister les rapports disponibles ; la valeur d'equipe est dans `yooz.shared.env` |
| une societe declaree qu'il n'utilise pas | elle bloque le diagnostic pour rien | la retirer du champ « Societes Yooz a interroger » dans `/plugin` |
| `%LOCALAPPDATA%\yooz-mcp` existe mais aucun fichier n'y est visible | virtualisation des interpreteurs Python empaquetes - le fichier est bien la, ce processus ne le voit pas | rien a corriger : la configuration du plugin est justement le chemin qui n'en souffre pas. Dis-le, ne le laisse pas chercher |

---

## Etape 4 - Le premier rapatriement

Explique d'abord **pourquoi**, en une phrase : l'API Yooz n'a aucun filtre
serveur, les questions sur les factures se repondent donc sur un cache local,
qui doit d'abord etre constitue.

Appelle `yooz_sync` avec `company="all"` et `mode="full"`.

Previens **avant de lancer** que le premier passage peut durer plusieurs
minutes selon l'historique - sinon il croira que c'est bloque. Rapporte ensuite
le nombre de lignes par societe.

Precise que les passages suivants se font en `mode="incremental"` et prennent
quelques secondes.

---

## Etape 5 - Montrer que ca marche

Deux appels courts, pas plus :

- `yooz_summary` avec `group_by="mois"` - le volume facture par mois ;
- `yooz_summary` avec `group_by="blockingCause"` - ce qui est bloque, et pourquoi.

---

## Etape 6 - Clore, en trois lignes

Pas plus de trois lignes :

1. ce qui est configure (quelles societes), et la date de la synchro ;
2. deux exemples de ce qu'il peut demander maintenant : « les factures VIR de
   juillet », « ce qui est bloque chez DistriService », « la fiche du
   fournisseur T-VIR » ;
3. les deux rappels qui comptent : le connecteur est en **lecture seule** - il
   ne valide ni ne comptabilise rien dans Yooz - et **un chiffre qui en sort se
   cite avec sa date de synchro**, sinon il sera faux dans trois semaines.
