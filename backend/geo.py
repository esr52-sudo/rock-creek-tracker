"""Projection helpers. All distance math happens in UTM 18N (meters),
which covers Washington, DC."""
from pyproj import Transformer
from shapely.ops import transform as _transform

_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)
_TO_WGS = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True)


def to_utm(geom):
    return _transform(_TO_UTM.transform, geom)


def to_wgs(geom):
    return _transform(_TO_WGS.transform, geom)
