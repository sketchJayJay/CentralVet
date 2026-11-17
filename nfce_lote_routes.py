from datetime import date, datetime
from hashlib import sha256
import secrets
from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for
import nfce_emissao as ne


def registrar_rotas(app,get_db,backup_db,emitir,consultar,validar_config):
    # A própria aplicação já autentica as demais rotas; este grupo usa o mesmo login.
    from functools import wraps
    def protegido(fn):
        @wraps(fn)
        def wrapper(*args,**kwargs):
            if not session.get('logged'):
                return redirect(url_for('login'))
            return fn(*args,**kwargs)
        return wrapper
    def csrf():
        if 'nfce_csrf' not in session:
            session['nfce_csrf']=secrets.token_urlsafe(32)
        return session['nfce_csrf']
    app.jinja_env.globals['nfce_csrf']=csrf
    def validar_csrf():
        enviado=request.headers.get('X-CSRF-Token') or request.form.get('csrf_token') or ''
        if not secrets.compare_digest(str(session.get('nfce_csrf') or ''),str(enviado)) or not enviado:
            abort(403)
    def usuario():
        return session.get('user') or 'Administrador'
    def resumo(lote_id,item=None,db=None):
        def ler(conn):
            lote=conn.execute('SELECT * FROM nfce_lotes WHERE id=?',(lote_id,)).fetchone()
            if not lote:abort(404)
            contagens={r['estado']:r['n'] for r in conn.execute('SELECT estado,COUNT(*) n FROM nfce_lote_itens WHERE lote_id=? GROUP BY estado',(lote_id,))}
            return dict(id=lote_id,estado=lote['estado'],contagens=contagens,total=sum(contagens.values()),item=item,hora=ne.agora())
        if db is not None:return ler(db)
        with get_db() as conn:return ler(conn)
    def item_json(db,item_id):
        row=db.execute('''SELECT li.*,v.nf_numero,v.nf_serie FROM nfce_lote_itens li
            LEFT JOIN vendas v ON v.id=li.venda_id WHERE li.id=?''',(item_id,)).fetchone()
        return dict(row) if row else None
    def salvar_resultado(lote_id,item_id,resultado):
        estado='Autorizada' if resultado.get('authorized') else resultado.get('state') or 'Falha local'
        motivo=resultado.get('xMotivo') or resultado.get('error') or ''
        cstat=str(resultado.get('cStat') or '')
        with get_db() as db:
            db.execute('BEGIN IMMEDIATE')
            if estado=='Bloqueada':
                item_atual=db.execute('SELECT venda_id FROM nfce_lote_itens WHERE id=?',(item_id,)).fetchone()
                venda=db.execute('SELECT * FROM vendas WHERE id=?',(item_atual['venda_id'],)).fetchone()
                aberta=db.execute('SELECT id FROM nfce_tentativas WHERE venda_id=? AND concluida_em IS NULL',(item_atual['venda_id'],)).fetchone()
                if aberta:
                    estado='Incerta'
                    resultado['tentativa_id']=aberta['id']
                elif venda and ne.autorizada(venda):
                    estado='Pulada'
                else:
                    estado='Falha local'
            db.execute('UPDATE nfce_lote_itens SET estado=?,motivo=?,cstat=?,tentativa_id=? WHERE id=?',
                (estado,motivo,cstat,resultado.get('tentativa_id'),item_id))
            interromper=estado in ('Incerta','Bloqueada') or cstat in ('703','704','108','109','656')
            pendentes=db.execute("SELECT COUNT(*) FROM nfce_lote_itens WHERE lote_id=? AND estado='Pendente'",(lote_id,)).fetchone()[0]
            db.execute('UPDATE nfce_lotes SET estado=? WHERE id=?',('Interrompido' if interromper else ('Em andamento' if pendentes else 'Concluído'),lote_id))
            return item_json(db,item_id)

    @app.route('/vendas/nfce-em-lote')
    @protegido
    def nfce_lote():
        inicio,fim=request.args.get('inicio',''),request.args.get('fim','')
        try:
            for valor in (inicio,fim):
                if valor:date.fromisoformat(valor)
            if inicio and fim and inicio>fim:raise ValueError
        except ValueError:
            flash('Confira o período das vendas.','erro')
            return redirect(url_for('nfce_lote'))
        with get_db() as db:
            rows=db.execute("SELECT * FROM vendas WHERE status='Pago' AND (?='' OR data>=?) AND (?='' OR data<=?) ORDER BY data,id",(inicio,inicio,fim,fim)).fetchall()
            disponiveis=[dict(v) for v in rows if ne.selecionavel(db,v)]
            bloqueadas=[dict(v)|{'bloqueio':ne.bloqueio(db,v) or 'Tipo fiscal diferente de NFC-e.'} for v in rows if not ne.selecionavel(db,v) and not ne.autorizada(v)]
            lotes=db.execute('SELECT * FROM nfce_lotes ORDER BY id DESC LIMIT 20').fetchall()
        return render_template('nfce_lote.html',rows=disponiveis,bloqueadas=bloqueadas,lotes=lotes,inicio=inicio,fim=fim,agora=ne.agora())

    @app.route('/vendas/nfce-em-lote/datas-hoje',methods=['POST'])
    @protegido
    def nfce_datas_hoje():
        validar_csrf()
        try:
            alteradas,ignoradas=ne.atualizar_datas(get_db,request.form.getlist('venda_id'),usuario())
            if alteradas:backup_db('nfce-data-hoje')
            flash(f'Data fiscal preparada para hoje em {len(alteradas)} venda(s). O horário será atualizado novamente no envio.','ok')
            if ignoradas:flash(f'{len(ignoradas)} venda(s) protegidas ou indisponíveis foram preservadas.','erro')
        except ValueError as exc:
            flash(str(exc),'erro')
        return redirect(url_for('nfce_lote'))

    @app.route('/vendas/nfce-em-lote/criar',methods=['POST'])
    @protegido
    def criar_nfce_lote():
        validar_csrf()
        try:
            ids=ne.ids_validos(request.form.getlist('venda_id'))
            problema=validar_config()
            if problema:raise ValueError(problema)
            with get_db() as db:
                db.execute('BEGIN IMMEDIATE')
                vendas=[]
                for venda_id in ids:
                    v=db.execute('SELECT * FROM vendas WHERE id=?',(venda_id,)).fetchone()
                    if not v or not ne.selecionavel(db,v):
                        raise ValueError(f'A venda #{venda_id} mudou ou está protegida. Atualize a seleção.')
                    vendas.append(venda_id)
                assinatura=sha256(','.join(map(str,vendas)).encode()).hexdigest()
                existente=db.execute("SELECT id FROM nfce_lotes WHERE assinatura=? AND estado!='Concluído' ORDER BY id DESC LIMIT 1",(assinatura,)).fetchone()
                if existente:lote_id=existente['id']
                else:
                    for venda_id in vendas:
                        pendente=db.execute('''SELECT l.id FROM nfce_lote_itens i JOIN nfce_lotes l ON l.id=i.lote_id
                            WHERE i.venda_id=? AND i.estado IN ('Pendente','Enviando','Incerta','Bloqueada')
                            AND l.estado!='Concluído' LIMIT 1''',(venda_id,)).fetchone()
                        if pendente:raise ValueError(f"A venda #{venda_id} já está no lote #{pendente['id']}. Abra esse lote para continuar.")
                    lote_id=db.execute('INSERT INTO nfce_lotes(criado_em,usuario,assinatura) VALUES (?,?,?)',(ne.agora(),usuario(),assinatura)).lastrowid
                    db.executemany('INSERT INTO nfce_lote_itens(lote_id,venda_id) VALUES (?,?)',[(lote_id,v) for v in vendas])
            backup_db('nfce-lote-criado')
            session['nfce_lote_iniciar']={'id':lote_id,'em':ne.agora()}
            return redirect(url_for('ver_nfce_lote',lote_id=lote_id))
        except ValueError as exc:
            flash(str(exc),'erro')
            return redirect(url_for('nfce_lote'))

    @app.route('/vendas/nfce-em-lote/<int:lote_id>')
    @protegido
    def ver_nfce_lote(lote_id):
        dados=resumo(lote_id)
        with get_db() as db:
            itens=db.execute('''SELECT li.*,v.data,v.cliente_nome,v.nf_numero FROM nfce_lote_itens li
                LEFT JOIN vendas v ON v.id=li.venda_id WHERE li.lote_id=? ORDER BY li.id''',(lote_id,)).fetchall()
        marcador=session.pop('nfce_lote_iniciar',None)
        iniciar=bool(marcador and marcador['id']==lote_id and
            (datetime.fromisoformat(ne.agora())-datetime.fromisoformat(marcador['em'])).total_seconds()<60
            and dados['estado'] in ('Pronto','Em andamento'))
        return render_template('nfce_lote_detalhe.html',dados=dados,itens=itens,iniciar=iniciar)

    @app.route('/vendas/nfce-em-lote/<int:lote_id>/status')
    @protegido
    def status_nfce_lote(lote_id):
        return jsonify(resumo(lote_id))

    @app.route('/vendas/nfce-em-lote/<int:lote_id>/passo',methods=['POST'])
    @protegido
    def passo_nfce_lote(lote_id):
        validar_csrf()
        with get_db() as db:
            db.execute('BEGIN IMMEDIATE')
            lote=db.execute('SELECT * FROM nfce_lotes WHERE id=?',(lote_id,)).fetchone()
            if not lote:abort(404)
            incerta=db.execute("SELECT id FROM nfce_lote_itens WHERE lote_id=? AND estado IN ('Incerta','Bloqueada') LIMIT 1",(lote_id,)).fetchone()
            if incerta:
                return jsonify(resumo(lote_id,db=db)|{'aviso':'Consulte ou confira a tentativa bloqueada antes de continuar.'})
            em_curso=db.execute("SELECT * FROM nfce_lote_itens WHERE lote_id=? AND estado='Enviando' LIMIT 1",(lote_id,)).fetchone()
            if em_curso:
                idade=(datetime.fromisoformat(ne.agora())-datetime.fromisoformat(em_curso['iniciada_em'])).total_seconds()
                if idade<180:return jsonify(resumo(lote_id,db=db)|{'ocupado':True})
                item_id=em_curso['id']
                tentativa=db.execute('SELECT * FROM nfce_tentativas WHERE venda_id=? AND lote_id=? ORDER BY id DESC LIMIT 1',(em_curso['venda_id'],lote_id)).fetchone()
                if tentativa:
                    recuperacao=dict(tentativa)
                else:
                    db.execute("UPDATE nfce_lote_itens SET estado='Pendente',iniciada_em=NULL WHERE id=?",(item_id,))
                    recuperacao=None
            else:recuperacao=None
            if not recuperacao:
                item=db.execute("SELECT * FROM nfce_lote_itens WHERE lote_id=? AND estado='Pendente' ORDER BY id LIMIT 1",(lote_id,)).fetchone()
                if not item:
                    db.execute("UPDATE nfce_lotes SET estado='Concluído' WHERE id=?",(lote_id,))
                    return jsonify(resumo(lote_id,db=db))
                item_id=item['id']
                venda=db.execute('SELECT * FROM vendas WHERE id=?',(item['venda_id'],)).fetchone()
                if not venda or not ne.selecionavel(db,venda):
                    db.execute("UPDATE nfce_lote_itens SET estado='Pulada',motivo='Venda já emitida ou indisponível.' WHERE id=?",(item_id,))
                    return jsonify(resumo(lote_id,item_json(db,item_id),db))
                db.execute("UPDATE nfce_lote_itens SET estado='Enviando',iniciada_em=? WHERE id=?",(ne.agora(),item_id))
                db.execute("UPDATE nfce_lotes SET estado='Em andamento' WHERE id=?",(lote_id,))
                venda_id=item['venda_id']
        if recuperacao:
            resultado=ne.diario(recuperacao)
            if not resultado:resultado={'error':'A tentativa anterior foi interrompida. Confira sua situação antes de reenviar.'}
            resultado=ne.finalizar(get_db,recuperacao,resultado)
        else:
            try:
                resultado=emitir(venda_id,lote_id,usuario())
            except Exception as exc:
                with get_db() as db:
                    t=db.execute('SELECT * FROM nfce_tentativas WHERE venda_id=? AND lote_id=? AND concluida_em IS NULL ORDER BY id DESC LIMIT 1',(venda_id,lote_id)).fetchone()
                resultado={'ok':False,'authorized':False,'error':f'Emissão interrompida: {exc}'}
                if t:resultado=ne.finalizar(get_db,dict(t),resultado)
                else:resultado['state']='Falha local'
        item=salvar_resultado(lote_id,item_id,resultado)
        return jsonify(resumo(lote_id,item))

    @app.route('/vendas/nfce-tentativas/<int:tentativa_id>/consultar',methods=['POST'])
    @protegido
    def consultar_tentativa_nfce(tentativa_id):
        validar_csrf()
        resultado=consultar(tentativa_id)
        with get_db() as db:
            tentativa=db.execute('SELECT * FROM nfce_tentativas WHERE id=?',(tentativa_id,)).fetchone()
            if not tentativa:abort(404)
            item=db.execute('SELECT * FROM nfce_lote_itens WHERE tentativa_id=?',(tentativa_id,)).fetchone()
            if resultado.get('authorized') and item:
                db.execute("UPDATE nfce_lote_itens SET estado='Autorizada',motivo=?,cstat=? WHERE id=?",(resultado.get('xMotivo') or 'Autorização recuperada.',str(resultado.get('cStat') or '100'),item['id']))
                pendente=db.execute("SELECT COUNT(*) FROM nfce_lote_itens WHERE lote_id=? AND estado='Pendente'",(item['lote_id'],)).fetchone()[0]
                db.execute('UPDATE nfce_lotes SET estado=? WHERE id=?',('Pronto' if pendente else 'Concluído',item['lote_id']))
        flash(resultado.get('xMotivo') or resultado.get('error') or 'Consulta concluída.','ok' if resultado.get('authorized') else 'erro')
        if tentativa['lote_id']:return redirect(url_for('ver_nfce_lote',lote_id=tentativa['lote_id']))
        return redirect(url_for('recibo_venda',venda_id=tentativa['venda_id']))
