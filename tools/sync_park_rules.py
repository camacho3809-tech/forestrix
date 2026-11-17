#!/usr/bin/env python3
"""Refresh the generated Firestore park catalog from the checked-in JSON files."""
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = "    // BEGIN GENERATED PARK CATALOG"
END = "    // END GENERATED PARK CATALOG"


def main():
    rules = ROOT / "firestore.rules"
    body = rules.read_text()
    areas, reserved, weekly, bounds = {}, [], {}, {}
    for file in sorted((ROOT / "data").glob("*.json")):
        park = json.loads(file.read_text())
        key = park["park"]
        polygons = park["outline"]["coordinates"]
        if park["outline"]["type"] == "Polygon":
            polygons = [polygons]
        coordinates = [point for polygon in polygons for ring in polygon for point in ring]
        longitude, latitude = zip(*coordinates)
        bounds[key] = [min(latitude),max(latitude),min(longitude),max(longitude),
                       round(111320 * math.cos(math.radians(park["center"][0])))]
        for tree in park["trees"]:
            if tree["game"]:
                areas[tree["id"]] = [round(tree["lat"],7),round(tree["lon"],7),key]
        for zone in park["zones"]:
            if zone["weeklyTree"]:
                reserved.append(zone["weeklyTree"])
                weekly[key+"--"+zone["id"]] = zone["weeklyTree"]
        if park.get("weeklyTree"):
            reserved.append(park["weeklyTree"])
            if key != "oxford":
                weekly[key+"--park"] = park["weeklyTree"]
    pretty = lambda obj: json.dumps(obj,ensure_ascii=True,separators=(",", ":")).replace('"',"'")
    groups = [[] for _ in range(64)]
    for key,value in sorted(areas.items()):
        groups[int(key.rsplit("-",1)[-1]) % len(groups)].append(pretty(key)+":"+pretty(value))
    def bucket_choice(lower, upper):
        if upper-lower == 1:
            return "{"+",".join(groups[lower])+"}"
        middle = (lower+upper)//2
        return f"bucket < {middle} ? ({bucket_choice(lower,middle)}) : ({bucket_choice(middle,upper)})"
    lines = [
        START,
        "    function gameBucket(bucket) { return "+bucket_choice(0,len(groups))+"; }",
        "    function reservedTree(id) { return id in "+pretty(sorted(reserved))+"; }",
        "    function parkBounds() { return "+pretty(bounds)+"; }",
        "    function weeklyScopes() { return "+pretty(weekly)+"; }",
        END,
    ]
    first,last=body.index(START),body.index(END)+len(END)
    rules.write_text(body[:first]+"\n".join(lines)+body[last:])


if __name__ == "__main__":
    main()
