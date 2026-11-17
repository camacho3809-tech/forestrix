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
from shapely import STRtree, make_valid, voronoi_polygons
from shapely.geometry import LineString, MultiPoint, Point, Polygon, box, mapping, shape
from shapely.ops import nearest_points, polygonize, transform, unary_union

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


def fetch_osm(bounds, park):
    west, south, east, north = bounds
    extent = f"({south:.6f},{west:.6f},{north:.6f},{east:.6f})"
    filters = (
        'node["natural"="tree"]',
        'way["highway"~"^(' + "|".join(sorted(PATH_TYPES)) + ')$"]',
        'way["highway"~"^(' + "|".join(sorted(ROAD_TYPES)) + ')$"]',
        'way["name"]["leisure"~"^(park|garden|horse_riding|sports_centre|track|nature_reserve)$"]',
        'nwr["name"]["natural"~"^(water|wood)$"]',
        'way["natural"="wood"]',
        'way["landuse"="forest"]',
        'way["leisure"~"^(sports_centre|stadium|horse_riding|golf_course|pitch|track)$"]',
        'way["access"~"^(private|no|customers|permit)$"]',
        'way["fee"]["fee"!="no"]',
        'way["name"]["landuse"="recreation_ground"]',
    )
    fixed = defaultdict(set)
    for _, _, kind, identifier in park.get("landmarks",[]):
        fixed[kind].add(identifier)
    fixed["way"].update(park.get("excludedWays",[]))
    explicit = [kind+"(id:"+",".join(map(str,sorted(ids)))+")" for kind,ids in fixed.items() if ids]
    query = "[out:json][timeout:25];(" + ";".join([*(f + extent for f in filters),*explicit]) + ";);out geom;"
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


LIMITED_ACCESS = {"private", "no", "customers", "permit", "members", "staff", "military"}
SPORTS_GROUNDS = {"sports_centre", "stadium", "horse_riding", "golf_course", "pitch", "track"}


def osm_shapes(elements, project):
    nodes = {item["id"]: (item["lon"], item["lat"]) for item in elements if item["type"] == "node"}
    ways = {item["id"]: item for item in elements if item["type"] == "way"}
    for item in {(row["type"], row["id"]):row for row in elements}.values():
        kind, tags = item["type"], item.get("tags", {})
        if kind == "way":
            points = geometry_of(item, nodes)
            if len(points)>=4 and points[0]==points[-1]:
                yield item, make_valid(transform(project, Polygon(points))).buffer(0)
        elif kind == "relation" and tags.get("type") == "multipolygon":
            parts = defaultdict(list)
            for member in item.get("members", []):
                if member["type"] != "way":
                    continue
                points = geometry_of(member if member.get("geometry") else ways.get(member["ref"], {}), nodes)
                if len(points)>=2:
                    parts[member.get("role") or "outer"].append(LineString(points))
            exterior = unary_union(list(polygonize(parts["outer"])))
            interior = unary_union(list(polygonize(parts["inner"])))
            if not exterior.is_empty:
                yield item, transform(project, exterior.difference(interior))


def access_layers(elements, boundary, project, park):
    restricted, water, woods = [], [], []
    anchors = {}
    ids = set(park["excludedWays"])
    landmarks = {(kind, number) for _, _, kind, number in park["landmarks"]}
    for item, area in osm_shapes(elements, project):
        key = item["type"], item["id"]
        tags = item.get("tags", {})
        if area.is_empty or not boundary.intersects(area):
            continue
        if key in landmarks:
            anchors[key] = area.centroid
        if (item["type"]=="way" and item["id"] in ids) or (
            tags.get("access") in LIMITED_ACCESS or tags.get("foot") in LIMITED_ACCESS
            or tags.get("leisure") in SPORTS_GROUNDS or tags.get("landuse") in {"military","greenhouse_horticulture"}
            or tags.get("fee") not in {None,"no"}
        ):
            restricted.append(area)
        if tags.get("natural")=="water" or "water" in tags:
            water.append(area)
        if tags.get("natural")=="wood" or tags.get("landuse")=="forest":
            woods.append(area)
    for node in (item for item in elements if item["type"]=="node" and ("node",item["id"]) in landmarks):
        anchors["node",node["id"]]=Point(project(node["lon"],node["lat"]))
    return (unary_union(restricted).buffer(4).intersection(boundary),
            unary_union(water).intersection(boundary), unary_union(woods).intersection(boundary), anchors)


