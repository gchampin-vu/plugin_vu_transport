#!/usr/bin/env python3
"""
Lecture des GeoPackage GISCO et geometrie elementaire, sans dependance.

Pourquoi ce fichier existe. GISCO publie chaque couche en six formats. Le
GeoJSON est le plus simple a lire et le plus lourd de tous : 150 Mo pour les
communes, 490 Mo pour les codes postaux. Le GeoPackage porte les MEMES donnees
en deux fois moins de place, et c'est une base SQLite - donc le `sqlite3` de la
bibliotheque standard l'ouvre, sans GDAL, sans geopandas, sans roue compilee a
installer sur le poste d'un collegue. Le prix a payer est ici : la geometrie y
est en WKB, et il faut la lire soi-meme. C'est trois cents lignes, ecrites une
fois.

Rien ici ne fait d'entree/sortie reseau ni ne parle a MCP : c'est de la lecture
de blob et du calcul plan. Tout est testable hors ligne, et test_offline.py le
fait.

Reference : OGC GeoPackage 1.3 (en-tete "GP") et OGC Simple Features WKB 1.2.
"""

from __future__ import annotations

import math
import sqlite3
import struct
from typing import Any, Iterator

# Identifiants WKB des types geometriques utilises par GISCO. Les couches
# regionales sont en MULTIPOLYGON, les codes postaux en POINT.
WKB_POINT = 1
WKB_LINESTRING = 2
WKB_POLYGON = 3
WKB_MULTIPOINT = 4
WKB_MULTILINESTRING = 5
WKB_MULTIPOLYGON = 6
WKB_GEOMETRYCOLLECTION = 7

# Rayon moyen de la Terre, en kilometres. Valeur IUGG.
EARTH_RADIUS_KM = 6371.0088


class GeoError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# En-tete GeoPackage
# --------------------------------------------------------------------------

def strip_gpkg_header(blob: bytes) -> tuple[int, tuple[float, float, float, float] | None, bytes]:
    """Retire l'en-tete GeoPackage d'un blob et rend (srs_id, enveloppe, wkb).

    Un blob GeoPackage commence par la signature "GP", un octet de version et un
    octet de drapeaux. Le bit 0 des drapeaux donne le boutisme des entiers de
    l'en-tete ; les bits 1 a 3 disent quelle enveloppe suit (aucune, 2D, 3D...).
    L'enveloppe est un cadeau : elle evite de decoder le polygone entier pour
    savoir s'il peut contenir un point.
    """
    if len(blob) < 8 or blob[0:2] != b"GP":
        # Certains producteurs ecrivent du WKB nu. On l'accepte.
        return 0, None, blob
    flags = blob[3]
    little = bool(flags & 0x01)
    order = "<" if little else ">"
    envelope_code = (flags >> 1) & 0x07
    srs_id = struct.unpack_from(order + "i", blob, 4)[0]
    offset = 8
    envelope: tuple[float, float, float, float] | None = None
    # 0 : pas d'enveloppe. 1 : minx,maxx,miny,maxy. 2 et 3 : idem + z ou m.
    # 4 : + z ET m. Le nombre de doubles suit ce tableau.
    doubles = {0: 0, 1: 4, 2: 6, 3: 6, 4: 8}.get(envelope_code)
    if doubles is None:
        raise GeoError(f"Enveloppe GeoPackage de code inconnu : {envelope_code}")
    if doubles:
        vals = struct.unpack_from(order + f"{doubles}d", blob, offset)
        offset += 8 * doubles
        # GeoPackage ordonne minx, maxx, miny, maxy - PAS minx, miny, maxx, maxy.
        envelope = (vals[0], vals[2], vals[1], vals[3])
    return srs_id, envelope, blob[offset:]


# --------------------------------------------------------------------------
# WKB -> GeoJSON
# --------------------------------------------------------------------------

