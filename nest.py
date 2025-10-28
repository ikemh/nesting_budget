# -*- coding: utf-8 -*-
# Versão com:
# - Índice espacial + prepared geometries (colisão rápida, mesma lógica).
# - Novas estratégias (8 no total): H/V com e sem alternância + as mesmas com peça pré-rotacionada 90°.
# - Competição entre estratégias (paralelo quando possível).
# - **Saída limpa**: imprime e salva **apenas a vencedora**.
#
# Dependências:
#   pip install shapely>=2.0 ezdxf rtree
#
# Observação: lógica de encaixe (ordem, first_touch, decisão cabe/não cabe) permanece idêntica.
# Apenas aceleramos a checagem de colisão e adicionamos variações de estratégia.

import sys, argparse, os, io
import ezdxf
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stdout, contextmanager

from ezdxf.path import make_path
from shapely.geometry import Polygon, LineString
from shapely.ops import unary_union, polygonize
from shapely.affinity import translate as shp_translate, rotate as shp_rotate
from shapely.prepared import prep

# ============================================================
# Utilidades: silêncio de prints durante a competição
# ============================================================
@contextmanager
def silence(quiet: bool):
    if not quiet:
        yield
        return
    buf = io.StringIO()
    with redirect_stdout(buf):
        yield

# ============================================================
# Colisão acelerada (índice espacial + prepared)
# ============================================================
try:
    from rtree import index as rtree_index
    HAVE_RTREE = True
except Exception:
    HAVE_RTREE = False

class CollisionIndex:
    """
    Mantém a lógica 'colide/não colide' idêntica:
      antes: cand.buffer(m).intersects(other)
      agora: other.buffer(m).intersects(cand)
    É equivalente geométrico; buffer é feito 1x por peça adicionada.
    """
    def __init__(self, margin_half=0.0):
        self.margin_half = margin_half
        self.items = []
        self.items_buf = []
        self.items_prep = []
        self.bounds = []
        self.count = 0
        self.idx = None
        if HAVE_RTREE:
            p = rtree_index.Property()
            p.interleaved = True
            self.idx = rtree_index.Index(properties=p)

    def add(self, geom):
        gbuf = geom.buffer(self.margin_half) if self.margin_half > 0 else geom
        gprep = prep(gbuf)
        b = gbuf.bounds
        i = self.count
        self.items.append(geom)
        self.items_buf.append(gbuf)
        self.items_prep.append(gprep)
        self.bounds.append(b)
        if self.idx is not None:
            self.idx.insert(i, b)
        self.count += 1

    def collides(self, cand):
        if not self.items:
            return False
        if self.idx is not None:
            cand_bbox = cand.bounds
            for i in self.idx.intersection(cand_bbox):
                if self.items_prep[i].intersects(cand):
                    return True
            return False
        else:
            # Fallback sem Rtree: broad-phase por bbox + prepared
            cand_bbox = cand.bounds
            for i, b in enumerate(self.bounds):
                if cand_bbox[2] < b[0] or cand_bbox[0] > b[2] or cand_bbox[3] < b[1] or cand_bbox[1] > b[3]:
                    continue
                if self.items_prep[i].intersects(cand):
                    return True
            return False

# ============================================================
# Leitura e construcao do poligono da peca
# ============================================================
SKIP_TYPES = frozenset({"TEXT", "MTEXT", "DIMENSION"})

def collect_all_lines(msp, tol=0.5):
    lines = []
    for e in msp:
        if e.dxftype() in SKIP_TYPES:
            continue
        try:
            p = make_path(e)
            for sub in p.sub_paths():
                pts = [(v.x, v.y) for v in sub.flattening(tol)]
                if len(pts) >= 2:
                    lines.append(LineString(pts))
        except:
            pass
    return lines

