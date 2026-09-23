
import os
import sqlite3
import shutil
import hashlib
import base64
import json
import xml.etree.ElementTree as ET
from datetime import datetime, date
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, jsonify

APP_NAME = "CENTRALVET Agropecuária"
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "centralvet.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "centralvet-veltrix-2026")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "1234")


# ---------------- Helpers ----------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def money_to_float(v):
    if v is None:
        return 0.0
    s = str(v).strip()
    if not s:
        return 0.0
    s = s.replace("R$", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0

def num_to_float(v):
    return money_to_float(v)

def today_str():
    return date.today().isoformat()

def br_date(s):
    if not s:
        return ""
    try:
        if "/" in str(s):
            return s
        y, m, d = str(s)[:10].split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return str(s)

def fmt_money(v):
    try:
        v = float(v or 0)
    except Exception:
        v = 0
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def fmt_num(v):
    try:
        v = float(v or 0)
    except Exception:
        v = 0
    txt = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return txt

app.jinja_env.filters["money"] = fmt_money
app.jinja_env.filters["num"] = fmt_num
app.jinja_env.filters["brdate"] = br_date

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("logged"):
            return fn(*args, **kwargs)
        return redirect(url_for("login"))
    return wrapper

def exec_sql(sql, params=()):
    with get_db() as db:
        db.execute(sql, params)
        db.commit()

def fetch_one(sql, params=()):
    with get_db() as db:
        return db.execute(sql, params).fetchone()

def fetch_all(sql, params=()):
    with get_db() as db:
        return db.execute(sql, params).fetchall()

def backup_db(reason="manual"):
    if not os.path.exists(DB_PATH):
        return None
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    name = f"backup-centralvet-{reason}-{stamp}.db"
    dest = os.path.join(BACKUP_DIR, name)
    shutil.copy2(DB_PATH, dest)
    # keep latest 40 backups
    files = sorted([os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR) if f.endswith(".db")])
    for old in files[:-40]:
        try:
            os.remove(old)
        except Exception:
            pass
    return dest