def _read_header(buf: bytes, pos: int) -> tuple[str, int, int]:
    if pos + 5 > len(buf):
        raise GeoError("WKB tronque : en-tete de geometrie incomplet.")
    order = "<" if buf[pos] == 1 else ">"
    gtype = struct.unpack_from(order + "I", buf, pos + 1)[0]
    # Les variantes Z (1000+), M (2000+) et ZM (3000+) portent le meme type de
    # base. GISCO publie du 2D, mais on ne casse pas sur du Z.
    base = gtype % 1000
    if gtype >= 3000:      # ZM
        dims = 4
    elif gtype >= 1000:    # Z ou M
        dims = 3
    else:
        dims = 2
    return order, base, dims


def _read_points(buf: bytes, pos: int, order: str, dims: int, count: int) -> tuple[list, int]:
    size = 8 * dims
    need = size * count
    if pos + need > len(buf):
        raise GeoError("WKB tronque : liste de points incomplete.")
    out = []
    for i in range(count):
        vals = struct.unpack_from(order + f"{dims}d", buf, pos + i * size)
        # On ne garde que x, y : la troisieme dimension n'a pas de sens ici et
        # doublerait le poids du cache pour rien.
        out.append([vals[0], vals[1]])
    return out, pos + need


def _read_geometry(buf: bytes, pos: int) -> tuple[dict[str, Any], int]:
    order, base, dims = _read_header(buf, pos)
    pos += 5
    if base == WKB_POINT:
        pts, pos = _read_points(buf, pos, order, dims, 1)
        return {"type": "Point", "coordinates": pts[0]}, pos
    if base == WKB_LINESTRING:
        (n,) = struct.unpack_from(order + "I", buf, pos)
        pos += 4
        pts, pos = _read_points(buf, pos, order, dims, n)
        return {"type": "LineString", "coordinates": pts}, pos
    if base == WKB_POLYGON:
        (nrings,) = struct.unpack_from(order + "I", buf, pos)
        pos += 4
        rings = []
        for _ in range(nrings):
            (n,) = struct.unpack_from(order + "I", buf, pos)
            pos += 4
            pts, pos = _read_points(buf, pos, order, dims, n)
            rings.append(pts)
        return {"type": "Polygon", "coordinates": rings}, pos
    if base in (WKB_MULTIPOINT, WKB_MULTILINESTRING, WKB_MULTIPOLYGON):
        (n,) = struct.unpack_from(order + "I", buf, pos)
        pos += 4
        parts = []
        for _ in range(n):
            geom, pos = _read_geometry(buf, pos)
            parts.append(geom["coordinates"])
        name = {
            WKB_MULTIPOINT: "MultiPoint",
            WKB_MULTILINESTRING: "MultiLineString",
            WKB_MULTIPOLYGON: "MultiPolygon",
        }[base]
        return {"type": name, "coordinates": parts}, pos
    if base == WKB_GEOMETRYCOLLECTION:
        (n,) = struct.unpack_from(order + "I", buf, pos)
        pos += 4
        geoms = []
        for _ in range(n):
            geom, pos = _read_geometry(buf, pos)
            geoms.append(geom)
        return {"type": "GeometryCollection", "geometries": geoms}, pos
    raise GeoError(f"Type WKB non gere : {base}")


def wkb_to_geojson(wkb: bytes) -> dict[str, Any]:
    """Decode un WKB (en-tete GeoPackage tolere) en geometrie GeoJSON."""
    _, _, body = strip_gpkg_header(wkb)
    geom, _ = _read_geometry(body, 0)
    return geom


# --------------------------------------------------------------------------
# GeoJSON -> WKB
# --------------------------------------------------------------------------

