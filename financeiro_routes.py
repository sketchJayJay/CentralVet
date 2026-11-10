"""Telas de contas a pagar, receber e ficha de crediário."""
import hmac
from flask import request, render_template, redirect, url_for, flash, session, abort
import financeiro_service as fs


def registrar_rotas(app, get_db, backup_db, login_required, today_str):
    @app.context_processor
    def formularios_financeiros():
        if "financeiro_csrf" not in session:
            session["financeiro_csrf"] = fs.novo_token()
        return dict(financeiro_csrf=session["financeiro_csrf"], financeiro_token=fs.novo_token,
                    formas_financeiras=fs.FORMAS)

    def proteger():
        esperado = session.get("financeiro_csrf", "")
        recebido = request.form.get("csrf_token", "")
        if not esperado or not hmac.compare_digest(esperado, recebido):
            abort(400, "A sessão do formulário expirou. Atualize a página e tente novamente.")

    def pagina_contas(tipo):
        hoje = today_str()
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "abertas")
        inicio = request.args.get("inicio", "")
        fim = request.args.get("fim", "")
        visao = request.args.get("visao", "clientes" if tipo=="Entrada" else "contas")
        with get_db() as db:
            todas = fs.contas(db, hoje, tipo)
        cards = dict(total=sum(r["saldo_centavos"] for r in todas)/100,
                     vencidas=sum(r["saldo_centavos"] for r in todas if r["vencida"])/100,
                     hoje=sum(r["saldo_centavos"] for r in todas if r["vence_hoje"])/100,
                     quitado=sum(r["baixado_centavos"] for r in todas)/100)
        rows = []
        for r in todas:
            if status == "abertas" and not r["saldo_centavos"]: continue
            if status == "vencidas" and not r["vencida"]: continue
            if status == "hoje" and not r["vence_hoje"]: continue
            if status == "pagas" and r["saldo_centavos"]: continue
            if inicio and (not r["vencimento"] or r["vencimento"] < inicio): continue
            if fim and (not r["vencimento"] or r["vencimento"] > fim): continue
            texto = " ".join(str(r.get(k) or "") for k in ("nome_exibicao","documento_exibicao","descricao","documento","cadastro_telefone","id"))
            numeros_q = "".join(c for c in q if c.isdigit())
            numeros_doc = "".join(c for c in r["documento_exibicao"] if c.isdigit())
            busca_documento = numeros_q and not any(c.isalpha() for c in q) and numeros_q in numeros_doc
            if q and q.casefold() not in texto.casefold() and not busca_documento:
                continue
            rows.append(r)
        grupos = {}
        sem_cadastro = []
        for r in rows:
            if not r["cliente_id"]:
                sem_cadastro.append(r)
                continue
            g = grupos.setdefault(r["cliente_id"], dict(id=r["cliente_id"], nome=r["nome_exibicao"],
                cpf_cnpj=r["documento_exibicao"], telefone=r["cadastro_telefone"], saldo_centavos=0,
                vencido_centavos=0, quantidade=0, vencimento=""))
            g["saldo_centavos"] += r["saldo_centavos"]
            g["vencido_centavos"] += r["saldo_centavos"] if r["vencida"] else 0
            g["quantidade"] += 1
            if r["saldo_centavos"] and r["vencimento"] and (not g["vencimento"] or r["vencimento"] < g["vencimento"]):
                g["vencimento"] = r["vencimento"]
        grupos = sorted(grupos.values(), key=lambda g:(-g["vencido_centavos"],-g["saldo_centavos"],g["nome"].casefold()))
        return render_template("contas.html", tipo=tipo, rows=rows, cards=cards, q=q, status=status,
            inicio=inicio, fim=fim, visao=visao, grupos=grupos, sem_cadastro=sem_cadastro, hoje=hoje)

    @app.route("/financeiro", methods=["GET", "POST"])
    @login_required
    def financeiro():
        if request.method == "POST":
            return nova_conta()
        hoje = today_str()
        with get_db() as db:
            cards = fs.resumo(db, hoje)
            proximas = [r for r in fs.contas(db,hoje) if r["saldo_centavos"]][:8]
            baixas = db.execute("""SELECT b.*,f.tipo,f.descricao,f.id conta_id,COALESCE(c.nome,f.pessoa) pessoa
                FROM financeiro_baixas b JOIN financeiro f ON f.id=b.financeiro_id
                LEFT JOIN clientes c ON c.id=f.cliente_id
                WHERE b.estornado_em IS NULL ORDER BY b.data DESC,b.id DESC LIMIT 10""").fetchall()
        return render_template("financeiro.html",cards=cards,proximas=proximas,baixas=baixas,hoje=hoje)

    @app.route("/financeiro/pagar")
    @login_required
    def contas_pagar():
        return pagina_contas("Saída")

    @app.route("/financeiro/receber")
    @login_required
    def contas_receber():
        return pagina_contas("Entrada")

    def render_form(row, erro=False):
        with get_db() as db:
            clientes = db.execute("SELECT * FROM clientes ORDER BY nome").fetchall()
            fornecedores = db.execute("SELECT * FROM fornecedores ORDER BY nome").fetchall()
        return render_template("conta_form.html",row=row,clientes=clientes,fornecedores=fornecedores,
                               erro=erro,hoje=today_str())

    def ler_conta(db, antiga=None):
        venda = antiga and antiga["referencia_tipo"]=="Venda"
        tipo = antiga["tipo"] if antiga else request.form.get("tipo", "Saída")
        if tipo not in ("Entrada","Saída"): raise ValueError("Escolha contas a pagar ou a receber.")
        descricao = (antiga["descricao"] if venda else request.form.get("descricao","")).strip()
        if not descricao: raise ValueError("Informe a descrição da conta.")
        total = fs.centavos(antiga["valor"] if venda else request.form.get("valor"))
        if total <= 0: raise ValueError("O valor da conta deve ser maior que zero.")
        if antiga and total < antiga["baixado_centavos"]:
            raise ValueError("O valor não pode ser menor que o total já pago. Estorne a baixa antes de corrigir.")
        data = antiga["data"] if venda else fs.data_valida(request.form.get("data") or today_str())
        vencimento = fs.data_valida(request.form.get("vencimento"))
        pessoa = request.form.get("pessoa","").strip()
        documento = request.form.get("documento","").strip()
        cliente_id = fornecedor_id = None
        if venda:
            cliente_id = antiga["cliente_id"]
            pessoa = antiga["pessoa"]
        elif tipo == "Entrada":
            escolha = request.form.get("cliente_id", "")
            if escolha and escolha != "novo":
                c = db.execute("SELECT * FROM clientes WHERE id=?", (escolha,)).fetchone()
                if not c: raise ValueError("Cliente não encontrado.")
                cliente_id,pessoa = c["id"],c["nome"]
            else:
                pessoa = request.form.get("novo_nome","").strip()
                if not pessoa: raise ValueError("Selecione um cliente ou informe o nome no novo cadastro.")
                cliente_id = db.execute("INSERT INTO clientes (nome,cpf_cnpj,telefone) VALUES (?,?,?)",
                    (pessoa,request.form.get("novo_documento","").strip(),request.form.get("novo_telefone","").strip())).lastrowid
        else:
            escolha = request.form.get("fornecedor_id", "")
            if escolha:
                c = db.execute("SELECT * FROM fornecedores WHERE id=?", (escolha,)).fetchone()
                if not c: raise ValueError("Fornecedor não encontrado.")
                fornecedor_id,pessoa = c["id"],c["nome"]
            if not pessoa: raise ValueError("Informe o fornecedor ou a pessoa que vai receber.")
        if antiga and antiga["baixado_centavos"] and (cliente_id or None)!=(antiga["cliente_id"] or None):
            raise ValueError("Esta conta tem recebimentos. Estorne-os antes de trocar o cliente.")
        return dict(tipo=tipo,descricao=descricao,valor=total/100,data=data,vencimento=vencimento,
                    pessoa=pessoa,documento=documento,cliente_id=cliente_id,fornecedor_id=fornecedor_id,
                    forma=request.form.get("forma","Boleto" if tipo=="Saída" else "Outro"),
                    codigo_barras=request.form.get("codigo_barras","").strip(),
                    observacao=request.form.get("observacao","").strip())

    @app.route("/financeiro/contas/nova", methods=["GET","POST"])
    @login_required
    def nova_conta():
        tipo = "Entrada" if request.args.get("tipo")=="Entrada" else "Saída"
        row = dict(id=None,tipo=tipo,data=today_str(),status="Pendente",forma="Boleto" if tipo=="Saída" else "Outro",
                   criacao_token=fs.novo_token())
        if request.method == "POST":
            proteger()
            row.update(request.form.to_dict())
            try:
                with get_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    token = request.form.get("criacao_token","")
                    if not token or len(token)>150: raise ValueError("Atualize o formulário e tente novamente.")
                    anterior = db.execute("SELECT id FROM financeiro WHERE criacao_token=?", (token,)).fetchone()
                    if anterior: return redirect(url_for("ver_conta",item_id=anterior["id"]))
                    valores = ler_conta(db)
                    if request.form.get("status") not in ("Pago","Pendente"):
                        raise ValueError("Escolha o status da conta.")
                    item_id = db.execute("""INSERT INTO financeiro
                        (tipo,descricao,valor,data,vencimento,pessoa,documento,cliente_id,fornecedor_id,forma,codigo_barras,observacao,status,criacao_token)
                        VALUES (:tipo,:descricao,:valor,:data,:vencimento,:pessoa,:documento,:cliente_id,:fornecedor_id,:forma,:codigo_barras,:observacao,'Pendente',:criacao_token)""",
                        dict(valores,criacao_token=token)).lastrowid
                    if request.form.get("status") == "Pago":
                        fs.registrar_baixa(db,item_id,str(valores["valor"]),request.form.get("data_pagamento") or today_str(),
                                          valores["forma"],"Baixa no cadastro",fs.novo_token(),today_str())
                backup_db("conta-criada")
                flash("Conta salva.", "ok")
                return redirect(url_for("ver_conta",item_id=item_id))
            except ValueError as exc:
                flash(str(exc),"erro")
        return render_form(row)

    @app.route("/financeiro/contas/<int:item_id>")
    @login_required
    def ver_conta(item_id):
        with get_db() as db:
            try: row = fs.conta(db,item_id,today_str())
            except ValueError:
                flash("Conta não encontrada.","erro")
                return redirect(url_for("financeiro"))
            baixas = db.execute("SELECT * FROM financeiro_baixas WHERE financeiro_id=? ORDER BY data DESC,id DESC",(item_id,)).fetchall()
        return render_template("conta_detalhe.html",row=row,baixas=baixas,hoje=today_str())

    @app.route("/financeiro/contas/<int:item_id>/editar", methods=["GET","POST"])
    @login_required
    def editar_conta(item_id):
        with get_db() as db:
            try: row = fs.conta(db,item_id,today_str())
            except ValueError:
                flash("Conta não encontrada.","erro")
                return redirect(url_for("financeiro"))
        if request.method == "POST":
            proteger()
            try:
                with get_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    antiga=fs.conta(db,item_id,today_str())
                    valores=ler_conta(db,antiga)
                    db.execute("""UPDATE financeiro SET descricao=:descricao,valor=:valor,data=:data,
                        vencimento=:vencimento,pessoa=:pessoa,documento=:documento,cliente_id=:cliente_id,
                        fornecedor_id=:fornecedor_id,forma=:forma,codigo_barras=:codigo_barras,observacao=:observacao
                        WHERE id=:id""",dict(valores,id=item_id))
                    if antiga["referencia_tipo"]=="Venda":
                        db.execute("UPDATE vendas SET vencimento=? WHERE id=?",(valores["vencimento"],antiga["referencia_id"]))
                    fs.atualizar_status(db,item_id)
                backup_db("conta-editada")
                flash("Conta atualizada. Histórico de pagamentos preservado.","ok")
                return redirect(url_for("ver_conta",item_id=item_id))
            except ValueError as exc:
                flash(str(exc),"erro")
                row.update(request.form.to_dict())
        return render_form(row)

    @app.route("/financeiro/<int:item_id>/pagar", methods=["POST"])
    @login_required
    def financeiro_pagar(item_id):
        proteger()
        try:
            with get_db() as db:
                db.execute("BEGIN IMMEDIATE")
                registrado=fs.registrar_baixa(db,item_id,request.form.get("valor"),request.form.get("data"),request.form.get("forma"),
                    request.form.get("observacao","").strip(),request.form.get("token"),today_str())
            if registrado: backup_db("conta-baixada")
            flash("Pagamento registrado." if registrado else "Este pagamento já foi registrado.","ok")
        except ValueError as exc:
            flash(str(exc),"erro")
        return redirect(url_for("ver_conta",item_id=item_id))

    @app.route("/financeiro/contas/<int:item_id>/baixas/<int:baixa_id>/estornar", methods=["POST"])
    @login_required
    def estornar_baixa(item_id,baixa_id):
        proteger()
        try:
            with get_db() as db:
                db.execute("BEGIN IMMEDIATE")
                alterado=fs.estornar(db,item_id,baixa_id,today_str())
            if alterado: backup_db("baixa-estornada")
            flash("Baixa estornada e saldo atualizado.","ok")
        except ValueError as exc:
            flash(str(exc),"erro")
        return redirect(url_for("ver_conta",item_id=item_id))

    @app.route("/financeiro/<int:item_id>/excluir", methods=["POST"])
    @login_required
    def financeiro_excluir(item_id):
        proteger()
        try:
            with get_db() as db:
                db.execute("BEGIN IMMEDIATE")
                row=fs.conta(db,item_id,today_str())
                if row["referencia_tipo"]=="Venda":
                    raise ValueError("Este lançamento pertence a uma venda. Use Excluir recibo na venda para ajustar também o estoque.")
                if db.execute("SELECT 1 FROM financeiro_baixas WHERE financeiro_id=? LIMIT 1",(item_id,)).fetchone():
                    raise ValueError("Esta conta possui histórico de pagamentos. O histórico deve ser preservado; use Estornar para corrigir uma baixa.")
                db.execute("DELETE FROM financeiro WHERE id=?",(item_id,))
            backup_db("conta-excluida")
            flash("Conta excluída.","ok")
            return redirect(url_for("contas_receber" if row["tipo"]=="Entrada" else "contas_pagar"))
        except ValueError as exc:
            flash(str(exc),"erro")
            return redirect(url_for("ver_conta",item_id=item_id))

    @app.route("/financeiro/crediario/<int:cliente_id>")
    @login_required
    def crediario_cliente(cliente_id):
        hoje=today_str()
        with get_db() as db:
            cliente=db.execute("SELECT * FROM clientes WHERE id=?",(cliente_id,)).fetchone()
            if not cliente:
                flash("Cliente não encontrado.","erro")
                return redirect(url_for("contas_receber"))
            rows=fs.contas(db,hoje,"Entrada",cliente_id)
            baixas=db.execute("""SELECT b.*,f.descricao FROM financeiro_baixas b
                JOIN financeiro f ON f.id=b.financeiro_id WHERE f.cliente_id=? AND f.tipo='Entrada'
                ORDER BY b.data DESC,b.id DESC""",(cliente_id,)).fetchall()
        saldo=sum(r["saldo_centavos"] for r in rows)/100
        recebido=sum(r["baixado_centavos"] for r in rows)/100
        vencido=sum(r["saldo_centavos"] for r in rows if r["vencida"])/100
        return render_template("crediario_cliente.html",cliente=cliente,rows=rows,baixas=baixas,
                               saldo=saldo,recebido=recebido,vencido=vencido,hoje=hoje)

    @app.route("/financeiro/crediario/<int:cliente_id>/receber", methods=["POST"])
    @login_required
    def receber_cliente(cliente_id):
        proteger()
        try:
            with get_db() as db:
                db.execute("BEGIN IMMEDIATE")
                token=request.form.get("token","")
                if not token or len(token)>100: raise ValueError("Atualize a página e tente novamente.")
                anterior=db.execute("SELECT cliente_id FROM financeiro_lotes WHERE token=?",(token,)).fetchone()
                if anterior:
                    if anterior["cliente_id"] != cliente_id: raise ValueError("Solicitação já utilizada.")
                    flash("Este recebimento já foi registrado.","ok")
                    return redirect(url_for("crediario_cliente",cliente_id=cliente_id))
                abertas=[r for r in fs.contas(db,today_str(),"Entrada",cliente_id) if r["saldo_centavos"]]
                valor=fs.centavos(request.form.get("valor"))
                if valor<=0 or valor>sum(r["saldo_centavos"] for r in abertas):
                    raise ValueError("Informe um valor maior que zero e até o saldo devedor do cliente.")
                db.execute("INSERT INTO financeiro_lotes (token,cliente_id) VALUES (?,?)",(token,cliente_id))
                restante=valor
                for r in abertas:
                    if not restante: break
                    parcela=min(restante,r["saldo_centavos"])
                    fs.registrar_baixa(db,r["id"],str(parcela/100),request.form.get("data"),request.form.get("forma"),
                        request.form.get("observacao","").strip() or "Recebimento na ficha do cliente",
                        token+":"+str(r["id"]),today_str())
                    restante-=parcela
            backup_db("crediario-recebido")
            flash("Recebimento registrado, abatendo primeiro as contas com vencimento mais antigo.","ok")
        except ValueError as exc:
            flash(str(exc),"erro")
        return redirect(url_for("crediario_cliente",cliente_id=cliente_id))
