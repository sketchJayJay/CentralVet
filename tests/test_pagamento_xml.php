<?php
// Teste offline com o mesmo NFePHP do deploy. Não utiliza certificado/SEFAZ.
declare(strict_types=1);
$autoload = getenv('NFE_VENDOR_AUTOLOAD') ?: dirname(__DIR__) . '/vendor/autoload.php';
require $autoload;
require dirname(__DIR__) . '/fiscal_engine/pagamento.php';

function conferir(bool $condicao, string $mensagem): void
{
    if (!$condicao) throw new RuntimeException($mensagem);
}

// Usa a definição real de <pag> e os tipos do schema distribuído com NFePHP.
$xsdOriginal = dirname($autoload) . '/nfephp-org/sped-nfe/schemes/PL_010_V1/leiauteNFe_v4.00.xsd';
$original = new DOMDocument();
$original->load($xsdOriginal);
$xpath = new DOMXPath($original);
$xpath->registerNamespace('xs', 'http://www.w3.org/2001/XMLSchema');
$pag = $xpath->query('//xs:element[@name="pag"]')->item(0);
conferir($pag instanceof DOMElement, 'Definição de pagamento não encontrada no XSD.');
$schema = new DOMDocument();
$schema->loadXML('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns="http://www.portalfiscal.inf.br/nfe" targetNamespace="http://www.portalfiscal.inf.br/nfe" elementFormDefault="qualified"><xs:include/></xs:schema>');
$schema->documentElement->firstChild->setAttribute('schemaLocation', $xsdOriginal);
$elemento = $schema->importNode($pag, true);
$elemento->removeAttribute('minOccurs');
$elemento->removeAttribute('maxOccurs');
$schema->documentElement->appendChild($elemento);
$xsd = tempnam(sys_get_temp_dir(), 'centralvet-pag-');
$schema->save($xsd);

$comprovante = ['tpIntegra'=>2, 'CNPJ'=>'05577343000137', 'tBand'=>'02',
    'cAut'=>'000123', 'CNPJReceb'=>'31551765000143', 'idTermPag'=>'PDV-01'];
$casos = [];
foreach (['03','04','17','01','05','15','90','99'] as $tpag) {
    $casos[] = ['tPag'=>$tpag, 'valor_pago'=>$tpag === '90' ? 0 : 45,
        'xPag'=>$tpag === '99' ? 'Outros' : null];
}
$casos[] = ['tPag'=>'03', 'payment_details'=>$comprovante];
$casos[] = ['tPag'=>'17', 'payment_details'=>$comprovante];
$casos[] = ['tPag'=>'17', 'payment_details'=>$comprovante + []];
$casos[10]['payment_details']['cAut'] = str_repeat('E',128);
$casos[] = ['tPag'=>'01', 'payment_details'=>$comprovante];

try {
    foreach ($casos as $i=>$dados) {
        $make = new NFePHP\NFe\Make('PL_010_V1');
        $make->tagpag((object)['vTroco'=>null]);
        $no = $make->tagdetPag(centralvetPagamento($dados,45.0));
        $xml = $no->ownerDocument->saveXML($no);
        $documento = new DOMDocument();
        $documento->loadXML('<pag xmlns="http://www.portalfiscal.inf.br/nfe">'.$xml.'</pag>');
        conferir($documento->schemaValidate($xsd), 'XML de pagamento inválido no caso '.$i);
        $cards = $documento->getElementsByTagName('card');
        $eletronico = in_array($dados['tPag'], ['03','04','17'], true);
        conferir($cards->length === ($eletronico ? 1 : 0), 'Grupo eletrônico incorreto no caso '.$i);
        if ($eletronico) {
            conferir($documento->getElementsByTagName('tpIntegra')->item(0)->textContent === '2', 'Integração incorreta.');
            $fornecido = $dados['payment_details'] ?? [];
            foreach (['CNPJ','cAut','CNPJReceb','idTermPag'] as $campo) {
                $nos = $documento->getElementsByTagName($campo);
                if (isset($fornecido[$campo])) {
                    conferir($nos->length === 1 && $nos->item(0)->textContent === $fornecido[$campo], 'Dado alterado: '.$campo);
                } else {
                    conferir($nos->length === 0, 'Dado inventado: '.$campo);
                }
            }
            if ($dados['tPag'] === '17') conferir($documento->getElementsByTagName('tBand')->length === 0, 'Pix com bandeira.');
        }
    }
    echo count($casos)." casos de XML aprovados pelo schema real do NFePHP.\n";
} finally {
    unlink($xsd);
}
