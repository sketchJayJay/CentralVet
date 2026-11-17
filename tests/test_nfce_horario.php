<?php
declare(strict_types=1);
require dirname(__DIR__).'/fiscal_engine/emissao_horario.php';
$casos = [
    ['2026-11-18T00:30:00+00:00','2026-11-17T21:30:00-03:00'],
    ['2026-11-17T23:59:59-03:00','2026-11-17T23:59:59-03:00'],
    ['2026-11-17T15:45:00','2026-11-17T15:45:00-03:00'],
];
foreach ($casos as [$entrada,$esperada]) {
    if (centralvetDataHora($entrada) !== $esperada) throw new RuntimeException('Data ou fuso incorreto: '.$entrada);
}
echo count($casos)." casos de data/hora e virada do dia em Brasília aprovados.\n";