def create_closed_polygon(msp, tol=0.5, snap_tolerance=1.0):
    lines = collect_all_lines(msp, tol)
    if not lines:
        return None, None

    print(f"🔍 {len(lines)} segmentos")

    all_coords = [coord for line in lines for coord in line.coords]
    xs = [c[0] for c in all_coords]
    ys = [c[1] for c in all_coords]
    bbox_w = max(xs) - min(xs)
    bbox_h = max(ys) - min(ys)
    bbox_area = bbox_w * bbox_h
    print(f"📦 Bbox: {bbox_w:.0f}x{bbox_h:.0f}mm")

    try:
        polys = list(polygonize(lines))
        if polys:
            largest = max(polys, key=lambda p: p.area)
            if largest.area < bbox_area * 0.01:
                print(f"⚠️ Furo detectado")
                raise Exception("Furo")
            print(f"✅ Polígono: {largest.area:.0f}mm²")
            return Polygon(largest.exterior.coords), Polygon(largest.exterior.coords)
    except:
        pass

    print(f"🔄 Buffer...")
    try:
        buffered = [line.buffer(snap_tolerance) for line in lines]
        merged = unary_union(buffered)
        if merged.geom_type == 'MultiPolygon':
            largest = max(merged.geoms, key=lambda p: p.area)
        else:
            largest = merged
        b = largest.bounds
        w = b[2] - b[0]
        h = b[3] - b[1]
        print(f"✅ {w:.0f}x{h:.0f}mm")
        return Polygon(largest.exterior.coords), Polygon(largest.exterior.coords)
    except:
        pass

    return None, None

# ============================================================
# Normalizacao e transformacoes
# ============================================================
def normalize_polygon(poly):
    b = poly.bounds
    return shp_translate(poly, xoff=-b[0], yoff=-b[1])

def apply_transform(poly, angle_deg=0.0, x=0.0, y=0.0):
    q = poly
    if angle_deg != 0.0:
        q = shp_rotate(q, angle_deg, origin='centroid')
    q = normalize_polygon(q)
    return shp_translate(q, xoff=x, yoff=y)

# ============================================================
# Detecção de área vazia retangular
# ============================================================
def find_empty_rectangle(sheet_w, sheet_h, placed_pieces, margin, min_area_ratio=0.05):
    if not placed_pieces:
        return None

    print(f"\n🔍 Detectando área vazia...")

    all_bounds = [p.bounds for p in placed_pieces]
    min_x = min(b[0] for b in all_bounds)
    min_y = min(b[1] for b in all_bounds)
    max_x = max(b[2] for b in all_bounds)
    max_y = max(b[3] for b in all_bounds)

    print(f"   Área ocupada: ({min_x:.0f}, {min_y:.0f}) até ({max_x:.0f}, {max_y:.0f})")

    candidates = []

    # 1. Área à direita
    if max_x + margin < sheet_w:
        empty_w = sheet_w - max_x - margin
        empty_h = sheet_h
        empty_area = empty_w * empty_h
        if empty_area > sheet_w * sheet_h * min_area_ratio:
            candidates.append({'x': max_x + margin, 'y': 0, 'w': empty_w, 'h': empty_h, 'area': empty_area, 'name': 'direita'})
            print(f"   📊 Área direita: {empty_w:.0f}x{empty_h:.0f}mm = {empty_area:.0f}mm²")

    # 2. Área abaixo
    if max_y + margin < sheet_h:
        empty_w = max_x
        empty_h = sheet_h - max_y - margin
        empty_area = empty_w * empty_h
        if empty_area > sheet_w * sheet_h * min_area_ratio:
            candidates.append({'x': 0, 'y': max_y + margin, 'w': empty_w, 'h': empty_h, 'area': empty_area, 'name': 'inferior'})
        print(f"   📊 Área inferior: {empty_w:.0f}x{empty_h:.0f}mm = {empty_area:.0f}mm²")

    # 3. Canto inferior direito
    if max_x + margin < sheet_w and max_y + margin < sheet_h:
        empty_w = sheet_w - max_x - margin
        empty_h = sheet_h - max_y - margin
        empty_area = empty_w * empty_h
        if empty_area > sheet_w * sheet_h * min_area_ratio:
            candidates.append({'x': max_x + margin, 'y': max_y + margin, 'w': empty_w, 'h': empty_h, 'area': empty_area, 'name': 'canto'})
            print(f"   📊 Canto inferior direito: {empty_w:.0f}x{empty_h:.0f}mm = {empty_area:.0f}mm²")

    if not candidates:
        print(f"   ⚠️ Nenhuma área vazia significativa encontrada")
        return None

    best = max(candidates, key=lambda c: c['area'])
    print(f"   ✅ Melhor área vazia: {best['name']} - {best['w']:.0f}x{best['h']:.0f}mm")
    return (best['x'], best['y'], best['w'], best['h'])

