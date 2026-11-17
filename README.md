# Forestrix

Jeu web statique sur GitHub Pages. Le catalogue, les limites, les zones et les sélections proviennent de `data/<parc>.json`. Le navigateur met en cache le dernier catalogue chargé. Les aperçus Folium servent à inspecter les sélections avant de publier.

## Construire un parc

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r tools/requirements.txt
python tools/build_park.py paris
python tools/build_park.py oxford
python tools/sync_park_rules.py
```

Le script produit `data/paris.json` et `data/paris-preview.html` (ou les équivalents pour le parc demandé). Ouvre l’aperçu dans un navigateur, puis vérifie les chemins piétons, les routes et les arbres rejetés près de la chaussée. L’aperçu affiche aussi les zones et les distances mesurées. Pour actualiser les sources, utilise un dossier de cache vide avec `--cache`.

| Parc | Arbres | Limites, chemins et routes | Projection |
| --- | --- | --- | --- |
| Bois de Boulogne | [Arbres](https://opendata.paris.fr/explore/dataset/les-arbres/) et [Les arbres remarquables](https://opendata.paris.fr/explore/dataset/arbresremarquablesparis/) de la Ville de Paris | OpenStreetMap, relation 16731685 | Lambert-93, EPSG:2154 |
| University Parks | OpenStreetMap, nœuds `natural=tree` | OpenStreetMap, relation 12532311 | British National Grid, EPSG:27700 |
| Jardin Lecoq | OpenStreetMap, nœuds `natural=tree`. Le champ `municipalInventory` est prévu pour renseigner la future source municipale. | OpenStreetMap, chemin fermé 4245800 | Lambert-93, EPSG:2154 |

Les requêtes Overpass distinguent chemins piétons et routes. Si Overpass échoue, le script utilise l’API cartographique d’OSM. Les distances sont mesurées sur les géométries projetées. Les arbres ordinaires retenus se trouvent à 20 m ou moins d’un chemin et à 10 m ou plus d’une route, avec 45 m d’espacement.

À Paris, le générateur exclut des emplacements de jeu les enceintes sportives, les secteurs marqués privés, les zones payantes et les plans d’eau cartographiés. Les remarcables situés dans ces emprises restent à l’herbier. Les emprises nommées à contrôler avant une mise à jour sont dans `excludedWays`. L’aperçu affiche ce masque séparément. Les parcours sont découpés sur le bord public des emprises. Les anciens emplacements conformes sont prioritaires, puis les arbres OSM sont complétés par des bosquets représentés sur la lisière d’une surface boisée cartographiée à proximité d’un chemin public. Un bosquet représente un lieu de jeu et n’atteste pas d’une espèce.

Les zones de Paris portent le nom des repères définis dans `landmarks`. Le catalogue est limité à 30 emplacements jouables par zone. Le script `sync_park_rules.py` recopie la sélection et les arbres d’enchères dans les règles Firestore. Les données OSM et les inventaires de Paris sont sous licence ODbL.

### Exemple Jardin Lecoq, Clermont-Ferrand

1. Le parc est déjà défini dans `tools/parks.json`. Exécute `python tools/build_park.py clermont` et ouvre `data/clermont-preview.html`.
2. Après vérification, ajoute dans `PARKS` de `index.html` l’entrée `clermont:{name:'Jardin Lecoq',city:'Clermont-Ferrand',center:[45.7726,3.0879]}`. Le sélecteur la proposera avec le lien `?park=clermont`. Conserve la clef du parc pour préserver les identifiants des arbres.
3. Pour adopter un inventaire municipal, renseigne sa source dans la configuration et adapte le transformateur du script aux champs de l’API. Garde les identifiants existants pour préserver l’historique, ou prépare une correspondance avant de changer de source.
4. Exécute `python tools/sync_park_rules.py` pour synchroniser les coordonnées autorisées et les arbres réservés aux enchères.
5. Publie les fichiers dans GitHub, puis remplace les règles dans **Firestore Database → Règles** avec le contenu complet de `firestore.rules`.

Les comptes, l’herbier et les adhésions aux clans sont partagés. Les classements et les objectifs de clan sont calculés par parc. Les grandes cartes utilisent les zones pour les missions et enchères. Les règles valident les coordonnées envoyées pour une revendication ou un défi contre le catalogue. La provenance du signal GPS dépend de l’appareil.
