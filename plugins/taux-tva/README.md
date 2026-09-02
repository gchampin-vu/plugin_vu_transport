---
updated: 2026-09-01
updated_by: Guillaume_Champin
type: process
---

# Plugin `taux-tva` — connecteur MCP en lecture seule

Serveur MCP qui donne à Claude Code un accès **en lecture** aux taux de TVA des
**27 États membres de l'Union européenne** : taux normal, taux réduits,
super-réduit, taux parking, devise, et — la colonne qui change tout — les
**catégories de biens et services** auxquelles chaque taux réduit s'applique.

C'est le portage du script Power Query qui appelait `api.vatcomply.com/vat_rates`
et n'en gardait que trois colonnes (`country_code`, `standard_rate`, `currency`).
La source en rend dix. Les sept jetées sont précisément celles qui répondent aux
questions qu'on se pose réellement : *quel taux sur le transport de personnes en
Italie*, *pourquoi 5,5 % en France*, *le Portugal a-t-il un taux parking*.

Quatre différences avec le script d'origine :

| Le script Power Query | Ce connecteur |
| --- | --- |
| Trois colonnes, le reste jeté | Les dix colonnes, catégories comprises |
| Un rafraîchissement de modèle pour voir un taux | La réponse en session, en une question en français |
| `GR` et `EL` se perdent au rapprochement | La Grèce est traduite, et le piège est nommé dans chaque rendu |
| Rien sur ce qui manque | La Suisse, la Norvège et le Royaume-Uni sont **nommés comme non couverts**, avec où chercher |

## Ce que ce connecteur n'est pas

**Ce n'est pas une référence fiscale.** VAT Comply est une source publique,
gratuite, sans engagement de mise à jour. Elle est excellente pour comparer 27
pays d'un coup et pour repérer qu'un taux a bougé ; elle n'établit pas le taux
d'une facture.

Dès qu'un taux va servir à facturer, à déclarer ou à paramétrer un ERP, la
référence est **l'administration fiscale du pays**. Chaque rendu du serveur porte
cette phrase en pied : c'est volontaire, et ça ne se retire pas.

C'est la règle n° 3 du `CLAUDE.md` de la bibliothèque — *on n'invente jamais un
chiffre* — appliquée à une source externe. Un taux cité sans sa provenance ni sa
date part dans un mail et engage l'entreprise.

## La particularité : ce qui n'est PAS couvert compte autant que le reste

La source s'arrête aux **27 États membres**. Trois pays où le groupe vend n'y
sont pas : **la Suisse, la Norvège, le Royaume-Uni**.

Un connecteur naïf rendrait une ligne vide, qu'on lirait comme un zéro. Celui-ci
porte un **référentiel local** (`server/pays.json`) qui n'a aucun taux dedans —
seulement ce que l'API ne dit pas :

| Ce que porte `pays.json` | Pourquoi |
| --- | --- |
| Les 27 codes et leur nom français | Pour comprendre « Grèce », « Pays-Bas », « Tchéquie » |
| Les alias, dont **`GR` → `EL`** | La Grèce porte le code **EL** dans les nomenclatures TVA de l'Union, pas le code ISO `GR`. Un rapprochement fait sur des codes ISO perd la Grèce **en silence** |
| 7 pays **non couverts**, avec la raison et où chercher | CH, NO, GB, IS, LI, TR, XI. La réponse n'est pas « je n'ai pas trouvé », c'est « cette source ne les couvre pas, voici où c'est » |
| 11 **territoires à régime particulier** | Canaries (IGIC), Ceuta-Melilla, Madère, Açores, Corse, DOM, Monaco, Åland, Büsingen-Helgoland, Livigno-Campione, Mont Athos. La source rend **un** taux par pays : le taux national ne s'y applique pas |

Un pays demandé et non couvert n'est **jamais écarté en silence** : il ressort en
tête du rendu, nommé, avec l'administration où chercher. Une ligne manquante dans
un tableau se lit comme une absence de TVA — c'est exactement le chiffre faux
qu'on veut éviter.

## Ce que ça sait faire

| Outil | Ce qu'il rend |
| --- | --- |
| `tva_taux` | **l'outil principal** : les taux des pays demandés, en CSV point-virgule. Accepte les codes, les noms français, `UE`, `VU`. Une colonne de plus si on précise une catégorie |
| `tva_detail` | tout ce que la source dit d'**un** pays : les 27 catégories de biens et le taux de chacune, les commentaires officiels, et les territoires rattachés |
| `tva_categories` | les catégories que la source sait qualifier, et combien de pays en déclarent une. **À lire avant** de passer `categorie` à `tva_taux` |
| `tva_pays` | ce que le connecteur reconnaît, et surtout ce qu'il **ne couvre pas**. Répond hors ligne, depuis le référentiel embarqué |
| `tva_changements` | ce qui a bougé entre le relevé courant et le précédent — taux, devise, pays apparu ou disparu |
| `tva_rafraichir` | force un appel à la source sans attendre l'expiration du cache |
| `tva_export_csv` | écrit un CSV, **sur demande explicite**. Deux formes : une ligne par pays, ou une ligne par pays × catégorie |
| `tva_doctor` | interpréteur, chemins, configuration lue, cache, et un vrai appel. Le premier à appeler quand ça coince |

Et une commande : **`/tva-export`**, pour demander un export en français sans
savoir qu'un outil s'appelle `tva_export_csv`.

Les outils sont annotés `readOnlyHint` conformément à la spécification MCP. Ce
qui *garantit* la lecture seule reste le code : un seul chemin appelé, en GET,
sans paramètre.