# ============================================================
# FASE 1: Preenchimento principal (faixas)
# ============================================================
def fill_phase_1(poly_piece, count, margin, sheet_w, sheet_h, direction='x', alternate_180=False):
    placed = []
    total = 0
    margin_half = margin * 0.5
    placed_bounds = []
    piece_counter = 0

    print(f"\n🔷 FASE 1: Preenchimento em faixas {'horizontais' if direction == 'x' else 'verticais'}")

    coll_idx = CollisionIndex(margin_half=margin_half)

    def fits_sheet(geom):
        x1, y1, x2, y2 = geom.bounds
        return (x1 >= 0) and (y1 >= 0) and (x2 <= sheet_w) and (y2 <= sheet_h)

    def collides(cand):
        return coll_idx.collides(cand)

    def first_touch(base_geom, direction):
        from shapely.affinity import translate as t
        step = 2.0
        shift = 0.0
        while shift < 20000:
            cand = t(base_geom,
                     xoff=shift if direction == 'x' else 0.0,
                     yoff=shift if direction == 'y' else 0.0)
            if not fits_sheet(cand):
                break
            if not collides(cand):
                return cand
            shift += step
        return None

    piece_normalized_base = normalize_polygon(poly_piece)
    x0, y0 = 0.0, 0.0

    while total < count:
        # CORREÇÃO: Alternância 180° corrigida
        if alternate_180 and (piece_counter % 2 == 1):
            # Rotaciona 180° mantendo o mesmo ponto de origem
            current_piece = shp_rotate(piece_normalized_base, 180, origin=(0.0, 0.0))
            current_piece = normalize_polygon(current_piece)
        else:
            current_piece = piece_normalized_base

        placed_geom = shp_translate(current_piece, xoff=x0, yoff=y0)
        if not fits_sheet(placed_geom) or collides(placed_geom):
            break

        placed.append(placed_geom)
        placed_bounds.append(placed_geom.bounds)
        coll_idx.add(placed_geom)
        total += 1
        piece_counter += 1
        base_geom = placed_geom

        while total < count:
            # CORREÇÃO: Alternância 180° corrigida
            if alternate_180 and (piece_counter % 2 == 1):
                current_piece = shp_rotate(piece_normalized_base, 180, origin=(0.0, 0.0))
                current_piece = normalize_polygon(current_piece)
            else:
                current_piece = piece_normalized_base

            next_template = shp_translate(
                current_piece,
                xoff=base_geom.bounds[0],
                yoff=base_geom.bounds[1]
            )

            next_geom = first_touch(next_template, direction)
            if next_geom is None:
                break

            placed.append(next_geom)
            placed_bounds.append(next_geom.bounds)
            coll_idx.add(next_geom)
            total += 1
            piece_counter += 1
            base_geom = next_geom

        if direction == 'x':
            max_y_line = max((p.bounds[3] for p in placed), default=0.0)
            y0 = max_y_line + margin
            x0 = 0.0
        else:
            max_x_line = max((p.bounds[2] for p in placed), default=0.0)
            x0 = max_x_line + margin
            y0 = 0.0

    print(f"   ✅ Fase 1: {total} peças colocadas")
    return placed, total