def add_column(db, table, column, ddl):
    cols = [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def xml_local(tag):
    return tag.split('}', 1)[-1] if '}' in tag else tag

def xml_first_text(node, name, default=""):
    if node is None:
        return default
    for child in node.iter():
        if xml_local(child.tag) == name:
            return (child.text or "").strip()
    return default

def xml_first(node, name):
    if node is None:
        return None
    for child in node.iter():
        if xml_local(child.tag) == name:
            return child
    return None

def xml_all(root, name):
    return [child for child in root.iter() if xml_local(child.tag) == name]

def only_digits(v):
    return ''.join(ch for ch in str(v or '') if ch.isdigit())

def xml_float(v):
    try:
        return float(str(v or '0').replace(',', '.'))
    except Exception:
        return 0.0

def get_product_tax(det):
    for key in ["CSOSN", "CST"]:
        val = xml_first_text(det, key, "")
        if val:
            return val
    return ""

def parse_nfe_xml(xml_bytes):
    """Extrai os principais dados de XML NF-e/NFC-e de entrada."""
    root = ET.fromstring(xml_bytes)
    inf = xml_first(root, "infNFe")
    chave = ""
    if inf is not None:
        chave = (inf.attrib.get("Id") or "").replace("NFe", "").strip()
    ide = xml_first(root, "ide")
    emit = xml_first(root, "emit")
    total_node = xml_first(root, "ICMSTot")
    dest = xml_first(root, "dest")
    fornecedor_nome = xml_first_text(emit, "xNome") or xml_first_text(emit, "xFant")
    fornecedor_cnpj = xml_first_text(emit, "CNPJ") or xml_first_text(emit, "CPF")
    dados = {
        "chave": chave,
        "numero": xml_first_text(ide, "nNF"),
        "serie": xml_first_text(ide, "serie"),
        "emissao": (xml_first_text(ide, "dhEmi") or xml_first_text(ide, "dEmi"))[:10],
        "fornecedor_nome": fornecedor_nome,
        "fornecedor_cnpj": fornecedor_cnpj,
        "destinatario_nome": xml_first_text(dest, "xNome"),
        "destinatario_cnpj": xml_first_text(dest, "CNPJ") or xml_first_text(dest, "CPF"),
        "natureza": xml_first_text(ide, "natOp"),
        "modelo": xml_first_text(ide, "mod"),
        "total": xml_float(xml_first_text(total_node, "vNF")),
        "itens": []
    }
    for det in xml_all(root, "det"):
        prod = xml_first(det, "prod")
        if prod is None:
            continue
        item = {
            "codigo": xml_first_text(prod, "cProd"),
            "ean": xml_first_text(prod, "cEAN") or xml_first_text(prod, "cEANTrib"),
            "nome": xml_first_text(prod, "xProd"),
            "ncm": xml_first_text(prod, "NCM"),
            "cest": xml_first_text(prod, "CEST"),
            "cfop": xml_first_text(prod, "CFOP"),
            "unidade": xml_first_text(prod, "uCom") or xml_first_text(prod, "uTrib") or "UN",
            "quantidade": xml_float(xml_first_text(prod, "qCom") or xml_first_text(prod, "qTrib")),
            "valor_unitario": xml_float(xml_first_text(prod, "vUnCom") or xml_first_text(prod, "vUnTrib")),
            "valor_total": xml_float(xml_first_text(prod, "vProd")),
            "cst_csosn": get_product_tax(det)
        }
        if item["nome"]:
            dados["itens"].append(item)
    return dados

def cert_runtime_status():
    path = os.environ.get("CERTIFICADO_PATH", "").strip()
    has_pass = bool(os.environ.get("CERTIFICADO_SENHA", "").strip())
    exists = bool(path and os.path.exists(path))
    return {"path": path, "has_password": has_pass, "exists": exists}


def cfg_val(cfg, key):
    try:
        return str(cfg[key] or '').strip()
    except Exception:
        return ''

def fiscal_checklist(cfg, cert_status):
    checks = [
        {"item": "Certificado A1 no servidor", "ok": bool(cert_status.get("exists")), "obs": cert_status.get("path") or "Verifique o upload do A1"},
        {"item": "Senha do certificado no Coolify", "ok": bool(cert_status.get("has_password")), "obs": "Variável CERTIFICADO_SENHA"},
        {"item": "CNPJ da empresa", "ok": bool(cfg_val(cfg, "cnpj")), "obs": cfg_val(cfg, "cnpj") or "Preencher CNPJ"},
        {"item": "Inscrição Estadual", "ok": bool(cfg_val(cfg, "inscricao_estadual")), "obs": cfg_val(cfg, "inscricao_estadual") or "Preencher IE"},
        {"item": "Regime tributário", "ok": bool(cfg_val(cfg, "regime")), "obs": cfg_val(cfg, "regime") or "Confirmar com contador"},
        {"item": "Série e número inicial NF-e", "ok": bool(cfg_val(cfg, "nf_serie") and cfg_val(cfg, "nf_numero_inicial")), "obs": f"Série {cfg_val(cfg,'nf_serie') or '-'} • Nº {cfg_val(cfg,'nf_numero_inicial') or '-'}"},
        {"item": "Série e número inicial NFC-e", "ok": bool(cfg_val(cfg, "nfce_serie") and cfg_val(cfg, "nfce_numero_inicial")), "obs": f"Série {cfg_val(cfg,'nfce_serie') or '-'} • Nº {cfg_val(cfg,'nfce_numero_inicial') or '-'}"},
        {"item": "CSC/Token da NFC-e", "ok": bool(cfg_val(cfg, "csc_token")), "obs": "Preenchido" if cfg_val(cfg, "csc_token") else "Obrigatório para NFC-e em produção"},
        {"item": "CFOP padrão", "ok": bool(cfg_val(cfg, "cfop_padrao")), "obs": cfg_val(cfg, "cfop_padrao") or "Pode usar provisório em homologação"},
        {"item": "CST/CSOSN padrão", "ok": bool(cfg_val(cfg, "cst_csosn_padrao")), "obs": cfg_val(cfg, "cst_csosn_padrao") or "Pode usar provisório em homologação"},
    ]
    return checks

def fiscal_prod_ready(cfg, cert_status):
    required = ["cnpj", "inscricao_estadual", "regime", "nf_serie", "nf_numero_inicial", "nfce_serie", "nfce_numero_inicial", "csc_token", "cfop_padrao", "cst_csosn_padrao"]
    missing = [r for r in required if not cfg_val(cfg, r)]
    if not cert_status.get("exists"):
        missing.append("certificado_a1")
    if not cert_status.get("has_password"):
        missing.append("senha_certificado")
    return (len(missing) == 0, missing)

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            cpf_cnpj TEXT,
            telefone TEXT,
            email TEXT,
            endereco TEXT,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fornecedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            cpf_cnpj TEXT,
            telefone TEXT,
            email TEXT,
            endereco TEXT,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS produtos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT,
            nome TEXT NOT NULL,
            categoria TEXT,
            unidade TEXT DEFAULT 'UN',
            preco_custo REAL DEFAULT 0,
            preco_venda REAL DEFAULT 0,
            estoque REAL DEFAULT 0,
            estoque_minimo REAL DEFAULT 0,
            ncm TEXT,
            cfop TEXT,
            cst_csosn TEXT,
            aliquota REAL DEFAULT 0,
            ativo INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS estoque_mov (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT,
            produto_id INTEGER,
            tipo TEXT,
            quantidade REAL DEFAULT 0,
            custo_unit REAL DEFAULT 0,
            valor_total REAL DEFAULT 0,
            origem TEXT,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS vendas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT,
            cliente_id INTEGER,
            cliente_nome TEXT,
            forma_pagamento TEXT,
            status TEXT,
            subtotal REAL DEFAULT 0,
            desconto REAL DEFAULT 0,
            total REAL DEFAULT 0,
            lucro REAL DEFAULT 0,
            observacao TEXT,
            nf_tipo TEXT,
            nf_status TEXT DEFAULT 'Não emitida',
            nf_numero TEXT,
            nf_obs TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS venda_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            venda_id INTEGER,
            produto_id INTEGER,
            produto_nome TEXT,
            quantidade REAL DEFAULT 0,
            preco_unit REAL DEFAULT 0,
            custo_unit REAL DEFAULT 0,
            total REAL DEFAULT 0,
            lucro REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS financeiro (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT,
            tipo TEXT,
            descricao TEXT,
            valor REAL DEFAULT 0,
            forma TEXT,
            status TEXT DEFAULT 'Pago',
            vencimento TEXT,
            pessoa TEXT,
            referencia_tipo TEXT,
            referencia_id INTEGER,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fiscal_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            razao_social TEXT,
            nome_fantasia TEXT,
            cnpj TEXT,
            inscricao_estadual TEXT,
            endereco TEXT,
            municipio TEXT,
            uf TEXT,
            cep TEXT,
            telefone TEXT,
            email TEXT,
            regime TEXT,
            ambiente TEXT DEFAULT 'Homologação',
            certificado_nome TEXT,
            certificado_path TEXT,
            certificado_senha_env TEXT,
            nf_serie TEXT,
            nf_numero_inicial TEXT,
            nfce_serie TEXT,
            nfce_numero_inicial TEXT,
            csc_id TEXT,
            csc_token TEXT,
            cfop_padrao TEXT,
            cst_csosn_padrao TEXT,
            observacao TEXT
        );
        CREATE TABLE IF NOT EXISTS xml_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chave TEXT UNIQUE,
            xml_hash TEXT UNIQUE,
            numero TEXT,
            serie TEXT,
            emissao TEXT,
            fornecedor_nome TEXT,
            fornecedor_cnpj TEXT,
            total REAL DEFAULT 0,
            itens_qtd INTEGER DEFAULT 0,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS devolucoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT,
            fornecedor_nome TEXT,
            fornecedor_cnpj TEXT,
            destinatario_nome TEXT,
            destinatario_cnpj TEXT,
            chave_origem TEXT,
            numero_origem TEXT,
            serie_origem TEXT,
            emissao_origem TEXT,
            total_original REAL DEFAULT 0,
            motivo TEXT,
            status TEXT DEFAULT 'Rascunho',
            baixou_estoque INTEGER DEFAULT 0,
            nf_devolucao_numero TEXT,
            nf_devolucao_chave TEXT,
            xml_hash TEXT,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS devolucao_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            devolucao_id INTEGER,
            produto_id INTEGER,
            codigo TEXT,
            ean TEXT,
            nome TEXT,
            ncm TEXT,
            cest TEXT,
            cfop_original TEXT,
            cfop_devolucao TEXT,
            cst_csosn TEXT,
            unidade TEXT,
            quantidade_original REAL DEFAULT 0,
            quantidade_devolver REAL DEFAULT 0,
            valor_unitario REAL DEFAULT 0,
            valor_total REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # default fiscal config
        db.execute("""
            INSERT OR IGNORE INTO fiscal_config
            (id, razao_social, nome_fantasia, cnpj, ambiente, uf, municipio, certificado_path, certificado_senha_env)
            VALUES (1, 'CENTRALVET AGROPECUÁRIA LTDA', 'CENTRALVET AGROPECUÁRIA', '68.690.225/0001-50', 'Homologação', 'MG', '', '/app/certs/centralvet_a1.pfx', 'CERTIFICADO_SENHA')
        """)
        for column, ddl in [
            ('ean', 'TEXT'), ('cest', 'TEXT'), ('ultima_chave_xml', 'TEXT'), ('fornecedor_id', 'INTEGER')
        ]:
            add_column(db, 'produtos', column, ddl)
        for column, ddl in [
            ('certificado_path', 'TEXT'), ('certificado_senha_env', 'TEXT'), ('nf_serie', 'TEXT'), ('nf_numero_inicial', 'TEXT'),
            ('nfce_serie', 'TEXT'), ('nfce_numero_inicial', 'TEXT'), ('csc_id', 'TEXT'), ('csc_token', 'TEXT'),
            ('cfop_padrao', 'TEXT'), ('cst_csosn_padrao', 'TEXT')
        ]:
            add_column(db, 'fiscal_config', column, ddl)
        db.execute("UPDATE fiscal_config SET certificado_path=COALESCE(NULLIF(certificado_path,''), '/app/certs/centralvet_a1.pfx'), certificado_senha_env=COALESCE(NULLIF(certificado_senha_env,''), 'CERTIFICADO_SENHA') WHERE id=1")
        db.commit()

init_db()


# ---------------- Auth ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("usuario") == ADMIN_USER and request.form.get("senha") == ADMIN_PASSWORD:
            session["logged"] = True
            session["user"] = ADMIN_USER
            return redirect(url_for("index"))
        flash("Usuário ou senha inválidos.", "erro")
    return render_template("login.html", app_name=APP_NAME)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- Dashboard ----------------

@app.route("/")
@login_required
def index():
    hoje = today_str()
    total_produtos = fetch_one("SELECT COUNT(*) c FROM produtos WHERE ativo=1")["c"]
    baixo = fetch_one("SELECT COUNT(*) c FROM produtos WHERE ativo=1 AND estoque <= estoque_minimo")["c"]
    estoque_valor = fetch_one("SELECT COALESCE(SUM(estoque * preco_custo),0) v FROM produtos WHERE ativo=1")["v"]
    vendas_mes = fetch_one("SELECT COALESCE(SUM(total),0) v FROM vendas WHERE substr(data,1,7)=substr(?,1,7)", (hoje,))["v"]
    lucro_mes = fetch_one("SELECT COALESCE(SUM(lucro),0) v FROM vendas WHERE substr(data,1,7)=substr(?,1,7)", (hoje,))["v"]
    a_receber = fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Entrada' AND status!='Pago'")["v"]
    a_pagar = fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Saída' AND status!='Pago'")["v"]
    saldo_caixa = fetch_one("""
        SELECT COALESCE(SUM(CASE WHEN tipo='Entrada' THEN valor ELSE -valor END),0) v
        FROM financeiro WHERE status='Pago'
    """)["v"]
    ult_vendas = fetch_all("SELECT * FROM vendas ORDER BY id DESC LIMIT 6")
    estoque_baixo = fetch_all("SELECT * FROM produtos WHERE ativo=1 AND estoque <= estoque_minimo ORDER BY estoque ASC LIMIT 8")
    pendencias = []
    if baixo:
        pendencias.append(f"{baixo} produto(s) com estoque baixo")
    if a_receber:
        pendencias.append(f"{fmt_money(a_receber)} a receber")
    if a_pagar:
        pendencias.append(f"{fmt_money(a_pagar)} a pagar")
    return render_template("index.html", **locals())


# ---------------- Pessoas ----------------

def salvar_pessoa(table):
    nome = request.form.get("nome", "").strip()
    if not nome:
        flash("Informe o nome.", "erro")
        return None
    data = (
        nome,
        request.form.get("cpf_cnpj", "").strip(),
        request.form.get("telefone", "").strip(),
        request.form.get("email", "").strip(),
        request.form.get("endereco", "").strip(),
        request.form.get("observacao", "").strip()
    )
    with get_db() as db:
        cur = db.execute(f"""
            INSERT INTO {table} (nome, cpf_cnpj, telefone, email, endereco, observacao)
            VALUES (?, ?, ?, ?, ?, ?)
        """, data)
        db.commit()
        return cur.lastrowid

def atualizar_pessoa(table, item_id):
    nome = request.form.get("nome", "").strip()
    if not nome:
        flash("Informe o nome.", "erro")
        return
    data = (
        nome,
        request.form.get("cpf_cnpj", "").strip(),
        request.form.get("telefone", "").strip(),
        request.form.get("email", "").strip(),
        request.form.get("endereco", "").strip(),
        request.form.get("observacao", "").strip(),
        item_id
    )
    exec_sql(f"""
        UPDATE {table} SET nome=?, cpf_cnpj=?, telefone=?, email=?, endereco=?, observacao=?
        WHERE id=?
    """, data)

@app.route("/clientes", methods=["GET", "POST"])
@login_required
def clientes():
    if request.method == "POST":
        salvar_pessoa("clientes")
        backup_db("cliente")
        flash("Cliente salvo.", "ok")
        return redirect(url_for("clientes"))
    q = request.args.get("q", "").strip()
    if q:
        rows = fetch_all("SELECT * FROM clientes WHERE nome LIKE ? OR cpf_cnpj LIKE ? OR telefone LIKE ? ORDER BY nome", (f"%{q}%", f"%{q}%", f"%{q}%"))
    else:
        rows = fetch_all("SELECT * FROM clientes ORDER BY id DESC LIMIT 80")
    return render_template("pessoas.html", titulo="Clientes", rows=rows, tipo="clientes", q=q)

@app.route("/clientes/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
def editar_cliente(item_id):
    row = fetch_one("SELECT * FROM clientes WHERE id=?", (item_id,))
    if not row:
        flash("Cliente não encontrado.", "erro")
        return redirect(url_for("clientes"))
    if request.method == "POST":
        atualizar_pessoa("clientes", item_id)
        backup_db("cliente-editado")
        flash("Cliente atualizado.", "ok")
        return redirect(url_for("clientes"))
    return render_template("pessoa_form.html", titulo="Editar cliente", row=row, tipo="clientes")

@app.route("/fornecedores", methods=["GET", "POST"])
@login_required
def fornecedores():
    if request.method == "POST":
        salvar_pessoa("fornecedores")
        backup_db("fornecedor")
        flash("Fornecedor salvo.", "ok")
        return redirect(url_for("fornecedores"))
    q = request.args.get("q", "").strip()
    if q:
        rows = fetch_all("SELECT * FROM fornecedores WHERE nome LIKE ? OR cpf_cnpj LIKE ? OR telefone LIKE ? ORDER BY nome", (f"%{q}%", f"%{q}%", f"%{q}%"))
    else:
        rows = fetch_all("SELECT * FROM fornecedores ORDER BY id DESC LIMIT 80")
    return render_template("pessoas.html", titulo="Fornecedores", rows=rows, tipo="fornecedores", q=q)

@app.route("/fornecedores/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
def editar_fornecedor(item_id):
    row = fetch_one("SELECT * FROM fornecedores WHERE id=?", (item_id,))
    if not row:
        flash("Fornecedor não encontrado.", "erro")
        return redirect(url_for("fornecedores"))
    if request.method == "POST":
        atualizar_pessoa("fornecedores", item_id)
        backup_db("fornecedor-editado")
        flash("Fornecedor atualizado.", "ok")
        return redirect(url_for("fornecedores"))
    return render_template("pessoa_form.html", titulo="Editar fornecedor", row=row, tipo="fornecedores")


# ---------------- Produtos / Estoque ----------------

@app.route("/produtos", methods=["GET", "POST"])
@login_required
def produtos():
    if request.method == "POST":
        with get_db() as db:
            db.execute("""
                INSERT INTO produtos
                (codigo,nome,categoria,unidade,preco_custo,preco_venda,estoque,estoque_minimo,ncm,cfop,cst_csosn,aliquota,ean,cest)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                request.form.get("codigo","").strip(),
                request.form.get("nome","").strip(),
                request.form.get("categoria","").strip(),
                request.form.get("unidade","UN").strip() or "UN",
                money_to_float(request.form.get("preco_custo")),
                money_to_float(request.form.get("preco_venda")),
                num_to_float(request.form.get("estoque")),
                num_to_float(request.form.get("estoque_minimo")),
                request.form.get("ncm","").strip(),
                request.form.get("cfop","").strip(),
                request.form.get("cst_csosn","").strip(),
                money_to_float(request.form.get("aliquota")),
                request.form.get("ean","").strip(),
                request.form.get("cest","").strip(),
            ))
            db.commit()
        backup_db("produto")
        flash("Produto salvo.", "ok")
        return redirect(url_for("produtos"))
    q = request.args.get("q","").strip()
    if q:
        rows = fetch_all("SELECT * FROM produtos WHERE ativo=1 AND (nome LIKE ? OR codigo LIKE ? OR categoria LIKE ?) ORDER BY nome", (f"%{q}%", f"%{q}%", f"%{q}%"))
    else:
        rows = fetch_all("SELECT * FROM produtos WHERE ativo=1 ORDER BY id DESC LIMIT 100")
    return render_template("produtos.html", rows=rows, q=q)

@app.route("/produtos/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
def editar_produto(item_id):
    row = fetch_one("SELECT * FROM produtos WHERE id=?", (item_id,))
    if not row:
        flash("Produto não encontrado.", "erro")
        return redirect(url_for("produtos"))
    if request.method == "POST":
        exec_sql("""
            UPDATE produtos SET codigo=?, nome=?, categoria=?, unidade=?, preco_custo=?, preco_venda=?,
            estoque=?, estoque_minimo=?, ncm=?, cfop=?, cst_csosn=?, aliquota=?, ean=?, cest=? WHERE id=?
        """, (
            request.form.get("codigo","").strip(),
            request.form.get("nome","").strip(),
            request.form.get("categoria","").strip(),
            request.form.get("unidade","UN").strip() or "UN",
            money_to_float(request.form.get("preco_custo")),
            money_to_float(request.form.get("preco_venda")),
            num_to_float(request.form.get("estoque")),
            num_to_float(request.form.get("estoque_minimo")),
            request.form.get("ncm","").strip(),
            request.form.get("cfop","").strip(),
            request.form.get("cst_csosn","").strip(),
            money_to_float(request.form.get("aliquota")),
            request.form.get("ean","").strip(),
            request.form.get("cest","").strip(),
            item_id
        ))
        backup_db("produto-editado")
        flash("Produto atualizado.", "ok")
        return redirect(url_for("produtos"))
    return render_template("produto_form.html", row=row)

@app.route("/produtos/<int:item_id>/excluir", methods=["POST"])
@login_required
def excluir_produto(item_id):
    exec_sql("UPDATE produtos SET ativo=0 WHERE id=?", (item_id,))
    backup_db("produto-excluido")
    flash("Produto removido.", "ok")
    return redirect(url_for("produtos"))

@app.route("/estoque", methods=["GET", "POST"])
@login_required
def estoque():
    produtos_list = fetch_all("SELECT * FROM produtos WHERE ativo=1 ORDER BY nome")
    if request.method == "POST":
        produto_id = int(request.form.get("produto_id") or 0)
        tipo = request.form.get("tipo") or "Entrada"
        qtd = num_to_float(request.form.get("quantidade"))
        custo = money_to_float(request.form.get("custo_unit"))
        valor_total = qtd * custo
        prod = fetch_one("SELECT * FROM produtos WHERE id=?", (produto_id,))
        if not prod or qtd <= 0:
            flash("Informe produto e quantidade válida.", "erro")
            return redirect(url_for("estoque"))
        sinal = 1 if tipo in ["Entrada", "Ajuste +"] else -1
        with get_db() as db:
            db.execute("UPDATE produtos SET estoque=estoque+? WHERE id=?", (sinal*qtd, produto_id))
            if tipo in ["Entrada", "Ajuste +"] and custo > 0:
                db.execute("UPDATE produtos SET preco_custo=? WHERE id=?", (custo, produto_id))
            db.execute("""
                INSERT INTO estoque_mov (data, produto_id, tipo, quantidade, custo_unit, valor_total, origem, observacao)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (request.form.get("data") or today_str(), produto_id, tipo, qtd, custo, valor_total, request.form.get("origem",""), request.form.get("observacao","")))
            db.commit()
        backup_db("estoque")
        flash("Movimentação salva.", "ok")
        return redirect(url_for("estoque"))
    movs = fetch_all("""
        SELECT m.*, p.nome produto_nome, p.codigo codigo FROM estoque_mov m
        LEFT JOIN produtos p ON p.id=m.produto_id
        ORDER BY m.id DESC LIMIT 100
    """)
    return render_template("estoque.html", produtos=produtos_list, movs=movs, hoje=today_str())


# ---------------- Vendas ----------------

@app.route("/vendas")
@login_required
def vendas():
    q = request.args.get("q","").strip()
    if q:
        rows = fetch_all("SELECT * FROM vendas WHERE cliente_nome LIKE ? OR id LIKE ? ORDER BY id DESC", (f"%{q}%", f"%{q}%"))
    else:
        rows = fetch_all("SELECT * FROM vendas ORDER BY id DESC LIMIT 100")
    return render_template("vendas.html", rows=rows, q=q)

@app.route("/vendas/nova", methods=["GET","POST"])
@login_required
def nova_venda():
    clientes_list = fetch_all("SELECT * FROM clientes ORDER BY nome")
    produtos_list = fetch_all("SELECT * FROM produtos WHERE ativo=1 ORDER BY nome")
    if request.method == "POST":
        cliente_id = int(request.form.get("cliente_id") or 0)
        cliente_nome = request.form.get("cliente_nome","").strip()
        if cliente_id:
            c = fetch_one("SELECT * FROM clientes WHERE id=?", (cliente_id,))
            cliente_nome = c["nome"] if c else cliente_nome
        elif cliente_nome:
            with get_db() as db:
                cur = db.execute("INSERT INTO clientes (nome) VALUES (?)", (cliente_nome,))
                cliente_id = cur.lastrowid
                db.commit()
        forma = request.form.get("forma_pagamento","Dinheiro")
        status = request.form.get("status","Pago")
        desconto = money_to_float(request.form.get("desconto"))
        prod_ids = request.form.getlist("produto_id[]")
        qtds = request.form.getlist("quantidade[]")
        precos = request.form.getlist("preco_unit[]")
        itens = []
        subtotal = lucro_total = 0.0
        for pid, q, pu in zip(prod_ids, qtds, precos):
            if not pid:
                continue
            qtd = num_to_float(q)
            preco = money_to_float(pu)
            if qtd <= 0:
                continue
            prod = fetch_one("SELECT * FROM produtos WHERE id=?", (int(pid),))
            if not prod:
                continue
            if qtd > float(prod["estoque"] or 0):
                flash(f"Estoque insuficiente para {prod['nome']}. Disponível: {fmt_num(prod['estoque'])}", "erro")
                return redirect(url_for("nova_venda"))
            custo = float(prod["preco_custo"] or 0)
            total = qtd * preco
            lucro = (preco - custo) * qtd
            subtotal += total
            lucro_total += lucro
            itens.append((int(pid), prod["nome"], qtd, preco, custo, total, lucro))
        if not itens:
            flash("Adicione pelo menos um produto.", "erro")
            return redirect(url_for("nova_venda"))
        total = max(0, subtotal - desconto)
        with get_db() as db:
            cur = db.execute("""
                INSERT INTO vendas (data, cliente_id, cliente_nome, forma_pagamento, status, subtotal, desconto, total, lucro, observacao, nf_tipo)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (request.form.get("data") or today_str(), cliente_id, cliente_nome, forma, status, subtotal, desconto, total, lucro_total, request.form.get("observacao",""), request.form.get("nf_tipo","NFC-e")))
            venda_id = cur.lastrowid
            for pid, nome, qtd, preco, custo, total_item, lucro in itens:
                db.execute("""
                    INSERT INTO venda_itens (venda_id, produto_id, produto_nome, quantidade, preco_unit, custo_unit, total, lucro)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (venda_id, pid, nome, qtd, preco, custo, total_item, lucro))
                db.execute("UPDATE produtos SET estoque=estoque-? WHERE id=?", (qtd, pid))
                db.execute("""
                    INSERT INTO estoque_mov (data, produto_id, tipo, quantidade, custo_unit, valor_total, origem, observacao)
                    VALUES (?, ?, 'Saída', ?, ?, ?, 'Venda', ?)
                """, (request.form.get("data") or today_str(), pid, qtd, custo, qtd*custo, f"Venda #{venda_id}"))
            db.execute("""
                INSERT INTO financeiro (data, tipo, descricao, valor, forma, status, pessoa, referencia_tipo, referencia_id)
                VALUES (?, 'Entrada', ?, ?, ?, ?, ?, 'Venda', ?)
            """, (request.form.get("data") or today_str(), f"Venda #{venda_id}", total, forma, "Pago" if status=="Pago" else "Pendente", cliente_nome, venda_id))
            db.commit()
        backup_db("venda")
        flash("Venda salva e estoque baixado automaticamente.", "ok")
        return redirect(url_for("recibo_venda", venda_id=venda_id))
    return render_template("venda_form.html", clientes=clientes_list, produtos=produtos_list, hoje=today_str())

@app.route("/vendas/<int:venda_id>/recibo")
@login_required
def recibo_venda(venda_id):
    venda = fetch_one("SELECT * FROM vendas WHERE id=?", (venda_id,))
    itens = fetch_all("SELECT * FROM venda_itens WHERE venda_id=?", (venda_id,))
    return render_template("recibo_venda.html", venda=venda, itens=itens)

@app.route("/vendas/<int:venda_id>/fiscal", methods=["POST"])
@login_required
def emitir_fiscal(venda_id):
    venda = fetch_one("SELECT * FROM vendas WHERE id=?", (venda_id,))
    if not venda:
        flash("Venda não encontrada.", "erro")
        return redirect(url_for("vendas"))
    # Estrutura pronta para integração. Nesta primeira versão, marca como pré-nota/fila de emissão.
    nf_tipo = request.form.get("nf_tipo") or venda["nf_tipo"] or "NFC-e"
    nf_num = f"PRE-{venda_id:06d}"
    exec_sql("UPDATE vendas SET nf_tipo=?, nf_status='Pré-nota gerada', nf_numero=?, nf_obs=? WHERE id=?",
             (nf_tipo, nf_num, "Documento preparado para emissão fiscal após configuração de certificado e ambiente fiscal.", venda_id))
    backup_db("fiscal")
    flash(f"{nf_tipo} preparada como {nf_num}.", "ok")
    return redirect(url_for("recibo_venda", venda_id=venda_id))

@app.route("/api/produtos")
@login_required
def api_produtos():
    rows = fetch_all("SELECT id,codigo,nome,estoque,preco_venda,preco_custo,unidade FROM produtos WHERE ativo=1 ORDER BY nome")
    return jsonify([dict(r) for r in rows])


# ---------------- Financeiro ----------------

@app.route("/financeiro", methods=["GET","POST"])
@login_required
def financeiro():
    if request.method == "POST":
        exec_sql("""
            INSERT INTO financeiro (data,tipo,descricao,valor,forma,status,vencimento,pessoa,observacao)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            request.form.get("data") or today_str(),
            request.form.get("tipo","Entrada"),
            request.form.get("descricao","").strip(),
            money_to_float(request.form.get("valor")),
            request.form.get("forma","").strip(),
            request.form.get("status","Pago"),
            request.form.get("vencimento",""),
            request.form.get("pessoa","").strip(),
            request.form.get("observacao","").strip()
        ))
        backup_db("financeiro")
        flash("Lançamento financeiro salvo.", "ok")
        return redirect(url_for("financeiro"))
    q = request.args.get("q","").strip()
    if q:
        rows = fetch_all("SELECT * FROM financeiro WHERE descricao LIKE ? OR pessoa LIKE ? ORDER BY id DESC", (f"%{q}%", f"%{q}%"))
    else:
        rows = fetch_all("SELECT * FROM financeiro ORDER BY id DESC LIMIT 150")
    cards = {
        "entradas": fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Entrada' AND status='Pago'")["v"],
        "saidas": fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Saída' AND status='Pago'")["v"],
        "receber": fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Entrada' AND status!='Pago'")["v"],
        "pagar": fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Saída' AND status!='Pago'")["v"],
    }
    return render_template("financeiro.html", rows=rows, cards=cards, q=q, hoje=today_str())

@app.route("/financeiro/<int:item_id>/pagar", methods=["POST"])
@login_required
def financeiro_pagar(item_id):
    exec_sql("UPDATE financeiro SET status='Pago', data=? WHERE id=?", (today_str(), item_id))
    backup_db("financeiro-pago")
    flash("Marcado como pago.", "ok")
    return redirect(url_for("financeiro"))

@app.route("/financeiro/<int:item_id>/excluir", methods=["POST"])
@login_required
def financeiro_excluir(item_id):
    exec_sql("DELETE FROM financeiro WHERE id=?", (item_id,))
    backup_db("financeiro-excluido")
    flash("Lançamento excluído.", "ok")
    return redirect(url_for("financeiro"))


# ---------------- Fiscal / XML ----------------

@app.route("/fiscal", methods=["GET","POST"])
@login_required
def fiscal():
    if request.method == "POST":
        ambiente = request.form.get("ambiente", "Homologação")
        if ambiente == "Produção":
            temp_cfg = {k: request.form.get(k, "") for k in [
                "cnpj", "inscricao_estadual", "regime", "nf_serie", "nf_numero_inicial", "nfce_serie",
                "nfce_numero_inicial", "csc_token", "cfop_padrao", "cst_csosn_padrao"
            ]}
            class TempCfg(dict):
                def __getitem__(self, key):
                    return self.get(key, "")
            ready, missing = fiscal_prod_ready(TempCfg(temp_cfg), cert_runtime_status())
            if not ready:
                ambiente = "Homologação"
                flash("Mantive em Homologação: ainda falta completar dados fiscais antes de liberar Produção.", "erro")
        exec_sql("""
            UPDATE fiscal_config SET razao_social=?, nome_fantasia=?, cnpj=?, inscricao_estadual=?,
            endereco=?, municipio=?, uf=?, cep=?, telefone=?, email=?, regime=?, ambiente=?, certificado_nome=?,
            certificado_path=?, certificado_senha_env=?, nf_serie=?, nf_numero_inicial=?, nfce_serie=?, nfce_numero_inicial=?,
            csc_id=?, csc_token=?, cfop_padrao=?, cst_csosn_padrao=?, observacao=?
            WHERE id=1
        """, (
            request.form.get("razao_social",""), request.form.get("nome_fantasia",""), request.form.get("cnpj",""),
            request.form.get("inscricao_estadual",""), request.form.get("endereco",""), request.form.get("municipio",""),
            request.form.get("uf",""), request.form.get("cep",""), request.form.get("telefone",""), request.form.get("email",""),
            request.form.get("regime",""), ambiente, request.form.get("certificado_nome",""),
            request.form.get("certificado_path",""), request.form.get("certificado_senha_env","CERTIFICADO_SENHA"),
            request.form.get("nf_serie",""), request.form.get("nf_numero_inicial",""), request.form.get("nfce_serie",""),
            request.form.get("nfce_numero_inicial",""), request.form.get("csc_id",""), request.form.get("csc_token",""),
            request.form.get("cfop_padrao",""), request.form.get("cst_csosn_padrao",""), request.form.get("observacao","")
        ))
        backup_db("fiscal-config")
        flash("Configuração fiscal salva.", "ok")
        return redirect(url_for("fiscal"))
    cfg = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    notas = fetch_all("SELECT * FROM vendas WHERE nf_status!='Não emitida' ORDER BY id DESC LIMIT 80")
    xmls = fetch_all("SELECT * FROM xml_imports ORDER BY id DESC LIMIT 10")
    devolucoes = fetch_all("SELECT * FROM devolucoes ORDER BY id DESC LIMIT 8")
    cert_status = cert_runtime_status()
    envs = {
        "CERTIFICADO_PATH": os.environ.get("CERTIFICADO_PATH", ""),
        "CERTIFICADO_SENHA": "configurada" if os.environ.get("CERTIFICADO_SENHA") else "não configurada",
        "AMBIENTE_FISCAL": os.environ.get("AMBIENTE_FISCAL", "homologacao"),
        "UF_EMPRESA": os.environ.get("UF_EMPRESA", "MG"),
    }
    checklist = fiscal_checklist(cfg, cert_status)
    prod_ready, prod_missing = fiscal_prod_ready(cfg, cert_status)
    return render_template("fiscal.html", cfg=cfg, notas=notas, xmls=xmls, devolucoes=devolucoes, cert_status=cert_status, envs=envs, checklist=checklist, prod_ready=prod_ready, prod_missing=prod_missing)

@app.route("/fiscal/preencher-padrao", methods=["POST"])
@login_required
def fiscal_preencher_padrao():
    """Preenche campos provisórios para homologação quando a contabilidade demora a responder.
    Não libera produção sem os dados reais.
    """
    exec_sql("""
        UPDATE fiscal_config SET
            ambiente='Homologação',
            uf=COALESCE(NULLIF(uf,''), 'MG'),
            certificado_path=COALESCE(NULLIF(certificado_path,''), '/app/certs/centralvet_a1.pfx'),
            certificado_senha_env=COALESCE(NULLIF(certificado_senha_env,''), 'CERTIFICADO_SENHA'),
            regime=COALESCE(NULLIF(regime,''), 'Simples Nacional - conferir com contador'),
            nf_serie=COALESCE(NULLIF(nf_serie,''), '1'),
            nf_numero_inicial=COALESCE(NULLIF(nf_numero_inicial,''), '1'),
            nfce_serie=COALESCE(NULLIF(nfce_serie,''), '1'),
            nfce_numero_inicial=COALESCE(NULLIF(nfce_numero_inicial,''), '1'),
            cfop_padrao=COALESCE(NULLIF(cfop_padrao,''), '5102'),
            cst_csosn_padrao=COALESCE(NULLIF(cst_csosn_padrao,''), '102'),
            observacao=COALESCE(NULLIF(observacao,''), 'Configuração provisória para homologação/testes. Conferir CFOP, CST/CSOSN, CSC/Token, séries e impostos com a contabilidade antes de produção.')
        WHERE id=1
    """)
    backup_db("fiscal-padrao-homologacao")
    flash("Preenchi o básico para homologação. Produção continua bloqueada até confirmar CSC/Token e regras fiscais reais.", "ok")
    return redirect(url_for("fiscal"))

@app.route("/fiscal/certificado", methods=["GET", "POST"])
@login_required
def enviar_certificado():
    cfg = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    env_path = os.environ.get("CERTIFICADO_PATH", "").strip()
    cfg_path = cfg["certificado_path"] if cfg and cfg["certificado_path"] else ""
    cert_path = env_path or cfg_path or "/app/certs/centralvet_a1.pfx"
    if not cert_path.lower().endswith((".pfx", ".p12")):
        cert_path = os.path.join(cert_path, "centralvet_a1.pfx")
    cert_path = os.path.abspath(cert_path)
    cert_dir = os.path.dirname(cert_path)
    status = {
        "path": cert_path,
        "dir": cert_dir,
        "exists": os.path.exists(cert_path),
        "has_password": bool(os.environ.get("CERTIFICADO_SENHA", "").strip()),
        "size": os.path.getsize(cert_path) if os.path.exists(cert_path) else 0,
        "updated": datetime.fromtimestamp(os.path.getmtime(cert_path)).strftime("%d/%m/%Y %H:%M") if os.path.exists(cert_path) else ""
    }
    if request.method == "POST":
        arq = request.files.get("certificado")
        if not arq or not arq.filename:
            flash("Selecione o arquivo do certificado A1 (.pfx ou .p12).", "erro")
            return redirect(url_for("enviar_certificado"))
        filename = arq.filename.lower().strip()
        if not filename.endswith((".pfx", ".p12")):
            flash("Arquivo inválido. Envie somente certificado A1 com final .pfx ou .p12.", "erro")
            return redirect(url_for("enviar_certificado"))
        try:
            os.makedirs(cert_dir, exist_ok=True)
            tmp_path = cert_path + ".tmp"
            arq.save(tmp_path)
            if os.path.getsize(tmp_path) <= 0:
                os.remove(tmp_path)
                flash("O arquivo enviado veio vazio. Tente enviar novamente.", "erro")
                return redirect(url_for("enviar_certificado"))
            os.replace(tmp_path, cert_path)
            try:
                os.chmod(cert_path, 0o600)
            except Exception:
                pass
            exec_sql("UPDATE fiscal_config SET certificado_path=?, certificado_nome=? WHERE id=1", (cert_path, arq.filename))
            backup_db("certificado-config")
            flash("Certificado A1 salvo com segurança no servidor.", "ok")
            return redirect(url_for("fiscal"))
        except Exception as e:
            flash(f"Não consegui salvar o certificado em {cert_path}. Detalhe: {e}", "erro")
            return redirect(url_for("enviar_certificado"))
    return render_template("certificado_upload.html", status=status, cfg=cfg)

@app.route("/xml/importar", methods=["GET", "POST"])
@login_required
def importar_xml():
    preview = None
    if request.method == "POST":
        arq = request.files.get("xml_file")
        aplicar_margem = request.form.get("aplicar_margem") == "1"
        margem = money_to_float(request.form.get("margem_lucro"))
        if not arq or not arq.filename:
            flash("Selecione o XML da nota de entrada.", "erro")
            return redirect(url_for("importar_xml"))
        xml_bytes = arq.read()
        xml_hash = hashlib.sha256(xml_bytes).hexdigest()
        try:
            dados = parse_nfe_xml(xml_bytes)
        except Exception as e:
            flash(f"Não consegui ler esse XML. Verifique se é o XML completo da NF-e. Detalhe: {e}", "erro")
            return redirect(url_for("importar_xml"))
        if not dados["itens"]:
            flash("XML lido, mas não encontrei produtos dentro da nota.", "erro")
            return redirect(url_for("importar_xml"))
        chave_unica = dados["chave"] or xml_hash
        ja = fetch_one("SELECT * FROM xml_imports WHERE chave=? OR xml_hash=?", (chave_unica, xml_hash))
        if ja:
            flash(f"Essa nota/XML já foi importada antes. Registro #{ja['id']}.", "erro")
            return redirect(url_for("importar_xml"))
        with get_db() as db:
            fornecedor_id = None
            if dados["fornecedor_nome"] or dados["fornecedor_cnpj"]:
                cnpj_digits = only_digits(dados["fornecedor_cnpj"])
                fornecedor = None
                if cnpj_digits:
                    fornecedor = db.execute("SELECT * FROM fornecedores WHERE cpf_cnpj LIKE ? LIMIT 1", (f"%{cnpj_digits[-8:]}%",)).fetchone()
                if not fornecedor and dados["fornecedor_nome"]:
                    fornecedor = db.execute("SELECT * FROM fornecedores WHERE nome LIKE ? LIMIT 1", (dados["fornecedor_nome"],)).fetchone()
                if fornecedor:
                    fornecedor_id = fornecedor["id"]
                else:
                    cur = db.execute("INSERT INTO fornecedores (nome, cpf_cnpj, observacao) VALUES (?, ?, ?)",
                                     (dados["fornecedor_nome"] or "Fornecedor do XML", dados["fornecedor_cnpj"], "Cadastrado automaticamente pela importação de XML."))
                    fornecedor_id = cur.lastrowid
            novos = atualizados = 0
            data_mov = dados["emissao"] or today_str()
            origem = f"XML NF-e {dados['numero'] or ''}".strip()
            for item in dados["itens"]:
                prod = None
                if item["codigo"]:
                    prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND codigo=? LIMIT 1", (item["codigo"],)).fetchone()
                if not prod and item["ean"]:
                    prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND ean=? LIMIT 1", (item["ean"],)).fetchone()
                if not prod:
                    prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND nome=? LIMIT 1", (item["nome"],)).fetchone()
                qtd = item["quantidade"]
                custo = item["valor_unitario"] or (item["valor_total"] / qtd if qtd else 0)
                if prod:
                    produto_id = prod["id"]
                    novo_preco_venda = prod["preco_venda"] or 0
                    if aplicar_margem and margem > 0:
                        novo_preco_venda = custo * (1 + margem / 100.0)
                    db.execute("""
                        UPDATE produtos SET nome=?, unidade=?, preco_custo=?, preco_venda=?, estoque=estoque+?,
                        ncm=?, cfop=?, cst_csosn=?, ean=?, cest=?, ultima_chave_xml=?, fornecedor_id=?
                        WHERE id=?
                    """, (item["nome"], item["unidade"], custo, novo_preco_venda, qtd, item["ncm"], item["cfop"],
                          item["cst_csosn"], item["ean"], item["cest"], chave_unica, fornecedor_id, produto_id))
                    atualizados += 1
                else:
                    preco_venda = custo * (1 + margem / 100.0) if aplicar_margem and margem > 0 else 0
                    cur = db.execute("""
                        INSERT INTO produtos (codigo,nome,categoria,unidade,preco_custo,preco_venda,estoque,estoque_minimo,ncm,cfop,cst_csosn,ean,cest,ultima_chave_xml,fornecedor_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (item["codigo"], item["nome"], "Importado XML", item["unidade"] or "UN", custo, preco_venda, qtd, 0,
                          item["ncm"], item["cfop"], item["cst_csosn"], item["ean"], item["cest"], chave_unica, fornecedor_id))
                    produto_id = cur.lastrowid
                    novos += 1
                db.execute("""
                    INSERT INTO estoque_mov (data, produto_id, tipo, quantidade, custo_unit, valor_total, origem, observacao)
                    VALUES (?, ?, 'Entrada', ?, ?, ?, ?, ?)
                """, (data_mov, produto_id, qtd, custo, item["valor_total"] or qtd*custo, origem, f"Importado pelo XML. Chave: {dados['chave'] or 'sem chave'}"))
            db.execute("""
                INSERT INTO xml_imports (chave, xml_hash, numero, serie, emissao, fornecedor_nome, fornecedor_cnpj, total, itens_qtd, observacao)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (chave_unica, xml_hash, dados["numero"], dados["serie"], dados["emissao"], dados["fornecedor_nome"], dados["fornecedor_cnpj"],
                  dados["total"], len(dados["itens"]), f"{novos} novo(s), {atualizados} atualizado(s)."))
            db.commit()
        backup_db("xml-importado")
        flash(f"XML importado: {novos} produto(s) novo(s), {atualizados} atualizado(s), estoque lançado automaticamente.", "ok")
        return redirect(url_for("produtos"))
    ultimos = fetch_all("SELECT * FROM xml_imports ORDER BY id DESC LIMIT 20")
    cfg = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    return render_template("importar_xml.html", ultimos=ultimos, cfg=cfg)


# ---------------- Devolução de mercadoria ----------------

def find_product_for_xml_item(db, item):
    prod = None
    if item.get("codigo"):
        prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND codigo=? LIMIT 1", (item.get("codigo"),)).fetchone()
    if not prod and item.get("ean"):
        prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND ean=? LIMIT 1", (item.get("ean"),)).fetchone()
    if not prod and item.get("nome"):
        prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND nome=? LIMIT 1", (item.get("nome"),)).fetchone()
    return prod

@app.route("/devolucoes")
@login_required
def devolucoes():
    q = (request.args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        rows = fetch_all("""
            SELECT * FROM devolucoes
            WHERE fornecedor_nome LIKE ? OR fornecedor_cnpj LIKE ? OR chave_origem LIKE ? OR numero_origem LIKE ?
            ORDER BY id DESC
        """, (like, like, like, like))
    else:
        rows = fetch_all("SELECT * FROM devolucoes ORDER BY id DESC LIMIT 120")
    return render_template("devolucoes.html", rows=rows, q=q)

@app.route("/devolucoes/nova", methods=["GET", "POST"])
@login_required
def nova_devolucao():
    preview = None
    encoded_xml = ""
    if request.method == "POST" and request.form.get("acao") == "preview":
        arq = request.files.get("xml_file")
        if not arq or not arq.filename:
            flash("Selecione o XML da nota de compra original.", "erro")
            return redirect(url_for("nova_devolucao"))
        xml_bytes = arq.read()
        try:
            dados = parse_nfe_xml(xml_bytes)
        except Exception as e:
            flash(f"Não consegui ler esse XML. Envie o XML completo da NF-e. Detalhe: {e}", "erro")
            return redirect(url_for("nova_devolucao"))
        if not dados["itens"]:
            flash("XML lido, mas não encontrei produtos na nota.", "erro")
            return redirect(url_for("nova_devolucao"))
        xml_hash = hashlib.sha256(xml_bytes).hexdigest()
        encoded_xml = base64.b64encode(xml_bytes).decode("ascii")
        with get_db() as db:
            itens = []
            for i, item in enumerate(dados["itens"], start=1):
                prod = find_product_for_xml_item(db, item)
                d = dict(item)
                d["idx"] = i
                d["produto_id"] = prod["id"] if prod else ""
                d["produto_estoque"] = prod["estoque"] if prod else 0
                d["produto_status"] = "Encontrado no cadastro" if prod else "Ainda não cadastrado"
                itens.append(d)
        preview = {"dados": dados, "itens": itens, "xml_hash": xml_hash}
    return render_template("devolucao_form.html", preview=preview, encoded_xml=encoded_xml, hoje=today_str())

@app.route("/devolucoes/salvar", methods=["POST"])
@login_required
def salvar_devolucao():
    xml_data = request.form.get("xml_data") or ""
    if not xml_data:
        flash("Importe primeiro o XML da nota original.", "erro")
        return redirect(url_for("nova_devolucao"))
    try:
        xml_bytes = base64.b64decode(xml_data.encode("ascii"))
        dados = parse_nfe_xml(xml_bytes)
    except Exception as e:
        flash(f"Não consegui recuperar os dados do XML. Tente importar novamente. Detalhe: {e}", "erro")
        return redirect(url_for("nova_devolucao"))
    xml_hash = hashlib.sha256(xml_bytes).hexdigest()
    motivo = request.form.get("motivo") or "Devolução por avaria"
    baixar_estoque = request.form.get("baixar_estoque") == "1"
    cfop_devolucao_padrao = (request.form.get("cfop_devolucao_padrao") or "").strip()
    obs = request.form.get("observacao") or ""
    selecionados = 0
    with get_db() as db:
        cur = db.execute("""
            INSERT INTO devolucoes
            (data, fornecedor_nome, fornecedor_cnpj, destinatario_nome, destinatario_cnpj, chave_origem, numero_origem,
             serie_origem, emissao_origem, total_original, motivo, status, baixou_estoque, xml_hash, observacao)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            request.form.get("data") or today_str(), dados.get("fornecedor_nome"), dados.get("fornecedor_cnpj"),
            dados.get("destinatario_nome"), dados.get("destinatario_cnpj"), dados.get("chave"), dados.get("numero"),
            dados.get("serie"), dados.get("emissao"), dados.get("total"), motivo, "Rascunho", 1 if baixar_estoque else 0,
            xml_hash, obs
        ))
        devolucao_id = cur.lastrowid
        for idx, item in enumerate(dados["itens"], start=1):
            usar = request.form.get(f"usar_{idx}") == "1"
            if not usar:
                continue
            qtd_dev = money_to_float(request.form.get(f"qtd_devolver_{idx}"))
            if qtd_dev <= 0:
                continue
            qtd_original = float(item.get("quantidade") or 0)
            if qtd_original > 0 and qtd_dev > qtd_original:
                qtd_dev = qtd_original
            cfop_dev = request.form.get(f"cfop_devolucao_{idx}") or cfop_devolucao_padrao
            prod = find_product_for_xml_item(db, item)
            produto_id = prod["id"] if prod else None
            valor_unit = float(item.get("valor_unitario") or 0)
            valor_total = qtd_dev * valor_unit
            db.execute("""
                INSERT INTO devolucao_itens
                (devolucao_id, produto_id, codigo, ean, nome, ncm, cest, cfop_original, cfop_devolucao, cst_csosn,
                 unidade, quantidade_original, quantidade_devolver, valor_unitario, valor_total)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (devolucao_id, produto_id, item.get("codigo"), item.get("ean"), item.get("nome"), item.get("ncm"),
                  item.get("cest"), item.get("cfop"), cfop_dev, item.get("cst_csosn"), item.get("unidade"), qtd_original,
                  qtd_dev, valor_unit, valor_total))
            selecionados += 1
            if baixar_estoque and produto_id:
                db.execute("UPDATE produtos SET estoque=estoque-? WHERE id=?", (qtd_dev, produto_id))
                db.execute("""
                    INSERT INTO estoque_mov (data, produto_id, tipo, quantidade, custo_unit, valor_total, origem, observacao)
                    VALUES (?, ?, 'Saída', ?, ?, ?, ?, ?)
                """, (request.form.get("data") or today_str(), produto_id, qtd_dev, valor_unit, valor_total,
                      f"DEVOLUCAO #{devolucao_id}", f"Saída por devolução. NF origem {dados.get('numero') or ''}. Chave {dados.get('chave') or ''}"))
        if selecionados <= 0:
            db.execute("DELETE FROM devolucoes WHERE id=?", (devolucao_id,))
            db.commit()
            flash("Selecione pelo menos um produto e informe a quantidade para devolver.", "erro")
            return redirect(url_for("nova_devolucao"))
        db.commit()
    backup_db("devolucao-criada")
    flash("Devolução criada em rascunho. Confira os itens e envie os dados para o contador validar CFOP/CST antes da emissão real.", "ok")
    return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id))

@app.route("/devolucoes/<int:devolucao_id>")
@login_required
def ver_devolucao(devolucao_id):
    dev = fetch_one("SELECT * FROM devolucoes WHERE id=?", (devolucao_id,))
    if not dev:
        flash("Devolução não encontrada.", "erro")
        return redirect(url_for("devolucoes"))
    itens = fetch_all("SELECT * FROM devolucao_itens WHERE devolucao_id=? ORDER BY id", (devolucao_id,))
    total = sum(float(i["valor_total"] or 0) for i in itens)
    return render_template("devolucao_detalhe.html", dev=dev, itens=itens, total=total)

@app.route("/devolucoes/<int:devolucao_id>/emitida", methods=["POST"])
@login_required
def marcar_devolucao_emitida(devolucao_id):
    exec_sql("UPDATE devolucoes SET status=?, nf_devolucao_numero=?, nf_devolucao_chave=? WHERE id=?",
             (request.form.get("status") or "Emitida", request.form.get("nf_devolucao_numero") or "", request.form.get("nf_devolucao_chave") or "", devolucao_id))
    backup_db("devolucao-status")
    flash("Status da devolução atualizado.", "ok")
    return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id))

@app.route("/devolucoes/<int:devolucao_id>/excluir", methods=["POST"])
@login_required
def excluir_devolucao(devolucao_id):
    dev = fetch_one("SELECT * FROM devolucoes WHERE id=?", (devolucao_id,))
    if not dev:
        flash("Devolução não encontrada.", "erro")
        return redirect(url_for("devolucoes"))
    if dev["status"] != "Rascunho":
        flash("Só é possível excluir devolução em rascunho.", "erro")
        return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id))
    with get_db() as db:
        if dev["baixou_estoque"]:
            itens = db.execute("SELECT * FROM devolucao_itens WHERE devolucao_id=?", (devolucao_id,)).fetchall()
            for i in itens:
                if i["produto_id"]:
                    db.execute("UPDATE produtos SET estoque=estoque+? WHERE id=?", (i["quantidade_devolver"], i["produto_id"]))
            db.execute("DELETE FROM estoque_mov WHERE origem=?", (f"DEVOLUCAO #{devolucao_id}",))
        db.execute("DELETE FROM devolucao_itens WHERE devolucao_id=?", (devolucao_id,))
        db.execute("DELETE FROM devolucoes WHERE id=?", (devolucao_id,))
        db.commit()
    backup_db("devolucao-excluida")
    flash("Devolução excluída.", "ok")
    return redirect(url_for("devolucoes"))


# ---------------- Relatórios / Backup ----------------

@app.route("/relatorios")
@login_required
def relatorios():
    inicio = request.args.get("inicio") or date.today().replace(day=1).isoformat()
    fim = request.args.get("fim") or today_str()
    vendas_periodo = fetch_one("SELECT COALESCE(SUM(total),0) total, COALESCE(SUM(lucro),0) lucro, COUNT(*) qtd FROM vendas WHERE data BETWEEN ? AND ?", (inicio, fim))
    entradas = fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Entrada' AND status='Pago' AND data BETWEEN ? AND ?", (inicio, fim))["v"]
    saidas = fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Saída' AND status='Pago' AND data BETWEEN ? AND ?", (inicio, fim))["v"]
    mais_vendidos = fetch_all("""
        SELECT produto_nome, SUM(quantidade) qtd, SUM(total) total
        FROM venda_itens vi JOIN vendas v ON v.id=vi.venda_id
        WHERE v.data BETWEEN ? AND ?
        GROUP BY produto_nome ORDER BY qtd DESC LIMIT 20
    """, (inicio, fim))
    vendas = fetch_all("SELECT * FROM vendas WHERE data BETWEEN ? AND ? ORDER BY data DESC, id DESC", (inicio, fim))
    return render_template("relatorios.html", **locals())

@app.route("/backup")
@login_required
def backup():
    path = backup_db("manual")
    if not path:
        flash("Banco ainda não encontrado.", "erro")
        return redirect(url_for("index"))
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))

@app.route("/backups")
@login_required
def backups():
    files = []
    if os.path.exists(BACKUP_DIR):
        for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if fn.endswith(".db"):
                p = os.path.join(BACKUP_DIR, fn)
                files.append({"name": fn, "size": os.path.getsize(p), "mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%d/%m/%Y %H:%M")})
    return render_template("backups.html", files=files)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
