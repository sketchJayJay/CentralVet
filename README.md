# CENTRALVET Agropecuária — NF-e + NFC-e direto com SEFAZ/MG

Esta versão inclui emissão de **NF-e modelo 55 de devolução diretamente na SEFAZ/MG**, sem provedor fiscal mensal.


## Venda de balcão com NFC-e

A venda agora pode ser enviada como **NFC-e modelo 65 diretamente para a SEFAZ/MG**.

Fluxo:

1. Salvar a venda normalmente.
2. Abrir o recibo e clicar **Finalizar venda e emitir NFC-e**.
3. O sistema monta o XML modelo 65, assina com o A1, insere o QR Code da NFC-e e envia à SEFAZ.
4. Somente após autorização grava número, série, chave, protocolo, XML autorizado e DANFE NFC-e.
5. O botão **Imprimir DANFE NFC-e na POS-5890U** envia o cupom fiscal em ESC/POS direto via QZ Tray, sem página gigante do Edge.
6. Também ficam disponíveis **Abrir DANFE NFC-e** (PDF 58 mm) e **Baixar XML**.

A numeração NFC-e é persistida em `/app/data/centralvet.db`. Não recrie o volume em redeploy.

Antes de emitir, cada produto precisa ter NCM válido. A regra de saída usa o cadastro fiscal do produto e o padrão da empresa; rejeições da SEFAZ aparecem com `cStat`/motivo na própria venda.

## Fluxo da devolução

1. Importar o XML da NF-e de compra original.
2. Selecionar produtos e quantidades.
3. O sistema detecta ST no XML e sugere CFOP por item.
4. O sistema gera a NF-e de devolução (finNFe=4), referencia a chave original, assina com o certificado A1 e envia para a SEFAZ.
5. Se a SEFAZ autorizar, o sistema salva chave/protocolo/XML autorizado, gera DANFE PDF e só então baixa o estoque.
6. Se houver rejeição, mostra `cStat` e `xMotivo`, não baixa estoque e permite corrigir/tentar novamente com a mesma numeração.

## Coolify

Build Pack: **Dockerfile**  
Porta: **8080**

Variáveis:

```env
PORT=8080
PYTHONUNBUFFERED=1
TZ=America/Sao_Paulo
SECRET_KEY=troque-por-uma-chave-forte
DATA_DIR=/app/data
ADMIN_USER=admin
ADMIN_PASSWORD=troque-a-senha
CERTIFICADO_PATH=/app/certs/centralvet_a1.pfx
CERTIFICADO_SENHA=SENHA_REAL_DO_A1
UF_EMPRESA=MG
```

Volumes persistentes:

```text
/app/data
/app/certs
```

O certificado deve existir em `/app/certs/centralvet_a1.pfx`. Não coloque o A1 nem a senha dentro do repositório/ZIP.

## Dependências fiscais

O Dockerfile instala PHP + Composer e usa:

- `nfephp-org/sped-nfe` 5.2.8
- `nfephp-org/sped-da` 1.1.6

O motor interno fica em `fiscal_engine/nfe_cli.php`.

## Primeira verificação após deploy

Abra **Fiscal > Testar conexão SEFAZ**. O retorno operacional esperado do serviço de status é `cStat 107`.

Depois, antes da primeira emissão real, confira na tela Fiscal:

- Ambiente = Produção
- Série NF-e e próximo número
- Certificado A1 encontrado
- Senha do A1 configurada


## Importante sobre redeploy e numeração

Mantenha os volumes `/app/data` e `/app/certs` ligados ao **mesmo volume persistente** no Coolify. A série e o próximo número têm valores-base no código para recuperar uma instalação vazia, mas, depois que uma NF-e real for autorizada, a sequência passa a ser atualizada em `/app/data/centralvet.db`. Trocar ou apagar esse volume pode fazer o sistema perder o histórico e a sequência local.

Antes da primeira NF-e real desta versão, use **Fiscal > Testar conexão SEFAZ**. Se o serviço estiver operacional, o retorno normal do status é `107 - Serviço em Operação`.

## Software house em Minas Gerais

A exigência específica da Portaria SRE 277/2025 para credenciamento da empresa desenvolvedora e preenchimento do Grupo ZD é aplicável ao contribuinte classificado no CNAE 4731-8 (comércio varejista de combustíveis). A CENTRALVET está cadastrada em outro CNAE, portanto essa regra específica não é ativada por essa portaria para esta empresa.

