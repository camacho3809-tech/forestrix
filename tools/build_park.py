#!/usr/bin/env python3
"""Build Forestrix's catalog and Folium inspection map from open geodata."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import folium
from pyproj import Transformer
from shapely import STRtree, make_valid
from shapely.geometry import LineString, Point, Polygon, box, mapping, shape
from shapely.ops import polygonize, transform, unary_union

OVERPASS = ("https://overpass.private.coffee/api/interpreter", "https://overpass-api.de/api/interpreter")
PARIS_API = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/"
PATH_TYPES = {"footway", "path", "pedestrian", "track", "bridleway", "cycleway"}
ROAD_TYPES = {"primary", "secondary", "tertiary", "unclassified", "residential", "service",
              "primary_link", "secondary_link", "tertiary_link", "trunk", "trunk_link", "motorway", "motorway_link"}
ROOT = Path(__file__).resolve().parents[1]
PARKS = json.loads((ROOT / "tools/parks.json").read_text())


def download(url, **params):
    suffix = "?" + urlencode(params) if params else ""
    request = Request(url + suffix, headers={"User-Agent": "Forestrix park builder"})
    with urlopen(request, timeout=32) as response:
        return json.load(response)


def load_or_fetch(cache, name, fetch):
    path = cache / name
    if path.exists():
        return json.loads(path.read_text())
    data = fetch()
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return data


def fetch_outline(park):
    item = download(
        "https://nominatim.openstreetmap.org/lookup",
        osm_ids=park["osm"], format="jsonv2", polygon_geojson=1,
    )[0]
    return item


def fetch_osm(bounds):
    west, south, east, north = bounds
    extent = f"({south:.6f},{west:.6f},{north:.6f},{east:.6f})"
    filters = (
        'node["natural"="tree"]',
        'way["highway"~"^(' + "|".join(sorted(PATH_TYPES)) + ')$"]',
        'way["highway"~"^(' + "|".join(sorted(ROAD_TYPES)) + ')$"]',
        'way["name"]["leisure"~"^(park|garden|horse_riding|sports_centre|track|nature_reserve)$"]',
        'nwr["name"]["natural"~"^(water|wood)$"]',
        'way["name"]["landuse"="recreation_ground"]',
    )
    query = "[out:json][timeout:25];(" + ";".join(f + extent for f in filters) + ";);out geom;"
    for server in OVERPASS:
        try:
            data = download(server, data=query)
            if data.get("remark") or not data.get("elements"):
                raise ValueError("Incomplete Overpass data")
            return data
        except (OSError, ValueError) as error:
            print(f"Overpass {server}: {error}")
    print("Overpass unavailable. Using the OSM map API for the same features.")
    cols, rows = max(1, math.ceil((east-west)/.013)), max(1, math.ceil((north-south)/.012))
    bounds_list = [
        (west+(east-west)*x/cols, south+(north-south)*y/rows,
         west+(east-west)*(x+1)/cols+1e-6, south+(north-south)*(y+1)/rows+1e-6)
        for y in range(rows) for x in range(cols)
    ]
    def tile(tile_bounds):
        return download("https://api.openstreetmap.org/api/0.6/map.json",
                        bbox=",".join(f"{value:.7f}" for value in tile_bounds))["elements"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = [item for response in pool.map(tile, bounds_list) for item in response]
    return {"elements": items}


def geometry_of(way, nodes):
    if way.get("geometry"):
        return [(p["lon"], p["lat"]) for p in way["geometry"] if p]
    return [nodes[node] for node in way.get("nodes", []) if node in nodes]


def osm_geometry(elements, boundary, project):
    nodes = {row["id"]: (row["lon"], row["lat"]) for row in elements if row["type"] == "node"}
    paths, roads, named = [], [], []
    for way in {row["id"]: row for row in elements if row["type"] == "way"}.values():
        tags = way.get("tags", {})
        points = geometry_of(way, nodes)
        if len(points) < 2:
            continue
        hwy = tags.get("highway")
        if hwy in PATH_TYPES | ROAD_TYPES:
            line = transform(project, LineString(points)).intersection(boundary.buffer(30))
            if line.is_empty:
                continue
            if hwy in PATH_TYPES and tags.get("foot") not in {"no", "private"} and tags.get("access") not in {"no", "private"}:
                paths.append(line)
            if hwy in ROAD_TYPES and tags.get("motor_vehicle") not in {"no", "private"} and tags.get("access") not in {"no", "private"} and tags.get("service") not in {"driveway", "parking_aisle", "emergency_access"}:
                roads.append(line)
        if (tags.get("name") and len(points) >= 4 and points[0] == points[-1] and
                (tags.get("leisure") in {"garden", "horse_riding", "sports_centre", "track", "park", "nature_reserve"}
                 or tags.get("natural") in {"water", "wood"} or tags.get("landuse") == "recreation_ground")):
            area = make_valid(transform(project, Polygon(points)))
            if 10_000 <= area.area < boundary.area * .65 and boundary.covers(area.representative_point()):
                named.append((way["id"], tags.get("name:fr", tags["name"]), area))
    ways = {row["id"]:row for row in elements if row["type"]=="way"}
    relations = {row["id"]:row for row in elements if row["type"]=="relation"}.values()
    for relation in relations:
        tags=relation.get("tags",{})
        if not tags.get("name") or tags.get("natural")!="water":
            continue
        rings=[geometry_of(member if member.get("geometry") else ways.get(member["ref"],{}),nodes)
               for member in relation.get("members",[]) if member["type"]=="way" and member.get("role")=="outer"]
        parts=list(polygonize([LineString(ring) for ring in rings if len(ring)>=2]))
        if parts:
            area=transform(project,unary_union(parts))
            if 10_000 <= area.area < boundary.area * .65 and boundary.covers(area.representative_point()):
                named.append((relation["id"],tags.get("name:fr",tags["name"]),area))
    if not paths or not roads:
        raise ValueError("Missing pedestrian paths or roads. Check the OSM extract before publishing.")
    return paths, roads, sorted(named, key=lambda item: (item[2].area, item[0]))


def metres(raw):
    match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(m|ft)?\s*", str(raw))
    return float(match[1].replace(",", ".")) * (.3048 if match[2] == "ft" else 1) if match and float(match[1].replace(",", ".")) > 0 else None


def paris_rows(municipal, remarkable, outline):
    def location(row, key):
        point = row.get(key)
        return point if point and outline.covers(Point(point["lon"], point["lat"])) else None
    cells, positions = defaultdict(list), set()
    priority = lambda row: (row.get("remarquable") != "OUI", hashlib.sha256(str(row["idbase"]).encode()).digest())
    for row in sorted(municipal, key=priority):
        point = location(row, "geo_point_2d")
        if not point or row.get("domanialite") != "Alignement":
            continue
        lat, lon = round(point["lat"], 6), round(point["lon"], 6)
        if (lat, lon) in positions:
            continue
        positions.add((lat, lon))
        cells[math.floor((lat-48.84)*111320/105), math.floor((lon-2.22)*73200/105)].append(row)
    selected = {int(row["idbase"]): row for cell in cells.values() for row in cell[:4]}
    labels = {}
    for row in remarkable:
        point = location(row, "geom_x_y")
        if not point:
            continue
        identifier = int(row.get("arbres_idbase") or row["com_idarbre"])
        labels[identifier] = row
        selected.setdefault(identifier, {
            "geo_point_2d": point, "genre": row.get("arbres_genre"), "espece": row.get("arbres_espece"),
            "hauteurenm": row.get("arbres_hauteurenm"), "libellefrancais": row.get("arbres_libellefrancais"),
        })
    for identifier, row in sorted(selected.items()):
        point = row["geo_point_2d"]
        genus, species = row.get("genre") or "", row.get("espece") or ""
        name = genus + " " + species if genus and species.lower() not in {"", "sp", "sp.", "spp", "spp.", "x", "non renseigné", "indéterminé"} else ""
        label = labels.get(identifier, {})
        yield {"id": f"paris-{identifier}", "lat": point["lat"], "lon": point["lon"], "commonName": label.get("com_nom_usuel") or row.get("libellefrancais") or "",
               "remarkable": identifier in labels or row.get("remarquable") == "OUI",
               "tags": {"natural":"tree", "genus":genus, "species":label.get("com_nom_latin") or name, "height":metres(row.get("hauteurenm"))}}


def osm_rows(elements, outline, prefix):
    rows = {row["id"]: row for row in elements if row["type"]=="node" and row.get("tags", {}).get("natural")=="tree" and outline.covers(Point(row["lon"],row["lat"]))}
    for identifier, row in sorted(rows.items()):
        tags = row["tags"]
        yield {"id":f"{prefix}{identifier}","lat":row["lat"],"lon":row["lon"],"remarkable":tags.get("denotation")=="natural_monument",
               "tags":{key:tags[key] for key in ("natural","species","genus","height","age","planting_date","start_date") if key in tags}}


def make_zones(park, boundary, named):
    if not park["zones"]:
        return {"park": (park["name"], boundary)}
    left = boundary
    zones = {}
    for identifier, label, area in named:
        district = left.intersection(area.buffer(100))
        if district.area >= 12_500:
            zones[f"osm-{identifier}"] = (label, district)
            left = left.difference(district)
    west, south, east, north = left.bounds
    for y in range(math.floor(south/500), math.ceil(north/500)):
        for x in range(math.floor(west/500), math.ceil(east/500)):
            district = left.intersection(box(x*500,y*500,(x+1)*500,(y+1)*500))
            if district.area:
                zones[f"g-{x}-{y}"] = (f"Secteur {x-math.floor(west/500)+1}-{y-math.floor(south/500)+1}",district)
    return zones


def select_trees(trees, zones, paths, roads, project, reserved):
    path_index, road_index = STRtree(paths), STRtree(roads)
    counts = Counter(t["tags"].get("species","").strip().casefold() for t in trees)
    positions = {}
    for tree in trees:
        identifier = tree["id"]
        point = Point(project(tree["lon"], tree["lat"]))
        positions[identifier] = point
        zone = next((key for key, (_, area) in zones.items() if area.covers(point)), None)
        if zone is None:
            zone = min(zones, key=lambda key: zones[key][1].distance(point))
        near_path = paths[path_index.nearest(point)].distance(point)
        near_road = roads[road_index.nearest(point)].distance(point)
        height = metres(tree["tags"].get("height")) or 0
        species = tree["tags"].get("species","").strip().casefold()
        rarity = (3 if counts[species]<5 else 1 if counts[species]<20 else 0) if species else 0
        score = min(3,int(height//6)) + rarity + (10 if tree["remarkable"] else 0)
        reason = "road" if near_road < 10 else "path" if near_path > 20 else ""
        tree.update(zone=zone, pathM=round(near_path,1), roadM=round(near_road,1), score=score, game=False, reason=reason)
    accepted = []
    used = Counter()
    ordered = sorted(trees, key=lambda entry: (-entry["score"], entry["id"]))
    for tree in sorted(ordered,key=lambda entry:not(entry["remarkable"] or entry["id"]==reserved)):
        special = tree["remarkable"] or tree["id"]==reserved
        if not special and (tree["reason"] or used[tree["zone"]] >= 60):
            continue
        point = positions[tree["id"]]
        if not special and any(point.distance(other)<45 for other in accepted):
            tree["reason"] = "distance"
            continue
        tree["game"] = True
        accepted.append(point)
        used[tree["zone"]] += 1
    return sorted(trees,key=lambda tree:tree["id"])


def feature(geom, **properties):
    def round_coords(value):
        if isinstance(value, (list, tuple)):
            return [round_coords(item) for item in value]
        return round(value, 6) if isinstance(value, float) else value
    data = mapping(geom)
    data["coordinates"] = round_coords(data["coordinates"])
    return {"type":"Feature","geometry":data,"properties":properties}


def preview(path, park, trees, zones, paths, roads, inverse):
    chart = folium.Map(location=park["center"],zoom_start=14,tiles="OpenStreetMap")
    for title, lines, color in [("Chemins piétons", paths, "#707878"), ("Routes", roads, "#202020")]:
        folium.GeoJson({"type":"FeatureCollection","features":[feature(transform(inverse,unary_union(lines).simplify(1.75)))]},
                       name=title,style_function=lambda _, color=color:{"color":color,"weight":2}).add_to(chart)
    folium.GeoJson({"type":"FeatureCollection","features":[feature(transform(inverse,area.simplify(4,preserve_topology=True)),zone=name) for name,area in zones.values()]},
                   name="Zones",style_function=lambda _:{"color":"#485cb2","weight":2,"fillOpacity":.03},
                   tooltip=folium.GeoJsonTooltip(fields=["zone"])).add_to(chart)
    for title, color, predicate in [
        ("Arbres de jeu", "#237542", lambda t:t["game"] and not t["remarkable"]),
        ("Herbier", "#777e78", lambda t:not t["game"] and t["reason"]!="road"),
        ("Écartés : route", "#c13932", lambda t:not t["game"] and t["reason"]=="road"),
        ("Arbres remarquables", "#c99300", lambda t:t["remarkable"]),
    ]:
        entries = [feature(Point(t["lon"],t["lat"]),id=t["id"],espece=t["tags"].get("species",""),chemin=t["pathM"],route=t["roadM"]) for t in trees if predicate(t)]
        if entries:
            folium.GeoJson({"type":"FeatureCollection","features":entries},name=title,
                           marker=folium.CircleMarker(radius=4,color=color,weight=1,fill=True,fill_color=color,fill_opacity=.9),
                           tooltip=folium.GeoJsonTooltip(fields=["id","espece","chemin","route"])).add_to(chart)
    folium.LayerControl().add_to(chart)
    chart.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("park",choices=PARKS)
    parser.add_argument("--cache",type=Path,default=ROOT/".park-cache")
    parser.add_argument("--out",type=Path,default=ROOT/"data")
    args = parser.parse_args()
    park = PARKS[args.park]
    args.cache.mkdir(parents=True,exist_ok=True)
    args.out.mkdir(parents=True,exist_ok=True)
    prefix = args.park+"-"
    outline = make_valid(shape(load_or_fetch(args.cache,prefix+"outline.json",lambda:fetch_outline(park))["geojson"]))
    project = Transformer.from_crs(4326,park["epsg"],always_xy=True).transform
    inverse = Transformer.from_crs(park["epsg"],4326,always_xy=True).transform
    west,south,east,north = outline.bounds
    osm = load_or_fetch(args.cache,prefix+"osm.json",lambda:fetch_osm((west-.001,south-.001,east+.001,north+.001)))["elements"]
    boundary = transform(project,outline)
    paths, roads, named = osm_geometry(osm,boundary,project)
    zones = make_zones(park,boundary,named)
    if park["source"]=="paris":
        municipal = load_or_fetch(args.cache,prefix+"municipal.json",lambda:download(
            PARIS_API+"les-arbres/exports/json",where="arrondissement = 'BOIS DE BOULOGNE' and typeemplacement = 'Arbre'"))
        remarkable = load_or_fetch(args.cache,prefix+"remarkables.json",lambda:download(
            PARIS_API+"arbresremarquablesparis/exports/json",where="arbres_arrondissement = 'BOIS DE BOULOGNE'"))
        trees = list(paris_rows(municipal,remarkable,outline))
    else:
        trees = list(osm_rows(osm,outline,park.get("idPrefix",prefix)))
    trees = select_trees(trees,zones,paths,roads,project,park.get("weeklyTree"))
    zone_data=[]
    game_zones=Counter(t["zone"] for t in trees if t["game"])
    # Give each retained zone room for both its weekly tree and ordinary play.
    if len(game_zones)>3:
        anchors={key for key,count in game_zones.items() if count>=3}
        for key in list(zones):
            if key in anchors:
                continue
            candidates=[other for other in anchors if game_zones[other]+game_zones[key]<=60]
            if not candidates:
                continue
            area=zones[key][1]
            near=min(candidates,key=lambda other:(area.representative_point().distance(zones[other][1]), other))
            zones[near]=(zones[near][0],zones[near][1].union(area).buffer(0))
            del zones[key]
            for tree in trees:
                if tree["zone"]==key:
                    tree["zone"]=near
            game_zones[near]+=game_zones.pop(key,0)
    populated={t["zone"] for t in trees}
    per_zone=len(game_zones)>3
    weekly_tree=park.get("weeklyTree")
    if not per_zone and not any(t["id"]==weekly_tree and t["game"] for t in trees):
        weekly_tree=next((t["id"] for t in sorted(trees,key=lambda t:(-t["score"],t["id"])) if t["game"]),None)
    for key,(name,area) in zones.items():
        if key not in populated:
            continue
        weekly=next((t["id"] for t in sorted(trees,key=lambda t:(-t["score"],t["id"])) if t["zone"]==key and t["game"]),None)
        zone_data.append(dict(id=key,name=name,geometry=mapping(transform(inverse,area.simplify(3))),weeklyTree=weekly if per_zone else None))
    unique_species={t["tags"]["species"].strip().casefold() for t in trees if t["tags"].get("species")}
    result = {"park":args.park,"source":park["source"],"center":park["center"],"outline":mapping(outline),"trees":trees,"zones":zone_data,"byZone":per_zone,"weeklyTree":weekly_tree if not per_zone else None,
              "summary":{"total":len(trees),"game":sum(t["game"] for t in trees),"zones":len(game_zones),"species":len(unique_species),"remarkable":sum(t["remarkable"] for t in trees)}}
    (args.out/f"{args.park}.json").write_text(json.dumps(result,ensure_ascii=False,separators=(",", ":"))+"\n")
    preview(args.out/f"{args.park}-preview.html",park,trees,{key:value for key,value in zones.items() if key in populated},paths,roads,inverse)
    print(json.dumps({"summary":result["summary"],"game_by_zone":dict(sorted(game_zones.items()))},ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
