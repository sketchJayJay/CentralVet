<?php
declare(strict_types=1);

function centralvetDataHora(string $valor = ''): string
{
    $fuso = new DateTimeZone('America/Sao_Paulo');
    $data = new DateTimeImmutable($valor !== '' ? $valor : 'now', $fuso);
    return $data->setTimezone($fuso)->format('Y-m-d\TH:i:sP');
}
