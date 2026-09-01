---
description: Mettre en service le connecteur Reflex WMS, ou comprendre pourquoi il ne repond pas.
---

# Mise en service du connecteur Reflex

Tu guides la mise en service du connecteur `reflex`. Va au bout, sans poser de
question dont tu as déjà la réponse dans les outils.

## Ce qu'il faut savoir avant de commencer

**Dans le cas normal, il n'y a rien à saisir.** Tout est dans la configuration
d'équipe, `08_ENGINE/04_mcp/00_config/reflex.shared.env` : serveur, les deux
bases, schéma, garde-fous, et le compte de service `query` en lecture. Un poste
qui synchronise la bibliothèque est configuré au premier démarrage.

Ce qui peut manquer, c'est autre chose — le réseau, la bibliothèque non
synchronisée, ou la dépendance Python.

**Ne demande jamais un mot de passe à l'utilisateur.** S'il en faut un, c'est
qu'on est dans un cas particulier, et la saisie se fait dans
`/plugin > reflex-wms`, jamais dans la conversation.

## Déroulé

### 1. L'état

Lance `reflex_setup_status`. Il dit d'où vient chaque réglage, quel pilote ODBC
est retenu, où en est le catalogue MLD embarqué, et comment les garde-fous sont
réglés. **Ne se connecte pas** à la base.

Regarde en premier la ligne `Config d'equipe`. Si elle dit « aucune », c'est la
cause de tout le reste : le poste ne trouve pas la bibliothèque, donc ni les
bases ni l'identifiant.

Lis-le et restitue en clair : ce qui est prêt, ce qui manque.

### 2. La connexion

Lance `reflex_doctor`. Il tente une connexion réelle, vérifie le schéma, la
fonction de date `RFX_DHB_DATE2DATETIME`, et l'accès à la base d'épuration.

Quatre échecs possibles, quatre gestes différents — ne les confonds pas :

| Ce que dit le doctor | Ce que ça veut dire | Le geste |
|---|---|---|
| serveur injoignable, délai de connexion dépassé | Reflex est sur le réseau interne | être au bureau ou sur le VPN, puis relancer |
| `Login failed` / 18456, **en mode trusted** | la config d'équipe n'a pas été trouvée, le poste est retombé sur l'authentification Windows que l'instance refuse | vérifier la synchronisation de la bibliothèque, ou pointer le fichier avec `REFLEX_SHARED_ENV` |
| `Login failed` / 18456, **en mode sql** | le mot de passe du compte de service a changé côté base | à corriger **une fois** dans `reflex.shared.env`, pour tout le monde — voir plus bas |
| `pyodbc` absent | la dépendance Python n'est pas installée | relancer la session : `bootstrap.py` l'installe au premier démarrage |

**Le catalogue MLD répond même hors ligne.** Si le serveur est injoignable,
`reflex_tables`, `reflex_columns`, `reflex_find_column`, `reflex_prefix`,
`reflex_guide` et `reflex_recipes` fonctionnent quand même — dis-le, c'est ce
qui permet de préparer une requête depuis chez soi.

### 3. Le premier essai

Une fois le doctor au vert, propose un essai concret plutôt qu'un message de
succès :

```
reflex_peek('HLDEPPP', max_rows=10)
```

Il liste les dépôts physiques, c'est court, et ça confirme que la lecture
fonctionne de bout en bout.

### 4. Ce qu'il faut dire pour finir

- Le connecteur est en **lecture seule par construction** : aucun outil
  d'écriture, tout ce qui n'est pas un `SELECT` est refusé avant d'atteindre la
  base, la session ne pose aucun verrou, et le compte SQL est en lecture.
- Les **garde-fous** sont actifs : gouverneur de coût, délai borné,
  `WITH (NOLOCK)` exigé, une requête à la fois. Rappelle les valeurs lues dans
  le statut.
- **Par défaut une lecture interroge l'épuration puis la base courante**, et le
  connecteur dit toujours laquelle a répondu. Ne jamais citer un chiffre sans
  cette mention.
- `reflex_guide()` est à lire avant la première requête.
- **Jalon Webfacto** : brancher ce connecteur à une automatisation, un flux ou
  un outil partagé demande un cadrage Webfacto préalable. Le prototypage et
  l'usage individuel, non.

## Si le mot de passe du compte de service a changé

C'est une écriture dans le collectif, et elle prend effet **sur tous les
postes** au prochain démarrage de session. Donc :

1. propose la ligne exacte à modifier dans
   `08_ENGINE/04_mcp/00_config/reflex.shared.env` — la clé `REFLEX_PASSWORD` ;
2. **attends le feu vert de Guillaume** ;
3. mets `updated` / `updated_by` à jour ;
4. avant d'écrire, vérifie qu'aucun fichier suffixé du nom d'une machine
   (`-VU-XXXXX`) ne traîne dans le dossier : SharePoint ne fusionne pas les
   écritures concurrentes, il duplique ;
5. relance `/reflex-setup` après.

Une valeur fausse ici casse les 26 postes en même temps.
