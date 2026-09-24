<?php
// Motor fiscal interno da CENTRALVET: NF-e modelo 55 direto com SEFAZ via NFePHP.
// Entrada: JSON no STDIN. Saída: um único JSON no STDOUT.
declare(strict_types=1);

ini_set('display_errors', '0');
error_reporting(E_ALL);
date_default_timezone_set('America/Sao_Paulo');

$autoload = dirname(__DIR__) . '/vendor/autoload.php';
if (!file_exists($autoload)) {
    echo json_encode([
        'ok' => false,
        'authorized' => false,
        'error' => 'Dependências fiscais não instaladas. Rode o deploy pelo Dockerfile desta versão (Composer/NFePHP).'
    ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit(2);
}
require_once $autoload;

use NFePHP\Common\Certificate;
use NFePHP\NFe\Common\Standardize;
use NFePHP\NFe\Complements;
use NFePHP\NFe\Make;
use NFePHP\NFe\Tools;
use NFePHP\DA\NFe\Danfe;

function out(array $data, int $code = 0): void {
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit($code);
}

function digits($v): string {
    return preg_replace('/\D+/', '', (string)($v ?? '')) ?? '';
}

function s($v): string {
    return trim((string)($v ?? ''));
}

function f($v): float {
    if ($v === null || $v === '') return 0.0;
    return (float)str_replace(',', '.', (string)$v);
}

function valOrNull(float $v): ?float {
    return abs($v) > 0.0000001 ? round($v, 2) : null;
}

function textOf(?DOMNode $ctx, string $local): string {
    if (!$ctx) return '';
    $doc = $ctx instanceof DOMDocument ? $ctx : $ctx->ownerDocument;
    $xp = new DOMXPath($doc);
    $nodes = $xp->query('.//*[local-name()="' . $local . '"]', $ctx);
    if ($nodes && $nodes->length) return trim((string)$nodes->item(0)->textContent);
    return '';
}

function directTextOf(?DOMNode $ctx, string $local): string {
    if (!$ctx) return '';
    foreach ($ctx->childNodes as $child) {
        if ($child instanceof DOMElement && $child->localName === $local) {
            return trim((string)$child->textContent);
        }
    }
    return '';
}

function firstNode(DOMXPath $xp, string $query, ?DOMNode $ctx = null): ?DOMNode {
    $nodes = $xp->query($query, $ctx);
    return ($nodes && $nodes->length) ? $nodes->item(0) : null;
}

function proportionalFromDet(?DOMNode $det, string $tag, float $ratio): float {
    if (!$det) return 0.0;
    return round(f(textOf($det, $tag)) * $ratio, 2);
}

function originalPercent(?DOMNode $det, string $tag): ?float {
    if (!$det) return null;
    $txt = textOf($det, $tag);
    return $txt === '' ? null : (float)$txt;
}

function makeTools(array $p): Tools {
    $issuer = $p['issuer'] ?? [];
    $tpAmb = (int)($p['tpAmb'] ?? 2);
    $certPath = s($p['cert_path'] ?? '');
    $certPass = (string)($p['cert_password'] ?? '');
    if (!$certPath || !is_file($certPath)) {
        throw new RuntimeException('Certificado A1 não encontrado em ' . ($certPath ?: '(caminho vazio)'));
    }
    if ($certPass === '') {
        throw new RuntimeException('Senha do certificado A1 não configurada em CERTIFICADO_SENHA.');
    }
    $config = [
        'atualizacao' => date('Y-m-d H:i:s'),
        'tpAmb' => $tpAmb,
        'razaosocial' => s($issuer['razao_social'] ?? ''),
        'cnpj' => digits($issuer['cnpj'] ?? ''),
        'ie' => digits($issuer['inscricao_estadual'] ?? ''),
        'siglaUF' => strtoupper(s($issuer['uf'] ?? 'MG')),
        'schemes' => 'PL_010_V1',
        'versao' => '4.00',
        'tokenIBPT' => '',
        'CSC' => s($issuer['csc_token'] ?? ''),
        'CSCid' => s($issuer['csc_id'] ?? ''),
        'proxyConf' => [
            'proxyIp' => '', 'proxyPort' => '', 'proxyUser' => '', 'proxyPass' => ''
        ],
        'aProxyConf' => [
            'proxyIp' => '', 'proxyPort' => '', 'proxyUser' => '', 'proxyPass' => ''
        ]
    ];
    $cert = Certificate::readPfx(file_get_contents($certPath), $certPass);
    $tools = new Tools(json_encode($config, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), $cert);
    $tools->model('55');
    if (method_exists($tools, 'setVerAplic')) {
        $tools->setVerAplic('CENTRALVET-1.0');
    }
    return $tools;
}

function parseOriginal(string $path): array {
    if (!$path || !is_file($path)) {
        throw new RuntimeException('XML original não encontrado no servidor. Importe novamente a nota de compra.');
    }
    $dom = new DOMDocument();
    $dom->preserveWhiteSpace = false;
    if (!$dom->load($path)) throw new RuntimeException('Não foi possível abrir o XML original.');
    $xp = new DOMXPath($dom);
    $emit = firstNode($xp, '//*[local-name()="infNFe"]/*[local-name()="emit"]');
    $ender = firstNode($xp, '//*[local-name()="infNFe"]/*[local-name()="emit"]/*[local-name()="enderEmit"]');
    $ide = firstNode($xp, '//*[local-name()="infNFe"]/*[local-name()="ide"]');
    $inf = firstNode($xp, '//*[local-name()="infNFe"]');
    if (!$emit || !$ender || !$ide || !$inf) throw new RuntimeException('XML original incompleto: emitente/endereço/identificação não encontrados.');
    $id = $inf instanceof DOMElement ? (string)$inf->getAttribute('Id') : '';
    return [
        'dom' => $dom,
        'xp' => $xp,
        'chave' => preg_replace('/^NFe/', '', $id),
        'emit' => $emit,
        'ender' => $ender,
        'ide' => $ide,
    ];
}

try {
    $raw = stream_get_contents(STDIN);
    $p = json_decode($raw ?: '{}', true, 512, JSON_THROW_ON_ERROR);
    $action = s($p['action'] ?? '');
    if (!$action) throw new RuntimeException('Ação fiscal não informada.');

    if ($action === 'status') {
        $tools = makeTools($p);
        $uf = strtoupper(s(($p['issuer']['uf'] ?? 'MG')));
        $tpAmb = (int)($p['tpAmb'] ?? 2);
        $resp = $tools->sefazStatus($uf, $tpAmb);
        $std = (new Standardize($resp))->toStd();
        out([
            'ok' => true,
            'cStat' => (string)($std->cStat ?? ''),
            'xMotivo' => (string)($std->xMotivo ?? ''),
            'tpAmb' => (string)($std->tpAmb ?? $tpAmb),
            'verAplic' => (string)($std->verAplic ?? '')
        ]);
    }

    if ($action !== 'emit_return') throw new RuntimeException('Ação fiscal desconhecida.');

    $issuer = $p['issuer'] ?? [];
    $tpAmb = (int)($p['tpAmb'] ?? 2);
    $original = parseOriginal(s($p['original_xml_path'] ?? ''));
    $xp = $original['xp'];
    $emitOrig = $original['emit'];
    $enderOrig = $original['ender'];
    $origKey = s($p['chave_origem'] ?? $original['chave']);
    if (strlen(digits($origKey)) !== 44) throw new RuntimeException('Chave da NF-e original inválida ou ausente.');
    $origKey = digits($origKey);

    // Destinatário da devolução = fornecedor/emitente da compra original.
    $destCnpj = digits(directTextOf($emitOrig, 'CNPJ'));
    $destCpf = digits(directTextOf($emitOrig, 'CPF'));
    $destIE = s(directTextOf($emitOrig, 'IE'));
    $destNome = s(directTextOf($emitOrig, 'xNome')) ?: s(directTextOf($emitOrig, 'xFant'));
    $destUF = strtoupper(s(directTextOf($enderOrig, 'UF')));
    $destCMun = digits(directTextOf($enderOrig, 'cMun'));
    if (!$destNome || (!$destCnpj && !$destCpf) || !$destUF || strlen($destCMun) !== 7) {
        throw new RuntimeException('XML original não possui dados completos do fornecedor para montar a devolução.');
    }

    $items = $p['items'] ?? [];
    if (!is_array($items) || count($items) < 1) throw new RuntimeException('Nenhum item selecionado para devolução.');

    $schema = 'PL_010_V1';
    $mk = new Make($schema);
    $mk->setOnlyAscii(false);
    $mk->setCheckGtin(false);

    $inf = new stdClass();
    $inf->versao = '4.00';
    $inf->Id = null;
    $inf->pk_nItem = null;
    $mk->taginfNFe($inf);

    $issueIso = s($p['issue_datetime'] ?? '');
    if (!$issueIso) $issueIso = (new DateTime('now', new DateTimeZone('America/Sao_Paulo')))->format('c');
    $sameState = ($destUF === strtoupper(s($issuer['uf'] ?? 'MG')));
    $ide = (object)[
        'cUF' => 31,
        'cNF' => s($p['cNF'] ?? '') ?: null,
        'natOp' => 'DEVOLUCAO DE COMPRA',
        'mod' => 55,
        'serie' => (int)($p['serie'] ?? 1),
        'nNF' => (int)($p['nNF'] ?? 1),
        'dhEmi' => $issueIso,
        'dhSaiEnt' => null,
        'dPrevEntrega' => null,
        'tpNF' => 1,
        'idDest' => $sameState ? 1 : 2,
        'cMunFG' => (int)digits($issuer['codigo_municipio'] ?? '3145877'),
        'cMunFGIBS' => null,
        'tpImp' => 1,
        'tpEmis' => 1,
        'cDV' => null,
        'tpAmb' => $tpAmb,
        'finNFe' => 4,
        'tpNFDebito' => null,
        'tpNFCredito' => null,
        'indFinal' => 0,
        'indPres' => 9,
        'indIntermed' => 0,
        'procEmi' => 0,
        'verProc' => 'CENTRALVET-1.0',
        'dhCont' => null,
        'xJust' => null,
    ];
    $mk->tagide($ide);

    $emit = (object)[
        'xNome' => s($issuer['razao_social'] ?? ''),
        'xFant' => s($issuer['nome_fantasia'] ?? ''),
        'IE' => digits($issuer['inscricao_estadual'] ?? ''),
        'IEST' => null,
        'IM' => null,
        'CNAE' => digits($issuer['cnae'] ?? '4683400'),
        'CRT' => (int)($issuer['crt'] ?? 1),
        'CNPJ' => digits($issuer['cnpj'] ?? ''),
        'CPF' => null,
    ];
    if (strlen($emit->CNPJ) !== 14 || !$emit->IE || !$emit->xNome) throw new RuntimeException('Dados do emitente incompletos na Configuração Fiscal.');
    $mk->tagEmit($emit);

    $enderEmit = (object)[
        'xLgr' => s($issuer['logradouro'] ?? 'R MANOEL FRANCISCO DE CASTRO'),
        'nro' => s($issuer['numero'] ?? '21'),
        'xCpl' => s($issuer['complemento'] ?? 'B') ?: null,
        'xBairro' => s($issuer['bairro'] ?? 'CENTRO'),
        'cMun' => (int)digits($issuer['codigo_municipio'] ?? '3145877'),
        'xMun' => s($issuer['municipio'] ?? 'ORIZANIA'),
        'UF' => strtoupper(s($issuer['uf'] ?? 'MG')),
        'CEP' => digits($issuer['cep'] ?? ''),
        'cPais' => 1058,
        'xPais' => 'BRASIL',
        'fone' => digits($issuer['telefone'] ?? '') ?: null,
    ];
    $mk->tagenderEmit($enderEmit);
    $mk->tagrefNFe((object)['refNFe' => $origKey]);

    $indIEDest = 9;
    if (strtoupper($destIE) === 'ISENTO') $indIEDest = 2;
    elseif ($destIE !== '') $indIEDest = 1;
    $dest = (object)[
        'xNome' => $destNome,
        'CNPJ' => $destCnpj ?: null,
        'CPF' => $destCpf ?: null,
        'idEstrangeiro' => null,
        'indIEDest' => $indIEDest,
        'IE' => $indIEDest === 1 ? digits($destIE) : ($indIEDest === 2 ? 'ISENTO' : null),
        'ISUF' => null,
        'IM' => null,
        'email' => s(directTextOf($emitOrig, 'email')) ?: null,
    ];
    $mk->tagdest($dest);
    $enderDest = (object)[
        'xLgr' => s(directTextOf($enderOrig, 'xLgr')),
        'nro' => s(directTextOf($enderOrig, 'nro')) ?: 'S/N',
        'xCpl' => s(directTextOf($enderOrig, 'xCpl')) ?: null,
        'xBairro' => s(directTextOf($enderOrig, 'xBairro')) ?: 'CENTRO',
        'cMun' => (int)$destCMun,
        'xMun' => s(directTextOf($enderOrig, 'xMun')),
        'UF' => $destUF,
        'CEP' => digits(directTextOf($enderOrig, 'CEP')) ?: null,
        'cPais' => (int)(digits(directTextOf($enderOrig, 'cPais')) ?: 1058),
        'xPais' => s(directTextOf($enderOrig, 'xPais')) ?: 'BRASIL',
        'fone' => digits(directTextOf($enderOrig, 'fone')) ?: null,
    ];
    if (!$enderDest->xLgr || !$enderDest->xMun) throw new RuntimeException('Endereço do fornecedor incompleto no XML original.');
    $mk->tagenderdest($enderDest);

    $seq = 0;
    foreach ($items as $it) {
        $seq++;
        $originIndex = (int)($it['item_origem_idx'] ?? $seq);
        $det = firstNode($xp, '//*[local-name()="infNFe"]/*[local-name()="det" and @nItem="' . $originIndex . '"]');
        if (!$det) throw new RuntimeException("Item {$originIndex} não foi localizado no XML original.");
        $prodOrig = firstNode($xp, './*[local-name()="prod"]', $det);
        if (!$prodOrig) throw new RuntimeException("Produto do item {$originIndex} não encontrado no XML original.");

        $qOriginal = f($it['quantidade_original'] ?? directTextOf($prodOrig, 'qCom'));
        $qDev = f($it['quantidade_devolver'] ?? 0);
        if ($qOriginal <= 0 || $qDev <= 0 || $qDev > $qOriginal + 0.000001) {
            throw new RuntimeException("Quantidade inválida no item {$originIndex}.");
        }
        $ratio = min(1.0, $qDev / $qOriginal);
        $vUn = f($it['valor_unitario'] ?? directTextOf($prodOrig, 'vUnCom'));
        $vProd = round($qDev * $vUn, 2);
        $ncm = digits($it['ncm'] ?? directTextOf($prodOrig, 'NCM'));
        if (!(strlen($ncm) === 8 || strlen($ncm) === 2)) throw new RuntimeException("NCM inválido no item {$originIndex}.");
        $ean = digits($it['ean'] ?? directTextOf($prodOrig, 'cEAN'));
        if (!in_array(strlen($ean), [8, 12, 13, 14], true)) $ean = 'SEM GTIN';
        $eanTrib = digits(directTextOf($prodOrig, 'cEANTrib'));
        if (!in_array(strlen($eanTrib), [8, 12, 13, 14], true)) $eanTrib = $ean;
        $cest = digits($it['cest'] ?? directTextOf($prodOrig, 'CEST'));
        if (strlen($cest) !== 7) $cest = null;

        $vFrete = proportionalFromDet($det, 'vFrete', $ratio);
        $vSeg = proportionalFromDet($det, 'vSeg', $ratio);
        $vDesc = proportionalFromDet($det, 'vDesc', $ratio);
        $vOutro = proportionalFromDet($det, 'vOutro', $ratio);

        $prod = new stdClass();
        $prod->item = $seq;
        $prod->cProd = s($it['codigo'] ?? directTextOf($prodOrig, 'cProd')) ?: (string)$originIndex;
        $prod->cEAN = $ean;
        $prod->cBarra = null;
        $prod->xProd = s($it['nome'] ?? directTextOf($prodOrig, 'xProd'));
        $prod->NCM = $ncm;
        $prod->CEST = $cest;
        $prod->indEscala = null;
        $prod->CNPJFab = null;
        $prod->cBenef = s(directTextOf($prodOrig, 'cBenef')) ?: null;
        $prod->tpCredPresIBSZFM = null;
        $prod->EXTIPI = s(directTextOf($prodOrig, 'EXTIPI')) ?: null;
        $prod->CFOP = digits($it['cfop_devolucao'] ?? '');
        $prod->uCom = s($it['unidade'] ?? directTextOf($prodOrig, 'uCom')) ?: 'UN';
        $prod->qCom = $qDev;
        $prod->vUnCom = $vUn;
        $prod->vProd = $vProd;
        $prod->cEANTrib = $eanTrib;
        $prod->uTrib = s(directTextOf($prodOrig, 'uTrib')) ?: $prod->uCom;
        $qTribOrig = f(directTextOf($prodOrig, 'qTrib'));
        $prod->qTrib = $qTribOrig > 0 ? round($qTribOrig * $ratio, 4) : $qDev;
        $vUnTribOrig = f(directTextOf($prodOrig, 'vUnTrib'));
        $prod->vUnTrib = $vUnTribOrig > 0 ? $vUnTribOrig : $vUn;
        $prod->vFrete = valOrNull($vFrete);
        $prod->vSeg = valOrNull($vSeg);
        $prod->vDesc = valOrNull($vDesc);
        $prod->vOutro = valOrNull($vOutro);
        $prod->indTot = 1;
        $prod->indBemMovelUsado = null;
        $prod->xPed = null;
        $prod->nItemPed = null;
        $prod->nFCI = null;
        $prod->vItem = null;
        if (!$prod->CFOP || strlen($prod->CFOP) !== 4) throw new RuntimeException("CFOP inválido no item {$originIndex}.");
        $mk->tagprod($prod);

        $imp = new stdClass();
        $imp->item = $seq;
        $imp->vTotTrib = null;
        $mk->tagimposto($imp);

        // CENTRALVET é Simples Nacional. Em devolução usamos CSOSN 900 e reproduzimos,
        // proporcionalmente, bases/valores de ICMS existentes na nota original.
        $origMerc = textOf($det, 'orig');
        if ($origMerc === '' || !preg_match('/^[0-8]$/', $origMerc)) $origMerc = '0';
        $vBC = proportionalFromDet($det, 'vBC', $ratio);
        $vICMS = proportionalFromDet($det, 'vICMS', $ratio);
        $vBCST = proportionalFromDet($det, 'vBCST', $ratio);
        $vICMSST = proportionalFromDet($det, 'vICMSST', $ratio);
        $vBCSTRet = proportionalFromDet($det, 'vBCSTRet', $ratio);
        $vICMSSTRet = proportionalFromDet($det, 'vICMSSTRet', $ratio);
        $vBCFCPST = proportionalFromDet($det, 'vBCFCPST', $ratio);
        $vFCPST = proportionalFromDet($det, 'vFCPST', $ratio);
        $vBCFCPSTRet = proportionalFromDet($det, 'vBCFCPSTRet', $ratio);
        $vFCPSTRet = proportionalFromDet($det, 'vFCPSTRet', $ratio);
        $vICMSSubstituto = proportionalFromDet($det, 'vICMSSubstituto', $ratio);

        $ic = new stdClass();
        $ic->item = $seq;
        $ic->orig = $origMerc;
        $ic->CSOSN = '900';
        $ic->pCredSN = null;
        $ic->vCredICMSSN = null;
        $ic->modBCST = $vBCST > 0 ? 6 : null;
        $ic->pMVAST = null;
        $ic->pRedBCST = null;
        $ic->vBCST = valOrNull($vBCST);
        $ic->pICMSST = $vBCST > 0 ? originalPercent($det, 'pICMSST') : null;
        $ic->vICMSST = valOrNull($vICMSST);
        $ic->vBCFCPST = valOrNull($vBCFCPST);
        $ic->pFCPST = $vBCFCPST > 0 ? originalPercent($det, 'pFCPST') : null;
        $ic->vFCPST = valOrNull($vFCPST);
        $ic->vBCSTRet = valOrNull($vBCSTRet);
        $ic->pST = $vBCSTRet > 0 ? originalPercent($det, 'pST') : null;
        $ic->vICMSSTRet = valOrNull($vICMSSTRet);
        $ic->vBCFCPSTRet = valOrNull($vBCFCPSTRet);
        $ic->pFCPSTRet = $vBCFCPSTRet > 0 ? originalPercent($det, 'pFCPSTRet') : null;
        $ic->vFCPSTRet = valOrNull($vFCPSTRet);
        $ic->modBC = $vBC > 0 ? 3 : null;
        $ic->vBC = valOrNull($vBC);
        $ic->pRedBC = null;
        $ic->pICMS = $vBC > 0 ? originalPercent($det, 'pICMS') : null;
        $ic->vICMS = valOrNull($vICMS);
        $ic->pRedBCEfet = null;
        $ic->vBCEfet = null;
        $ic->pICMSEfet = null;
        $ic->vICMSEfet = null;
        $ic->vICMSSubstituto = valOrNull($vICMSSubstituto);
        $mk->tagICMSSN($ic);

        $pis = new stdClass();
        $pis->item = $seq;
        $pis->CST = '49';
        $pis->vBC = 0.00;
        $pis->pPIS = 0.00;
        $pis->vPIS = 0.00;
        $pis->qBCProd = null;
        $pis->vAliqProd = null;
        $mk->tagPIS($pis);

        $cof = new stdClass();
        $cof->item = $seq;
        $cof->CST = '49';
        $cof->vBC = 0.00;
        $cof->pCOFINS = 0.00;
        $cof->vCOFINS = 0.00;
        $cof->qBCProd = null;
        $cof->vAliqProd = null;
        $mk->tagCOFINS($cof);

        $vIPIOrig = f(textOf($det, 'vIPI'));
        if ($vIPIOrig > 0) {
            $ipiDev = new stdClass();
            $ipiDev->item = $seq;
            $ipiDev->pDevol = round($ratio * 100, 2);
            $ipiDev->vIPIDevol = round($vIPIOrig * $ratio, 2);
            $mk->tagimpostoDevol($ipiDev);
        }
    }

    // A Make calcula os totais a partir dos itens quando os campos não são passados.
    $mk->tagICMSTot(new stdClass());
    $mk->tagtransp((object)['modFrete' => 9]);
    $mk->tagpag((object)['vTroco' => null]);
    $mk->tagdetpag((object)[
        'indPag' => 0,
        'tPag' => '90',
        'vPag' => 0.00,
        'xPag' => null,
        'dPag' => null,
        'tpIntegra' => null,
        'CNPJPag' => null,
        'UFPag' => null,
        'CNPJReceb' => null,
        'idTermPag' => null,
    ]);
    $motivo = s($p['motivo'] ?? 'Devolução de mercadoria');
    $mk->taginfadic((object)[
        'infAdFisco' => null,
        'infCpl' => 'NF-e de devolução referente à NF-e ' . $origKey . '. Motivo: ' . $motivo,
    ]);

    $mk->monta();
    $makeErrors = $mk->getErrors();
    if (!empty($makeErrors)) {
        throw new RuntimeException('Erro ao montar a NF-e: ' . implode(' | ', array_slice($makeErrors, 0, 8)));
    }
    $xml = $mk->getXML();
    if (!$xml) throw new RuntimeException('Falha ao montar o XML da NF-e.');

    $tools = makeTools($p);
    $signed = $tools->signNFe($xml);
    $signedDom = new DOMDocument();
    $signedDom->loadXML($signed);
    $signedXp = new DOMXPath($signedDom);
    $signedInf = firstNode($signedXp, '//*[local-name()="infNFe"]');
    $chaveGerada = '';
    if ($signedInf instanceof DOMElement) $chaveGerada = preg_replace('/^NFe/', '', (string)$signedInf->getAttribute('Id'));

    $outputDir = s($p['output_dir'] ?? '');
    if (!$outputDir) throw new RuntimeException('Diretório fiscal de saída não informado.');
    if (!is_dir($outputDir) && !mkdir($outputDir, 0770, true) && !is_dir($outputDir)) {
        throw new RuntimeException('Não foi possível criar o diretório fiscal de saída.');
    }
    $baseName = 'NFe-' . ((int)($p['serie'] ?? 1)) . '-' . ((int)($p['nNF'] ?? 1));
    $signedPath = $outputDir . '/' . $baseName . '-assinado.xml';
    file_put_contents($signedPath, $signed);

    $idLoteSeed = digits((string)($p['lote_id'] ?? ''));
    if (!$idLoteSeed) $idLoteSeed = (string)time();
    $idLote = str_pad(substr($idLoteSeed, -15), 15, '0', STR_PAD_LEFT);
    $response = $tools->sefazEnviaLote([$signed], $idLote, 1);
    $respObj = (new Standardize($response))->toStd();

    $loteCstat = (string)($respObj->cStat ?? '');
    $loteMotivo = (string)($respObj->xMotivo ?? '');

    // Algumas SEFAZ podem retornar lote recebido (103) mesmo quando solicitamos processamento síncrono.
    // Nesse caso consultamos o recibo antes de decidir se houve autorização/rejeição.
    if ($loteCstat === '103' && isset($respObj->infRec->nRec)) {
        $response = $tools->sefazConsultaRecibo((string)$respObj->infRec->nRec);
        $respObj = (new Standardize($response))->toStd();
        $loteCstat = (string)($respObj->cStat ?? '');
        $loteMotivo = (string)($respObj->xMotivo ?? '');
    }

    if ($loteCstat !== '104') {
        out([
            'ok' => true,
            'authorized' => false,
            'cStat' => $loteCstat,
            'xMotivo' => $loteMotivo ?: 'Lote não processado pela SEFAZ.',
            'chave' => $chaveGerada,
            'signed_xml_path' => $signedPath,
            'raw_response' => $response,
        ]);
    }

    $prot = $respObj->protNFe->infProt ?? null;
    $cStat = (string)($prot->cStat ?? '');
    $xMotivo = (string)($prot->xMotivo ?? '');
    $chave = (string)($prot->chNFe ?? $chaveGerada);
    $nProt = (string)($prot->nProt ?? '');
    $authorized = in_array($cStat, ['100', '150'], true);

    // Se a conexão caiu depois da autorização, uma nova tentativa pode voltar como duplicidade.
    // Consultamos a própria chave e recuperamos o protocolo já autorizado em vez de criar outra numeração.
    if (!$authorized && $cStat === '204' && strlen(digits($chaveGerada)) === 44) {
        $consulta = $tools->sefazConsultaChave($chaveGerada);
        $consObj = (new Standardize($consulta))->toStd();
        $consCStat = (string)($consObj->cStat ?? '');
        if (in_array($consCStat, ['100', '150'], true)) {
            $response = $consulta;
            $authorized = true;
            $cStat = $consCStat;
            $xMotivo = (string)($consObj->xMotivo ?? 'Autorizada');
            $chave = (string)($consObj->chNFe ?? $chaveGerada);
            $consProt = $consObj->protNFe->infProt ?? null;
            if ($consProt) {
                $cStat = (string)($consProt->cStat ?? $cStat);
                $xMotivo = (string)($consProt->xMotivo ?? $xMotivo);
                $chave = (string)($consProt->chNFe ?? $chave);
                $nProt = (string)($consProt->nProt ?? '');
            }
        }
    }

    if (!$authorized) {
        out([
            'ok' => true,
            'authorized' => false,
            'cStat' => $cStat,
            'xMotivo' => $xMotivo ?: 'NF-e rejeitada pela SEFAZ.',
            'chave' => $chave,
            'signed_xml_path' => $signedPath,
            'raw_response' => $response,
        ]);
    }

    $authorizedXml = Complements::toAuthorize($signed, $response);
    $xmlPath = $outputDir . '/' . $baseName . '-autorizado.xml';
    file_put_contents($xmlPath, $authorizedXml);

    $danfe = new Danfe($authorizedXml);
    $pdf = $danfe->render();
    $pdfPath = $outputDir . '/' . $baseName . '-DANFE.pdf';
    file_put_contents($pdfPath, $pdf);

    out([
        'ok' => true,
        'authorized' => true,
        'cStat' => $cStat,
        'xMotivo' => $xMotivo,
        'chave' => $chave,
        'protocolo' => $nProt,
        'xml_path' => $xmlPath,
        'danfe_path' => $pdfPath,
        'signed_xml_path' => $signedPath,
        'tpAmb' => $tpAmb,
    ]);
} catch (Throwable $e) {
    out([
        'ok' => false,
        'authorized' => false,
        'error' => $e->getMessage(),
        'type' => get_class($e),
    ], 1);
}
