# -*- coding: utf-8 -*-
"""
GUI COMPLETO - Sistema Automático de Nesting + Preços

Funcionalidades:
  1) Ler um DXF.
  2) Executar AUTOMATICAMENTE nesting para:
     - Inox (chapa configurável, padrão 3000×1240)
     - Carbono (chapa configurável, padrão 3000×1200)
  3) Calcular tempos de corte por material/espessura.
  4) CALCULAR PREÇO UNITÁRIO automaticamente.
  5) Mostrar TODOS os resultados em uma única tabela.

Aba de Configurações:
  - Tamanhos de chapas (Inox e Carbono)
  - Valores das chapas (R$)
  - Valor do minuto de corte (R$)
  - Coeficiente de aproveitamento (padrão: 0.95)
  - Velocidades de corte

FÓRMULA DO PREÇO UNITÁRIO:
  Preço = (Valor_Chapa / Qtd_Coef) + (Valor_Minuto * Tempo_Corte / Qtd_Max)
  Onde: Qtd_Coef = Qtd_Max * Coeficiente
"""

import json
import math
import os
import re
import shlex
import subprocess
import threading
from tkinter import Tk, StringVar, IntVar, DoubleVar, N, S, E, W, filedialog, messagebox
from tkinter import ttk

import ezdxf
from ezdxf.path import make_path

# -----------------------------
# CONFIGURAÇÕES PADRÃO
# -----------------------------
DEFAULT_CONFIG = {
    "sheet_prices": {
        "Inox": {
            2.0: 2671.35,
            3.0: 4070.86,
            4.0: 4865.76,
        },
        "Carbono": {
            2.0: 1878.86,
            3.0: 2701.46,
            3.75: 3300.05,
            4.75: 4191.30,
        },
    },
    "cut_speed": {
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
    },
    "sheet_sizes": {
        "Inox": {"w": 3000.0, "h": 1240.0},
        "Carbono": {"w": 3000.0, "h": 1200.0},
    },
    "minute_price": 3.37,
    "coefficient": 0.95,
}

CONFIG_FILE = "nesting_config.json"

# Regex para extrair "FINAL: N peças"
FINAL_REGEX = re.compile(r"FINAL:\s*(\d+)\s*pe", re.IGNORECASE)

# -----------------------------
# Funções auxiliares
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

def load_config():
    """Carrega configurações do arquivo JSON ou retorna padrão"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
                # Garantir que sheet_sizes existe (compatibilidade com versões antigas)
                if "sheet_sizes" not in config:
                    config["sheet_sizes"] = DEFAULT_CONFIG["sheet_sizes"].copy()
                return config
        except:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))  # Deep copy

def save_config(config):
    """Salva configurações em arquivo JSON"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Erro ao salvar config: {e}")
        return False

# -----------------------------
# Lógica principal
# -----------------------------
def run_nesting_and_get_qty(nest_cmd: str, infile: str, w: float, h: float,
                            margin: float = 0.1, tol: float = 0.5, snap: float = 2.0,
                            out_dir: str = "outputs_nesting", extra_flags=None) -> int:
    if extra_flags is None:
        extra_flags = []
    cmd_parts = list(shlex.split(nest_cmd))
    cmd_parts += ["--in", infile,
                  "--w", str(w), "--h", str(h),
                  "--margin", str(margin),
                  "--tol", str(tol),
                  "--snap", str(snap),
                  "--out", out_dir]
    cmd_parts += list(extra_flags)

    proc = subprocess.run(
        cmd_parts,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False
    )
    
    if proc.returncode != 0:
        raise RuntimeError(
            "Nesting retornou erro.\n"
            f"CMD: {' '.join(cmd_parts)}\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}"
        )

    full_output = proc.stdout + "\n" + proc.stderr
    m = FINAL_REGEX.search(full_output)
    
    if not m:
        raise RuntimeError(
            "Não foi possível extrair a quantidade FINAL do nesting.\n"
            f"Saída (últimas 1000 chars):\n{full_output[-1000:]}"
        )
    return int(m.group(1))