def landmark_zones(park, boundary, anchors):
    landmarks = [(key,label,anchors[kind,identifier]) for key,label,kind,identifier in park["landmarks"] if (kind,identifier) in anchors]
    if len(landmarks)!=len(park["landmarks"]):
        missing=[label for _,label,kind,identifier in park["landmarks"] if (kind,identifier) not in anchors]
        raise ValueError("Missing mapped landmarks: "+", ".join(missing))
    cells = voronoi_polygons(MultiPoint([point for _,_,point in landmarks]), extend_to=boundary.envelope).geoms
    return {key:(label,next(cell for cell in cells if cell.covers(point)).intersection(boundary))
            for key,label,point in landmarks}


def public_groves(path_network, woods, excluded, roads, inverse, existing, project):
    reachable_wood=woods.difference(excluded.union(roads.buffer(10.5)))
    corridors=path_network.intersection(reachable_wood.buffer(12))
    if corridors.is_empty:
        return []
    position_index=STRtree([Point(project(tree["lon"],tree["lat"])) for tree in existing])
    masked=excluded.union(roads.buffer(10.5))
    west,south,east,north=corridors.bounds
    candidates=[]
    for x in range(math.floor(west/63),math.ceil(east/63)):
        for y in range(math.floor(south/63),math.ceil(north/63)):
            square=box(x*63,y*63,(x+1)*63,(y+1)*63)
            line=corridors.intersection(square)
            if line.length<8:
                continue
            on_path=nearest_points(square.centroid,line)[1]
            point=nearest_points(on_path,reachable_wood)[1]
            if point.distance(on_path)>12.1 or masked.covers(point) or point.distance(position_index.geometries[position_index.nearest(point)])<12:
                continue
            lon,lat=inverse(point.x,point.y)
            identifier=str(int.from_bytes(hashlib.sha256(f"{x},{y}".encode()).digest()[:5],"big"))
            candidates.append({"id":"paris-grove-"+identifier,"lat":lat,"lon":lon,"kind":"grove",
                               "remarkable":False,"tags":{}})
    return candidates


def osm_geometry(elements, boundary, project, restricted=None):
    nodes = {row["id"]: (row["lon"], row["lat"]) for row in elements if row["type"] == "node"}
    paths, roads, named = [], [], []
    park_margin=boundary.buffer(30)
    for way in {row["id"]: row for row in elements if row["type"] == "way"}.values():
        tags = way.get("tags", {})
        points = geometry_of(way, nodes)
        if len(points) < 2:
            continue
        hwy = tags.get("highway")
        if hwy in PATH_TYPES | ROAD_TYPES:
            line = transform(project, LineString(points)).intersection(park_margin)
            if line.is_empty:
                continue
            if hwy in PATH_TYPES and tags.get("foot") not in LIMITED_ACCESS and tags.get("access") not in LIMITED_ACCESS and tags.get("fee") in {None,"no"}:
                public = line if restricted is None else line.difference(restricted)
                if public.length:
                    paths.append(public)
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


def select_trees(trees, zones, paths, roads, project, reserved, restricted=None, max_per_zone=60, preferred=()):
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
        reason = ("restricted" if restricted is not None and restricted.covers(point) else
                  "road" if near_road < 10 else "path" if near_path > 20 else "")
        tree.update(zone=zone, pathM=round(near_path,1), roadM=round(near_road,1), score=score, game=False, reason=reason)
    accepted = []
    used = Counter()
    preferred=set(preferred)
    ordered = sorted(trees, key=lambda entry: (entry.get("kind")=="grove",entry["id"] not in preferred,-entry["score"],entry["id"]))
    for tree in sorted(ordered,key=lambda entry:not(entry["remarkable"] or entry["id"]==reserved)):
        special = tree["remarkable"] or tree["id"]==reserved
        if tree["reason"]=="restricted" or (restricted is not None and tree["reason"]) or (not special and (tree["reason"] or used[tree["zone"]] >= max_per_zone)):
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


