import csv
from io import StringIO
import json
import unittest
from unittest.mock import patch

import test_pagamento_fiscal as fixtures
import conferencia_fiscal as cf


class ConferenciaFiscalTests(unittest.TestCase):
    def setUp(self):
        self.base=fixtures.PagamentoFiscalTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.cv=fixtures.cv
        self.client=self.base.client

    def venda(self,forma='Pix',emitida=False):
        numero=self.base.venda(forma=forma)
        if emitida:
            self.cv.exec_sql("UPDATE vendas SET nf_status='Autorizada SEFAZ',nf_chave='CHAVE',nf_protocolo='PROTOCOLO' WHERE id=?",(numero,))
        return numero

    def preparar(self,ids):
        with self.cv.get_db() as db:
            return cf.preparar(db,'2026-10-01','2026-10-31',list(cf.FORMAS),ids)

    def post(self,ids,**outros):
        return self.client.post('/vendas/conferencia-fiscal/preparar',data={
            'inicio':'2026-10-01','fim':'2026-10-31','forma':list(cf.FORMAS),'venda_id':[str(i) for i in ids],**outros})

    def registro(self):
        return self.cv.fetch_one('SELECT * FROM fiscal_conferencias ORDER BY id DESC LIMIT 1')

    def test_filtro_exclui_notas_ja_emitidas_e_retorno_incerto(self):
        validos=[self.venda(f) for f in cf.FORMAS]
        self.venda(emitida=True)
        self.venda('Dinheiro')
        pendente=self.venda()
        self.cv.exec_sql("UPDATE vendas SET status='Pendente' WHERE id=?",(pendente,))
        duplicada=self.venda()
        self.cv.exec_sql("UPDATE vendas SET nf_cstat='204' WHERE id=?",(duplicada,))
        chave=self.venda()
        self.cv.exec_sql("UPDATE vendas SET nf_chave='CHAVE' WHERE id=?",(chave,))
        with self.cv.get_db() as db:
            rows=cf.candidatos(db,'2026-10-01','2026-10-31',list(cf.FORMAS))
        self.assertEqual([r['id'] for r in rows],validos)

    def test_soma_separa_pagamentos_e_nao_duplica_ids(self):
        ids=[self.venda(f) for f in cf.FORMAS]
        resumo=self.preparar(ids+ids)
        self.assertEqual(resumo['total_centavos'],13500)
        self.assertEqual(resumo['bruto_centavos'],15000)
        self.assertEqual(resumo['desconto_centavos'],1500)
        self.assertEqual(resumo['pagamentos'],{f:4500 for f in cf.FORMAS})
        self.assertEqual(len(resumo['vendas']),3)
        self.assertEqual(len(resumo['produtos']),1)
        self.assertEqual(resumo['produtos'][0]['quantidade'],'6')
        self.assertEqual(sum(p['total_centavos'] for p in resumo['produtos']),13500)
        self.assertIsNone(resumo['modelo'])

    def test_rateio_de_um_centavo_fecha_sem_valores_negativos(self):
        venda=self.venda()
        with self.cv.get_db() as db:
            db.execute('DELETE FROM venda_itens WHERE venda_id=?',(venda,))
            db.execute('UPDATE vendas SET subtotal=.03,desconto=.01,total=.02 WHERE id=?',(venda,))
            for i in range(3):
                db.execute('''INSERT INTO venda_itens(venda_id,produto_id,produto_nome,quantidade,preco_unit,total)
                    VALUES (?,?,?,1,.01,.01)''',(venda,self.base.produto,f'Item {i}'))
        resumo=self.preparar([venda])
        self.assertEqual(resumo['total_centavos'],2)
        self.assertEqual(sum(p['desconto_centavos'] for p in resumo['produtos']),1)
        self.assertEqual(sum(p['total_centavos'] for p in resumo['produtos']),2)
        self.assertTrue(all(p['total_centavos']>=0 for p in resumo['produtos']))

    def test_preparar_preserva_vendas_estoque_financeiro_e_numeracao(self):
        ids=[self.venda(f) for f in cf.FORMAS]
        before=self.base.snapshot()
        vendas=[tuple(r) for r in self.cv.fetch_all('SELECT * FROM vendas ORDER BY id')]
        config=tuple(self.cv.fetch_one('SELECT * FROM fiscal_config'))
        with patch.object(self.cv,'run_fiscal_engine',side_effect=AssertionError('Não deve emitir nota')):
            response=self.post(ids,total_centavos='1',modelo='65')
            self.assertEqual(response.status_code,302)
            detalhe=self.client.get(response.location)
            self.assertEqual(detalhe.status_code,200)
        self.assertEqual(before,self.base.snapshot())
        self.assertEqual(vendas,[tuple(r) for r in self.cv.fetch_all('SELECT * FROM vendas ORDER BY id')])
        self.assertEqual(config,tuple(self.cv.fetch_one('SELECT * FROM fiscal_config')))
        resumo=json.loads(self.registro()['conteudo_json'])
        self.assertEqual(resumo['total_centavos'],13500)
        self.assertIsNone(resumo['modelo'])

    def test_duplo_clique_reaproveita_o_mesmo_resumo(self):
        ids=[self.venda(f) for f in cf.FORMAS]
        primeira=self.post(ids)
        segunda=self.post(list(reversed(ids))+ids)
        self.assertEqual(primeira.location,segunda.location)
        self.assertEqual(self.cv.fetch_one('SELECT COUNT(*) FROM fiscal_conferencias')[0],1)

    def test_revalida_venda_alterada_entre_lista_e_confirmacao(self):
        venda=self.venda()
        self.client.get('/vendas/conferencia-fiscal?inicio=2026-10-01&fim=2026-10-31')
        self.cv.exec_sql("UPDATE vendas SET nf_status='Autorizada SEFAZ',nf_protocolo='123' WHERE id=?",(venda,))
        self.post([venda])
        self.assertEqual(self.cv.fetch_one('SELECT COUNT(*) FROM fiscal_conferencias')[0],0)

    def test_snapshot_sinaliza_mudancas_e_mantem_os_valores_originais(self):
        venda=self.venda()
        response=self.post([venda])
        self.cv.exec_sql("UPDATE vendas SET nf_status='Autorizada SEFAZ',nf_protocolo='123' WHERE id=?",(venda,))
        detalhe=self.client.get(response.location)
        self.assertIn('Algumas vendas mudaram'.encode(),detalhe.data)
        csv_result=self.client.get(response.location+'/exportar.csv').data.decode('utf-8-sig')
        self.assertIn('REVISAR',csv_result)
        self.assertEqual(self.registro()['total_centavos'],4500)

    def test_exporta_pdf_e_csv_com_desconto_e_pagamentos(self):
        ids=[self.venda(f) for f in cf.FORMAS]
        response=self.post(ids)
        pdf=self.client.get(response.location+'/exportar.pdf')
        self.assertEqual(pdf.status_code,200)
        self.assertEqual(pdf.mimetype,'application/pdf')
        self.assertTrue(pdf.data.startswith(b'%PDF-'))
        self.assertGreater(len(pdf.data),2500)
        csv_result=self.client.get(response.location+'/exportar.csv')
        linhas=list(csv.reader(StringIO(csv_result.data.decode('utf-8-sig')),delimiter=';'))
        self.assertIn(['Pix','45,00'],linhas)
        self.assertIn(['Cartão crédito','45,00'],linhas)
        self.assertIn(['Cartão débito','45,00'],linhas)
        self.assertEqual(linhas[4],['Subtotal','150,00','Descontos','15,00','Total','135,00'])

    def test_exportacao_trata_nomes_com_formula_e_caracteres_especiais(self):
        venda=self.venda()
        self.cv.exec_sql('UPDATE vendas SET cliente_nome=? WHERE id=?',('=2+2',venda))
        self.cv.exec_sql('UPDATE venda_itens SET produto_nome=? WHERE venda_id=?',('Ração <Premium> & Suplemento',venda))
        response=self.post([venda])
        self.assertIn(b"'=2+2",self.client.get(response.location+'/exportar.csv').data)
        self.assertEqual(self.client.get(response.location+'/exportar.pdf').status_code,200)
        self.assertIn(b'&lt;Premium&gt;',self.client.get(response.location).data)

    def test_recusa_itens_ausentes_totais_inconsistentes_e_filtros_invalidos(self):
        venda=self.venda()
        for ids in ([],['x'],['-1'],[99999]):
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):self.preparar(ids)
        for inicio,fim,formas in [('2026-02-30','2026-03-01',['Pix']),('2026-10-31','2026-10-01',['Pix']),('2026-10-01','2026-10-31',[])]:
            with self.assertRaises(ValueError):cf.filtros(inicio,fim,formas)
        self.cv.exec_sql('UPDATE vendas SET total=46 WHERE id=?',(venda,))
        with self.assertRaises(ValueError):self.preparar([venda])
        self.cv.exec_sql('UPDATE vendas SET total=45 WHERE id=?',(venda,))
        self.cv.exec_sql('DELETE FROM venda_itens WHERE venda_id=?',(venda,))
        with self.assertRaises(ValueError):self.preparar([venda])

    def test_resumos_sobrevivem_a_migracao_e_exigem_login(self):
        self.post([self.venda()])
        original=dict(self.registro())
        self.cv.init_db()
        self.cv.init_db()
        self.assertEqual(original,dict(self.registro()))
        anonimo=self.cv.app.test_client()
        for rota in ('/vendas/conferencia-fiscal','/vendas/conferencia-fiscal/1','/vendas/conferencia-fiscal/1/exportar.pdf'):
            self.assertEqual(anonimo.get(rota).status_code,302)
        self.assertEqual(anonimo.post('/vendas/conferencia-fiscal/preparar',data={}).status_code,302)

    def test_precos_diferentes_nao_sao_misturados(self):
        a,b=self.venda(),self.venda()
        self.cv.exec_sql('UPDATE venda_itens SET preco_unit=12.5,quantidade=4 WHERE venda_id=?',(b,))
        resumo=self.preparar([a,b])
        self.assertEqual(len(resumo['produtos']),2)
        self.assertEqual(sum(p['total_centavos'] for p in resumo['produtos']),9000)


if __name__=='__main__':unittest.main()
