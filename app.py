
import os
import sqlite3
import shutil
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
            observacao TEXT
        );
        """)
        # default fiscal config
        db.execute("""
            INSERT OR IGNORE INTO fiscal_config
            (id, razao_social, nome_fantasia, cnpj, ambiente)
            VALUES (1, 'CENTRALVET AGROPECUÁRIA', 'CENTRALVET agropecuária', '68.690.225/0001-50', 'Homologação')
        """)
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
                (codigo,nome,categoria,unidade,preco_custo,preco_venda,estoque,estoque_minimo,ncm,cfop,cst_csosn,aliquota)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
            estoque=?, estoque_minimo=?, ncm=?, cfop=?, cst_csosn=?, aliquota=? WHERE id=?
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


# ---------------- Fiscal ----------------

@app.route("/fiscal", methods=["GET","POST"])
@login_required
def fiscal():
    if request.method == "POST":
        exec_sql("""
            UPDATE fiscal_config SET razao_social=?, nome_fantasia=?, cnpj=?, inscricao_estadual=?,
            endereco=?, municipio=?, uf=?, cep=?, telefone=?, email=?, regime=?, ambiente=?, certificado_nome=?, observacao=?
            WHERE id=1
        """, (
            request.form.get("razao_social",""), request.form.get("nome_fantasia",""), request.form.get("cnpj",""),
            request.form.get("inscricao_estadual",""), request.form.get("endereco",""), request.form.get("municipio",""),
            request.form.get("uf",""), request.form.get("cep",""), request.form.get("telefone",""), request.form.get("email",""),
            request.form.get("regime",""), request.form.get("ambiente","Homologação"), request.form.get("certificado_nome",""),
            request.form.get("observacao","")
        ))
        backup_db("fiscal-config")
        flash("Configuração fiscal salva.", "ok")
        return redirect(url_for("fiscal"))
    cfg = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    notas = fetch_all("SELECT * FROM vendas WHERE nf_status!='Não emitida' ORDER BY id DESC LIMIT 80")
    return render_template("fiscal.html", cfg=cfg, notas=notas)


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
