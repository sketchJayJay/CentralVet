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
