"""Conferência de vendas existentes. Não emite documentos nem movimenta saldos."""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json

FORMAS = ('Pix', 'Cartão crédito', 'Cartão débito')
REJEICOES_PAGAMENTO = {'391', '392', '437', '496', '796', '961', '963'}


def migrar(db):
    db.execute('''CREATE TABLE IF NOT EXISTS fiscal_conferencias (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        criado_em TEXT NOT NULL, inicio TEXT NOT NULL, fim TEXT NOT NULL,
        assinatura TEXT NOT NULL UNIQUE, conteudo_json TEXT NOT NULL,
        total_centavos INTEGER NOT NULL, vendas_qtd INTEGER NOT NULL
    )''')


def filtros(inicio, fim, formas):
    try:
        inicio, fim = date.fromisoformat(inicio), date.fromisoformat(fim)
    except (ValueError, TypeError):
        raise ValueError('Informe as datas inicial e final válidas.')
    if inicio > fim:
        raise ValueError('A data inicial não pode ser posterior à data final.')
    formas = [f for f in FORMAS if f in formas]
    if not formas:
        raise ValueError('Selecione Pix, cartão de crédito ou cartão de débito.')
    return inicio.isoformat(), fim.isoformat(), formas


def elegivel(venda):
    v = dict(venda)
    if v.get('status') != 'Pago' or v.get('forma_pagamento') not in FORMAS:
        return False
    if v.get('nf_chave') or v.get('nf_protocolo'):
        return False
    status = v.get('nf_status') or ''
    if status in ('', 'Não emitida'):
        return not v.get('nf_numero')
    return status == 'Rejeitada SEFAZ' and str(v.get('nf_cstat') or '') in REJEICOES_PAGAMENTO