# ============================================================
# FASE 2: Preencher retângulo vazio com orientação inteligente
# ============================================================
def fill_phase_2_smart(poly_piece, count, margin, sheet_w, sheet_h, placed_phase1, alternate_180=False):
    if len(placed_phase1) >= count:
        return placed_phase1, len(placed_phase1)

    print(f"\n🔶 FASE 2: Preenchimento inteligente da área vazia")

    empty_rect = find_empty_rectangle(sheet_w, sheet_h, placed_phase1, margin)
    if empty_rect is None:
        print("   ⚠️ Nenhuma área vazia significativa para preencher")
        return placed_phase1, len(placed_phase1)

    empty_x, empty_y, empty_w, empty_h = empty_rect

    piece_b = poly_piece.bounds
    piece_w = piece_b[2] - piece_b[0]
    piece_h = piece_b[3] - piece_b[1]

    print(f"   📐 Peça original: {piece_w:.0f}x{piece_h:.0f}mm")
    print(f"   📐 Área vazia: {empty_w:.0f}x{empty_h:.0f}mm")

    orientations_to_test = []
    orientations_to_test.append({'piece': poly_piece, 'rotation': 0, 'name': 'original 0°'})
    piece_rot90 = normalize_polygon(shp_rotate(poly_piece, 90, origin='centroid'))
    orientations_to_test.append({'piece': piece_rot90, 'rotation': 90, 'name': 'rotacionada 90°'})

    print(f"\n   🔬 Testando todas combinações (2 orientações x 2 direções = 4 testes)...")

    margin_half = margin * 0.5

    def fits_empty_rect(geom):
        x1, y1, x2, y2 = geom.bounds
        return (x1 >= empty_x) and (y1 >= empty_y) and \
               (x2 <= empty_x + empty_w) and (y2 <= empty_y + empty_h)

    def test_combination(piece_to_use, direction):
        test_placed = list(placed_phase1)
        test_total = len(test_placed)
        test_counter = len(test_placed)

        comb_idx = CollisionIndex(margin_half=margin_half)
        for p in placed_phase1:
            comb_idx.add(p)

        piece_normalized = normalize_polygon(piece_to_use)
        x0, y0 = empty_x, empty_y

        def _collides(cand):
            return comb_idx.collides(cand)

        def _first_touch(base_geom, direction):
            from shapely.affinity import translate as t
            step = 2.0
            shift = 0.0
            while shift < 20000:
                cand = t(base_geom,
                         xoff=shift if direction == 'x' else 0.0,
                         yoff=shift if direction == 'y' else 0.0)
                if not fits_empty_rect(cand):
                    break
                if not _collides(cand):
                    return cand
                shift += step
            return None

        while test_total < count:
            # CORREÇÃO: Alternância 180° corrigida
            if alternate_180 and (test_counter % 2 == 1):
                current_piece = shp_rotate(piece_normalized, 180, origin=(0.0, 0.0))
                current_piece = normalize_polygon(current_piece)
            else:
                current_piece = piece_normalized

            placed_geom = shp_translate(current_piece, xoff=x0, yoff=y0)

            if not fits_empty_rect(placed_geom) or _collides(placed_geom):
                break

            test_placed.append(placed_geom)
            test_total += 1
            test_counter += 1
            base_geom = placed_geom
            comb_idx.add(placed_geom)

            while test_total < count:
                # CORREÇÃO: Alternância 180° corrigida
                if alternate_180 and (test_counter % 2 == 1):
                    current_piece = shp_rotate(piece_normalized, 180, origin=(0.0, 0.0))
                    current_piece = normalize_polygon(current_piece)
                else:
                    current_piece = piece_normalized

                next_template = shp_translate(
                    current_piece,
                    xoff=base_geom.bounds[0],
                    yoff=base_geom.bounds[1]
                )

                next_geom = _first_touch(next_template, direction)
                if next_geom is None:
                    break
                if not fits_empty_rect(next_geom):
                    break

                test_placed.append(next_geom)
                test_total += 1
                test_counter += 1
                base_geom = next_geom
                comb_idx.add(next_geom)

            if direction == 'x':
                y0 = base_geom.bounds[3] + margin
                x0 = empty_x
                piece_h_local = piece_normalized.bounds[3] - piece_normalized.bounds[1]
                if y0 + piece_h_local > empty_y + empty_h:
                    break
            else:
                x0 = base_geom.bounds[2] + margin
                y0 = empty_y
                piece_w_local = piece_normalized.bounds[2] - piece_normalized.bounds[0]
                if x0 + piece_w_local > empty_x + empty_w:
                    break

        added = test_total - len(placed_phase1)
        return test_placed, test_total, added

    best_result = None
    best_count = 0
    best_config = ""

    for orientation_info in orientations_to_test:
        for direction in ['x', 'y']:
            direction_name = 'horizontal' if direction == 'x' else 'vertical'
            config_name = f"{orientation_info['name']} + {direction_name}"

            result_placed, result_total, result_added = test_combination(
                orientation_info['piece'],
                direction
            )
            print(f"      • {config_name}: +{result_added} peças")

            if result_added > best_count:
                best_result = result_placed
                best_count = result_added
                best_config = config_name

    if best_result is None or best_count == 0:
        print(f"   ⚠️ Nenhuma combinação conseguiu adicionar peças")
        return placed_phase1, len(placed_phase1)

    print(f"\n   🏆 MELHOR: {best_config} com +{best_count} peças")
    return best_result, len(best_result)

