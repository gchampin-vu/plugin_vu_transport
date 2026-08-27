---
description: Met en service le connecteur Shiptify : verifie la cle d'API, la range sur la machine, teste la connexion.
---

Mets en service le connecteur Shiptify pour cet utilisateur. Suis ces etapes
dans l'ordre, et arrete-toi des qu'une etape bloque.

## 1. Etat des lieux

Appelle `shiptify_setup_status`.

## 2. Si aucune cle n'est saisie

Ne demande **jamais** a l'utilisateur de coller sa cle dans la conversation, et
n'appelle aucun outil en lui passant une cle : la saisie se fait dans
l'interface de Claude Code, ou la cle reste hors du contexte du modele et hors
de la transcription.

Dis-lui exactement ceci, en une fois :

> Ta cle d'API Shiptify se saisit dans la configuration du plugin :
>
> 1. tape `/plugin`
> 2. choisis **shiptify** (marketplace `vu-transport`)
> 3. ouvre sa configuration et renseigne **Cle d'API Shiptify**
> 4. redemarre la session
>
> Puis relance `/shiptify-setup`.

Precise, s'il ne l'a pas, que la cle s'obtient aupres de Shiptify : le plugin
n'en fournit aucune, il utilise la sienne. Puis termine le tour : il n'y a rien
d'autre a faire avant qu'il ait saisi la cle.

## 3. Si une cle est saisie mais pas rangee sur la machine

Appelle `shiptify_save_key` (il ne prend aucun argument : il range la cle deja
saisie). Rapporte le chemin du magasin et les droits appliques.

Explique en une phrase ce que ca apporte : la cle survit a une reinstallation du
plugin, et l'installation directe la trouve aussi.

## 4. Verifie la connexion

Appelle `shiptify_doctor`. Il doit rendre `HTTP 200` sur `GET /` et sur
`GET /accounts/`.

Si l'API refuse la cle (`HTTP 401`), dis-le franchement : la cle est saisie mais
rejetee, elle est probablement erronee ou revoquee. Renvoie l'utilisateur vers
`/plugin` pour la corriger. Ne conclus pas que le connecteur fonctionne.

## 5. Montre que ca marche

Fais un seul appel de demonstration, court : `shiptify_dictionary` avec
`name="shipment-modes"`. Dix modes de transport doivent sortir.

Termine en trois lignes maximum : la cle est en place, ou elle est rangee, et ce
qu'il peut demander maintenant (« les envois vers l'Espagne cette semaine »,
« exporte les envois du mois en CSV »). Rappelle que le connecteur est en
**lecture seule** : il ne cree ni n'annule rien dans Shiptify.
