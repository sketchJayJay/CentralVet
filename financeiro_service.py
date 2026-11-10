"""Contas e baixas. Operações recebem a mesma conexão da transação da venda."""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import secrets

FORMAS = ("Dinheiro", "Pix", "Cartão débito", "Cartão crédito", "Transferência", "Boleto", "Outro")


def centavos(valor):
    """Lê moeda sem aceitar NaN, infinitos, negativos ou precisão oculta."""
    texto = str(valor if valor is not None else "").strip().replace("R$", "").replace(" ", "")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        numero = Decimal(texto)
        if not numero.is_finite() or numero < 0 or numero > Decimal("999999999.99"):
            raise ValueError()
        arredondado = numero.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        if numero != arredondado:
            raise ValueError()
        return int(arredondado * 100)
    except (InvalidOperation, ValueError):
        raise ValueError("Informe um valor válido, com até duas casas decimais.")


def data_valida(texto, obrigatoria=True):
    texto = str(texto or "").strip()
    if not texto and not obrigatoria:
        return ""
    try:
        return date.fromisoformat(texto).isoformat()
    except ValueError:
        raise ValueError("Informe uma data válida.")


def novo_token():
    return secrets.token_urlsafe(24)


def centavos_db(valor):
    return int(Decimal(str(valor or 0)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)*100)