# ============================================================
# ESTRATÉGIAS (8 variantes)
# ============================================================
def strategy_horizontal_smart(poly_piece, count, margin, sheet_w, sheet_h, alternate_180=False):
    print("\n" + "="*60)
    print(f"🎯 ESTRATÉGIA: Horizontal + Área vazia inteligente{' (alternada)' if alternate_180 else ''}")
    print("="*60)
    placed, total = fill_phase_1(poly_piece, count, margin, sheet_w, sheet_h, 'x', alternate_180)
    if total < count:
        placed, total = fill_phase_2_smart(poly_piece, count, margin, sheet_w, sheet_h, placed, alternate_180)
    return placed

def strategy_vertical_smart(poly_piece, count, margin, sheet_w, sheet_h, alternate_180=False):
    print("\n" + "="*60)
    print(f"🎯 ESTRATÉGIA: Vertical + Área vazia inteligente{' (alternada)' if alternate_180 else ''}")
    print("="*60)
    placed, total = fill_phase_1(poly_piece, count, margin, sheet_w, sheet_h, 'y', alternate_180)
    if total < count:
        placed, total = fill_phase_2_smart(poly_piece, count, margin, sheet_w, sheet_h, placed, alternate_180)
    return placed

def strategy_horizontal_smart_rot90(poly_piece, count, margin, sheet_w, sheet_h, alternate_180=False):
    # Peça pré-rotacionada 90° (mantém a mesma lógica, apenas outra orientação base)
    piece_rot90 = normalize_polygon(shp_rotate(poly_piece, 90, origin='centroid'))
    return strategy_horizontal_smart(piece_rot90, count, margin, sheet_w, sheet_h, alternate_180)

def strategy_vertical_smart_rot90(poly_piece, count, margin, sheet_w, sheet_h, alternate_180=False):
    piece_rot90 = normalize_polygon(shp_rotate(poly_piece, 90, origin='centroid'))
    return strategy_vertical_smart(piece_rot90, count, margin, sheet_w, sheet_h, alternate_180)

# ============================================================
# COMPETIÇÃO (paralelo) — imprime/salva apenas a vencedora
# ============================================================
def _run_strategy(tag, fn_code, poly_piece, count, margin, sheet_w, sheet_h, alternate, quiet):
    # fn_code mapeia para função específica (evita pickle de funções parciais)
    with silence(quiet):
        if fn_code == "H":
            placed = strategy_horizontal_smart(poly_piece, count, margin, sheet_w, sheet_h, alternate)
        elif fn_code == "V":
            placed = strategy_vertical_smart(poly_piece, count, margin, sheet_w, sheet_h, alternate)
        elif fn_code == "H90":
            placed = strategy_horizontal_smart_rot90(poly_piece, count, margin, sheet_w, sheet_h, alternate)
        elif fn_code == "V90":
            placed = strategy_vertical_smart_rot90(poly_piece, count, margin, sheet_w, sheet_h, alternate)
        else:
            raise ValueError("fn_code inválido")
    util = (len(placed) * poly_piece.area) / (sheet_w * sheet_h) * 100.0
    return (tag, placed, util)

