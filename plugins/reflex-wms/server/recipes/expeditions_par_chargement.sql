-- Expeditions agregees par chargement (touliv), avec volume et poids.
--
-- Pourquoi agreger au chargement et pas a l'expedition : une expedition VUD,
-- c'est un client, et elle plafonne vers 6 m3 et 350 kg. Un seuil "XXL" n'a de
-- sens qu'au niveau du CAMION, donc du chargement.
--
-- Le volume et le poids ne sont PAS sur HLEXPEP : ils viennent de l'en-tete de
-- preparation, PEVATP en dm3 et PEPATP en kg. D'ou le / 1000.
--
-- Normalisation du code chargement : 4 chiffres (le touliv) puis un suffixe de
-- sous-tour separe par un tiret. On coupe au tiret pour regrouper la tournee.
-- Les suffixes alphabetiques (AM, TL, VI, RE, MO) ne sont pas coupes par cette
-- regle - c'est connu, et assume.
--
-- Regroupements VUD connus : 6128, toute la famille 71%, 8120.
--
-- A adapter : le depot, la periode, le filtre de regroupement.

WITH lignes AS (
    SELECT
        ex.EXCDPO                                AS depot,
        ex.EXNEXP                                AS num_expedition,
        CASE WHEN CHARINDEX('-', LTRIM(RTRIM(ex.EXCCHA))) > 0
             THEN LEFT(LTRIM(RTRIM(ex.EXCCHA)), CHARINDEX('-', LTRIM(RTRIM(ex.EXCCHA))) - 1)
             ELSE LTRIM(RTRIM(ex.EXCCHA))
        END                                      AS chargement,
        ex.EXSSCA, ex.EXANCA, ex.EXMOCA, ex.EXJOCA,
        pe.PEVATP                                AS volume_dm3,
        pe.PEPATP                                AS poids_kg
    FROM   reflex.HLEXPEP ex  WITH (NOLOCK)
    INNER  JOIN reflex.HLPRENP pe  WITH (NOLOCK)
           ON  pe.PECDPO = ex.EXCDPO
           AND pe.PENANN = ex.EXNANN
           AND pe.PENPRE = ex.EXNPRE
    WHERE  ex.EXCDPO = 'AMB'
      AND  (ex.EXSSCA * 1000000 + ex.EXANCA * 10000 + ex.EXMOCA * 100 + ex.EXJOCA) >= 20260801
)
SELECT
    depot,
    chargement,
    REFLEX.RFX_DHB_DATE2DATETIME(EXSSCA, EXANCA, EXMOCA, EXJOCA, 0) AS date_chargement,
    COUNT(DISTINCT num_expedition)             AS nb_expeditions,
    COUNT(*)                                   AS nb_preparations,
    SUM(volume_dm3) / 1000.0                   AS volume_m3,
    SUM(poids_kg)                              AS poids_kg
FROM   lignes
GROUP  BY depot, chargement, EXSSCA, EXANCA, EXMOCA, EXJOCA
ORDER  BY date_chargement DESC, volume_m3 DESC