def compute_length_m(infile: str, tol: float = 0.3, units: str = "mm") -> float:
    UNIT_FACTORS_TO_M = {"mm": 0.001, "cm": 0.01, "m": 1.0}
    factor_to_m = UNIT_FACTORS_TO_M[units]

    doc = ezdxf.readfile(infile)
    msp = doc.modelspace()

    total_len_model = 0.0
    for path in iter_paths(msp):
        total_len_model += length_of_path_flattened(path, tol=tol)

    return total_len_model * factor_to_m

def compute_times_and_prices(total_len_m: float, qty: int, config: dict, decimals: int = 3, material_filter: str = None):
    """
    Calcula tempos e PREÇOS UNITÁRIOS para todas as combinações material/espessura.
    
    Fórmula do preço:
    Preço = (Valor_Chapa / Qtd_Coef) + (Valor_Minuto * Tempo_Corte / Qtd_Max)
    Onde: Qtd_Coef = Qtd_Max * Coeficiente
    
    Se material_filter for especificado, calcula apenas para aquele material.
    """
    rows = []
    sheet_prices = config["sheet_prices"]
    cut_speed = config["cut_speed"]
    minute_price = config["minute_price"]
    coefficient = config["coefficient"]
    
    qty_coef = qty * coefficient  # Quantidade com coeficiente aplicado
    
    for material in cut_speed.keys():
        # Se filtro especificado, pula outros materiais
        if material_filter and material != material_filter:
            continue
            
        for thickness, speed in sorted(cut_speed[material].items()):
            # Tempo de corte
            per_piece_min = total_len_m / speed if speed > 0 else float("inf")
            total_min = per_piece_min * qty
            
            # Preço da chapa correspondente
            sheet_price = sheet_prices.get(material, {}).get(thickness, 0.0)
            
            # CÁLCULO DO PREÇO UNITÁRIO
            # Preço = (Valor_Chapa / Qtd_Coef) + (Valor_Minuto * Tempo_Corte / Qtd_Max)
            if qty_coef > 0 and qty > 0:
                price_per_piece = (sheet_price / qty_coef) + (minute_price * total_min / qty)
            else:
                price_per_piece = 0.0
            
            rows.append({
                "Material": material,
                "Espessura_mm": thickness,
                "Velocidade_m_min": speed,
                "Min_por_peca": round(per_piece_min, decimals),
                "Quantidade": qty,
                "Min_total": round(total_min, decimals),
                "Preco_unitario": round(price_per_piece, 2),
                "Valor_chapa": sheet_price,
            })
    return rows

