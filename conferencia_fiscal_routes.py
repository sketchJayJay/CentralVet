import csv
from datetime import datetime
from decimal import Decimal
from io import BytesIO, StringIO
import json
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape

from flask import flash, redirect, render_template, request, send_file, url_for
import conferencia_fiscal as cf


def dinheiro(centavos):
    return 'R$ ' + format(Decimal(centavos) / 100, ',.2f').replace(',', '_').replace('.', ',').replace('_', '.')


def data_br(valor):
    return datetime.fromisoformat(valor).strftime('%d/%m/%Y')


def csv_resumo(resumo, numero, revisar):
    out = StringIO(newline='')
    writer = csv.writer(out,delimiter=';')
    def linha(*valores):
        seguros = []
        for valor in valores:
            valor = str(valor if valor is not None else '')
            if valor.lstrip().startswith(('=','+','-','@')) or valor.startswith(('\t','\r','\n')):
                valor = "'" + valor
            seguros.append(valor)
        writer.writerow(seguros)
    def valor(n):
        return format(Decimal(n)/100,'.2f').replace('.',',')
    linha('CONFERÊNCIA DE VENDAS',numero,'DOCUMENTO NÃO FISCAL')
    linha('Período',resumo['inicio'],resumo['fim'])
    linha('Modelo da nota','A confirmar com o contador')
    linha('Situação','REVISAR: vendas alteradas após a preparação' if revisar else resumo['emissao'])
    linha('Subtotal',valor(resumo['bruto_centavos']),'Descontos',valor(resumo['desconto_centavos']),'Total',valor(resumo['total_centavos']))
    linha()
    linha('PAGAMENTOS','Valor das vendas (R$)')
    for forma,total in resumo['pagamentos'].items():
        linha(forma,valor(total))
    linha()
    linha('VENDAS','Data','Cliente','Pagamento','Subtotal (R$)','Desconto (R$)','Total (R$)','Situação fiscal','Tentativa nº','Série','cStat')
    for v in resumo['vendas']:
        linha(v['id'],v['data'],v['cliente'],v['forma'],valor(v['bruto_centavos']),valor(v['desconto_centavos']),
              valor(v['total_centavos']),v['fiscal'],v['numero_tentativa'],v['serie_tentativa'],v['cstat'])
    linha()
    linha('PRODUTOS','Quantidade','Unidade','Preço unitário (R$)','Subtotal (R$)','Desconto rateado (R$)','Total (R$)','NCM (cadastro)','CFOP (cadastro)','CST/CSOSN (cadastro)')
    for p in resumo['produtos']:
        linha(p['nome'],p['quantidade'].replace('.',','),p['unidade'],valor(p['preco_unit_centavos']),valor(p['bruto_centavos']),
              valor(p['desconto_centavos']),valor(p['total_centavos']),p['ncm'],p['cfop'],p['cst_csosn'])
    return BytesIO(out.getvalue().encode('utf-8-sig'))


def pdf_resumo(resumo, numero, criado_em, revisar):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    out = BytesIO()
    doc = SimpleDocTemplate(out,pagesize=A4,rightMargin=16*mm,leftMargin=16*mm,
        topMargin=16*mm,bottomMargin=18*mm,title=f'Conferência de vendas #{numero}',author='CENTRALVET')
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='CelulaCV',parent=styles['BodyText'],fontSize=8,leading=10,spaceAfter=0))
    def p(texto):
        return Paragraph(escape(str(texto)[:400]),styles['CelulaCV'])
    def tabela(cabecalho,linhas,larguras):
        t=Table([[p(v) for v in cabecalho]]+[[p(v) for v in linha] for linha in linhas],
            colWidths=[v*mm for v in larguras],repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e9f1eb')),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f7f9f7')]),
            ('GRID',(0,0),(-1,-1),.25,colors.HexColor('#d7e2da')),
            ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),6),
            ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
        return t
    partes=[Paragraph('CENTRALVET Agropecuária',styles['Title']),
        Paragraph(f'Conferência de vendas #{numero}',styles['Heading2']),
        p(f"Período: {data_br(resumo['inicio'])} a {data_br(resumo['fim'])} | {len(resumo['vendas'])} vendas"),
        p(f'Preparada em {criado_em}'),Spacer(1,3*mm),
        p('DOCUMENTO NÃO FISCAL. Modelo e procedimento de emissão a confirmar com o contador.'),
        Spacer(1,4*mm)]
    if revisar:
        partes += [p('REVISAR: algumas vendas foram alteradas ou receberam nota após a preparação deste resumo.'),Spacer(1,3*mm)]
    partes += [tabela(['Subtotal','Descontos','Total das vendas'],[[dinheiro(resumo['bruto_centavos']),
        dinheiro(resumo['desconto_centavos']),dinheiro(resumo['total_centavos'])]],[59,59,60]),Spacer(1,4*mm),
        tabela(['Forma de pagamento','Total'],[[f,dinheiro(v)] for f,v in resumo['pagamentos'].items()],[118,60]),
        Spacer(1,4*mm),Paragraph('Produtos e descontos',styles['Heading2']),
        tabela(['Produto','Qtd / un.','Subtotal','Desconto','Total'],[
            [pdt['nome'],pdt['quantidade'].replace('.',',')+' '+pdt['unidade'],dinheiro(pdt['bruto_centavos']),
             dinheiro(pdt['desconto_centavos']),dinheiro(pdt['total_centavos'])] for pdt in resumo['produtos']],[68,23,29,29,29]),
        Spacer(1,4*mm),Paragraph('Vendas de origem',styles['Heading2']),
        tabela(['Venda','Data','Cliente','Pagamento','Total'],[
            [f"#{v['id']}",data_br(v['data']),v['cliente'],v['forma'],dinheiro(v['total_centavos'])]
            for v in resumo['vendas']],[18,25,65,38,32])]
    def rodape(canvas,documento):
        canvas.setFont('Helvetica',8)
        canvas.drawString(16*mm,10*mm,'Conferência para o contador. Não substitui NF-e ou NFC-e.')
        canvas.drawRightString(194*mm,10*mm,f'Página {documento.page}')
    doc.build(partes,onFirstPage=rodape,onLaterPages=rodape)
    out.seek(0)
    return out