def geojson_to_wkb(geom: dict[str, Any]) -> bytes:
    """Encode une geometrie GeoJSON en WKB petit-boutiste.

    Sert au versant britannique : l'ArcGIS de l'ONS ne sait pas rendre du
    GeoPackage, il rend du GeoJSON. On l'encode a l'entree pour que le cache
    n'ait qu'UNE representation de geometrie, et que le test d'appartenance ne
    connaisse qu'un seul format.
    """
    kind = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates")
    if kind is None:
        raise GeoError("Geometrie GeoJSON sans champ 'type'.")

    def pt(p) -> bytes:
        return struct.pack("<2d", float(p[0]), float(p[1]))

    def ring(r) -> bytes:
        return struct.pack("<I", len(r)) + b"".join(pt(p) for p in r)

    def polygon(rings) -> bytes:
        return struct.pack("<I", len(rings)) + b"".join(ring(r) for r in rings)

    head = lambda t: struct.pack("<BI", 1, t)  # noqa: E731

    if kind == "Point":
        return head(WKB_POINT) + pt(coords)
    if kind == "LineString":
        return head(WKB_LINESTRING) + ring(coords)
    if kind == "Polygon":
        return head(WKB_POLYGON) + polygon(coords)
    if kind == "MultiPoint":
        return (
            head(WKB_MULTIPOINT)
            + struct.pack("<I", len(coords))
            + b"".join(head(WKB_POINT) + pt(p) for p in coords)
        )
    if kind == "MultiLineString":
        return (
            head(WKB_MULTILINESTRING)
            + struct.pack("<I", len(coords))
            + b"".join(head(WKB_LINESTRING) + ring(r) for r in coords)
        )
    if kind == "MultiPolygon":
        return (
            head(WKB_MULTIPOLYGON)
            + struct.pack("<I", len(coords))
            + b"".join(head(WKB_POLYGON) + polygon(p) for p in coords)
        )
    raise GeoError(f"Type GeoJSON non gere a l'encodage : {kind}")


# --------------------------------------------------------------------------
# Calculs
# --------------------------------------------------------------------------

def bbox(geom: dict[str, Any]) -> tuple[float, float, float, float]:
    """Cadre englobant (min_lon, min_lat, max_lon, max_lat)."""
    xs: list[float] = []
    ys: list[float] = []

    def walk(node) -> None:
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)):
                xs.append(float(node[0]))
                ys.append(float(node[1]))
                return
            for child in node:
                walk(child)

    if geom.get("type") == "GeometryCollection":
        for sub in geom.get("geometries") or []:
            walk(sub.get("coordinates"))
    else:
        walk(geom.get("coordinates"))
    if not xs:
        raise GeoError("Geometrie vide : pas de cadre englobant.")
    return min(xs), min(ys), max(xs), max(ys)


def _ring_contains(ring: list, lon: float, lat: float) -> bool:
    """Lancer de rayon sur un anneau ferme.

    Un point exactement sur le bord est indetermine par nature ; la convention
    retenue ici (bord compte comme dedans a gauche, dehors a droite) suffit :
    aucune adresse reelle ne tombe sur un vertex de contour administratif.
    """
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            denom = yj - yi
            if denom != 0.0 and lon < (xj - xi) * (lat - yi) / denom + xi:
                inside = not inside
        j = i
    return inside


def _polygon_contains(rings: list, lon: float, lat: float) -> bool:
    if not rings:
        return False
    if not _ring_contains(rings[0], lon, lat):
        return False
    # Les anneaux suivants sont des trous : un point dans un trou est dehors.
    for hole in rings[1:]:
        if _ring_contains(hole, lon, lat):
            return False
    return True


def contains(geom: dict[str, Any], lon: float, lat: float) -> bool:
    """Le point (lon, lat) est-il dans la geometrie ?"""
    kind = geom.get("type")
    if kind == "Polygon":
        return _polygon_contains(geom.get("coordinates") or [], lon, lat)
    if kind == "MultiPolygon":
        return any(
            _polygon_contains(poly, lon, lat) for poly in (geom.get("coordinates") or [])
        )
    if kind == "GeometryCollection":
        return any(contains(sub, lon, lat) for sub in (geom.get("geometries") or []))
    return False


def _ring_area_and_centroid(ring: list) -> tuple[float, float, float]:
    """Aire signee et centroide plan d'un anneau. Suffit pour un point figuratif."""
    area = 0.0
    cx = 0.0
    cy = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area /= 2.0
    if abs(area) < 1e-12:
        xs = [p[0] for p in ring] or [0.0]
        ys = [p[1] for p in ring] or [0.0]
        return 0.0, sum(xs) / len(xs), sum(ys) / len(ys)
    return area, cx / (6.0 * area), cy / (6.0 * area)


