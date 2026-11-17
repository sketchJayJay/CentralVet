"""Regressões locais, sem certificados reais e sem enviar notas à SEFAZ."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STARTUP = tempfile.TemporaryDirectory()
os.environ['DATA_DIR'] = STARTUP.name + '/data'
os.environ['PERSIST_MIRROR_DIR'] = STARTUP.name + '/mirror'
os.environ['CERTIFICADO_PATH'] = STARTUP.name + '/certs/test.pfx'
import app as cv
import pagamento_fiscal as pf


class PagamentoFiscalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        cv.DB_PATH = str(base / 'centralvet.db')
        cv.BACKUP_DIR = str(base / 'backups')
        cv.FISCAL_NFCE_DIR = str(base / 'nfce')
        Path(cv.BACKUP_DIR).mkdir()
        self.mirror = patch.object(cv, 'sync_persistent_mirror')
        self.mirror.start()
        self.addCleanup(self.mirror.stop)
        cv.init_db()
        cv.app.config['TESTING'] = True
        self.client = cv.app.test_client()
        with self.client.session_transaction() as session:
            session['logged'] = True
        with cv.get_db() as db:
            self.produto = db.execute("""INSERT INTO produtos
                (nome, codigo, estoque, preco_venda, preco_custo, unidade, ncm, cst_csosn)
                VALUES ('Produto de teste', 'TESTE', 18, 25, 10, 'UN', '23099090', '102')""").lastrowid

    def venda(self, forma='Cartão crédito', status_fiscal='Rejeitada SEFAZ', cstat='391'):
        with cv.get_db() as db:
            venda_id = db.execute("""INSERT INTO vendas
                (data, cliente_nome, forma_pagamento, status, subtotal, desconto, total, lucro,
                 nf_tipo, nf_status, nf_cstat, nf_numero, nf_serie, nf_emissao_tentativa_em)
                VALUES ('2026-10-01','Cliente de teste',?,'Pago',50,5,45,25,
                        'NFC-e',?,?, '87','1','2026-10-01T12:00:00-03:00')""",
                (forma, status_fiscal, cstat)).lastrowid
            db.execute("""INSERT INTO venda_itens
                (venda_id,produto_id,produto_nome,quantidade,preco_unit,custo_unit,total,lucro)
                VALUES (?,?,'Produto de teste',2,25,10,50,30)""", (venda_id,self.produto))
            db.execute("""INSERT INTO estoque_mov
                (data,produto_id,tipo,quantidade,origem,observacao)
                VALUES ('2026-10-01',?,'Saída',2,'Venda',?)""", (self.produto,f'Venda #{venda_id}'))
            cv.fs.sincronizar_venda(db, venda_id, cv.today_str())
        return venda_id

    def snapshot(self):
        with cv.get_db() as db:
            return {t: [tuple(r) for r in db.execute('SELECT * FROM ' + t + ' ORDER BY id')]
                    for t in ('produtos','venda_itens','estoque_mov','financeiro','financeiro_baixas')}

    def dados(self):
        return dict(pagamento_cnpj='05.577.343/0001-37', pagamento_bandeira='02',
                    pagamento_autorizacao='000123', pagamento_cnpj_recebedor='31.551.765/0001-43',
                    pagamento_terminal='PDV-01')

    def test_venda_antiga_sem_detalhes_gera_grupo_para_cartao_e_pix(self):
        for forma in ('Cartão crédito','Cartão débito','Pix'):
            with self.subTest(forma=forma):
                venda_id = self.venda(forma)
                row = cv.fetch_one('SELECT * FROM vendas WHERE id=?',(venda_id,))
                payload = pf.payload(row, cv._nfce_payment_code(forma)[0])
                self.assertEqual(payload['tpIntegra'], 2)
                self.assertIsNone(payload['cAut'])
                self.assertIsNone(payload['CNPJ'])

    def test_complementar_rejeitada_preserva_venda_estoque_e_financeiro(self):
        venda_id = self.venda()
        before = self.snapshot()
        original = dict(cv.fetch_one('SELECT * FROM vendas WHERE id=?',(venda_id,)))
        response = self.client.post(f'/vendas/{venda_id}/pagamento-fiscal',data=self.dados())
        self.assertEqual(response.status_code,302)
        after = dict(cv.fetch_one('SELECT * FROM vendas WHERE id=?',(venda_id,)))
        for chave in original.keys() - set(pf.CAMPOS):
            self.assertEqual(original[chave],after[chave],chave)
        self.assertEqual(after['pagamento_autorizacao'],'000123')
        self.assertEqual(after['pagamento_cnpj'],'05577343000137')
        self.assertEqual(before,self.snapshot())

    def test_cnpj_invalido_nao_grava_mudancas_parciais(self):
        venda_id = self.venda()
        for invalido in ('00000000000000','12345678901234','ABC05577343000137'):
            dados = self.dados() | {'pagamento_cnpj':invalido}
            response=self.client.post(f'/vendas/{venda_id}/pagamento-fiscal',data=dados,follow_redirects=True)
            self.assertIn(b'CNPJ',response.data)
            self.assertIsNone(cv.fetch_one('SELECT pagamento_autorizacao FROM vendas WHERE id=?',(venda_id,))[0])

    def test_autorizada_e_retorno_incerto_nao_permitem_alterar(self):
        for status,cstat in (('Autorizada SEFAZ','100'),('Rejeitada SEFAZ','204'),
                             ('Falha antes do envio à SEFAZ',''),('Rejeitada SEFAZ','539')):
            with self.subTest(status=status,cstat=cstat):
                venda_id=self.venda(status_fiscal=status,cstat=cstat)
                self.client.post(f'/vendas/{venda_id}/pagamento-fiscal',data=self.dados())
                row=cv.fetch_one('SELECT * FROM vendas WHERE id=?',(venda_id,))
                self.assertIsNone(row['pagamento_autorizacao'])
                self.assertFalse(cv.pagamento_fiscal_editavel(row))

    def test_protocolo_ou_chave_bloqueiam_dados_mesmo_com_status_incorreto(self):
        for campo in ('nf_protocolo','nf_chave'):
            venda_id=self.venda()
            cv.exec_sql(f'UPDATE vendas SET {campo}=? WHERE id=?',('12345',venda_id))
            self.client.post(f'/vendas/{venda_id}/pagamento-fiscal',data=self.dados())
            self.assertIsNone(cv.fetch_one('SELECT pagamento_autorizacao FROM vendas WHERE id=?',(venda_id,))[0])

    def test_campos_tem_limites_e_nao_truncam_autorizacao(self):
        dados=self.dados()
        dados['pagamento_autorizacao']='E'*128
        self.assertEqual(pf.validar(dados,'17')['pagamento_autorizacao'],'E'*128)
        for chave,valor in (('pagamento_autorizacao','E'*129),('pagamento_autorizacao','A\nB'),
                             ('pagamento_terminal','T'*41),('pagamento_bandeira','00')):
            with self.subTest(chave=chave):
                with self.assertRaises(ValueError):
                    pf.validar(self.dados() | {chave:valor},'03')

    def test_pix_nao_envia_bandeira_e_dinheiro_nao_envia_card(self):
        self.assertIsNone(pf.payload(self.dados(),'17')['tBand'])
        for tpag in ('01','05','15','90','99'):
            self.assertEqual(pf.payload(self.dados(),tpag),{})

    def test_migracao_aditiva_preserva_dados_e_pode_repetir(self):
        venda_id=self.venda()
        before=self.snapshot()
        with cv.get_db() as db:
            for coluna in pf.CAMPOS:
                db.execute('ALTER TABLE vendas DROP COLUMN ' + coluna)
        cv.init_db()
        cv.init_db()
        self.assertEqual(before,self.snapshot())
        row=cv.fetch_one('SELECT * FROM vendas WHERE id=?',(venda_id,))
        self.assertEqual(row['total'],45)
        for coluna in pf.CAMPOS:
            self.assertIsNone(row[coluna])

    def test_reenvio_corrigido_e_idempotencia_nao_repetem_baixa(self):
        numeros=set()
        for forma in ('Cartão crédito','Cartão débito','Pix','Dinheiro'):
            with self.subTest(forma=forma):
                venda_id=self.venda(forma)
                before=self.snapshot()
                captured=[]
                def engine(payload,**kwargs):
                    captured.append(payload)
                    return dict(ok=True,authorized=True,cStat='100',xMotivo='Teste local',
                                chave='CHAVE-DE-TESTE',protocolo='PROTOCOLO-DE-TESTE')
                with patch.object(cv,'cert_runtime_status',return_value=dict(exists=True,has_password=True)), \
                     patch.object(cv,'run_fiscal_engine',side_effect=engine):
                    result=cv.emitir_nfce_sefaz_internal(venda_id)
                    again=cv.emitir_nfce_sefaz_internal(venda_id)
                self.assertTrue(result['authorized'])
                self.assertTrue(again['authorized'])
                self.assertEqual(len(captured),1)
                self.assertEqual(captured[0]['nNF'],87+len(numeros))
                self.assertNotIn(captured[0]['nNF'],numeros)
                numeros.add(captured[0]['nNF'])
                self.assertEqual(captured[0]['issue_datetime'][:10],cv.today_str())
                self.assertEqual(captured[0]['valor_pago'],45)
                if forma=='Dinheiro':
                    self.assertEqual(captured[0]['payment_details'],{})
                else:
                    self.assertEqual(captured[0]['payment_details']['tpIntegra'],2)
                self.assertEqual(before,self.snapshot())

    def test_dados_do_comprovante_chegam_ao_motor_fiscal(self):
        venda_id=self.venda()
        self.client.post(f'/vendas/{venda_id}/pagamento-fiscal',data=self.dados())
        with patch.object(cv,'cert_runtime_status',return_value=dict(exists=True,has_password=True)), \
             patch.object(cv,'run_fiscal_engine',return_value=dict(ok=False,authorized=False,cStat='391')) as engine:
            self.client.post(f'/vendas/{venda_id}/fiscal',data={'nf_tipo':'NFC-e'})
        details=engine.call_args.args[0]['payment_details']
        self.assertEqual(details['cAut'],'000123')
        self.assertEqual(details['tBand'],'02')
        self.assertEqual(details['CNPJReceb'],'31551765000143')

    def test_nova_venda_e_edicao_salvam_os_dados(self):
        for forma in ('Cartão crédito','Cartão débito','Pix'):
            with self.subTest(forma=forma):
                form=self.dados() | dict(data=cv.today_str(),forma_pagamento=forma,status='Pago',
                    nf_tipo='Sem nota agora',cliente_nome='Consumidor',desconto='0',
                    **{'produto_id[]':str(self.produto),'quantidade[]':'1','preco_unit[]':'25'})
                response=self.client.post('/vendas/nova',data=form)
                self.assertEqual(response.status_code,302)
                row=cv.fetch_one('SELECT * FROM vendas ORDER BY id DESC LIMIT 1')
                self.assertEqual(row['pagamento_autorizacao'],'000123')
                self.assertEqual(row['forma_pagamento'],forma)
                self.client.post(f'/vendas/{row["id"]}/editar',data=form | {
                    'cliente_id':str(row['cliente_id'] or ''), 'pagamento_autorizacao':'000456'})
                updated=cv.fetch_one('SELECT * FROM vendas WHERE id=?',(row['id'],))
                self.assertEqual(updated['pagamento_autorizacao'],'000456')
                self.assertEqual(updated['total'],25)

    def test_telas_renderizam_e_campos_respeitam_o_status(self):
        venda_id=self.venda()
        for url in ('/vendas/nova',f'/vendas/{venda_id}/recibo'):
            response=self.client.get(url)
            self.assertEqual(response.status_code,200)
            self.assertIn(b'pagamento_autorizacao',response.data)
        cv.exec_sql("UPDATE vendas SET nf_status='Autorizada SEFAZ', nf_protocolo='123' WHERE id=?",(venda_id,))
        self.assertNotIn(b'pagamento_autorizacao',self.client.get(f'/vendas/{venda_id}/recibo').data)


if __name__ == '__main__':
    unittest.main()
