import math
from collections import defaultdict
from contextlib import asynccontextmanager

import pickle
import os

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import h3
from shapely.geometry import Polygon

# 1. Генерация датасета 

DATASET_DICT = {}
AVG_CACHE = {}

def generate_dataset():
    """Генерация датасета"""
    center_lat, center_lng = 56.0, 38.0
    radius_km = 7.0+0.07
    EARTH_RADIUS_KM = 6371.0

    points = []
    for i in range(64):
        angle = math.radians(i * 360 / 64)
        dlat = (radius_km / EARTH_RADIUS_KM) * math.degrees(math.cos(angle))
        dlng = (radius_km / (EARTH_RADIUS_KM * math.cos(math.radians(center_lat)))) * math.degrees(math.sin(angle))
        points.append((center_lat + dlat, center_lng + dlng))

    h3_poly = h3.LatLngPoly(points)

    cells = h3.polygon_to_cells(h3_poly, res=12)
    
    dataset_dict = {}
    center = (center_lat, center_lng)
    
    for c in cells:
        c_lat, c_lng = h3.cell_to_latlng(c)
        if h3.great_circle_distance(center, (c_lat, c_lng), unit='km') < 7.0:
            h_int = int(c, 16)
            base = h_int // 512
            level = (base % 74) - 120
            cell_id = (base % 100) + 1
            dataset_dict[c] = (level, cell_id)
            
    return dataset_dict


CACHE_FILE = "h3_dataset_cache.pkl"

def load_or_build():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'rb') as f:
            return pickle.load(f)
    
    data = generate_dataset()
    
    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(data, f)
    
    return data

@asynccontextmanager
async def lifespan(app: FastAPI):
    global DATASET_DICT
    DATASET_DICT = load_or_build()
    yield

app = FastAPI(title="H3 High-Performance Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. API Endpoints

HEX_INDEX = {}

@app.get("/hex")
def get_hex(hex: str):
    hex = hex.strip().rstrip('\\')
    if not h3.is_valid_cell(hex):
        print (hex)
        raise HTTPException(status_code=400, detail="Ошибка в  индексе")
    res = h3.get_resolution(hex)
    
    if res == 12:
        if hex in DATASET_DICT:
            level, cell_id = DATASET_DICT[hex]
            return [[hex, level, cell_id]]
        return []
    
    if res not in HEX_INDEX:
        idx = defaultdict(list)
        for c in DATASET_DICT:
            idx[h3.cell_to_parent(c, res=res)].append(c)
        HEX_INDEX[res] = idx
    
    return [
        [c, *DATASET_DICT[c]]
        for c in HEX_INDEX[res].get(hex, [])
    ]

@app.get("/avg")
def get_avg(resolution: int = Query(..., ge=0, le=12, description="0-12")):
    if resolution in AVG_CACHE:
        return AVG_CACHE[resolution]
    
    groups = defaultdict(list)
    for c, (level, cell_id) in DATASET_DICT.items():
        parent = h3.cell_to_parent(c, res=resolution)
        groups[(parent, cell_id)].append(level)
    
    res_result = []
    for (parent, cell_id), levels in groups.items():
        levels.sort()
        n = len(levels)
        mid = n // 2
        if n % 2 == 0:
            med = (levels[mid-1] + levels[mid]) / 2.0
        else:
            med = float(levels[mid])
        
        res_result.append([parent, math.floor(med), cell_id])
    
    AVG_CACHE[resolution] = res_result
    return res_result


def get_bbox_results(border: str):
    try:
        coords = [tuple(map(float, p.strip().split('/'))) for p in border.split(',')]
    except ValueError:
        raise HTTPException(status_code=400, detail="Не правильная последовательность. Вводи lat/lng,lat/lng")
    
    if len(coords) < 3:
        raise HTTPException(status_code=400, detail="Мало координат для полигона")

    border_poly = Polygon([(lng, lat) for lat, lng in coords])

    h3_poly = h3.LatLngPoly(coords)
    candidates = h3.polygon_to_cells(h3_poly, res=12)
    
    results = []
    for c in candidates:
        if c not in DATASET_DICT:
            continue
            
        boundary = h3.cell_to_boundary(c)
        hex_poly = Polygon([(lng, lat) for lat, lng in boundary])
        
        if border_poly.covers(hex_poly):
            level, cell_id = DATASET_DICT[c]
            results.append((c, level, cell_id))
            
    return results


@app.get("/bbox")
def get_bbox(border: str = Query(..., description="Формат: lat/lng,lat/lng,lat/lng",examples="56.0/38.0,56.1/38.1,56.2/38.2")):
    results = get_bbox_results(border)
    return [[c, level, cell_id] for c, level, cell_id in results]


@app.get("/bbox_kml")
def get_bbox_kml(border: str = Query(..., description="Формат: lat/lng,lat/lng,lat/lng",examples="56.0/38.0,56.1/38.1,56.2/38.2")):
    results = get_bbox_results(border)
    
    def generate_kml():
        yield '<?xml version="1.0" encoding="UTF-8"?>\n'
        yield '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Hexagons</name>\n'
        for c, level, cell_id in results:
            boundary = h3.cell_to_boundary(c)
            closed = list(boundary) + [boundary[0]]
            coords_str = " ".join(f"{lng},{lat},0" for lat, lng in closed)#По спецификации KML <LinearRing> должен быть замкнут
            yield (
                f'<Placemark>'
                f'<name>{c}</name>'
                f'<Polygon><outerBoundaryIs><LinearRing>'
                f'<coordinates>{coords_str}</coordinates>'
                f'</LinearRing></outerBoundaryIs></Polygon>'
                f'</Placemark>\n'
            )
        yield '</Document></kml>'

    return StreamingResponse(
        generate_kml(),
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": "attachment; filename=hexagons.kml"}
    )