## Le cache local, et pourquoi il existe

Le connecteur garde le dernier relevé dans `~/.tva-mcp/vat_rates.json`, et le
**relevé précédent** à côté. Trois raisons, dans l'ordre :

1. **Ne pas marteler une API publique gratuite.** Un taux de TVA change par une
   loi de finances, pas dans la journée : 24 h de fraîcheur est large.
2. **Répondre quand le réseau est coupé.** Le rendu dit alors, en majuscules,
   que ce ne sont **pas forcément les taux du jour**. Un cache périmé n'est
   jamais servi en silence.
3. **Pouvoir dire ce qui a changé.** « Le taux a-t-il bougé » n'a pas de réponse
   sans point de comparaison — on ne compare pas un taux à un souvenir. Le
   relevé précédent n'est conservé que le jour où la source rend autre chose.

Sans cache **et** sans réseau, le serveur ne rend rien et le dit. Il n'affiche
pas un taux qu'il n'a pas lu.

## Installation

Le plugin fait partie du marketplace `vu-transport`. Une fois le catalogue
ajouté :

```
/plugin install taux-tva@vu-transport
```

**Il n'y a rien à mettre en service.** Aucune clé d'API : la source est publique
et anonyme. Le premier démarrage crée un environnement Python sous
`~/.tva-mcp/venv` et y installe `mcp` et `httpx` — une fois, automatiquement, par
`server/bootstrap.py`.

Pour vérifier : appeler `tva_doctor`, ou en ligne de commande

```bash
python server/bootstrap.py doctor
```

Il n'y a **pas d'`install.ps1`** ici, à la différence des autres connecteurs de
l'équipe : le `bootstrap.py` est le chemin nominal, et un chemin réservé à
Windows rendrait le plugin non installable sur Mac
(voir [`../../../04_mcp/README.md`](../../../04_mcp/README.md)).

## Configuration

Tout est optionnel — le connecteur démarre sans rien.

| Réglage | Ce qu'il fait | Où il vit |
| --- | --- | --- |
| `TVA_PAYS_VU` | les pays interrogés quand la question n'en nomme aucun, et ce que désigne `pays="VU"` | **fichier d'équipe** — c'est une décision, pas un réglage de poste |
| `TVA_TTL_HOURS` | fraîcheur du cache, en heures (défaut 24) | équipe ou poste |
| `TVA_TIMEOUT_S` | délai d'attente d'un appel (défaut 30) | équipe ou poste |
| `TVA_BASE_URL` | racine de l'API (défaut `https://api.vatcomply.com`) | équipe ou poste |
| `TVA_EXPORT_DIR` | où atterrissent les CSV (défaut `~/.tva-mcp/exports`) | **poste seulement** |
| `TVA_CACHE_FILE` | où est écrit le cache | **poste seulement** |

Priorité : configuration du plugin > `~/.tva-mcp/tva.env` > fichier d'équipe
`08_ENGINE/04_mcp/00_config/tva.shared.env` > défaut du serveur. Le poste passe
devant l'équipe.

Modèles : `server/tva.env.example` et `server/tva.shared.env.example`.

### `TVA_PAYS_VU` n'est pas renseigné, et c'est délibéré

La liste des pays où l'équipe travaille **n'est pas écrite dans le code**. Être
présent sur un marché et y être immatriculé à la TVA sont deux choses
différentes : déduire ce périmètre de la liste des boutiques du groupe aurait été
un fait faux gravé dans du code.

Tant qu'il n'est pas posé, une question sans pays rend les 27. Le poser est une
**écriture dans le collectif** : elle prend effet sur tous les postes, et elle se
fait après avoir demandé la liste à qui la connaît.

## Le fichier d'équipe

Il n'a **rien d'indispensable** ici : pas de clé, pas de racine à distribuer. Il
n'a qu'une raison d'être, `TVA_PAYS_VU`. Tant qu'il n'existe pas, `tva_doctor`
l'annonce sans en faire un problème — ce qu'il n'est pas.

S'il est déposé dans `08_ENGINE/04_mcp/00_config/`, le tableau du
[`README` de `00_config`](../../../04_mcp/00_config/README.md) est à compléter
dans le même mouvement.

## Contrôles hors ligne

```bash
python server/test_offline.py
```

99 contrôles, aucun appel réseau. Ce qui est vérifié, c'est ce qui protège contre
un chiffre faux : la traduction `GR` → `EL`, le refus de rendre un taux pour un
pays non couvert, la présence de la date de relevé dans chaque rendu, le
comportement quand la source est muette, l'absence de fabrication quand il n'y a
ni cache ni réseau — et les conventions de portabilité Mac / Windows (racine
locale sous le profil, `sys.executable`, `newline=""` à l'écriture du CSV, rien
sur `stdout`).

## État

Construit et testé le **2026-09-01** : 99 contrôles hors ligne, et confronté à la
vraie source le même jour — 27 pays rendus, `tva_doctor` au vert.

Ce qui reste ouvert :

- **`TVA_PAYS_VU`** — à établir avec qui connaît le périmètre d'immatriculation,
  puis à déposer dans le fichier d'équipe.
- **La Suisse et la Norvège** — deux pays de vente que cette source ne couvrira
  jamais. Si le besoin est réel, c'est une **deuxième source** à brancher, pas
  une ligne à ajouter au référentiel.
- **La validation de numéro de TVA** (VIES) est volontairement hors périmètre :
  elle enverrait un identifiant de tiers à un service externe. C'est une
  décision à prendre, pas un oubli.
