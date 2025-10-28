# -*- coding: utf-8 -*-
"""
Calcula o comprimento total de corte de um DXF (inclui furos/vazados)
e imprime o tempo de corte em MINUTOS ABSOLUTOS para todas as velocidades.

Uso mínimo:
  python dxf_all_speeds_time.py --in peça.dxf

Opções:
  --units mm|cm|m      (padrão: mm)
  --tol 0.3            tolerância de flattening para curvas
  --dedup              deduplicar segmentos aproximados
  --eps 0.05           tolerância da deduplicação
  --estimate_pierces   soma pierce_time por subpath
  --pierce_time 0.4    tempo (s) por furo (default: 0)
  --csv out.csv        salva resultados em CSV
  --decimals 3         casas decimais para minutos absolutos
"""

import argparse
import csv
import math
import ezdxf
from ezdxf.path import make_path

# -----------------------------
# Tabela de velocidades (m/min)
# -----------------------------
CUT_SPEED = {
    "Inox": {
        2.0: 2.7,
        3.0: 1.8,
        4.0: 1.0,
    },
    "Carbono": {
        2.0: 2.7,
        3.0: 1.5,
        3.75: 1.2,
        4.75: 0.8,
    },
}

# -----------------------------
# Geometria
# -----------------------------
SKIP_TYPES = frozenset({"TEXT", "MTEXT", "DIMENSION"})

def iter_paths(msp):
    for e in msp:
        if e.dxftype() in SKIP_TYPES:
            continue
        try:
            yield make_path(e)
        except Exception:
            continue

def length_of_path_flattened(path, tol=0.3):
    total = 0.0
    for sub in path.sub_paths():
        pts = [(v.x, v.y) for v in sub.flattening(tol)]
        if len(pts) < 2:
            continue
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            total += math.hypot(x2 - x1, y2 - y1)
    return total

def rounded(val: float, eps: float) -> float:
    return val if eps <= 0 else round(val / eps) * eps

def segment_key(p1, p2, eps: float):
    x1, y1 = rounded(p1[0], eps), rounded(p1[1], eps)
    x2, y2 = rounded(p2[0], eps), rounded(p2[1], eps)
    a, b = (x1, y1), (x2, y2)
    return (a, b) if a <= b else (b, a)

def length_of_path_flattened_dedup(path, tol=0.3, eps=0.05):
    seen = set()
    total = 0.0
    for sub in path.sub_paths():
        pts = [(v.x, v.y) for v in sub.flattening(tol)]
        if len(pts) < 2:
            continue
        for p1, p2 in zip(pts, pts[1:]):
            key = segment_key(p1, p2, eps)
            if key in seen:
                continue
            seen.add(key)
            total += math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    return total

# -----------------------------
# Unidades
# -----------------------------
UNIT_FACTORS_TO_M = {"mm": 0.001, "cm": 0.01, "m": 1.0}

# -----------------------------
# Main
# -----------------------------
def main():
    ap = argparse.ArgumentParser(description="Tempo de corte (minutos absolutos) para todas as velocidades.")
    ap.add_argument("--in", dest="infile", required=True, help="Arquivo DXF de entrada")
    ap.add_argument("--units", choices=["mm", "cm", "m"], default="mm", help="Unidade geométrica do DXF")
    ap.add_argument("--tol", type=float, default=0.3, help="Tolerância de flattening para curvas")
    ap.add_argument("--dedup", action="store_true", help="Deduplicação aproximada de segmentos")
    ap.add_argument("--eps", type=float, default=0.05, help="Tolerância da deduplicação")
    ap.add_argument("--estimate_pierces", action="store_true", help="Soma pierce_time por subcaminho")
    ap.add_argument("--pierce_time", type=float, default=0.0, help="Tempo (s) por furo")
    ap.add_argument("--csv", type=str, default=None, help="Salvar resultados em CSV (opcional)")
    ap.add_argument("--decimals", type=int, default=3, help="Casas decimais para minutos")
    args = ap.parse_args()

    # Leitura DXF e soma de comprimentos
    doc = ezdxf.readfile(args.infile)
    msp = doc.modelspace()

    total_len_model = 0.0
    total_subpaths = 0
    for path in iter_paths(msp):
        L = (length_of_path_flattened_dedup(path, tol=args.tol, eps=args.eps)
             if args.dedup else
             length_of_path_flattened(path, tol=args.tol))
        total_len_model += L
        total_subpaths += len(list(path.sub_paths()))

    # Para metros
    total_len_m = total_len_model * UNIT_FACTORS_TO_M[args.units]

    # Cabeçalho
    print("=" * 70)
    print(f"Arquivo: {args.infile}")
    print(f"Comprimento total: {total_len_m:.5f} m")
    print(f"Unidades DXF: {args.units} | Tol: {args.tol} | Dedup: {'ON' if args.dedup else 'OFF'}")
    if args.estimate_pierces and args.pierce_time > 0.0:
        print(f"Piercing: ~{total_subpaths} pierces @ {args.pierce_time:.3f}s (estimado)")
    else:
        print("Piercing: ignorado")
    print("-" * 70)
    print(f"{'Material':8s}  {'Esp(mm)':7s}  {'Vel(m/min)':10s}  {'Tempo_min(abs)':>15s}")

    rows = []
    for material, table in CUT_SPEED.items():
        for thickness, speed in sorted(table.items()):
            time_min = (total_len_m / speed) if speed > 0 else float("inf")
            if args.estimate_pierces and args.pierce_time > 0.0:
                time_min += (total_subpaths * args.pierce_time) / 60.0
            time_min = round(time_min, args.decimals)
            rows.append({
                "Material": material,
                "Espessura_mm": thickness,
                "Velocidade_m_min": speed,
                "Tempo_min": time_min,
                "Comprimento_m": round(total_len_m, 5),
                "Pierces_est": total_subpaths if (args.estimate_pierces and args.pierce_time > 0.0) else 0,
            })
            print(f"{material:8s}  {thickness:7.2f}  {speed:10.2f}  {time_min:15.{args.decimals}f}")

    print("=" * 70)

    if args.csv and rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"CSV salvo em: {args.csv}")

if __name__ == "__main__":
    main()
