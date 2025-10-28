# -*- coding: utf-8 -*-
"""
Sistema Automático de Nesting + Preços
Calcula automaticamente nesting e preços para Inox e Carbono
"""

import json
import math
import os
import re
import subprocess
import threading
from tkinter import Tk, StringVar, DoubleVar, N, S, E, W, filedialog, messagebox
from tkinter import ttk
from tkinterdnd2 import DND_FILES, TkinterDnD

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
FINAL_REGEX = re.compile(r"FINAL:\s*(\d+)\s*pe", re.IGNORECASE)
SKIP_TYPES = frozenset({"TEXT", "MTEXT", "DIMENSION"})

# -----------------------------
# Funções auxiliares
# -----------------------------

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

def convert_keys_to_float(d):
    """Converte chaves string para float recursivamente"""
    if not isinstance(d, dict):
        return d
    
    new_dict = {}
    for k, v in d.items():
        try:
            new_key = float(k)
        except (ValueError, TypeError):
            new_key = k
        
        if isinstance(v, dict):
            new_dict[new_key] = convert_keys_to_float(v)
        else:
            new_dict[new_key] = v
    
    return new_dict

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
                if "sheet_sizes" not in config:
                    config["sheet_sizes"] = DEFAULT_CONFIG["sheet_sizes"].copy()
                
                config["sheet_prices"] = convert_keys_to_float(config.get("sheet_prices", {}))
                config["cut_speed"] = convert_keys_to_float(config.get("cut_speed", {}))
                
                return config
        except:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))

def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Erro ao salvar config: {e}")
        return False