## Segurança operacional

- Em produção, a devolução só é enviada se o XML original estiver destinado ao CNPJ da CENTRALVET e tiver chave válida de 44 dígitos.
- O estoque só é baixado depois da autorização da SEFAZ.
- Uma NF-e autorizada não pode ser excluída pelo botão comum.
- XMLs originais/autorizados e DANFEs ficam em `/app/data/fiscal`, portanto esse diretório deve estar em volume persistente e entrar na rotina de backup.

## Observação fiscal

O sistema automatiza a emissão e expõe a rejeição real da SEFAZ. Regras tributárias especiais de um produto/operação podem exigir ajuste dos dados fiscais antes de um reenvio. Não force uma nota rejeitada trocando campos às cegas.


## Correção de compatibilidade sped-nfe 5.2.8
A montagem do XML usa `Make::getXML()`. A chamada antiga `Make::monta()` foi removida porque não existe na versão instalada pelo Composer.


## Compartilhamento por WhatsApp
- Após a NF-e ser autorizada, a tela mostra **Imprimir DANFE** e **Enviar DANFE pelo WhatsApp**.
- Em navegadores compatíveis com Web Share, o sistema tenta compartilhar o PDF do DANFE como arquivo.
- Como fallback, abre o WhatsApp com mensagem pronta e link assinado do DANFE.
- O link público do DANFE expira em 7 dias e só funciona para notas autorizadas pela SEFAZ.

## Correções 24/09/2026
- Compatibilidade/migração de bancos antigos para impedir queda em Relatórios.
- Histórico de devoluções mostra XML e DANFE persistidos; recuperação automática dos caminhos no volume `/app/data/fiscal/nfe`.
- XML usado diretamente em uma devolução passa a entrar também no histórico de XMLs.
- Novo cupom térmico dedicado (80 mm), sem layout/base da PWA e sem altura mínima de viewport, evitando grande área branca no fim.
- Cache do PWA incrementado para aplicar o CSS novo após redeploy.

## Recuperação automática de NF-e autorizada
Esta versão varre `/app/data/fiscal/nfe` e recria no banco devoluções autorizadas que ainda tenham XML autorizado no volume. Também contém uma recuperação específica da NF-e nº 1/série 1 fornecida pelo usuário, usando o DANFE oficial apenas para repor o histórico e a impressão caso o XML tenha se perdido. Nenhuma nota é reenviada à SEFAZ e nenhum estoque é baixado novamente durante a recuperação.

## Persistência reforçada contra redeploy
Esta versão usa `/app/data/centralvet.db` como banco principal e mantém automaticamente uma segunda cópia consistente em `/app/certs/centralvet-persistence/centralvet.db`.

No Coolify, mantenha DOIS volumes persistentes fixos:

```text
centralvet-agropecuaria-data  -> /app/data
centralvet-agropecuaria-certs -> /app/certs
```

Se `/app/data` nascer vazio após um redeploy, o sistema restaura automaticamente o banco espelho de `/app/certs` antes de inicializar as tabelas. Cadastros de produtos, clientes, fornecedores, vendas, financeiro e devoluções deixam de depender do filesystem efêmero do container.

Não renomeie nem recrie esses volumes entre redeploys. Um redeploy normal deve reutilizar exatamente os mesmos volumes.

## POS-5890U - impressão direta ESC/POS
Esta versão não depende do tamanho de página do Edge para a impressora térmica.
A POS-5890U trabalha com papel 57,5/58 mm e 384 dots por linha. O recibo é enviado em comandos ESC/POS raw via QZ Tray instalado no notebook do caixa.

Passos no notebook do caixa:
1. Instalar QZ Tray (gratuito) em https://qz.io/download/
2. Manter QZ Tray aberto/iniciando com o Windows.
3. No recibo, clicar em "Conectar impressora" e permitir acesso.
4. O sistema procura primeiro a fila "POS-58", depois nomes contendo POS-5890U/POS-58/5890.
5. Usar "Imprimir direto POS-5890U". O Edge não abre a prévia de página; os bytes vão direto para a impressora.

O PDF 58 mm continua disponível apenas como reserva.
