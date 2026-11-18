"""Consulta e entrega privada dos documentos já autorizados, sem emitir notas."""
from datetime import datetime
import sqlite3
from flask import abort, render_template, request, send_file
import arquivo_fiscal as af


def registrar_rotas(app,get_db,login_required,raiz_fiscal,cnpj_emitente):
    def sincronizar():
        return af.sincronizar(get_db,raiz_fiscal(),cnpj_emitente())

    def mes_selecionado():
        mes=request.args.get('mes',af.agora()[:7])
        if mes=='sem-data':return mes
        try:return af.mes_valido(mes)
        except ValueError as exc:abort(400,description=str(exc))

    def documentos(mes):
        with get_db() as db:
            if mes=='sem-data':return db.execute('SELECT * FROM fiscal_documentos WHERE mes IS NULL ORDER BY id DESC').fetchall()
            return db.execute('SELECT * FROM fiscal_documentos WHERE mes=? ORDER BY emitido_em DESC,id DESC',(mes,)).fetchall()

    def documento(numero):
        with get_db() as db:doc=db.execute('SELECT * FROM fiscal_documentos WHERE id=?',(numero,)).fetchone()
        if not doc:abort(404,description='Nota não encontrada no arquivo fiscal.')
        return doc

    def privado(resposta):
        resposta.headers['Cache-Control']='private, no-store, max-age=0'
        resposta.headers['X-Content-Type-Options']='nosniff'
        return resposta

    def pacote(docs,nome,modo):
        if modo not in ('xml','completo'):abort(400,description='Selecione XML ou XML e PDF.')
        if not docs:abort(404,description='Não há notas neste período.')
        stream,faltas=af.montar_zip(raiz_fiscal(),docs,modo)
        nome=nome+('-XML' if modo=='xml' else '-XML-PDF')+('-PARCIAL' if faltas else '')+'.zip'
        resposta=send_file(stream,mimetype='application/zip',as_attachment=True,download_name=nome)
        resposta.call_on_close(stream.close)
        resposta.headers['X-Arquivo-Nome']=nome
        resposta.headers['X-Fiscal-Pendencias']=str(len(faltas))
        return privado(resposta)

    @app.get('/fiscal/notas')
    @login_required
    def notas_emitidas():
        mes=mes_selecionado();erro=''
        try:sincronizar()
        except (OSError,ValueError,sqlite3.Error):
            app.logger.exception('Falha ao atualizar o arquivo mensal de notas')
            erro='Não foi possível atualizar todos os documentos. Confira o armazenamento e tente abrir esta tela novamente.'
        with get_db() as db:
            meses={r[0] for r in db.execute('SELECT DISTINCT mes FROM fiscal_documentos WHERE mes IS NOT NULL')}
            sem_data=db.execute('SELECT COUNT(*) FROM fiscal_documentos WHERE mes IS NULL').fetchone()[0]
        meses.add(af.agora()[:7])
        if mes!='sem-data':meses.add(mes)
        linhas=[]
        for row in documentos(mes):
            d=dict(row)
            d['xml_ok']=bool(af.arquivo_documento(raiz_fiscal(),d,'xml'))
            d['pdf_ok']=bool(af.arquivo_documento(raiz_fiscal(),d,'pdf'))
            d['data_exibicao']=datetime.fromisoformat(d['emitido_em']).strftime('%d/%m/%Y %H:%M') if d['emitido_em'] else 'Data a conferir'
            linhas.append(d)
        return render_template('notas_emitidas.html',mes=mes,meses=sorted(meses,reverse=True),sem_data=sem_data,
            rows=linhas,erro=erro,total_centavos=sum(d['valor_centavos'] for d in linhas if d['ambiente']=='1'),
            nfce=sum(d['modelo']=='65' for d in linhas),nfe=sum(d['modelo']=='55' for d in linhas),
            pendentes=sum(not d['xml_ok'] or not d['pdf_ok'] for d in linhas))

    @app.get('/fiscal/notas/<int:documento_id>/<tipo>')
    @login_required
    def baixar_nota_emitida(documento_id,tipo):
        if tipo not in ('xml','pdf'):abort(404)
        doc=documento(documento_id)
        path=af.arquivo_documento(raiz_fiscal(),doc,tipo)
        if not path:abort(404,description='Arquivo indisponível ou com integridade divergente. Confira a nota na área Notas emitidas.')
        nome=('NFCe' if doc['modelo']=='65' else 'NFe')+'-'+(doc['chave'] or str(doc['id']))+('-DANFE' if tipo=='pdf' else '')+'.'+tipo
        return privado(send_file(path,mimetype='application/xml' if tipo=='xml' else 'application/pdf',as_attachment=True,download_name=nome))

    @app.get('/fiscal/notas/<int:documento_id>/pacote.zip')
    @login_required
    def pacote_nota_emitida(documento_id):
        doc=documento(documento_id)
        return pacote([doc],('NFCe' if doc['modelo']=='65' else 'NFe')+'-'+(doc['chave'] or str(doc['id'])),'completo')

    @app.get('/fiscal/notas/pacote.zip')
    @login_required
    def pacote_notas_mes():
        mes=mes_selecionado();modo=request.args.get('modo','completo')
        if modo not in ('xml','completo'):abort(400,description='Selecione XML ou XML e PDF.')
        try:sincronizar()
        except (OSError,ValueError,sqlite3.Error):
            app.logger.exception('Falha ao preparar pacote de notas')
            abort(503,description='Não foi possível conferir todas as notas. Abra Notas emitidas e tente novamente.')
        return pacote(documentos(mes),'CentralVet-Notas-'+mes,modo)