def registrar_rotas(app,get_db,backup_db,login_required,today_str):
    app.jinja_env.filters['centavos_brl'] = dinheiro

    @app.route('/vendas/conferencia-fiscal')
    @login_required
    def conferencia_fiscal():
        hoje=today_str()
        inicio=request.args.get('inicio') or hoje[:8]+'01'
        fim=request.args.get('fim') or hoje
        formas=request.args.getlist('forma') if request.args.get('filtrar') else list(cf.FORMAS)
        try:
            inicio,fim,formas=cf.filtros(inicio,fim,formas)
            with get_db() as db:
                rows=cf.candidatos(db,inicio,fim,formas)
                recentes=db.execute('SELECT id,criado_em,inicio,fim,total_centavos,vendas_qtd FROM fiscal_conferencias ORDER BY id DESC LIMIT 20').fetchall()
        except ValueError as exc:
            flash(str(exc),'erro')
            return redirect(url_for('conferencia_fiscal'))
        pagamentos={f:sum(v['total_centavos'] for v in rows if v['forma_pagamento']==f) for f in cf.FORMAS}
        return render_template('conferencia_fiscal.html',inicio=inicio,fim=fim,formas=formas,
            todas_formas=cf.FORMAS,rows=rows,pagamentos=pagamentos,total_centavos=sum(pagamentos.values()),recentes=recentes)

    @app.route('/vendas/conferencia-fiscal/preparar',methods=['POST'])
    @login_required
    def preparar_conferencia_fiscal():
        try:
            with get_db() as db:
                db.execute('BEGIN IMMEDIATE')
                resumo=cf.preparar(db,request.form.get('inicio'),request.form.get('fim'),
                    request.form.getlist('forma'),request.form.getlist('venda_id'))
                numero,novo=cf.salvar(db,resumo,datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat(timespec='seconds'))
            if novo:
                backup_db('conferencia-fiscal')
            flash('Resumo preparado para conferir com o contador.' if novo else 'Este resumo já estava preparado. Abrimos o registro existente.','ok')
            return redirect(url_for('ver_conferencia_fiscal',conferencia_id=numero))
        except ValueError as exc:
            flash(str(exc),'erro')
            return redirect(url_for('conferencia_fiscal'))

    def carregar(numero):
        with get_db() as db:
            row=db.execute('SELECT * FROM fiscal_conferencias WHERE id=?',(numero,)).fetchone()
            if not row:
                return None,None,None
            resumo=json.loads(row['conteudo_json'])
            return dict(row),resumo,cf.desatualizado(db,resumo)

    @app.route('/vendas/conferencia-fiscal/<int:conferencia_id>')
    @login_required
    def ver_conferencia_fiscal(conferencia_id):
        registro,resumo,revisar=carregar(conferencia_id)
        if not registro:
            flash('Resumo não encontrado.','erro')
            return redirect(url_for('conferencia_fiscal'))
        return render_template('conferencia_fiscal_detalhe.html',registro=registro,resumo=resumo,revisar=revisar)

    @app.route('/vendas/conferencia-fiscal/<int:conferencia_id>/exportar.<formato>')
    @login_required
    def exportar_conferencia_fiscal(conferencia_id,formato):
        if formato not in ('csv','pdf'):
            return 'Formato não encontrado.',404
        registro,resumo,revisar=carregar(conferencia_id)
        if not registro:
            return 'Resumo não encontrado.',404
        if formato=='csv':
            arquivo=csv_resumo(resumo,conferencia_id,revisar)
            mime='text/csv; charset=utf-8'
        else:
            arquivo=pdf_resumo(resumo,conferencia_id,registro['criado_em'],revisar)
            mime='application/pdf'
        return send_file(arquivo,mimetype=mime,as_attachment=True,
            download_name=f'CentralVet-Conferencia-{conferencia_id}.{formato}')