def centavos(valor):
    try:
        numero = Decimal(str(valor or 0))
        if not numero.is_finite() or numero < 0:
            raise ValueError
        return int((numero * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise ValueError('Há um valor inválido nas vendas selecionadas. Confira os registros.')


def candidatos(db, inicio, fim, formas):
    marcadores = ','.join('?' for _ in formas)
    rows = db.execute(f'''SELECT * FROM vendas WHERE data BETWEEN ? AND ?
        AND forma_pagamento IN ({marcadores}) AND status='Pago' ORDER BY data, id''',
        (inicio, fim, *formas)).fetchall()
    return [dict(r) | {'total_centavos': centavos(r['total'])} for r in rows if elegivel(r)]


def ids_selecionados(valores):
    try:
        if not valores or len(valores) > 5000:
            raise ValueError
        ids = sorted(set(int(v) for v in valores))
        if any(v <= 0 for v in ids):
            raise ValueError
        return ids
    except (ValueError, TypeError):
        raise ValueError('Selecione de 1 a 5.000 vendas válidas para conferir.')


def preparar(db, inicio, fim, formas, valores):
    inicio, fim, formas = filtros(inicio, fim, formas)
    ids = ids_selecionados(valores)
    placeholders = ','.join('?' for _ in ids)
    vendas = [dict(v) for v in db.execute(f'SELECT * FROM vendas WHERE id IN ({placeholders}) ORDER BY data,id',ids)]
    if len(vendas) != len(ids):
        raise ValueError('Uma das vendas não existe mais. Atualize a lista antes de preparar o resumo.')
    for venda in vendas:
        if not elegivel(venda) or not inicio <= venda['data'] <= fim or venda['forma_pagamento'] not in formas:
            raise ValueError(f"A venda #{venda['id']} mudou ou não pode entrar neste resumo. Atualize a lista.")
    itens = db.execute(f'''SELECT vi.*,p.unidade,p.ncm,p.cfop,p.cst_csosn
        FROM venda_itens vi LEFT JOIN produtos p ON p.id=vi.produto_id
        WHERE vi.venda_id IN ({placeholders}) ORDER BY vi.venda_id,vi.id''',ids).fetchall()
    por_venda = defaultdict(list)
    for item in itens:
        por_venda[item['venda_id']].append(dict(item))
    resumo_vendas, agrupados, pagamentos = [], {}, {f: 0 for f in FORMAS}
    for venda in vendas:
        linhas = por_venda[venda['id']]
        if not linhas:
            raise ValueError(f"A venda #{venda['id']} não tem produtos. Confira o recibo antes de agrupar.")
        brutos = [centavos(i['total']) for i in linhas]
        bruto, desconto, liquido = sum(brutos), centavos(venda['desconto']), centavos(venda['total'])
        if bruto - desconto != liquido or desconto > bruto or centavos(venda['subtotal']) != bruto:
            raise ValueError(f"Os totais da venda #{venda['id']} não conferem com os produtos e o desconto.")
        # Rateio em centavos, pelo maior resto, preservando o desconto exato.
        rateios = [desconto * b // bruto if bruto else 0 for b in brutos]
        ordem = sorted(range(len(brutos)), key=lambda k: (desconto * brutos[k] % bruto) if bruto else 0, reverse=True)
        for k in ordem[:desconto - sum(rateios)]:
            rateios[k] += 1
        for item, valor_bruto, valor_desconto in zip(linhas, brutos, rateios):
            try:
                quantidade = Decimal(str(item['quantidade']))
                if not quantidade.is_finite() or quantidade <= 0:
                    raise ValueError
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError(f"Quantidade inválida na venda #{venda['id']}.")
            # Mantém separados preços ou classificações diferentes do mesmo produto.
            chave = (item['produto_id'],item['produto_nome'],str(item['preco_unit']),item['unidade'],item['ncm'],item['cfop'],item['cst_csosn'])
            if chave not in agrupados:
                agrupados[chave] = dict(produto_id=item['produto_id'],nome=item['produto_nome'] or 'Produto',
                    unidade=item['unidade'] or 'UN',ncm=item['ncm'] or '',cfop=item['cfop'] or '',
                    cst_csosn=item['cst_csosn'] or '',preco_unit_centavos=centavos(item['preco_unit']),
                    quantidade=Decimal(0),bruto_centavos=0,desconto_centavos=0,total_centavos=0)
            grupo = agrupados[chave]
            grupo['quantidade'] += quantidade
            grupo['bruto_centavos'] += valor_bruto
            grupo['desconto_centavos'] += valor_desconto
            grupo['total_centavos'] += valor_bruto - valor_desconto
        resumo_vendas.append(dict(id=venda['id'],data=venda['data'],cliente=venda['cliente_nome'] or 'Consumidor',
            forma=venda['forma_pagamento'],bruto_centavos=bruto,desconto_centavos=desconto,total_centavos=liquido,
            fiscal=venda['nf_status'] or 'Não emitida',numero_tentativa=venda['nf_numero'] or '',
            serie_tentativa=venda.get('nf_serie') or '',cstat=venda.get('nf_cstat') or ''))
        pagamentos[venda['forma_pagamento']] += liquido
    produtos = []
    for grupo in agrupados.values():
        grupo['quantidade'] = format(grupo['quantidade'].normalize(), 'f')
        produtos.append(grupo)
    bruto = sum(v['bruto_centavos'] for v in resumo_vendas)
    desconto = sum(v['desconto_centavos'] for v in resumo_vendas)
    total = sum(pagamentos.values())
    return dict(versao=1,inicio=inicio,fim=fim,formas=formas,vendas=resumo_vendas,produtos=produtos,
        pagamentos=pagamentos,bruto_centavos=bruto,desconto_centavos=desconto,total_centavos=total,
        modelo=None,emissao='Aguardando definição fiscal com o contador')


def assinatura(resumo):
    return hashlib.sha256(json.dumps(resumo,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def salvar(db, resumo, criado_em):
    chave = assinatura(resumo)
    existente = db.execute('SELECT id FROM fiscal_conferencias WHERE assinatura=?',(chave,)).fetchone()
    if existente:
        return existente['id'], False
    item_id = db.execute('''INSERT INTO fiscal_conferencias
        (criado_em,inicio,fim,assinatura,conteudo_json,total_centavos,vendas_qtd) VALUES (?,?,?,?,?,?,?)''',
        (criado_em,resumo['inicio'],resumo['fim'],chave,json.dumps(resumo,ensure_ascii=False),
         resumo['total_centavos'],len(resumo['vendas']))).lastrowid
    return item_id, True


def desatualizado(db, resumo):
    try:
        atual = preparar(db,resumo['inicio'],resumo['fim'],resumo['formas'],[v['id'] for v in resumo['vendas']])
        return assinatura(atual) != assinatura(resumo)
    except ValueError:
        return True