def migrar(db, add_column):
    for coluna, ddl in (
        ("cliente_id", "INTEGER"), ("fornecedor_id", "INTEGER"),
        ("documento", "TEXT"), ("codigo_barras", "TEXT"), ("criacao_token", "TEXT")
    ):
        add_column(db, "financeiro", coluna, ddl)
    add_column(db, "vendas", "vencimento", "TEXT")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS financeiro_baixas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            financeiro_id INTEGER NOT NULL,
            valor_centavos INTEGER NOT NULL CHECK(valor_centavos > 0),
            data TEXT NOT NULL,
            forma TEXT,
            observacao TEXT,
            origem TEXT NOT NULL DEFAULT 'Manual',
            token TEXT NOT NULL UNIQUE,
            estornado_em TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_baixas_conta ON financeiro_baixas(financeiro_id);
        CREATE INDEX IF NOT EXISTS idx_financeiro_cliente ON financeiro(cliente_id);
        CREATE INDEX IF NOT EXISTS idx_financeiro_vencimento ON financeiro(vencimento);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_conta_criacao ON financeiro(criacao_token) WHERE criacao_token IS NOT NULL;
        CREATE TABLE IF NOT EXISTS financeiro_lotes (
            token TEXT PRIMARY KEY, cliente_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Vínculo por ID da venda; homônimos nunca são combinados pelo nome.
    db.execute("""UPDATE financeiro SET cliente_id=(
        SELECT v.cliente_id FROM vendas v WHERE v.id=financeiro.referencia_id
    ) WHERE referencia_tipo='Venda' AND cliente_id IS NULL
        AND EXISTS (SELECT 1 FROM vendas v JOIN clientes c ON c.id=v.cliente_id
                    WHERE v.id=financeiro.referencia_id)""")
    # Preserve a data em que a versão antiga marcou a conta como paga.
    db.execute("""INSERT OR IGNORE INTO financeiro_baixas
        (financeiro_id,valor_centavos,data,forma,origem,token,observacao)
        SELECT f.id, CAST(ROUND(f.valor*100) AS INTEGER), COALESCE(NULLIF(f.data,''),date('now')),
               f.forma, 'Legado', 'legado:' || f.id, 'Pagamento registrado na versão anterior'
        FROM financeiro f WHERE f.status='Pago' AND ROUND(f.valor*100)>0
        AND NOT EXISTS (SELECT 1 FROM financeiro_baixas b WHERE b.financeiro_id=f.id)""")


BASE_QUERY = """
    SELECT f.*, c.nome cadastro_nome, c.cpf_cnpj cadastro_documento, c.telefone cadastro_telefone,
           fo.nome fornecedor_nome, fo.cpf_cnpj fornecedor_documento,
           COALESCE(b.baixado_centavos,0) baixado_centavos,
           COALESCE(b.baixas_ativas,0) baixas_ativas,
           MAX(0,CAST(ROUND(f.valor*100) AS INTEGER)-COALESCE(b.baixado_centavos,0)) saldo_centavos
    FROM financeiro f
    LEFT JOIN clientes c ON c.id=f.cliente_id
    LEFT JOIN fornecedores fo ON fo.id=f.fornecedor_id
    LEFT JOIN (
        SELECT financeiro_id,SUM(valor_centavos) baixado_centavos,COUNT(*) baixas_ativas
        FROM financeiro_baixas WHERE estornado_em IS NULL GROUP BY financeiro_id
    ) b ON b.financeiro_id=f.id
"""


def apresentar(row, hoje):
    r = dict(row)
    r["baixado"] = r["baixado_centavos"]/100
    r["saldo"] = r["saldo_centavos"]/100
    r["nome_exibicao"] = r["cadastro_nome"] or r["fornecedor_nome"] or r["pessoa"] or "Sem identificação"
    r["documento_exibicao"] = r["cadastro_documento"] or r["fornecedor_documento"] or ""
    r["status"] = "Pago" if r["saldo_centavos"] == 0 else ("Parcial" if r["baixado_centavos"] else "Pendente")
    r["vencida"] = bool(r["saldo_centavos"] and r["vencimento"] and r["vencimento"] < hoje)
    r["vence_hoje"] = bool(r["saldo_centavos"] and r["vencimento"] == hoje)
    return r


def conta(db, item_id, hoje):
    row = db.execute(BASE_QUERY+" WHERE f.id=?", (item_id,)).fetchone()
    if not row:
        raise ValueError("Conta não encontrada.")
    return apresentar(row, hoje)


def contas(db, hoje, tipo=None, cliente_id=None):
    where, params = [], []
    if tipo:
        where.append("f.tipo=?")
        params.append(tipo)
    if cliente_id is not None:
        where.append("f.cliente_id=?")
        params.append(cliente_id)
    sql = BASE_QUERY + (" WHERE "+" AND ".join(where) if where else "")
    sql += " ORDER BY COALESCE(NULLIF(f.vencimento,''),'9999-12-31'),f.id DESC"
    return [apresentar(r, hoje) for r in db.execute(sql, params).fetchall()]


def atualizar_status(db, item_id):
    r = db.execute("""SELECT f.valor, COALESCE(SUM(CASE WHEN b.estornado_em IS NULL
        THEN b.valor_centavos ELSE 0 END),0) pago FROM financeiro f
        LEFT JOIN financeiro_baixas b ON b.financeiro_id=f.id WHERE f.id=? GROUP BY f.id""",
        (item_id,)).fetchone()
    if not r:
        raise ValueError("Conta não encontrada.")
    total = centavos_db(r["valor"])
    status = "Pago" if r["pago"] >= total else ("Parcial" if r["pago"] else "Pendente")
    db.execute("UPDATE financeiro SET status=? WHERE id=?", (status, item_id))
    db.execute("""UPDATE vendas SET status=? WHERE id=(
        SELECT referencia_id FROM financeiro WHERE id=? AND referencia_tipo='Venda'
    )""", (status, item_id))
    return status


def registrar_baixa(db, item_id, valor, data, forma, observacao, token, hoje, origem="Manual"):
    if not token or len(token) > 150:
        raise ValueError("Atualize a página antes de registrar o pagamento.")
    anterior = db.execute("SELECT financeiro_id FROM financeiro_baixas WHERE token=?", (token,)).fetchone()
    if anterior:
        if anterior["financeiro_id"] != item_id:
            raise ValueError("Solicitação já utilizada em outra conta.")
        return False
    r = conta(db, item_id, hoje)
    qtd = centavos(valor)
    if qtd <= 0:
        raise ValueError("O valor do pagamento deve ser maior que zero.")
    if qtd > r["saldo_centavos"]:
        raise ValueError("O pagamento não pode ser maior que o saldo em aberto.")
    data = data_valida(data)
    if data > hoje:
        raise ValueError("A data do pagamento não pode estar no futuro.")
    if forma not in FORMAS:
        raise ValueError("Escolha a forma de pagamento.")
    db.execute("""INSERT INTO financeiro_baixas
        (financeiro_id,valor_centavos,data,forma,observacao,origem,token) VALUES (?,?,?,?,?,?,?)""",
        (item_id,qtd,data,forma,observacao,origem,token))
    atualizar_status(db, item_id)
    return True


def estornar(db, item_id, baixa_id, hoje):
    r = db.execute("SELECT * FROM financeiro_baixas WHERE id=? AND financeiro_id=?",
                   (baixa_id,item_id)).fetchone()
    if not r:
        raise ValueError("Pagamento não encontrado.")
    if r["estornado_em"]:
        return False
    db.execute("UPDATE financeiro_baixas SET estornado_em=CURRENT_TIMESTAMP WHERE id=?", (baixa_id,))
    atualizar_status(db, item_id)
    return True


def resumo(db, hoje):
    todas = contas(db, hoje)
    recebidos = db.execute("""SELECT f.tipo,COALESCE(SUM(b.valor_centavos),0) total
        FROM financeiro_baixas b JOIN financeiro f ON f.id=b.financeiro_id
        WHERE b.estornado_em IS NULL GROUP BY f.tipo""").fetchall()
    pagos = {r["tipo"]:r["total"]/100 for r in recebidos}
    receber = sum(r["saldo_centavos"] for r in todas if r["tipo"] == "Entrada")/100
    pagar = sum(r["saldo_centavos"] for r in todas if r["tipo"] == "Saída")/100
    return dict(entradas=pagos.get("Entrada",0), saidas=pagos.get("Saída",0), receber=receber, pagar=pagar,
                receber_vencido=sum(r["saldo_centavos"] for r in todas if r["tipo"]=="Entrada" and r["vencida"])/100,
                pagar_vencido=sum(r["saldo_centavos"] for r in todas if r["tipo"]=="Saída" and r["vencida"])/100)


def caixa_periodo(db, inicio, fim, tipo):
    return db.execute("""SELECT COALESCE(SUM(b.valor_centavos),0) total
        FROM financeiro_baixas b JOIN financeiro f ON f.id=b.financeiro_id
        WHERE b.estornado_em IS NULL AND f.tipo=? AND b.data BETWEEN ? AND ?""",
        (tipo,inicio,fim)).fetchone()["total"]/100


def sincronizar_venda(db, venda_id, hoje):
    v = db.execute("SELECT * FROM vendas WHERE id=?", (venda_id,)).fetchone()
    existentes = db.execute("SELECT id FROM financeiro WHERE referencia_tipo='Venda' AND referencia_id=? ORDER BY id", (venda_id,)).fetchall()
    if len(existentes) > 1:
        raise ValueError("Esta venda possui mais de um lançamento financeiro. Confira os lançamentos antes de editar.")
    tem_baixas = False
    if existentes:
        item_id = existentes[0]["id"]
        r = conta(db,item_id,hoje)
        if centavos_db(v["total"]) < r["baixado_centavos"]:
            raise ValueError("O novo total é menor que o valor já recebido. Estorne o recebimento na conta antes de corrigir o recibo.")
        if r["baixado_centavos"] and (v["cliente_id"] or None) != (r["cliente_id"] or None):
            raise ValueError("Esta venda já tem recebimentos. Estorne-os antes de trocar o cliente.")
        tem_baixas = db.execute("SELECT 1 FROM financeiro_baixas WHERE financeiro_id=? LIMIT 1", (item_id,)).fetchone() is not None
        # Não apaga baixas nem troca sua conta de origem ao editar um recibo.
        db.execute("""UPDATE financeiro SET data=?,descricao=?,valor=?,forma=?,pessoa=?,
            cliente_id=?,vencimento=? WHERE id=?""",
            (v["data"],f"Venda #{venda_id}",v["total"],v["forma_pagamento"],v["cliente_nome"],v["cliente_id"] or None,v["vencimento"],item_id))
    else:
        item_id = db.execute("""INSERT INTO financeiro
            (data,tipo,descricao,valor,forma,status,pessoa,referencia_tipo,referencia_id,cliente_id,vencimento)
            VALUES (?,'Entrada',?,?,?,'Pendente',?,'Venda',?,?,?)""",
            (v["data"],f"Venda #{venda_id}",v["total"],v["forma_pagamento"],v["cliente_nome"],venda_id,v["cliente_id"] or None,v["vencimento"])).lastrowid
    if v["status"] == "Pago" and not tem_baixas and centavos_db(v["total"]) > 0:
        forma = v["forma_pagamento"] if v["forma_pagamento"] in FORMAS else "Outro"
        registrar_baixa(db,item_id,str(centavos_db(v["total"])/100),v["data"],forma,"Pagamento da venda",
                        novo_token(),hoje,origem="Inicial")
    atualizar_status(db,item_id)
    return item_id