def run_nesting_and_get_qty(infile: str, w: float, h: float,
                            margin: float = 0.1, tol: float = 0.5, snap: float = 2.0,
                            out_dir: str = "outputs_nesting") -> int:
    cmd_parts = ["python", "nest.py",
                  "--in", infile,
                  "--w", str(w), "--h", str(h),
                  "--margin", str(margin),
                  "--tol", str(tol),
                  "--snap", str(snap),
                  "--out", out_dir]

    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'

    proc = subprocess.run(
        cmd_parts,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
        env=env,
        check=False
    )
    
    if proc.returncode != 0:
        raise RuntimeError(
            f"Erro no nesting.\nCMD: {' '.join(cmd_parts)}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )

    full_output = proc.stdout + "\n" + proc.stderr
    m = FINAL_REGEX.search(full_output)
    
    if not m:
        raise RuntimeError(
            f"Não foi possível extrair a quantidade.\nSaída:\n{full_output[-1000:]}"
        )
    return int(m.group(1))

def compute_length_m(infile: str, tol: float = 0.3) -> float:
    doc = ezdxf.readfile(infile)
    msp = doc.modelspace()

    total_len_model = 0.0
    for path in iter_paths(msp):
        total_len_model += length_of_path_flattened(path, tol=tol)

    return total_len_model * 0.001

def compute_times_and_prices(total_len_m: float, qty: int, config: dict, material_filter: str = None):
    rows = []
    sheet_prices = config["sheet_prices"]
    cut_speed = config["cut_speed"]
    minute_price = config["minute_price"]
    coefficient = config["coefficient"]
    
    qty_coef = qty * coefficient
    
    for material in cut_speed.keys():
        if material_filter and material != material_filter:
            continue
            
        for thickness, speed in sorted(cut_speed[material].items()):
            per_piece_min = total_len_m / speed if speed > 0 else float("inf")
            total_min = per_piece_min * qty
            
            sheet_price = sheet_prices.get(material, {}).get(thickness, 0.0)
            
            if qty_coef > 0 and qty > 0:
                price_per_piece = (sheet_price / qty_coef) + (minute_price * total_min / qty)
            else:
                price_per_piece = 0.0
            
            rows.append({
                "Material": material,
                "Espessura_mm": thickness,
                "Velocidade_m_min": speed,
                "Min_por_peca": round(per_piece_min, 3),
                "Quantidade": qty,
                "Min_total": round(total_min, 3),
                "Preco_unitario": round(price_per_piece, 2),
                "Valor_chapa": sheet_price,
            })
    return rows

# -----------------------------
# GUI Principal
# -----------------------------
class App:
    def __init__(self, root: TkinterDnD.Tk):
        self.root = root
        self.root.title("Sistema de Nesting + Preços")
        self.root.geometry("1000x650")
        
        self.config = load_config()
        self.dxf_files = []
        
        # Notebook principal
        self.main_notebook = ttk.Notebook(root)
        self.main_notebook.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Frame de cálculos
        self.calc_container = ttk.Frame(self.main_notebook)
        self.main_notebook.add(self.calc_container, text="Cálculos")
        
        # Notebook para resultados
        self.results_notebook = ttk.Notebook(self.calc_container)
        self.results_notebook.pack(fill="both", expand=True, pady=(0, 10))
        
        # Frame de controles
        self._init_controls()
        
        # Aba de configurações
        self.frame_config = ttk.Frame(self.main_notebook, padding=15)
        self.main_notebook.add(self.frame_config, text="Configurações")
        self._init_config_tab()
        
        # Configura drag and drop
        self._setup_drag_drop()
    
    def _init_controls(self):
        """Inicializa área de controles e status"""
        control_frame = ttk.Frame(self.calc_container)
        control_frame.pack(fill="x", padx=10, pady=(10, 0))
        
        # Área de drag and drop - EXPANDIDA PARA OCUPAR TODO O ESPAÇO
        self.drop_frame = ttk.LabelFrame(control_frame, text="📁 Arquivos DXF", padding=20)
        self.drop_frame.pack(fill="both", expand=True, pady=(0, 10))
        
        # Label interno centralizado
        label_container = ttk.Frame(self.drop_frame)
        label_container.pack(expand=True)
        
        self.drop_label = ttk.Label(
            label_container,
            text="Arraste arquivos DXF aqui ou clique em 'Adicionar'\nSuporta múltiplos arquivos",
            justify="center",
            foreground="#666",
            font=("", 10)
        )
        self.drop_label.pack(pady=30)
        
        # Botões
        btn_frame = ttk.Frame(self.drop_frame)
        btn_frame.pack(pady=(10, 0))
        
        ttk.Button(btn_frame, text="Adicionar DXF", command=self.add_dxf_files).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Limpar", command=self.clear_files).pack(side="left", padx=5)
        self.btn_calculate = ttk.Button(btn_frame, text="Calcular Todos", command=self.calculate_all)
        self.btn_calculate.pack(side="left", padx=5)
        self.btn_calculate.config(state="disabled")
        
        # Status
        self.var_status = StringVar(value="Aguardando arquivos")
        ttk.Label(control_frame, textvariable=self.var_status, foreground="#666").pack(pady=5)
    
    def _setup_drag_drop(self):
        """Configura drag and drop - TODA A ÁREA DO FRAME"""
        self.drop_frame.drop_target_register(DND_FILES)
        self.drop_frame.dnd_bind('<<Drop>>', self.on_drop)
        
        self.drop_label.drop_target_register(DND_FILES)
        self.drop_label.dnd_bind('<<Drop>>', self.on_drop)
    
    def on_drop(self, event):
        """Handler para drag and drop"""
        files = self.root.tk.splitlist(event.data)
        dxf_files = [f for f in files if f.lower().endswith('.dxf')]
        
        if dxf_files:
            for file in dxf_files:
                if file not in self.dxf_files:
                    self.dxf_files.append(file)
            
            self.update_file_list()
    
    def add_dxf_files(self):
        """Adiciona arquivos DXF via dialog"""
        files = filedialog.askopenfilenames(
            title="Selecione arquivos DXF",
            filetypes=[("DXF files", "*.dxf"), ("Todos", "*.*")]
        )
        
        if files:
            for file in files:
                if file not in self.dxf_files:
                    self.dxf_files.append(file)
            
            self.update_file_list()
    
    def clear_files(self):
        """Limpa lista de arquivos"""
        self.dxf_files.clear()
        
        # Remove todas as abas de resultados
        for tab in self.results_notebook.tabs():
            self.results_notebook.forget(tab)
        
        self.update_file_list()
    
    def update_file_list(self):
        """Atualiza exibição da lista de arquivos"""
        if self.dxf_files:
            file_names = "\n".join([os.path.basename(f) for f in self.dxf_files])
            self.drop_label.config(
                text=f"📄 {len(self.dxf_files)} arquivo(s) carregado(s):\n{file_names}",
                foreground="#000"
            )
            self.btn_calculate.config(state="normal")
            self.var_status.set(f"{len(self.dxf_files)} arquivo(s) pronto(s) para calcular")
        else:
            self.drop_label.config(
                text="Arraste arquivos DXF aqui ou clique em 'Adicionar'\nSuporta múltiplos arquivos",
                foreground="#666"
            )
            self.btn_calculate.config(state="disabled")
            self.var_status.set("Aguardando arquivos")
    
    def calculate_all(self):
        """Calcula todos os arquivos carregados"""
        if not self.dxf_files:
            return
        
        self.btn_calculate.config(state="disabled")
        self._update_config_from_ui()
        
        # Remove abas antigas
        for tab in self.results_notebook.tabs():
            self.results_notebook.forget(tab)
        
        # Processa cada arquivo em thread
        threading.Thread(
            target=self._process_all_files,
            daemon=True
        ).start()
    
    def _process_all_files(self):
        """Processa todos os arquivos DXF"""
        total = len(self.dxf_files)
        
        for idx, dxf_file in enumerate(self.dxf_files, 1):
            file_name = os.path.basename(dxf_file)
            
            self.root.after(0, lambda: self.var_status.set(
                f"Processando {idx}/{total}: {file_name}..."
            ))
            
            try:
                # Calcula comprimento
                total_len_m = compute_length_m(dxf_file, tol=0.3)
                
                all_rows = []
                
                # Processa cada material
                for material in ["Inox", "Carbono"]:
                    sheet_size = self.config["sheet_sizes"][material]
                    w, h = sheet_size["w"], sheet_size["h"]
                    
                    qty_material = run_nesting_and_get_qty(
                        infile=dxf_file,
                        w=w, h=h,
                        out_dir=f"outputs_nesting_{material.lower()}_{idx}"
                    )
                    
                    rows = compute_times_and_prices(
                        total_len_m=total_len_m,
                        qty=qty_material,
                        config=self.config,
                        material_filter=material
                    )
                    
                    all_rows.extend(rows)
                
                # Cria aba com resultados
                self.root.after(0, self._create_result_tab, file_name, all_rows)
                
            except Exception as e:
                error_msg = f"Erro ao processar {file_name}: {str(e)}"
                self.root.after(0, lambda msg=error_msg: messagebox.showerror("Erro", msg))
        
        self.root.after(0, lambda: self.var_status.set(
            f"✓ Concluído: {total} arquivo(s) processado(s)"
        ))
        self.root.after(0, lambda: self.btn_calculate.config(state="normal"))
    
    def _create_result_tab(self, file_name, rows):
        """Cria aba com resultados para um arquivo"""
        # Frame da aba
        tab_frame = ttk.Frame(self.results_notebook)
        self.results_notebook.add(tab_frame, text=file_name[:25])
        
        # Tabela
        cols = ("Material", "Esp(mm)", "Vel(m/min)", "Min/peça", "Qtd", "Min total", "💰 Preço R$")
        tree = ttk.Treeview(tab_frame, columns=cols, show="headings", height=20)
        
        col_widths = [85, 70, 85, 75, 55, 80, 110]
        for col, width in zip(cols, col_widths):
            tree.heading(col, text=col)
            tree.column(col, width=width, anchor="center")
        
        # Estilo zebra e destaque de preço
        tree.tag_configure("oddrow", background="#f9f9f9")
        tree.tag_configure("evenrow", background="#ffffff")
        tree.tag_configure("price_highlight", foreground="#047857", font=("", 9, "bold"))
        
        # Adiciona dados
        for idx, r in enumerate(rows):
            tag = "oddrow" if idx % 2 == 0 else "evenrow"
            values = (
                r["Material"],
                f'{r["Espessura_mm"]:.2f}',
                f'{r["Velocidade_m_min"]:.2f}',
                f'{r["Min_por_peca"]:.3f}',
                str(r["Quantidade"]),
                f'{r["Min_total"]:.3f}',
                f'R$ {r["Preco_unitario"]:.2f}',
            )
            item = tree.insert("", "end", values=values, tags=(tag, "price_highlight"))
        
        tree.pack(side="left", fill="both", expand=True)
        
        # Scrollbar
        vsb = ttk.Scrollbar(tab_frame, orient="vertical", command=tree.yview)
        tree.configure(yscroll=vsb.set)
        vsb.pack(side="right", fill="y")
        
        # Bind para copiar preço ao clicar
        tree.bind('<Button-1>', lambda e: self._on_tree_click(e, tree))
        
        # Seleciona a nova aba
        self.results_notebook.select(tab_frame)
    
    def _on_tree_click(self, event, tree):
        """Handler para clique na tabela - copia preço"""
        region = tree.identify("region", event.x, event.y)
        
        if region == "cell":
            column = tree.identify_column(event.x)
            item = tree.identify_row(event.y)
            
            # Coluna 7 é a coluna de preço (índice #6)
            if column == "#7" and item:
                values = tree.item(item, "values")
                price_text = values[6]  # "R$ XX.XX"
                
                # Extrai apenas o número
                price_number = price_text.replace("R$", "").strip()
                
                # Copia para área de transferência
                self.root.clipboard_clear()
                self.root.clipboard_append(price_number)
                
                # Feedback visual
                original_bg = tree.tag_configure("price_highlight", "background")
                tree.tag_configure("price_highlight", background="#d1fae5")
                
                def reset_bg():
                    tree.tag_configure("price_highlight", background=original_bg[4] if original_bg else "")
                
                self.root.after(200, reset_bg)
                
                self.var_status.set(f"✓ Preço copiado: {price_number}")
    
    def _init_config_tab(self):
        """Inicializa aba de configurações - OCUPANDO TODA A ÁREA"""
        from tkinter import Canvas
        
        # Canvas com scroll
        canvas = Canvas(self.frame_config)
        scrollbar = ttk.Scrollbar(self.frame_config, orient="vertical", command=canvas.yview)
        
        # Frame scrollável
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Frame principal ocupando toda a largura
        main_frame = ttk.Frame(scrollable_frame)
        main_frame.pack(fill="both", expand=True, padx=50, pady=30)
        
        # Configurações gerais
        general_frame = ttk.LabelFrame(main_frame, text="Configurações Gerais", padding=20)
        general_frame.pack(fill="x", pady=(0, 15))
        
        row = 0
        ttk.Label(general_frame, text="Valor do Minuto (R$):").grid(row=row, column=0, sticky=W, pady=8, padx=(0, 20))
        self.var_minute_price = DoubleVar(value=self.config["minute_price"])
        ttk.Entry(general_frame, textvariable=self.var_minute_price, width=15).grid(row=row, column=1, sticky=W)
        
        row += 1
        ttk.Label(general_frame, text="Coeficiente:").grid(row=row, column=0, sticky=W, pady=8, padx=(0, 20))
        self.var_coefficient = DoubleVar(value=self.config["coefficient"])
        ttk.Entry(general_frame, textvariable=self.var_coefficient, width=15).grid(row=row, column=1, sticky=W)
        
        # Tamanhos das chapas
        size_frame = ttk.LabelFrame(main_frame, text="Tamanhos das Chapas (mm)", padding=20)
        size_frame.pack(fill="x", pady=(0, 15))
        
        self.size_vars = {}
        
        for idx, material in enumerate(["Inox", "Carbono"]):
            self.size_vars[material] = {}
            
            ttk.Label(size_frame, text=f"{material}:", font=("", 9, "bold")).grid(
                row=idx, column=0, sticky=W, pady=8, padx=(0, 20)
            )
            
            w_var = DoubleVar(value=self.config["sheet_sizes"][material]["w"])
            self.size_vars[material]["w"] = w_var
            ttk.Entry(size_frame, textvariable=w_var, width=12).grid(row=idx, column=1, padx=(0, 5))
            
            ttk.Label(size_frame, text="×").grid(row=idx, column=2, padx=5)
            
            h_var = DoubleVar(value=self.config["sheet_sizes"][material]["h"])
            self.size_vars[material]["h"] = h_var
            ttk.Entry(size_frame, textvariable=h_var, width=12).grid(row=idx, column=3)
        
        # Preços e velocidades lado a lado - OCUPANDO TODO O ESPAÇO
        materials_container = ttk.Frame(main_frame)
        materials_container.pack(fill="both", expand=True, pady=(0, 15))
        
        self.price_vars = {}
        self.speed_vars = {}
        
        for col_idx, material in enumerate(["Inox", "Carbono"]):
            material_frame = ttk.LabelFrame(materials_container, text=material, padding=15)
            material_frame.grid(row=0, column=col_idx, sticky=(N, S, E, W), padx=5)
            
            # Cabeçalhos
            ttk.Label(material_frame, text="Espessura (mm)", font=("", 9, "bold")).grid(row=0, column=0, padx=10, pady=8, sticky=W)
            ttk.Label(material_frame, text="Preço (R$)", font=("", 9, "bold")).grid(row=0, column=1, padx=10, pady=8, sticky=W)
            ttk.Label(material_frame, text="Velocidade (m/min)", font=("", 9, "bold")).grid(row=0, column=2, padx=10, pady=8, sticky=W)
            
            self.price_vars[material] = {}
            self.speed_vars[material] = {}
            
            for idx, thickness in enumerate(sorted(self.config["sheet_prices"][material].keys()), start=1):
                ttk.Label(material_frame, text=f"{thickness} mm").grid(row=idx, column=0, sticky=W, padx=10, pady=5)
                
                price_var = DoubleVar(value=self.config["sheet_prices"][material][thickness])
                self.price_vars[material][thickness] = price_var
                ttk.Entry(material_frame, textvariable=price_var, width=15).grid(row=idx, column=1, padx=10, pady=5, sticky=W)
                
                speed_var = DoubleVar(value=self.config["cut_speed"][material][thickness])
                self.speed_vars[material][thickness] = speed_var
                ttk.Entry(material_frame, textvariable=speed_var, width=15).grid(row=idx, column=2, padx=10, pady=5, sticky=W)
            
            # Expande as colunas proporcionalmente
            material_frame.columnconfigure(0, weight=1)
            material_frame.columnconfigure(1, weight=1)
            material_frame.columnconfigure(2, weight=1)
        
        # Distribui peso igualmente entre as colunas
        materials_container.columnconfigure(0, weight=1)
        materials_container.columnconfigure(1, weight=1)
        
        # Botões centralizados
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=(10, 20))
        
        ttk.Button(btn_frame, text="💾 Salvar Configurações", command=self.save_config_ui, width=22).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="🔄 Restaurar Padrões", command=self.reset_config_ui, width=22).pack(side="left", padx=5)
    
    def _update_config_from_ui(self):
        """Atualiza self.config com valores da UI"""
        self.config["minute_price"] = self.var_minute_price.get()
        self.config["coefficient"] = self.var_coefficient.get()
        
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
            messagebox.showinfo("Sucesso", "Configurações salvas!")
        else:
            messagebox.showerror("Erro", "Erro ao salvar.")
    
    def reset_config_ui(self):
        """Restaura configurações padrão"""
        if messagebox.askyesno("Confirmar", "Restaurar configurações padrão?"):
            self.config = json.loads(json.dumps(DEFAULT_CONFIG))
            
            self.var_minute_price.set(self.config["minute_price"])
            self.var_coefficient.set(self.config["coefficient"])
            
            for material in self.size_vars:
                self.size_vars[material]["w"].set(self.config["sheet_sizes"][material]["w"])
                self.size_vars[material]["h"].set(self.config["sheet_sizes"][material]["h"])
            
            for material in self.price_vars:
                for thickness, var in self.price_vars[material].items():
                    var.set(self.config["sheet_prices"][material][thickness])
            
            for material in self.speed_vars:
                for thickness, var in self.speed_vars[material].items():
                    var.set(self.config["cut_speed"][material][thickness])
            
            messagebox.showinfo("Sucesso", "Configurações restauradas!")

def main():
    root = TkinterDnD.Tk()
    
    style = ttk.Style()
    if "clam" in style.theme_names():
        style.theme_use("clam")
    
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()