-- Les colonnes reelles d'une table, telles qu'elles existent SUR LA BASE.
--
-- Le catalogue MLD embarque dans ce connecteur repond plus vite et sans
-- toucher la production : commence toujours par reflex_columns. Cette requete
-- ne sert que dans deux cas ou le MLD ne suffit pas :
--   - une colonne ajoutee par un correctif posterieur a la v9.14 (le MLD
--     embarque date du 18/11/2019) ;
--   - une vue ou une table maison, absente du MLD editeur.
--
-- Elle lit INFORMATION_SCHEMA, pas les tables Reflex : elle est instantanee et
-- ne pese rien.
--
-- A adapter : le nom de la table.

SELECT
    c.TABLE_NAME                     AS nom_table,
    c.ORDINAL_POSITION               AS position,
    c.COLUMN_NAME                    AS nom_colonne,
    c.DATA_TYPE                      AS type_sql,
    c.CHARACTER_MAXIMUM_LENGTH       AS longueur,
    c.NUMERIC_PRECISION              AS precision_num,
    c.NUMERIC_SCALE                  AS echelle_num,
    c.IS_NULLABLE                    AS nullable
FROM   INFORMATION_SCHEMA.COLUMNS c  WITH (NOLOCK)
WHERE  c.TABLE_SCHEMA = 'reflex'
  AND  c.TABLE_NAME   = 'HLPRENP'
ORDER  BY c.ORDINAL_POSITION