def centroid(geom: dict[str, Any]) -> tuple[float, float]:
    """Point figuratif d'une geometrie : le centroide de son plus grand anneau.

    Ce n'est PAS le chef-lieu et ce n'est pas garanti a l'interieur d'une
    commune en croissant. Quand le centre officiel compte, c'est la couche des
    points-etiquettes GISCO (_LB_) qu'il faut, pas un calcul.
    """
    kind = geom.get("type")
    if kind == "Point":
        c = geom["coordinates"]
        return float(c[0]), float(c[1])
    rings: list = []
    if kind == "Polygon":
        rings = [geom.get("coordinates", [[]])[0]]
    elif kind == "MultiPolygon":
        rings = [poly[0] for poly in (geom.get("coordinates") or []) if poly]
    if not rings:
        min_x, min_y, max_x, max_y = bbox(geom)
        return (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
    best = None
    for ring in rings:
        area, cx, cy = _ring_area_and_centroid(ring)
        if best is None or abs(area) > abs(best[0]):
            best = (area, cx, cy)
    return best[1], best[2]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique en kilometres.

    C'est une distance A VOL D'OISEAU. Elle ne remplace jamais un kilometrage
    routier : sur un plan de transport, l'ecart courant est de 20 a 30 %, et
    davantage des qu'il y a un fleuve, un relief ou un bras de mer.
    """
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


# --------------------------------------------------------------------------
# Parcours d'un GeoPackage
# --------------------------------------------------------------------------

def gpkg_feature_table(path) -> tuple[str, str]:
    """Rend (table, colonne_geometrie) de la couche vectorielle du fichier."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = list(
            conn.execute(
                "SELECT table_name, column_name FROM gpkg_geometry_columns LIMIT 1"
            )
        )
    except sqlite3.DatabaseError as exc:
        raise GeoError(
            f"{path} n'est pas un GeoPackage lisible : {exc}. Le telechargement "
            "a probablement ete tronque - relance la synchronisation."
        ) from exc
    finally:
        conn.close()
    if not rows:
        raise GeoError(f"{path} ne declare aucune couche vectorielle.")
    return rows[0][0], rows[0][1]


def gpkg_columns(path) -> list[str]:
    """Colonnes reelles de la couche, hors identifiant technique et geometrie.

    Sert a retrouver une colonne dont le NOM porte un millesime : la population
    s'appelle POP_2024 dans un fichier et POP_2023 dans le precedent, et le
    NUTS3 du jeu des codes postaux ne suit meme pas le millesime du fichier.
    On lit donc les colonnes plutot que de calculer leur nom.
    """
    table, geom_col = gpkg_feature_table(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = conn.execute(f'SELECT * FROM "{table}" LIMIT 0')
        return [d[0] for d in cur.description if d[0] not in (geom_col, "fid")]
    finally:
        conn.close()


def iter_gpkg(path, batch: int = 2000) -> Iterator[dict[str, Any]]:
    """Parcourt un GeoPackage et rend un dictionnaire par entite.

    La geometrie est rendue telle quelle sous la cle "__wkb" (WKB nu, en-tete
    GeoPackage retire) : la convertir en GeoJSON pour 830 000 codes postaux
    couterait des minutes et des gigaoctets, alors que le cache la range en
    binaire.
    """
    table, geom_col = gpkg_feature_table(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    try:
        cur = conn.execute(f'SELECT * FROM "{table}"')
        cols = [d[0] for d in cur.description]
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            for row in rows:
                item: dict[str, Any] = {}
                for name, value in zip(cols, row):
                    if name == geom_col:
                        if isinstance(value, (bytes, bytearray)):
                            _, envelope, body = strip_gpkg_header(bytes(value))
                            item["__wkb"] = body
                            item["__env"] = envelope
                        else:
                            item["__wkb"] = None
                            item["__env"] = None
                    elif name != "fid":
                        item[name] = value
                yield item
    finally:
        conn.close()
