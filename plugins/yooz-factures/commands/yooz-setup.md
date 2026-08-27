---
description: Met en service le connecteur Yooz : verifie la configuration, teste la connexion, lance le premier rapatriement.
---

Mets en service le connecteur Yooz pour cet utilisateur. Suis ces etapes dans
l'ordre, et arrete-toi des qu'une etape bloque.

## 1. Etat des lieux

Appelle `yooz_status`. Il dit, societe par societe, ce qui est renseigne, si le
jeton s'obtient, si l'API repond, et ou en est le cache.

## 2. S'il manque des identifiants

Ne demande **jamais** a l'utilisateur de coller un secret dans la conversation,
et n'appelle aucun outil en lui passant un secret : la saisie se fait dans
l'interface de Claude Code, ou la valeur reste hors du contexte du modele et
hors de la transcription.

Dis-lui exactement ceci, en une fois, en citant les champs que `yooz_status`
signale comme manquants :

> Tes identifiants Yooz se saisissent dans la configuration du plugin :
>
> 1. tape `/plugin`
> 2. choisis **yooz-factures** (marketplace `vu-transport`)
> 3. ouvre sa configuration et renseigne, pour chaque societe :
>    `applicationId`, `client_id`, `client_secret`, refresh token
> 4. redemarre la session
>
> Puis relance `/yooz-setup`.

Deux precisions a donner, elles font gagner un aller-retour :

- **`applicationId` n'est pas le `client_id`.** C'est l'identifiant de
  l'application Yooz, un par societe, envoye en en-tete HTTP. Une erreur dessus
  rend un `403 EMPTY_OR_BAD_APPLICATION_ID`, qui se lit a tort comme une erreur
  d'authentification.
- Il peut ne renseigner **qu'une seule societe** : dans ce cas, il retire l'autre
  du champ « Societes Yooz a interroger ».

Si les identifiants ne sont pas en sa possession, dis-lui qu'ils s'obtiennent
aupres de l'administrateur de l'application Yooz : le plugin n'en fournit aucun,
il utilise les siens. Puis termine le tour.

## 3. Verifie la connexion

`yooz_status` doit rendre `jeton : OK` **et** `appel API : OK` pour chaque
societe declaree.

- `invalid_grant` : le refresh token est revoque ou expire. Il faut en regenerer
  un dans Yooz. Ne conclus pas que le connecteur fonctionne.
- `403 EMPTY_OR_BAD_APPLICATION_ID` : c'est l'`applicationId`, pas le secret.
- Si `yooz_status` signale que le dossier `%LOCALAPPDATA%\yooz-mcp` existe mais
  qu'aucun fichier n'y est visible, explique-le : c'est la virtualisation des
  interpreteurs Python empaquetes, et la configuration du plugin est justement le
  chemin qui n'en souffre pas. Rien a corriger.

## 4. Le premier rapatriement

L'API Yooz n'a aucun filtre serveur : les questions sur les factures se
repondent sur un cache local, qui doit d'abord etre constitue.

Appelle `yooz_sync` avec `company="all"` et `mode="full"`. Previens que le
premier passage peut durer quelques minutes selon l'historique, puis rapporte le
nombre de lignes par societe.

## 5. Montre que ca marche

Deux appels courts, pas plus :

- `yooz_summary` avec `group_by="mois"` - le volume facture par mois ;
- `yooz_summary` avec `group_by="blockingCause"` - ce qui est bloque, et pourquoi.

Termine en trois lignes maximum : ce qui est configure, la date de la synchro, et
ce qu'il peut demander maintenant (« les factures VIR de juillet », « ce qui est
bloque chez DistriService », « la fiche du fournisseur T-VIR »). Rappelle deux
choses : le connecteur est en **lecture seule** - il ne valide ni ne comptabilise
rien dans Yooz - et un chiffre qui en sort se cite **avec sa date de synchro**.