def compete_strategies(poly_piece, count, margin, sheet_w, sheet_h, output_dir, parallel=True, quiet=True):
    # 8 estratégias: H/V, alternada true/false, e as mesmas com peça pré-rotacionada 90°
    jobs = [
        ("Horizontal + Smart",            "H",   False, "winner_horizontal_smart.dxf"),
        ("Vertical + Smart",              "V",   False, "winner_vertical_smart.dxf"),
        ("Horizontal + Smart (alt)",      "H",   True,  "winner_horizontal_smart_alt.dxf"),
        ("Vertical + Smart (alt)",        "V",   True,  "winner_vertical_smart_alt.dxf"),
        ("Horizontal 90° + Smart",        "H90", False, "winner_horizontal90_smart.dxf"),
        ("Vertical 90° + Smart",          "V90", False, "winner_vertical90_smart.dxf"),
        ("Horizontal 90° + Smart (alt)",  "H90", True,  "winner_horizontal90_smart_alt.dxf"),
        ("Vertical 90° + Smart (alt)",    "V90", True,  "winner_vertical90_smart_alt.dxf"),
    ]

    os.makedirs(output_dir, exist_ok=True)
    results = []

    if parallel:
        try:
            with ProcessPoolExecutor(max_workers=min(len(jobs), os.cpu_count() or 2)) as ex:
                futs = []
                for name, fn_code, alt, _fname in jobs:
                    futs.append(ex.submit(_run_strategy, name, fn_code, poly_piece, count, margin, sheet_w, sheet_h, alt, quiet))
                for f in as_completed(futs):
                    name, placed, util = f.result()
                    results.append((name, placed, util))
        except Exception:
            # Fallback sequencial
            parallel = False

    if not parallel:
        for name, fn_code, alt, _fname in jobs:
            name, placed, util = _run_strategy(name, fn_code, poly_piece, count, margin, sheet_w, sheet_h, alt, quiet)
            results.append((name, placed, util))

    # Escolhe melhor
    best = max(results, key=lambda s: (len(s[1]), s[2]))

    # Salva apenas o vencedor
    best_name, best_placed, best_util = best
    best_filename = next(fname for n,_,_,fname in jobs if n == best_name)
    best_path = os.path.join(output_dir, best_filename)
    export_dxf(sheet_w, sheet_h, best_placed, best_path)

    # Impressão apenas do vencedor (conforme solicitado)
    print("\n" + "🏆"*30)
    print(f"🥇 VENCEDORA: {best_name}")
    print(f"   Peças: {len(best_placed)}")
    print(f"   Utilização: {best_util:.1f}%")
    print(f"   Arquivo: {best_path}")
    print("🏆"*30 + "\n")

    return best_placed, best_name, best_path, best_util

# ============================================================
# Exportacao DXF
# ============================================================
def export_dxf(container_w, container_h, placed, out_path):
    doc = ezdxf.new(setup=True)
    msp = doc.modelspace()

    msp.add_lwpolyline(
        [(0,0),(container_w,0),(container_w,container_h),(0,container_h),(0,0)],
        dxfattribs={"closed": True, "color": 7}
    )

    for poly in placed:
        msp.add_lwpolyline(
            list(poly.exterior.coords),
            dxfattribs={"closed": True, "color": 1}
        )

    doc.saveas(out_path)

# ============================================================
# Main
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--w", type=float, required=True)
    ap.add_argument("--h", type=float, required=True)
    ap.add_argument("--margin", type=float, default=0.0)
    ap.add_argument("--tol", type=float, default=0.5)
    ap.add_argument("--snap", type=float, default=2.0)
    ap.add_argument("--out", default="outputs_nesting", help="Diretorio de saida")
    ap.add_argument("--count", type=int, default=None, help="Numero de pecas (deixe vazio para maximo possivel)")
    ap.add_argument("--no-parallel", action="store_true", help="Desativa paralelismo na competição")
    ap.add_argument("--verbose", action="store_true", help="Mostra logs internos das estratégias")
    args = ap.parse_args()

    doc = ezdxf.readfile(args.infile)
    print("="*60)

    poly_env, poly_full = create_closed_polygon(doc.modelspace(), args.tol, args.snap)
    if not poly_env:
        sys.exit(1)

    poly_env = normalize_polygon(poly_env)
    b = poly_env.bounds
    piece_area = poly_env.area
    sheet_area = args.w * args.h

    print(f"✅ Peça: {b[2]-b[0]:.1f}x{b[3]-b[1]:.1f} mm | Área: {piece_area:.0f}mm²")
    print(f"📄 Chapa: {args.w:.0f}x{args.h:.0f} mm | Área: {sheet_area:.0f}mm²")

    if args.count is None:
        max_theoretical = int((sheet_area / piece_area) * 1)
        args.count = max(max_theoretical, 100)
        print(f"🔢 Count automático: {args.count} peças (preencherá até não caber mais)")
    else:
        print(f"🔢 Count definido: {args.count} peças")

    print("="*60)

    # Executa competição (silencia logs internos se não for verbose)
    placed, winner_name, winner_path, util = compete_strategies(
        poly_env,
        count=args.count,
        margin=args.margin,
        sheet_w=args.w,
        sheet_h=args.h,
        output_dir=args.out,
        parallel=(not args.no_parallel),
        quiet=(not args.verbose)
    )

    # Resumo final (apenas vencedora)
    print(f"🎯 FINAL: {len(placed)} peças | {util:.1f}%")
    print(f"📁 Arquivo salvo: {winner_path}")
    print("="*60)

if __name__ == "__main__":
    main()