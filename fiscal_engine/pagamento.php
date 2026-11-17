<?php
declare(strict_types=1);

/** Detalhe do pagamento da NFC-e; pagamentos manuais não têm integração TEF. */
function centralvetPagamento(array $p, float $total): stdClass
{
    $tpag = trim((string)($p['tPag'] ?? '01')) ?: '01';
    $eletronico = in_array($tpag, ['03', '04', '17'], true);
    $dados = $eletronico ? ($p['payment_details'] ?? []) : [];
    return (object)[
        'indPag' => (int)($p['indPag'] ?? 0),
        'tPag' => $tpag,
        'vPag' => round((float)($p['valor_pago'] ?? $total), 2),
        'xPag' => trim((string)($p['xPag'] ?? '')) ?: null,
        'dPag' => null,
        // A presença de tpIntegra faz NFePHP gerar <card>, inclusive para PIX.
        'tpIntegra' => $eletronico ? 2 : null,
        'CNPJ' => $dados['CNPJ'] ?? null,
        'tBand' => in_array($tpag, ['03', '04'], true) ? ($dados['tBand'] ?? null) : null,
        'cAut' => $dados['cAut'] ?? null,
        'CNPJPag' => null,
        'UFPag' => null,
        'CNPJReceb' => $dados['CNPJReceb'] ?? null,
        'idTermPag' => $dados['idTermPag'] ?? null,
    ];
}