# -----------------------------
# GUI Principal
# -----------------------------
class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("Sistema de Nesting + Preços - Auto Inox/Carbono")
        self.root.geometry("1000x650")
        
        # Carrega configurações
        self.config = load_config()
        
        # Variáveis
        self.var_dxf = StringVar()
        self.var_nest_cmd = StringVar(value="python nest.py")
        self.var_status = StringVar(value="Pronto.")
        
        # Notebook com abas
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Aba 1: Cálculo
        self.frame_calc = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.frame_calc, text="📊 Cálculo")
        
        # Aba 2: Configurações
        self.frame_config = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.frame_config, text="⚙️ Configurações")
        
        # Inicializa abas
        self._init_calc_tab()
        self._init_config_tab()
    
    def _init_calc_tab(self):
        """Inicializa aba de cálculo"""
        frm = self.frame_calc
        
        # ===== INPUTS =====
        input_frame = ttk.LabelFrame(frm, text="Entrada", padding=10)
        input_frame.grid(row=0, column=0, sticky=(N, S, E, W), pady=(0, 10))
        frm.columnconfigure(0, weight=1)
        
        # DXF
        ttk.Label(input_frame, text="Arquivo DXF:").grid(row=0, column=0, sticky=W, padx=(0,6))
        ent_dxf = ttk.Entry(input_frame, textvariable=self.var_dxf, width=50)
        ent_dxf.grid(row=0, column=1, sticky=E+W, padx=6)
        input_frame.columnconfigure(1, weight=1)
        ttk.Button(input_frame, text="Procurar…", command=self.choose_dxf).grid(row=0, column=2, padx=(6,0))
        
        # Comando nesting
        ttk.Label(input_frame, text="Comando:").grid(row=1, column=0, sticky=W, pady=(8,0))
        ent_cmd = ttk.Entry(input_frame, textvariable=self.var_nest_cmd, width=50)
        ent_cmd.grid(row=1, column=1, sticky=E+W, padx=6, pady=(8,0))
        ttk.Label(input_frame, text="ex: python nest.py", foreground="gray").grid(row=1, column=2, sticky=W, pady=(8,0))
        
        # Info sobre chapas
        info_label = ttk.Label(input_frame, 
                              text="ℹ️  Sistema calculará automaticamente para Inox (3000×1240) e Carbono (3000×1200)",
                              foreground="blue", font=("", 9))
        info_label.grid(row=2, column=0, columnspan=3, sticky=W, pady=(10,0))
        
        # Botão executar
        btn_frame = ttk.Frame(input_frame)
        btn_frame.grid(row=3, column=0, columnspan=3, pady=(10,0))
        self.btn_run = ttk.Button(btn_frame, text="▶ EXECUTAR CÁLCULO", 
                                  command=self.on_run_clicked, style="Accent.TButton")
        self.btn_run.pack(side="left", padx=5)
        
        # Status
        status_label = ttk.Label(btn_frame, textvariable=self.var_status, foreground="green")
        status_label.pack(side="left", padx=10)
        
        # ===== RESULTADOS =====
        result_frame = ttk.LabelFrame(frm, text="Resultados", padding=10)
        result_frame.grid(row=1, column=0, sticky=(N, S, E, W))
        frm.rowconfigure(1, weight=1)
        
        # Tabela
        cols = ("Material", "Esp(mm)", "Vel(m/min)", "Min/peça", "Qtd", "Min total", "💰 Preço R$")
        self.tree = ttk.Treeview(result_frame, columns=cols, show="headings", height=18)
        
        # Configurar colunas
        col_widths = [90, 70, 85, 75, 60, 80, 100]
        for col, width in zip(cols, col_widths):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=width, anchor="center")
        
        # Destacar coluna de preço
        self.tree.tag_configure("price", background="#e8f5e9")
        
        self.tree.grid(row=0, column=0, sticky=(N, S, E, W))
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)
        
        # Scrollbar
        vsb = ttk.Scrollbar(result_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=vsb.set)
        vsb.grid(row=0, column=1, sticky=(N,S))
        
        # Footer
        footer = ttk.Label(result_frame, 
                          text="💡 Preço = (Valor_Chapa/Qtd_Coef) + (R$/min × Tempo_Total/Qtd) | Coef = Qtd × 0.95",
                          foreground="blue", font=("", 9, "italic"))
        footer.grid(row=1, column=0, columnspan=2, sticky=W, pady=(5,0))
    
    def _init_config_tab(self):
        """Inicializa aba de configurações"""
        frm = self.frame_config
        
        # Container com scroll
        from tkinter import Canvas
        canvas = Canvas(frm)
        scrollbar = ttk.Scrollbar(frm, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # ===== CONFIGURAÇÕES GERAIS =====
        general_frame = ttk.LabelFrame(scrollable_frame, text="⚙️ Configurações Gerais", padding=15)
        general_frame.pack(fill="x", padx=10, pady=10)
        
        # Valor minuto
        ttk.Label(general_frame, text="Valor do Minuto de Corte (R$):").grid(row=0, column=0, sticky=W, pady=5)
        self.var_minute_price = DoubleVar(value=self.config["minute_price"])
        ttk.Entry(general_frame, textvariable=self.var_minute_price, width=15).grid(row=0, column=1, sticky=W, padx=10)
        
        # Coeficiente
        ttk.Label(general_frame, text="Coeficiente de Aproveitamento:").grid(row=1, column=0, sticky=W, pady=5)
        self.var_coefficient = DoubleVar(value=self.config["coefficient"])
        ttk.Entry(general_frame, textvariable=self.var_coefficient, width=15).grid(row=1, column=1, sticky=W, padx=10)
        ttk.Label(general_frame, text="(padrão: 0.95)", foreground="gray").grid(row=1, column=2, sticky=W)
        
        # ===== TAMANHOS DAS CHAPAS =====
        size_frame = ttk.LabelFrame(scrollable_frame, text="📐 Tamanhos das Chapas (mm)", padding=15)
        size_frame.pack(fill="x", padx=10, pady=10)
        
        self.size_vars = {}
        
        row = 0
        for material in ["Inox", "Carbono"]:
            self.size_vars[material] = {}
            
            ttk.Label(size_frame, text=f"{material}:", font=("", 10, "bold")).grid(row=row, column=0, sticky=W, pady=5)
            
            ttk.Label(size_frame, text="Largura:").grid(row=row, column=1, sticky=W, padx=(20,5))
            w_var = DoubleVar(value=self.config["sheet_sizes"][material]["w"])
            self.size_vars[material]["w"] = w_var
            ttk.Entry(size_frame, textvariable=w_var, width=10).grid(row=row, column=2, sticky=W)
            
            ttk.Label(size_frame, text="× Altura:").grid(row=row, column=3, sticky=W, padx=(10,5))
            h_var = DoubleVar(value=self.config["sheet_sizes"][material]["h"])
            self.size_vars[material]["h"] = h_var
            ttk.Entry(size_frame, textvariable=h_var, width=10).grid(row=row, column=4, sticky=W)
            
            ttk.Label(size_frame, text="mm", foreground="blue").grid(row=row, column=5, sticky=W, padx=(5,0))
            
            row += 1
        
        # ===== PREÇOS DAS CHAPAS =====
        self.price_vars = {}
        
        for material in ["Inox", "Carbono"]:
            price_frame = ttk.LabelFrame(scrollable_frame, text=f"💰 Preços {material}", padding=15)
            price_frame.pack(fill="x", padx=10, pady=10)
            
            self.price_vars[material] = {}
            
            row = 0
            for thickness in sorted(self.config["sheet_prices"][material].keys()):
                price = self.config["sheet_prices"][material][thickness]
                
                ttk.Label(price_frame, text=f"{material} {thickness}mm:").grid(row=row, column=0, sticky=W, pady=5)
                
                var = DoubleVar(value=price)
                self.price_vars[material][thickness] = var
                
                entry = ttk.Entry(price_frame, textvariable=var, width=15)
                entry.grid(row=row, column=1, sticky=W, padx=10)
                
                ttk.Label(price_frame, text="R$", foreground="green").grid(row=row, column=2, sticky=W)
                
                row += 1
        
        # ===== VELOCIDADES DE CORTE =====
        self.speed_vars = {}
        
        for material in ["Inox", "Carbono"]:
            speed_frame = ttk.LabelFrame(scrollable_frame, text=f"⚡ Velocidades {material}", padding=15)
            speed_frame.pack(fill="x", padx=10, pady=10)
            
            self.speed_vars[material] = {}
            
            row = 0
            for thickness in sorted(self.config["cut_speed"][material].keys()):
                speed = self.config["cut_speed"][material][thickness]
                
                ttk.Label(speed_frame, text=f"{material} {thickness}mm:").grid(row=row, column=0, sticky=W, pady=5)
                
                var = DoubleVar(value=speed)
                self.speed_vars[material][thickness] = var
                
                entry = ttk.Entry(speed_frame, textvariable=var, width=15)
                entry.grid(row=row, column=1, sticky=W, padx=10)
                
                ttk.Label(speed_frame, text="m/min", foreground="blue").grid(row=row, column=2, sticky=W)
                
                row += 1
        
        # ===== BOTÕES =====
        btn_frame = ttk.Frame(scrollable_frame)
        btn_frame.pack(fill="x", padx=10, pady=20)
        
        ttk.Button(btn_frame, text="💾 Salvar Configurações", 
                  command=self.save_config_ui).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="🔄 Restaurar Padrões", 
                  command=self.reset_config_ui).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="📁 Abrir arquivo de config", 
                  command=self.open_config_file).pack(side="left", padx=5)
    
    def choose_dxf(self):
        path = filedialog.askopenfilename(
            title="Selecione o DXF",
            filetypes=[("DXF files","*.dxf"),("Todos","*.*")]
        )
        if path:
            self.var_dxf.set(path)
    
    def on_run_clicked(self):
        dxf = self.var_dxf.get().strip()
        if not dxf:
            messagebox.showwarning("Atenção", "Selecione um arquivo DXF.")
            return
        
        nest_cmd = self.var_nest_cmd.get().strip()
        if not nest_cmd:
            messagebox.showwarning("Atenção", "Informe o comando do nesting.")
            return
        
        # Atualiza config da UI antes de calcular
        self._update_config_from_ui()
        
        self.btn_run.config(state="disabled")
        self.var_status.set("🔄 Executando...")
        
        threading.Thread(
            target=self._run_pipeline,
            args=(dxf, nest_cmd),
            daemon=True
        ).start()
    
    def _run_pipeline(self, dxf, nest_cmd):
        try:
            # Calcula comprimento da peça uma vez
            self.root.after(0, lambda: self.var_status.set("🔄 Calculando comprimentos..."))
            total_len_m = compute_length_m(dxf, tol=0.3, units="mm")
            
            all_rows = []
            quantities = {}  # Armazena quantidade de cada material
            
            # Para cada material (Inox e Carbono)
            for material in ["Inox", "Carbono"]:
                # Pega tamanho da chapa deste material
                sheet_size = self.config["sheet_sizes"][material]
                w, h = sheet_size["w"], sheet_size["h"]
                
                self.root.after(0, lambda m=material, ww=w, hh=h: 
                              self.var_status.set(f"🔄 Rodando nesting {m} ({int(ww)}×{int(hh)})..."))
                
                # Executa nesting
                qty_material = run_nesting_and_get_qty(
                    nest_cmd=nest_cmd,
                    infile=dxf,
                    w=w, h=h,
                    margin=0.1,
                    tol=0.5,
                    snap=2.0,
                    out_dir=f"outputs_nesting_{material.lower()}"
                )
                
                quantities[material] = qty_material
                print(f"DEBUG: {material} → {qty_material} peças (chapa {int(w)}×{int(h)})")
                
                # Calcula tempos e preços para este material
                self.root.after(0, lambda m=material: self.var_status.set(f"🔄 Calculando preços {m}..."))
                
                rows = compute_times_and_prices(
                    total_len_m=total_len_m, 
                    qty=qty_material,  # USA A QUANTIDADE ESPECÍFICA
                    config=self.config, 
                    decimals=3,
                    material_filter=material
                )
                
                all_rows.extend(rows)
            
            print(f"DEBUG: Total de linhas geradas: {len(all_rows)}")
            for r in all_rows:
                print(f"  {r['Material']} {r['Espessura_mm']}mm → Qtd: {r['Quantidade']}")
            
            # Atualiza UI com todos os resultados
            self.root.after(0, self._update_results, all_rows, total_len_m)
            
        except Exception as e:
            error_msg = str(e)
            print(f"ERRO: {error_msg}")
            import traceback
            traceback.print_exc()
            self.root.after(0, lambda: messagebox.showerror("Erro", error_msg))
            self.root.after(0, lambda: self.var_status.set("❌ Falhou"))
            self.root.after(0, lambda: self.btn_run.config(state="normal"))
    
    def _update_results(self, rows, total_len_m):
        # Limpa tabela
        for i in self.tree.get_children():
            self.tree.delete(i)
        
        # Adiciona resultados
        for r in rows:
            values = (
                r["Material"],
                f'{r["Espessura_mm"]:.2f}' if isinstance(r["Espessura_mm"], (int, float)) else str(r["Espessura_mm"]),
                f'{r["Velocidade_m_min"]:.2f}' if isinstance(r["Velocidade_m_min"], (int, float)) else str(r["Velocidade_m_min"]),
                f'{r["Min_por_peca"]:.3f}' if isinstance(r["Min_por_peca"], (int, float)) else str(r["Min_por_peca"]),
                str(r["Quantidade"]),
                f'{r["Min_total"]:.3f}' if isinstance(r["Min_total"], (int, float)) else str(r["Min_total"]),
                f'R$ {r["Preco_unitario"]:.2f}' if isinstance(r["Preco_unitario"], (int, float)) else str(r["Preco_unitario"]),
            )
            self.tree.insert("", "end", values=values, tags=("price",))
        
        self.var_status.set(
            f"✅ OK | Comprimento/peça: {total_len_m:.5f}m | "
            f"Inox: {self.config['sheet_sizes']['Inox']['w']:.0f}×{self.config['sheet_sizes']['Inox']['h']:.0f}mm | "
            f"Carbono: {self.config['sheet_sizes']['Carbono']['w']:.0f}×{self.config['sheet_sizes']['Carbono']['h']:.0f}mm"
        )
        self.btn_run.config(state="normal")
    
    def _update_config_from_ui(self):
        """Atualiza self.config com valores da UI"""
        self.config["minute_price"] = self.var_minute_price.get()
        self.config["coefficient"] = self.var_coefficient.get()
        
        # Tamanhos de chapa
        for material in self.size_vars:
            self.config["sheet_sizes"][material]["w"] = self.size_vars[material]["w"].get()
            self.config["sheet_sizes"][material]["h"] = self.size_vars[material]["h"].get()
        
        for material in self.price_vars:
            for thickness, var in self.price_vars[material].items():
                self.config["sheet_prices"][material][thickness] = var.get()
        
        for material in self.speed_vars:
            for thickness, var in self.speed_vars[material].items():
                self.config["cut_speed"][material][thickness] = var.get()
    
    def save_config_ui(self):
        """Salva configurações da UI"""
        self._update_config_from_ui()
        if save_config(self.config):
            messagebox.showinfo("Sucesso", "Configurações salvas com sucesso!")
        else:
            messagebox.showerror("Erro", "Erro ao salvar configurações.")
    
    def reset_config_ui(self):
        """Restaura configurações padrão"""
        if messagebox.askyesno("Confirmar", "Restaurar todas as configurações para os valores padrão?"):
            self.config = json.loads(json.dumps(DEFAULT_CONFIG))  # Deep copy
            
            # Atualiza UI
            self.var_minute_price.set(self.config["minute_price"])
            self.var_coefficient.set(self.config["coefficient"])
            
            # Tamanhos de chapa
            for material in self.size_vars:
                self.size_vars[material]["w"].set(self.config["sheet_sizes"][material]["w"])
                self.size_vars[material]["h"].set(self.config["sheet_sizes"][material]["h"])
            
            for material in self.price_vars:
                for thickness, var in self.price_vars[material].items():
                    var.set(self.config["sheet_prices"][material][thickness])
            
            for material in self.speed_vars:
                for thickness, var in self.speed_vars[material].items():
                    var.set(self.config["cut_speed"][material][thickness])
            
            messagebox.showinfo("Sucesso", "Configurações restauradas para os valores padrão!")
    
    def open_config_file(self):
        """Abre o arquivo de configuração no editor padrão"""
        if os.path.exists(CONFIG_FILE):
            try:
                if os.name == 'nt':  # Windows
                    os.startfile(CONFIG_FILE)
                elif os.name == 'posix':  # Linux/Mac
                    os.system(f'xdg-open "{CONFIG_FILE}"')
                else:
                    messagebox.showinfo("Info", f"Arquivo: {os.path.abspath(CONFIG_FILE)}")
            except:
                messagebox.showinfo("Info", f"Arquivo: {os.path.abspath(CONFIG_FILE)}")
        else:
            messagebox.showwarning("Aviso", "Arquivo de configuração ainda não existe. Salve as configurações primeiro.")

def main():
    root = Tk()
    
    # Estilo
    try:
        root.call("tk", "scaling", 1.2)
    except:
        pass
    
    style = ttk.Style()
    if "clam" in style.theme_names():
        style.theme_use("clam")
    
    # Estilo do botão principal
    style.configure("Accent.TButton", font=("", 10, "bold"))
    
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()