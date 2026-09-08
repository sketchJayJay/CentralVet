# CENTRALVET Agropecuária - Sistema Veltrix

Primeira versão funcional para Coolify.

## Variáveis
PORT=8080
PYTHONUNBUFFERED=1
TZ=America/Sao_Paulo
SECRET_KEY=centralvet-veltrix-2026
DATA_DIR=/app/data
ADMIN_USER=admin
ADMIN_PASSWORD=1234

## Storage
Volume Mount
Destination Path: /app/data
Sugestão de Volume Name: centralvet-agropecuaria-data

## Login inicial
Usuário: admin
Senha: 1234

## Observação fiscal
A estrutura de NF-e/NFC-e está preparada na interface. A autorização fiscal real depende de certificado digital, dados da empresa, ambiente fiscal e validação com contador.
