# CENTRALVET Agropecuária - Sistema Veltrix

Primeira base funcional para gestão de agropecuária com vendas, estoque, financeiro, recibos, configuração fiscal e importação de XML de nota de entrada.

## Login inicial

- Usuário: `admin`
- Senha: `1234`

Pode alterar pelas variáveis de ambiente `ADMIN_USER` e `ADMIN_PASSWORD`.

## Deploy no Coolify

Use Build Pack: **Dockerfile**

Porta: `8080`

Variáveis:

```env
PORT=8080
PYTHONUNBUFFERED=1
TZ=America/Sao_Paulo
SECRET_KEY=centralvet-veltrix-2026
DATA_DIR=/app/data
ADMIN_USER=admin
ADMIN_PASSWORD=1234
AMBIENTE_FISCAL=homologacao
UF_EMPRESA=MG
CERTIFICADO_PATH=/app/certs/centralvet_a1.pfx
CERTIFICADO_SENHA=COLOCAR_A_SENHA_NO_COOLIFY
```

Storage principal do banco:

```text
Tipo: Volume Mount
Volume Name: centralvet-agropecuaria-data
Destination Path: /app/data
```

Certificado digital:

```text
NÃO coloque o certificado dentro do ZIP.
Coloque o arquivo A1 em uma pasta/volume privado do servidor, por exemplo:
/app/certs/centralvet_a1.pfx
```

## Novidades desta versão fiscal

- Tela Fiscal com dados da empresa.
- Campo para ambiente Homologação/Produção.
- Campos de série e número inicial NF-e/NFC-e.
- Campos CSC/Token NFC-e.
- Diagnóstico básico se o certificado e senha estão configurados via servidor.
- Tela Importar XML.
- Importação de XML de nota de entrada.
- Cadastro automático de fornecedor pelo XML.
- Cadastro automático de produtos pelo XML.
- Atualização de produto existente por código/EAN/nome.
- Lançamento automático de estoque pela nota.
- Histórico de XMLs importados.
- Bloqueio de importação duplicada por chave/hash do XML.
- Produtos agora têm EAN e CEST.

## Fiscal real

Esta versão deixa a estrutura pronta e já resolve a entrada por XML. A autorização real da NF-e/NFC-e precisa ser validada com certificado, CSC/Token, credenciamento, regime tributário, séries, numeração e regras fiscais dos produtos.


## Atualização fiscal: Devolução por XML

Esta versão inclui o módulo de devolução de mercadoria:

1. Acesse **Fiscal NF-e/NFC-e > Nota de devolução** ou **Devoluções > Nova devolução**.
2. Importe o XML da nota fiscal de compra original.
3. Selecione os produtos avariados e informe as quantidades a devolver.
4. Gere o rascunho da devolução.
5. Valide CFOP/CST/CSOSN com o contador antes de emitir em produção.

A tela cria o rascunho vinculado à chave da NF-e original e permite baixar o estoque se necessário.

## Versão modo fiscal acelerado

Esta versão adiciona um botão em Fiscal NF-e/NFC-e chamado **Preencher básico p/ teste**.

Ele completa campos provisórios para homologação quando a contabilidade demora a responder:
- Série NF-e: 1
- Número inicial NF-e: 1
- Série NFC-e: 1
- Número inicial NFC-e: 1
- CFOP padrão provisório: 5102
- CST/CSOSN padrão provisório: 102
- Ambiente: Homologação

Importante: esses dados são para teste/homologação. O sistema mantém produção bloqueada se faltar CSC/Token, séries, CFOP/CST/CSOSN, certificado ou senha.

## Atualização fiscal com documentos da CENTRALVET
Esta versão já vem com preenchimento dos dados cadastrais da empresa conforme documentos enviados:
- Razão social: CENTRALVET AGROPECUARIA LTDA
- Nome fantasia: CENTRALVET AGROPECUARIA
- CNPJ: 68.690.225/0001-50
- Inscrição Estadual: 005626088.00-49
- Regime: SIMPLES NACIONAL
- Endereço: R MANOEL FRANCISCO DE CASTRO, 21, B, CENTRO, ORIZANIA/MG, CEP 36.828-000

Na tela Fiscal existe o botão “Preencher dados da empresa”. Ele restaura esses dados e mantém o ambiente em Homologação. Produção continua dependendo de CSC/Token, séries, CFOP/CST/CSOSN e regras fiscais corretas.

## Atualização: assistente de CFOP para devolução

Na tela **Devoluções > Nova devolução**, depois de importar o XML da compra, o sistema agora permite escolher:

- Mesmo estado (MG) ou outro estado
- Sem substituição tributária ou com substituição tributária

Com isso, o sistema sugere automaticamente:

- 5202: mesmo estado, sem ST
- 6202: outro estado, sem ST
- 5411: mesmo estado, com ST
- 6411: outro estado, com ST

O campo continua editável por produto para exceções fiscais.


## Instalar como aplicativo no Microsoft Edge

Esta versão foi configurada como PWA. Depois do deploy:

1. Abra o sistema no Microsoft Edge pelo notebook.
2. Clique nos três pontinhos do Edge.
3. Vá em **Aplicativos**.
4. Clique em **Instalar este site como aplicativo**.
5. Abra pelo atalho criado na área de trabalho ou menu Iniciar.

O app abre sem a barra normal do navegador, com visual de aplicativo e manifestação em modo tela cheia/standalone.

## Atualização - CFOP de devolução automático por XML
- Ao importar o XML da nota de compra, o sistema verifica UF do fornecedor e sinais de ICMS-ST por produto.
- Se o produto vier sem ST, sugere 5202 para MG ou 6202 para outro estado.
- Se o produto vier com ST, sugere 5411 para MG ou 6411 para outro estado.
- O campo continua editável por produto para exceções fiscais.


## Fluxo simplificado de devolução
- Sem status de rascunho/emitida pelo contador/cancelada na interface.
- Botão único: Emitir devolução e imprimir.
- Após salvar, abre automaticamente a tela de impressão.
- CFOP continua sugerido automaticamente pelo XML e pode ser ajustado antes da emissão.