def preview(path, park, trees, zones, paths, roads, inverse, restricted=None):
    chart = folium.Map(location=park["center"],zoom_start=14,tiles="OpenStreetMap")
    for title, lines, color in [("Chemins piétons", paths, "#707878"), ("Routes", roads, "#202020")]:
        folium.GeoJson({"type":"FeatureCollection","features":[feature(transform(inverse,unary_union(lines).simplify(1.75)))]},
                       name=title,style_function=lambda _, color=color:{"color":color,"weight":2}).add_to(chart)
    folium.GeoJson({"type":"FeatureCollection","features":[feature(transform(inverse,area.simplify(4,preserve_topology=True)),zone=name) for name,area in zones.values()]},
                   name="Zones",style_function=lambda _:{"color":"#485cb2","weight":2,"fillOpacity":.03},
                   tooltip=folium.GeoJsonTooltip(fields=["zone"])).add_to(chart)
    if restricted is not None:
        folium.GeoJson(feature(transform(inverse,restricted.simplify(1.5))),name="Emprises à accès restreint",
                       style_function=lambda _:{"color":"#9c3348","weight":1.5,"fillOpacity":.27}).add_to(chart)
        chart.get_root().html.add_child(folium.Element('<div style="position:fixed;bottom:24px;left:12px;z-index:900;background:white;padding:4px">Arbres : <a href="https://opendata.paris.fr/explore/dataset/les-arbres/">Ville de Paris</a> · ODbL</div>'))
    for title, color, predicate in [
        ("Arbres de jeu", "#237542", lambda t:t["game"] and not t["remarkable"] and t.get("kind")!="grove"),
        ("Bosquets de jeu", "#146f86", lambda t:t["game"] and t.get("kind")=="grove"),
        ("Herbier", "#777e78", lambda t:not t["game"] and t["reason"] not in {"road","restricted"}),
        ("Écartés : accès restreint", "#9c3348", lambda t:not t["game"] and t["reason"]=="restricted"),
        ("Écartés : route", "#c13932", lambda t:not t["game"] and t["reason"]=="road"),
        ("Arbres remarquables de jeu", "#c99300", lambda t:t["remarkable"] and t["game"]),
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
    osm = load_or_fetch(args.cache,prefix+"osm.json",lambda:fetch_osm((west-.001,south-.001,east+.001,north+.001),park))["elements"]
    boundary = transform(project,outline)
    restricted = None
    if park.get("excludedWays"):
        restricted, water, woods, anchors = access_layers(osm, boundary, project, park)
    paths, roads, named = osm_geometry(osm,boundary,project,restricted)
    zones = landmark_zones(park,boundary,anchors) if park.get("landmarks") else make_zones(park,boundary,named)
    if park["source"]=="paris":
        municipal = load_or_fetch(args.cache,prefix+"municipal.json",lambda:download(
            PARIS_API+"les-arbres/exports/json",where="arrondissement = 'BOIS DE BOULOGNE' and typeemplacement = 'Arbre'"))
        remarkable = load_or_fetch(args.cache,prefix+"remarkables.json",lambda:download(
            PARIS_API+"arbresremarquablesparis/exports/json",where="arbres_arrondissement = 'BOIS DE BOULOGNE'"))
        trees = list(paris_rows(municipal,remarkable,outline))
        municipal_index=STRtree([Point(project(tree["lon"],tree["lat"])) for tree in trees])
        for tree in osm_rows(osm,outline,"paris-osm-"):
            point=Point(project(tree["lon"],tree["lat"]))
            if point.distance(municipal_index.geometries[municipal_index.nearest(point)])>=8:
                trees.append(tree)
        road_network=unary_union(roads)
        trees.extend(public_groves(unary_union(paths), woods, restricted.union(water), road_network, inverse, trees,project))
    else:
        trees = list(osm_rows(osm,outline,park.get("idPrefix",prefix)))
    trees = select_trees(trees,zones,paths,roads,project,park.get("weeklyTree"),restricted.union(water) if restricted is not None else None,park.get("maxPerZone",60),park.get("preferredGameIds",()))
    # Keep the existing municipal herbarium. Publish only playable additions.
    trees = [tree for tree in trees if tree["game"] or (tree.get("kind")!="grove" and not tree["id"].startswith("paris-osm-"))]
    zone_data=[]
    game_zones=Counter(t["zone"] for t in trees if t["game"])
    # Give each retained zone room for both its weekly tree and ordinary play.
    if len(game_zones)>3 and not park.get("landmarks"):
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
        weekly=next((t["id"] for t in sorted(trees,key=lambda t:(t.get("kind")=="grove",-t["score"],t["id"])) if t["zone"]==key and t["game"]),None)
        zone_data.append(dict(id=key,name=name,geometry=mapping(transform(inverse,area.simplify(3))),weeklyTree=weekly if per_zone else None))
    unique_species={t["tags"]["species"].strip().casefold() for t in trees if t["tags"].get("species")}
    result = {"park":args.park,"source":park["source"],"center":park["center"],"outline":mapping(outline),"trees":trees,"zones":zone_data,"byZone":per_zone,"weeklyTree":weekly_tree if not per_zone else None,
              "summary":{"total":len(trees),"game":sum(t["game"] for t in trees),"zones":len(game_zones),"species":len(unique_species),"remarkable":sum(t["remarkable"] for t in trees)}}
    if park.get("excludedWays"):
        result["revision"]="public-paths-v1"
    (args.out/f"{args.park}.json").write_text(json.dumps(result,ensure_ascii=False,separators=(",", ":"))+"\n")
    preview(args.out/f"{args.park}-preview.html",park,trees,{key:value for key,value in zones.items() if key in populated},paths,roads,inverse,restricted)
    print(json.dumps({"summary":result["summary"],"game_by_zone":dict(sorted(game_zones.items()))},ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
